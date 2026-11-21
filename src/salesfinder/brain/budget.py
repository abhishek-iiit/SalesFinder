"""Daily Claude call budget, counted from ``usage_events`` (one row per attempted call)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.brain.runtime import BrainError, BrainResult
from salesfinder.config import AppConfig
from salesfinder.db.models import UsageEvent

_TASK_MAX = 32
_ERROR_MAX = 200


class BudgetReached(BrainError):
    """Today's Claude call budget is spent: nothing was sent. Callers stop for the day (a
    ``BrainError``, so code that treats every Claude failure alike still catches it)."""

    def __init__(self, cap: int) -> None:
        super().__init__(f"Daily Claude budget reached ({cap} calls).")


def _utc_day(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def calls_today(db: AsyncSession, *, now: datetime) -> int:
    """Every recorded call (success or failure) whose ``at`` falls in ``now``'s UTC day."""
    start, end = _utc_day(now)
    count = await db.scalar(
        select(func.count())
        .select_from(UsageEvent)
        .where(UsageEvent.at >= start, UsageEvent.at < end)
    )
    return int(count or 0)


async def remaining_calls(db: AsyncSession, *, now: datetime, config: AppConfig) -> int:
    return max(0, config.usage.max_daily_claude_calls - await calls_today(db, now=now))


async def record_usage(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    task: str,
    ok: bool,
    duration_ms: int,
    result: BrainResult | None,
    error: str | None,
    campaign_id: uuid.UUID | None,
    at: datetime | None = None,
) -> None:
    """Record one attempted call in its own short transaction (never the caller's). ``at``
    defaults to the database clock; pass the caller's ``now`` to keep an injected clock and the
    budget check consistent. Only metadata is stored — never prompt or reply text."""
    event = UsageEvent(
        task=task[:_TASK_MAX],
        ok=ok,
        duration_ms=max(0, duration_ms),
        input_tokens=result.input_tokens if result else None,
        output_tokens=result.output_tokens if result else None,
        cost_usd=result.cost_usd if result else None,
        error=error[:_ERROR_MAX] if error is not None else None,
        campaign_id=campaign_id,
    )
    if at is not None:
        event.at = at
    async with db_factory() as db, db.begin():
        db.add(event)
