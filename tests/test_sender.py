"""The sender (Task 6): claim under locks, re-gate, send exactly what was stored, record the
outcome, reconcile from the Sent folder; never a duplicate send, never a send that skipped the
live gate, never a transaction during SMTP. Real DB, FakeSender/FakeReader, FakeResolver,
fixed clocks, ``random.Random(0)``."""

import email
import random
import uuid
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from email import policy as email_policy
from email.message import EmailMessage
from typing import Any

import dns.exception
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from campaign_helpers import (
    CATALOG,
    CLEAN,
    CONFIG,
    FACTS,
    add_address,
    add_person,
    seed_campaign,
    seed_company,
    seed_draft,
    seed_user,
)
from fakes.dns import FakeResolver
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, refresh_mx
from salesfinder.contacts.service import verify_company
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    Company,
    Conversation,
    DomainMx,
    EmailAddress,
    EmailPattern,
    HubSpotSync,
    Mailbox,
    Message,
    Sequence,
    Suppression,
    User,
)
from salesfinder.exclusions.dnc import add_entry
from salesfinder.gate.checks import ALREADY_CONTACTED, MX_OUT_OF_DATE
from salesfinder.mail.smtp import MailboxCredentials, SendResult
from salesfinder.outbox import service as outbox
from salesfinder.sending import sender as sender_module
from salesfinder.sending import state
from salesfinder.sending.mailboxes import no_password
from salesfinder.sending.sender import (
    AUTH_FAILED,
    CAMPAIGN_PAUSED,
    DNS_NOT_READY,
    HUBSPOT_STALE,
    MAILBOX_OFF,
    MX_RETRY,
    NO_MAILBOX,
    NOT_CHECKABLE,
    OUTSIDE_HOURS,
    SPACING,
    ClaimResult,
    SendOutcome,
    SendSnapshot,
    claim,
    deliver,
    send_pass,
)
from salesfinder.sending.state import NOT_TURNED_ON
from salesfinder.settings import Settings
from salesfinder.writer.compose import assemble
from sending_helpers import (
    BOX,
    RECIPIENT,
    SECRET,
    SEND_NOW,
    SENT_FOLDER,
    SUPPLIER_SECRET,
    Ready,
    approve,
    ready_mailbox,
    seed_ready,
    sending_settings,
)


@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return sending_settings(test_settings)


def _checker() -> MxChecker:
    return MxChecker(CONFIG.contacts, resolve=FakeResolver())


async def _pass(
    engine: AsyncEngine,
    settings: Settings,
    sender: FakeSender,
    reader: FakeReader | None = None,
    *,
    at: datetime = SEND_NOW,
    config: AppConfig = CONFIG,
) -> SendOutcome:
    return await send_pass(
        make_session_factory(engine),
        mail=fake_mail(sender, reader),
        settings=settings,
        config=config,
        facts=FACTS,
        catalog=CATALOG,
        now=lambda: at,
        mx_checker=_checker(),
        rng=random.Random(0),  # noqa: S311
    )


async def _claim(
    engine: AsyncEngine,
    settings: Settings,
    ready: Ready,
    *,
    at: datetime = SEND_NOW,
    message_id: uuid.UUID | None = None,
    mailbox_id: uuid.UUID | None = None,
) -> ClaimResult:
    return await claim(
        make_session_factory(engine),
        message_id or ready.message_id,
        mailbox=mailbox_id or ready.mailbox_id,
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        catalog=CATALOG,
        now=at,
        rng=random.Random(0),  # noqa: S311
    )


async def _get[T](engine: AsyncEngine, model: type[T], key: Any) -> T:
    async with make_session_factory(engine)() as s:
        row = await s.get(model, key)
        assert row is not None
        return row


async def _rows[T](engine: AsyncEngine, model: type[T]) -> list[T]:
    async with make_session_factory(engine)() as s:
        return list((await s.scalars(select(model))).all())


async def _set(engine: AsyncEngine, model: Any, key: uuid.UUID, **values: Any) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(update(model).where(model.id == key).values(**values))
        await s.commit()


def _wire(message: EmailMessage) -> email.message.EmailMessage:
    """The message as the server receives it, parsed back."""
    parsed = email.message_from_bytes(message.as_bytes(), policy=email_policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


def _body(message: EmailMessage) -> str:
    content = _wire(message).get_content()
    assert isinstance(content, str)
    return content.replace("\r\n", "\n")


# --------------------------------------------------------------------------- paused by default


async def test_paused_by_default_sends_nothing(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db, resume=False)
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert outcome.paused == NOT_TURNED_ON
    assert (outcome.sent, outcome.held) == (0, {})
    result = await _claim(engine, settings, ready)
    assert (result.kind, result.reason) == ("held", NOT_TURNED_ON)
    assert sender.sent == []
    assert (await _get(engine, Message, ready.message_id)).status == "approved"


# --------------------------------------------------------------------------- happy path


async def test_happy_path_exact_bytes_and_snapshot(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert outcome == SendOutcome(sent=1)
    stored = await _get(engine, Message, ready.message_id)
    assert stored.status == "sent"
    assert stored.sent_at == SEND_NOW and stored.claimed_at == SEND_NOW
    [(from_box, envelope_to, mime)] = sender.sent
    assert (from_box, envelope_to) == (BOX, RECIPIENT)
    # Exactly the stored subject and body (the email API ends a body with a line break).
    assert _body(mime) == stored.body + ("" if stored.body.endswith("\n") else "\n")
    wire = _wire(mime)
    assert wire["Subject"] == stored.subject
    assert str(wire["From"]) == f"Asha Rao <{BOX}>"
    assert str(wire["To"]) == RECIPIENT
    assert wire["Message-ID"] == stored.message_id_header
    assert stored.message_id_header is not None
    assert stored.message_id_header.endswith("@send-domain.com>")
    assert wire["List-Unsubscribe"] == f"<mailto:{BOX}?subject=unsubscribe>"
    assert wire["In-Reply-To"] is None
    assert stored.mailbox_id == ready.mailbox_id

    snapshot = stored.send_snapshot
    assert snapshot is not None
    assert set(snapshot) == {f.name for f in fields(SendSnapshot)}
    assert snapshot == {
        "address_status": "published",
        "override_sendable": None,
        "blocked_reason": None,
        "mx_state": "has_mx",
        "mx_checked_at": SEND_NOW.isoformat(),
        "gate_problems": [],
        "mailbox_id": str(ready.mailbox_id),
        "mailbox_address": BOX,
        "persona_name": "Asha Rao",
        "zones": ["America/New_York", "America/Los_Angeles"],
        "envelope_to": RECIPIENT,
        "campaign_status": "active",
        "sending_paused": False,
        "hubspot_state": "fresh",
        "claimed_at": SEND_NOW.isoformat(),
    }

    # The follow-up is due 4 business days later at 09:00 New York time (Mon 16 Nov).
    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.due_at) == ("pending", datetime(2026, 11, 16, 14, tzinfo=UTC))
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.mailbox_id) == ("active", ready.mailbox_id)
    box = await _get(engine, Mailbox, ready.mailbox_id)
    assert box.hold_reason is None and box.next_send_at is not None
    [sent_audit] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_SENT]
    assert sent_audit.actor_id is None
    assert sent_audit.details["approved_by"] == str(ready.user_id)
    assert sent_audit.target_id == str(ready.message_id)


