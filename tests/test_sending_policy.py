import asyncio
import random
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import seed_campaign, seed_company, seed_draft, seed_mailbox, seed_user
from salesfinder.config import AppConfig
from salesfinder.db.models import Company, Mailbox, Message, Observation
from salesfinder.sending import policy, state, timezones

CONFIG = AppConfig()
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
ET, PT = "America/New_York", "America/Los_Angeles"


def _box(start: date | None, daily_max: int = 30) -> Mailbox:
    return Mailbox(address="a@x.org", warmup_started_on=start, daily_max=daily_max)


def _on(day: int) -> tuple[Mailbox, date]:
    start = date(2026, 1, 1)
    return _box(start), start + timedelta(days=day)


@pytest.mark.parametrize(
    ("day", "cap"), [(0, 0), (13, 0), (14, 5), (15, 8), (22, 29), (23, 30), (40, 30)]
)
def test_ramp_cap(day: int, cap: int) -> None:
    box, today = _on(day)
    assert policy.ramp_cap(box, today=today, config=CONFIG) == cap


def test_ramp_cap_respects_daily_max_and_missing_start() -> None:
    start = date(2026, 1, 1)
    assert policy.ramp_cap(_box(start, 50), today=start + timedelta(days=29), config=CONFIG) == 50
    assert policy.ramp_cap(_box(None), today=start, config=CONFIG) == 0


def test_ramp_hold_texts() -> None:
    assert policy.ramp_hold(_box(None), today=date(2026, 1, 1), sent_today=0, config=CONFIG) == (
        "Warm-up hasn't started (set its start date)."
    )
    box, today = _on(3)
    assert policy.ramp_hold(box, today=today, sent_today=0, config=CONFIG) == (
        "Warming up: cold email starts on 2026-01-15."
    )
    box, today = _on(14)
    assert policy.ramp_hold(box, today=today, sent_today=4, config=CONFIG) is None
    assert policy.ramp_hold(box, today=today, sent_today=5, config=CONFIG) == (
        "Today's cap reached (5/5)."
    )


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_office_hours_dst_and_edges() -> None:
    assert policy.in_office_hours(_utc(2026, 3, 9, 13, 30), [ET], CONFIG)  # 09:30 EDT
    assert not policy.in_office_hours(_utc(2026, 3, 6, 13, 30), [ET], CONFIG)  # 08:30 EST
    assert not policy.in_office_hours(_utc(2026, 11, 13, 16, 59, 59), ["Asia/Kolkata"], CONFIG)
    assert policy.in_office_hours(_utc(2026, 11, 13, 4, 0), ["Asia/Kolkata"], CONFIG)
    # A UTC Friday that is a Saturday in India.
    assert not policy.in_office_hours(_utc(2026, 11, 13, 22, 0), ["Asia/Kolkata"], CONFIG)
    assert not policy.in_office_hours(_utc(2026, 11, 13, 14, 0), timezones.UNKNOWN_BUYER, CONFIG)
    assert policy.in_office_hours(_utc(2026, 11, 13, 17, 0), timezones.UNKNOWN_BUYER, CONFIG)
    assert not policy.in_office_hours(_utc(2026, 11, 13, 22, 0), [ET], CONFIG)  # 17:00 exactly
    # Every zone must be in hours: 09:00 ET is 06:00 PT.
    assert not policy.in_office_hours(_utc(2026, 11, 13, 14, 0), [ET, PT], CONFIG)
    assert policy.in_office_hours(_utc(2026, 11, 13, 17, 0), [ET, PT], CONFIG)


def test_followup_due_dates() -> None:
    sent = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)  # Fri 15:00 CST
    due = policy.followup_due_at(sent, ["America/Chicago"], business_days=4, config=CONFIG)
    assert due == _utc(2026, 11, 19, 15, 0)
    # Across the DST change on Sun 2026-11-01: Fri 10-30 15:00 CDT, due Thu 11-05 09:00 CST.
    sent = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    due = policy.followup_due_at(sent, ["America/Chicago"], business_days=4, config=CONFIG)
    assert due == _utc(2026, 11, 5, 15, 0)
    # Due Tue 10-27 at 09:00 CDT = 14:00 UTC (no DST shift in between).
    sent = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    due = policy.followup_due_at(sent, ["America/Chicago"], business_days=3, config=CONFIG)
    assert due == _utc(2026, 10, 27, 14, 0)


def test_next_spacing_within_bounds() -> None:
    rng = random.Random(1)  # noqa: S311
    for _ in range(50):
        gap = policy.next_spacing(rng, CONFIG)
        assert timedelta(seconds=180) <= gap <= timedelta(seconds=540)


