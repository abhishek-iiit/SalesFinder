"""Archive versus reject, and archive versus the sender's claim, on the same campaign, in two
real sessions, interleaved at the points where the old lock orders deadlocked:

- reject holds the step-1 email (and its sequence) when archive starts: archive locked the
  campaign FOR UPDATE and waited on the email, while reject's feedback insert needed FOR KEY
  SHARE on the campaign;
- the follow-up's id sorts below the step-1 email's: archive locked the follow-up first, while
  reject held step 1 and wanted the follow-up;
- archive holds the campaign when reject starts: reject's feedback insert waited on the
  campaign, then archive waited on reject's email.

Each case must finish with no deadlock error. The sender's claim takes the same order as
reject (messages by id, the sequence, the recipient, then the mailbox) and never locks the
campaign, so it can't cross archive either; two claims of one message give one send."""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import CATALOG, CLEAN, CONFIG, FACTS, seed_campaign, seed_company, seed_user
from fakes.mail import FakeSender, fake_mail
from mail_helpers import reply_to
from salesfinder.campaigns import service as campaigns_service
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    Campaign,
    MailboxFolder,
    MailboxMessage,
    Message,
    OutboxFeedback,
    Sequence,
    User,
)
from salesfinder.mail.parse import parse_mail
from salesfinder.outbox import service as outbox_service
from salesfinder.replies import ingest as ingest_module
from salesfinder.sending import sender as sender_module
from salesfinder.settings import Settings
from salesfinder.writer.compose import assemble
from sending_helpers import BOX, RECIPIENT, SEND_NOW, Ready, seed_ready, sending_settings

LOW = uuid.UUID(int=1)
HIGH = uuid.UUID(int=2**127)
TIMEOUT = 15.0
FIRST_ID = "<first@send-domain.com>"


async def _draft(
    db: AsyncSession, campaign: Campaign, *, step1_id: uuid.UUID, step2_id: uuid.UUID
) -> Sequence:
    company, person, row = await seed_company(db)
    assert row is not None
    sequence = Sequence(
        campaign_id=campaign.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=row.id,
        recipient=row.address,
        status="awaiting_approval",
    )
    db.add(sequence)
    await db.flush()
    first = assemble(
        CLEAN["first_subject"], CLEAN["first_body"], facts=FACTS, writing=CONFIG.writing,
        side="buyer",
    )  # fmt: skip
    db.add_all(
        [
            Message(
                id=step1_id,
                sequence_id=sequence.id,
                step=1,
                subject=first[0],
                body=first[1],
                status="awaiting_approval",
            ),
            Message(
                id=step2_id,
                sequence_id=sequence.id,
                step=2,
                subject="Re: hi",
                body="Follow-up",
                status="pending",
            ),
        ]
    )
    await db.commit()
    return sequence


async def _blocked_or_done(engine: AsyncEngine, pid: int, task: asyncio.Task[Any]) -> None:
    """Wait until the backend ``pid`` is waiting on a lock, or ``task`` has finished."""
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not task.done():
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if waiting:
            return
        assert asyncio.get_running_loop().time() < deadline, "never blocked nor finished"
        await asyncio.sleep(0.02)


