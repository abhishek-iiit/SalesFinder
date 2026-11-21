"""Stopping a sequence: one way for every reason (excluded, bounced, unsubscribed, replied,
taken over, archived campaign's leftovers).

Only unsent messages are cancelled. A message that is ``sending``, ``sent`` or ``unconfirmed``
is never touched: it may already be on the wire, and the sender settles it."""

import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.db.models import AuditLog, Company, Message, Sequence
from salesfinder.exclusions.check import exclusion_for, exclusions_for_emails
from salesfinder.outreach.locks import Locked, lock_message
from salesfinder.replies.match import SENT_STATUSES

__all__ = [
    "IN_PROGRESS",
    "UNSENT",
    "cancel_reply_drafts",
    "excluded_reason",
    "may_have_gone",
    "reached_them",
    "stop_excluded",
    "stop_sequence",
]

log = structlog.get_logger(__name__)

# Not sent yet: cancelling one can't take back mail.
UNSENT = ("pending", "awaiting_approval", "gate_failed", "approved")
# Sequences still working toward (or answering) their recipient.
IN_PROGRESS = ("awaiting_approval", "active", "replied")
STOP_REASON_LIMIT = 300  # sequences.stop_reason column
_EXCLUSION_LABELS = {
    "existing_relationship": "existing relationship",
    "do_not_contact": "do-not-contact",
}


def _cancel(
    messages: list[Message],
    *,
    reply_only: bool = False,
    claude_only: bool = False,
    keep_teammate_replies: bool = False,
) -> int:
    """Mark ``messages`` that are unsent (and, with ``reply_only``, reply drafts; with
    ``claude_only``, only those Claude drafted, ``written_by_id IS NULL``; with
    ``keep_teammate_replies``, every unsent message except a reply a teammate wrote)
    cancelled. Who approved one stays recorded (``approved_by_id``/``approved_at`` are
    kept)."""
    count = 0
    for m in messages:
        teammate_reply = m.kind == "reply" and m.written_by_id is not None
        if (
            m.status in UNSENT
            and (not reply_only or m.kind == "reply")
            and (not claude_only or m.written_by_id is None)
            and not (keep_teammate_replies and teammate_reply)
        ):
            m.status = "cancelled"
            count += 1
    return count


async def may_have_gone(db: AsyncSession, message: Message) -> bool:
    """Whether an earlier attempt of ``message`` may have been delivered although it isn't in
    a sent status now: it keeps its Message-ID and was once ``unconfirmed`` (every such
    transition is audited ``message.unconfirmed``). Retries and failures before DATA, and
    refusals at RCPT, never delivered."""
    if message.message_id_header is None:
        return False
    found = await db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.action == audit.MESSAGE_UNCONFIRMED,
            AuditLog.target_type == "message",
            AuditLog.target_id == str(message.id),
        )
        .limit(1)
    )
    return found is not None


async def reached_them(db: AsyncSession, locked: Locked) -> bool:
    """Whether any email of the locked sequence may have reached its recipient: one is in a
    sent status (``sending``/``sent``/``unconfirmed``/``bounced``), or ``may_have_gone``."""
    for m in locked.siblings:
        if m.status in SENT_STATUSES:
            return True
    for m in locked.siblings:
        if await may_have_gone(db, m):
            return True
    return False


async def stop_sequence(
    db: AsyncSession,
    locked: Locked,
    *,
    reason: str,
    status: Literal["stopped", "completed", "replied"] = "stopped",
    keep_teammate_replies: bool = False,
) -> int:
    """Cancel the sequence's unsent messages (``UNSENT``; never ``sending``/``sent``/
    ``unconfirmed``; with ``keep_teammate_replies``, an answer a teammate wrote is kept), set
    its status and ``stop_reason`` (clipped to 300 characters), and return how many messages
    were cancelled. ``locked`` comes from ``lock_message``, so every sibling is already
    row-locked. Flushes; the caller commits and audits."""
    count = _cancel(locked.siblings, keep_teammate_replies=keep_teammate_replies)
    locked.sequence.status = status
    locked.sequence.stop_reason = reason.strip()[:STOP_REASON_LIMIT]
    await db.flush()
    return count


