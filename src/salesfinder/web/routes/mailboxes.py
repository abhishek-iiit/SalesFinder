"""Admin: the sending mailboxes. Add, edit, enable or disable one, check its domain's DNS and
test its connection. Every change is audited (never a password); refusals show the service's
message; flash copy comes from fixed keys. DNS and mail I/O never run inside a transaction."""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts import dns as dns_module
from salesfinder.contacts.dns import Resolve
from salesfinder.db.models import Mailbox, MailboxFolder, MailboxMessage
from salesfinder.db.models.mail import MAILBOX_FOLDERS
from salesfinder.mail import factory as mail_factory
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.readiness import Report, report_from_json
from salesfinder.sending import mailboxes as service
from salesfinder.web.deps import (
    AdminDep,
    AuthContext,
    ConfigDep,
    CsrfDep,
    DbDep,
    FactsDep,
    SettingsDep,
    client_ip,
    require_admin,
    retry_on_conflict,
    rollback_keeping_auth,
    utcnow,
)
from salesfinder.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

NO_MAILBOX = "No such mailbox."
CHECK_NAMES = ("spf", "dkim", "dmarc", "mx")
FLASH = {
    "created": "Mailbox added. It is disabled until you enable it.",
    "saved": "Mailbox saved.",
    "enabled": "Mailbox enabled.",
    "disabled": "Mailbox disabled.",
    "dns_ok": "DNS checks passed.",
    "dns_failed": "Some DNS checks failed. Hover a ✗ for the detail.",
    "dismissed": "Mail-reading error dismissed. It comes back if the next read fails again.",
}
FOLDER_LABELS = {"inbox": "INBOX", "sent": "Sent"}
ERROR_CHANGED = "The error changed — check it again."
# Mail we read but couldn't tie to anyone (a bounce we couldn't read, a mail we couldn't
# read at all): listed for a teammate, newest first, this far back.
UNPLACED_KINDS = ("unclear_bounce", "unreadable")
UNPLACED_LABELS = {
    "unclear_bounce": "Possible bounce we couldn't read",
    "unreadable": "A mail we couldn't read",
}
UNPLACED_DAYS = 30
UNPLACED_SHOWN = 50
FIELDS = (
    "address", "side", "username", "secret_name", "smtp_host", "smtp_port", "smtp_security",
    "imap_host", "imap_port", "sent_folder", "dkim_selector", "daily_max", "warmup_started_on",
)  # fmt: skip
FIELD_LABELS = {
    "smtp_port": "SMTP port",
    "imap_port": "IMAP port",
    "daily_max": "Daily maximum",
    "warmup_started_on": "Warm-up start date",
    "username": "Username",
    "dkim_selector": "DKIM selector",
    "side": "Side",
}
DEFAULTS: dict[str, Any] = {
    "address": "",
    "side": "buyer",
    "username": "",
    "secret_name": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "imap_host": "imap.gmail.com",
    "imap_port": "993",
    "sent_folder": "",
    "dkim_selector": "google",
    "daily_max": "30",
    "warmup_started_on": "",
}


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


def _mail(request: Request) -> MailFactory:
    """The app's mail transport (tests put a fake in ``app.state.mail``), built on first use."""
    mail = getattr(request.app.state, "mail", None)
    if not isinstance(mail, MailFactory):
        mail = mail_factory.default_mail()
        request.app.state.mail = mail
    return mail


def _resolve(request: Request, config: AppConfig) -> Resolve:
    resolve = getattr(request.app.state, "dns_resolve", None)
    if resolve is None:
        resolve = dns_module.default_resolve(config.contacts)
        request.app.state.dns_resolve = resolve
    return cast(Resolve, resolve)


@dataclass(frozen=True)
class Row:
    mailbox: Mailbox
    report: Report
    cap_today: int
    sent_today: int