# --------------------------------------------------------------------------- re-gate


async def test_regate_at_send_refuses(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await add_entry(db, raw=RECIPIENT, reason="Asked us not to", added_by=None)
    await db.commit()
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert (outcome.sent, outcome.refused) == (0, 1)
    assert sender.sent == []
    stored = await _get(engine, Message, ready.message_id)
    assert stored.status == "gate_failed"
    assert stored.gate_errors is not None
    assert any(
        p.startswith("Recipient is excluded: Do-not-contact: jane.doe@acme-soap.com")
        for p in stored.gate_errors
    )
    # Back in the Outbox: a first email shows while its sequence awaits approval.
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "awaiting_approval"
    assert stored.message_id_header is None and stored.send_snapshot is None
    [held] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_HELD]
    assert held.actor_id is None and held.target_id == str(ready.message_id)
    assert held.details == {
        "sequence_id": str(ready.sequence_id),
        "campaign_id": str(ready.campaign_id),
        "stage": "send",
        "problems": len(stored.gate_errors),
    }


async def test_send_time_refusal_resets_a_retried_message(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Retried before (nothing delivered): a refusal starts it over, with a fresh Message-ID
    once it is approved again."""
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("retry", 421, "Try again later")})
    await _pass(engine, settings, sender)
    retried = await _get(engine, Message, ready.message_id)
    assert (retried.send_attempts, retried.message_id_header is not None) == (1, True)
    await add_entry(db, raw=RECIPIENT, reason="Asked us not to", added_by=None)
    await db.commit()

    assert (await _pass(engine, settings, sender, at=SEND_NOW + timedelta(minutes=10))).refused == 1

    stored = await _get(engine, Message, ready.message_id)
    assert stored.status == "gate_failed"
    assert (stored.send_attempts, stored.next_attempt_at, stored.message_id_header) == (
        0,
        None,
        None,
    )


async def test_send_time_refusal_keeps_the_id_of_mail_that_may_have_gone(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("ambiguous", None, "TimeoutError")})
    reader, _ = _sent_reader()
    await _pass(engine, settings, sender, reader)
    await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=11))
    unconfirmed = await _get(engine, Message, ready.message_id)
    assert unconfirmed.status == "unconfirmed"
    # "Send again" (Task 7), then the recipient is excluded before it goes.
    await _set(engine, Message, ready.message_id, status="approved", send_attempts=0)
    await add_entry(db, raw=RECIPIENT, reason="Asked us not to", added_by=None)
    await db.commit()

    result = await _claim(engine, settings, ready, at=SEND_NOW + timedelta(minutes=30))

    assert result.kind == "refused"
    stored = await _get(engine, Message, ready.message_id)
    assert stored.message_id_header == unconfirmed.message_id_header


# --------------------------------------------------------------------------- holds


HOLDS = {
    "campaign paused": CAMPAIGN_PAUSED,
    "no mailbox on campaign": NO_MAILBOX,
    "mailbox disabled": MAILBOX_OFF,
    "no password": no_password(SECRET),
    "DNS stale": DNS_NOT_READY,
    "HubSpot stale": HUBSPOT_STALE,
    "warm-up day 3": "Warming up: cold email starts on 2026-11-21.",
    "cap reached": "Today's cap reached (1/1).",
    "cap reached (in flight)": "Today's cap reached (1/1).",
    "08:59 local": OUTSIDE_HOURS,
    "spacing": SPACING,
    "recipient MX unconfirmed": MX_RETRY,
}


@pytest.mark.parametrize("case", list(HOLDS))
async def test_holds(engine: AsyncEngine, db: AsyncSession, settings: Settings, case: str) -> None:
    ready = await seed_ready(db, campaign_mailbox=case != "no mailbox on campaign")
    at = SEND_NOW
    match case:
        case "campaign paused":
            await _set(engine, Campaign, ready.campaign_id, status="paused")
        case "mailbox disabled":
            await _set(engine, Mailbox, ready.mailbox_id, enabled=False)
        case "no password":
            settings = settings.model_copy(update={"mailbox_passwords": {}})
        case "DNS stale":
            await _set(
                engine, Mailbox, ready.mailbox_id, dns_checked_at=SEND_NOW - timedelta(hours=25)
            )
        case "HubSpot stale":
            async with make_session_factory(engine)() as s:
                await s.execute(
                    update(HubSpotSync).values(finished_at=SEND_NOW - timedelta(hours=49))
                )
                await s.commit()
        case "warm-up day 3":
            await _set(
                engine,
                Mailbox,
                ready.mailbox_id,
                warmup_started_on=SEND_NOW.date() - timedelta(days=3),
            )
        case "cap reached":
            await _set(engine, Mailbox, ready.mailbox_id, daily_max=1)
            campaign = await _get(engine, Campaign, ready.campaign_id)
            other = await seed_draft(db, campaign, domain="other-soap.com", checked_at=SEND_NOW)
            await _set(
                engine,
                Message,
                other.id,
                status="sent",
                sent_at=SEND_NOW - timedelta(hours=2),
                mailbox_id=ready.mailbox_id,
            )
        case "cap reached (in flight)":
            # Claimed today, not yet confirmed: it counts (it may have gone).
            await _set(engine, Mailbox, ready.mailbox_id, daily_max=1)
            campaign = await _get(engine, Campaign, ready.campaign_id)
            other = await seed_draft(db, campaign, domain="other-soap.com", checked_at=SEND_NOW)
            await _set(
                engine,
                Message,
                other.id,
                status="unconfirmed",
                claimed_at=SEND_NOW - timedelta(hours=2),
                mailbox_id=ready.mailbox_id,
            )
        case "08:59 local":
            await _set(engine, Company, ready.company_id, region="NY")
            at = datetime(2026, 11, 10, 13, 59, tzinfo=UTC)  # 08:59 in New York
        case "spacing":
            await _set(
                engine, Mailbox, ready.mailbox_id, next_send_at=SEND_NOW + timedelta(minutes=2)
            )
        case "recipient MX unconfirmed":
            # The cached answer is too old and DNS times out now: wait, don't refuse.
            async with make_session_factory(engine)() as s:
                await s.execute(
                    update(DomainMx)
                    .where(DomainMx.domain == "acme-soap.com")
                    .values(checked_at=SEND_NOW - timedelta(days=60))
                )
                await s.commit()
            await refresh_mx(
                make_session_factory(engine),
                MxChecker(
                    CONFIG.contacts,
                    resolve=FakeResolver({("acme-soap.com", "MX"): dns.exception.Timeout()}),
                ),
                ["acme-soap.com"],
                now=SEND_NOW,
                config=CONFIG.contacts,
            )

    result = await _claim(engine, settings, ready, at=at)

    assert (result.kind, result.reason) == ("held", HOLDS[case])
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.claimed_at, stored.message_id_header) == ("approved", None, None)


async def test_a_hold_is_shown_on_the_mailbox_until_a_send(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _set(engine, Company, ready.company_id, region="NY")
    early = datetime(2026, 11, 10, 13, 59, tzinfo=UTC)  # 08:59 in New York
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender, at=early)

    assert outcome.held == {BOX: OUTSIDE_HOURS} and sender.sent == []
    assert (await _get(engine, Mailbox, ready.mailbox_id)).hold_reason == OUTSIDE_HOURS
    assert (await _pass(engine, settings, sender)).sent == 1
    assert (await _get(engine, Mailbox, ready.mailbox_id)).hold_reason is None


async def test_a_stale_hold_clears_when_nothing_is_held(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _set(engine, Mailbox, ready.mailbox_id, hold_reason=OUTSIDE_HOURS)
    await add_entry(db, raw=RECIPIENT, reason="Asked us not to", added_by=None)
    await db.commit()

    outcome = await _pass(engine, settings, FakeSender())

    assert (outcome.refused, outcome.held) == (1, {})  # refused, nothing held this pass
    assert (await _get(engine, Mailbox, ready.mailbox_id)).hold_reason is None


# --------------------------------------------------------------------------- replies


async def _reply_ready(db: AsyncSession, engine: AsyncEngine) -> tuple[Ready, uuid.UUID]:
    """Step 1 went out from the mailbox, they replied, and a teammate approved our answer."""
    ready = await seed_ready(db)
    await _set(
        engine,
        Message,
        ready.message_id,
        status="sent",
        sent_at=SEND_NOW - timedelta(days=1),
        message_id_header="<first@send-domain.com>",
        mailbox_id=ready.mailbox_id,
    )
    await _set(engine, Message, ready.step2_id, status="cancelled")
    await _set(engine, Sequence, ready.sequence_id, status="replied", mailbox_id=ready.mailbox_id)
    await _set(engine, Company, ready.company_id, region="NY")
    subject, body = assemble(
        "Re: Surfactants for your body wash line",
        "Hi Jane,\n\nThanks for your note. Happy to send a sample next week.\n\nThanks,",
        facts=FACTS,
        writing=CONFIG.writing,
        side="buyer",
    )
    reply = Message(
        sequence_id=ready.sequence_id,
        step=3,
        kind="reply",
        subject=subject,
        body=body,
        status="approved",
        approved_by_id=ready.user_id,
        approved_at=SEND_NOW - timedelta(hours=1),
        in_reply_to="<their-answer@acme-soap.com>",
        references_header="<first@send-domain.com> <their-answer@acme-soap.com>",
    )
    db.add(reply)
    await db.commit()
    return ready, reply.id


async def test_reply_kind_ignores_ramp_and_hours(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready, reply_id = await _reply_ready(db, engine)
    # Warm-up day 0 (no cold email for 14 days), stale DNS, and 03:00 in New York.
    await _set(
        engine,
        Mailbox,
        ready.mailbox_id,
        warmup_started_on=SEND_NOW.date(),
        dns_checked_at=SEND_NOW - timedelta(days=3),
    )
    at = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    sender = FakeSender()

    # The global pause still holds it.
    async with make_session_factory(engine)() as s:
        await state.pause(s, reason="Holiday break", actor=None, now=at - timedelta(minutes=5))
        await s.commit()
    paused = await _pass(engine, settings, sender, at=at)
    assert paused.paused == "Holiday break" and sender.sent == []
    held = await _claim(engine, settings, ready, at=at, message_id=reply_id)
    assert (held.kind, held.reason) == ("held", "Sending is paused: Holiday break")

    async with make_session_factory(engine)() as s:
        await state.resume(s, actor=None, now=at - timedelta(minutes=1))
        await s.commit()
    outcome = await _pass(engine, settings, sender, at=at)

    assert outcome.sent == 1
    [(_, envelope_to, mime)] = sender.sent
    assert envelope_to == RECIPIENT
    wire = _wire(mime)
    assert wire["In-Reply-To"] == "<their-answer@acme-soap.com>"
    assert wire["References"] == "<first@send-domain.com> <their-answer@acme-soap.com>"
    stored = await _get(engine, Message, reply_id)
    assert (stored.status, stored.sent_at) == ("sent", at)
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "replied"


async def test_followup_threads_under_step1_and_completes(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _set(
        engine,
        Message,
        ready.message_id,
        status="sent",
        sent_at=SEND_NOW - timedelta(days=6),
        message_id_header="<first@send-domain.com>",
        mailbox_id=ready.mailbox_id,
    )
    await _set(engine, Sequence, ready.sequence_id, mailbox_id=ready.mailbox_id)
    await _set(
        engine,
        Message,
        ready.step2_id,
        status="approved",
        approved_by_id=ready.user_id,
        approved_at=SEND_NOW - timedelta(hours=1),
    )
    sender = FakeSender()

    assert (await _pass(engine, settings, sender)).sent == 1

    [(_, _, mime)] = sender.sent
    wire = _wire(mime)
    assert wire["In-Reply-To"] == "<first@send-domain.com>"
    assert wire["References"] == "<first@send-domain.com>"
    step2 = await _get(engine, Message, ready.step2_id)
    assert (step2.status, step2.in_reply_to) == ("sent", "<first@send-domain.com>")
    assert wire["Subject"] == step2.subject
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "completed"


# --------------------------------------------------------------------------- never sends


async def test_archived_never_sends(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    # Archived after the candidate select: the message is still approved when claimed.
    await _set(engine, Campaign, ready.campaign_id, status="archived")
    sender = FakeSender()

    result = await _claim(engine, settings, ready)

    assert (result.kind, result.reason) == ("skipped", "Campaign archived")
    assert (await _get(engine, Message, ready.message_id)).status == "cancelled"
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Campaign archived")
    assert (await _pass(engine, settings, sender)).sent == 0
    assert sender.sent == []


async def test_only_status_approved(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _set(engine, Message, ready.message_id, status="cancelled")
    stored = await _get(engine, Message, ready.message_id)
    assert stored.approved_by_id is not None and stored.approved_at is not None
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert (outcome.sent, outcome.held) == (0, {})
    assert (await _claim(engine, settings, ready)).kind == "skipped"
    assert sender.sent == []
    assert (await _get(engine, Message, ready.message_id)).status == "cancelled"


# --------------------------------------------------------------------------- outcomes


async def test_retry_then_fail(engine: AsyncEngine, db: AsyncSession, settings: Settings) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("retry", 421, "Try again later")})

    first = await _pass(engine, settings, sender)
    assert first.retried == 1
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.send_attempts) == ("approved", 1)
    assert stored.next_attempt_at == SEND_NOW + timedelta(minutes=5)
    assert stored.last_send_error == "SMTP 421: Try again later"
    assert stored.message_id_header is not None
    header = stored.message_id_header

    # Not before next_attempt_at, even with the mailbox free again.
    assert (await _pass(engine, settings, sender, at=SEND_NOW + timedelta(minutes=4))) == (
        SendOutcome()
    )
    second_at = SEND_NOW + timedelta(minutes=10)
    assert (await _pass(engine, settings, sender, at=second_at)).retried == 1
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.send_attempts) == ("approved", 2)
    assert stored.next_attempt_at == second_at + timedelta(minutes=15)
    assert (await _pass(engine, settings, sender, at=second_at + timedelta(minutes=14))).sent == 0
    assert len(sender.sent) == 2

    third = await _pass(engine, settings, sender, at=second_at + timedelta(minutes=16))
    assert third.failed == 1
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.send_attempts, stored.next_attempt_at) == ("failed", 3, None)
    assert stored.message_id_header == header  # one Message-ID for every attempt
    assert len(sender.sent) == 3
    assert {_wire(m)["Message-ID"] for _, _, m in sender.sent} == {header}
    [gave_up] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_SEND_FAILED]
    assert gave_up.actor_id is None and gave_up.target_id == str(ready.message_id)
    assert gave_up.details == {
        "sequence_id": str(ready.sequence_id),
        "campaign_id": str(ready.campaign_id),
        "outcome": "retry",
        "code": 421,
        "attempts": 3,
    }


async def test_rcpt_rejected_bounces_and_suppresses(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("rejected", 550, "5.1.1 No such user")})

    outcome = await _pass(engine, settings, sender)

    assert (outcome.sent, outcome.failed) == (0, 1)
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.bounced_at, stored.sent_at) == ("bounced", SEND_NOW, SEND_NOW)
    assert stored.bounce_detail == "SMTP 550: 5.1.1 No such user"
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Bounced")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    [suppression] = await _rows(engine, Suppression)
    assert (suppression.address, suppression.reason, suppression.message_id) == (
        RECIPIENT,
        "bounced",
        ready.message_id,
    )
    [address] = await _rows(engine, EmailAddress)
    assert address.status == "invalid"
    assert address.blocked_reason is not None  # refreshed after the locked transaction
    company = await _get(engine, Company, ready.company_id)
    assert company.sendable_contacts == 0
    actions = [a.action for a in await _rows(engine, AuditLog)]
    assert audit.SEQUENCE_STOPPED in actions and audit.SUPPRESSION_ADDED in actions
    logged = capsys.readouterr()
    assert "send_pass" in logged.out  # the log is captured here
    for text_ in ("No such user", stored.subject, RECIPIENT):  # server text, content, address
        assert text_ not in logged.out + logged.err


async def test_bounce_drops_pattern(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    domain = "acme-soap.com"
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, mailbox=False, activate=False)
    box = await ready_mailbox(db, user.id)
    row = await db.get(Campaign, campaign.id)
    assert row is not None
    row.status, row.mailbox_id = "active", box.id
    db.add(HubSpotSync(status="succeeded", finished_at=SEND_NOW - timedelta(hours=1)))
    company, jane, jane_row = await seed_company(
        db, domain=domain, address="jane.doe", status="pattern_confirmed", checked_at=SEND_NOW
    )
    assert jane_row is not None
    john = await add_person(db, company, "John Smith", rank=2)
    sibling = await add_address(
        db, company, john, f"john.smith@{domain}", status="pattern_confirmed"
    )
    ravi = await add_person(db, company, "Ravi Kumar", rank=None, target=False)
    await add_address(db, company, ravi, f"ravi.kumar@{domain}")  # published: the evidence
    db.add(
        EmailPattern(
            domain=domain,
            pattern="{first}.{last}",
            support=1,
            examples=[f"ravi.kumar@{domain}"],
            learned_at=SEND_NOW - timedelta(days=1),
        )
    )
    sequence = Sequence(
        campaign_id=campaign.id,
        company_id=company.id,
        person_id=jane.id,
        email_address_id=jane_row.id,
        recipient=jane_row.address,
        status="active",
    )
    db.add(sequence)
    await db.flush()
    subject, body = assemble(
        CLEAN["first_subject"], CLEAN["first_body"], facts=FACTS, writing=CONFIG.writing,
        side="buyer",
    )  # fmt: skip
    message = Message(
        sequence_id=sequence.id,
        step=1,
        subject=subject,
        body=body,
        status="approved",
        approved_by_id=user.id,
        approved_at=SEND_NOW - timedelta(hours=1),
    )
    db.add(message)
    await state.resume(db, actor=None, now=SEND_NOW - timedelta(days=1))
    await db.commit()
    sender = FakeSender({RECIPIENT: SendResult("rejected", 550, "5.1.1 No such user")})

    assert (await _pass(engine, settings, sender)).failed == 1

    pattern = await _get(engine, EmailPattern, domain)
    assert pattern.bounced_at == SEND_NOW
    john_row = await _get(engine, EmailAddress, sibling.id)
    assert (john_row.status, john_row.source) == ("pattern_guess", "guess")
    assert (await _get(engine, EmailAddress, jane_row.id)).status == "invalid"

    # A later contacts check relearns {first}.{last} from Ravi's published address, but never
    # confirms it again: John stays a guess and Jane stays invalid.
    checker = MxChecker(CONFIG.contacts, resolve=FakeResolver({(domain, "MX"): [(10, "mx.a.")]}))
    await verify_company(
        make_session_factory(engine),
        company.id,
        checker=checker,
        verifier=None,
        config=CONFIG,
        now=SEND_NOW + timedelta(hours=1),
    )
    john_row = await _get(engine, EmailAddress, sibling.id)
    assert (john_row.status, john_row.source) == ("pattern_guess", "guess")
    jane_after = await _get(engine, EmailAddress, jane_row.id)
    assert jane_after.status == "invalid"
    pattern = await _get(engine, EmailPattern, domain)
    assert (pattern.pattern, pattern.bounced_at) == ("{first}.{last}", SEND_NOW)


async def test_auth_failure_disables_mailbox(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("mailbox_error", 535, "5.7.8 Bad credentials")})

    outcome = await _pass(engine, settings, sender)

    assert outcome.held == {BOX: AUTH_FAILED} and outcome.sent == 0
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.send_attempts) == ("approved", 0)
    box = await _get(engine, Mailbox, ready.mailbox_id)
    assert (box.enabled, box.hold_reason, box.last_error_at) == (False, AUTH_FAILED, SEND_NOW)
    assert box.last_error == "SMTP 535: 5.7.8 Bad credentials"
    disabled = [a for a in await _rows(engine, AuditLog) if a.action == audit.MAILBOX_DISABLED]
    assert len(disabled) == 1 and disabled[0].actor_id is None
    # The disabled mailbox isn't tried again, and its reason stays.
    assert await _pass(engine, settings, sender, at=SEND_NOW + timedelta(hours=1)) == (
        SendOutcome()
    )
    assert len(sender.sent) == 1
    assert (await _get(engine, Mailbox, ready.mailbox_id)).hold_reason == AUTH_FAILED


@pytest.mark.parametrize("hold", [None, "SETTINGS_CHANGED", "NOT_TESTED"])
async def test_a_mailbox_turned_off_or_held_after_the_claim_gets_no_smtp(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, hold: str | None
) -> None:
    """Final-wave race (b): deliver re-reads the mailbox; turned off, or held untested
    (P6-V/W), since the claim -> no SMTP, a retry that puts the message back to approved."""
    from salesfinder.sending import mailboxes
    from salesfinder.sending.sender import MAILBOX_OFF_NOW

    ready = await seed_ready(db)
    assert (await _claim(engine, settings, ready)).kind == "claimed"
    await _set(
        engine,
        Mailbox,
        ready.mailbox_id,
        enabled=False,
        hold_reason=getattr(mailboxes, hold) if hold else None,
    )
    sender = FakeSender()

    result = await deliver(
        make_session_factory(engine),
        ready.message_id,
        mail=fake_mail(sender, None),
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        now=SEND_NOW,
    )

    assert (result.outcome, result.detail) == ("retry", MAILBOX_OFF_NOW)
    assert sender.sent == []
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.last_send_error) == ("approved", MAILBOX_OFF_NOW)
    assert stored.sent_at is None


async def test_a_sign_in_failure_never_replaces_an_untested_hold(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Final-wave race (a): the admin changed the settings while the send was on the wire;
    its sign-in failure disables the mailbox but keeps SETTINGS_CHANGED (AUTH_FAILED would
    let polling and enabling reach the untested host)."""
    from salesfinder.sending import sender as sender_module
    from salesfinder.sending.mailboxes import SETTINGS_CHANGED

    ready = await seed_ready(db)
    assert (await _claim(engine, settings, ready)).kind == "claimed"
    await _set(engine, Mailbox, ready.mailbox_id, hold_reason=SETTINGS_CHANGED)
    failure = SendResult("mailbox_error", 535, "5.7.8 Bad credentials")

    async with make_session_factory(engine)() as s:
        await sender_module.record(s, ready.message_id, failure, now=SEND_NOW, config=CONFIG)

    box = await _get(engine, Mailbox, ready.mailbox_id)
    assert (box.enabled, box.hold_reason) == (False, SETTINGS_CHANGED)
    assert (await _get(engine, Message, ready.message_id)).status == "approved"


# --------------------------------------------------------------------------- reconcile


def _sent_reader() -> tuple[FakeReader, FakeFolder]:
    folder = FakeFolder(uidvalidity=1)
    return FakeReader({SENT_FOLDER: folder}), folder


async def test_crash_after_smtp_reconciles_from_sent(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender()
    reader, folder = _sent_reader()

    async def crashed(*_args: Any, **_kwargs: Any) -> None:
        return None  # the process died between SMTP success and recording it

    with monkeypatch.context() as patch:
        patch.setattr(sender_module, "record", crashed)
        await _pass(engine, settings, sender, reader)
    assert len(sender.sent) == 1
    assert (await _get(engine, Message, ready.message_id)).status == "sending"
    folder.add(sender.sent[0][2].as_bytes())  # Gmail saved it to Sent

    # Too early: left alone.
    assert (await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=9))) == (
        SendOutcome()
    )
    later = await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=11))

    assert (later.confirmed, later.unconfirmed, later.sent) == (1, 0, 0)
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.sent_at) == ("sent", SEND_NOW)  # sent at the claim time
    assert (await _get(engine, Message, ready.step2_id)).due_at is not None
    assert len(sender.sent) == 1  # never resent
    assert reader.closed


