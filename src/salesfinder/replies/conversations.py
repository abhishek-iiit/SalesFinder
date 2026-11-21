"""Conversations on the dashboard: the list, one conversation's thread, and a teammate's
actions on it (assign to me, mark handled, take over; answering is ``draft.write_reply``).

Services flush only; the routes audit and commit.

Lock order, as everywhere (``outreach.locks``): an action that changes outreach rows (marking
handled cancels Claude's reply drafts; taking over stops the sequence) takes the sequence's
messages by id, then the sequence, then the recipient lock, and only then the conversation
``FOR UPDATE``. Assigning changes the conversation row only and locks only that row.

Their mail (subjects, bodies, From addresses) is untrusted: it is returned for escaped
rendering and never logged."""

import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from salesfinder.brain.budget import calls_today
from salesfinder.brain.runtime import SIGNED_OUT
from salesfinder.config import AppConfig
from salesfinder.db.models import (
    Campaign,
    Company,
    Conversation,
    MailboxMessage,
    Message,
    Person,
    Sequence,
    UsageEvent,
    User,
)
from salesfinder.outreach.locks import Locked, lock_message, lock_recipient
from salesfinder.outreach.stop import cancel_reply_drafts, stop_sequence

__all__ = [
    "IN_APP_TAKEOVER",
    "NOT_FOUND",
    "VIEWS",
    "Action",
    "ConversationRow",
    "ThreadEntry",
    "assign_to",
    "claude_waiting_reason",
    "count_conversations",
    "list_conversations",
    "mark_handled",
    "needs_you_count",
    "pending_answers",
    "take_over",
    "thread",
    "waiting_for_claude",
]

NOT_FOUND = "No such conversation."
IN_APP_TAKEOVER = "A teammate took the conversation over in SalesFinder"
View = Literal["needs_you", "mine", "open", "handled", "all"]
VIEWS: tuple[View, ...] = ("needs_you", "mine", "open", "handled", "all")
# What a teammate still has to look at: flagged or open, not closed by a person.
ACTIVE = ("open", "needs_you")
CLOSED = ("handled", "taken_over")
# Our emails that went out (or may have).
OUT = ("sending", "sent", "unconfirmed", "bounced")
# A reply draft not sent yet (Claude's or a teammate's).
PENDING_ANSWER = ("awaiting_approval", "gate_failed", "approved")
# Claude classification reasons the dashboard can name without running anything.
BUDGET_SPENT = "today's Claude budget is used up"
CLI_MISSING = "the Claude CLI isn't installed on this machine"
CLAUDE_SIGNED_OUT = "Claude is signed out or out of quota"


@dataclass(frozen=True)
class ConversationRow:
    conversation: Conversation
    sequence: Sequence
    company: Company
    person: Person | None
    campaign_name: str
    assigned_name: str | None


def _view_filter(view: View, user_id: uuid.UUID) -> list[ColumnElement[bool]]:
    if view == "needs_you":
        return [Conversation.status == "needs_you"]
    if view == "mine":
        return [Conversation.assigned_to_id == user_id, Conversation.status.in_(ACTIVE)]
    if view == "open":
        return [Conversation.status == "open"]
    if view == "handled":
        return [Conversation.status.in_(CLOSED)]
    return []


async def needs_you_count(db: AsyncSession) -> int:
    """How many conversations are flagged "needs you" (the nav's count)."""
    count = await db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.status == "needs_you")
    )
    return int(count or 0)


async def count_conversations(db: AsyncSession, *, view: View, user_id: uuid.UUID) -> int:
    """How many conversations ``list_conversations`` lists for ``view`` (the same filter)."""
    return int(
        await db.scalar(
            select(func.count()).select_from(Conversation).where(*_view_filter(view, user_id))
        )
        or 0
    )


async def list_conversations(
    db: AsyncSession, *, view: View, user_id: uuid.UUID, limit: int = 25, offset: int = 0
) -> tuple[list[ConversationRow], int]:
    """One view of the conversations, latest inbound mail first, and its total:
    ``needs_you`` (flagged), ``mine`` (assigned to ``user_id``, open or flagged), ``open``,
    ``handled`` (handled or taken over) or ``all``."""
    where = _view_filter(view, user_id)
    total = await count_conversations(db, view=view, user_id=user_id)
    assignee = aliased(User)
    rows = await db.execute(
        select(Conversation, Sequence, Company, Person, Campaign.name, assignee.name)
        .join(Sequence, Sequence.id == Conversation.sequence_id)
        .join(Company, Company.id == Sequence.company_id)
        .join(Campaign, Campaign.id == Sequence.campaign_id)
        .outerjoin(Person, Person.id == Sequence.person_id)
        .outerjoin(assignee, assignee.id == Conversation.assigned_to_id)
        .where(*where)
        .order_by(
            func.coalesce(Conversation.last_inbound_at, Conversation.created_at).desc(),
            Conversation.id,
        )
        .limit(limit)
        .offset(offset)
    )
    items = [
        ConversationRow(conv, seq, company, person, campaign_name, assigned)
        for conv, seq, company, person, campaign_name, assigned in rows.all()
    ]
    return items, total


