"""Hard bounces: an address refused at RCPT (the sender) or a definite 5.x.x DSN (ingest).

Two halves, because of the lock rule in ``suppressions.service``:

- ``apply_bounce`` runs inside the caller's transaction while it holds the message, sequence
  and recipient (advisory) locks: the message is ``bounced``, the sequence stops, the address
  is suppressed (``refresh=False``) and the kill switch is checked.
- ``after_bounce`` runs after that commits, in its own short transaction holding no outreach
  lock: it takes company locks (as the contact check does) to mark the stored address
  ``invalid``, distrust the domain's learned pattern if the address came from it
  (``contacts.service.drop_pattern``), and refresh the address's ``blocked_reason`` and its
  companies' counts (``refresh_blocks``). Doing these under the outreach locks could deadlock
  with a contact check that holds a company and deletes an address (``ON DELETE SET NULL`` on
  ``sequences.email_address_id`` then waits for the sequence row).

Skipping ``after_bounce`` (a crash in between) is safe for sending: the suppression is already
committed and the gate reads it live; the worker's Sweep step brings ``blocked_reason`` up to
date. Services flush; callers commit."""

from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts.service import (
    drop_pattern,
    lock_bounce_companies,
    mark_invalid,
    refresh_blocks,
)
from salesfinder.mail.smtp import clean_detail
from salesfinder.outreach.locks import Locked
from salesfinder.outreach.stop import stop_sequence
from salesfinder.sending.state import trip_if_bouncing
from salesfinder.suppressions.service import suppress

__all__ = ["BOUNCED", "DETAIL_LIMIT", "after_bounce", "apply_bounce"]

log = structlog.get_logger(__name__)

BOUNCED = "Bounced"
DETAIL_LIMIT = 300  # messages.bounce_detail, suppressions.detail
# A message that may have reached the recipient's server; a bounce settles it as bounced.
_BOUNCEABLE = ("sending", "sent", "unconfirmed", "bounced")


async def apply_bounce(
    db: AsyncSession,
    locked: Locked,
    *,
    recipient: str,
    detail: str,
    now: datetime,
    config: AppConfig,
) -> str | None:
    """Record a hard bounce of ``locked.message`` to ``recipient``. The caller holds the
    message, sequence and recipient locks (``lock_message`` + ``lock_recipient``).

    The message becomes ``bounced`` (from ``sending`` for a refusal at RCPT, from ``sent`` or
    ``unconfirmed`` for a DSN) with ``bounced_at`` and ``bounce_detail`` (server text, cleaned
    and clipped: stored, never logged). It keeps (or gets) a ``sent_at`` so the bounce counts
    toward the kill-switch window. The sequence stops ("Bounced"; unsent mail cancelled, audited
    ``sequence.stopped``), the address is suppressed (``bounced``; ``refresh=False``), and the
    kill switch is checked last. Returns the kill switch's reason when it just paused sending
    (the caller audits ``sending.auto_paused``), else None. Flushes; the caller commits, then
    runs ``after_bounce``."""
    message = locked.message
    text = clean_detail(detail, DETAIL_LIMIT) or "Bounced"
    if message.status in _BOUNCEABLE:
        message.status = "bounced"
        message.sent_at = message.sent_at or now
    message.bounced_at = message.bounced_at or now
    message.bounce_detail = text
    await db.flush()
    cancelled = await stop_sequence(db, locked, reason=BOUNCED)
    await audit.record(
        db,
        action=audit.SEQUENCE_STOPPED,
        actor_id=None,
        target_type="sequence",
        target_id=str(locked.sequence.id),
        details={
            "campaign_id": str(locked.sequence.campaign_id),
            "company_id": str(locked.sequence.company_id),
            "message_id": str(message.id),
            "reason": "bounced",
            "cancelled": cancelled,
        },
    )
    try:
        await suppress(
            db,
            recipient,
            reason="bounced",
            detail=text,
            message_id=message.id,
            refresh=False,
        )
    except ValueError:
        # Not a normalizable address: it can never be sent to anyway (the gate fails closed).
        log.warning("bounce_unsuppressible", message_id=str(message.id))
    return await trip_if_bouncing(db, now=now, config=config)


async def after_bounce(db: AsyncSession, *, recipient: str, now: datetime) -> None:
    """The second half of a bounce, holding no outreach lock (see the module docstring): every
    stored copy of ``recipient`` becomes ``invalid``; if one was ``pattern_confirmed`` (made
    from the domain's learned pattern) the pattern is dropped (``drop_pattern``); then the
    address's ``blocked_reason`` and its companies' counts are refreshed. Takes company locks.
    Flushes; the caller commits."""
    address = recipient.strip().lower()
    # Every company it may write, locked once in id order before any update.
    await lock_bounce_companies(db, address)
    before = await mark_invalid(db, address)
    if ("pattern", "pattern_confirmed") in before:
        await drop_pattern(db, address.rpartition("@")[2], now=now)
    await refresh_blocks(db, email=address)