async def cancel_reply_drafts(
    db: AsyncSession, locked: Locked, *, claude_only: bool = False
) -> int:
    """Cancel the sequence's unsent reply drafts (``kind='reply'``; with ``claude_only``, only
    Claude's, never one a teammate wrote), leaving the first email, the follow-up and the
    sequence status alone. Returns how many. Flushes."""
    count = _cancel(locked.siblings, reply_only=True, claude_only=claude_only)
    await db.flush()
    return count


def _company_reason(company_status: str, reason: str | None, detail: str | None) -> str | None:
    if company_status != "excluded":
        return None
    return (detail or _EXCLUSION_LABELS.get(reason or "", reason or "excluded")).strip()


async def excluded_reason(db: AsyncSession, locked: Locked) -> tuple[str, str] | None:
    """Why the locked sequence may no longer go ahead, read live, as ``(code, detail)``: its
    company is excluded (``company_excluded``), or its recipient is (the ``Exclusion.reason``:
    ``suppressed``, ``do_not_contact``, ``existing_relationship``, ``invalid_address``). None if
    neither."""
    live = (
        await db.execute(
            select(Company.status, Company.exclusion_reason, Company.exclusion_detail).where(
                Company.id == locked.sequence.company_id
            )
        )
    ).one_or_none()
    if live is not None and (why := _company_reason(*live)) is not None:
        return "company_excluded", why
    hit = await exclusion_for(db, email=locked.sequence.recipient)
    return (hit.reason, hit.detail) if hit is not None else None


async def _candidates(db: AsyncSession) -> list[uuid.UUID]:
    """The lowest message id of each in-progress sequence whose company is excluded or whose
    recipient is on an exclusion list now (read without locks; each is re-checked locked)."""
    rows = (
        await db.execute(
            select(Sequence.id, Sequence.recipient, Company.status)
            .join(Company, Company.id == Sequence.company_id)
            .where(Sequence.status.in_(IN_PROGRESS))
        )
    ).all()
    if not rows:
        return []
    hits = await exclusions_for_emails(db, sorted({r.recipient for r in rows}))
    flagged = {r.id for r in rows if r.status == "excluded" or hits.get(r.recipient) is not None}
    if not flagged:
        return []
    by_sequence: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for message_id, sequence_id in (
        await db.execute(
            select(Message.id, Message.sequence_id).where(Message.sequence_id.in_(flagged))
        )
    ).all():
        by_sequence[sequence_id].append(message_id)
    return sorted(min(ids) for ids in by_sequence.values())


async def stop_excluded(db_factory: Callable[[], AsyncSession], *, now: datetime) -> int:
    """Stop every in-progress sequence (``awaiting_approval``/``active``/``replied``) whose
    company is excluded or whose recipient is now on an exclusion list (follow-up 9), each in
    its own short transaction: ``lock_message(<its lowest message id>)``, re-check live, then
    ``stop_sequence(reason="Excluded: <detail>")`` and a system audit row (``sequence.stopped``,
    actor None: ids, the reason code and the number cancelled). Returns how many were stopped;
    running it again stops nothing more. ``now`` is accepted for parity with the other worker
    passes (the stop itself is timestamped by ``updated_at``)."""
    del now
    async with db_factory() as db:
        candidates = await _candidates(db)
    stopped = 0
    for message_id in candidates:
        async with db_factory() as db:
            locked = await lock_message(db, message_id)
            if locked is None or locked.sequence.status not in IN_PROGRESS:
                await db.rollback()
                continue
            why = await excluded_reason(db, locked)
            if why is None:  # lifted meanwhile
                await db.rollback()
                continue
            code, detail = why
            cancelled = await stop_sequence(db, locked, reason=f"Excluded: {detail}")
            await audit.record(
                db,
                action=audit.SEQUENCE_STOPPED,
                actor_id=None,
                target_type="sequence",
                target_id=str(locked.sequence.id),
                details={
                    "campaign_id": str(locked.sequence.campaign_id),
                    "company_id": str(locked.sequence.company_id),
                    "reason": "excluded",
                    "exclusion": code,
                    "cancelled": cancelled,
                },
            )
            await db.commit()
            stopped += 1
    if stopped:
        log.info("sequences_stopped_excluded", count=stopped)
    return stopped