async def _list_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    config: AppConfig,
    *,
    status_code: int = 200,
    error: str | None = None,
    test_result: dict[str, Any] | None = None,
) -> HTMLResponse:
    now = utcnow()
    boxes = list(await db.scalars(select(Mailbox).order_by(Mailbox.side, Mailbox.address)))
    sent = await service.sent_today_counts(db, now=now)
    rows = [
        Row(
            m,
            report_from_json(m.dns_report),
            service.ramp_cap_today(m, today=now.date(), config=config),
            sent.get(m.id, 0),
        )
        for m in boxes
    ]
    folder_errors = (
        await db.execute(
            select(MailboxFolder, Mailbox.address)
            .join(Mailbox, Mailbox.id == MailboxFolder.mailbox_id)
            .where(MailboxFolder.last_error.is_not(None))
            .order_by(Mailbox.address, MailboxFolder.folder)
        )
    ).all()
    unplaced = (
        await db.execute(
            select(MailboxMessage, Mailbox.address)
            .join(Mailbox, Mailbox.id == MailboxMessage.mailbox_id)
            .where(
                MailboxMessage.kind.in_(UNPLACED_KINDS),
                MailboxMessage.sequence_id.is_(None),
                MailboxMessage.created_at >= now - timedelta(days=UNPLACED_DAYS),
            )
            .order_by(MailboxMessage.created_at.desc(), MailboxMessage.id)
            .limit(UNPLACED_SHOWN)
        )
    ).all()
    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "rows": rows,
        "folder_errors": folder_errors,
        "folder_labels": FOLDER_LABELS,
        "unplaced": unplaced,
        "unplaced_labels": UNPLACED_LABELS,
        "unplaced_days": UNPLACED_DAYS,
        "check_names": CHECK_NAMES,
        "fresh": {m.id: service.readiness_is_fresh(m, now=now, config=config) for m in boxes},
        "flash": None if error else (FLASH.get(msg) if msg else None),
        "error": error,
        "test_result": test_result,
    }
    return render(request, "mailboxes.html", context, status_code=status_code)


@router.get("/mailboxes", response_class=HTMLResponse)
async def mailboxes_page(
    request: Request, auth: AdminDep, db: DbDep, config: ConfigDep
) -> HTMLResponse:
    return await _list_page(request, db, auth, config)


def _form_values(mailbox: Mailbox) -> dict[str, Any]:
    return {
        "address": mailbox.address,
        "side": mailbox.side,
        "username": mailbox.username,
        "secret_name": mailbox.secret_name,
        "smtp_host": mailbox.smtp_host,
        "smtp_port": str(mailbox.smtp_port),
        "smtp_security": mailbox.smtp_security,
        "imap_host": mailbox.imap_host,
        "imap_port": str(mailbox.imap_port),
        "sent_folder": mailbox.sent_folder or "",
        "dkim_selector": mailbox.dkim_selector,
        "daily_max": str(mailbox.daily_max),
        "warmup_started_on": (
            mailbox.warmup_started_on.isoformat() if mailbox.warmup_started_on else ""
        ),
    }


