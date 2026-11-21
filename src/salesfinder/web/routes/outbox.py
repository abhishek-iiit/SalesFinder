"""The Outbox, in three tabs:

- **To review**: first emails, follow-ups that fell due and drafted answers to replies. Approve,
  re-check, reject, edit and bulk approve; every one re-runs the gate on live state.
- **Approved — waiting to send**: what the sender will send, with its mailbox's hold reason;
  Withdraw takes an approval back.
- **Problems**: sends that couldn't be confirmed ("It was sent" / "Send again") or failed
  ("Send again").

Nothing is sent here: the worker's sender does that.

Before Approve, Re-check, a saved edit and a bulk approval run the gate, a recipient domain
whose mail-server check is stale is looked up again (its own short sessions, outside this
request's transaction), so the gate sees fresh data; if DNS can't answer, the gate holds as
usual.

Every change is audited with the message id, campaign and recipient only — never the subject,
body or a reject reason's text. Flash messages come from fixed keys (and, for a bulk approval,
small validated counts), so nothing from the URL is ever echoed."""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import FormData

from salesfinder import audit
from salesfinder.catalog import Facts, Side
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, refresh_mx, stale_mx_domains
from salesfinder.db.models import (
    Campaign,
    Company,
    Conversation,
    Mailbox,
    Message,
    Person,
    Sequence,
)
from salesfinder.outbox import service
from salesfinder.outbox.service import (
    KINDS,
    REFUSALS,
    REVIEW_SEQUENCE,
    STALE_EDIT,
    VIEWS,
    Kind,
    OutboxItem,
    View,
    list_outbox,
    outbox_tab_counts,
)
from salesfinder.sending import state as sending_state
from salesfinder.web.deps import (
    AuthContext,
    AuthDep,
    CatalogDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    FactsDep,
    client_ip,
    retry_on_conflict,
    rollback_keeping_auth,
    utcnow,
)
from salesfinder.web.templating import render
from salesfinder.writer.compose import SIGNATURE_MARKER, footer, signature

router = APIRouter(prefix="/outbox")
log = structlog.get_logger(__name__)

PAGE_SIZE = 25
MAX_PAGE = 10_000
BULK_LIMIT = 50  # ids per bulk approval: each takes row and advisory locks until the commit
NO_MESSAGE = "No such email."
TOO_MANY = f"Approve at most {BULK_LIMIT} emails at a time."
BAD_SELECTION = "That selection isn't valid. Reload the Outbox and try again."
REVIEWABLE = ("awaiting_approval", "gate_failed")
STATUS_LABELS = {
    "awaiting_approval": "Ready",
    "gate_failed": "Held by the gate",
    "approved": "Approved",
    "unconfirmed": "Not confirmed",
    "failed": "Failed",
}
KIND_LABELS = {"first": "First email", "followup": "Follow-up", "reply": "Reply"}
VIEW_LABELS = {
    "review": "To review",
    "approved": "Approved — waiting to send",
    "problems": "Problems",
}
KIND_FILTER_LABELS = {
    "all": "All kinds",
    "first": "First emails",
    "followup": "Follow-ups",
    "reply": "Replies",
}
UNCONFIRMED_COPY = "We couldn't confirm this went out — check the mailbox's Sent folder"
NO_MAILBOX = "No sending mailbox is set for its campaign."
# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {
    "approved": "Approved. It is sent from its mailbox when sending allows (see the Approved tab).",
    "held": "Not approved: the gate held this email. The reasons are on its card below.",
    "ready": "Re-checked: the email passed the gate and is ready to approve.",
    "still_held": "Re-checked: the gate still holds this email. The reasons are on its card.",
    "saved": "Saved. The email passed the gate and is ready to approve.",
    "rejected": "Rejected. The reason will guide this campaign's next drafts.",
    "none": "Select at least one email to approve.",
    "withdrawn": "Approval withdrawn: the email is back in To review.",
    "marked_sent": "Marked as sent.",
    "resent": "It will be sent again (same Message-ID), after the gate re-checks it.",
}
EDIT_FLASH = {"saved_held": "Saved, but the gate holds this email: see the reasons below."}


