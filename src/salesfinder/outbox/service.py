"""Outbox: approve, edit, reject and bulk-approve drafts; withdraw an approval; settle a send
that couldn't be confirmed or failed (mark sent, send again). Every approval and edit re-runs
the whole gate on live state; nothing is sent here (the sender does that).

Three kinds of message come through it, each reviewed while its sequence is in one status:
a first email (sequence ``awaiting_approval``), a follow-up that fell due (``active``; queued
by ``followups.service.queue_due``) and a drafted answer to a reply (``replied``).

An archived campaign's emails are refused (``ARCHIVED``). A paused campaign's emails can still
be approved: pausing stops drafting and cold sending (first emails and follow-ups are held
while it is paused); answers to replies still go (Ruling P6-I).

Services flush only: callers commit and audit (``message.approved`` / ``edited`` / ``rejected``
/ ``withdrawn`` / ``marked_sent`` / ``resend`` with message id, campaign and recipient, never
the body)."""

import unicodedata
import uuid
from collections.abc import Sequence as Seq
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from salesfinder import audit
from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.config import AppConfig
from salesfinder.db.models import (
    Campaign,
    Company,
    Conversation,
    MailboxMessage,
    Message,
    OutboxFeedback,
    Person,
    Sequence,
    User,
)
from salesfinder.gate.checks import gate_message
from salesfinder.gate.prompts import instructions_for
from salesfinder.outreach.locks import Locked, lock_message, lock_recipient
from salesfinder.outreach.stop import stop_sequence
from salesfinder.writer.compose import assemble, footer, signature

__all__ = [
    "ARCHIVED",
    "DRAFT_REJECTED",
    "NOT_APPROVED",
    "NOT_FOUND",
    "NOT_RESENDABLE",
    "NOT_UNCONFIRMED",
    "NOT_WAITING",
    "NO_PERSONA",
    "REFUSALS",
    "SEQUENCE_MOVED_ON",
    "STALE_EDIT",
    "OutboxItem",
    "approve",
    "bulk_approve",
    "edit",
    "list_outbox",
    "mark_sent",
    "outbox_counts",
    "outbox_tab_counts",
    "recheck",
    "reject",
    "resend",
    "waiting_for_review",
    "withdraw",
]

NOT_FOUND = "Email not found."
NOT_WAITING = "This email is no longer waiting for approval."
NOT_EDITABLE = "This email can't be edited any more."
NOT_REJECTABLE = "This email can't be rejected any more."
REASON_BAD = "Give a reason of 1 to 500 characters."
NO_PERSONA = "No sender persona is set for this side in facts.yaml."
STALE_EDIT = "Someone else changed this email — reload and try again."
NEED_CATALOG = "A catalog is needed to rebuild the writer prompt."
ARCHIVED = "This campaign is archived."
NOT_APPROVED = "This email isn't waiting to send any more."
NOT_UNCONFIRMED = "This email isn't waiting for a check any more."
NOT_RESENDABLE = "This email can't be sent again."
SEQUENCE_MOVED_ON = "Its sequence has moved on, so it can't be sent again."
# Refusals that leave a message exactly as it was (bulk_approve reports them per id).
REFUSALS = frozenset({NOT_FOUND, NOT_WAITING, NO_PERSONA, NEED_CATALOG, ARCHIVED})
STOP_REASON = "Rejected in the Outbox"
DRAFT_REJECTED = "Drafted answer rejected — write one or answer from the mailbox"
FOLLOWUP_STOP_REASON = "Follow-up rejected in the Outbox"
SUBJECT_LIMIT = 200  # messages.subject column
REASON_LIMIT = 500
_REVIEWABLE = ("awaiting_approval", "gate_failed")
# What a message's sequence is while the message is up for review, by kind.
REVIEW_SEQUENCE = {"first": "awaiting_approval", "followup": "active", "reply": "replied"}
_PROBLEMS = ("unconfirmed", "failed")
_COLD = ("first", "followup")

Kind = Literal["all", "first", "followup", "reply"]
View = Literal["review", "approved", "problems"]
VIEWS: tuple[View, ...] = ("review", "approved", "problems")
KINDS: tuple[Kind, ...] = ("all", "first", "followup", "reply")


