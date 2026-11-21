"""Pure sending policy: warm-up ramp, office hours, follow-up due dates and spacing."""

import random
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.models import Mailbox, Message


def ramp_cap(mailbox: Mailbox, *, today: date, config: AppConfig) -> int:
    """Today's cold-send cap for one mailbox (0 while warming up or without a start date)."""
    sending = config.sending
    if mailbox.warmup_started_on is None:
        return 0
    days = (today - mailbox.warmup_started_on).days
    if days < sending.warmup_days:
        return 0
    return min(
        mailbox.daily_max, sending.ramp_start + sending.ramp_step * (days - sending.warmup_days)
    )


def ramp_hold(mailbox: Mailbox, *, today: date, sent_today: int, config: AppConfig) -> str | None:
    if mailbox.warmup_started_on is None:
        return "Warm-up hasn't started (set its start date)."
    cap = ramp_cap(mailbox, today=today, config=config)
    if cap == 0:
        starts = mailbox.warmup_started_on + timedelta(days=config.sending.warmup_days)
        return f"Warming up: cold email starts on {starts.isoformat()}."
    if sent_today >= cap:
        return f"Today's cap reached ({sent_today}/{cap})."
    return None


# Cold sends that may have reached the wire: sent, bounced, in flight, or not yet confirmed.
COUNTED_STATUSES = ("sent", "bounced", "sending", "unconfirmed")


def cold_sent_today_filter(now: datetime) -> list[ColumnElement[bool]]:
    """WHERE clauses for first emails and follow-ups that count against ``now``'s UTC day:
    sent (or since bounced) that day, or claimed that day and still ``sending`` /
    ``unconfirmed`` (their ``sent_at`` isn't known). Replies never count against the ramp.
    The one rule: the sender's cap and the Mailboxes page both use it."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    when = func.coalesce(Message.sent_at, Message.claimed_at)
    return [
        Message.kind.in_(("first", "followup")),
        Message.status.in_(COUNTED_STATUSES),
        when >= start,
        when < start + timedelta(days=1),
    ]


async def sent_today(db: AsyncSession, mailbox_id: uuid.UUID, *, now: datetime) -> int:
    """First emails and follow-ups counted against ``now``'s UTC day (``cold_sent_today_filter``);
    replies never count against the ramp."""
    count = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.mailbox_id == mailbox_id, *cold_sent_today_filter(now))
    )
    return int(count or 0)


def in_office_hours(now: datetime, zones: Sequence[str], config: AppConfig) -> bool:
    """True iff it is a weekday inside office hours in every one of ``zones``."""
    sending = config.sending
    for name in zones:
        local = now.astimezone(ZoneInfo(name))
        if local.weekday() >= 5 or not sending.office_start <= local.time() < sending.office_end:
            return False
    return True


def followup_due_at(
    sent_at: datetime, zones: Sequence[str], *, business_days: int, config: AppConfig
) -> datetime:
    """``business_days`` weekdays after the local send date, at office start (in zones[0]), UTC."""
    zone = ZoneInfo(zones[0])
    day = sent_at.astimezone(zone).date()
    remaining = business_days
    while remaining > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    local = datetime.combine(day, config.sending.office_start, tzinfo=zone)
    return local.astimezone(UTC)


def next_spacing(rng: random.Random, config: AppConfig) -> timedelta:
    sending = config.sending
    low = min(sending.min_spacing_seconds, sending.max_spacing_seconds)
    high = max(sending.min_spacing_seconds, sending.max_spacing_seconds)
    return timedelta(seconds=rng.uniform(low, high))