# --------------------------------------------------------------------------- the thread

Who = Literal["us", "them", "teammate", "event"]
_INBOUND_LABELS = {
    "reply": "Their reply",
    "auto_reply": "Auto-reply (set aside)",
    "unsubscribe": "They asked to unsubscribe",
    "human_sent": "A teammate wrote from the mailbox",
    "bounce": "Bounced: their mail server refused the address",
    "soft_bounce": "Delivery delayed (a soft bounce)",
    "unclear_bounce": "Possible bounce we couldn't read",
    "unreadable": "A mail we couldn't read — check the mailbox",
}
_OUR_LABELS = {"first": "Our first email", "followup": "Our follow-up", "reply": "Our answer"}


@dataclass(frozen=True)
class ThreadEntry:
    """One item of a conversation's thread. ``subject``, ``body`` and ``sender`` may hold
    their (untrusted) text: render escaped."""

    at: datetime
    who: Who
    label: str
    subject: str | None = None
    body: str | None = None
    sender: str | None = None
    note: str | None = None
    message_id: uuid.UUID | None = None


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


async def thread(db: AsyncSession, conv: Conversation, sequence: Sequence) -> list[ThreadEntry]:
    """The conversation in time order: our emails that went out (subject and body), the mail
    read for it (their replies with their text, auto-replies, unsubscribes, a teammate's mail
    from the mailbox, bounces) and the takeover and handled events."""
    entries: list[tuple[datetime, int, ThreadEntry]] = []
    ours = (
        await db.scalars(
            select(Message).where(Message.sequence_id == sequence.id, Message.status.in_(OUT))
        )
    ).all()
    for m in ours:
        at = _aware(m.sent_at or m.claimed_at or m.created_at)
        note = None
        if m.status == "unconfirmed":
            note = "We couldn't confirm this went out."
        elif m.status == "sending":
            note = "Being sent."
        elif m.status == "bounced":
            note = "Bounced."
        entries.append(
            (
                at,
                0,
                ThreadEntry(
                    at=at,
                    who="us",
                    label=_OUR_LABELS.get(m.kind, "Our email"),
                    subject=m.subject,
                    body=m.body,
                    note=note,
                    message_id=m.id,
                ),
            )
        )
    inbound = (
        await db.scalars(
            select(MailboxMessage).where(
                (MailboxMessage.conversation_id == conv.id)
                | (MailboxMessage.sequence_id == sequence.id)
            )
        )
    ).all()
    human_sent = False
    for row in inbound:
        at = _aware(row.received_at)
        who: Who = "them"
        if row.kind == "human_sent":
            who, human_sent = "teammate", True
        elif row.kind not in ("reply", "unsubscribe"):
            who = "event"
        entries.append(
            (
                at,
                1,
                ThreadEntry(
                    at=at,
                    who=who,
                    label=_INBOUND_LABELS.get(row.kind, row.kind),
                    subject=row.subject,
                    body=row.body_text,
                    sender=row.from_address,
                ),
            )
        )
    if conv.taken_over_at is not None and not human_sent:
        at = _aware(conv.taken_over_at)
        entries.append((at, 2, ThreadEntry(at=at, who="event", label="Taken over by a teammate")))
    if conv.status == "handled" and conv.handled_at is not None:
        at = _aware(conv.handled_at)
        entries.append((at, 2, ThreadEntry(at=at, who="event", label="Marked handled")))
    entries.sort(key=lambda e: (e[0], e[1]))
    return [entry for _, _, entry in entries]


async def pending_answers(db: AsyncSession, sequence_id: uuid.UUID) -> list[Message]:
    """The sequence's answers not sent yet (Claude's drafts and teammates' answers), oldest
    first: each links to its Outbox card."""
    return list(
        (
            await db.scalars(
                select(Message)
                .where(
                    Message.sequence_id == sequence_id,
                    Message.kind == "reply",
                    Message.status.in_(PENDING_ANSWER),
                )
                .order_by(Message.step, Message.id)
            )
        ).all()
    )


async def waiting_for_claude(db: AsyncSession, conv: Conversation, *, config: AppConfig) -> int:
    """How many of the conversation's replies Claude has still to read: unclassified and not
    given up on, while the conversation is open or flagged (``handle_replies`` reads only
    those). Not stored anywhere: derived from the rows."""
    if conv.status not in ACTIVE:
        return 0
    count = await db.scalar(
        select(func.count())
        .select_from(MailboxMessage)
        .where(
            MailboxMessage.conversation_id == conv.id,
            MailboxMessage.kind == "reply",
            MailboxMessage.classified_at.is_(None),
            MailboxMessage.classify_failures < config.replies.max_classify_failures,
        )
    )
    return int(count or 0)


