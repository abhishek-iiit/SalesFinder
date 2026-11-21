"""Quiet hours (Phase 7 Task 1): a window in the configured timezone, crossing midnight or not,
across both DST changeover nights, with ``end`` exclusive and ``start == end`` meaning none."""

from datetime import UTC, date, datetime, time

import pytest

from salesfinder.config import QuietHours
from salesfinder.ops.quiet import in_quiet_hours, quiet_line, quiet_period

DEFAULT = QuietHours()  # 22:00-07:00 America/New_York


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 14, 3, 30, tzinfo=UTC), True),  # 22:30 EST
        (datetime(2026, 11, 14, 12, 0, tzinfo=UTC), False),  # 07:00 EST: end is exclusive
        (datetime(2026, 11, 14, 11, 59, 59, tzinfo=UTC), True),  # 06:59:59 EST
        (datetime(2026, 11, 14, 2, 59, tzinfo=UTC), False),  # 21:59 EST
        (datetime(2026, 11, 14, 3, 0, tzinfo=UTC), True),  # 22:00 EST: start is inclusive
        (datetime(2026, 11, 14, 17, 0, tzinfo=UTC), False),  # 12:00 EST
        (datetime(2026, 7, 1, 2, 30, tzinfo=UTC), True),  # 22:30 EDT
        (datetime(2026, 7, 1, 11, 30, tzinfo=UTC), False),  # 07:30 EDT
        (datetime(2026, 3, 8, 6, 30, tzinfo=UTC), True),  # 01:30 EST, the night clocks go forward
        (datetime(2026, 3, 8, 11, 30, tzinfo=UTC), False),  # 07:30 EDT the same morning
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), True),  # 01:30 EDT, the night clocks go back
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), True),  # 01:30 EST, the repeated hour
    ],
)
def test_default_window_crosses_midnight(now: datetime, expected: bool) -> None:
    assert in_quiet_hours(now, DEFAULT) is expected


def test_supplier_office_hours_fall_inside_the_default_window() -> None:
    # 09:00-17:00 IST is 22:30-06:30 EST: the whole Indian office day is US-Eastern night.
    for hour_utc, minute in [(3, 30), (6, 0), (9, 0), (11, 29)]:
        assert in_quiet_hours(datetime(2026, 11, 16, hour_utc, minute, tzinfo=UTC), DEFAULT)


def test_start_equal_to_end_means_no_quiet_hours() -> None:
    quiet = QuietHours(start=time(9, 0), end=time(9, 0), timezone="UTC")
    for hour in range(24):
        assert not in_quiet_hours(datetime(2026, 11, 14, hour, 0, tzinfo=UTC), quiet)
    assert not in_quiet_hours(datetime(2026, 11, 14, 9, 0, tzinfo=UTC), quiet)


def test_window_not_crossing_midnight() -> None:
    quiet = QuietHours(start=time(13, 0), end=time(14, 0), timezone="America/New_York")
    at = datetime(2026, 11, 14, 18, 59, tzinfo=UTC)  # 13:59 EST
    assert in_quiet_hours(at, quiet)
    assert not in_quiet_hours(datetime(2026, 11, 14, 19, 0, tzinfo=UTC), quiet)  # 14:00 EST
    assert in_quiet_hours(datetime(2026, 11, 14, 18, 0, tzinfo=UTC), quiet)  # 13:00 EST
    assert not in_quiet_hours(datetime(2026, 11, 14, 17, 59, tzinfo=UTC), quiet)  # 12:59 EST
    assert not in_quiet_hours(datetime(2026, 11, 14, 3, 30, tzinfo=UTC), quiet)  # 22:30 EST


def test_other_timezone_input_is_converted() -> None:
    from zoneinfo import ZoneInfo

    # 09:00 IST on 2026-11-14 is 22:30 EST on the 13th.
    at = datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert in_quiet_hours(at, DEFAULT)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        in_quiet_hours(datetime(2026, 11, 14, 3, 30), DEFAULT)


def test_quiet_line_exact_text() -> None:
    assert quiet_line(DEFAULT) == (
        "Quiet hours (22:00\u201307:00 America/New_York): Claude steps wait until 07:00."
    )
    quiet = QuietHours(start=time(13, 5), end=time(14, 30), timezone="UTC")
    assert quiet_line(quiet) == "Quiet hours (13:05\u201314:30 UTC): Claude steps wait until 14:30."


def test_start_inside_the_spring_forward_gap() -> None:
    # 2026-03-08 in New York: 01:59:59 EST is followed by 03:00 EDT, so 02:30 never happens.
    # A window starting at 02:30 begins at the first wall-clock time past it: 03:00 EDT.
    quiet = QuietHours(start=time(2, 30), end=time(7, 0), timezone="America/New_York")
    assert not in_quiet_hours(datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC), quiet)  # 01:59:59 EST
    assert in_quiet_hours(datetime(2026, 3, 8, 7, 0, tzinfo=UTC), quiet)  # 03:00 EDT
    assert not in_quiet_hours(datetime(2026, 3, 8, 11, 0, tzinfo=UTC), quiet)  # 07:00 EDT


def test_end_inside_the_spring_forward_gap() -> None:
    # Quiet until 02:30 that night ends at the jump: 01:59:59 EST is quiet, 03:00 EDT isn't.
    quiet = QuietHours(start=time(22, 0), end=time(2, 30), timezone="America/New_York")
    assert in_quiet_hours(datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC), quiet)  # 01:59:59 EST
    assert not in_quiet_hours(datetime(2026, 3, 8, 7, 0, tzinfo=UTC), quiet)  # 03:00 EDT


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 14, 3, 30, tzinfo=UTC), date(2026, 11, 13)),  # Fri 22:30 EST
        (datetime(2026, 11, 14, 11, 0, tzinfo=UTC), date(2026, 11, 13)),  # Sat 06:00: same night
        (datetime(2026, 11, 14, 12, 0, tzinfo=UTC), None),  # 07:00: over
        (datetime(2026, 11, 15, 3, 0, tzinfo=UTC), date(2026, 11, 14)),  # the next night
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), date(2026, 10, 31)),  # the repeated hour
    ],
)
def test_quiet_period_is_the_local_start_date(now: datetime, expected: date | None) -> None:
    """Keys "once per quiet period" (the worker's quiet line)."""
    assert quiet_period(now, DEFAULT) == expected


def test_quiet_period_of_a_daytime_window() -> None:
    noon = QuietHours(start=time(12, 0), end=time(14, 0), timezone="America/New_York")
    assert quiet_period(datetime(2026, 11, 7, 18, 0, tzinfo=UTC), noon) == date(2026, 11, 7)
    assert quiet_period(datetime(2026, 11, 7, 19, 0, tzinfo=UTC), noon) is None
    never = QuietHours(start=time(9), end=time(9))
    assert quiet_period(datetime(2026, 11, 7, 18, 0, tzinfo=UTC), never) is None