async def _in_session(
    engine: AsyncEngine,
    pid: asyncio.Future[int],
    work: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    async with make_session_factory(engine)() as s:
        pid.set_result(int(await s.scalar(text("SELECT pg_backend_pid()")) or 0))
        result = await work(s)
        await s.commit()
        return result


def _pause_after(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    reached: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """Make ``module.name`` stop, holding whatever it locked, until ``release`` is set."""
    real = getattr(module, name)

    async def paused(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(module, name, paused)


@pytest.mark.parametrize("first", ["reject", "archive"])
@pytest.mark.parametrize("follow_up_sorts_first", [False, True], ids=["step1-low", "step2-low"])
async def test_archive_and_reject_never_deadlock(
    engine: AsyncEngine,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    follow_up_sorts_first: bool,
) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    step1, step2 = (HIGH, LOW) if follow_up_sorts_first else (LOW, HIGH)
    sequence = await _draft(db, campaign, step1_id=step1, step2_id=step2)
    campaign_id, sequence_id, user_id = campaign.id, sequence.id, user.id

    async def reject(s: AsyncSession) -> None:
        actor = await s.get(User, user_id)
        assert actor is not None
        await outbox_service.reject(
            s, step1, reason="Not a fit", actor=actor, now=datetime.now(UTC)
        )

    async def archive(s: AsyncSession) -> campaigns_service.ArchiveOutcome:
        return await campaigns_service.archive(s, campaign_id, actor=None)

    reached, release = asyncio.Event(), asyncio.Event()
    if first == "reject":  # pause reject right after it has locked its rows
        _pause_after(monkeypatch, outbox_service, "_lock", reached, release)
        leader, follower = reject, archive
    else:  # pause archive right after it has locked the campaign
        _pause_after(monkeypatch, campaigns_service, "_locked", reached, release)
        leader, follower = archive, reject
    loop = asyncio.get_running_loop()
    leader_pid: asyncio.Future[int] = loop.create_future()
    follower_pid: asyncio.Future[int] = loop.create_future()

    lead = asyncio.create_task(_in_session(engine, leader_pid, leader))
    follow: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(reached.wait(), TIMEOUT)
        follow = asyncio.create_task(_in_session(engine, follower_pid, follower))
        await _blocked_or_done(engine, await follower_pid, follow)
    finally:
        # Never leave the leader paused holding locks: the db fixture's
        # TRUNCATE would then wait forever instead of the test failing.
        release.set()
        if follow is None or not follow.done():
            pending = [t for t in (lead, follow) if t is not None]
            done, _ = await asyncio.wait(pending, timeout=TIMEOUT)
            for t in pending:
                if t not in done:
                    t.cancel()
    assert follow is not None
    results = await asyncio.wait_for(asyncio.gather(lead, follow), TIMEOUT)

    outcome = results[0] if first == "archive" else results[1]
    assert isinstance(outcome, campaigns_service.ArchiveOutcome)
    async with make_session_factory(engine)() as s:
        assert (await s.get(Campaign, campaign_id)).status == "archived"  # type: ignore[union-attr]
        statuses = dict(
            (
                await s.execute(
                    select(Message.id, Message.status).where(Message.id.in_([step1, step2]))
                )
            ).all()
        )
        seq = await s.get(Sequence, sequence_id)
        assert seq is not None
        feedback = (await s.scalars(select(OutboxFeedback))).all()
    # Reject committed first either way: as the leader it held its rows while archive waited;
    # as the follower it didn't wait for archive's campaign lock (FOR NO KEY UPDATE lets its
    # feedback insert through). Archive then found nothing left to stop or cancel.
    assert statuses == {step1: "rejected", step2: "cancelled"}
    assert (seq.status, seq.stop_reason) == ("stopped", "Rejected in the Outbox")
    assert (outcome.sequences_stopped, outcome.messages_cancelled) == (0, 0)
    assert len(feedback) == 1


# --------------------------------------------------------------------------- sending

SEND_TIMEOUT = 5.0


async def _claim_in(s: AsyncSession, ready: Ready, settings: Settings) -> sender_module.ClaimResult:
    """``sender.claim``'s transaction body in the caller's session (committed by
    ``_in_session``)."""
    out = await sender_module._claim(
        s,
        ready.message_id,
        mailbox=ready.mailbox_id,
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        catalog=CATALOG,
        now=SEND_NOW,
        rng=random.Random(0),  # noqa: S311
    )
    return out.result


async def _race(
    engine: AsyncEngine,
    leader: Callable[[AsyncSession], Awaitable[Any]],
    follower: Callable[[AsyncSession], Awaitable[Any]],
    reached: asyncio.Event,
    release: asyncio.Event,
) -> list[Any]:
    """Run ``leader`` until it pauses holding its locks, start ``follower``, wait until it is
    blocked on a lock (or done), release the leader; both must finish within 5 s."""
    loop = asyncio.get_running_loop()
    leader_pid: asyncio.Future[int] = loop.create_future()
    follower_pid: asyncio.Future[int] = loop.create_future()
    lead = asyncio.create_task(_in_session(engine, leader_pid, leader))
    follow: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(reached.wait(), SEND_TIMEOUT)
        follow = asyncio.create_task(_in_session(engine, follower_pid, follower))
        await _blocked_or_done(engine, await follower_pid, follow)
    finally:
        release.set()
        if follow is None or not follow.done():
            pending = [t for t in (lead, follow) if t is not None]
            done, _ = await asyncio.wait(pending, timeout=SEND_TIMEOUT)
            for t in pending:
                if t not in done:
                    t.cancel()
    assert follow is not None
    return list(await asyncio.wait_for(asyncio.gather(lead, follow), SEND_TIMEOUT))


async def _deliver(
    engine: AsyncEngine, ready: Ready, settings: Settings, sender: FakeSender
) -> None:
    await sender_module.deliver(
        make_session_factory(engine),
        ready.message_id,
        mail=fake_mail(sender, None),
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        now=SEND_NOW,
    )


@pytest.mark.parametrize("first", ["claim", "archive", "archive-campaign-only"])
async def test_archive_vs_claim_never_deadlock(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """Archive and the sender's claim on the same message, interleaved three ways: the claim
    holding its message/sequence/recipient locks, archive holding everything it locked, and
    archive holding only the campaign. No deadlock; either the email is cancelled and never
    sent, or it was claimed first, goes out once, and archive stops the sequence."""
    settings = sending_settings(test_settings)
    ready = await seed_ready(db)

    async def claim(s: AsyncSession) -> sender_module.ClaimResult:
        return await _claim_in(s, ready, settings)

    async def archive(s: AsyncSession) -> campaigns_service.ArchiveOutcome:
        return await campaigns_service.archive(s, ready.campaign_id, actor=None)

    reached, release = asyncio.Event(), asyncio.Event()
    if first == "claim":  # paused holding the messages, the sequence and the recipient lock
        _pause_after(monkeypatch, sender_module, "lock_recipient", reached, release)
        results = await _race(engine, claim, archive, reached, release)
        claimed = results[0]
    elif first == "archive":  # paused after archiving, before its commit: every lock held
        _pause_after(monkeypatch, campaigns_service, "archive", reached, release)
        results = await _race(engine, archive, claim, reached, release)
        claimed = results[1]
    else:  # paused holding only the campaign row
        _pause_after(monkeypatch, campaigns_service, "_locked", reached, release)
        results = await _race(engine, archive, claim, reached, release)
        claimed = results[1]
    assert isinstance(claimed, sender_module.ClaimResult)
    sender = FakeSender()
    if claimed.kind == "claimed":
        await _deliver(engine, ready, settings, sender)

    async with make_session_factory(engine)() as s:
        message = await s.get(Message, ready.message_id)
        step2 = await s.get(Message, ready.step2_id)
        sequence = await s.get(Sequence, ready.sequence_id)
        assert message is not None and step2 is not None and sequence is not None
        assert (await s.get(Campaign, ready.campaign_id)).status == "archived"  # type: ignore[union-attr]
    assert (sequence.status, step2.status) == ("stopped", "cancelled")
    if first == "archive":
        assert claimed.kind == "skipped"
        assert message.status == "cancelled" and sender.sent == []
    else:
        assert claimed.kind == "claimed"
        assert message.status == "sent" and len(sender.sent) == 1


async def test_two_claims_one_send(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = sending_settings(test_settings)
    ready = await seed_ready(db)

    async def claim(s: AsyncSession) -> sender_module.ClaimResult:
        return await _claim_in(s, ready, settings)

    reached, release = asyncio.Event(), asyncio.Event()
    _pause_after(monkeypatch, sender_module, "lock_recipient", reached, release)
    results = await _race(engine, claim, claim, reached, release)

    assert sorted(r.kind for r in results) == ["claimed", "skipped"]
    sender = FakeSender()
    await _deliver(engine, ready, settings, sender)
    assert len(sender.sent) == 1
    async with make_session_factory(engine)() as s:
        message = await s.get(Message, ready.message_id)
        assert message is not None and message.status == "sent"


# --------------------------------------------------------------------------- reply vs claim


async def _step2_ready(db: AsyncSession, engine: AsyncEngine) -> Ready:
    """Step 1 went out six days ago; the follow-up is approved and due."""
    ready = await seed_ready(db)
    async with make_session_factory(engine)() as s:
        step1 = await s.get(Message, ready.message_id)
        step2 = await s.get(Message, ready.step2_id)
        sequence = await s.get(Sequence, ready.sequence_id)
        assert step1 is not None and step2 is not None and sequence is not None
        step1.status = "sent"
        step1.sent_at = SEND_NOW - timedelta(days=6)
        step1.message_id_header = FIRST_ID
        step1.mailbox_id = ready.mailbox_id
        sequence.mailbox_id = ready.mailbox_id
        step2.status = "approved"
        step2.approved_by_id = ready.user_id
        step2.approved_at = SEND_NOW - timedelta(hours=1)
        await s.commit()
    return ready


async def _ingest_reply_in(s: AsyncSession, ready: Ready) -> object:
    """Ingest of their reply to step 1: the per-mail transaction body in the caller's session
    (committed by ``_in_session``)."""
    async with make_session_factory(s.bind)() as setup:  # type: ignore[arg-type]
        await setup.execute(
            insert(MailboxFolder)
            .values(mailbox_id=ready.mailbox_id, folder="inbox", name="INBOX")
            .on_conflict_do_nothing()
        )
        await setup.commit()
    ctx = ingest_module._Ctx(
        mailbox_id=ready.mailbox_id,
        mailbox_address=BOX,
        folder="inbox",
        name="INBOX",
        uidvalidity=1,
        known_recipients=frozenset({RECIPIENT}),
        now=SEND_NOW,
        config=CONFIG,
    )
    parsed = parse_mail(
        reply_to(FIRST_ID, date=SEND_NOW - timedelta(hours=1)), max_body_chars=20_000
    )
    return await ingest_module._ingest(s, ctx, 1, parsed, cursor_uid=1)


@pytest.mark.parametrize("first", ["reply", "claim"])
async def test_reply_vs_step2_claim(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """Review Focus 2: whichever takes the message/sequence locks first wins. A reply ingested
    first cancels the approved follow-up (never sent); a claim committed first sends it, and
    ingest leaves the ``sending`` row alone and still marks the sequence ``replied``."""
    settings = sending_settings(test_settings)
    ready = await _step2_ready(db, engine)

    async def claim(s: AsyncSession) -> sender_module.ClaimResult:
        out = await sender_module._claim(
            s,
            ready.step2_id,
            mailbox=ready.mailbox_id,
            settings=settings,
            config=CONFIG,
            facts=FACTS,
            catalog=CATALOG,
            now=SEND_NOW,
            rng=random.Random(0),  # noqa: S311
        )
        return out.result

    async def ingest(s: AsyncSession) -> object:
        return await _ingest_reply_in(s, ready)

    reached, release = asyncio.Event(), asyncio.Event()
    if first == "reply":  # paused holding the messages, the sequence and the recipient lock
        _pause_after(monkeypatch, ingest_module, "lock_recipient", reached, release)
        results = await _race(engine, ingest, claim, reached, release)
        claimed = results[1]
    else:
        _pause_after(monkeypatch, sender_module, "lock_recipient", reached, release)
        results = await _race(engine, claim, ingest, reached, release)
        claimed = results[0]
    assert isinstance(claimed, sender_module.ClaimResult)
    sender = FakeSender()
    if claimed.kind == "claimed":
        await sender_module.deliver(
            make_session_factory(engine),
            ready.step2_id,
            mail=fake_mail(sender, None),
            settings=settings,
            config=CONFIG,
            facts=FACTS,
            now=SEND_NOW,
        )

    async with make_session_factory(engine)() as s:
        step2 = await s.get(Message, ready.step2_id)
        sequence = await s.get(Sequence, ready.sequence_id)
        assert step2 is not None and sequence is not None
        stored = (await s.scalars(select(MailboxMessage))).all()
    assert sequence.status == "replied"
    assert [r.kind for r in stored] == ["reply"]
    if first == "reply":
        assert claimed.kind == "skipped"
        assert step2.status == "cancelled" and sender.sent == []
    else:
        assert claimed.kind == "claimed"
        assert step2.status == "sent" and len(sender.sent) == 1
