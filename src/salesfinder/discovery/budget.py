"""The monthly Serper budget: the configured (or admin-overridden) cap and this month's spend.

Budget months are UTC calendar months. Services flush; callers commit.
"""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import DiscoveryConfig
from salesfinder.db.models import AppSetting, DiscoveryJob, User
from salesfinder.discovery.queries import MONEY_QUANTUM

BUDGET_KEY = "discovery.monthly_budget_usd"
MAX_BUDGET = Decimal("10000")
BUDGET_RANGE = "Enter a budget between $0 and $10,000."
SPENT_STATUSES = ("running", "succeeded", "failed", "stopped_budget")
ZERO = Decimal(0)


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of the UTC calendar month containing ``now`` (naive means UTC)."""
    at = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


async def get_budget(db: AsyncSession, config: DiscoveryConfig) -> Decimal:
    """The admin override from app_settings, else ``config.monthly_budget_usd``."""
    row = await db.get(AppSetting, BUDGET_KEY, populate_existing=True)
    if row is not None:
        try:
            value = Decimal(str(row.value))
        except (InvalidOperation, ValueError):
            value = None
        if value is not None and value.is_finite() and ZERO <= value <= MAX_BUDGET:
            return value
    return config.monthly_budget_usd


async def set_budget(db: AsyncSession, value: Decimal, *, actor: User) -> None:
    """Upsert the override. Stored as decimal text in JSONB so no float ever touches it."""
    if not isinstance(value, Decimal) or not value.is_finite() or not ZERO <= value <= MAX_BUDGET:
        raise ValueError(BUDGET_RANGE)
    # Never round the cap up: cut to the 0.0001 precision every stored amount uses.
    text_value = format(value.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN), "f")
    stmt = insert(AppSetting).values(key=BUDGET_KEY, value=text_value, updated_by_id=actor.id)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value, "updated_by_id": actor.id, "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.flush()


async def month_spend(
    db: AsyncSession, *, now: datetime, exclude_job_id: uuid.UUID | None = None
) -> Decimal:
    """Actual cost of jobs started this UTC month that ran (running, succeeded, failed,
    stopped_budget), plus the estimate of every approved job not yet started (it will spend
    soon, whenever it was approved). ``exclude_job_id`` leaves one job out entirely."""
    start, end = month_bounds(now)
    spent = select(func.coalesce(func.sum(DiscoveryJob.actual_cost_usd), 0)).where(
        DiscoveryJob.status.in_(SPENT_STATUSES),
        DiscoveryJob.started_at >= start,
        DiscoveryJob.started_at < end,
    )
    reserved = select(func.coalesce(func.sum(DiscoveryJob.estimated_cost_usd), 0)).where(
        DiscoveryJob.status == "approved"
    )
    if exclude_job_id is not None:
        spent = spent.where(DiscoveryJob.id != exclude_job_id)
        reserved = reserved.where(DiscoveryJob.id != exclude_job_id)
    return Decimal(await db.scalar(spent) or 0) + Decimal(await db.scalar(reserved) or 0)


async def remaining_budget(db: AsyncSession, *, now: datetime, config: DiscoveryConfig) -> Decimal:
    """What is left this month, never below zero."""
    left = await get_budget(db, config) - await month_spend(db, now=now)
    return max(left, ZERO)
