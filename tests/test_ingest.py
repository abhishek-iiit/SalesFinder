"""Mail ingest (Task 8): match, bounces, unsubscribes, auto-replies and takeover, read from a
FakeReader serving Task 2's fixtures rewritten to our Message-IDs. Real DB, fixed clocks."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import CONFIG, add_address, add_person, seed_draft
from fakes.dns import FakeResolver
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from mail_helpers import dsn_for, fixture, reply_to, sent_copy
from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker
from salesfinder.contacts.service import verify_company
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    Company,
    Conversation,
    EmailAddress,
    EmailPattern,
    Mailbox,
    MailboxFolder,
    MailboxMessage,
    Message,
    Sequence,
    Suppression,
)
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.imap import MailReader
from salesfinder.mail.parse import ParsedMail
from salesfinder.mail.smtp import MailboxCredentials
from salesfinder.replies import ingest as ingest_module
from salesfinder.replies.ingest import (
    DIFFERENT_ADDRESS,
    POSSIBLE_BOUNCE,
    TAKEOVER,
    UNREADABLE,
    PollOutcome,
    poll_mailbox,
    poll_mailboxes,
)
from salesfinder.sending import state
from salesfinder.settings import Settings
from sending_helpers import (
    BOX,
    RECIPIENT,
    SEND_NOW,
    SENT_FOLDER,
    SUPPLIER_SECRET,
    Ready,
    ready_mailbox,
    seed_ready,
    sending_settings,
)

FIRST_ID = "<first@send-domain.com>"
POLL_NOW = SEND_NOW + timedelta(days=2)
BODY_MARKER = "ZEBRA-7731"


@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return sending_settings(test_settings)


async def _get[T](engine: AsyncEngine, model: type[T], key: Any) -> T:
    async with make_session_factory(engine)() as s:
        row = await s.get(model, key)
        assert row is not None
        return row


async def _rows[T](engine: AsyncEngine, model: type[T]) -> list[T]:
    async with make_session_factory(engine)() as s:
        return list((await s.scalars(select(model))).all())


async def _set(engine: AsyncEngine, model: Any, key: Any, **values: Any) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(update(model).where(model.id == key).values(**values))
        await s.commit()


async def _sent_first(db: AsyncSession, engine: AsyncEngine) -> Ready:
    """Step 1 went out from the mailbox a day ago (Message-ID ``FIRST_ID``); the follow-up is
    pending and the sequence active."""
    ready = await seed_ready(db)
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
    await _set(engine, Message, ready.step2_id, due_at=SEND_NOW + timedelta(days=6))
    await _set(
        engine,
        Sequence,
        ready.sequence_id,
        mailbox_id=ready.mailbox_id,
        created_at=SEND_NOW - timedelta(days=2),
    )
    return ready


def _reader(inbox: list[bytes] = (), sent: list[bytes] = (), **kw: Any) -> FakeReader:  # type: ignore[assignment]
    return FakeReader(
        {
            "INBOX": FakeFolder(1, [(i + 1, raw) for i, raw in enumerate(inbox)]),
            SENT_FOLDER: FakeFolder(1, [(i + 1, raw) for i, raw in enumerate(sent)]),
        },
        **kw,
    )


async def _poll(
    engine: AsyncEngine,
    settings: Settings,
    ready: Ready,
    reader: FakeReader,
    *,
    at: datetime = POLL_NOW,
    config: AppConfig = CONFIG,
) -> PollOutcome:
    return await poll_mailbox(
        make_session_factory(engine),
        ready.mailbox_id,
        mail=fake_mail(FakeSender(), reader),
        settings=settings,
        config=config,
        now=lambda: at,
    )


async def _conversation(engine: AsyncEngine, sequence_id: uuid.UUID) -> Conversation | None:
    async with make_session_factory(engine)() as s:
        return await s.scalar(select(Conversation).where(Conversation.sequence_id == sequence_id))


async def _actions(engine: AsyncEngine) -> list[str]:
    return [a.action for a in await _rows(engine, AuditLog)]


async def _needs_you_audits(engine: AsyncEngine) -> list[tuple[str | None, str]]:
    """The ``conversation.needs_you`` rows: (actor, reason)."""
    return [
        (str(a.actor_id) if a.actor_id else None, a.details["reason"])
        for a in await _rows(engine, AuditLog)
        if a.action == audit.CONVERSATION_NEEDS_YOU
    ]


async def _reply_draft(engine: AsyncEngine, ready: Ready, *, status: str) -> uuid.UUID:
    async with make_session_factory(engine)() as s:
        draft = Message(
            sequence_id=ready.sequence_id,
            step=3,
            kind="reply",
            subject="Re: Sourcing glycerin",
            body="Happy to send a sample.",
            status=status,
        )
        s.add(draft)
        await s.commit()
        return draft.id


# --------------------------------------------------------------------------- replies


async def test_reply_by_in_reply_to(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready = await _sent_first(db, engine)
    raw = reply_to(
        FIRST_ID,
        body=f"Yes, interested in 20 tonnes. {BODY_MARKER}",
        subject=f"Re: Glycerin {BODY_MARKER}-SUBJ",
        date=SEND_NOW + timedelta(days=1),
    )

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(replies=1)
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("replied", "Replied")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.mailbox_id, conv.last_inbound_at) == (
        "open",
        ready.mailbox_id,
        SEND_NOW + timedelta(days=1),
    )
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by, row.folder, row.uidvalidity, row.uid) == (
        "reply",
        "header",
        "inbox",
        1,
        1,
    )
    assert (row.sequence_id, row.message_id, row.conversation_id) == (
        ready.sequence_id,
        ready.message_id,
        conv.id,
    )
    assert row.body_text is not None and BODY_MARKER in row.body_text
    assert (row.message_id_header, row.in_reply_to) == ("<reply-0001@mail.acme-soap.com>", FIRST_ID)
    assert (row.from_address, row.to_addresses) == (RECIPIENT, [BOX])
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.uidvalidity, folder.last_uid, folder.polled_at, folder.last_error) == (
        1,
        1,
        POLL_NOW,
        None,
    )
    assert (await _get(engine, MailboxFolder, (ready.mailbox_id, "sent"))).name == SENT_FOLDER
    # Untrusted text never reaches the logs (the poll itself is logged, by counts).
    logged = capsys.readouterr()
    assert "mail_polled" in logged.out
    assert BODY_MARKER not in logged.out + logged.err
    assert "Jane Doe" not in logged.out + logged.err


async def test_same_mail_twice_is_one_row_and_a_uidvalidity_change_doesnt_duplicate(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))
    reader = _reader([raw])

    assert (await _poll(engine, settings, ready, reader)).replies == 1
    assert (await _poll(engine, settings, ready, reader)).replies == 0
    # The cursor moved: the second poll asked only for mail after UID 1. Even a re-read of
    # the same UID (cursor rewound) stores nothing twice.
    await _rewind(engine, ready.mailbox_id)
    assert (await _poll(engine, settings, ready, reader)) == PollOutcome()
    assert len(await _rows(engine, MailboxMessage)) == 1

    # The server renumbered the folder: the same mail comes back under a new UIDVALIDITY.
    reader.folders["INBOX"] = FakeFolder(7, [(40, raw)])
    assert (await _poll(engine, settings, ready, reader)) == PollOutcome()
    assert len(await _rows(engine, MailboxMessage)) == 1
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.uidvalidity, folder.last_uid) == (7, 40)


async def _rewind(engine: AsyncEngine, mailbox_id: uuid.UUID) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(
            update(MailboxFolder).where(MailboxFolder.mailbox_id == mailbox_id).values(last_uid=0)
        )
        await s.commit()


async def test_out_of_office_is_set_aside(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("ooo_auto_submitted.eml", ours=FIRST_ID)

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(auto_replies=1)
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    assert (await _get(engine, Message, ready.step2_id)).status == "pending"
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.body_text, row.sequence_id) == ("auto_reply", None, ready.sequence_id)
    assert await _conversation(engine, ready.sequence_id) is None


async def test_reply_from_another_address_needs_you(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = reply_to(
        FIRST_ID, sender="Pat Lee <pat.lee@acme-soap.com>", date=SEND_NOW + timedelta(days=1)
    )

    assert (await _poll(engine, settings, ready, _reader([raw]))).replies == 1

    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.needs_you_reason) == ("needs_you", DIFFERENT_ADDRESS)
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "replied"
    assert await _needs_you_audits(engine) == [(None, "different_address")]
    # A second one from that address: already flagged for it, so not audited again.
    again = reply_to(
        FIRST_ID,
        sender="Pat Lee <pat.lee@acme-soap.com>",
        date=SEND_NOW + timedelta(days=1, hours=1),
        mid="<second-pat@mail.acme-soap.com>",
    )
    assert (await _poll(engine, settings, ready, _reader([raw, again]))).replies == 1
    assert await _needs_you_audits(engine) == [(None, "different_address")]


@pytest.mark.parametrize(
    ("before", "after"), [("taken_over", "taken_over"), ("handled", "open"), ("open", "open")]
)
async def test_a_new_reply_to_an_existing_conversation(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, before: str, after: str
) -> None:
    ready = await _sent_first(db, engine)
    await _set(engine, Sequence, ready.sequence_id, status="replied")
    async with make_session_factory(engine)() as s:
        s.add(
            Conversation(
                sequence_id=ready.sequence_id,
                mailbox_id=ready.mailbox_id,
                status=before,
                handled_at=SEND_NOW if before == "handled" else None,
            )
        )
        await s.commit()
    raw = reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))

    assert (await _poll(engine, settings, ready, _reader([raw]))).replies == 1

    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None and conv.status == after
    assert conv.last_inbound_at == SEND_NOW + timedelta(days=1)
    if before == "handled":
        assert conv.handled_at is None
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "replied"


async def test_unmatched_mail_is_counted_not_stored(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    stranger = reply_to(
        None,
        sender="Sam <sam@unrelated.example>",
        subject="Newsletter",
        date=SEND_NOW + timedelta(days=1),
    )

    outcome = await _poll(engine, settings, ready, _reader([stranger]))

    assert outcome == PollOutcome(unmatched=1)
    assert await _rows(engine, MailboxMessage) == []
    assert (await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))).last_uid == 1


async def test_a_reply_leaves_a_follow_up_that_is_sending(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Review Focus 2: a follow-up already on its way is the sender's to settle."""
    ready = await _sent_first(db, engine)
    await _set(engine, Message, ready.step2_id, status="sending", claimed_at=POLL_NOW)
    raw = reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))

    assert (await _poll(engine, settings, ready, _reader([raw]))).replies == 1

    assert (await _get(engine, Message, ready.step2_id)).status == "sending"
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "replied"