async def test_ambiguous_not_found_is_unconfirmed(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("ambiguous", None, "TimeoutError")})
    reader, _ = _sent_reader()

    first = await _pass(engine, settings, sender, reader)
    assert first == SendOutcome()  # nothing settled yet
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.last_send_error) == ("sending", "TimeoutError")

    later = await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=11))
    assert (later.unconfirmed, later.confirmed, later.sent) == (1, 0, 0)
    stored = await _get(engine, Message, ready.message_id)
    assert stored.status == "unconfirmed"
    [audited] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_UNCONFIRMED]
    assert audited.actor_id is None and audited.details["reason"] == "not_in_sent"
    assert await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(hours=1)) == (
        SendOutcome()
    )
    assert len(sender.sent) == 1


async def test_reconcile_reader_error_leaves_it_for_later(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("ambiguous", None, "TimeoutError")})
    await _pass(engine, settings, sender, FakeReader({}))
    broken = FakeReader({}, fail=OSError("connection reset"))

    later = await _pass(engine, settings, sender, broken, at=SEND_NOW + timedelta(minutes=11))

    assert (later.confirmed, later.unconfirmed) == (0, 0)
    assert (await _get(engine, Message, ready.message_id)).status == "sending"


@pytest.mark.parametrize("hold", ["SETTINGS_CHANGED", "NOT_TESTED"])
async def test_reconcile_never_logs_in_to_untested_settings(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, hold: str
) -> None:
    """Rulings P6-V/P6-W: a mailbox whose connection settings haven't passed a test (changed,
    or never tested) isn't read for reconcile either; its rows wait (and escalate after a day
    as usual)."""
    from salesfinder.sending import mailboxes

    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("ambiguous", None, "TimeoutError")})
    await _pass(engine, settings, sender, FakeReader({}))
    await _set(
        engine, Mailbox, ready.mailbox_id, enabled=False, hold_reason=getattr(mailboxes, hold)
    )
    reader, folder = _sent_reader()
    folder.add(sender.sent[0][2].as_bytes())  # it is in Sent, but we may not look

    later = await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=11))

    assert (later.confirmed, later.unconfirmed) == (0, 0)
    assert (await _get(engine, Message, ready.message_id)).status == "sending"
    assert not reader.closed  # never opened


