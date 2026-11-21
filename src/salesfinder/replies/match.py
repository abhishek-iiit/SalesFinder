"""Which of our sequences a mail read from a mailbox belongs to.

Headers first (``In-Reply-To``/``References`` against the Message-IDs we sent, then against
mail we already stored for a sequence), then addresses: mail that doesn't thread (Gmail's
unsubscribe button, a teammate writing a fresh email) still finds its sequence (Review
Focus 5). Message ids keep their angle brackets everywhere. Reads only; no locks."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Campaign, MailboxMessage, Message, Sequence
from salesfinder.mail.parse import ParsedMail

__all__ = [
    "OURS_WINDOW",
    "SENT_STATUSES",
    "Match",
    "is_ours",
    "match_by_headers",
    "match_inbound",
    "match_sent",
    "sent_to",
    "thread_ids",
]

# Our outbound email that may have reached its recipient.
SENT_STATUSES = ("sending", "sent", "unconfirmed", "bounced")
# Sequences a teammate's mail from the mailbox takes over.
TAKEOVER_STATUSES = ("active", "awaiting_approval", "replied", "completed")
# A copy of our own send in the Sent folder, rewritten by the server: same recipient and
# subject, sent within this long of its Date.
OURS_WINDOW = timedelta(minutes=15)
# An address-only INBOX match ignores mail dated more than this before our first email
# (Ruling P6-T: a sender's clock may be days off; header matches never look at dates).
ADDRESS_SLACK = timedelta(days=7)


@dataclass(frozen=True)
class Match:
    sequence_id: uuid.UUID
    # Our outbound email the mail refers to (None when matched through stored mail or by
    # address with no email of ours to point at).
    message_id: uuid.UUID | None
    by: Literal["header", "address"]


def thread_ids(parsed: ParsedMail) -> list[str]:
    """In-Reply-To first, then References newest first, without repeats."""
    ordered: list[str] = []
    for mid in [*parsed.in_reply_to, *reversed(parsed.references)]:
        if mid not in ordered:
            ordered.append(mid)
    return ordered


async def match_by_headers(db: AsyncSession, parsed: ParsedMail) -> Match | None:
    """The ids in ``In-Reply-To`` and ``References`` (In-Reply-To first, then References newest
    first) looked up 1. among our ``messages.message_id_header``, then 2. among stored
    ``mailbox_messages.message_id_header`` that belong to a sequence."""
    ids = thread_ids(parsed)
    if not ids:
        return None
    ours = {
        r.message_id_header: r
        for r in (
            await db.execute(
                select(Message.id, Message.sequence_id, Message.message_id_header).where(
                    Message.message_id_header.in_(ids)
                )
            )
        ).all()
    }
    for mid in ids:
        if mid in ours:
            return Match(ours[mid].sequence_id, ours[mid].id, "header")
    stored = (
        await db.execute(
            select(
                MailboxMessage.message_id_header,
                MailboxMessage.sequence_id,
                MailboxMessage.message_id,
            )
            .where(
                MailboxMessage.message_id_header.in_(ids),
                MailboxMessage.sequence_id.is_not(None),
            )
            .order_by(MailboxMessage.created_at.desc(), MailboxMessage.id)
        )
    ).all()
    by_id: dict[str | None, Any] = {}
    for r in stored:
        by_id.setdefault(r.message_id_header, r)
    for mid in ids:
        if mid in by_id:
            row = by_id[mid]
            assert row.sequence_id is not None  # noqa: S101 - filtered above
            return Match(row.sequence_id, row.message_id, "header")
    return None


async def sent_to(
    db: AsyncSession, mailbox_id: uuid.UUID, recipient: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(sequence id, message id) of our latest email to ``recipient`` from this mailbox that
    may have reached them, or None."""
    row = (
        await db.execute(
            select(Message.sequence_id, Message.id)
            .join(Sequence, Sequence.id == Message.sequence_id)
            .where(
                Sequence.recipient == recipient,
                Message.mailbox_id == mailbox_id,
                Message.status.in_(SENT_STATUSES),
            )
            .order_by(
                func.coalesce(Message.sent_at, Message.claimed_at).desc().nulls_last(),
                Message.id.desc(),
            )
            .limit(1)
        )
    ).first()
    return (row.sequence_id, row.id) if row is not None else None