@dataclass(frozen=True)
class OutboxItem:
    message: Message
    sequence: Sequence
    company: Company
    person: Person | None
    recipient: str
    campaign_name: str
    campaign_status: str = "active"
    kind: str = "first"
    # When the sequence's first email went out (follow-ups and replies; None for a first email).
    step1_sent_at: datetime | None = None
    # The inbound reply an answer responds to: its new text, sanitized at ingest (untrusted;
    # render escaped). None for first emails and follow-ups.
    in_reply_to_text: str | None = None

    @property
    def campaign_paused(self) -> bool:
        return self.campaign_status == "paused"

    @property
    def held_by_campaign_pause(self) -> bool:
        """An approved first email or follow-up isn't sent while its campaign is paused (a
        reply still is, Ruling P6-I): the Approved tab shows this, as the mailbox's hold reason
        doesn't."""
        return self.campaign_paused and self.kind in _COLD


def waiting_for_review(campaign_id: uuid.UUID | None = None) -> list[ColumnElement[bool]]:
    """The Outbox's filter (Ruling AZ), for a query joining ``Message`` to ``Sequence``:
    messages ready or held by the gate whose sequence is in the status their kind is reviewed
    in (``REVIEW_SEQUENCE``): a first email of a sequence awaiting approval, a follow-up of an
    active one, a drafted answer of a replied one. Every Outbox count uses it."""
    where: list[ColumnElement[bool]] = [
        Message.status.in_(_REVIEWABLE),
        or_(
            *(
                and_(Message.kind == kind, Sequence.status == status)
                for kind, status in REVIEW_SEQUENCE.items()
            )
        ),
    ]
    if campaign_id is not None:
        where.append(Sequence.campaign_id == campaign_id)
    return where


def _view_filter(
    view: View, kind: Kind, campaign_id: uuid.UUID | None
) -> list[ColumnElement[bool]]:
    if view == "review":
        where = waiting_for_review(campaign_id)
    else:
        where = [
            Message.status == "approved" if view == "approved" else Message.status.in_(_PROBLEMS)
        ]
        if campaign_id is not None:
            where.append(Sequence.campaign_id == campaign_id)
    if kind != "all":
        where.append(Message.kind == kind)
    return where


async def outbox_counts(
    db: AsyncSession, *, campaign_id: uuid.UUID | None = None
) -> dict[str, int]:
    """How many Outbox emails are ``awaiting_approval`` and ``gate_failed`` (one query, the
    same filter as ``list_outbox``)."""
    rows = await db.execute(
        select(Message.status, func.count())
        .join(Sequence, Sequence.id == Message.sequence_id)
        .where(*waiting_for_review(campaign_id))
        .group_by(Message.status)
    )
    counts = dict.fromkeys(_REVIEWABLE, 0)
    counts.update({status: int(n) for status, n in rows.all()})
    return counts


async def outbox_tab_counts(
    db: AsyncSession, *, campaign_id: uuid.UUID | None = None, kind: Kind = "all"
) -> dict[str, int]:
    """How many emails each Outbox tab (``review``, ``approved``, ``problems``) holds, with
    the same filters as ``list_outbox``."""
    counts: dict[str, int] = {}
    for view in VIEWS:
        counts[view] = int(
            await db.scalar(
                select(func.count())
                .select_from(Message)
                .join(Sequence, Sequence.id == Message.sequence_id)
                .where(*_view_filter(view, kind, campaign_id))
            )
            or 0
        )
    return counts


async def list_outbox(
    db: AsyncSession,
    *,
    campaign_id: uuid.UUID | None = None,
    kind: Kind = "all",
    view: View = "review",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[OutboxItem], int]:
    """One Outbox tab, oldest first, and its total count:

    - ``review``: waiting for a teammate (``awaiting_approval`` or ``gate_failed``,
      ``waiting_for_review``), ordered by when they came up (a follow-up by when it fell due);
    - ``approved``: approved and waiting to send, by approval time;
    - ``problems``: sends that couldn't be confirmed (``unconfirmed``) or failed for good
      (``failed``), by when they were claimed.

    ``kind`` narrows it to first emails, follow-ups or replies."""
    where = _view_filter(view, kind, campaign_id)
    base = select(Message).join(Sequence, Sequence.id == Message.sequence_id).where(*where)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    first_key: ColumnElement[Any]
    if view == "review":
        first_key = func.coalesce(Message.due_at, Message.created_at)
    elif view == "approved":
        first_key = Message.approved_at.asc().nulls_last()
    else:
        first_key = func.coalesce(Message.claimed_at, Message.updated_at)
    step1 = aliased(Message)
    rows = await db.execute(
        select(
            Message,
            Sequence,
            Company,
            Person,
            Campaign.name,
            Campaign.status,
            step1.sent_at,
            MailboxMessage.body_text,
        )
        .join(Sequence, Sequence.id == Message.sequence_id)
        .join(Company, Company.id == Sequence.company_id)
        .join(Campaign, Campaign.id == Sequence.campaign_id)
        .outerjoin(Person, Person.id == Sequence.person_id)
        .outerjoin(step1, and_(step1.sequence_id == Sequence.id, step1.step == 1))
        .outerjoin(MailboxMessage, MailboxMessage.id == Message.reply_to_id)
        .where(*where)
        .order_by(first_key, Message.id)
        .limit(limit)
        .offset(offset)
    )
    items = [
        OutboxItem(
            message=m,
            sequence=s,
            company=c,
            person=p,
            recipient=s.recipient,
            campaign_name=name,
            campaign_status=status,
            kind=m.kind,
            step1_sent_at=first_sent if m.kind != "first" else None,
            in_reply_to_text=their_text if m.kind == "reply" else None,
        )
        for m, s, c, p, name, status, first_sent, their_text in rows.all()
    ]
    return items, total