async def test_message_id_reused_on_resend(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender(
        {RECIPIENT: [SendResult("ambiguous", None, "TimeoutError"), SendResult("sent", 250, "OK")]}
    )
    reader, _ = _sent_reader()
    await _pass(engine, settings, sender, reader)
    await _pass(engine, settings, sender, reader, at=SEND_NOW + timedelta(minutes=11))
    unconfirmed = await _get(engine, Message, ready.message_id)
    assert unconfirmed.status == "unconfirmed"
    header = unconfirmed.message_id_header
    assert header is not None

    # A teammate chose "Send again": approved again, Message-ID kept.
    at = SEND_NOW + timedelta(minutes=30)
    async with make_session_factory(engine)() as s:
        actor = await s.get(User, ready.user_id)
        assert actor is not None
        await outbox.resend(s, ready.message_id, actor=actor, now=at)
        await s.commit()
    result = await _claim(engine, settings, ready, at=at)
    assert result.kind == "claimed"
    assert (await _get(engine, Message, ready.message_id)).message_id_header == header
    await deliver(
        make_session_factory(engine),
        ready.message_id,
        mail=fake_mail(sender, reader),
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        now=at,
    )
    assert [_wire(m)["Message-ID"] for _, _, m in sender.sent] == [header, header]
    assert (await _get(engine, Message, ready.message_id)).status == "sent"


# --------------------------------------------------------------------------- pacing


async def test_one_send_per_mailbox_per_pass_and_spacing(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    campaign = await _get(engine, Campaign, ready.campaign_id)
    other = await seed_draft(db, campaign, domain="bubbles.com", checked_at=SEND_NOW)
    await approve(db, other, ready.user_id)
    await db.commit()
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert outcome.sent == 1 and len(sender.sent) == 1
    box = await _get(engine, Mailbox, ready.mailbox_id)
    assert box.next_send_at is not None
    assert (
        SEND_NOW + timedelta(seconds=180) <= box.next_send_at <= SEND_NOW + timedelta(seconds=540)
    )
    # The mailbox isn't due again until next_send_at.
    assert (await _pass(engine, settings, sender)).sent == 0
    assert (await _pass(engine, settings, sender, at=box.next_send_at)).sent == 1
    assert {to for _, to, _ in sender.sent} == {RECIPIENT, "jane.doe@bubbles.com"}


# --------------------------------------------------------------------------- transactions


class _ProbeSender(FakeSender):
    """Looks at the sender's own database backends while "SMTP" runs."""

    def __init__(self, probe: AsyncEngine, app_name: str) -> None:
        super().__init__()
        self.probe = probe
        self.app_name = app_name
        self.states: list[str] = []

    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult:
        async with self.probe.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT pid, state FROM pg_stat_activity "
                    "WHERE application_name = :name AND pid <> pg_backend_pid()"
                ),
                {"name": self.app_name},
            )
            self.states = [str(r.state) for r in rows.all()]
        return await super().send(
            creds, message, envelope_from=envelope_from, envelope_to=envelope_to
        )