def _render_form(
    request: Request,
    auth: AuthContext,
    config: AppConfig,
    *,
    mailbox: Mailbox | None,
    values: dict[str, Any],
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context = {
        "auth": auth,
        "mailbox": mailbox,
        "v": values,
        "error": error,
        "max_daily": config.sending.max_daily_cap,
    }
    return render(request, "mailbox_form.html", context, status_code=status_code)


async def _parse(
    request: Request, config: AppConfig
) -> tuple[dict[str, Any], service.MailboxForm | str]:
    """The submitted fields (for re-showing the form) and the validated form or a message."""
    posted = await request.form()
    values = {k: str(posted.get(k, "")).strip() for k in FIELDS}
    data: dict[str, Any] = {k: v for k, v in values.items() if k not in ("sent_folder",)}
    data["sent_folder"] = values["sent_folder"] or None
    data["warmup_started_on"] = values["warmup_started_on"] or None
    try:
        form = service.MailboxForm.model_validate(
            data, context={"max_daily": config.sending.max_daily_cap}
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        field = str(first["loc"][0]) if first["loc"] else ""
        if first["type"] == "value_error":
            return values, str(first["msg"]).removeprefix("Value error, ")
        label = FIELD_LABELS.get(field, field.replace("_", " ").capitalize() or "A field")
        return values, f"Check the {label.lower()}: it isn't valid."
    return values, form


@router.get("/mailboxes/new", response_class=HTMLResponse)
async def new_mailbox(request: Request, auth: AdminDep, config: ConfigDep) -> HTMLResponse:
    return _render_form(request, auth, config, mailbox=None, values=dict(DEFAULTS))


@router.post("/mailboxes/new", response_model=None)
async def create(
    request: Request, auth: CsrfDep, db: DbDep, config: ConfigDep, facts: FactsDep
) -> Response:
    values, form = await _parse(request, config)
    if isinstance(form, str):
        return _render_form(
            request, auth, config, mailbox=None, values=values, error=form, status_code=400
        )

    async def attempt() -> Response:
        try:
            mailbox = await service.create_mailbox(db, form, actor=auth.user, facts=facts)
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            return _render_form(
                request, auth, config, mailbox=None, values=values, error=str(exc), status_code=400
            )
        await audit.record(
            db,
            action=audit.MAILBOX_CREATED,
            actor_id=auth.user.id,
            target_type="mailbox",
            target_id=str(mailbox.id),
            details=service.audit_details(mailbox, daily_max=mailbox.daily_max),
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse("/admin/mailboxes?msg=created", status_code=303)

    return await retry_on_conflict(db, auth, attempt)


async def _get(db: AsyncSession, mailbox_id: uuid.UUID) -> Mailbox:
    try:
        return await service.get_mailbox(db, mailbox_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=NO_MAILBOX) from None


@router.get("/mailboxes/{mailbox_id}", response_class=HTMLResponse)
async def edit_page(
    request: Request, mailbox_id: uuid.UUID, auth: AdminDep, db: DbDep, config: ConfigDep
) -> HTMLResponse:
    mailbox = await _get(db, mailbox_id)
    return _render_form(request, auth, config, mailbox=mailbox, values=_form_values(mailbox))


@router.post("/mailboxes/{mailbox_id}", response_model=None)
async def update(
    request: Request,
    mailbox_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    facts: FactsDep,
) -> Response:
    mailbox = await _get(db, mailbox_id)
    values, form = await _parse(request, config)
    # The address is fixed once created; the form shows it read-only.
    values["address"] = mailbox.address
    if isinstance(form, str):
        return _render_form(
            request, auth, config, mailbox=mailbox, values=values, error=form, status_code=400
        )

    changed = service.connection_changes(mailbox, form)
    was_enabled = mailbox.enabled

    async def attempt() -> Response:
        try:
            row = await service.update_mailbox(db, mailbox_id, form, actor=auth.user, facts=facts)
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            if str(exc) == service.GONE:
                raise HTTPException(status_code=404, detail=NO_MAILBOX) from None
            current = await _get(db, mailbox_id)
            return _render_form(
                request, auth, config, mailbox=current, values=values, error=str(exc),
                status_code=400,
            )  # fmt: skip
        await audit.record(
            db,
            action=audit.MAILBOX_UPDATED,
            actor_id=auth.user.id,
            target_type="mailbox",
            target_id=str(row.id),
            details=service.audit_details(
                row,
                daily_max=row.daily_max,
                changed=changed,
                disabled_by_change=was_enabled and not row.enabled,
            ),
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse("/admin/mailboxes?msg=saved", status_code=303)

    return await retry_on_conflict(db, auth, attempt)


async def _toggle(
    request: Request,
    mailbox_id: uuid.UUID,
    enabled: bool,
    auth: AuthContext,
    db: AsyncSession,
    config: AppConfig,
    settings: Any,
    facts: Any,
) -> Response:
    async def attempt() -> Response:
        try:
            mailbox = await service.set_enabled(
                db, mailbox_id, enabled, actor=auth.user, settings=settings, facts=facts
            )
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            if str(exc) == service.GONE:
                raise HTTPException(status_code=404, detail=NO_MAILBOX) from None
            return await _list_page(request, db, auth, config, status_code=400, error=str(exc))
        await audit.record(
            db,
            action=audit.MAILBOX_ENABLED if enabled else audit.MAILBOX_DISABLED,
            actor_id=auth.user.id,
            target_type="mailbox",
            target_id=str(mailbox.id),
            details=service.audit_details(mailbox),
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse(
            f"/admin/mailboxes?msg={'enabled' if enabled else 'disabled'}", status_code=303
        )

    return await retry_on_conflict(db, auth, attempt)


@router.post("/mailboxes/{mailbox_id}/enable", response_model=None)
async def enable(
    request: Request,
    mailbox_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    settings: SettingsDep,
    facts: FactsDep,
) -> Response:
    return await _toggle(request, mailbox_id, True, auth, db, config, settings, facts)


@router.post("/mailboxes/{mailbox_id}/disable", response_model=None)
async def disable(
    request: Request,
    mailbox_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    settings: SettingsDep,
    facts: FactsDep,
) -> Response:
    return await _toggle(request, mailbox_id, False, auth, db, config, settings, facts)


@router.post("/mailboxes/{mailbox_id}/check-dns", response_model=None)
async def check_dns(
    request: Request, mailbox_id: uuid.UUID, auth: CsrfDep, db: DbDep, config: ConfigDep
) -> Response:
    async def attempt() -> Response:
        await _get(db, mailbox_id)
        await db.commit()  # end the read transaction: DNS runs in none
        report = await service.refresh_readiness(
            _factory(request),
            mailbox_id,
            resolve=_resolve(request, config),
            now=utcnow(),
            contacts=config.contacts,
        )
        mailbox = await _get(db, mailbox_id)
        await audit.record(
            db,
            action=audit.MAILBOX_TESTED,
            actor_id=auth.user.id,
            target_type="mailbox",
            target_id=str(mailbox_id),
            details=service.audit_details(
                mailbox,
                check="dns",
                ok=report.ok,
                failed=[c.name for c in report.checks if not c.ok],
            ),
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse(
            f"/admin/mailboxes?msg={'dns_ok' if report.ok else 'dns_failed'}", status_code=303
        )

    return await retry_on_conflict(db, auth, attempt)


@router.post("/mailboxes/{mailbox_id}/test", response_model=None)
async def test_connection(
    request: Request,
    mailbox_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    settings: SettingsDep,
) -> Response:
    async def attempt() -> Response:
        mailbox = await _get(db, mailbox_id)
        creds = service.credentials(mailbox, settings, config)
        if creds is None:
            return await _list_page(
                request,
                db,
                auth,
                config,
                status_code=400,
                error=service.no_password(mailbox.secret_name),
            )
        address = mailbox.address
        details = service.audit_details(mailbox, check="connection")
        tested = service.connection_snapshot(mailbox)
        await db.commit()  # end the read transaction: SMTP and IMAP run in none
        problems = await service.test_connection(mailbox, creds, _mail(request))
        # A pass clears the "settings changed" hold (Ruling P6-V), if they didn't change again.
        cleared = not problems and await service.clear_settings_hold(db, mailbox_id, tested)
        await audit.record(
            db,
            action=audit.MAILBOX_TESTED,
            actor_id=auth.user.id,
            target_type="mailbox",
            target_id=str(mailbox_id),
            details={
                **details,
                "ok": not problems,
                "problems": len(problems),
                "hold_cleared": cleared,
            },
            ip=client_ip(request),
        )
        await db.commit()
        return await _list_page(
            request, db, auth, config,
            test_result={"address": address, "problems": problems},
        )  # fmt: skip

    return await retry_on_conflict(db, auth, attempt)


@router.post("/mailboxes/{mailbox_id}/folders/{folder}/dismiss", response_model=None)
async def dismiss_folder_error(
    request: Request,
    mailbox_id: uuid.UUID,
    folder: str,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
) -> Response:
    """Clear a folder's mail-reading error once an admin has seen it (including the sticky
    "Skipped a mail we couldn't read"). Compare-and-set: only the error the page showed is
    cleared; a different one is shown again (``ERROR_CHANGED``) instead. Only the folder row
    is locked; the read cursor is left alone. Audited with the (fixed, our own) error text."""
    if folder not in MAILBOX_FOLDERS:
        raise HTTPException(status_code=404, detail=NO_MAILBOX)
    form = await request.form()
    shown = form.get("error")
    shown = shown if isinstance(shown, str) else None

    async def attempt() -> Response:
        row = (
            await db.execute(
                select(MailboxFolder)
                .where(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.folder == folder)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            await db.rollback()
            raise HTTPException(status_code=404, detail=NO_MAILBOX)
        error = row.last_error
        if error is not None and error != shown:
            await rollback_keeping_auth(db, auth)
            return await _list_page(request, db, auth, config, status_code=409, error=ERROR_CHANGED)
        if error is not None:
            row.last_error = None
            await db.flush()
            await audit.record(
                db,
                action=audit.MAILBOX_ERROR_DISMISSED,
                actor_id=auth.user.id,
                target_type="mailbox",
                target_id=str(mailbox_id),
                details={"folder": folder, "error": error},
                ip=client_ip(request),
            )
        await db.commit()
        return RedirectResponse("/admin/mailboxes?msg=dismissed", status_code=303)

    return await retry_on_conflict(db, auth, attempt)