async def _lock(
    db: AsyncSession,
    message_id: uuid.UUID,
    allowed: tuple[str, ...],
    refusal: str,
    *,
    sequence_for: dict[str, str] | None = REVIEW_SEQUENCE,
    archived_ok: bool = False,
) -> Locked:
    """``outreach.locks.lock_message``: every message of the message's sequence (step 1, its
    follow-up, reply drafts) in id order, then the sequence, row-locked (``FOR UPDATE``, fresh
    from the database), with their context (the campaign read without a lock); or
    ``ValueError`` with a human message when it is missing, its campaign is archived
    (``ARCHIVED``, unless ``archived_ok``), it is not in an ``allowed`` status, or its sequence
    isn't in the status ``sequence_for`` gives for its kind (by default the status it is
    reviewed in, ``REVIEW_SEQUENCE``; None skips that check).

    Lock order: a sequence's messages by id, then the sequence. ``campaigns.service.archive``
    locks messages by id, then sequences by id, so the two can't cross; and since the
    follow-up is locked up front, rejecting never waits for a row while holding another."""
    locked = await lock_message(db, message_id)
    if locked is None:
        raise ValueError(NOT_FOUND)
    if locked.campaign.status == "archived" and not archived_ok:
        raise ValueError(ARCHIVED)
    if locked.message.status not in allowed:
        raise ValueError(refusal)
    kind = locked.message.kind
    if sequence_for is not None and locked.sequence.status != sequence_for.get(kind):
        raise ValueError(refusal)
    return locked


def _side(campaign: Campaign, facts: Facts) -> Side:
    """The campaign's side; ``ValueError`` if facts.yaml has no sender persona for it (the
    signature couldn't be built)."""
    side: Side = "supplier" if campaign.side == "supplier" else "buyer"
    if side not in facts.personas:
        raise ValueError(NO_PERSONA)
    return side


def _need_prompt_source(catalog: Catalog | None, prompt_text: str | None) -> None:
    if catalog is None and prompt_text is None:
        raise ValueError(NEED_CATALOG)


async def _gate(
    db: AsyncSession,
    loaded: Locked,
    *,
    subject: str,
    body: str,
    now: datetime,
    facts: Facts,
    config: AppConfig,
    catalog: Catalog | None,
    prompt_text: str | None,
) -> list[str]:
    """The whole gate for this message, with its own sequence excluded from "another campaign".
    Without ``prompt_text`` the prompt is rebuilt read-only (no Claude call); a context that
    can't be built (persona no longer confirmed, ...) is itself the problem."""
    if prompt_text is None:
        _need_prompt_source(catalog, prompt_text)
        assert catalog is not None  # noqa: S101 - narrowed by _need_prompt_source
        try:
            prompt_text = await instructions_for(
                db,
                message=loaded.message,
                campaign=loaded.campaign,
                company=loaded.company,
                person=loaded.person,
                catalog=catalog,
                facts=facts,
                config=config,
            )
        except ValueError as exc:
            return [str(exc)]
    # One lock per address, as when drafting, so concurrent approvals see each other.
    await lock_recipient(db, loaded.sequence.recipient)
    return await gate_message(
        db,
        subject=subject,
        body=body,
        step=loaded.message.step,
        side=_side(loaded.campaign, facts),
        facts=facts,
        prompt_text=prompt_text,
        sequence_or_none=loaded.sequence,
        recipient=loaded.sequence.recipient,
        company=loaded.company,
        address=loaded.address,
        now=now,
        config=config,
    )