async def test_no_transaction_during_smtp(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, database_url: str
) -> None:
    await seed_ready(db)
    app_name = f"sf_sender_{uuid.uuid4().hex[:8]}"
    worker_engine = create_async_engine(
        database_url, connect_args={"server_settings": {"application_name": app_name}}
    )
    sender = _ProbeSender(engine, app_name)
    try:
        outcome = await send_pass(
            make_session_factory(worker_engine),
            mail=fake_mail(sender, None),
            settings=settings,
            config=CONFIG,
            facts=FACTS,
            catalog=CATALOG,
            now=lambda: SEND_NOW,
            mx_checker=_checker(),
            rng=random.Random(0),  # noqa: S311
        )
    finally:
        await worker_engine.dispose()

    assert outcome.sent == 1
    assert sender.states, "the sender's connections should be visible (pooled, idle)"
    assert all(s == "idle" for s in sender.states), sender.states


# --------------------------------------------------------------------------- cross side


async def test_cross_side_same_address_refused(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Phase 4 #6 at send time: the same person approved on both sides (a race the approval
    gate can't see) is refused, not sent twice."""
    ready = await seed_ready(db)
    user_id = ready.user_id
    supplier = await seed_campaign(db, mailbox=False, side="supplier", activate=False)
    box = await ready_mailbox(
        db, user_id, address="sourcing@send-domain.com", side="supplier", secret=SUPPLIER_SECRET
    )
    row = await db.get(Campaign, supplier.id)
    assert row is not None
    row.status, row.mailbox_id = "active", box.id
    company = Company(
        side="supplier",
        name="Acme Soap Works",
        domain="acme-soap.com",
        website_url="https://acme-soap.com",
        status="new",
        profile_status="ok",
        profiled_at=SEND_NOW,
        scored_at=SEND_NOW,
        score=50,
        contacts_checked_at=SEND_NOW,
        contacts_found=1,
        sendable_contacts=1,
    )
    db.add(company)
    await db.flush()
    person = await add_person(db, company, "Jane Doe", rank=1)
    address = await add_address(db, company, person, RECIPIENT)
    sequence = Sequence(
        campaign_id=supplier.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=address.id,
        recipient=RECIPIENT,
        status="active",
    )
    db.add(sequence)
    await db.flush()
    subject, body = assemble(
        CLEAN["first_subject"], CLEAN["first_body"], facts=FACTS, writing=CONFIG.writing,
        side="supplier",
    )  # fmt: skip
    message = Message(
        sequence_id=sequence.id,
        step=1,
        subject=subject,
        body=body,
        status="approved",
        approved_by_id=user_id,
        approved_at=SEND_NOW,
    )
    db.add(message)
    await db.commit()
    # 06:00 UTC: office hours in both India and Jakarta (a supplier with no country).
    at = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)

    result = await _claim(engine, settings, ready, at=at, message_id=message.id, mailbox_id=box.id)

    assert result.kind == "refused"
    stored = await _get(engine, Message, message.id)
    assert stored.status == "gate_failed"
    assert stored.gate_errors is not None and ALREADY_CONTACTED in stored.gate_errors


