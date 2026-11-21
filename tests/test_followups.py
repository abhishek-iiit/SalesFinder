"""Follow-ups (Task 7): a due step 2 is gated into the Outbox with ``Re: <step 1 subject>``,
never sent until a teammate approves it, and then threads under step 1. Real DB, FakeSender,
FakeResolver, fixed clocks."""

import email
import random
import uuid
from datetime import UTC, datetime, timedelta
from email import policy as email_policy
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import CATALOG, CONFIG, FACTS
from fakes.dns import FakeResolver
from fakes.mail import FakeSender, fake_mail
from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    HubSpotSync,
    Mailbox,
    Message,
    Sequence,
    User,
)
from salesfinder.followups.service import followup_subject, queue_due
from salesfinder.outbox import service as outbox
from salesfinder.sending.sender import CAMPAIGN_PAUSED, SendOutcome, claim, send_pass
from salesfinder.settings import Settings
from salesfinder.suppressions.service import suppress
from sending_helpers import RECIPIENT, SEND_NOW, Ready, seed_ready, sending_settings

STEP1_SUBJECT = "Surfactants for your body wash line"
FIRST_ID = "<first@send-domain.com>"
DRAFTED_SUBJECT = "A quick follow-up"
DUE = datetime(2026, 11, 16, 14, tzinfo=UTC)  # 09:00 New York, 4 business days after SEND_NOW


@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return sending_settings(test_settings)


def _checker() -> MxChecker:
    return MxChecker(CONFIG.contacts, resolve=FakeResolver())


async def _queue(engine: AsyncEngine, *, at: datetime = DUE) -> tuple[int, int]:
    return await queue_due(
        make_session_factory(engine),
        now=lambda: at,
        config=CONFIG,
        facts=FACTS,
        catalog=CATALOG,
        mx_checker=_checker(),
    )


async def _pass(
    engine: AsyncEngine,
    settings: Settings,
    sender: FakeSender,
    *,
    at: datetime,
    config: AppConfig = CONFIG,
) -> SendOutcome:
    return await send_pass(
        make_session_factory(engine),
        mail=fake_mail(sender, None),
        settings=settings,
        config=config,
        facts=FACTS,
        catalog=CATALOG,
        now=lambda: at,
        mx_checker=_checker(),
        rng=random.Random(0),  # noqa: S311
    )


async def _get[T](engine: AsyncEngine, model: type[T], key: Any) -> T:
    async with make_session_factory(engine)() as s:
        row = await s.get(model, key)
        assert row is not None
        return row


async def _set(engine: AsyncEngine, model: Any, key: uuid.UUID, **values: Any) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(update(model).where(model.id == key).values(**values))
        await s.commit()


async def _user(engine: AsyncEngine, user_id: uuid.UUID) -> User:
    return await _get(engine, User, user_id)


async def _step1_sent(engine: AsyncEngine, ready: Ready, *, due: datetime = DUE) -> None:
    """Step 1 went out on SEND_NOW from the mailbox; step 2 is pending, due at ``due``, with
    the subject it was drafted with."""
    await _set(
        engine,
        Message,
        ready.message_id,
        status="sent",
        sent_at=SEND_NOW,
        claimed_at=SEND_NOW,
        message_id_header=FIRST_ID,
        mailbox_id=ready.mailbox_id,
    )
    await _set(engine, Sequence, ready.sequence_id, mailbox_id=ready.mailbox_id)
    # As drafted, before it falls due (the writer's own subject).
    await _set(engine, Message, ready.step2_id, due_at=due, subject=DRAFTED_SUBJECT)


def _wire(message: EmailMessage) -> email.message.EmailMessage:
    parsed = email.message_from_bytes(message.as_bytes(), policy=email_policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


async def _fresh_for_cold_sending(engine: AsyncEngine, ready: Ready, at: datetime) -> None:
    """Mailbox DNS and HubSpot fresh again at ``at`` (both age out within days)."""
    await _set(engine, Mailbox, ready.mailbox_id, dns_checked_at=at - timedelta(hours=1))
    async with make_session_factory(engine)() as s:
        s.add(
            HubSpotSync(
                status="succeeded",
                started_at=at - timedelta(hours=2),
                finished_at=at - timedelta(hours=1),
            )
        )
        await s.commit()


# --------------------------------------------------------------------------- subject


def test_followup_subject() -> None:
    assert followup_subject(STEP1_SUBJECT) == f"Re: {STEP1_SUBJECT}"
    assert followup_subject("Re: already a reply") == "Re: already a reply"
    assert followup_subject("RE: shouting") == "RE: shouting"
    long = followup_subject("x" * 200)
    assert len(long) == 200 and long.startswith("Re: x")


# --------------------------------------------------------------------------- queue_due


async def test_not_due_queues_nothing(engine: AsyncEngine, db: AsyncSession) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)

    assert await _queue(engine, at=DUE - timedelta(seconds=1)) == (0, 0)

    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.subject) == ("pending", DRAFTED_SUBJECT)