async def approve(
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    actor: User,
    now: datetime,
    facts: Facts,
    config: AppConfig,
    catalog: Catalog | None = None,
    prompt_text: str | None = None,
) -> list[str]:
    """Approve a message waiting for approval, after re-running the gate. Problems: the message
    becomes ``gate_failed`` with them stored (and returned), nothing is approved. Otherwise it
    is ``approved``; a first email's sequence becomes ``active`` (a follow-up's stays
    ``active``, a reply's ``replied``). Raises ``ValueError`` if the message isn't
    ``awaiting_approval`` or its sequence isn't in its kind's review status."""
    _need_prompt_source(catalog, prompt_text)
    loaded = await _lock(db, message_id, ("awaiting_approval",), NOT_WAITING)
    _side(loaded.campaign, facts)  # refuse before any change
    message = loaded.message
    problems = await _gate(
        db,
        loaded,
        subject=message.subject,
        body=message.body,
        now=now,
        facts=facts,
        config=config,
        catalog=catalog,
        prompt_text=prompt_text,
    )
    if problems:
        message.status = "gate_failed"
        message.gate_errors = problems
    else:
        message.status = "approved"
        message.gate_errors = []
        message.approved_by_id = actor.id
        message.approved_at = now
        if message.kind == "first":
            loaded.sequence.status = "active"
    await db.flush()
    return problems


async def recheck(
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    by: User,
    now: datetime,
    facts: Facts,
    config: AppConfig,
    catalog: Catalog | None = None,
    prompt_text: str | None = None,
) -> list[str]:
    """Re-run the gate on a ``gate_failed`` message as it is stored (for a hold that has since
    cleared, e.g. a lifted exclusion). No problems -> ``awaiting_approval`` with errors cleared;
    otherwise it stays ``gate_failed`` with fresh errors. Never edits and never approves
    (``edited`` is unchanged). ``ValueError`` if the message isn't ``gate_failed``."""
    _need_prompt_source(catalog, prompt_text)
    loaded = await _lock(db, message_id, ("gate_failed",), NOT_WAITING)
    _side(loaded.campaign, facts)  # refuse before any change
    message = loaded.message
    problems = await _gate(
        db,
        loaded,
        subject=message.subject,
        body=message.body,
        now=now,
        facts=facts,
        config=config,
        catalog=catalog,
        prompt_text=prompt_text,
    )
    message.status = "gate_failed" if problems else "awaiting_approval"
    message.gate_errors = problems
    await db.flush()
    return problems


def _without_tail(written: str, signature_text: str, footer_text: str) -> str:
    """``written`` (already cleaned) minus what a teammate pasted back at its end: the exact
    signature + footer, else the footer alone, then the signature alone; each at most once."""
    text = written.rstrip()
    sig, foot = signature_text.strip(), footer_text.strip()
    both = f"{sig}\n\n{foot}"
    if both.strip() and text.endswith(both):
        return text[: len(text) - len(both)]
    if foot and text.endswith(foot):
        text = text[: len(text) - len(foot)].rstrip()
    if sig and text.endswith(sig):
        text = text[: len(text) - len(sig)]
    return text