# --------------------------------------------------------------------------- helpers


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _count(value: object, limit: int) -> int | None:
    """A small non-negative whole number from the URL, or None."""
    if not isinstance(value, str) or not value.isdigit() or len(value) > 4:
        return None
    number = int(value)
    return number if number <= limit else None


def _list_state(
    campaign: object = None, page: object = None, view: object = None, kind: object = None
) -> dict[str, str]:
    """The Outbox list's tab, kind filter, campaign filter and page, validated (anything else
    is dropped; the defaults are left out)."""
    state: dict[str, str] = {}
    if isinstance(view, str) and view in VIEWS and view != "review":
        state["view"] = view
    if isinstance(kind, str) and kind in KINDS and kind != "all":
        state["kind"] = kind
    campaign_id = _uuid_or_none(campaign)
    if campaign_id is not None:
        state["campaign"] = str(campaign_id)
    number = _count(page, MAX_PAGE)
    if number is not None and number > 1:
        state["page"] = str(number)
    return state


def _list_url(
    campaign: object = None,
    page: object = None,
    *,
    view: object = None,
    kind: object = None,
    anchor: str | None = None,
    **params: object,
) -> str:
    """``/outbox`` with the list's own (validated) state plus ``params``."""
    query = _list_state(campaign, page, view, kind)
    query.update({key: str(value) for key, value in params.items()})
    url = "/outbox" + (f"?{urlencode(query)}" if query else "")
    return url + (f"#{anchor}" if anchor else "")


def _back(form: FormData, *, anchor: uuid.UUID | None = None, **params: object) -> str:
    return _list_url(
        form.get("campaign"),
        form.get("page"),
        view=form.get("view"),
        kind=form.get("kind"),
        anchor=f"m-{anchor}" if anchor else None,
        **params,
    )


def _flash(query: Mapping[str, str]) -> str | None:
    msg = query.get("msg")
    if msg == "bulk":
        ok, held, skipped = (_count(query.get(k), BULK_LIMIT) for k in ("ok", "held", "skipped"))
        if ok is None or held is None or skipped is None:
            return None
        text = f"Approved {ok}; {held} held — see below." if held else f"Approved {ok}."
        if skipped:
            text += f" {skipped} skipped (no longer waiting, or couldn't be checked)."
        return text
    return FLASH.get(msg) if msg else None


def written_part(body: str, *, facts: Facts, config: AppConfig, side: Side) -> str:
    """What a teammate edits: the stored body without the signature and footer that code
    appends (the exact current tail, else everything from the last signature marker on, so the
    footer is never offered as editable text even after facts.yaml changed)."""
    foot = footer(facts, config.writing)
    persona = facts.personas.get(side)
    if persona is not None:
        tail = signature(facts, side) + foot
        if body.endswith(tail):
            return body[: len(body) - len(tail)]
    head, marker, _ = body.rpartition(SIGNATURE_MARKER)
    if marker:
        return head
    return body[: len(body) - len(foot)] if body.endswith(foot) else body


def _side(campaign: Campaign) -> Side:
    return "supplier" if campaign.side == "supplier" else "buyer"