# --------------------------------------------------------------------------- unsubscribes


async def test_gmail_unsubscribe_button_matched_by_address(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("gmail_unsubscribe_button.eml")  # subject "unsubscribe", no References

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(unsubscribes=1)
    [suppression] = await _rows(engine, Suppression)
    assert (suppression.address, suppression.reason) == (RECIPIENT, "unsubscribed")
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Unsubscribed")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.intent, conv.handled_at) == ("handled", "unsubscribe", POLL_NOW)
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by) == ("unsubscribe", "address")
    # The stored address is blocked after the locked transaction.
    [address] = await _rows(engine, EmailAddress)
    assert address.blocked_reason is not None
    actions = await _actions(engine)
    assert audit.SUPPRESSION_ADDED in actions and audit.CONVERSATION_HANDLED in actions
    assert audit.SEQUENCE_STOPPED in actions


async def test_unsubscribe_only_in_the_quoted_footer_is_a_reply(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("reply_gmail_quoted.eml", ours=FIRST_ID)

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(replies=1)
    assert await _rows(engine, Suppression) == []
    [row] = await _rows(engine, MailboxMessage)
    assert row.body_text is not None and "unsubscribe" not in row.body_text


async def test_colleague_unsubscribe_in_thread_suppresses_both(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    await _set(engine, Sequence, ready.sequence_id, status="replied")
    draft = await _reply_draft(engine, ready, status="awaiting_approval")
    raw = reply_to(
        FIRST_ID,
        sender="Pat Lee <pat.lee@acme-soap.com>",
        body="Please remove me and Jane from your list.",
        date=SEND_NOW + timedelta(days=1),
    )

    assert (await _poll(engine, settings, ready, _reader([raw]))).unsubscribes == 1

    assert sorted(s.address for s in await _rows(engine, Suppression)) == [
        RECIPIENT,
        "pat.lee@acme-soap.com",
    ]
    assert (await _get(engine, Message, draft)).status == "cancelled"
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "stopped"


async def test_unsubscribe_from_a_junk_address_still_suppresses_the_recipient(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = reply_to(
        FIRST_ID,
        sender="Odd <odd..dots@acme-soap.com>",
        body="unsubscribe",
        date=SEND_NOW + timedelta(days=1),
    )

    assert (await _poll(engine, settings, ready, _reader([raw]))).unsubscribes == 1

    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "stopped"


# --------------------------------------------------------------------------- bounces


async def test_hard_dsn_bounces_and_suppresses(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = dsn_for(original=FIRST_ID)

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(bounces=1)
    message = await _get(engine, Message, ready.message_id)
    assert (message.status, message.bounced_at) == ("bounced", POLL_NOW)
    assert message.bounce_detail is not None and "5.1.1" in message.bounce_detail
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
    assert address.status == "invalid" and address.blocked_reason is not None
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by, row.message_id) == ("bounce", "header", ready.message_id)


async def test_hard_dsn_on_a_pattern_confirmed_address_drops_the_pattern(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    domain = "acme-soap.com"
    ready = await _sent_first(db, engine)
    async with make_session_factory(engine)() as s:
        jane = await s.scalar(select(EmailAddress).where(EmailAddress.address == RECIPIENT))
        assert jane is not None
        jane.status, jane.source = "pattern_confirmed", "pattern"
        acme = await s.get(Company, ready.company_id)
        assert acme is not None
        john = await add_person(s, acme, "John Smith", rank=2)
        sibling = await add_address(
            s, acme, john, f"john.smith@{domain}", status="pattern_confirmed"
        )
        ravi = await add_person(s, acme, "Ravi Kumar", rank=None, target=False)
        await add_address(s, acme, ravi, f"ravi.kumar@{domain}")  # published: the evidence
        s.add(
            EmailPattern(
                domain=domain,
                pattern="{first}.{last}",
                support=1,
                examples=[f"ravi.kumar@{domain}"],
                learned_at=SEND_NOW - timedelta(days=1),
            )
        )
        await s.commit()
        sibling_id, jane_id = sibling.id, jane.id

    assert (
        await _poll(engine, settings, ready, _reader([dsn_for(original=FIRST_ID)]))
    ).bounces == 1

    pattern = await _get(engine, EmailPattern, domain)
    assert pattern.bounced_at == POLL_NOW
    john_row = await _get(engine, EmailAddress, sibling_id)
    assert (john_row.status, john_row.source) == ("pattern_guess", "guess")
    assert (await _get(engine, EmailAddress, jane_id)).status == "invalid"
    # A later contacts check relearns the pattern from Ravi's address but never confirms it
    # again.
    checker = MxChecker(CONFIG.contacts, resolve=FakeResolver({(domain, "MX"): [(10, "mx.a.")]}))
    await verify_company(
        make_session_factory(engine),
        ready.company_id,
        checker=checker,
        verifier=None,
        config=CONFIG,
        now=POLL_NOW + timedelta(hours=1),
    )
    assert (await _get(engine, EmailAddress, sibling_id)).status == "pattern_guess"
    assert (await _get(engine, EmailAddress, jane_id)).status == "invalid"
    assert (await _get(engine, EmailPattern, domain)).bounced_at == POLL_NOW


async def test_plain_bounce_with_one_known_recipient_is_hard(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("plain_bounce_no_report.eml", ours=FIRST_ID)

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(bounces=1)

    assert (await _get(engine, Message, ready.message_id)).status == "bounced"
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]


async def test_plain_bounce_naming_two_of_our_recipients_needs_you(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    campaign = await _get(engine, Campaign, ready.campaign_id)
    other = await seed_draft(db, campaign, domain="other-soap.com", checked_at=SEND_NOW)
    await _set(
        engine,
        Message,
        other.id,
        status="sent",
        sent_at=SEND_NOW,
        mailbox_id=ready.mailbox_id,
        message_id_header="<other@send-domain.com>",
    )
    raw = fixture("plain_bounce_no_report.eml", ours=FIRST_ID).replace(
        b"This is the mail system", b"Also jane.doe@other-soap.com. This is the mail system"
    )

    outcome = await _poll(engine, settings, ready, _reader([raw]))

    assert outcome == PollOutcome(unclear_bounces=1)
    assert await _rows(engine, Suppression) == []
    assert (await _get(engine, Message, ready.message_id)).status == "sent"
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.needs_you_reason) == ("needs_you", POSSIBLE_BOUNCE)
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by, row.body_text) == ("unclear_bounce", "header", None)
    assert await _needs_you_audits(engine) == [(None, "possible_bounce")]


async def test_delayed_4xx_changes_nothing(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = dsn_for("dsn_delayed_4xx.eml", original=FIRST_ID)

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome()

    assert (await _get(engine, Message, ready.message_id)).status == "sent"
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    assert await _rows(engine, Suppression) == []
    assert await _conversation(engine, ready.sequence_id) is None
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.sequence_id) == ("soft_bounce", ready.sequence_id)


async def test_dsn_with_an_unknown_message_id_matches_by_recipient(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("dsn_unknown_msgid.eml")  # quotes <not-ours-9999@...>, bounced Jane

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(bounces=1)

    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by, row.message_id) == ("bounce", "address", ready.message_id)
    assert (await _get(engine, Message, ready.message_id)).status == "bounced"


async def test_third_hard_bounce_pauses_sending(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    campaign = await _get(engine, Campaign, ready.campaign_id)
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

    outcome = await _poll(engine, settings, ready, _reader([dsn_for(original=FIRST_ID)]))

    reason = "Paused automatically: 3 of the last 50 emails bounced (6%)."
    assert outcome == PollOutcome(bounces=1, paused=reason)
    async with make_session_factory(engine)() as s:
        now_state = await state.get_state(s)
    assert (now_state.paused, now_state.reason) == (True, reason)
    paused = [a for a in await _rows(engine, AuditLog) if a.action == audit.SENDING_AUTO_PAUSED]
    assert len(paused) == 1 and paused[0].actor_id is None
    assert paused[0].details["reason"] == reason


# --------------------------------------------------------------------------- Sent folder


async def test_our_own_send_in_sent_is_ignored(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    step1 = await _get(engine, Message, ready.message_id)
    same_id = sent_copy(message_id=FIRST_ID, subject=step1.subject, date=SEND_NOW)
    # Some servers rewrite the Message-ID: same recipient and subject, 3 minutes later.
    rewritten = sent_copy(
        message_id="<rewritten-1@mail.gmail.com>",
        subject=step1.subject,
        date=SEND_NOW + timedelta(minutes=3),
    )

    outcome = await _poll(engine, settings, ready, _reader(sent=[same_id, rewritten]))

    assert outcome == PollOutcome()
    assert await _rows(engine, MailboxMessage) == []
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    assert (await _get(engine, MailboxFolder, (ready.mailbox_id, "sent"))).last_uid == 2


async def test_a_teammates_reply_in_the_thread_takes_over(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = sent_copy(
        message_id="<human-1@mail.gmail.com>",
        subject="Re: Sourcing glycerin for your soap line",
        date=SEND_NOW + timedelta(days=1),
        in_reply_to=FIRST_ID,
    )

    assert (await _poll(engine, settings, ready, _reader(sent=[raw]))) == PollOutcome(takeovers=1)

    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", TAKEOVER)
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.taken_over_at) == ("taken_over", POLL_NOW)
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.folder, row.matched_by) == ("human_sent", "sent", "header")
    assert audit.CONVERSATION_TAKEN_OVER in await _actions(engine)


async def test_a_teammates_fresh_email_takes_over_by_address(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    await _set(
        engine,
        Message,
        ready.step2_id,
        status="approved",
        approved_by_id=ready.user_id,
        approved_at=POLL_NOW - timedelta(hours=1),
    )
    raw = fixture("human_sent_no_inreplyto.eml")

    assert (await _poll(engine, settings, ready, _reader(sent=[raw]))).takeovers == 1

    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "stopped"
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.matched_by) == ("human_sent", "address")
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None and conv.status == "taken_over"


async def test_takeover_of_a_replied_sequence_keeps_it_replied(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    inbound = reply_to(FIRST_ID, date=SEND_NOW + timedelta(hours=2))
    assert (await _poll(engine, settings, ready, _reader([inbound]))).replies == 1
    draft = await _reply_draft(engine, ready, status="approved")
    human = sent_copy(
        message_id="<human-2@mail.gmail.com>",
        subject="Re: Sourcing glycerin for your soap line",
        date=SEND_NOW + timedelta(hours=5),
        in_reply_to="<reply-0001@mail.acme-soap.com>",  # their reply, stored above
    )

    outcome = await _poll(engine, settings, ready, _reader([inbound], sent=[human]))

    assert outcome == PollOutcome(takeovers=1)
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("replied", TAKEOVER)
    assert (await _get(engine, Message, draft)).status == "cancelled"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None and conv.status == "taken_over"


# --------------------------------------------------------------------------- failures


def _per_mailbox(readers: dict[str, MailReader]) -> MailFactory:
    async def open_reader(creds: MailboxCredentials) -> MailReader:
        return readers[creds.address]

    return MailFactory(sender=FakeSender(), open_reader=open_reader)


async def test_a_failing_reader_is_recorded_and_other_mailboxes_still_polled(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    broken = await ready_mailbox(
        db, ready.user_id, address="a-sales@send-domain.com", secret=SUPPLIER_SECRET
    )
    await db.commit()
    good = _reader([reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))])
    bad = _reader(fail=RuntimeError("server said: secret text"))

    outcomes = await poll_mailboxes(
        make_session_factory(engine),
        mail=_per_mailbox({BOX: good, "a-sales@send-domain.com": bad}),
        settings=settings,
        config=CONFIG,
        now=lambda: POLL_NOW,
    )

    error = "Couldn't read the mailbox: RuntimeError"
    assert list(outcomes) == ["a-sales@send-domain.com", BOX]
    assert outcomes["a-sales@send-domain.com"].error == error
    assert outcomes[BOX] == PollOutcome(replies=1)
    for folder in ("inbox", "sent"):
        row = await _get(engine, MailboxFolder, (broken.id, folder))
        assert row.last_error == error
    assert bad.closed and good.closed


async def test_no_sent_folder_still_reads_the_inbox(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    reader = _reader([reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))], special={})

    outcome = await _poll(engine, settings, ready, reader)

    assert outcome == PollOutcome(replies=1, error="Couldn't find the mailbox's Sent folder.")
    sent = await _get(engine, MailboxFolder, (ready.mailbox_id, "sent"))
    assert sent.last_error == "Couldn't find the mailbox's Sent folder."


async def test_an_empty_first_read_keeps_searching_by_date(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)

    assert await _poll(engine, settings, ready, _reader()) == PollOutcome()

    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.uidvalidity, folder.last_uid, folder.polled_at) == (None, 0, POLL_NOW)
    assert folder.polled_at is not None and folder.polled_at.tzinfo is not None
    assert POLL_NOW.tzinfo == UTC


# --------------------------------------------------------------------------- fix round 1


NUL_FROM = (
    b"From: =?utf-8?b?eABAZXZpbC5leGFtcGxl?=\r\n"
    b"To: sales@send-domain.com\r\n"
    b"Subject: hello\r\n"
    b"Message-ID: <nul-1@evil.example>\r\n"
    b"Date: Wed, 11 Nov 2026 08:00:00 +0000\r\n\r\nhi\r\n"
)


async def test_a_nul_in_the_from_never_blocks_the_unsubscribe_behind_it(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-K (a): the review's probe. The NUL address is dropped at the boundary, so
    the mail is unmatched and the Gmail unsubscribe queued behind it is honoured at once."""
    ready = await _sent_first(db, engine)
    reader = _reader([NUL_FROM, fixture("gmail_unsubscribe_button.eml")])

    outcome = await _poll(engine, settings, ready, reader)

    assert outcome == PollOutcome(unmatched=1, unsubscribes=1)
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.last_uid, folder.last_error) == (2, None)


async def test_ingest_drops_control_characters_even_past_the_parser(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P6-K (a), defence in depth: values with a NUL never reach a query parameter."""
    ready = await _sent_first(db, engine)
    real = ingest_module.parse_mail

    def dirty(raw: bytes, *, max_body_chars: int) -> ParsedMail:
        parsed = real(raw, max_body_chars=max_body_chars)
        return dataclasses.replace(
            parsed,
            message_id="<x\x00y@evil.example>",
            in_reply_to=[FIRST_ID, "<bad\x00@x>"],
            to_addresses=[BOX, "a\x00@b.c"],
            subject="Re: hi\x00there",
            text=parsed.text + "\x00",
            new_text=parsed.new_text + "\x00",
        )

    monkeypatch.setattr(ingest_module, "parse_mail", dirty)

    outcome = await _poll(engine, settings, ready, _reader([reply_to(FIRST_ID, date=SEND_NOW)]))

    assert outcome == PollOutcome(replies=1)
    [row] = await _rows(engine, MailboxMessage)
    assert (row.message_id_header, row.in_reply_to, row.to_addresses) == (None, FIRST_ID, [BOX])
    assert row.subject == "Re: hi there" and "\x00" not in (row.body_text or "")


async def test_a_mail_that_keeps_failing_becomes_a_placeholder_on_the_third_poll(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P6-K (b): a mail that fails three polls in a row is stored as ``unreadable``
    (its opt-out still honoured, its conversation flagged) and the folder moves on, so the
    unsubscribe queued behind it is honoured within three polls."""
    ready = await _sent_first(db, engine)
    real = ingest_module._inbox

    async def flaky(db: AsyncSession, ctx: Any, uid: int, parsed: ParsedMail) -> Any:
        if "POISON" in parsed.subject:
            raise RuntimeError("cannot process")
        return await real(db, ctx, uid, parsed)

    monkeypatch.setattr(ingest_module, "_inbox", flaky)
    poison = reply_to(FIRST_ID, subject="Re: POISON please unsubscribe me", date=SEND_NOW)
    behind = reply_to(
        None,
        sender="Pat Lee <pat.lee@acme-soap.com>",
        subject="unsubscribe",
        body="",
        mid="<unsub-pat@acme-soap.com>",
        date=SEND_NOW,
    )
    reader = _reader([poison, behind])
    error = "Couldn't process a mail: RuntimeError"

    for attempt in (1, 2):
        outcome = await _poll(engine, settings, ready, reader)
        assert outcome == PollOutcome(error=error)
        folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
        assert (folder.failed_uid, folder.failures, folder.last_uid) == (1, attempt, 0)
        assert folder.last_error == error
        assert await _rows(engine, Suppression) == []

    outcome = await _poll(engine, settings, ready, reader)

    assert outcome == PollOutcome(unsubscribes=2, error=error)
    assert sorted(s.address for s in await _rows(engine, Suppression)) == [
        RECIPIENT,
        "pat.lee@acme-soap.com",
    ]
    rows = sorted(await _rows(engine, MailboxMessage), key=lambda r: r.uid)
    assert [(r.uid, r.kind) for r in rows] == [(1, "unreadable"), (2, "unsubscribe")]
    assert (rows[0].sequence_id, rows[0].body_text) == (ready.sequence_id, None)
    assert rows[1].sequence_id is None
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.needs_you_reason) == ("needs_you", UNREADABLE)
    assert await _needs_you_audits(engine) == [(None, "unreadable")]
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.failed_uid, folder.failures, folder.last_uid) == (None, 0, 2)


async def test_a_sent_mail_dated_year_9999_is_read(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-K (a): an out-of-range Date counts as missing (no OverflowError)."""
    ready = await _sent_first(db, engine)
    raw = fixture("human_sent_no_inreplyto.eml").replace(
        b"Date: Thu, 12 Nov 2026 09:00:00 +0000", b"Date: Fri, 31 Dec 9999 23:59:59 -1200"
    )
    assert b"9999" in raw

    outcome = await _poll(engine, settings, ready, _reader(sent=[raw]))

    assert outcome == PollOutcome(takeovers=1)
    [row] = await _rows(engine, MailboxMessage)
    assert row.received_at == POLL_NOW


async def test_an_unsubscribe_to_a_disabled_mailbox_is_honoured(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-J: every mailbox with a password is read; ``enabled`` gates sending only."""
    ready = await _sent_first(db, engine)
    await _set(engine, Mailbox, ready.mailbox_id, enabled=False)
    reader = _reader([fixture("gmail_unsubscribe_button.eml")])

    outcomes = await poll_mailboxes(
        make_session_factory(engine),
        mail=fake_mail(FakeSender(), reader),
        settings=settings,
        config=CONFIG,
        now=lambda: POLL_NOW,
    )

    assert outcomes == {BOX: PollOutcome(unsubscribes=1)}
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    # A mailbox without a password isn't read at all.
    no_password = settings.model_copy(update={"mailbox_passwords": {}})
    assert (
        await poll_mailboxes(
            make_session_factory(engine),
            mail=fake_mail(FakeSender(), reader),
            settings=no_password,
            config=CONFIG,
            now=lambda: POLL_NOW,
        )
        == {}
    )


async def test_an_unmatched_unsubscribe_suppresses_its_sender(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-L: we mailed info@, Jane pressed the unsubscribe button."""
    ready = await _sent_first(db, engine)
    await _set(engine, Sequence, ready.sequence_id, recipient="info@acme-soap.com")

    outcome = await _poll(
        engine, settings, ready, _reader([fixture("gmail_unsubscribe_button.eml")])
    )

    assert outcome == PollOutcome(unsubscribes=1)
    [suppression] = await _rows(engine, Suppression)
    assert (suppression.address, suppression.reason) == (RECIPIENT, "unsubscribed")
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.sequence_id, row.matched_by) == ("unsubscribe", None, None)
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    # Its stored address is blocked after the commit.
    [address] = await _rows(engine, EmailAddress)
    assert address.blocked_reason is not None


async def test_an_auto_flagged_unsubscribe_is_honoured(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Ruling P6-M: the opt-out is checked before the auto-reply test."""
    ready = await _sent_first(db, engine)
    raw = reply_to(
        FIRST_ID,
        subject="unsubscribe",
        body="",
        date=SEND_NOW,
        headers={"Auto-Submitted": "auto-generated"},
    )

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(unsubscribes=1)

    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "stopped"


async def test_a_uidvalidity_change_never_duplicates_across_batches(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    one_at_a_time = CONFIG.model_copy(
        update={"mail": CONFIG.mail.model_copy(update={"max_messages_per_poll": 1})}
    )
    first = reply_to(FIRST_ID, date=SEND_NOW, mid="<r-1@acme-soap.com>")
    second = reply_to(FIRST_ID, date=SEND_NOW, mid="<r-2@acme-soap.com>", body="And samples?")
    reader = _reader([first, second])
    assert (await _poll(engine, settings, ready, reader, config=one_at_a_time)).replies == 2

    reader.folders["INBOX"] = FakeFolder(9, [(50, first), (51, second)])
    assert await _poll(engine, settings, ready, reader, config=one_at_a_time) == PollOutcome()

    assert len(await _rows(engine, MailboxMessage)) == 2
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.uidvalidity, folder.last_uid) == (9, 51)


async def test_address_matches_respect_dates(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """A mail written well before our first email isn't an answer to it, and a teammate's
    mail written before the sequence existed isn't a takeover. Missing dates are allowed.
    Ruling P6-T: an address-only INBOX match allows a Date up to 7 days before our first
    email (a sender's clock can be that wrong); header matches don't look at dates."""
    ready = await _sent_first(db, engine)
    old_inbound = reply_to(
        None, subject="Catalogue request", date=SEND_NOW - timedelta(days=7, hours=1)
    )
    old_sent = sent_copy(
        message_id="<old-human@mail.gmail.com>",
        subject="Our catalogue",
        date=SEND_NOW - timedelta(days=5),
    )

    outcome = await _poll(engine, settings, ready, _reader([old_inbound], sent=[old_sent]))

    assert outcome == PollOutcome(unmatched=2)
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    # Up to 7 days before the first email still counts (clock skew, time zones).
    recent = reply_to(
        None, subject="Catalogue", date=SEND_NOW - timedelta(days=6, hours=23), mid="<recent@a.com>"
    )
    reader = _reader([old_inbound, recent], sent=[old_sent])
    assert (await _poll(engine, settings, ready, reader)).replies == 1


async def test_an_automatic_message_in_sent_is_not_a_takeover(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    raw = fixture("human_sent_no_inreplyto.eml").replace(
        b"MIME-Version: 1.0", b"Auto-Submitted: auto-replied\nMIME-Version: 1.0"
    )

    assert await _poll(engine, settings, ready, _reader(sent=[raw])) == PollOutcome()

    assert (await _get(engine, Sequence, ready.sequence_id)).status == "active"
    assert await _rows(engine, MailboxMessage) == []


# --------------------------------------------------------------------------- fix round 2


def _utf8_unsubscribe(subject: str = "Re: Sourcing glycerin") -> bytes:
    """An opt-out whose Cc and References carry raw UTF-8 (RFC 6532, not RFC 2047)."""
    return (
        "From: Jane Doe <jane.doe@acme-soap.com>\r\n"
        "To: sales@send-domain.com\r\n"
        "Cc: béx@acme-soap.com, jane@münchen.de\r\n"
        f"Subject: {subject}\r\n"
        "Message-ID: <utf8-1@acme-soap.com>\r\n"
        f"In-Reply-To: {FIRST_ID}\r\n"
        f"References: <é@y.example> {FIRST_ID}\r\n"
        "Date: Wed, 11 Nov 2026 08:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\nPlease unsubscribe me.\r\n"
    ).encode()


async def test_an_unsubscribe_with_a_raw_utf8_cc_is_honoured_on_the_first_poll(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """C1: raw 8-bit header bytes used to come back as lone surrogates and fail the JSONB
    insert on every poll; they are decoded at the parse boundary now."""
    ready = await _sent_first(db, engine)

    outcome = await _poll(engine, settings, ready, _reader([_utf8_unsubscribe()]))

    assert outcome == PollOutcome(unsubscribes=1)
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Unsubscribed")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    [row] = await _rows(engine, MailboxMessage)
    assert row.to_addresses == [BOX, "béx@acme-soap.com", "jane@münchen.de"]
    # A non-ASCII id is dropped (final-wave M5: we could never send it back in References).
    assert row.references_header == FIRST_ID


def _always_fail_inbox(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    real = ingest_module._inbox

    async def flaky(db: AsyncSession, ctx: Any, uid: int, parsed: ParsedMail) -> Any:
        if marker in parsed.subject:
            raise RuntimeError("cannot process")
        return await real(db, ctx, uid, parsed)

    monkeypatch.setattr(ingest_module, "_inbox", flaky)


async def _three_polls(
    engine: AsyncEngine, settings: Settings, ready: Ready, reader: FakeReader
) -> PollOutcome:
    for _ in range(2):
        assert (await _poll(engine, settings, ready, reader)).error is not None
        assert await _rows(engine, Suppression) == []
    return await _poll(engine, settings, ready, reader)


async def test_an_unreadable_opt_out_survives_a_failing_placeholder(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1 safety net: on the third failure the opt-out is honoured in its own savepoint (the
    sender and the matched recipient suppressed, the sequence stopped), and a placeholder that
    can't be stored doesn't undo it."""
    ready = await _sent_first(db, engine)
    _always_fail_inbox(monkeypatch, "POISON")
    # The placeholder insert fails too (a value the database refuses).
    monkeypatch.setattr(ingest_module, "_received_at", lambda parsed, now: "not a date")
    raw = _utf8_unsubscribe(subject="Re: POISON unsubscribe")

    outcome = await _three_polls(engine, settings, ready, _reader([raw]))

    assert outcome.unsubscribes == 1
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("stopped", "Unsubscribed")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    assert await _rows(engine, MailboxMessage) == []  # the placeholder couldn't be stored
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.needs_you_reason) == ("needs_you", UNREADABLE)
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.last_uid, folder.failures) == (1, 0)


async def test_an_unreadable_opt_out_stores_a_minimal_placeholder(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = await _sent_first(db, engine)
    _always_fail_inbox(monkeypatch, "POISON")

    outcome = await _three_polls(
        engine, settings, ready, _reader([_utf8_unsubscribe(subject="Re: POISON unsubscribe")])
    )

    assert outcome.unsubscribes == 1
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    [row] = await _rows(engine, MailboxMessage)
    assert (row.kind, row.from_address, row.subject) == (
        "unreadable",
        RECIPIENT,
        "Re: POISON unsubscribe",
    )
    assert (row.to_addresses, row.in_reply_to, row.references_header, row.body_text) == (
        [],
        None,
        None,
        None,
    )
    assert (row.sequence_id, row.matched_by) == (ready.sequence_id, "header")
    assert audit.SEQUENCE_STOPPED in await _actions(engine)


async def test_an_unreadable_reply_stops_the_sequence_as_replied(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P6-O: we couldn't read it, so assume they answered: no follow-up goes."""
    ready = await _sent_first(db, engine)
    _always_fail_inbox(monkeypatch, "POISON")
    raw = reply_to(FIRST_ID, subject="Re: POISON", body="Sounds good.", date=SEND_NOW)

    outcome = await _three_polls(engine, settings, ready, _reader([raw]))

    assert (outcome.replies, outcome.unsubscribes) == (0, 0)
    assert await _rows(engine, Suppression) == []
    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("replied", "Replied")
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    conv = await _conversation(engine, ready.sequence_id)
    assert conv is not None
    assert (conv.status, conv.needs_you_reason) == ("needs_you", UNREADABLE)


async def test_when_even_the_placeholder_path_fails_the_skip_is_visible(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last resort still honours the opt-out from the sender, moves the cursor, and
    leaves "Skipped a mail we couldn't read" on the folder, even after later clean polls."""
    ready = await _sent_first(db, engine)
    _always_fail_inbox(monkeypatch, "POISON")

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("placeholder path broken")

    monkeypatch.setattr(ingest_module, "_unreadable", broken)
    reader = _reader([reply_to(None, subject="POISON unsubscribe", body="", date=SEND_NOW)])

    outcome = await _three_polls(engine, settings, ready, reader)

    assert outcome.unsubscribes == 1
    assert [s.address for s in await _rows(engine, Suppression)] == [RECIPIENT]
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.last_uid, folder.last_error) == (1, "Skipped a mail we couldn't read")
    reader.folders["INBOX"].add(reply_to(FIRST_ID, date=SEND_NOW, mid="<later@acme-soap.com>"))
    assert (await _poll(engine, settings, ready, reader)) == PollOutcome(replies=1)
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.last_uid, folder.last_error) == (2, "Skipped a mail we couldn't read")


async def test_a_read_error_clears_after_a_clean_poll(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_first(db, engine)
    assert (await _poll(engine, settings, ready, _reader(fail=TimeoutError()))).error
    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert folder.last_error == "Couldn't read the mailbox: TimeoutError"

    assert await _poll(engine, settings, ready, _reader()) == PollOutcome()

    for name in ("inbox", "sent"):
        assert (await _get(engine, MailboxFolder, (ready.mailbox_id, name))).last_error is None


# --------------------------------------------------------------- final fix wave (M2)


async def _sent_back_to_approval(
    db: AsyncSession, engine: AsyncEngine, *, unconfirmed_once: bool
) -> Ready:
    """Step 1 went out (or may have: it was ``unconfirmed`` once, ``unconfirmed_once``), was
    then sent again and refused by the gate at send time: ``gate_failed`` keeping its
    Message-ID, the sequence back to ``awaiting_approval``."""
    ready = await _sent_first(db, engine)
    await _set(engine, Message, ready.message_id, status="gate_failed", sent_at=None)
    await _set(engine, Sequence, ready.sequence_id, status="awaiting_approval")
    if unconfirmed_once:
        async with make_session_factory(engine)() as s:
            await audit.record(
                s,
                action=audit.MESSAGE_UNCONFIRMED,
                actor_id=None,
                target_type="message",
                target_id=str(ready.message_id),
                details={},
            )
            await s.commit()
    return ready


async def test_a_reply_stops_a_sequence_sent_back_to_approval_after_it_may_have_gone(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    ready = await _sent_back_to_approval(db, engine, unconfirmed_once=True)
    raw = reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(replies=1)

    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("replied", "Replied")
    assert (await _get(engine, Message, ready.message_id)).status == "cancelled"
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"
    assert audit.SEQUENCE_STOPPED in await _actions(engine)


async def test_a_sequence_back_in_approval_that_never_reached_them_is_left_alone(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Its Message-ID is kept (a retry before DATA), but nothing may have gone: the sequence
    stays in the Outbox and the reply is still stored."""
    ready = await _sent_back_to_approval(db, engine, unconfirmed_once=False)
    raw = reply_to(FIRST_ID, date=SEND_NOW + timedelta(days=1))

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(replies=1)

    assert (await _get(engine, Sequence, ready.sequence_id)).status == "awaiting_approval"
    assert (await _get(engine, Message, ready.message_id)).status == "gate_failed"
    assert audit.SEQUENCE_STOPPED not in await _actions(engine)


async def test_an_unreadable_reply_stops_a_sequence_sent_back_to_approval(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = await _sent_back_to_approval(db, engine, unconfirmed_once=True)
    _always_fail_inbox(monkeypatch, "POISON")
    raw = reply_to(FIRST_ID, subject="Re: POISON", body="Sounds good.", date=SEND_NOW)

    await _three_polls(engine, settings, ready, _reader([raw]))

    sequence = await _get(engine, Sequence, ready.sequence_id)
    assert (sequence.status, sequence.stop_reason) == ("replied", "Replied")
    assert (await _get(engine, Message, ready.message_id)).status == "cancelled"
    assert (await _get(engine, Message, ready.step2_id)).status == "cancelled"


async def test_our_footer_left_unquoted_in_a_reply_is_not_an_unsubscribe(
    engine: AsyncEngine, db: AsyncSession, settings: Settings
) -> None:
    """Final-wave M3: the quoted copy of our email wasn't recognised as quoted, so our footer's
    opt-out line ("just reply \"unsubscribe\"") is in their new text: still a reply."""
    ready = await _sent_first(db, engine)
    body = (
        "Thanks, we are interested. Can you send a quote?\n\n"
        f"Elchemy · 1 Main St\n{CONFIG.writing.opt_out_line}"
    )
    raw = reply_to(FIRST_ID, body=body, date=SEND_NOW + timedelta(days=1))

    assert await _poll(engine, settings, ready, _reader([raw])) == PollOutcome(replies=1)
    assert await _rows(engine, Suppression) == []
    assert (await _get(engine, Sequence, ready.sequence_id)).status == "replied"


@pytest.mark.parametrize("hold", ["SETTINGS_CHANGED", "NOT_TESTED"])
async def test_a_mailbox_with_untested_settings_isnt_polled_until_tested(
    engine: AsyncEngine, db: AsyncSession, settings: Settings, hold: str
) -> None:
    """Final-wave M4 / Rulings P6-V and P6-W: changed (or never tested) connection settings
    get no login (the password never goes to an untested host) until a successful Test
    connection clears the hold."""
    from salesfinder.sending import mailboxes

    ready = await _sent_first(db, engine)
    await _set(
        engine, Mailbox, ready.mailbox_id, enabled=False, hold_reason=getattr(mailboxes, hold)
    )
    opened: list[str] = []
    reader = _reader([fixture("gmail_unsubscribe_button.eml")])

    async def open_reader(creds: MailboxCredentials) -> MailReader:
        opened.append(creds.imap_host)
        return reader

    factory = MailFactory(sender=FakeSender(), open_reader=open_reader)

    async def poll() -> dict[str, PollOutcome]:
        return await poll_mailboxes(
            make_session_factory(engine),
            mail=factory,
            settings=settings,
            config=CONFIG,
            now=lambda: POLL_NOW,
        )

    assert await poll() == {BOX: PollOutcome(untested=True)}  # said, not read
    assert opened == []
    assert await _rows(engine, Suppression) == []

    async with make_session_factory(engine)() as s:
        box = await mailboxes.get_mailbox(s, ready.mailbox_id)
        assert await mailboxes.clear_settings_hold(
            s, ready.mailbox_id, mailboxes.connection_snapshot(box)
        )
        await s.commit()
    assert await poll() == {BOX: PollOutcome(unsubscribes=1)}
    assert len(opened) == 1


# --------------------------------------------------------------- final fix wave (T8)


async def _folder_error(engine: AsyncEngine, ready: Ready) -> str | None:
    return (await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))).last_error


async def test_skipped_survives_a_reader_error_and_a_mail_failure_then_a_clean_poll(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SKIPPED`` (a mail we never stored) stays until a teammate dismisses it: later errors
    don't overwrite it and a clean poll doesn't clear it."""
    from salesfinder.replies.ingest import SKIPPED

    ready = await _sent_first(db, engine)
    assert await _poll(engine, settings, ready, _reader()) == PollOutcome()  # folders exist
    await _set_folder_error(engine, ready, SKIPPED)

    assert (await _poll(engine, settings, ready, _reader(fail=TimeoutError()))).error
    assert await _folder_error(engine, ready) == SKIPPED

    _always_fail_inbox(monkeypatch, "POISON")
    reader = _reader([reply_to(FIRST_ID, subject="Re: POISON", date=SEND_NOW)])
    assert (await _poll(engine, settings, ready, reader)).error is not None  # failure 1 of 3
    assert await _folder_error(engine, ready) == SKIPPED

    assert await _poll(engine, settings, ready, _reader()) == PollOutcome()
    assert await _folder_error(engine, ready) == SKIPPED


async def _set_folder_error(engine: AsyncEngine, ready: Ready, error: str) -> None:
    async with make_session_factory(engine)() as s:
        await s.execute(
            update(MailboxFolder)
            .where(MailboxFolder.mailbox_id == ready.mailbox_id, MailboxFolder.folder == "inbox")
            .values(last_error=error)
        )
        await s.commit()


async def test_an_unmatched_mail_whose_placeholder_fails_leaves_skipped(
    engine: AsyncEngine,
    db: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No match, no opt-out, and the placeholder can't be stored: the folder still says a mail
    was skipped (otherwise nothing would show it was ever there)."""
    from salesfinder.replies.ingest import SKIPPED

    ready = await _sent_first(db, engine)
    _always_fail_inbox(monkeypatch, "POISON")
    monkeypatch.setattr(ingest_module, "_received_at", lambda parsed, now: "not a date")
    stranger = reply_to(
        None, sender="Sam <sam@elsewhere.example>", subject="POISON hello", date=SEND_NOW
    )

    await _three_polls(engine, settings, ready, _reader([stranger]))

    folder = await _get(engine, MailboxFolder, (ready.mailbox_id, "inbox"))
    assert (folder.last_uid, folder.last_error) == (1, SKIPPED)
    assert await _rows(engine, MailboxMessage) == []