def test_today_is_a_weekday() -> None:
    assert date(2026, 11, 10).weekday() == 1  # the fixed send time is a Tuesday


# --------------------------------------------------------------------------- fix round 1


async def test_paused_campaign_never_starves_another(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """25 older approved emails of a paused campaign share the mailbox with an active
    campaign's newer one: the active one goes in the first pass."""
    ready = await seed_ready(db)
    paused = await _get(engine, Campaign, ready.campaign_id)
    for i in range(24):
        older = await seed_draft(db, paused, domain=f"old{i}.com", checked_at=SEND_NOW)
        await approve(db, older, ready.user_id)
        older.approved_at = SEND_NOW - timedelta(days=1)
    await db.commit()
    await _set(engine, Campaign, ready.campaign_id, status="paused")
    other = await seed_campaign(db, user=None, mailbox=False, activate=False)
    await _set(engine, Campaign, other.id, status="active", mailbox_id=ready.mailbox_id)
    newer = await seed_draft(db, other, domain="fresh-soap.com", checked_at=SEND_NOW)
    await approve(db, newer, ready.user_id)
    await db.commit()
    sender = FakeSender()

    outcome = await _pass(engine, settings, sender)

    assert outcome.sent == 1
    assert [to for _, to, _ in sender.sent] == ["jane.doe@fresh-soap.com"]


async def test_a_mailbox_wide_hold_stops_the_mailboxs_pass(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = await seed_ready(db)
    campaign = await _get(engine, Campaign, ready.campaign_id)
    for domain in ("b-soap.com", "c-soap.com"):
        await approve(db, await seed_draft(db, campaign, domain=domain, checked_at=SEND_NOW),
                      ready.user_id)  # fmt: skip
    await db.commit()
    await _set(engine, Mailbox, ready.mailbox_id, dns_checked_at=SEND_NOW - timedelta(days=2))
    claims: list[uuid.UUID] = []
    real = sender_module.claim

    async def counting(db_factory: Any, message_id: uuid.UUID, **kwargs: Any) -> ClaimResult:
        claims.append(message_id)
        return await real(db_factory, message_id, **kwargs)

    monkeypatch.setattr(sender_module, "claim", counting)

    outcome = await _pass(engine, settings, FakeSender())

    assert outcome.held == {BOX: DNS_NOT_READY}
    assert len(claims) == 1  # the other two would only be held the same way


@pytest.mark.parametrize("outcome", ["retry", "mailbox_error"])
async def test_mail_that_didnt_go_is_cancelled_if_its_sequence_stopped(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, outcome: str
) -> None:
    """Archive (or a reply) stopped the sequence while the email was on the way and it didn't
    go: it is cancelled, not left approved where every pass would skip it."""
    ready = await seed_ready(db)
    assert (await _claim(engine, settings, ready)).kind == "claimed"
    await _set(engine, Sequence, ready.sequence_id, status="stopped")
    result = SendResult(outcome, 421 if outcome == "retry" else 535, "nope")  # type: ignore[arg-type]

    await deliver(
        make_session_factory(engine),
        ready.message_id,
        mail=fake_mail(FakeSender({RECIPIENT: result}), None),
        settings=settings,
        config=CONFIG,
        facts=FACTS,
        now=SEND_NOW,
    )

    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.next_attempt_at) == ("cancelled", None)
    [cancelled] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_CANCELLED]
    assert cancelled.actor_id is None and cancelled.target_id == str(ready.message_id)
    assert cancelled.details == {
        "sequence_id": str(ready.sequence_id),
        "campaign_id": str(ready.campaign_id),
        "reason": "sequence_moved_on",
        "outcome": outcome,
        "code": result.code,
    }