async def _addressed_to(
    db: AsyncSession, mailbox_id: uuid.UUID, sender: str, when: datetime | None
) -> Match | None:
    """Our newest sequence to ``sender`` from this mailbox whose first email went out no more
    than ``ADDRESS_SLACK`` after ``when`` (a mail written before we ever wrote to them isn't
    an answer), pointing at that sequence's latest email to them."""
    at = func.coalesce(Message.sent_at, Message.claimed_at)
    rows = (
        await db.execute(
            select(
                Message.sequence_id,
                func.min(at).label("first"),
                func.max(at).label("last"),
            )
            .join(Sequence, Sequence.id == Message.sequence_id)
            .where(
                Sequence.recipient == sender,
                Message.mailbox_id == mailbox_id,
                Message.status.in_(SENT_STATUSES),
            )
            .group_by(Message.sequence_id)
            .order_by(func.max(at).desc().nulls_last(), Message.sequence_id)
            .limit(20)
        )
    ).all()
    for row in rows:
        if when is not None and row.first is not None and when < row.first - ADDRESS_SLACK:
            continue
        message_id = await db.scalar(
            select(Message.id)
            .where(Message.sequence_id == row.sequence_id, Message.status.in_(SENT_STATUSES))
            .order_by(at.desc().nulls_last(), Message.id.desc())
            .limit(1)
        )
        return Match(row.sequence_id, message_id, "address")
    return None


async def match_inbound(
    db: AsyncSession, mailbox_id: uuid.UUID, parsed: ParsedMail
) -> Match | None:
    """An INBOX mail's sequence: by headers (``match_by_headers``), else its From address is
    the recipient of a sequence this mailbox has sent to, and the mail isn't dated more than
    ``ADDRESS_SLACK`` (7 days) before that sequence's first email (a missing Date is allowed)."""
    found = await match_by_headers(db, parsed)
    if found is not None:
        return found
    if not parsed.from_address:
        return None
    return await _addressed_to(db, mailbox_id, parsed.from_address, parsed.date)


def _norm_subject(subject: str) -> str:
    return " ".join(subject.split()).casefold()


async def is_ours(db: AsyncSession, mailbox_id: uuid.UUID, parsed: ParsedMail) -> bool:
    """A mail in the Sent folder that we sent: its Message-ID is one of ours, or an email of
    ours from this mailbox to one of its recipients, with the same subject, went out within
    ``OURS_WINDOW`` of its Date (the server rewrote the Message-ID)."""
    if parsed.message_id is not None:
        found = await db.scalar(
            select(Message.id).where(Message.message_id_header == parsed.message_id).limit(1)
        )
        if found is not None:
            return True
    recipients = [*parsed.to_addresses, *parsed.cc_addresses]
    if parsed.date is None or not recipients:
        return False
    try:
        start, end = parsed.date - OURS_WINDOW, parsed.date + OURS_WINDOW
    except OverflowError:  # parse_mail drops such dates; defensive
        return False
    when = func.coalesce(Message.sent_at, Message.claimed_at)
    rows = (
        await db.execute(
            select(Message.subject)
            .join(Sequence, Sequence.id == Message.sequence_id)
            .where(
                Message.mailbox_id == mailbox_id,
                Message.status.in_(SENT_STATUSES),
                Sequence.recipient.in_(recipients),
                when >= start,
                when <= end,
            )
        )
    ).all()
    subject = _norm_subject(parsed.subject)
    return any(_norm_subject(r.subject) == subject for r in rows)


async def _sequence_on_mailbox(
    db: AsyncSession, mailbox_id: uuid.UUID, recipients: list[str], when: datetime | None
) -> uuid.UUID | None:
    """The newest sequence on this mailbox (its own, else its campaign's) to one of
    ``recipients`` that automation is still working (``TAKEOVER_STATUSES``) and that existed
    when the mail was written (``when``; a missing Date is allowed)."""
    if not recipients:
        return None
    return await db.scalar(
        select(Sequence.id)
        .join(Campaign, Campaign.id == Sequence.campaign_id)
        .where(
            Sequence.created_at <= when if when is not None else true(),
            Sequence.recipient.in_(recipients),
            Sequence.status.in_(TAKEOVER_STATUSES),
            or_(
                Sequence.mailbox_id == mailbox_id,
                and_(Sequence.mailbox_id.is_(None), Campaign.mailbox_id == mailbox_id),
            ),
        )
        .order_by(Sequence.created_at.desc(), Sequence.id)
        .limit(1)
    )


async def match_sent(
    db: AsyncSession, mailbox_id: uuid.UUID, parsed: ParsedMail, *, check_ours: bool = True
) -> Match | None:
    """A Sent-folder mail written by a person (not by us), and the sequence it takes over:

    1. ours (``is_ours``; skipped with ``check_ours=False`` when the caller already asked)
       -> None;
    2. headers (``match_by_headers``);
    3. any To/Cc address is the recipient of a sequence on this mailbox in
       ``active/awaiting_approval/replied/completed`` created no later than the mail's Date."""
    if check_ours and await is_ours(db, mailbox_id, parsed):
        return None
    found = await match_by_headers(db, parsed)
    if found is not None:
        return found
    recipients = [*parsed.to_addresses, *parsed.cc_addresses]
    sequence_id = await _sequence_on_mailbox(db, mailbox_id, recipients, parsed.date)
    if sequence_id is None:
        return None
    return Match(sequence_id, None, "address")
