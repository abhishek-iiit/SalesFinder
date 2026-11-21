"""Suppressions, the shared message/sequence locks and stopping sequences (excluded ones too)."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import seed_campaign, seed_draft, seed_user
from salesfinder import audit
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    AuditLog,
    Company,
    DoNotContact,
    EmailAddress,
    Message,
    Sequence,
    Suppression,
)
from salesfinder.exclusions.check import exclusion_for
from salesfinder.outreach import locks, stop
from salesfinder.outreach.stop import cancel_reply_drafts, stop_excluded, stop_sequence
from salesfinder.suppressions import service

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
TIMEOUT = 15.0


def _day(row: Suppression) -> str:
    return f"{row.created_at.astimezone(UTC):%Y-%m-%d}"


# --------------------------------------------------------------------------- suppress / remove


async def test_suppress_is_idempotent_and_normalized(db: AsyncSession) -> None:
    assert await service.suppress(db, " Jane@ACME.com ", reason="unsubscribed", detail="Asked")
    assert not await service.suppress(db, "jane@acme.com", reason="bounced", detail="550")
    await db.commit()
    rows = (await db.scalars(select(Suppression))).all()
    assert [(r.address, r.reason, r.detail) for r in rows] == [
        ("jane@acme.com", "unsubscribed", "Asked")
    ]
    added = (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.SUPPRESSION_ADDED))
    ).all()
    assert len(added) == 1
    assert added[0].details["address"] == "jane@acme.com"
    assert added[0].details["reason"] == "unsubscribed"
    assert "detail" not in added[0].details  # may quote server text
    found = await service.is_suppressed(db, "JANE@acme.com")
    assert found is not None and found.id == rows[0].id
    assert await service.is_suppressed(db, "other@acme.com") is None
    assert await service.is_suppressed(db, "not an email") is None


async def test_suppress_refuses_bad_input_without_writing(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="Not a valid email address"):
        await service.suppress(db, "not an email", reason="manual", detail="x")
    with pytest.raises(ValueError, match="Unknown suppression reason"):
        await service.suppress(db, "a@b.com", reason="because", detail="x")
    assert await db.scalar(select(func.count()).select_from(Suppression)) == 0


async def test_suppress_records_message_and_actor_and_clips_detail(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    message = await seed_draft(db, campaign)
    assert await service.suppress(
        db,
        "jane.doe@acme-soap.com",
        reason="manual",
        detail="x" * 400,
        message_id=message.id,
        actor=user,
    )
    await db.commit()
    row = await service.is_suppressed(db, "jane.doe@acme-soap.com")
    assert row is not None
    assert (row.message_id, row.created_by_id, len(row.detail)) == (message.id, user.id, 300)


async def test_removed_unsubscribe_is_refused(db: AsyncSession) -> None:
    user = await seed_user(db)
    await service.suppress(db, "gone@acme.com", reason="unsubscribed", detail="Asked")
    await service.suppress(db, "bounce@acme.com", reason="bounced", detail="550 5.1.1")
    await db.commit()
    unsub = await service.is_suppressed(db, "gone@acme.com")
    bounced = await service.is_suppressed(db, "bounce@acme.com")
    assert unsub is not None and bounced is not None
    with pytest.raises(ValueError, match=r"Unsubscribes can't be removed\."):
        await service.remove(db, unsub.id, actor=user)  # refused before any write
    removed = await service.remove(db, bounced.id, actor=user)
    await db.commit()
    assert removed.address == "bounce@acme.com"
    assert await service.is_suppressed(db, "bounce@acme.com") is None
    assert await service.is_suppressed(db, "gone@acme.com") is not None
    with pytest.raises(LookupError):
        await service.remove(db, bounced.id, actor=user)
    actions = list(await db.scalars(select(AuditLog.action).order_by(AuditLog.at)))
    assert actions.count(audit.SUPPRESSION_REMOVED) == 1


async def test_unsubscribe_upgrades_an_earlier_reason_and_then_cannot_be_removed(
    db: AsyncSession,
) -> None:
    """P6-S: unsubscribed outranks every other reason. An unsubscribe for an address already
    suppressed as bounced/manual upgrades the row (audited ``suppression.upgraded``), so an
    admin can no longer delete it and lose the opt-out."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    message = await seed_draft(db, campaign)
    assert await service.suppress(db, "Jane@acme.com", reason="bounced", detail="550 5.1.1")
    await db.commit()
    first = await service.is_suppressed(db, "jane@acme.com")
    assert first is not None
    assert await service.suppress(
        db, "jane@acme.com", reason="unsubscribed", detail="Asked", message_id=message.id
    )
    await db.commit()
    row = await service.is_suppressed(db, "jane@acme.com")
    assert row is not None and row.id == first.id
    assert (row.reason, row.detail, row.message_id) == ("unsubscribed", "Asked", message.id)
    upgraded = (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.SUPPRESSION_UPGRADED))
    ).all()
    assert len(upgraded) == 1
    assert upgraded[0].target_id == str(row.id)
    assert upgraded[0].details == {
        "address": "jane@acme.com",
        "reason": "unsubscribed",
        "previous_reason": "bounced",
        "message_id": str(message.id),
    }
    with pytest.raises(ValueError, match=r"Unsubscribes can't be removed\."):
        await service.remove(db, row.id, actor=user)
    await db.rollback()
    assert await service.is_suppressed(db, "jane@acme.com") is not None