async def test_kill_switch_trips_from_a_refusal_at_rcpt(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    campaign = await _get(engine, Campaign, ready.campaign_id)
    # Two earlier bounces since sending was resumed: 2/50 is 4%, under the 5% limit.
    for domain in ("gone1.com", "gone2.com"):
        old = await seed_draft(db, campaign, domain=domain, checked_at=SEND_NOW)
        await _set(
            engine,
            Message,
            old.id,
            status="bounced",
            sent_at=SEND_NOW - timedelta(hours=3),
            bounced_at=SEND_NOW - timedelta(hours=3),
            mailbox_id=ready.mailbox_id,
        )
    # A second mailbox (later in address order) with its own campaign and approved email.
    second = await ready_mailbox(
        db, ready.user_id, address="z-sales@send-domain.com", secret=SUPPLIER_SECRET
    )
    other = await seed_campaign(db, mailbox=False, activate=False)
    await _set(engine, Campaign, other.id, status="active", mailbox_id=second.id)
    waiting = await seed_draft(db, other, domain="waiting-soap.com", checked_at=SEND_NOW)
    await approve(db, waiting, ready.user_id)
    await db.commit()
    sender = FakeSender({RECIPIENT: SendResult("rejected", 550, "5.1.1 No such user")})

    outcome = await _pass(engine, settings, sender)

    reason = "Paused automatically: 3 of the last 50 emails bounced (6%)."
    async with make_session_factory(engine)() as s:
        now_state = await state.get_state(s)
        bounces, window = await state.bounce_rate(s, now=SEND_NOW, config=CONFIG)
    assert (now_state.paused, now_state.reason) == (True, reason)
    assert (bounces, window) == (3, 3)  # the refusal at RCPT counts in the window
    paused = [a for a in await _rows(engine, AuditLog) if a.action == audit.SENDING_AUTO_PAUSED]
    assert len(paused) == 1 and paused[0].actor_id is None
    assert paused[0].details["reason"] == reason
    assert outcome.held == {"z-sales@send-domain.com": f"Sending is paused: {reason}"}
    assert [to for _, to, _ in sender.sent] == [RECIPIENT]
    assert (await _get(engine, Message, waiting.id)).status == "approved"


async def test_failed_at_data_is_final(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("failed", 554, "5.7.1 Message rejected")})

    outcome = await _pass(engine, settings, sender)

    assert (outcome.failed, outcome.sent, outcome.retried) == (1, 0, 0)
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.last_send_error) == ("failed", "SMTP 554: 5.7.1 Message rejected")
    [gave_up] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_SEND_FAILED]
    assert (gave_up.details["outcome"], gave_up.details["code"]) == ("failed", 554)
    assert await _pass(engine, settings, sender, at=SEND_NOW + timedelta(hours=1)) == SendOutcome()
    assert len(sender.sent) == 1


class _CrashingSender(FakeSender):
    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult:
        self.sent.append((creds.address, envelope_to, message))
        raise RuntimeError("connection dropped mid-DATA")


async def test_a_crashing_sender_is_ambiguous(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = _CrashingSender()

    outcome = await _pass(engine, settings, sender)

    assert outcome == SendOutcome()
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.last_send_error) == ("sending", "RuntimeError")
    assert len(sender.sent) == 1


async def test_sending_row_stuck_a_day_becomes_unconfirmed(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    sender = FakeSender({RECIPIENT: SendResult("ambiguous", None, "TimeoutError")})
    await _pass(engine, settings, sender)
    no_login = settings.model_copy(update={"mailbox_passwords": {}})

    soon = await _pass(engine, no_login, sender, at=SEND_NOW + timedelta(hours=2))
    assert soon.unconfirmed == 0
    assert (await _get(engine, Message, ready.message_id)).status == "sending"
    broken = FakeReader({}, fail=OSError("connection reset"))
    later = await _pass(engine, settings, sender, broken, at=SEND_NOW + timedelta(hours=25))

    assert later.unconfirmed == 1
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.last_send_error) == ("unconfirmed", NOT_CHECKABLE)
    [audited] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_UNCONFIRMED]
    assert audited.actor_id is None and audited.details["reason"] == "not_checkable"
    assert len(sender.sent) == 1


# --------------------------------------------------------------------------- fix round 2