async def edit(
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    subject: str,
    body: str,
    actor: User,
    now: datetime,
    facts: Facts,
    config: AppConfig,
    catalog: Catalog | None = None,
    prompt_text: str | None = None,
    expected_updated_at: datetime | None = None,
) -> list[str]:
    """Replace the written part of a message waiting for approval or failed by the gate.
    ``body`` excludes the signature and footer (code re-appends them; an exact copy a teammate
    pasted back is removed first). The gate re-runs: no problems -> ``awaiting_approval``,
    else ``gate_failed`` with them stored; never straight to ``approved``.

    Raises ``ValueError`` (message left untouched) for a wrong status, an empty body, a
    subject over 200 characters, a side without a sender persona, or — with
    ``expected_updated_at`` — a message changed since the teammate opened it (``STALE_EDIT``,
    an optimistic check against the locked row); the route shows its text."""
    _need_prompt_source(catalog, prompt_text)
    loaded = await _lock(db, message_id, _REVIEWABLE, NOT_EDITABLE)
    if expected_updated_at is not None and loaded.message.updated_at != expected_updated_at:
        raise ValueError(STALE_EDIT)
    side = _side(loaded.campaign, facts)
    # Assemble once to normalise line breaks, drop pasted tails, then assemble the rest; all
    # validation happens before any field is changed.
    _, cleaned = assemble(subject, body, facts=facts, writing=config.writing, side=side)
    tail = signature(facts, side) + footer(facts, config.writing)
    written = _without_tail(
        cleaned[: len(cleaned) - len(tail)], signature(facts, side), footer(facts, config.writing)
    )
    new_subject, new_body = assemble(
        subject, written, facts=facts, writing=config.writing, side=side
    )
    if len(new_subject) > SUBJECT_LIMIT:
        raise ValueError(f"Subject is longer than {SUBJECT_LIMIT} characters.")
    message = loaded.message
    message.subject = new_subject
    message.body = new_body
    message.edited = True
    problems = await _gate(
        db,
        loaded,
        subject=new_subject,
        body=new_body,
        now=now,
        facts=facts,
        config=config,
        catalog=catalog,
        prompt_text=prompt_text,
    )
    message.status = "gate_failed" if problems else "awaiting_approval"
    message.gate_errors = problems
    await db.flush()
    return problems


def _one_line(text: str) -> str:
    """``text`` with every whitespace run and control character made one space."""
    return " ".join(
        "".join(
            " " if ch.isspace() or unicodedata.category(ch) == "Cc" else ch for ch in text
        ).split()
    )


async def reject(
    db: AsyncSession, message_id: uuid.UUID, *, reason: str, actor: User, now: datetime
) -> None:
    """Reject a message (reason of 1 to 500 characters).

    - A first email: its sequence stops ("Rejected in the Outbox"), the pending follow-up is
      cancelled, and the reason is kept as feedback for the campaign's next drafts.
    - A follow-up: its sequence stops ("Follow-up rejected in the Outbox"), feedback kept.
    - A drafted answer to a reply: only the draft is rejected (no feedback; the sequence stays
      ``replied``), and an ``open``/``needs_you`` conversation needs a teammate
      (``DRAFT_REJECTED``; audited ``conversation.needs_you`` by ``actor`` here, as the
      caller audits only the message). The conversation is locked after the outreach locks."""
    why = _one_line(reason)
    if not 1 <= len(why) <= REASON_LIMIT:
        raise ValueError(REASON_BAD)
    loaded = await _lock(db, message_id, _REVIEWABLE, NOT_REJECTABLE)
    message = loaded.message
    message.status = "rejected"
    message.rejected_by_id = actor.id
    message.rejected_at = now
    message.reject_reason = why
    if message.kind == "reply":
        await _answer_rejected(db, loaded, actor=actor)
        return
    # Rejected first (no longer unsent), so stopping cancels only the rest: the pending
    # follow-up of a first email.
    await stop_sequence(
        db,
        loaded,
        reason=STOP_REASON if message.kind == "first" else FOLLOWUP_STOP_REASON,
    )
    db.add(
        OutboxFeedback(
            campaign_id=loaded.campaign.id,
            message_id=message.id,
            reason=why,
            created_by_id=actor.id,
            created_at=now,
        )
    )
    await db.flush()