async def test_due_is_queued_for_review_with_re_subject(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    step1 = await _get(engine, Message, ready.message_id)
    assert step1.subject == STEP1_SUBJECT

    assert await _queue(engine) == (1, 0)

    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.subject, step2.gate_errors) == (
        "awaiting_approval",
        f"Re: {STEP1_SUBJECT}",
        [],
    )
    assert step2.approved_at is None and step2.approved_by_id is None
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    assert await _queue(engine) == (0, 0)  # once only


async def test_unsubscribed_meanwhile_is_held_by_the_gate(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    await suppress(db, RECIPIENT, reason="unsubscribed", detail="Asked to stop")
    await db.commit()

    assert await _queue(engine) == (1, 1)

    step2 = await _get(engine, Message, ready.step2_id)
    assert step2.status == "gate_failed"
    assert step2.gate_errors is not None
    assert any(p.startswith("Recipient is excluded: Unsubscribed on ") for p in step2.gate_errors)


async def test_replied_meanwhile_is_not_queued(engine: AsyncEngine, db: AsyncSession) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    await _set(engine, Sequence, ready.sequence_id, status="replied")

    assert await _queue(engine) == (0, 0)
    assert (await _get(engine, Message, ready.step2_id)).status == "pending"


async def test_step1_not_sent_is_not_queued(engine: AsyncEngine, db: AsyncSession) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    await _set(engine, Message, ready.message_id, status="unconfirmed")

    assert await _queue(engine) == (0, 0)
    assert (await _get(engine, Message, ready.step2_id)).status == "pending"


async def test_archived_campaign_stops_instead(engine: AsyncEngine, db: AsyncSession) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    await _set(engine, Campaign, ready.campaign_id, status="archived")

    assert await _queue(engine) == (0, 0)

    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Campaign archived")
    async with make_session_factory(engine)() as s:
        [row] = (
            await s.scalars(select(AuditLog).where(AuditLog.action == audit.SEQUENCE_STOPPED))
        ).all()
    assert row.actor_id is None and row.details["reason"] == "archived"


async def test_a_pending_step2_is_never_sent(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready, due=SEND_NOW - timedelta(days=1))
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender, at=SEND_NOW)

    assert outcome.sent == 0 and sender.sent == []
    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.claimed_at, step2.message_id_header) == ("pending", None, None)
    direct = await claim(
        make_session_factory(engine),
        ready.step2_id,
        mailbox=ready.mailbox_id,
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        catalog=CATALOG,
        now=SEND_NOW,
        rng=random.Random(0),  # noqa: S311
    )
    assert direct.kind == "skipped"
    assert (await _get(engine, Message, ready.step2_id)).status == "pending"


