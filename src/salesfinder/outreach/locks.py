"""One lock order for everything that changes a drafted sequence (Outbox, sender, follow-ups,
reply ingest, exclusion sweep).

Lock order (follow-up 10): all of a sequence's messages by id -> the sequence ->
``pg_advisory_xact_lock(RECIPIENT_LOCK_NS, hashtext(recipient))`` (``lock_recipient``) -> the
mailbox row. The
campaign is never locked after a message: it is read without a lock (``populate_existing``).
``campaigns.service.archive`` locks campaign (``FOR NO KEY UPDATE``) -> messages by id ->
sequences by id, which agrees with this order, so the two can't cross."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Campaign, Company, EmailAddress, Message, Person, Sequence

__all__ = ["RECIPIENT_LOCK_NS", "Locked", "lock_message", "lock_recipient", "recipient_lock"]

# The first key of the two-key recipient advisory lock: a namespace of its own, so a recipient's
# ``hashtext`` can never collide with the single-key (bigint) locks (approvals, scoring, the
# worker). Every taker of the recipient lock must use ``recipient_lock`` (the same form).
RECIPIENT_LOCK_NS = 0x53465243  # "SFRC"; fits a signed int4


@dataclass
class Locked:
    """A message and its sequence, row-locked and fresh from the database, with context.

    ``siblings`` are all of the sequence's messages in id order, ``message`` among them (every
    one is locked). ``campaign`` is read without a lock; ``company``, ``person`` and
    ``address`` are plain reads."""

    message: Message
    siblings: list[Message]
    sequence: Sequence
    campaign: Campaign
    company: Company
    person: Person | None
    address: EmailAddress | None


async def lock_message(db: AsyncSession, message_id: uuid.UUID) -> Locked | None:
    """Lock every message of ``message_id``'s sequence in id order (``FOR UPDATE``), then the
    sequence, each refreshed from the database (``populate_existing``); then read the campaign
    (unlocked, refreshed), the company, person and address. None if the message (or, through
    cascades that make it unreachable, its sequence, campaign or company) is gone.

    Locking every sibling up front means a caller that also changes the follow-up or a reply
    draft never waits for a row while holding another of the same sequence."""
    sequence_id = await db.scalar(select(Message.sequence_id).where(Message.id == message_id))
    if sequence_id is None:
        return None
    siblings = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.sequence_id == sequence_id)
                .order_by(Message.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    message = next((m for m in siblings if m.id == message_id), None)
    if message is None:  # deleted meanwhile
        return None
    sequence = (
        await db.execute(
            select(Sequence)
            .where(Sequence.id == sequence_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if sequence is None:  # cascades make this unreachable
        return None
    campaign = await db.get(Campaign, sequence.campaign_id, populate_existing=True)
    company = await db.get(Company, sequence.company_id)
    if campaign is None or company is None:  # cascades make this unreachable
        return None
    person = await db.get(Person, sequence.person_id) if sequence.person_id else None
    address = (
        await db.get(EmailAddress, sequence.email_address_id) if sequence.email_address_id else None
    )
    return Locked(message, siblings, sequence, campaign, company, person, address)


def recipient_lock(recipient: str) -> Select[tuple[Any]]:
    """``SELECT pg_advisory_xact_lock(RECIPIENT_LOCK_NS, hashtext(recipient))``."""
    return select(func.pg_advisory_xact_lock(RECIPIENT_LOCK_NS, func.hashtext(recipient)))


async def lock_recipient(db: AsyncSession, recipient: str) -> None:
    """The per-address transaction lock (after the message and sequence locks): drafting,
    approval and sending to one address see each other."""
    await db.execute(recipient_lock(recipient))