async def _answer_rejected(db: AsyncSession, loaded: Locked, *, actor: User) -> None:
    """A rejected answer leaves them unanswered: the conversation needs a teammate (unless it
    was taken over or handled meanwhile). Locked after the outreach locks."""
    conv = (
        await db.execute(
            select(Conversation)
            .where(Conversation.sequence_id == loaded.sequence.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    changed = (
        conv is not None
        and conv.status in ("open", "needs_you")
        and (conv.status, conv.needs_you_reason) != ("needs_you", DRAFT_REJECTED)
    )
    if conv is not None and changed:
        conv.status = "needs_you"
        conv.needs_you_reason = DRAFT_REJECTED
    await db.flush()
    if conv is not None and changed:
        await audit.record(
            db,
            action=audit.CONVERSATION_NEEDS_YOU,
            actor_id=actor.id,
            target_type="conversation",
            target_id=str(conv.id),
            details={
                "sequence_id": str(conv.sequence_id),
                "reason": "draft_rejected",
                "message_id": str(loaded.message.id),
            },
        )


async def bulk_approve(
    db: AsyncSession,
    message_ids: Seq[uuid.UUID],
    *,
    actor: User,
    now: datetime,
    facts: Facts,
    config: AppConfig,
    catalog: Catalog | None = None,
    prompt_text: str | None = None,
) -> dict[uuid.UUID, list[str]]:
    """Approve each message independently, each in its own savepoint (one failing doesn't block
    the others). Maps message id -> problems: empty when approved; a gate failure's problems
    (the message is then ``gate_failed``); or a refusal (one of ``REFUSALS``: missing, wrong
    status, archived campaign, no persona), the message unchanged."""
    order = list(dict.fromkeys(message_ids))
    results: dict[uuid.UUID, list[str]] = {}
    for message_id in sorted(order):  # one lock order, whatever order the caller listed them in
        try:
            async with db.begin_nested():
                results[message_id] = await approve(
                    db,
                    message_id,
                    actor=actor,
                    now=now,
                    facts=facts,
                    config=config,
                    catalog=catalog,
                    prompt_text=prompt_text,
                )
        except ValueError as exc:
            results[message_id] = [str(exc)]
    return {message_id: results[message_id] for message_id in order}


# --------------------------------------------------------------------------- after approval


async def withdraw(db: AsyncSession, message_id: uuid.UUID, *, actor: User, now: datetime) -> None:
    """Take back an approval: an ``approved`` message goes back to ``awaiting_approval`` (who
    approved it and when are cleared, and any retry schedule with them) and a first email's
    sequence back to ``awaiting_approval``, so it is in the Outbox again. Its Message-ID is
    kept (an earlier attempt may have gone). ``ValueError(NOT_APPROVED)`` if it isn't
    ``approved`` any more (e.g. already ``sending``: the sender claimed it first)."""
    del actor, now  # the caller audits who withdrew it, and when
    loaded = await _lock(db, message_id, ("approved",), NOT_APPROVED, sequence_for=None)
    message = loaded.message
    message.status = "awaiting_approval"
    message.approved_by_id = None
    message.approved_at = None
    message.send_attempts = 0
    message.next_attempt_at = None
    if message.kind == "first" and loaded.sequence.status == "active":
        loaded.sequence.status = "awaiting_approval"
    await db.flush()


async def mark_sent(
    db: AsyncSession, message_id: uuid.UUID, *, actor: User, now: datetime, config: AppConfig
) -> None:
    """A teammate found an ``unconfirmed`` email in the mailbox's Sent folder ("It was sent"):
    it becomes ``sent`` with what follows a send (``sending.sender.apply_sent``: a first email
    schedules its follow-up, a follow-up completes its sequence), dated when it was claimed
    (else ``now``). Its Message-ID is kept. Allowed for an archived campaign too: it records
    what happened. ``ValueError(NOT_UNCONFIRMED)`` otherwise."""
    # Imported here: the sender imports the campaigns service, which imports this module.
    from salesfinder.sending.sender import apply_sent

    del actor  # the caller audits who marked it
    loaded = await _lock(
        db, message_id, ("unconfirmed",), NOT_UNCONFIRMED, sequence_for=None, archived_ok=True
    )
    await apply_sent(db, loaded, sent_at=loaded.message.claimed_at or now, config=config)


async def resend(db: AsyncSession, message_id: uuid.UUID, *, actor: User, now: datetime) -> None:
    """Send an ``unconfirmed`` or ``failed`` email again ("Send again"): back to ``approved``
    by ``actor`` at ``now`` (a fresh approval, which also restarts the sender's wait for the
    recipient's mail server), with its attempts and retry schedule reset. **Its Message-ID is
    kept**, so a duplicate (if the first attempt did go) is traceable; the sender re-runs the
    whole gate when it claims it. ``ValueError``: ``NOT_RESENDABLE`` for another status,
    ``ARCHIVED``, or ``SEQUENCE_MOVED_ON`` when its sequence can no longer send it (e.g. they
    replied, or it was stopped)."""
    # Imported here: the sender imports the campaigns service, which imports this module.
    from salesfinder.sending.sender import SEQUENCE_FOR_KIND

    loaded = await _lock(db, message_id, _PROBLEMS, NOT_RESENDABLE, sequence_for=None)
    message = loaded.message
    if loaded.sequence.status != SEQUENCE_FOR_KIND.get(message.kind):
        raise ValueError(SEQUENCE_MOVED_ON)
    message.status = "approved"
    message.approved_by_id = actor.id
    message.approved_at = now
    message.send_attempts = 0
    message.next_attempt_at = None
    message.last_send_error = None
    await db.flush()