async def test_other_reasons_never_downgrade_an_unsubscribe(db: AsyncSession) -> None:
    assert await service.suppress(db, "jane@acme.com", reason="unsubscribed", detail="Asked")
    assert not await service.suppress(db, "jane@acme.com", reason="bounced", detail="550")
    assert not await service.suppress(db, "jane@acme.com", reason="manual", detail="x")
    assert not await service.suppress(db, "jane@acme.com", reason="unsubscribed", detail="Again")
    await db.commit()
    row = await service.is_suppressed(db, "jane@acme.com")
    assert row is not None and (row.reason, row.detail) == ("unsubscribed", "Asked")
    actions = list(await db.scalars(select(AuditLog.action)))
    assert actions == [audit.SUPPRESSION_ADDED]


async def test_manual_then_unsubscribe_upgrades_and_bounce_keeps_manual(db: AsyncSession) -> None:
    assert await service.suppress(db, "a@acme.com", reason="manual", detail="Teammate")
    assert not await service.suppress(db, "a@acme.com", reason="bounced", detail="550")
    assert await service.suppress(db, "a@acme.com", reason="unsubscribed", detail="Asked")
    await db.commit()
    row = await service.is_suppressed(db, "a@acme.com")
    assert row is not None and row.reason == "unsubscribed"


async def test_list_suppressions_newest_first_and_paged(db: AsyncSession) -> None:
    for name in ("a", "b", "c"):
        await service.suppress(db, f"{name}@acme.com", reason="manual", detail=name)
        await db.commit()  # one transaction each: distinct created_at
    rows = await service.list_suppressions(db, limit=2, offset=0)
    assert [r.address for r in rows] == ["c@acme.com", "b@acme.com"]
    rest = await service.list_suppressions(db, limit=2, offset=2)
    assert [r.address for r in rest] == ["a@acme.com"]
    assert await service.count_suppressions(db) == 3