async def test_paused_campaign_queues_and_approves_but_holds_the_send(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    await _set(engine, Campaign, ready.campaign_id, status="paused")

    assert await _queue(engine) == (1, 0)
    async with make_session_factory(engine)() as s:
        [item], _ = await outbox.list_outbox(s, kind="followup")
        assert item.campaign_paused and item.kind == "followup"
        problems = await outbox.approve(
            s,
            ready.step2_id,
            actor=await _user(engine, ready.user_id),
            now=DUE,
            facts=FACTS,
            config=CONFIG,
            catalog=CATALOG,
        )
        await s.commit()
    assert problems == []
    assert (await _get(engine, Message, ready.step2_id)).status == "approved"
    async with make_session_factory(engine)() as s:
        [item], _ = await outbox.list_outbox(s, view="approved")
    assert item.held_by_campaign_pause

    at = datetime(2026, 11, 16, 18, tzinfo=UTC)  # office hours in New York and Los Angeles
    await _fresh_for_cold_sending(engine, ready, at)
    sender = FakeSender()
    assert (await _pass(engine, settings, sender, at=at)).sent == 0
    held = await claim(
        make_session_factory(engine),
        ready.step2_id,
        mailbox=ready.mailbox_id,
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        catalog=CATALOG,
        now=at,
        rng=random.Random(0),  # noqa: S311
    )
    assert (held.kind, held.reason) == ("held", CAMPAIGN_PAUSED)
    assert sender.sent == []
    assert (await _get(engine, Message, ready.step2_id)).status == "approved"


async def test_end_to_end_followup_threads_under_step1(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender()

    # Step 1 goes out; step 2 falls due 4 business days later.
    assert (await _pass(engine, settings, sender, at=SEND_NOW)).sent == 1
    step1 = await _get(engine, Message, ready.message_id)
    assert step1.message_id_header is not None
    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.due_at) == ("pending", DUE)

    # Not yet due: nothing queued, nothing sent.
    assert await _queue(engine, at=DUE - timedelta(hours=1)) == (0, 0)
    async with make_session_factory(engine)() as s:
        assert await outbox.outbox_counts(s) == {"awaiting_approval": 0, "gate_failed": 0}

    # Due: queued for review, counted in the Outbox.
    assert await _queue(engine) == (1, 0)
    async with make_session_factory(engine)() as s:
        assert await outbox.outbox_counts(s) == {"awaiting_approval": 1, "gate_failed": 0}
        [item], total = await outbox.list_outbox(s)
        assert (total, item.kind, item.message.id) == (1, "followup", ready.step2_id)
        assert item.step1_sent_at == SEND_NOW
        assert (
            await outbox.approve(
                s,
                ready.step2_id,
                actor=await _user(engine, ready.user_id),
                now=DUE,
                facts=FACTS,
                config=CONFIG,
                catalog=CATALOG,
            )
            == []
        )
        await s.commit()

    at = datetime(2026, 11, 16, 18, tzinfo=UTC)
    await _fresh_for_cold_sending(engine, ready, at)
    assert (await _pass(engine, settings, sender, at=at)).sent == 1

    assert len(sender.sent) == 2
    wire = _wire(sender.sent[1][2])
    assert wire["Subject"] == f"Re: {STEP1_SUBJECT}"
    assert wire["In-Reply-To"] == step1.message_id_header
    assert wire["References"] == step1.message_id_header
    assert wire["Message-ID"] != step1.message_id_header
    step2 = await _get(engine, Message, ready.step2_id)
    assert step2.status == "sent" and step2.in_reply_to == step1.message_id_header
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "completed"


async def test_queue_leaves_other_kinds_alone(engine: AsyncEngine, db: AsyncSession) -> None:
    """Only a due follow-up is queued; a first email of another sequence is untouched."""
    ready = await seed_ready(db, approved=False)
    assert await _queue(engine, at=DUE + timedelta(days=30)) == (0, 0)
    assert (await _get(engine, Message, ready.message_id)).status == "awaiting_approval"
    assert (await _get(engine, Message, ready.step2_id)).status == "pending"


async def test_one_bad_follow_up_never_aborts_the_pass(
    engine: AsyncEngine,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Final-wave T7: an error queueing one follow-up is logged (its type only) and the pass
    goes on with the next."""
    from salesfinder.followups import service as followups

    ready = await seed_ready(db)
    await _step1_sent(engine, ready)
    bad = uuid.uuid4()
    real_due, real_one = followups._due_ids, followups._queue_one

    async def due(*args: Any, **kwargs: Any) -> list[tuple[uuid.UUID, str]]:
        return [(bad, RECIPIENT), *await real_due(*args, **kwargs)]

    async def one(db: AsyncSession, message_id: uuid.UUID, **kwargs: Any) -> Any:
        if message_id == bad:
            raise RuntimeError("UNTRUSTED-ZEBRA in the error text")
        return await real_one(db, message_id, **kwargs)

    monkeypatch.setattr(followups, "_due_ids", due)
    monkeypatch.setattr(followups, "_queue_one", one)

    assert await _queue(engine) == (1, 0)

    assert (await _get(engine, Message, ready.step2_id)).status == "awaiting_approval"
    logged = capsys.readouterr()
    assert "followup_failed" in logged.out and "RuntimeError" in logged.out
    assert "UNTRUSTED-ZEBRA" not in logged.out + logged.err
