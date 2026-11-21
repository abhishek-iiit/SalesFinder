"""Today page, health check, and the emergency stop (pause sending, any teammate)."""

import asyncio
from datetime import timedelta
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder import audit
from salesfinder.brain.budget import calls_today
from salesfinder.campaigns.service import list_campaigns
from salesfinder.db.models import (
    Mailbox,
    MailboxFolder,
    MailboxMessage,
    Message,
    Sequence,
)
from salesfinder.ops import health
from salesfinder.ops import runs as runs_service
from salesfinder.outbox.service import waiting_for_review
from salesfinder.replies.conversations import count_conversations, list_conversations
from salesfinder.sending import state as sending_state
from salesfinder.sending.mailboxes import not_read
from salesfinder.web.deps import (
    AuthDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    SettingsDep,
    client_ip,
    page_worker_health,
    read_worker_health,
    retry_on_conflict,
    utcnow,
)
from salesfinder.web.sending_view import sending_view
from salesfinder.web.templating import render

router = APIRouter()

HEALTH_TIMEOUT_SECONDS = 5
# The Today Worker card's latest failures.
FAILURES_SHOWN = 5


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """``{"status": "ok", "worker": <state>}``; 503 ``db_unavailable`` when the database can't
    be reached. The worker's state is "unknown" if only reading it failed (the status code
    stays 200: the web app itself is fine)."""
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS), factory() as db:
            await db.execute(text("SELECT 1"))
            worker = await read_worker_health(request, db)
            await db.rollback()
    except (SQLAlchemyError, OSError):  # OSError covers refused connections and TimeoutError
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "worker": worker.state if worker else "unknown"})


# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {
    "paused": "Sending is paused. An admin resumes it from Settings.",
    "already_paused": "Sending was already paused.",
}
# Where the pause form may come back to.
PAUSE_BACK = ("/", "/admin/settings")
DEFAULT_PAUSE_REASON = "Paused from the dashboard."
NEEDS_YOU_SHOWN = 10


def _flash(request: Request) -> str | None:
    msg = request.query_params.get("msg")
    return FLASH.get(msg) if msg else None


async def _count(db: AsyncSession, *where: ColumnElement[bool], join: bool = False) -> int:
    stmt = select(func.count()).select_from(Message)
    if join:
        stmt = stmt.join(Sequence, Sequence.id == Message.sequence_id)
    return int(await db.scalar(stmt.where(*where)) or 0)


@router.get("/", response_class=HTMLResponse)
async def today(
    request: Request, auth: AuthDep, db: DbDep, config: ConfigDep, settings: SettingsDep
) -> HTMLResponse:
    now = utcnow()
    summaries = await list_campaigns(db, now=now)
    needs_you, _ = await list_conversations(
        db, view="needs_you", user_id=auth.user.id, limit=NEEDS_YOU_SHOWN
    )
    # The same filter as the Conversations page's "Mine" view, which the count links to.
    mine = await count_conversations(db, view="mine", user_id=auth.user.id)
    replies = await db.scalar(
        select(func.count())
        .select_from(MailboxMessage)
        .where(
            MailboxMessage.kind == "reply", MailboxMessage.created_at >= now - timedelta(hours=24)
        )
    )
    mail_errors = (
        await db.execute(
            select(Mailbox.address, MailboxFolder.folder, MailboxFolder.last_error)
            .join(Mailbox, Mailbox.id == MailboxFolder.mailbox_id)
            .where(MailboxFolder.last_error.is_not(None))
            .order_by(Mailbox.address, MailboxFolder.folder)
        )
    ).all()
    worker = await page_worker_health(request, db, auth)
    context = {
        "auth": auth,
        # The Worker card (it shows the worker banner on this page).
        "worker": worker,
        "worker_line": health.running_line(worker, now=now) if worker is not None else None,
        "failures_24h": await runs_service.count_recent_failures(db, now=now),
        "recent_failures": await runs_service.recent_failures(db, now=now, limit=FAILURES_SHOWN),
        # Mailboxes with a password held untested: their mail isn't read (P6-V/W).
        "not_read": await not_read(db, settings, config),
        "flash": _flash(request),
        "sending": await sending_view(db, now=now, config=config),
        # Counted once per page for the nav (a full-page GET always has them).
        "ready": auth.outbox_ready,
        "held": auth.outbox_held,
        "drafted": sum(s.drafted_today for s in summaries),
        "calls": await calls_today(db, now=now),
        "call_budget": config.usage.max_daily_claude_calls,
        "approved": await _count(db, Message.status == "approved"),
        "followups_due": await _count(
            db, *waiting_for_review(), Message.kind == "followup", join=True
        ),
        "replies_24h": int(replies or 0),
        "needs_you": needs_you,
        "needs_all": auth.needs_you,
        "needs_mine": mine,
        "unconfirmed": await _count(db, Message.status == "unconfirmed"),
        "failed": await _count(db, Message.status == "failed"),
        "mail_errors": mail_errors,
        # Each campaign's last writing error (cleared when it next stores a draft).
        "errors": [
            s for s in summaries if s.campaign.last_error and s.campaign.status != "archived"
        ],
    }
    return render(request, "today.html", context)


@router.post("/sending/pause", response_model=None)
async def pause_sending(request: Request, auth: CsrfDep, db: DbDep) -> Response:
    """The emergency stop: any signed-in teammate can pause all sending (only an admin
    resumes it). Already paused: nothing changes and nothing is audited."""
    form = await request.form()
    raw = form.get("reason")
    reason = (raw if isinstance(raw, str) else "").strip() or DEFAULT_PAUSE_REASON
    back_raw = form.get("back")
    back = back_raw if isinstance(back_raw, str) and back_raw in PAUSE_BACK else "/"

    async def attempt() -> Response:
        try:
            state, changed = await sending_state.pause(
                db, reason=reason, actor=auth.user, now=utcnow()
            )
        except ValueError as exc:
            await db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if changed:
            await audit.record(
                db,
                action=audit.SENDING_PAUSED,
                actor_id=auth.user.id,
                target_type="setting",
                target_id=sending_state.KEY,
                details={"source": "web", "reason": state.reason},
                ip=client_ip(request),
            )
        await db.commit()
        msg = "paused" if changed else "already_paused"
        return RedirectResponse(f"{back}?msg={msg}", status_code=303)

    return await retry_on_conflict(db, auth, attempt)
