"""Quiet hours: the window (in ``usage.quiet_hours.timezone``) when the worker's Claude steps
wait, leaving headroom for the operator's own Claude use. Everything else keeps running."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from salesfinder.config import QuietHours


def in_quiet_hours(now: datetime, quiet: QuietHours) -> bool:
    """Whether ``now`` (timezone-aware) falls in the quiet window: ``start`` inclusive, ``end``
    exclusive, crossing midnight when ``start > end``; ``start == end`` means no quiet hours.
    The wall-clock time in the configured zone decides, so DST nights follow local time."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("in_quiet_hours needs a timezone-aware datetime")
    if quiet.start == quiet.end:
        return False
    t = now.astimezone(ZoneInfo(quiet.timezone)).time()
    if quiet.start < quiet.end:
        return quiet.start <= t < quiet.end
    return t >= quiet.start or t < quiet.end


def quiet_period(now: datetime, quiet: QuietHours) -> date | None:
    """The local date the quiet period holding ``now`` started on (the day before, for the
    after-midnight part of a window crossing midnight), or None outside quiet hours. It keys
    "once per quiet period": two passes in different periods never share it."""
    if not in_quiet_hours(now, quiet):
        return None
    local = now.astimezone(ZoneInfo(quiet.timezone))
    if quiet.start > quiet.end and local.time() < quiet.end:
        return local.date() - timedelta(days=1)
    return local.date()


def quiet_line(quiet: QuietHours) -> str:
    return (
        f"Quiet hours ({quiet.start:%H:%M}\u2013{quiet.end:%H:%M} {quiet.timezone}): "
        f"Claude steps wait until {quiet.end:%H:%M}."
    )
