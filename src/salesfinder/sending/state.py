"""The global sending state (``app_settings["sending"]``) and the bounce kill switch.

Sending is paused until an admin resumes it. Services flush; callers commit and audit."""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.models import AppSetting, Message, User

KEY = "sending"
NOT_TURNED_ON = "Sending hasn't been turned on yet."
REASON_MAX = 200
STATE_CHANGED = "Sending state changed meanwhile — check it and try again."


class _Unchecked(enum.Enum):
    UNCHECKED = enum.auto()


# ``resume``'s default: no compare-and-set. Passing None instead means "the state shown was
# never written" (a fresh install), which is checked like any other value.
UNCHECKED: Final = _Unchecked.UNCHECKED


@dataclass(frozen=True)
class SendingState:
    paused: bool
    reason: str
    changed_at: datetime | None = None
    changed_by_id: uuid.UUID | None = None
    resumed_at: datetime | None = None


def _when(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _who(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _parse(value: Any) -> SendingState:
    if not isinstance(value, dict):
        return SendingState(paused=True, reason=NOT_TURNED_ON)
    paused = bool(value.get("paused", True))
    reason = str(value.get("reason") or "") if paused else ""
    if paused and not reason:
        reason = "Paused."
    return SendingState(
        paused=paused,
        reason=reason,
        changed_at=_when(value.get("changed_at")),
        changed_by_id=_who(value.get("changed_by_id")),
        resumed_at=_when(value.get("resumed_at")),
    )


async def get_state(db: AsyncSession) -> SendingState:
    row = await db.get(AppSetting, KEY)
    return _parse(row.value if row is not None else None)


def _encode(state: SendingState) -> dict[str, Any]:
    return {
        "paused": state.paused,
        "reason": state.reason,
        "changed_at": state.changed_at.isoformat() if state.changed_at else None,
        "changed_by_id": str(state.changed_by_id) if state.changed_by_id else None,
        "resumed_at": state.resumed_at.isoformat() if state.resumed_at else None,
    }


async def _locked_row(db: AsyncSession) -> AppSetting:
    """The ``sending`` row, created paused if missing, locked ``FOR UPDATE`` and freshly read.
    Every writer goes through here, so two first-ever writers never collide."""
    await db.execute(
        pg_insert(AppSetting)
        .values(key=KEY, value=_encode(_parse(None)))
        .on_conflict_do_nothing(index_elements=["key"])
    )
    return (
        await db.execute(
            select(AppSetting)
            .where(AppSetting.key == KEY)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _write(
    db: AsyncSession, row: AppSetting, state: SendingState, actor: User | None
) -> SendingState:
    row.value = _encode(state)
    row.updated_by_id = actor.id if actor else None
    await db.flush()
    return state


async def pause(
    db: AsyncSession, *, reason: str, actor: User | None, now: datetime
) -> tuple[SendingState, bool]:
    """Pause sending. Returns (state, changed). Already paused by someone: the first reason and
    actor win, nothing is written and ``changed`` is False (callers audit only a change)."""
    reason = reason.strip()
    if not 1 <= len(reason) <= REASON_MAX or "\n" in reason or "\r" in reason:
        raise ValueError(f"The reason must be one line of 1 to {REASON_MAX} characters.")
    row = await _locked_row(db)
    current = _parse(row.value)
    if current.paused and current.changed_at is not None:
        return current, False
    state = SendingState(
        paused=True,
        reason=reason,
        changed_at=now,
        changed_by_id=actor.id if actor else None,
        resumed_at=current.resumed_at,
    )
    return await _write(db, row, state, actor), True


async def resume(
    db: AsyncSession,
    *,
    actor: User | None,
    now: datetime,
    expected_changed_at: datetime | _Unchecked | None = UNCHECKED,
) -> tuple[SendingState, bool]:
    """Turn sending on. Returns (state, changed); callers audit only a change. The bounce
    window starts here: older sends no longer count. Already on: nothing is written (the
    bounce window and ``resumed_at`` are kept) and ``changed`` is False. When
    ``expected_changed_at`` is given (the ``changed_at`` the caller showed; None for a state
    never written) and the state changed since, refuse with ``STATE_CHANGED``."""
    row = await _locked_row(db)
    current = _parse(row.value)
    if expected_changed_at is not UNCHECKED and current.changed_at != expected_changed_at:
        raise ValueError(STATE_CHANGED)
    if not current.paused:
        return current, False
    state = SendingState(
        paused=False,
        reason="",
        changed_at=now,
        changed_by_id=actor.id if actor else None,
        resumed_at=now,
    )
    return await _write(db, row, state, actor), True


async def bounce_rate(db: AsyncSession, *, now: datetime, config: AppConfig) -> tuple[int, int]:
    """(bounces, window): over the last ``bounce_window`` outgoing messages sent since sending
    was last resumed."""
    del now  # the window is by count, not by time
    state = await get_state(db)
    stmt = select(Message.status).where(
        Message.status.in_(("sent", "bounced")), Message.sent_at.is_not(None)
    )
    if state.resumed_at is not None:
        stmt = stmt.where(Message.sent_at > state.resumed_at)
    stmt = stmt.order_by(Message.sent_at.desc(), Message.id).limit(config.sending.bounce_window)
    statuses = list((await db.scalars(stmt)).all())
    return sum(1 for s in statuses if s == "bounced"), len(statuses)


async def trip_if_bouncing(db: AsyncSession, *, now: datetime, config: AppConfig) -> str | None:
    """Pause sending when more than ``bounce_max_rate`` of the last ``bounce_window`` sends
    bounced. Returns the reason (the caller audits ``sending.auto_paused``), else None."""
    row = await _locked_row(db)
    if _parse(row.value).paused:
        return None
    bounces, _ = await bounce_rate(db, now=now, config=config)
    window = config.sending.bounce_window
    if bounces / window <= config.sending.bounce_max_rate:
        return None
    pct = round(100 * bounces / window)
    reason = f"Paused automatically: {bounces} of the last {window} emails bounced ({pct}%)."
    await pause(db, reason=reason, actor=None, now=now)
    return reason
