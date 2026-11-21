"""Conversations: people who wrote back (or whose mail we couldn't place in a thread), for any
signed-in teammate. The list has views (needs you, mine, open, handled, all); a conversation's
page shows the thread, the answers waiting in the Outbox, and the actions: assign to me, mark
handled, take over, write an answer.

Their mail is untrusted: it is shown escaped (Jinja autoescape) and never logged. Every change
is audited with ids, statuses and counts only — never a subject or body. Flash copy comes from
fixed keys, so nothing from the URL is echoed."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.catalog import Facts
from salesfinder.config import AppConfig
from salesfinder.db.models import Campaign, Company, Conversation, Message, Person, Sequence, User
from salesfinder.replies import conversations as service
from salesfinder.replies.conversations import NOT_FOUND, VIEWS, Action, View
from salesfinder.replies.draft import NOT_FOUND as DRAFT_NOT_FOUND
from salesfinder.replies.draft import write_reply
from salesfinder.web.deps import (
    AuthContext,
    AuthDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    FactsDep,
    SettingsDep,
    client_ip,
    retry_on_conflict,
    rollback_keeping_auth,
    utcnow,
)
from salesfinder.web.routes.outbox import refresh_stale_mx
from salesfinder.web.templating import render

router = APIRouter(prefix="/conversations")

PAGE_SIZE = 25
MAX_PAGE = 10_000
BODY_LIMIT = 10_000  # characters of a teammate's answer
VIEW_LABELS: dict[View, str] = {
    "needs_you": "Needs you",
    "mine": "Mine",
    "open": "Open",
    "handled": "Handled",
    "all": "All",
}
STATUS_LABELS = {
    "open": "Open",
    "needs_you": "Needs you",
    "handled": "Handled",
    "taken_over": "Taken over",
}
INTENT_LABELS = {
    "interested": "Interested",
    "objection": "Objection",
    "not_now": "Not now",
    "wrong_person": "Wrong person",
    "unsubscribe": "Unsubscribed",
    "auto_reply": "Auto-reply",
    "other": "Other",
}
ANSWER_LABELS = {
    "awaiting_approval": "Ready for approval",
    "gate_failed": "Held by the gate",
    "approved": "Approved — waiting to send",
}
FLASH = {
    "assigned": "Assigned to you.",
    "handled": "Marked handled. Claude's unsent drafts for it were cancelled.",
    "already_handled": "It was already handled.",
    "already_taken_over_handled": "Already taken over — Claude won't draft here. "
    "Nothing more to mark.",
    "taken_over": "Taken over: nothing more is sent or drafted for this person automatically.",
    "already_taken_over": "It was already taken over.",
    "reply_approved": "Your answer passed the gate and is approved: it goes out from the "
    "campaign's mailbox when sending allows.",
    "reply_held": "Your answer was saved but the gate holds it: see the reasons under "
    "“Answers waiting” and edit it in the Outbox.",
}
TOO_LONG = f"Keep the answer under {BODY_LIMIT:,} characters."


def _flash(request: Request) -> str | None:
    msg = request.query_params.get("msg")
    return FLASH.get(msg) if msg else None


def _refused(exc: ValueError) -> HTTPException:
    if str(exc) == NOT_FOUND:
        return HTTPException(status_code=404, detail=NOT_FOUND)
    return HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- list


@router.get("", response_class=HTMLResponse)
async def conversations_page(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    view: View = "needs_you",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> HTMLResponse:
    rows, total = await service.list_conversations(
        db, view=view, user_id=auth.user.id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    context = {
        "auth": auth,
        "rows": rows,
        "total": total,
        "view": view,
        "views": [(v, VIEW_LABELS[v]) for v in VIEWS],
        "page": page,
        "pages": pages,
        "prev_url": f"/conversations?view={view}&page={min(page - 1, pages)}" if page > 1 else None,
        "next_url": f"/conversations?view={view}&page={page + 1}" if page < pages else None,
        "status_labels": STATUS_LABELS,
        "intent_labels": INTENT_LABELS,
    }
    return render(request, "conversations.html", context)


# --------------------------------------------------------------------------- one


async def _render(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    config: AppConfig,
    facts: Facts,
    claude_bin: str,
    error: str | None = None,
    body: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    row = (
        await db.execute(
            select(Conversation, Sequence, Campaign, Company, Person)
            .join(Sequence, Sequence.id == Conversation.sequence_id)
            .join(Campaign, Campaign.id == Sequence.campaign_id)
            .join(Company, Company.id == Sequence.company_id)
            .outerjoin(Person, Person.id == Sequence.person_id)
            .where(Conversation.id == conversation_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    conv, sequence, campaign, company, person = row
    now = utcnow()
    answers = await service.pending_answers(db, sequence.id)
    user_ids = {conv.assigned_to_id, conv.handled_by_id} | {a.written_by_id for a in answers}
    names = {
        u.id: u.name
        for u in await db.scalars(select(User).where(User.id.in_({i for i in user_ids if i})))
    }
    waiting = await service.waiting_for_claude(db, conv, config=config)
    reason = (
        await service.claude_waiting_reason(db, claude_bin=claude_bin, config=config, now=now)
        if waiting
        else None
    )
    side = "supplier" if campaign.side == "supplier" else "buyer"
    can_answer = (
        sequence.status == "replied" and campaign.status != "archived" and side in facts.personas
    )
    context: dict[str, Any] = {
        "auth": auth,
        "conv": conv,
        "sequence": sequence,
        "campaign": campaign,
        "company": company,
        "person": person,
        "thread": await service.thread(db, conv, sequence),
        "answers": answers,
        "names": names,
        "waiting": waiting,
        "waiting_reason": reason,
        "can_answer": can_answer,
        "status_labels": STATUS_LABELS,
        "intent_labels": INTENT_LABELS,
        "answer_labels": ANSWER_LABELS,
        "flash": None if error else _flash(request),
        "error": error,
        "body": body,
        "body_limit": BODY_LIMIT,
    }
    return render(request, "conversation.html", context, status_code=status_code)


@router.get("/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(
    request: Request,
    conversation_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    config: ConfigDep,
    facts: FactsDep,
    settings: SettingsDep,
) -> HTMLResponse:
    return await _render(
        request,
        auth,
        db,
        conversation_id,
        config=config,
        facts=facts,
        claude_bin=settings.claude_bin,
    )


# --------------------------------------------------------------------------- actions


async def _audit(
    db: AsyncSession,
    request: Request,
    auth: AuthContext,
    action: str,
    done: Action,
    **extra: Any,
) -> None:
    await audit.record(
        db,
        action=action,
        actor_id=auth.user.id,
        target_type="conversation",
        target_id=str(done.conversation.id),
        details={
            "sequence_id": str(done.conversation.sequence_id),
            "was": done.was,
            "status": done.conversation.status,
            **extra,
        },
        ip=client_ip(request),
    )


def _back(conversation_id: uuid.UUID, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/conversations/{conversation_id}?msg={msg}", status_code=303)


@router.post("/{conversation_id}/assign", response_model=None)
async def assign(
    request: Request, conversation_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> Response:
    async def attempt() -> Response:
        try:
            done = await service.assign_to(db, conversation_id, user=auth.user)
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        if done.changed:
            await _audit(
                db,
                request,
                auth,
                audit.CONVERSATION_ASSIGNED,
                done,
                assigned_to_id=str(auth.user.id),
            )
        await db.commit()
        return _back(conversation_id, "assigned")

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{conversation_id}/handled", response_model=None)
async def handled(
    request: Request, conversation_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> Response:
    async def attempt() -> Response:
        try:
            done = await service.mark_handled(db, conversation_id, actor=auth.user, now=utcnow())
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        if done.changed:
            await _audit(
                db, request, auth, audit.CONVERSATION_HANDLED, done, cancelled=done.cancelled
            )
        await db.commit()
        if done.changed:
            msg = "handled"
        elif done.was == "taken_over":
            msg = "already_taken_over_handled"
        else:
            msg = "already_handled"
        return _back(conversation_id, msg)

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{conversation_id}/take-over", response_model=None)
async def take_over(
    request: Request, conversation_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> Response:
    async def attempt() -> Response:
        try:
            done = await service.take_over(db, conversation_id, actor=auth.user, now=utcnow())
        except ValueError as exc:
            await db.rollback()
            raise _refused(exc) from None
        if done.changed:
            await _audit(
                db,
                request,
                auth,
                audit.CONVERSATION_TAKEN_OVER,
                done,
                reason="in_app",
                cancelled=done.cancelled,
                sequence_status=done.sequence_status,
            )
        await db.commit()
        return _back(conversation_id, "taken_over" if done.changed else "already_taken_over")

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{conversation_id}/reply", response_model=None)
async def reply(
    request: Request,
    conversation_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    facts: FactsDep,
    settings: SettingsDep,
) -> Response:
    form = await request.form()
    raw = form.get("body")
    body = raw if isinstance(raw, str) else ""

    async def refuse(error: str) -> HTMLResponse:
        await rollback_keeping_auth(db, auth)
        return await _render(
            request,
            auth,
            db,
            conversation_id,
            config=config,
            facts=facts,
            claude_bin=settings.claude_bin,
            error=error,
            body=body[:BODY_LIMIT],
            status_code=400,
        )

    if len(body) > BODY_LIMIT:
        return await refuse(TOO_LONG)
    # A stale mail-server check of the recipient's domain is refreshed first (outside this
    # request's transaction), so the gate sees fresh data.
    first_id = await db.scalar(
        select(Message.id)
        .join(Conversation, Conversation.sequence_id == Message.sequence_id)
        .where(Conversation.id == conversation_id)
        .order_by(Message.id)
        .limit(1)
    )
    await db.commit()
    if first_id is not None:
        await refresh_stale_mx(request, config, [first_id])

    async def attempt() -> Response:
        try:
            problems = await write_reply(
                db,
                conversation_id,
                body=body,
                actor=auth.user,
                now=utcnow(),
                facts=facts,
                config=config,
            )
        except ValueError as exc:
            if str(exc) in (NOT_FOUND, DRAFT_NOT_FOUND):
                await db.rollback()
                raise HTTPException(status_code=404, detail=NOT_FOUND) from None
            return await refuse(str(exc))
        conv = await db.get(Conversation, conversation_id)
        assert conv is not None  # noqa: S101 - locked by write_reply in this transaction
        written = await db.scalar(
            select(Message)
            .where(
                Message.sequence_id == conv.sequence_id,
                Message.kind == "reply",
                Message.written_by_id == auth.user.id,
            )
            .order_by(Message.step.desc())
            .limit(1)
        )
        await audit.record(
            db,
            action=audit.REPLY_WRITTEN,
            actor_id=auth.user.id,
            target_type="message",
            target_id=str(written.id) if written is not None else None,
            details={
                "conversation_id": str(conversation_id),
                "sequence_id": str(conv.sequence_id),
                "result": "held" if problems else "approved",
                "problems": len(problems),
            },
            ip=client_ip(request),
        )
        await db.commit()
        return _back(conversation_id, "reply_held" if problems else "reply_approved")

    return await retry_on_conflict(db, auth, attempt)