async def test_reply_in_a_paused_campaign_is_sent(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-I: pausing a campaign stops cold outreach only; answers to people who wrote to
    us still go. The global pause stops everything."""
    ready, reply_id = await _reply_ready(db, engine)
    campaign = await _get(engine, Campaign, ready.campaign_id)
    cold = await seed_draft(db, campaign, domain="cold-soap.com", checked_at=SEND_NOW)
    await approve(db, cold, ready.user_id)
    await db.commit()
    await _set(engine, Campaign, ready.campaign_id, status="paused")
    async with make_session_factory(engine)() as s:
        candidates = await sender_module._candidates(s, ready.mailbox_id, now=SEND_NOW, limit=20)
    assert [message_id for message_id, _ in candidates] == [reply_id]  # never the first email
    sender = FakeSender()

    async with make_session_factory(engine)() as s:
        await state.pause(s, reason="Holiday break", actor=None, now=SEND_NOW)
        await s.commit()
    assert (await _pass(engine, settings, sender)).paused == "Holiday break"
    held = await _claim(engine, settings, ready, message_id=reply_id)
    assert (held.kind, held.reason) == ("held", "Sending is paused: Holiday break")
    assert sender.sent == []

    async with make_session_factory(engine)() as s:
        await state.resume(s, actor=None, now=SEND_NOW)
        await s.commit()
    assert (await _pass(engine, settings, sender)).sent == 1
    assert [to for _, to, _ in sender.sent] == [RECIPIENT]
    assert (await _get(engine, Message, reply_id)).status == "sent"
    box = await _get(engine, Mailbox, ready.mailbox_id)
    assert box.next_send_at is not None
    assert (await _pass(engine, settings, sender, at=box.next_send_at)).sent == 0
    assert (await _get(engine, Message, cold.id)).status == "approved"
    first = await _claim(engine, settings, ready, message_id=cold.id, at=box.next_send_at)
    assert (first.kind, first.reason) == ("held", CAMPAIGN_PAUSED)


async def _stale_mx(engine: AsyncEngine) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(
            update(DomainMx)
            .where(DomainMx.domain == "acme-soap.com")
            .values(checked_at=SEND_NOW - timedelta(days=60))
        )
        await s.commit()


async def test_unconfirmed_recipient_mx_backs_off(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _stale_mx(engine)

    held = await _claim(engine, settings, ready)
    assert (held.kind, held.reason) == ("held", MX_RETRY)
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.next_attempt_at) == ("approved", SEND_NOW + timedelta(minutes=30))
    [marker] = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_HELD]
    assert marker.actor_id is None
    assert (marker.details["hold"], marker.details["since"]) == (
        "mx_unconfirmed",
        SEND_NOW.isoformat(),
    )
    # Not a candidate while it backs off; tried again after.
    async with make_session_factory(engine)() as s:
        assert (
            await sender_module._candidates(
                s, ready.mailbox_id, now=SEND_NOW + timedelta(minutes=10), limit=20
            )
            == []
        )
    later = SEND_NOW + timedelta(minutes=31)
    assert (await _claim(engine, settings, ready, at=later)).reason == MX_RETRY
    stored = await _get(engine, Message, ready.message_id)
    assert stored.next_attempt_at == later + timedelta(minutes=30)
    # One marker: the wait is measured from the first hold.
    assert sum(a.action == audit.MESSAGE_HELD for a in await _rows(engine, AuditLog)) == 1


async def test_unconfirmed_recipient_mx_is_refused_after_a_day(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await seed_ready(db)
    await _stale_mx(engine)
    assert (await _claim(engine, settings, ready)).reason == MX_RETRY
    at = SEND_NOW + timedelta(hours=25)  # Wednesday 14:00 in New York
    await _set(engine, Mailbox, ready.mailbox_id, dns_checked_at=at - timedelta(hours=1))

    result = await _claim(engine, settings, ready, at=at)

    assert result.kind == "refused"
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.gate_errors) == ("gate_failed", [MX_OUT_OF_DATE])
    # The gate's problems say why; the "will retry" line is stale now (final-wave T6).
    assert stored.last_send_error is None
    refusals = [
        a
        for a in await _rows(engine, AuditLog)
        if a.action == audit.MESSAGE_HELD and "problems" in a.details
    ]
    assert len(refusals) == 1 and refusals[0].details["problems"] == 1


async def test_an_ended_mx_wait_doesnt_start_the_next_one_late(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Final-wave T6: a wait that ended (the message was claimed after it) isn't where a later
    wait starts: markers count from max(approved_at, claimed_at)."""
    ready = await seed_ready(db)
    await _stale_mx(engine)
    assert (await _claim(engine, settings, ready)).reason == MX_RETRY  # wait 1 starts
    # The MX was confirmed and the message claimed an hour later; the attempt was retried.
    await _set(
        engine,
        Message,
        ready.message_id,
        claimed_at=SEND_NOW + timedelta(hours=1),
        next_attempt_at=None,
        last_send_error=None,
    )
    at = SEND_NOW + timedelta(hours=25)  # Wednesday 14:00 in New York
    await _set(engine, Mailbox, ready.mailbox_id, dns_checked_at=at - timedelta(hours=1))

    result = await _claim(engine, settings, ready, at=at)

    assert (result.kind, result.reason) == ("held", MX_RETRY)  # wait 2 starts now
    stored = await _get(engine, Message, ready.message_id)
    assert (stored.status, stored.next_attempt_at) == ("approved", at + timedelta(minutes=30))
    markers = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_HELD]
    assert sorted(m.details["since"] for m in markers) == [SEND_NOW.isoformat(), at.isoformat()]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("taken_over", "A teammate took this conversation over."),
        ("handled", "This conversation was marked handled."),
        ("open", None),
        ("needs_you", None),
    ],
)
async def test_reply_never_goes_in_a_closed_conversation(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    status: str,
    reason: str | None,
) -> None:
    """Ruling P6-A: an approved reply is cancelled at claim time once its conversation was
    taken over or marked handled; an open one sends."""
    ready, reply_id = await _reply_ready(db, engine)
    async with make_session_factory(engine)() as s:
        s.add(
            Conversation(sequence_id=ready.sequence_id, mailbox_id=ready.mailbox_id, status=status)
        )
        await s.commit()
    sender = FakeSender()

    result = await _claim(engine, settings, ready, message_id=reply_id)

    stored = await _get(engine, Message, reply_id)
    if reason is None:
        assert result.kind == "claimed" and stored.status == "sending"
        return
    assert (result.kind, result.reason) == ("skipped", reason)
    assert stored.status == "cancelled"
    cancelled = [a for a in await _rows(engine, AuditLog) if a.action == audit.MESSAGE_CANCELLED]
    assert len(cancelled) == 1 and cancelled[0].details["reason"] == f"conversation_{status}"
    assert (await _pass(engine, settings, sender)).sent == 0
    assert sender.sent == []


@pytest.mark.parametrize("status", ["taken_over", "handled"])
async def test_a_teammate_written_reply_goes_in_a_closed_conversation(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    status: str,
) -> None:
    """Ruling P6-N: the closed-conversation cancel is for Claude's drafts only; a reply a
    teammate wrote themselves (``written_by_id``) is the takeover and still goes out."""
    ready, reply_id = await _reply_ready(db, engine)
    await _set(engine, Message, reply_id, written_by_id=ready.user_id)
    async with make_session_factory(engine)() as s:
        s.add(
            Conversation(sequence_id=ready.sequence_id, mailbox_id=ready.mailbox_id, status=status)
        )
        await s.commit()

    result = await _claim(engine, settings, ready, message_id=reply_id)

    assert result.kind == "claimed"
    assert (await _get(engine, Message, reply_id)).status == "sending"