async def claude_waiting_reason(
    db: AsyncSession, *, claude_bin: str, config: AppConfig, now: datetime
) -> str | None:
    """Why Claude may not have read a reply yet, when that can be told cheaply (nothing is
    run): today's budget is spent, the CLI isn't on this machine, or today's latest call
    found Claude signed out. None when none of these holds (it is probably just queued)."""
    if await calls_today(db, now=now) >= config.usage.max_daily_claude_calls:
        return BUDGET_SPENT
    if shutil.which(claude_bin) is None:
        return CLI_MISSING
    latest = (
        await db.execute(
            select(UsageEvent.ok, UsageEvent.error)
            .where(UsageEvent.at >= now - timedelta(days=1))
            .order_by(UsageEvent.at.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is not None and not latest.ok and latest.error == SIGNED_OUT:
        return CLAUDE_SIGNED_OUT
    return None


# --------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Action:
    """What an action did: the conversation (locked, updated), its status before, whether
    anything changed, and how many unsent messages it cancelled."""

    conversation: Conversation
    was: str
    changed: bool
    cancelled: int = 0
    sequence_status: str | None = None


async def _conversation_row(db: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
    return (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _lock_all(db: AsyncSession, conversation_id: uuid.UUID) -> tuple[Locked, Conversation]:
    """The outreach locks of the conversation's sequence (its messages by id, the sequence,
    the recipient), then the conversation ``FOR UPDATE``; ``ValueError(NOT_FOUND)``."""
    sequence_id = await db.scalar(
        select(Conversation.sequence_id).where(Conversation.id == conversation_id)
    )
    first_id = (
        await db.scalar(
            select(Message.id)
            .where(Message.sequence_id == sequence_id)
            .order_by(Message.id)
            .limit(1)
        )
        if sequence_id is not None
        else None
    )
    locked = await lock_message(db, first_id) if first_id is not None else None
    if locked is None:
        raise ValueError(NOT_FOUND)
    await lock_recipient(db, locked.sequence.recipient)
    conv = await _conversation_row(db, conversation_id)
    if conv is None or conv.sequence_id != locked.sequence.id:
        raise ValueError(NOT_FOUND)
    return locked, conv


async def assign_to(db: AsyncSession, conversation_id: uuid.UUID, *, user: User) -> Action:
    """Assign the conversation to ``user`` (only its row is locked). ``changed`` is False when
    it already was. ``ValueError(NOT_FOUND)``."""
    conv = await _conversation_row(db, conversation_id)
    if conv is None:
        raise ValueError(NOT_FOUND)
    changed = conv.assigned_to_id != user.id
    conv.assigned_to_id = user.id
    await db.flush()
    return Action(conv, conv.status, changed)


async def mark_handled(
    db: AsyncSession, conversation_id: uuid.UUID, *, actor: User, now: datetime
) -> Action:
    """Nothing more to do: Claude's unsent reply drafts are cancelled and the conversation is
    ``handled`` by ``actor``. An answer a teammate wrote is kept (Ruling P6-N: a person chose
    to send it). ``changed`` is False when it was already handled, or when it was taken over:
    taken over is final for automation (Ruling P6-Q), and handling it would let a later reply
    reopen it for Claude. ``ValueError(NOT_FOUND)``."""
    locked, conv = await _lock_all(db, conversation_id)
    was = conv.status
    if was in CLOSED:
        return Action(conv, was, False)
    cancelled = await cancel_reply_drafts(db, locked, claude_only=True)
    conv.status = "handled"
    conv.needs_you_reason = None
    conv.handled_by_id = actor.id
    conv.handled_at = now
    await db.flush()
    return Action(conv, was, True, cancelled, locked.sequence.status)


async def take_over(
    db: AsyncSession, conversation_id: uuid.UUID, *, actor: User, now: datetime
) -> Action:
    """A teammate takes the conversation over: automation stops for this person, as when
    their mail appears in the mailbox's Sent folder (Ruling P6-Q). Claude's unsent drafts and
    the sequence's unsent follow-up are cancelled: a sequence still sending is stopped, a
    replied one stays ``replied``. An answer a teammate wrote (approved, waiting or held, or
    not confirmed) is kept, as a person chose to send it (Ruling P6-N). The conversation
    becomes ``taken_over``, which is final for automation. ``changed`` is False when it
    already was. ``ValueError(NOT_FOUND)``."""
    del actor  # the caller audits who took it over
    locked, conv = await _lock_all(db, conversation_id)
    was = conv.status
    if was == "taken_over":
        return Action(conv, was, False)
    sequence = locked.sequence
    if sequence.status in ("awaiting_approval", "active"):
        cancelled = await stop_sequence(
            db, locked, reason=IN_APP_TAKEOVER, keep_teammate_replies=True
        )
    elif sequence.status == "replied":
        cancelled = await stop_sequence(
            db, locked, reason=IN_APP_TAKEOVER, status="replied", keep_teammate_replies=True
        )
    else:  # completed, stopped, failed: only Claude's reply drafts can be left
        cancelled = await cancel_reply_drafts(db, locked, claude_only=True)
    conv.status = "taken_over"
    conv.needs_you_reason = None
    conv.taken_over_at = conv.taken_over_at or now
    await db.flush()
    return Action(conv, was, True, cancelled, sequence.status)