async def test_suppress_and_remove_refresh_stored_blocks(db: AsyncSession) -> None:
    """``blocked_reason`` (and so the sendable count) now reflects suppressions."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    await seed_draft(db, campaign)
    await service.suppress(db, "jane.doe@acme-soap.com", reason="bounced", detail="550")
    await db.commit()
    row = await db.scalar(select(EmailAddress).execution_options(populate_existing=True))
    sup = await service.is_suppressed(db, "jane.doe@acme-soap.com")
    assert row is not None and sup is not None
    assert row.blocked_reason == f"Bounced on {_day(sup)}"
    company = await db.get(Company, row.company_id, populate_existing=True)
    assert company is not None and company.sendable_contacts == 0
    await service.remove(db, sup.id, actor=user)
    await db.commit()
    row = await db.get(EmailAddress, row.id, populate_existing=True)
    assert row is not None and row.blocked_reason is None
    company = await db.get(Company, row.company_id, populate_existing=True)
    assert company is not None and company.sendable_contacts == 1


async def test_suppress_without_refresh_leaves_blocks_and_sweep_heals(db: AsyncSession) -> None:
    """Callers holding outreach locks pass refresh=False: the suppression is written and read
    live by the exclusion check, the stored address is blocked later by the Sweep's
    ``refresh_suppressed_blocks``."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    await seed_draft(db, campaign)
    assert await service.suppress(
        db, "jane.doe@acme-soap.com", reason="unsubscribed", detail="Asked", refresh=False
    )
    await db.commit()
    row = await db.scalar(select(EmailAddress).execution_options(populate_existing=True))
    assert row is not None and row.blocked_reason is None
    company = await db.get(Company, row.company_id, populate_existing=True)
    assert company is not None and company.sendable_contacts == 1
    sup = await service.is_suppressed(db, "jane.doe@acme-soap.com")
    assert sup is not None
    hit = await exclusion_for(db, email="jane.doe@acme-soap.com")
    assert hit is not None and (hit.reason, hit.detail) == (
        "suppressed",
        f"Unsubscribed on {_day(sup)}",
    )

    assert await service.refresh_suppressed_blocks(db) == 1
    await db.commit()
    row = await db.get(EmailAddress, row.id, populate_existing=True)
    assert row is not None and row.blocked_reason == f"Unsubscribed on {_day(sup)}"
    company = await db.get(Company, row.company_id, populate_existing=True)
    assert company is not None and company.sendable_contacts == 0
    assert await service.refresh_suppressed_blocks(db) == 0  # nothing left to heal