async def _target(db: AsyncSession, message_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    """The message's campaign id and recipient (for the audit row), or 404."""
    row = (
        await db.execute(
            select(Sequence.campaign_id, Sequence.recipient)
            .join(Message, Message.sequence_id == Sequence.id)
            .where(Message.id == message_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=NO_MESSAGE)
    return row.campaign_id, row.recipient


def _details(campaign_id: uuid.UUID, recipient: str, **extra: Any) -> dict[str, Any]:
    return {"campaign_id": str(campaign_id), "recipient": recipient, **extra}


async def _audit(
    db: AsyncSession,
    request: Request,
    auth: AuthContext,
    action: str,
    message_id: uuid.UUID | None,
    details: dict[str, Any],
) -> None:
    await audit.record(
        db,
        action=action,
        actor_id=auth.user.id,
        target_type="message" if message_id else None,
        target_id=str(message_id) if message_id else None,
        details=details,
        ip=client_ip(request),
    )


def _refused(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _mx_checker(request: Request, config: AppConfig) -> MxChecker:
    """The app's MX checker (tests put one with a fake resolver in ``app.state``), built on
    first use."""
    checker = getattr(request.app.state, "mx_checker", None)
    if not isinstance(checker, MxChecker):
        checker = MxChecker(config.contacts)
        request.app.state.mx_checker = checker
    return checker


async def refresh_stale_mx(
    request: Request, config: AppConfig, message_ids: list[uuid.UUID]
) -> None:
    """Look up again the stale mail-server checks of these messages' recipient domains (at
    most ``BULK_LIMIT`` domains) before the gate runs. Call it before the request's session
    starts a transaction: it reads and writes in its own short sessions while DNS runs. A
    failure is logged (its type only) and the gate then holds as usual."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    now = utcnow()
    try:
        async with factory() as db:
            recipients = (
                await db.scalars(
                    select(Sequence.recipient)
                    .join(Message, Message.sequence_id == Sequence.id)
                    .where(Message.id.in_(message_ids))
                )
            ).all()
            stale = await stale_mx_domains(db, recipients, now=now, config=config.contacts)
        if stale:
            await refresh_mx(
                factory,
                _mx_checker(request, config),
                stale[:BULK_LIMIT],
                now=now,
                config=config.contacts,
            )
    except Exception as exc:
        log.warning("outbox_mx_refresh_failed", error_type=type(exc).__name__)


# --------------------------------------------------------------------------- list


async def _holds(db: AsyncSession, items: list[OutboxItem]) -> dict[uuid.UUID, str]:
    """Why each approved email may be waiting: its mailbox's hold reason (or that it is
    disabled, or that its campaign has none). A reply is held only by a missing or disabled
    mailbox (replies aren't held by the ramp, office hours or DNS)."""
    rows = await db.execute(
        select(Campaign.id, Campaign.mailbox_id).where(
            Campaign.id.in_({item.sequence.campaign_id for item in items})
        )
    )
    campaign_boxes: dict[uuid.UUID, uuid.UUID | None] = {
        campaign_id: box_id for campaign_id, box_id in rows.all()
    }
    chosen = {
        item.message.id: item.message.mailbox_id
        or item.sequence.mailbox_id
        or campaign_boxes.get(item.sequence.campaign_id)
        for item in items
    }
    wanted = {box_id for box_id in chosen.values() if box_id is not None}
    boxes = {box.id: box for box in await db.scalars(select(Mailbox).where(Mailbox.id.in_(wanted)))}
    holds: dict[uuid.UUID, str] = {}
    for item in items:
        box_id = chosen[item.message.id]
        box = boxes.get(box_id) if box_id else None
        if box is None:
            holds[item.message.id] = NO_MAILBOX
        elif not box.enabled:
            holds[item.message.id] = f"Its mailbox {box.address} is disabled."
        elif box.hold_reason and item.kind != "reply":
            holds[item.message.id] = f"{box.address}: {box.hold_reason}"
    return holds


async def _conversations(db: AsyncSession, items: list[OutboxItem]) -> dict[uuid.UUID, uuid.UUID]:
    """Each reply's conversation id, by sequence id (for a link to it)."""
    ids = {item.sequence.id for item in items if item.kind == "reply"}
    if not ids:
        return {}
    rows = await db.execute(
        select(Conversation.sequence_id, Conversation.id).where(Conversation.sequence_id.in_(ids))
    )
    return {sequence_id: conversation_id for sequence_id, conversation_id in rows.all()}


@router.get("", response_class=HTMLResponse)
async def outbox_page(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    campaign: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    view: View = "review",
    kind: Kind = "all",
) -> HTMLResponse:
    items, total = await list_outbox(
        db,
        campaign_id=campaign,
        kind=kind,
        view=view,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    campaigns = (
        await db.execute(select(Campaign.id, Campaign.name).order_by(Campaign.name, Campaign.id))
    ).all()
    campaign_str = campaign and str(campaign)
    state = _list_state(campaign_str, str(page), view, kind)
    counts = await outbox_tab_counts(db, campaign_id=campaign, kind=kind)
    context = {
        "auth": auth,
        "items": items,
        "total": total,
        "campaigns": campaigns,
        "campaign_filter": campaign,
        "page": page,
        "pages": pages,
        "view": view,
        "kind": kind,
        "tabs": [
            (v, VIEW_LABELS[v], counts[v], _list_url(campaign_str, None, view=v, kind=kind))
            for v in VIEWS
        ],
        "kind_filters": KIND_FILTER_LABELS,
        "kind_labels": KIND_LABELS,
        "prev_url": _list_url(campaign_str, str(min(page - 1, pages)), view=view, kind=kind)
        if page > 1
        else None,
        "next_url": _list_url(campaign_str, str(page + 1), view=view, kind=kind)
        if page < pages
        else None,
        "any_ready": any(item.message.status == "awaiting_approval" for item in items),
        "status_labels": STATUS_LABELS,
        "unconfirmed_copy": UNCONFIRMED_COPY,
        "holds": await _holds(db, items) if view == "approved" else {},
        "conversations": await _conversations(db, items),
        "sending": await sending_state.get_state(db),
        "flash": _flash(request.query_params),
        "bulk_limit": BULK_LIMIT,
        # The list's state, carried by every form and Edit link so actions come back here.
        "state": state,
        "state_query": _query(state),
    }
    return render(request, "outbox.html", context)


def _query(params: dict[str, str]) -> str:
    return f"?{urlencode(params)}" if params else ""


# --------------------------------------------------------------------------- actions


@router.post("/bulk-approve", response_model=None)
async def bulk_approve(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    facts: FactsDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> Response:
    form = await request.form()
    raw = form.getlist("ids")
    if not raw:
        return RedirectResponse(_back(form, msg="none"), status_code=303)
    if len(raw) > BULK_LIMIT:
        raise HTTPException(status_code=400, detail=TOO_MANY)
    ids = [_uuid_or_none(value) for value in raw]
    if any(message_id is None for message_id in ids):
        raise HTTPException(status_code=400, detail=BAD_SELECTION)
    wanted = [message_id for message_id in ids if message_id is not None]
    await refresh_stale_mx(request, config, wanted)

    async def attempt() -> Response:
        targets = {
            row.id: (row.campaign_id, row.recipient)
            for row in (
                await db.execute(
                    select(Message.id, Sequence.campaign_id, Sequence.recipient)
                    .join(Sequence, Sequence.id == Message.sequence_id)
                    .where(Message.id.in_(wanted))
                )
            ).all()
        }
        results = await service.bulk_approve(
            db, wanted, actor=auth.user, now=utcnow(), facts=facts, config=config, catalog=catalog
        )
        approved = held = skipped = 0
        for message_id, problems in results.items():
            refused = len(problems) == 1 and problems[0] in REFUSALS
            if refused or message_id not in targets:
                # A refusal (missing, no longer waiting, archived campaign, no persona, no
                # catalog): nothing changed, so it is skipped rather than held.
                skipped += 1
                continue
            campaign_id, recipient = targets[message_id]
            if problems:
                held += 1
                action = audit.MESSAGE_HELD
                details = _details(campaign_id, recipient, problems=len(problems), bulk=True)
            else:
                approved += 1
                action = audit.MESSAGE_APPROVED
                details = _details(campaign_id, recipient, bulk=True)
            await _audit(db, request, auth, action, message_id, details)
        counts = {"approved": approved, "held": held, "skipped": skipped}
        await _audit(db, request, auth, audit.MESSAGE_BULK_APPROVED, None, counts)
        await db.commit()
        url = _back(form, msg="bulk", ok=approved, held=held, skipped=skipped)
        return RedirectResponse(url, status_code=303)

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{message_id}/approve", response_model=None)
async def approve(
    request: Request,
    message_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    facts: FactsDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> Response:
    form = await request.form()
    await refresh_stale_mx(request, config, [message_id])

    async def attempt() -> Response:
        campaign_id, recipient = await _target(db, message_id)
        try:
            problems = await service.approve(
                db, message_id, actor=auth.user, now=utcnow(), facts=facts, config=config,
                catalog=catalog,
            )  # fmt: skip
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        if problems:
            action = audit.MESSAGE_HELD
            details = _details(campaign_id, recipient, problems=len(problems))
        else:
            action, details = audit.MESSAGE_APPROVED, _details(campaign_id, recipient)
        await _audit(db, request, auth, action, message_id, details)
        await db.commit()
        msg = "held" if problems else "approved"
        return RedirectResponse(
            _back(form, anchor=message_id if problems else None, msg=msg), status_code=303
        )

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{message_id}/recheck", response_model=None)
async def recheck(
    request: Request,
    message_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    facts: FactsDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> Response:
    form = await request.form()
    await refresh_stale_mx(request, config, [message_id])

    async def attempt() -> Response:
        campaign_id, recipient = await _target(db, message_id)
        try:
            problems = await service.recheck(
                db, message_id, by=auth.user, now=utcnow(), facts=facts, config=config,
                catalog=catalog,
            )  # fmt: skip
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        result = "held" if problems else "ready"
        details = _details(campaign_id, recipient, result=result, problems=len(problems))
        await _audit(db, request, auth, audit.MESSAGE_RECHECKED, message_id, details)
        await db.commit()
        msg = "still_held" if problems else "ready"
        return RedirectResponse(_back(form, anchor=message_id, msg=msg), status_code=303)

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{message_id}/reject", response_model=None)
async def reject(request: Request, message_id: uuid.UUID, auth: CsrfDep, db: DbDep) -> Response:
    form = await request.form()
    reason = form.get("reason")

    async def attempt() -> Response:
        campaign_id, recipient = await _target(db, message_id)
        try:
            await service.reject(
                db,
                message_id,
                reason=reason if isinstance(reason, str) else "",
                actor=auth.user,
                now=utcnow(),
            )
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        stored = await db.get(Message, message_id)
        length = len(stored.reject_reason or "") if stored is not None else 0
        details = _details(campaign_id, recipient, reason_length=length)
        await _audit(db, request, auth, audit.MESSAGE_REJECTED, message_id, details)
        await db.commit()
        return RedirectResponse(_back(form, msg="rejected"), status_code=303)

    return await retry_on_conflict(db, auth, attempt)


# --------------------------------------------------------------------------- edit


async def _render_edit(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    facts: Facts,
    config: AppConfig,
    back: Mapping[str, str],
    subject: str | None = None,
    body: str | None = None,
    version: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The edit page. ``back`` is the list's campaign filter and page (validated); ``version``
    the submitted ``updated_at`` (kept on a refused save, so a stale form stays stale)."""
    row = (
        await db.execute(
            select(Message, Sequence, Campaign, Company, Person)
            .join(Sequence, Sequence.id == Message.sequence_id)
            .join(Campaign, Campaign.id == Sequence.campaign_id)
            .join(Company, Company.id == Sequence.company_id)
            .outerjoin(Person, Person.id == Sequence.person_id)
            .where(Message.id == message_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=NO_MESSAGE)
    message, sequence, campaign, company, person = row
    side = _side(campaign)
    editable = message.status in REVIEWABLE and sequence.status == REVIEW_SEQUENCE.get(message.kind)
    tail = (
        signature(facts, side) + footer(facts, config.writing)
        if side in facts.personas
        else footer(facts, config.writing)
    )
    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "message": message,
        "sequence": sequence,
        "campaign": campaign,
        "company": company,
        "person": person,
        "editable": editable,
        "subject": message.subject if subject is None else subject,
        "body": (
            written_part(message.body, facts=facts, config=config, side=side)
            if body is None
            else body
        ),
        "tail": tail.strip("\n"),
        "version": message.updated_at.isoformat() if version is None else version,
        "back": back,
        "list_url": _list_url(
            back.get("campaign"), back.get("page"), view=back.get("view"), kind=back.get("kind")
        ),
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "error": error,
        "flash": None if error else (EDIT_FLASH.get(msg) if msg else None),
    }
    return render(request, "outbox_message.html", context, status_code=status_code)


@router.get("/{message_id}", response_class=HTMLResponse)
async def edit_page(
    request: Request,
    message_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    facts: FactsDep,
    config: ConfigDep,
) -> HTMLResponse:
    params = request.query_params
    back = _list_state(
        params.get("campaign"), params.get("page"), params.get("view"), params.get("kind")
    )
    return await _render_edit(request, auth, db, message_id, facts=facts, config=config, back=back)


@router.post("/{message_id}", response_model=None)
async def save_edit(
    request: Request,
    message_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    facts: FactsDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> Response:
    form = await request.form()
    subject, body, version = form.get("subject"), form.get("body"), form.get("version")
    subject = subject if isinstance(subject, str) else ""
    body = body if isinstance(body, str) else ""
    version = version if isinstance(version, str) else ""
    back = _list_state(form.get("campaign"), form.get("page"), form.get("view"), form.get("kind"))
    await refresh_stale_mx(request, config, [message_id])

    async def attempt() -> Response:
        campaign_id, recipient = await _target(db, message_id)
        try:
            try:
                expected = datetime.fromisoformat(version)
            except ValueError:
                raise ValueError(STALE_EDIT) from None
            if expected.tzinfo is None:
                raise ValueError(STALE_EDIT)
            problems = await service.edit(
                db, message_id, subject=subject, body=body, actor=auth.user, now=utcnow(),
                facts=facts, config=config, catalog=catalog, expected_updated_at=expected,
            )  # fmt: skip
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            return await _render_edit(
                request,
                auth,
                db,
                message_id,
                facts=facts,
                config=config,
                back=back,
                subject=subject,
                body=body,
                version=version,
                error=str(exc),
                status_code=400,
            )
        result = "held" if problems else "ready"
        details = _details(campaign_id, recipient, result=result, problems=len(problems))
        await _audit(db, request, auth, audit.MESSAGE_EDITED, message_id, details)
        await db.commit()
        if problems:
            query = _query({**back, "msg": "saved_held"})
            return RedirectResponse(f"/outbox/{message_id}{query}", status_code=303)
        url = _list_url(
            back.get("campaign"), back.get("page"), view=back.get("view"), kind=back.get("kind"),
            msg="saved",
        )  # fmt: skip
        return RedirectResponse(url, 303)

    return await retry_on_conflict(db, auth, attempt)


# --------------------------------------------------------------------------- after approval


async def _settle(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    act: Callable[[], Awaitable[None]],
    action: str,
    msg: str,
) -> Response:
    """Run ``act`` (a service call that flushes) on the message, audit ``action``, commit and
    go back to the tab it was done from; a refusal is a 400 with the service's text."""
    form = await request.form()

    async def attempt() -> Response:
        campaign_id, recipient = await _target(db, message_id)
        try:
            await act()
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        await _audit(db, request, auth, action, message_id, _details(campaign_id, recipient))
        await db.commit()
        return RedirectResponse(_back(form, msg=msg), status_code=303)

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{message_id}/withdraw", response_model=None)
async def withdraw(request: Request, message_id: uuid.UUID, auth: CsrfDep, db: DbDep) -> Response:
    async def act() -> None:
        await service.withdraw(db, message_id, actor=auth.user, now=utcnow())

    return await _settle(
        request, auth, db, message_id, act=act, action=audit.MESSAGE_WITHDRAWN, msg="withdrawn"
    )


@router.post("/{message_id}/mark-sent", response_model=None)
async def mark_sent(
    request: Request, message_id: uuid.UUID, auth: CsrfDep, db: DbDep, config: ConfigDep
) -> Response:
    async def act() -> None:
        await service.mark_sent(db, message_id, actor=auth.user, now=utcnow(), config=config)

    return await _settle(
        request, auth, db, message_id, act=act, action=audit.MESSAGE_MARKED_SENT, msg="marked_sent"
    )


@router.post("/{message_id}/resend", response_model=None)
async def resend(request: Request, message_id: uuid.UUID, auth: CsrfDep, db: DbDep) -> Response:
    async def act() -> None:
        await service.resend(db, message_id, actor=auth.user, now=utcnow())

    return await _settle(
        request, auth, db, message_id, act=act, action=audit.MESSAGE_RESEND, msg="resent"
    )