def test_state_zone_tables() -> None:
    assert len(timezones.STATE_ZONES) == 51
    assert timezones.STATE_ZONES["TX"] == "America/Chicago"
    assert timezones.STATE_ZONES["FL"] == ET
    assert timezones.STATE_ZONES["IN"] == "America/Indiana/Indianapolis"


async def _company(db: AsyncSession, side: str, **fields: str | None) -> Company:
    company, _, _ = await seed_company(db, side=side, domain=f"{uuid.uuid4().hex[:8]}.com")
    for key, value in fields.items():
        setattr(company, key, value)
    await db.flush()
    return company


async def test_recipient_zones(db: AsyncSession) -> None:
    tx = await _company(db, "buyer")
    db.add(
        Observation(
            company_id=tx.id,
            code="US_LOCATION",
            value={"state": "TX", "city": "Dallas"},
            is_latest=True,
        )
    )
    ca = await _company(db, "buyer", region="California")
    nowhere = await _company(db, "buyer")
    th = await _company(db, "supplier", country="TH")
    unknown = await _company(db, "supplier")
    await db.flush()
    assert await timezones.recipient_zones(db, tx) == ("America/Chicago",)
    assert await timezones.recipient_zones(db, ca) == (PT,)
    assert await timezones.recipient_zones(db, nowhere) == timezones.UNKNOWN_BUYER
    assert await timezones.recipient_zones(db, th) == ("Asia/Bangkok",)
    assert await timezones.recipient_zones(db, unknown) == timezones.UNKNOWN_SUPPLIER


async def test_state_defaults_paused_and_resumes(db: AsyncSession) -> None:
    s = await state.get_state(db)
    assert (s.paused, s.reason) == (True, "Sending hasn't been turned on yet.")
    user = await seed_user(db)
    s, changed = await state.resume(db, actor=user, now=NOW)
    assert changed and not s.paused
    assert s.resumed_at == NOW and s.changed_by_id == user.id
    assert (await state.get_state(db)) == s
    s, changed = await state.pause(db, reason="Taking a break", actor=user, now=NOW)
    assert changed
    assert s.paused and s.reason == "Taking a break" and s.resumed_at == NOW
    for bad in ("", "x" * 201, "two\nlines"):
        with pytest.raises(ValueError):
            await state.pause(db, reason=bad, actor=None, now=NOW)


async def _sends(db: AsyncSession, outcomes: list[tuple[str, datetime]]) -> None:
    campaign = await seed_campaign(db)
    for status, sent_at in outcomes:
        msg = await seed_draft(db, campaign, domain=f"{uuid.uuid4().hex[:8]}.com")
        msg.status = status
        msg.sent_at = sent_at
    await db.flush()


async def test_bounce_trip(db: AsyncSession) -> None:
    config = AppConfig.model_validate({"sending": {"bounce_window": 10, "bounce_max_rate": 0.2}})
    resumed = NOW
    await state.resume(db, actor=None, now=resumed)
    before = resumed - timedelta(hours=1)
    # Old bounces (before resume) are ignored.
    await _sends(db, [("bounced", before)] * 5)
    after = [("sent", resumed + timedelta(minutes=i + 1)) for i in range(8)]
    await _sends(db, [*after, ("bounced", resumed + timedelta(minutes=20))])
    await _sends(db, [("bounced", resumed + timedelta(minutes=21))])
    assert await state.bounce_rate(db, now=NOW, config=config) == (2, 10)
    assert await state.trip_if_bouncing(db, now=NOW, config=config) is None
    assert not (await state.get_state(db)).paused
    await _sends(db, [("bounced", resumed + timedelta(minutes=22))])
    reason = await state.trip_if_bouncing(db, now=NOW, config=config)
    assert reason == "Paused automatically: 3 of the last 10 emails bounced (30%)."
    paused = await state.get_state(db)
    assert paused.paused and paused.reason == reason and paused.changed_by_id is None


async def test_sent_today_counts_first_and_followup_only(db: AsyncSession) -> None:
    user = await seed_user(db)
    box = await seed_mailbox(db, user)
    campaign = await seed_campaign(db)
    msgs: list[Message] = []
    for _ in range(3):
        msgs.append(await seed_draft(db, campaign, domain=f"{uuid.uuid4().hex[:8]}.com"))
    for msg, status, when in zip(
        msgs,
        ("sent", "bounced", "sent"),
        (NOW, NOW - timedelta(hours=3), NOW - timedelta(days=1)),
        strict=True,
    ):
        msg.mailbox_id, msg.status, msg.sent_at = box.id, status, when
    await db.flush()
    assert await policy.sent_today(db, box.id, now=NOW) == 2