async def test_remove_without_refresh_leaves_blocks(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    await seed_draft(db, campaign)
    await service.suppress(db, "jane.doe@acme-soap.com", reason="bounced", detail="550")
    await db.commit()
    sup = await service.is_suppressed(db, "jane.doe@acme-soap.com")
    assert sup is not None
    await service.remove(db, sup.id, actor=user, refresh=False)
    await db.commit()
    row = await db.scalar(select(EmailAddress).execution_options(populate_existing=True))
    assert row is not None and row.blocked_reason == f"Bounced on {_day(sup)}"
    assert await exclusion_for(db, email="jane.doe@acme-soap.com") is None


# --------------------------------------------------------------------------- locks


async def _sequence_of(db: AsyncSession, message: Message) -> Sequence:
    seq = await db.get(Sequence, message.sequence_id)
    assert seq is not None
    return seq


async def test_lock_message_none_when_gone(db: AsyncSession) -> None:
    assert await locks.lock_message(db, uuid.uuid4()) is None


async def test_lock_message_locks_siblings_and_sequence_not_campaign(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    campaign = await seed_campaign(db)
    message = await seed_draft(db, campaign)
    seq = await _sequence_of(db, message)
    step2 = await db.scalar(
        select(Message.id).where(Message.sequence_id == seq.id, Message.step == 2)
    )
    locked = await locks.lock_message(db, message.id)
    assert locked is not None
    assert locked.message.id == message.id
    assert [m.id for m in locked.siblings] == sorted([message.id, step2])  # type: ignore[type-var]
    assert locked.sequence.id == seq.id
    assert locked.campaign.id == campaign.id
    assert locked.company.id == seq.company_id
    assert locked.person is not None and locked.address is not None
    async with make_session_factory(engine)() as other:
        for sql, params in (
            ("SELECT id FROM messages WHERE id = :id FOR UPDATE NOWAIT", {"id": step2}),
            ("SELECT id FROM sequences WHERE id = :id FOR UPDATE NOWAIT", {"id": seq.id}),
        ):
            with pytest.raises(DBAPIError):
                await other.execute(text(sql), params)
            await other.rollback()
        # The campaign is read without a lock: archive (FOR NO KEY UPDATE) is never blocked.
        await other.execute(
            text("SELECT id FROM campaigns WHERE id = :id FOR UPDATE NOWAIT"), {"id": campaign.id}
        )
        await other.rollback()
    await db.rollback()


async def test_lock_recipient_is_a_transaction_lock(db: AsyncSession, engine: AsyncEngine) -> None:
    await locks.lock_recipient(db, "jane@acme.com")
    try_lock = text(f"SELECT pg_try_advisory_xact_lock({locks.RECIPIENT_LOCK_NS}, hashtext(:r))")
    async with make_session_factory(engine)() as other:
        assert await other.scalar(try_lock, {"r": "jane@acme.com"}) is False
        assert await other.scalar(try_lock, {"r": "raj@acme.com"}) is True
        await other.rollback()
    await db.rollback()  # released with the transaction
    async with make_session_factory(engine)() as other:
        assert await other.scalar(try_lock, {"r": "jane@acme.com"}) is True
        await other.rollback()


# --------------------------------------------------------------------------- stop_sequence


async def _sent_step1_with_replies(db: AsyncSession) -> tuple[Message, list[Message]]:
    """A sequence whose step 1 is ``sent``, step 2 ``approved`` (by a teammate) and with reply
    drafts in every state: step 3 awaiting_approval, 4 sending, 5 gate_failed, 6 unconfirmed."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    first = await seed_draft(db, campaign, status="sent")
    msgs = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.sequence_id == first.sequence_id)
                .order_by(Message.step)
            )
        ).all()
    )
    msgs[1].status = "approved"
    msgs[1].approved_by_id = user.id
    msgs[1].approved_at = NOW
    for step, status in ((3, "awaiting_approval"), (4, "sending"), (5, "gate_failed")):
        db.add(
            Message(
                sequence_id=first.sequence_id, step=step, subject="Re: hi", body="b", status=status
            )
        )
    db.add(
        Message(sequence_id=first.sequence_id, step=6, subject="Re", body="b", status="unconfirmed")
    )
    seq = await db.get(Sequence, first.sequence_id)
    assert seq is not None
    seq.status = "active"
    await db.commit()
    return first, msgs


async def _statuses(db: AsyncSession, sequence_id: uuid.UUID) -> dict[int, str]:
    rows = await db.execute(
        select(Message.step, Message.status)
        .where(Message.sequence_id == sequence_id)
        .execution_options(populate_existing=True)
    )
    return {step: status for step, status in rows.all()}


async def test_stop_sequence_cancels_only_unsent_and_keeps_approver(db: AsyncSession) -> None:
    first, msgs = await _sent_step1_with_replies(db)
    locked = await locks.lock_message(db, first.id)
    assert locked is not None
    cancelled = await stop_sequence(db, locked, reason="  Bounced " + "x" * 400)
    await db.commit()
    assert cancelled == 3  # step 2 approved, step 3 awaiting, step 5 gate_failed
    assert await _statuses(db, first.sequence_id) == {
        1: "sent",
        2: "cancelled",
        3: "cancelled",
        4: "sending",
        5: "cancelled",
        6: "unconfirmed",
    }
    seq = await db.get(Sequence, first.sequence_id, populate_existing=True)
    assert seq is not None and seq.status == "stopped"
    assert seq.stop_reason is not None and seq.stop_reason.startswith("Bounced x")
    assert len(seq.stop_reason) == 300
    step2 = await db.get(Message, msgs[1].id, populate_existing=True)
    assert step2 is not None
    assert (step2.status, step2.approved_by_id, step2.approved_at) == (
        "cancelled",
        msgs[1].approved_by_id,
        NOW,
    )


async def test_stop_sequence_replied_status(db: AsyncSession) -> None:
    first, _ = await _sent_step1_with_replies(db)
    locked = await locks.lock_message(db, first.id)
    assert locked is not None
    await stop_sequence(db, locked, reason="They replied", status="replied")
    await db.commit()
    seq = await db.get(Sequence, first.sequence_id, populate_existing=True)
    assert seq is not None and (seq.status, seq.stop_reason) == ("replied", "They replied")


async def test_cancel_reply_drafts_leaves_the_rest(db: AsyncSession) -> None:
    first, _ = await _sent_step1_with_replies(db)
    locked = await locks.lock_message(db, first.id)
    assert locked is not None
    assert await cancel_reply_drafts(db, locked) == 2  # steps 3 and 5
    await db.commit()
    assert await _statuses(db, first.sequence_id) == {
        1: "sent",
        2: "approved",
        3: "cancelled",
        4: "sending",
        5: "cancelled",
        6: "unconfirmed",
    }
    seq = await db.get(Sequence, first.sequence_id, populate_existing=True)
    assert seq is not None and seq.status == "active"


# --------------------------------------------------------------------------- stop_excluded


async def _set(db: AsyncSession, message: Message, step1: str, step2: str, seq: str) -> None:
    rows = (
        await db.scalars(select(Message).where(Message.sequence_id == message.sequence_id))
    ).all()
    for m in rows:
        m.status = step1 if m.step == 1 else step2
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    sequence.status = seq
    await db.commit()


async def test_stop_excluded_cancels_unsent_leaves_sending_and_is_idempotent(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    user = await seed_user(db)
    buyer = await seed_campaign(db, user=user)
    dnc_msg = await seed_draft(db, buyer, domain="dnc-co.com")
    sup_msg = await seed_draft(db, buyer, domain="sup-co.com")
    company_msg = await seed_draft(db, buyer, domain="excluded-co.com")
    clean_msg = await seed_draft(db, buyer, domain="clean-co.com")
    done_msg = await seed_draft(db, buyer, domain="done-co.com")
    await _set(db, dnc_msg, "approved", "pending", "active")
    await _set(db, sup_msg, "sending", "pending", "active")
    await _set(db, company_msg, "awaiting_approval", "pending", "awaiting_approval")
    await _set(db, clean_msg, "approved", "pending", "active")
    await _set(db, done_msg, "sent", "cancelled", "completed")
    approved = await db.get(Message, dnc_msg.id)
    assert approved is not None
    approved.approved_by_id = user.id
    db.add(DoNotContact(kind="email", value="jane.doe@dnc-co.com", reason="Asked us to stop"))
    db.add(DoNotContact(kind="domain", value="done-co.com", reason="Finished"))
    excluded = await db.get(Company, (await _sequence_of(db, company_msg)).company_id)
    assert excluded is not None
    excluded.status = "excluded"
    excluded.exclusion_reason = "existing_relationship"
    excluded.exclusion_detail = "HubSpot company Excluded Co (owner: Raj)"
    await db.commit()
    await service.suppress(db, "jane.doe@sup-co.com", reason="unsubscribed", detail="Asked")
    await db.commit()
    sup = await service.is_suppressed(db, "jane.doe@sup-co.com")
    assert sup is not None

    factory = make_session_factory(engine)
    assert await stop_excluded(factory, now=NOW) == 3
    assert await stop_excluded(factory, now=NOW) == 0  # idempotent

    logs = (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.SEQUENCE_STOPPED))
    ).all()
    by_target = {log.target_id: log for log in logs}
    assert len(logs) == 3 and all(log.actor_id is None for log in logs)
    dnc_seq = await _sequence_of(db, dnc_msg)
    assert by_target[str(dnc_seq.id)].details == {
        "campaign_id": str(buyer.id),
        "company_id": str(dnc_seq.company_id),
        "reason": "excluded",
        "exclusion": "do_not_contact",
        "cancelled": 2,
    }
    sup_seq = await _sequence_of(db, sup_msg)
    assert by_target[str(sup_seq.id)].details["exclusion"] == "suppressed"
    assert by_target[str(sup_seq.id)].details["cancelled"] == 1  # the sending one is kept
    company_seq = await _sequence_of(db, company_msg)
    assert by_target[str(company_seq.id)].details["exclusion"] == "company_excluded"

    async def seq_of(m: Message) -> tuple[str, str | None]:
        s = await db.get(Sequence, m.sequence_id, populate_existing=True)
        assert s is not None
        return s.status, s.stop_reason

    assert await seq_of(dnc_msg) == (
        "stopped",
        "Excluded: Do-not-contact: jane.doe@dnc-co.com — Asked us to stop",
    )
    assert await _statuses(db, dnc_msg.sequence_id) == {1: "cancelled", 2: "cancelled"}
    kept = await db.get(Message, dnc_msg.id, populate_existing=True)
    assert kept is not None and kept.approved_by_id == user.id  # follow-up 11 basis
    assert await seq_of(sup_msg) == ("stopped", f"Excluded: Unsubscribed on {_day(sup)}")
    assert await _statuses(db, sup_msg.sequence_id) == {1: "sending", 2: "cancelled"}
    assert await seq_of(company_msg) == (
        "stopped",
        "Excluded: HubSpot company Excluded Co (owner: Raj)",
    )
    assert await _statuses(db, company_msg.sequence_id) == {1: "cancelled", 2: "cancelled"}
    assert await seq_of(clean_msg) == ("active", None)
    assert await _statuses(db, clean_msg.sequence_id) == {1: "approved", 2: "pending"}
    assert await seq_of(done_msg) == ("completed", None)  # finished: left alone


async def _wait_until_blocked(engine: AsyncEngine, task: asyncio.Task[int]) -> None:
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not task.done():
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
        if waiting:
            return
        assert asyncio.get_running_loop().time() < deadline, "never blocked"
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("meanwhile", ["finished", "lifted"])
async def test_stop_excluded_rechecks_under_the_lock(
    db: AsyncSession, engine: AsyncEngine, meanwhile: str
) -> None:
    """stop_excluded waits for a sequence another transaction holds, then re-checks it live:
    a sequence finished meanwhile, or whose exclusion was lifted meanwhile (the do-not-contact
    entry removed), is left alone."""
    campaign = await seed_campaign(db)
    message = await seed_draft(db, campaign)
    await _set(db, message, "approved", "pending", "active")
    entry = DoNotContact(kind="email", value="jane.doe@acme-soap.com", reason="Stop")
    db.add(entry)
    await db.commit()
    entry_id = entry.id
    factory = make_session_factory(engine)

    holder = factory()
    try:
        locked = await locks.lock_message(holder, message.id)
        assert locked is not None
        task = asyncio.create_task(stop_excluded(factory, now=NOW))
        await _wait_until_blocked(engine, task)
        assert not task.done()
        if meanwhile == "finished":
            await stop_sequence(holder, locked, reason="They replied", status="completed")
            await holder.commit()
        else:
            async with factory() as other:  # the DNC row isn't locked by anyone
                gone = await other.get(DoNotContact, entry_id)
                assert gone is not None
                await other.delete(gone)
                await other.commit()
            await holder.rollback()
    finally:
        await holder.close()
    assert await asyncio.wait_for(task, TIMEOUT) == 0
    seq = await db.get(Sequence, message.sequence_id, populate_existing=True)
    assert seq is not None
    if meanwhile == "finished":
        assert (seq.status, seq.stop_reason) == ("completed", "They replied")
    else:
        assert (seq.status, seq.stop_reason) == ("active", None)
        assert await _statuses(db, message.sequence_id) == {1: "approved", 2: "pending"}
    stops = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == audit.SEQUENCE_STOPPED)
    )
    assert stops == 0


def test_unsent_never_includes_in_flight_or_sent() -> None:
    assert stop.UNSENT == ("pending", "awaiting_approval", "gate_failed", "approved")
    assert not {"sending", "sent", "unconfirmed"} & set(stop.UNSENT)
