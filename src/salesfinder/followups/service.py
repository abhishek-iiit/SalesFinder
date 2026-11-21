"""Queue due follow-ups for review.

A sequence's step 2 is drafted with step 1 and stays ``pending`` (no ``due_at``) until step 1
is sent; the sender then sets its ``due_at``. Once due, ``queue_due`` gives it the subject
``Re: <step 1 subject as sent>`` (so it threads in Gmail, and what a teammate approves is
exactly what is sent), runs the whole gate on live state and puts it in the Outbox:
``awaiting_approval`` or ``gate_failed``. **It never sends**: the sender only ever takes
``approved`` messages, and a ``pending`` one is never approved here.

A paused campaign's follow-ups are still queued (approving is allowed while paused; the sender
holds cold mail of a paused campaign). An archived campaign's sequence is stopped instead."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Literal

import structlog
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from salesfinder import audit
from salesfinder.campaigns.service import ARCHIVE_STOP_REASON
from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, refresh_mx, stale_mx_domains
from salesfinder.db.models import Message, Sequence
from salesfinder.gate.checks import gate_message
from salesfinder.gate.prompts import instructions_for
from salesfinder.outbox.service import NO_PERSONA, SUBJECT_LIMIT
from salesfinder.outreach.locks import Locked, lock_message, lock_recipient
from salesfinder.outreach.stop import stop_sequence

__all__ = ["followup_subject", "queue_due"]

log = structlog.get_logger(__name__)

_Result = Literal["queued", "held", "stopped", "skipped"]


def followup_subject(step1_subject: str) -> str:
    """``Re: <step 1 subject>``, unless it already starts with ``Re:`` (any case); at most
    200 characters (the column)."""
    subject = step1_subject.strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return subject[:SUBJECT_LIMIT]


def _due(now: datetime) -> list[ColumnElement[bool]]:
    """Step 2 still ``pending``, due, in an ``active`` sequence whose step 1 was sent."""
    step1 = aliased(Message)
    return [
        Message.kind == "followup",
        Message.status == "pending",
        Message.due_at.is_not(None),
        Message.due_at <= now,
        Sequence.status == "active",
        select(step1.id)
        .where(
            and_(step1.sequence_id == Message.sequence_id, step1.step == 1),
            step1.status == "sent",
        )
        .exists(),
    ]


async def _due_ids(
    db_factory: async_sessionmaker[AsyncSession], *, now: datetime, limit: int
) -> list[tuple[uuid.UUID, str]]:
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(Message.id, Sequence.recipient)
                .join(Sequence, Sequence.id == Message.sequence_id)
                .where(*_due(now))
                .order_by(Message.due_at, Message.id)
                .limit(limit)
            )
        ).all()
        await db.rollback()
    return [(r.id, r.recipient) for r in rows]


async def _refresh_mx(
    db_factory: async_sessionmaker[AsyncSession],
    mx_checker: MxChecker | None,
    recipient: str,
    *,
    now: datetime,
    config: AppConfig,
) -> None:
    """Look the recipient's MX up again if it is stale (``mx_for``), outside any transaction,
    so the gate below judges a fresh answer."""
    if mx_checker is None:
        return
    async with db_factory() as db:
        stale = await stale_mx_domains(db, [recipient], now=now, config=config.contacts)
        await db.rollback()
    if stale:
        await refresh_mx(db_factory, mx_checker, stale, now=now, config=config.contacts)


async def _problems(
    db: AsyncSession,
    locked: Locked,
    *,
    subject: str,
    now: datetime,
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
) -> list[str]:
    side: Side = "supplier" if locked.campaign.side == "supplier" else "buyer"
    if side not in facts.personas:
        return [NO_PERSONA]
    try:
        prompt_text = await instructions_for(
            db,
            message=locked.message,
            campaign=locked.campaign,
            company=locked.company,
            person=locked.person,
            catalog=catalog,
            facts=facts,
            config=config,
        )
    except ValueError as exc:
        return [str(exc)]
    return await gate_message(
        db,
        subject=subject,
        body=locked.message.body,
        step=2,
        side=side,
        facts=facts,
        prompt_text=prompt_text,
        sequence_or_none=locked.sequence,
        recipient=locked.sequence.recipient,
        company=locked.company,
        address=locked.address,
        now=now,
        config=config,
    )


async def _queue_one(
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    now: datetime,
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
) -> _Result:
    # Lock order: the sequence's messages by id -> the sequence -> the recipient.
    locked = await lock_message(db, message_id)
    if locked is None:
        return "skipped"
    message, sequence = locked.message, locked.sequence
    step1 = next((m for m in locked.siblings if m.step == 1), None)
    if (
        message.kind != "followup"
        or message.status != "pending"
        or message.due_at is None
        or message.due_at > now
        or sequence.status != "active"
        or step1 is None
        or step1.status != "sent"
    ):
        return "skipped"  # moved on since it was selected (a reply, a stop, ...)
    if locked.campaign.status == "archived":
        cancelled = await stop_sequence(db, locked, reason=ARCHIVE_STOP_REASON)
        await audit.record(
            db,
            action=audit.SEQUENCE_STOPPED,
            actor_id=None,
            target_type="sequence",
            target_id=str(sequence.id),
            details={
                "campaign_id": str(sequence.campaign_id),
                "company_id": str(sequence.company_id),
                "reason": "archived",
                "cancelled": cancelled,
            },
        )
        return "stopped"
    await lock_recipient(db, sequence.recipient)
    subject = followup_subject(step1.subject)
    problems = await _problems(
        db, locked, subject=subject, now=now, config=config, facts=facts, catalog=catalog
    )
    message.subject = subject
    message.status = "gate_failed" if problems else "awaiting_approval"
    message.gate_errors = problems
    await db.flush()
    return "held" if problems else "queued"


async def queue_due(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime],
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
    mx_checker: MxChecker | None,
    limit: int = 50,
) -> tuple[int, int]:
    """Queue up to ``limit`` due follow-ups (oldest due first) for review and return
    ``(queued, held)``: ``queued`` counts every one put in the Outbox, ``held`` those of them
    the gate held (``gate_failed``, with the problems stored).

    Selected (unlocked): step 2 ``pending`` with ``due_at <= now``, its sequence ``active`` and
    step 1 ``sent``. Each one, after a stale recipient MX is refreshed outside any transaction,
    runs in its own transaction: ``lock_message`` -> ``lock_recipient`` -> re-check -> subject
    ``Re: <step 1 subject>`` -> ``gate_message(step=2)`` -> ``awaiting_approval``
    (``gate_errors=[]``) or ``gate_failed``. An archived campaign's sequence is stopped
    instead (audited ``sequence.stopped``, system actor). Never approves, never sends.

    Each follow-up is isolated: an unexpected error with one (its transaction rolled back) is
    logged by exception type only and the pass goes on with the next."""
    queued = held = 0
    for message_id, recipient in await _due_ids(db_factory, now=now(), limit=limit):
        try:
            await _refresh_mx(db_factory, mx_checker, recipient, now=now(), config=config)
            async with db_factory() as db:
                result = await _queue_one(
                    db, message_id, now=now(), config=config, facts=facts, catalog=catalog
                )
                if result == "skipped":
                    await db.rollback()
                    continue
                await db.commit()
        except Exception as exc:
            log.warning(
                "followup_failed", message_id=str(message_id), error_type=type(exc).__name__
            )
            continue
        if result in ("queued", "held"):
            queued += 1
        if result == "held":
            held += 1
    if queued:
        log.info("followups_queued", queued=queued, held=held)
    return queued, held