async def test_pause_when_paused_keeps_first_reason(db: AsyncSession) -> None:
    first, changed = await state.pause(db, reason="First", actor=None, now=NOW)
    assert changed
    again, changed = await state.pause(
        db, reason="Second", actor=None, now=NOW + timedelta(hours=1)
    )
    assert not changed
    assert again == first
    assert (await state.get_state(db)).reason == "First"


async def test_first_ever_pauses_from_two_sessions(database_url: str, db: AsyncSession) -> None:
    from sqlalchemy import func, select

    from salesfinder.db.engine import make_engine, make_session_factory
    from salesfinder.db.models import AppSetting

    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    try:

        async def _pause(reason: str) -> bool:
            async with factory() as s:
                _, changed = await state.pause(s, reason=reason, actor=None, now=NOW)
                await s.commit()
                return changed

        results = await asyncio.gather(_pause("A"), _pause("B"))
        assert sorted(results) == [False, True]
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(AppSetting)) == 1
            assert (await state.get_state(s)).reason in ("A", "B")
    finally:
        await engine.dispose()
        async with make_session_factory(make_engine(database_url))() as s:
            await s.execute(delete(AppSetting))
            await s.commit()


async def test_resume_refused_after_an_auto_pause(db: AsyncSession) -> None:
    on, _ = await state.resume(db, actor=None, now=NOW)
    shown = on.changed_at
    paused, _ = await state.pause(
        db, reason="Paused automatically: 3 of the last 50 emails bounced (6%).",
        actor=None, now=NOW + timedelta(minutes=5),
    )  # fmt: skip
    with pytest.raises(ValueError, match="changed meanwhile"):
        await state.resume(
            db, actor=None, now=NOW + timedelta(minutes=6), expected_changed_at=shown
        )
    assert await state.get_state(db) == paused
    again, changed = await state.resume(
        db, actor=None, now=NOW + timedelta(minutes=7), expected_changed_at=paused.changed_at
    )
    assert changed and not again.paused


async def test_resume_when_already_on_keeps_the_bounce_window(db: AsyncSession) -> None:
    first, changed = await state.resume(db, actor=None, now=NOW)
    assert changed and first.resumed_at == NOW
    later = NOW + timedelta(hours=3)
    again, changed = await state.resume(db, actor=None, now=later)
    assert not changed and again == first
    assert (await state.get_state(db)).resumed_at == NOW
    # The compare-and-set still applies before the no-op.
    with pytest.raises(ValueError, match="changed meanwhile"):
        await state.resume(db, actor=None, now=later, expected_changed_at=None)
    same, changed = await state.resume(
        db, actor=None, now=later, expected_changed_at=first.changed_at
    )
    assert not changed and same == first


async def test_two_bounces_among_three_sends_do_not_trip(db: AsyncSession) -> None:
    await state.resume(db, actor=None, now=NOW)
    await _sends(
        db,
        [
            ("sent", NOW + timedelta(minutes=1)),
            ("bounced", NOW + timedelta(minutes=2)),
            ("bounced", NOW + timedelta(minutes=3)),
        ],
    )
    assert await state.bounce_rate(db, now=NOW, config=CONFIG) == (2, 3)
    assert await state.trip_if_bouncing(db, now=NOW, config=CONFIG) is None  # 2/50 = 4%


async def test_default_window_trips_at_three_of_fifty(db: AsyncSession) -> None:
    await state.resume(db, actor=None, now=NOW)
    sends = [("sent", NOW + timedelta(minutes=i + 1)) for i in range(47)]
    bounces = [("bounced", NOW + timedelta(minutes=100 + i)) for i in range(3)]
    await _sends(db, [*sends, *bounces])
    reason = await state.trip_if_bouncing(db, now=NOW, config=CONFIG)
    assert reason == "Paused automatically: 3 of the last 50 emails bounced (6%)."


async def test_trip_when_already_paused_is_a_noop(db: AsyncSession) -> None:
    await state.resume(db, actor=None, now=NOW)
    await _sends(db, [("bounced", NOW + timedelta(minutes=i + 1)) for i in range(5)])
    paused, _ = await state.pause(db, reason="By hand", actor=None, now=NOW)
    assert await state.trip_if_bouncing(db, now=NOW, config=CONFIG) is None
    assert await state.get_state(db) == paused


def test_state_code_longest_name_and_bare_codes() -> None:
    assert timezones._state_code("Washington, DC") == "DC"
    assert timezones._state_code("Washington") == "WA"
    assert timezones._state_code("West Virginia") == "WV"
    assert timezones._state_code("Austin, TX") == "TX"
    assert timezones._state_code("TX") == "TX"
    for prose in ("in", "or", "based in the south or east", "me"):
        assert timezones._state_code(prose) is None
