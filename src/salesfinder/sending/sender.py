"""The sender: claim an approved email, re-gate it, send it, record the result.

Never a duplicate send, never a send that skipped the live gate, never a transaction held
during SMTP:

1. ``claim`` (one short transaction): lock the message's sequence (messages by id, then the
   sequence), the recipient (advisory lock) and the mailbox row, in that order (the campaign is
   read without a lock). Re-check status, campaign, the global pause, the mailbox and persona,
   the cold-sending rules, and the whole gate on live state. A pass marks the message
   ``sending`` with its Message-ID and a snapshot of what was in force, and commits.
2. ``deliver``: read the claimed row, build the MIME from the **stored** subject and body, and
   call SMTP with no session open.
3. ``record`` (short transactions): settle the outcome under the message locks. Anything
   unclear (a timeout during DATA, a crash before ``record``) leaves the row ``sending``.
4. ``reconcile``: after ``sending.reconcile_after_minutes``, a ``sending`` row is looked up in
   its mailbox's Sent folder by Message-ID: found -> ``sent``; not found -> ``unconfirmed``
   for a teammate. It is never resent automatically.

Server text (SMTP replies, bounce details) is stored cleaned and clipped, never logged."""

import random
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Literal

import structlog
from sqlalchemy import ColumnElement, and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder import audit
from salesfinder.campaigns.service import ARCHIVE_STOP_REASON, persona_unconfirmed
from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, mx_domain, refresh_mx, stale_mx_domains
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    Conversation,
    DomainMx,
    EmailAddress,
    Mailbox,
    Message,
    Sequence,
)
from salesfinder.exclusions.sync import sync_health
from salesfinder.gate.checks import MX_OUT_OF_DATE, gate_message
from salesfinder.gate.prompts import instructions_for
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.mime import build_message, envelope_address, new_message_id
from salesfinder.mail.smtp import MailboxCredentials, SendResult, clean_detail
from salesfinder.outreach.locks import Locked, lock_message, lock_recipient
from salesfinder.outreach.stop import cancel_reply_drafts, may_have_gone, stop_sequence
from salesfinder.sending import policy
from salesfinder.sending.bounces import after_bounce, apply_bounce
from salesfinder.sending.mailboxes import (
    credentials,
    no_password,
    readiness_is_fresh,
    untested,
)
from salesfinder.sending.state import NOT_TURNED_ON, get_state
from salesfinder.sending.timezones import recipient_zones
from salesfinder.settings import Settings

__all__ = [
    "AUTH_FAILED",
    "CAMPAIGN_PAUSED",
    "DNS_NOT_READY",
    "HUBSPOT_STALE",
    "MAILBOX_OFF",
    "NOT_IN_SENT",
    "NO_MAILBOX",
    "OUTSIDE_HOURS",
    "ClaimResult",
    "SendOutcome",
    "SendSnapshot",
    "apply_sent",
    "claim",
    "deliver",
    "pause_line",
    "reconcile",
    "record",
    "send_pass",
]

log = structlog.get_logger(__name__)

# Hold reasons (the message stays approved; the mailbox shows the last one).
CAMPAIGN_PAUSED = "Campaign is paused."
CAMPAIGN_NOT_ACTIVE = "Campaign isn't active."
NO_MAILBOX = "No sending mailbox is set for this campaign."
MAILBOX_OFF = "Mailbox is turned off."
MAILBOX_OTHER_SIDE = "Mailbox is for the other side."
MAILBOX_BAD_DOMAIN = "Mailbox address has an invalid domain."
DNS_NOT_READY = (
    "Mailbox DNS (SPF, DKIM, DMARC, MX) isn't confirmed recently — check it on the Mailboxes page."
)
HUBSPOT_STALE = "HubSpot data isn't fresh — cold sending waits."
OUTSIDE_HOURS = "Outside the recipient's office hours."
AUTH_FAILED = "Mailbox sign-in failed — check its app password, then re-enable it."
NOT_IN_SENT = "Couldn't confirm it was sent: not found in the mailbox's Sent folder."
NO_PASSWORD_NOW = "The mailbox's password isn't set any more."  # noqa: S105
# Re-read just before SMTP: turned off, or its connection settings not tested (P6-V/W).
MAILBOX_OFF_NOW = "The mailbox was turned off or needs a connection test before it could send."
SPACING = "Waiting between sends."
MX_RETRY = "Couldn't confirm the recipient's mail server — will retry."
NOT_CHECKABLE = "Couldn't check the mailbox's Sent folder for a day — check whether it went."
# A ``sending`` row its mailbox couldn't be checked for this long becomes ``unconfirmed``.
STUCK_AFTER = timedelta(hours=24)
# A recipient domain DNS can't confirm: tried again after this, refused after MX_GIVE_UP.
MX_RETRY_AFTER = timedelta(minutes=30)
MX_GIVE_UP = timedelta(hours=24)
_MX_HOLD = "mx_unconfirmed"  # the ``hold`` of the audit row marking the start of the wait
# What each kind of message needs its sequence to be, to go out.
SEQUENCE_FOR_KIND = {"first": "active", "followup": "active", "reply": "replied"}
# A reply never goes out in a conversation a teammate took over or marked handled (P6-A).
CLOSED_CONVERSATION = ("taken_over", "handled")
CONVERSATION_CLOSED = {
    "taken_over": "A teammate took this conversation over.",
    "handled": "This conversation was marked handled.",
}
ERROR_LIMIT = 300  # messages.last_send_error, mailboxes.last_error / hold_reason


def pause_line(reason: str) -> str:
    """How a global pause reads as a hold reason and in the worker's output."""
    return reason if reason == NOT_TURNED_ON else f"Sending is paused: {reason}"


@dataclass(frozen=True)
class SendSnapshot:
    """What was in force when the message was claimed (``messages.send_snapshot``)."""

    address_status: str | None
    override_sendable: bool | None
    blocked_reason: str | None
    mx_state: str | None
    mx_checked_at: str | None
    gate_problems: list[str]
    mailbox_id: str
    mailbox_address: str
    persona_name: str
    zones: list[str]
    envelope_to: str
    campaign_status: str
    sending_paused: bool
    hubspot_state: str
    claimed_at: str


@dataclass(frozen=True)
class ClaimResult:
    kind: Literal["claimed", "held", "refused", "skipped"]
    message_id: uuid.UUID
    reason: str | None = None
    # A hold that applies to everything the mailbox would send next (the global pause, the
    # mailbox, its persona, spacing; for cold mail DNS, HubSpot and the ramp): the pass stops
    # trying its other candidates.
    mailbox_wide: bool = False


@dataclass(frozen=True)
class SendOutcome:
    sent: int = 0
    # Mailbox address -> why nothing went out from it this pass.
    held: dict[str, str] = field(default_factory=dict)
    refused: int = 0  # the gate refused at send time (back in the Outbox as gate_failed)
    failed: int = 0  # failed for good or bounced
    retried: int = 0  # back to approved, to try again later
    unconfirmed: int = 0  # reconcile couldn't find them in Sent
    confirmed: int = 0  # reconcile found them in Sent
    # The global pause's reason, when sending is paused and approved mail is waiting.
    paused: str | None = None


def _side(campaign: Campaign) -> Side:
    return "supplier" if campaign.side == "supplier" else "buyer"


def _error_text(result: SendResult) -> str:
    code = f"SMTP {result.code}: " if result.code else ""
    return clean_detail(f"{code}{result.detail or result.outcome}", ERROR_LIMIT)


# --------------------------------------------------------------------------- claim


@dataclass(frozen=True)
class _Claimed:
    result: ClaimResult
    commit: bool = False


def _held(message_id: uuid.UUID, reason: str, *, mailbox_wide: bool = False) -> _Claimed:
    return _Claimed(ClaimResult("held", message_id, reason, mailbox_wide))


def _skipped(message_id: uuid.UUID, reason: str) -> _Claimed:
    return _Claimed(ClaimResult("skipped", message_id, reason))


async def _live_address(
    db: AsyncSession, locked: Locked
) -> tuple[str | None, bool | None, str | None]:
    """(status, override_sendable, blocked_reason) of the sequence's address as stored now."""
    if locked.sequence.email_address_id is None:
        return None, None, None
    row = (
        await db.execute(
            select(
                EmailAddress.status, EmailAddress.override_sendable, EmailAddress.blocked_reason
            ).where(EmailAddress.id == locked.sequence.email_address_id)
        )
    ).one_or_none()
    return (row.status, row.override_sendable, row.blocked_reason) if row else (None, None, None)


async def _live_mx(db: AsyncSession, recipient: str) -> tuple[str | None, str | None]:
    name = mx_domain(recipient.rpartition("@")[2])
    if name is None:
        return None, None
    row = (
        await db.execute(select(DomainMx.state, DomainMx.checked_at).where(DomainMx.domain == name))
    ).one_or_none()
    if row is None:
        return None, None
    return row.state, row.checked_at.isoformat()


async def _wait_for_mx(db: AsyncSession, message: Message, *, now: datetime) -> bool:
    """Hold ``message`` for the recipient's MX: back off ``MX_RETRY_AFTER`` (so a dead domain
    doesn't take candidate slots every pass) and return True, unless it has waited longer than
    ``MX_GIVE_UP`` since its first such hold after its latest approval or claim (a wait that
    ended in a claim is over); then False (refuse).

    The start of the wait is durable: the first hold writes a ``message.held`` audit row
    (system actor, ``hold: "mx_unconfirmed"``, ``since``: the claim's clock)."""
    floors = [t for t in (message.approved_at, message.claimed_at) if t is not None]
    floor = max(floors) if floors else None
    started: datetime | None = None
    for details in (
        await db.scalars(
            select(AuditLog.details).where(
                AuditLog.action == audit.MESSAGE_HELD,
                AuditLog.target_type == "message",
                AuditLog.target_id == str(message.id),
            )
        )
    ).all():
        if not isinstance(details, dict) or details.get("hold") != _MX_HOLD:
            continue
        try:
            since = datetime.fromisoformat(str(details.get("since")))
        except ValueError:
            continue
        if floor is not None and since < floor:
            continue  # a wait before it was approved again, or one that ended in a claim
        started = since if started is None else min(started, since)
    if started is None:
        started = now
        await audit.record(
            db,
            action=audit.MESSAGE_HELD,
            actor_id=None,
            target_type="message",
            target_id=str(message.id),
            details={
                "sequence_id": str(message.sequence_id),
                "stage": "send",
                "hold": _MX_HOLD,
                "since": now.isoformat(),
            },
        )
    if now - started > MX_GIVE_UP:
        return False
    message.next_attempt_at = now + MX_RETRY_AFTER
    message.last_send_error = MX_RETRY
    await db.flush()
    return True


async def _gate(
    db: AsyncSession,
    locked: Locked,
    *,
    side: Side,
    facts: Facts,
    catalog: Catalog,
    config: AppConfig,
    now: datetime,
) -> list[str]:
    message = locked.message
    try:
        prompt_text = await instructions_for(
            db,
            message=message,
            campaign=locked.campaign,
            company=locked.company,
            person=locked.person,
            catalog=catalog,
            facts=facts,
            config=config,
        )
    except ValueError as exc:
        return [str(exc)]
    problems = await gate_message(
        db,
        subject=message.subject,
        body=message.body,
        step=message.step,
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
    if not problems:
        try:
            envelope_address(locked.sequence.recipient)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


async def _claim(
    db: AsyncSession,
    message_id: uuid.UUID,
    *,
    mailbox: uuid.UUID,
    settings: Settings,
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
    now: datetime,
    rng: random.Random,
) -> _Claimed:
    # Lock order: the sequence's messages by id -> the sequence -> the recipient -> the mailbox.
    locked = await lock_message(db, message_id)
    if locked is None:
        return _skipped(message_id, "Email not found.")
    message, sequence, campaign = locked.message, locked.sequence, locked.campaign
    await lock_recipient(db, sequence.recipient)
    box = (
        await db.execute(
            select(Mailbox)
            .where(Mailbox.id == mailbox)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    # 1. Still waiting to go: selected on status only, never on approved_at.
    if message.status != "approved":
        return _skipped(message_id, "No longer approved.")
    if message.next_attempt_at is not None and message.next_attempt_at > now:
        return _skipped(message_id, "Waiting to try again.")
    if campaign.status == "archived":
        cancelled = await stop_sequence(db, locked, reason=ARCHIVE_STOP_REASON)
        await audit.record(
            db,
            action=audit.SEQUENCE_STOPPED,
            actor_id=None,
            target_type="sequence",
            target_id=str(sequence.id),
            details={
                "campaign_id": str(campaign.id),
                "company_id": str(sequence.company_id),
                "reason": "archived",
                "cancelled": cancelled,
            },
        )
        return _Claimed(ClaimResult("skipped", message_id, ARCHIVE_STOP_REASON), commit=True)
    if sequence.status != SEQUENCE_FOR_KIND.get(message.kind):
        return _skipped(message_id, "Its sequence has stopped.")
    if message.kind == "reply" and message.written_by_id is None:
        # Ruling P6-A: a teammate took the conversation over (wrote from the mailbox) or
        # closed it; Claude's approved answer must not go out after that. Cancelled, not left
        # approved, so it never crowds out the mailbox's other candidates. Ruling P6-N: a
        # reply a teammate wrote themselves (``written_by_id``) still goes: it is the
        # takeover (Task 9's ``write_reply``).
        closed = await db.scalar(
            select(Conversation.status).where(
                Conversation.sequence_id == sequence.id,
                Conversation.status.in_(CLOSED_CONVERSATION),
            )
        )
        if closed is not None:
            message.status = "cancelled"
            await db.flush()
            await audit.record(
                db,
                action=audit.MESSAGE_CANCELLED,
                actor_id=None,
                target_type="message",
                target_id=str(message.id),
                details={
                    "sequence_id": str(sequence.id),
                    "campaign_id": str(campaign.id),
                    "reason": f"conversation_{closed}",
                },
            )
            return _Claimed(
                ClaimResult("skipped", message_id, CONVERSATION_CLOSED[closed]), commit=True
            )
    if box is None:
        return _skipped(message_id, "That mailbox no longer exists.")
    expected = sequence.mailbox_id or campaign.mailbox_id
    if expected is None:
        return _held(message_id, NO_MAILBOX)
    if expected != box.id:
        return _skipped(message_id, "It goes out from another mailbox now.")

    # 2. Campaign and the global pause. Pausing a campaign stops cold outreach only: answers
    # to people who wrote to us still go (Ruling P6-I).
    if campaign.status == "paused" and message.kind != "reply":
        return _held(message_id, CAMPAIGN_PAUSED)
    if campaign.status not in ("active", "paused"):
        return _held(message_id, CAMPAIGN_NOT_ACTIVE)
    state = await get_state(db)
    if state.paused:
        return _held(message_id, pause_line(state.reason), mailbox_wide=True)

    # 3. The mailbox, who it signs as, and its spacing (all under the mailbox row lock).
    if not box.enabled:
        return _held(message_id, MAILBOX_OFF, mailbox_wide=True)
    if credentials(box, settings, config) is None:
        return _held(message_id, no_password(box.secret_name), mailbox_wide=True)
    side = _side(campaign)
    if box.side != campaign.side:
        return _held(message_id, MAILBOX_OTHER_SIDE)
    persona = facts.personas.get(side)
    if persona is None or not persona.confirmed or not persona.name.strip():
        # A mailbox serves one side, so its persona is the same for every message.
        return _held(message_id, persona_unconfirmed(side), mailbox_wide=True)
    if box.next_send_at is not None and box.next_send_at > now:
        return _held(message_id, SPACING, mailbox_wide=True)

    # 4. Cold mail only: DNS readiness, HubSpot freshness, the warm-up ramp, office hours.
    # Replies to people who wrote to us are held by none of these (Ruling P6-E).
    zones = await recipient_zones(db, locked.company)
    health = await sync_health(
        db,
        now=now,
        token_configured=settings.hubspot_token is not None,
        config=config.hubspot,
    )
    if message.kind != "reply":
        # Candidates come replies first, so after a cold hold only cold mail is left to try.
        if not readiness_is_fresh(box, now=now, config=config):
            return _held(message_id, DNS_NOT_READY, mailbox_wide=True)
        if health.state != "fresh":
            return _held(message_id, HUBSPOT_STALE, mailbox_wide=True)
        sent = await policy.sent_today(db, box.id, now=now)
        hold = policy.ramp_hold(
            box, today=now.astimezone(UTC).date(), sent_today=sent, config=config
        )
        if hold is not None:
            return _held(message_id, hold, mailbox_wide=True)
        # Due dates use the first zone only; the send itself must suit every zone.
        if not policy.in_office_hours(now, zones, config):
            return _held(message_id, OUTSIDE_HOURS)

    # 5. The whole gate, on live state.
    problems = await _gate(
        db, locked, side=side, facts=facts, catalog=catalog, config=config, now=now
    )
    if problems == [MX_OUT_OF_DATE] and await _wait_for_mx(db, message, now=now):
        # DNS couldn't answer just now (the refresh before the claim failed): try again in a
        # while rather than send it back to the Outbox (refused after MX_GIVE_UP).
        return _Claimed(ClaimResult("held", message_id, MX_RETRY), commit=True)
    if problems:
        message.status = "gate_failed"
        message.gate_errors = problems
        message.send_attempts = 0
        message.next_attempt_at = None
        message.last_send_error = None  # e.g. MX_RETRY: the gate's problems say why now
        if not await may_have_gone(db, message):
            # Re-approved (maybe edited) content gets a fresh Message-ID. Never when an
            # earlier attempt may have reached them: the same id keeps a duplicate traceable.
            message.message_id_header = None
        if message.kind == "first":
            # Back to the Outbox, which shows first emails of sequences awaiting approval.
            sequence.status = "awaiting_approval"
        await db.flush()
        await audit.record(
            db,
            action=audit.MESSAGE_HELD,
            actor_id=None,
            target_type="message",
            target_id=str(message.id),
            details={
                "sequence_id": str(sequence.id),
                "campaign_id": str(campaign.id),
                "stage": "send",
                "problems": len(problems),
            },
        )
        return _Claimed(ClaimResult("refused", message_id, problems[0]), commit=True)

    # 6. Claimed: headers fixed now, kept for any later resend.
    if message.message_id_header is None:
        try:
            message.message_id_header = new_message_id(box.address)
        except ValueError:
            return _held(message_id, MAILBOX_BAD_DOMAIN, mailbox_wide=True)
    if message.kind == "followup":
        first = next((m for m in locked.siblings if m.step == 1), None)
        parent = first.message_id_header if first is not None else None
        message.in_reply_to = parent
        message.references_header = parent
    address_status, override, blocked = await _live_address(db, locked)
    mx_state, mx_checked_at = await _live_mx(db, sequence.recipient)
    snapshot = SendSnapshot(
        address_status=address_status,
        override_sendable=override,
        blocked_reason=blocked,
        mx_state=mx_state,
        mx_checked_at=mx_checked_at,
        gate_problems=[],
        mailbox_id=str(box.id),
        mailbox_address=box.address,
        persona_name=persona.name.strip(),
        zones=list(zones),
        envelope_to=envelope_address(sequence.recipient),
        campaign_status=campaign.status,
        sending_paused=state.paused,
        hubspot_state=health.state,
        claimed_at=now.isoformat(),
    )
    message.status = "sending"
    message.claimed_at = now
    message.mailbox_id = box.id
    message.last_send_error = None
    message.send_snapshot = asdict(snapshot)
    if sequence.mailbox_id is None:
        sequence.mailbox_id = box.id
    box.next_send_at = now + policy.next_spacing(rng, config)
    box.hold_reason = None
    await db.flush()
    return _Claimed(ClaimResult("claimed", message_id), commit=True)


async def claim(
    db_factory: async_sessionmaker[AsyncSession],
    message_id: uuid.UUID,
    *,
    mailbox: uuid.UUID,
    settings: Settings,
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
    now: datetime,
    rng: random.Random,
) -> ClaimResult:
    """Claim ``message_id`` to go out from the mailbox with id ``mailbox``, in one transaction
    (see the module docstring). Refresh the recipient domain's MX (``mx_for``) before calling
    this, outside any transaction.

    - ``skipped``: no longer approved (selected on status only), its sequence isn't ``active``
      (``replied`` for a reply), its campaign is archived (the sequence is stopped and the
      message cancelled), a Claude-drafted reply's conversation was taken over or marked handled
      (the reply is cancelled, audited ``message.cancelled``; a teammate-written reply still
      goes), or it now goes out from another mailbox.
    - ``held``: it stays approved and the reason says why (campaign paused, sending paused,
      mailbox off or without a password or for the other side, persona unconfirmed, and for
      cold mail only DNS readiness, HubSpot freshness, the warm-up ramp and office hours).
    - ``refused``: the gate found problems; the message is ``gate_failed`` with them stored
      (a first email's sequence goes back to ``awaiting_approval``, so the Outbox shows it).
    - ``claimed``: ``sending``, with ``claimed_at``, its Message-ID (generated once, kept on a
      resend), threading headers for a follow-up, the mailbox and a ``SendSnapshot``; the
      mailbox's ``next_send_at`` moves on by a random spacing and its hold reason clears."""
    async with db_factory() as db:
        out = await _claim(
            db,
            message_id,
            mailbox=mailbox,
            settings=settings,
            config=config,
            facts=facts,
            catalog=catalog,
            now=now,
            rng=rng,
        )
        if out.commit:
            await db.commit()
        else:
            await db.rollback()
    return out.result


# --------------------------------------------------------------------------- deliver


@dataclass(frozen=True)
class _Outgoing:
    creds: MailboxCredentials
    message: EmailMessage
    envelope_from: str
    envelope_to: str


async def deliver(
    db_factory: async_sessionmaker[AsyncSession],
    message_id: uuid.UUID,
    *,
    mail: MailFactory,
    settings: Settings,
    config: AppConfig,
    facts: Facts,
    now: datetime,
) -> SendResult:
    """Send a claimed (``sending``) message and record the outcome. The row is read in a short
    session that is closed before SMTP starts; a mailbox turned off or held untested since the
    claim gets no SMTP (a ``retry``); the MIME carries exactly the stored subject and
    body (only encoded) plus headers. ``ValueError`` if the message isn't ``sending``."""
    del facts  # the From name is the persona confirmed at claim time (the snapshot)
    async with db_factory() as db:
        message = await db.get(Message, message_id)
        if message is None or message.status != "sending":
            raise ValueError("That email isn't claimed for sending.")
        sequence = await db.get(Sequence, message.sequence_id)
        box = await db.get(Mailbox, message.mailbox_id) if message.mailbox_id else None
        assert sequence is not None  # noqa: S101 - messages cascade with their sequence
        creds = credentials(box, settings, config) if box is not None else None
        snapshot = message.send_snapshot or {}
        result: SendResult | None = None
        outgoing: _Outgoing | None = None
        if box is None or not box.enabled or untested(box):
            # Turned off (or its settings changed) since the claim: no SMTP. A retry puts it
            # back to approved; the next claim holds it until the mailbox is usable again.
            result = SendResult("retry", None, MAILBOX_OFF_NOW)
        elif creds is None:
            result = SendResult("retry", None, NO_PASSWORD_NOW)
        else:
            try:
                mime = build_message(
                    from_name=str(snapshot.get("persona_name") or ""),
                    from_address=box.address,
                    to_address=sequence.recipient,
                    subject=message.subject,
                    body=message.body,
                    message_id=message.message_id_header or "",
                    date=now,
                    in_reply_to=message.in_reply_to,
                    references=(message.references_header or "").split(),
                    unsubscribe_mailto=box.address,
                )
                outgoing = _Outgoing(
                    creds,
                    mime,
                    envelope_address(box.address),
                    envelope_address(sequence.recipient),
                )
            except ValueError as exc:
                # Our own validation text (no message content in it).
                result = SendResult("failed", None, f"Couldn't build the email: {exc}")
        await db.rollback()
    # No session is open from here until the result is recorded.
    if outgoing is not None:
        try:
            result = await mail.sender.send(
                outgoing.creds,
                outgoing.message,
                envelope_from=outgoing.envelope_from,
                envelope_to=outgoing.envelope_to,
            )
        except Exception as exc:
            # We can't tell whether it went: reconcile looks for it in Sent later.
            log.warning("send_crashed", message_id=str(message_id), error_type=type(exc).__name__)
            result = SendResult("ambiguous", None, type(exc).__name__)
    assert result is not None  # noqa: S101 - set on every path above
    async with db_factory() as db:
        await record(db, message_id, result, now=now, config=config)
    return result


# --------------------------------------------------------------------------- record


async def apply_sent(
    db: AsyncSession, locked: Locked, *, sent_at: datetime, config: AppConfig
) -> None:
    """Mark the locked message ``sent`` at ``sent_at`` and do what follows a send: a first
    email schedules its pending follow-up (``due_at``, ``writing.followup_business_days``
    after the send in the recipient's first zone); a follow-up completes an ``active``
    sequence; a reply a teammate wrote (``written_by_id``) takes the conversation over
    (Ruling P6-D). Audited ``message.sent`` (system actor; ``approved_by`` in the details).
    Flushes; the caller commits."""
    message, sequence = locked.message, locked.sequence
    message.status = "sent"
    message.sent_at = sent_at
    message.next_attempt_at = None
    message.last_send_error = None
    if message.kind == "first":
        follow = next((m for m in locked.siblings if m.step == 2 and m.status == "pending"), None)
        if follow is not None:
            zones = await recipient_zones(db, locked.company)
            follow.due_at = policy.followup_due_at(
                sent_at,
                zones,
                business_days=config.writing.followup_business_days,
                config=config,
            )
    elif message.kind == "followup" and sequence.status == "active":
        sequence.status = "completed"
    await db.flush()
    if message.kind == "reply" and message.written_by_id is not None:
        await _take_over(db, locked, at=sent_at)
    await audit.record(
        db,
        action=audit.MESSAGE_SENT,
        actor_id=None,
        target_type="message",
        target_id=str(message.id),
        details={
            "kind": message.kind,
            "sequence_id": str(sequence.id),
            "campaign_id": str(sequence.campaign_id),
            "mailbox_id": str(message.mailbox_id) if message.mailbox_id else None,
            "recipient": sequence.recipient,
            "approved_by": str(message.approved_by_id) if message.approved_by_id else None,
        },
    )


async def _take_over(db: AsyncSession, locked: Locked, *, at: datetime) -> None:
    """Ruling P6-D: a reply a teammate wrote themselves went out, so a human is now in the
    thread: the conversation is ``taken_over`` (automation stops for them) and Claude's pending
    drafts are cancelled. The conversation row is locked after the outreach locks the caller
    holds. Audited ``conversation.taken_over`` (actor: the teammate who wrote it)."""
    message, sequence = locked.message, locked.sequence
    cancelled = await cancel_reply_drafts(db, locked, claude_only=True)
    conv = (
        await db.execute(
            select(Conversation)
            .where(Conversation.sequence_id == sequence.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    was = conv.status if conv is not None else None
    if conv is None:
        mailbox_id = message.mailbox_id or sequence.mailbox_id
        if mailbox_id is None:  # never sent from a mailbox: unreachable after a send
            return
        conv = Conversation(sequence_id=sequence.id, mailbox_id=mailbox_id, status="taken_over")
        db.add(conv)
    conv.status = "taken_over"
    conv.needs_you_reason = None
    conv.taken_over_at = conv.taken_over_at or at
    await db.flush()
    if was != "taken_over":
        await audit.record(
            db,
            action=audit.CONVERSATION_TAKEN_OVER,
            actor_id=message.written_by_id,
            target_type="conversation",
            target_id=str(conv.id),
            details={
                "sequence_id": str(sequence.id),
                "reason": "teammate_reply",
                "message_id": str(message.id),
                "cancelled": cancelled,
            },
        )


def _retry_delay(attempts: int, config: AppConfig) -> timedelta:
    minutes = config.sending.retry_minutes or [5]
    return timedelta(minutes=minutes[min(attempts, len(minutes)) - 1])


async def _disable_mailbox(
    db: AsyncSession, mailbox_id: uuid.UUID | None, result: SendResult, now: datetime
) -> None:
    if mailbox_id is None:
        return
    box = (
        await db.execute(
            select(Mailbox)
            .where(Mailbox.id == mailbox_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if box is None:
        return
    box.last_error = _error_text(result)
    box.last_error_at = now
    was_enabled = box.enabled
    box.enabled = False
    if not untested(box):
        # An untested hold (settings changed meanwhile, P6-V/W) is kept: it is what stops
        # every login until a Test connection passes.
        box.hold_reason = AUTH_FAILED
    await db.flush()
    if was_enabled:
        await audit.record(
            db,
            action=audit.MAILBOX_DISABLED,
            actor_id=None,
            target_type="mailbox",
            target_id=str(box.id),
            details={"address": box.address, "reason": "sign_in_failed", "code": result.code},
        )


async def _record(
    db: AsyncSession,
    message_id: uuid.UUID,
    result: SendResult,
    *,
    now: datetime,
    config: AppConfig,
    sent_at: datetime | None = None,
) -> bool:
    locked = await lock_message(db, message_id)
    if locked is None or locked.message.status != "sending":
        await db.rollback()
        return False
    message = locked.message
    bounced_to: str | None = None
    tripped: str | None = None
    gave_up = False
    cancelled = False
    # Its sequence stopped (or was replied to, ...) while it was on the way: a message that
    # didn't go must not stay approved for a sequence it can no longer go out in.
    stopped = locked.sequence.status != SEQUENCE_FOR_KIND.get(message.kind)
    match result.outcome:
        case "sent":
            await apply_sent(db, locked, sent_at=sent_at or now, config=config)
        case "retry":
            message.send_attempts += 1
            message.last_send_error = _error_text(result)
            message.next_attempt_at = None
            if stopped:
                message.status = "cancelled"
                cancelled = True
            elif message.send_attempts >= config.sending.max_send_attempts:
                message.status = "failed"
                gave_up = True
            else:
                message.status = "approved"
                message.next_attempt_at = now + _retry_delay(message.send_attempts, config)
        case "rejected":
            recipient = locked.sequence.recipient
            await lock_recipient(db, recipient)
            message.last_send_error = _error_text(result)
            tripped = await apply_bounce(
                db, locked, recipient=recipient, detail=_error_text(result), now=now, config=config
            )
            bounced_to = recipient
        case "mailbox_error":
            # Not the message's fault: it waits for the mailbox (no attempt counted).
            message.status = "cancelled" if stopped else "approved"
            cancelled = stopped
            message.last_send_error = _error_text(result)
            await _disable_mailbox(db, message.mailbox_id, result, now)
        case "failed":
            message.status = "failed"
            message.next_attempt_at = None
            message.last_send_error = _error_text(result)
            gave_up = True
        case "ambiguous":
            # It may have gone: left ``sending`` for reconcile to settle from the Sent folder.
            message.last_send_error = _error_text(result)
    await db.flush()
    if cancelled:
        await audit.record(
            db,
            action=audit.MESSAGE_CANCELLED,
            actor_id=None,
            target_type="message",
            target_id=str(message.id),
            details={
                "sequence_id": str(locked.sequence.id),
                "campaign_id": str(locked.sequence.campaign_id),
                "reason": "sequence_moved_on",
                "outcome": result.outcome,
                "code": result.code,
            },
        )
    if gave_up:
        await audit.record(
            db,
            action=audit.MESSAGE_SEND_FAILED,
            actor_id=None,
            target_type="message",
            target_id=str(message.id),
            details={
                "sequence_id": str(locked.sequence.id),
                "campaign_id": str(locked.sequence.campaign_id),
                "outcome": result.outcome,
                "code": result.code,
                "attempts": message.send_attempts,
            },
        )
    if tripped is not None:
        await audit.record(
            db,
            action=audit.SENDING_AUTO_PAUSED,
            actor_id=None,
            target_type="app_setting",
            target_id="sending",
            details={"reason": tripped, "message_id": str(message.id)},
        )
    await db.commit()
    if bounced_to is not None:
        # The second half of the bounce, holding no outreach lock (sending.bounces).
        try:
            await after_bounce(db, recipient=bounced_to, now=now)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            log.warning(
                "after_bounce_failed", message_id=str(message_id), error_type=type(exc).__name__
            )
    return True


async def record(
    db: AsyncSession,
    message_id: uuid.UUID,
    result: SendResult,
    *,
    now: datetime,
    config: AppConfig,
    sent_at: datetime | None = None,
) -> None:
    """Settle a send's ``result`` under ``lock_message``; does nothing unless the message is
    still ``sending``. Owns its transactions (commits; the session may be reused after):

    - ``sent``: ``apply_sent`` (``sent_at`` defaults to ``now``; reconcile passes the claim
      time).
    - ``retry``: back to ``approved`` with ``send_attempts + 1`` and ``next_attempt_at`` per
      ``sending.retry_minutes``; at ``max_send_attempts`` it is ``failed``.
    - ``rejected`` (refused at RCPT): a hard bounce (``apply_bounce`` under the recipient lock;
      an auto-pause is audited), then ``after_bounce`` in a second transaction.
    - ``mailbox_error``: back to ``approved`` (no attempt counted); the mailbox is turned off
      with ``AUTH_FAILED`` and its ``last_error`` (audited ``mailbox.disabled``).
    - ``failed``: ``failed``. ``ambiguous``: left ``sending`` (reconcile decides).

    ``last_send_error`` keeps the server's code and text, cleaned and clipped (never logged)."""
    await _record(db, message_id, result, now=now, config=config, sent_at=sent_at)


# --------------------------------------------------------------------------- reconcile


async def _mark_unconfirmed(
    db_factory: async_sessionmaker[AsyncSession],
    message_id: uuid.UUID,
    *,
    why: Literal["not_in_sent", "not_checkable"],
) -> bool:
    """``sending`` -> ``unconfirmed`` for a teammate to settle; audited (system actor)."""
    async with db_factory() as db:
        locked = await lock_message(db, message_id)
        if locked is None or locked.message.status != "sending":
            await db.rollback()
            return False
        locked.message.status = "unconfirmed"
        locked.message.last_send_error = NOT_IN_SENT if why == "not_in_sent" else NOT_CHECKABLE
        await db.flush()
        await audit.record(
            db,
            action=audit.MESSAGE_UNCONFIRMED,
            actor_id=None,
            target_type="message",
            target_id=str(message_id),
            details={
                "sequence_id": str(locked.sequence.id),
                "campaign_id": str(locked.sequence.campaign_id),
                "mailbox_id": str(locked.message.mailbox_id) if locked.message.mailbox_id else None,
                "reason": why,
            },
        )
        await db.commit()
    return True


async def _give_up_unchecked(
    db_factory: async_sessionmaker[AsyncSession],
    items: list[tuple[uuid.UUID, str | None, datetime | None]],
    *,
    now: datetime,
) -> int:
    """Rows whose mailbox couldn't be checked: those claimed more than ``STUCK_AFTER`` ago
    become ``unconfirmed`` (never resent); the rest wait for the next pass."""
    count = 0
    for message_id, _header, claimed_at in items:
        if claimed_at is not None and claimed_at < now - STUCK_AFTER:
            count += await _mark_unconfirmed(db_factory, message_id, why="not_checkable")
    return count


async def reconcile(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    mail: MailFactory,
    settings: Settings,
    config: AppConfig,
    now: datetime,
) -> tuple[int, int]:
    """Settle ``sending`` rows claimed more than ``sending.reconcile_after_minutes`` ago by
    looking for their Message-ID in their mailbox's Sent folder (IMAP outside any
    transaction): found -> ``sent`` (at the claim time); not found -> ``unconfirmed``. A
    mailbox without a password or with untested connection settings (Ruling P6-V), a reader
    that can't open, or a lookup that errors leaves its rows for the next pass. Never resends.
    Returns (confirmed, unconfirmed)."""
    cutoff = now - timedelta(minutes=config.sending.reconcile_after_minutes)
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(
                    Message.id, Message.mailbox_id, Message.message_id_header, Message.claimed_at
                )
                .where(Message.status == "sending", Message.claimed_at < cutoff)
                .order_by(Message.claimed_at, Message.id)
            )
        ).all()
        boxes = {
            b.id: b
            for b in (
                await db.scalars(
                    select(Mailbox).where(Mailbox.id.in_({r.mailbox_id for r in rows}))
                )
            ).all()
        }
        # An untested mailbox (Ruling P6-V) can't be checked yet: its rows wait.
        logins = {
            box_id: None if untested(b) else credentials(b, settings, config)
            for box_id, b in boxes.items()
        }
        await db.rollback()
    by_box: dict[uuid.UUID | None, list[tuple[uuid.UUID, str | None, datetime | None]]] = {}
    for r in rows:
        by_box.setdefault(r.mailbox_id, []).append((r.id, r.message_id_header, r.claimed_at))
    confirmed = unconfirmed = 0
    for box_id, items in by_box.items():
        creds = logins.get(box_id) if box_id is not None else None
        if creds is None:
            unconfirmed += await _give_up_unchecked(db_factory, items, now=now)
            continue
        try:
            reader = await mail.open_reader(creds)
        except Exception as exc:
            log.warning(
                "reconcile_open_failed", mailbox_id=str(box_id), error_type=type(exc).__name__
            )
            unconfirmed += await _give_up_unchecked(db_factory, items, now=now)
            continue
        settled: set[uuid.UUID] = set()
        try:
            folder = creds.sent_folder or await reader.special_folder("sent")
            if folder is None:
                log.warning("reconcile_no_sent_folder", mailbox_id=str(box_id))
                unconfirmed += await _give_up_unchecked(db_factory, items, now=now)
                continue
            for message_id, header, claimed_at in items:
                found = False
                if header is not None:
                    found = await reader.has_message_id(folder, header)
                settled.add(message_id)
                if found:
                    async with db_factory() as db:
                        if await _record(
                            db,
                            message_id,
                            SendResult("sent", 250, "Found in the Sent folder."),
                            now=now,
                            config=config,
                            sent_at=claimed_at,
                        ):
                            confirmed += 1
                elif await _mark_unconfirmed(db_factory, message_id, why="not_in_sent"):
                    unconfirmed += 1
        except Exception as exc:
            # Left ``sending`` for the next pass, unless stuck for a day already.
            log.warning("reconcile_failed", mailbox_id=str(box_id), error_type=type(exc).__name__)
            unconfirmed += await _give_up_unchecked(
                db_factory, [i for i in items if i[0] not in settled], now=now
            )
        finally:
            try:
                await reader.aclose()
            except Exception as exc:
                log.warning("reconcile_close_failed", error_type=type(exc).__name__)
    if confirmed or unconfirmed:
        log.info("reconciled", confirmed=confirmed, unconfirmed=unconfirmed)
    return confirmed, unconfirmed


# --------------------------------------------------------------------------- the pass


_KIND_ORDER = case((Message.kind == "reply", 0), (Message.kind == "followup", 1), else_=2)


def _ready_to_go(now: datetime) -> list[ColumnElement[bool]]:
    """Approved, due for a try, in an active campaign (a paused one too for a reply), and its
    sequence in the status its kind needs (``SEQUENCE_FOR_KIND``), for a query joining Message,
    Sequence and Campaign. A paused campaign's cold mail (or a stopped sequence's) is never a
    candidate, so it can't crowd out the mail that may go."""
    return [
        Message.status == "approved",
        (Message.next_attempt_at.is_(None)) | (Message.next_attempt_at <= now),
        # A paused campaign still answers people who wrote to us (Ruling P6-I).
        or_(
            Campaign.status == "active",
            and_(Message.kind == "reply", Campaign.status == "paused"),
        ),
        or_(
            *(
                and_(Message.kind == kind, Sequence.status == status)
                for kind, status in SEQUENCE_FOR_KIND.items()
            )
        ),
    ]


async def _candidates(
    db: AsyncSession, mailbox_id: uuid.UUID, *, now: datetime, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """Approved messages for this mailbox (through the sequence, else the campaign) that may
    go now (``_ready_to_go``): replies, then follow-ups, then first emails, oldest approval
    first. Read without locks; ``claim`` re-checks everything."""
    rows = await db.execute(
        select(Message.id, Sequence.recipient)
        .join(Sequence, Sequence.id == Message.sequence_id)
        .join(Campaign, Campaign.id == Sequence.campaign_id)
        .where(
            *_ready_to_go(now),
            (Sequence.mailbox_id == mailbox_id)
            | (Sequence.mailbox_id.is_(None) & (Campaign.mailbox_id == mailbox_id)),
        )
        .order_by(_KIND_ORDER, Message.approved_at.asc().nulls_last(), Message.id)
        .limit(limit)
    )
    return [(r.id, r.recipient) for r in rows.all()]


async def _set_hold(
    db_factory: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID, reason: str | None
) -> None:
    """Show why the mailbox sent nothing (None clears it). A mailbox turned off meanwhile keeps
    its own reason."""
    async with db_factory() as db:
        box = (
            await db.execute(
                select(Mailbox)
                .where(Mailbox.id == mailbox_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if box is not None and box.enabled and box.hold_reason != reason:
            box.hold_reason = clean_detail(reason, ERROR_LIMIT) if reason else None
            await db.commit()
        else:
            await db.rollback()


async def _refresh_recipient_mx(
    db_factory: async_sessionmaker[AsyncSession],
    mx_checker: MxChecker | None,
    recipient: str,
    *,
    now: datetime,
    config: AppConfig,
) -> None:
    """Look the recipient's MX up again if it is stale, outside any transaction."""
    if mx_checker is None:
        return
    async with db_factory() as db:
        stale = await stale_mx_domains(db, [recipient], now=now, config=config.contacts)
    if stale:
        await refresh_mx(db_factory, mx_checker, stale, now=now, config=config.contacts)


async def _waiting(db: AsyncSession, now: datetime) -> bool:
    found = await db.scalar(
        select(Message.id)
        .join(Sequence, Sequence.id == Message.sequence_id)
        .join(Campaign, Campaign.id == Sequence.campaign_id)
        .where(*_ready_to_go(now))
        .limit(1)
    )
    return found is not None


async def send_pass(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    mail: MailFactory,
    settings: Settings,
    config: AppConfig,
    facts: Facts,
    catalog: Catalog,
    now: Callable[[], datetime],
    mx_checker: MxChecker | None,
    rng: random.Random,
) -> SendOutcome:
    """One sending pass: ``reconcile`` first, then for each enabled mailbox whose
    ``next_send_at`` has come, try its candidates in order (``claim``, after a stale-MX
    refresh) until one is claimed, and ``deliver`` it: **at most one send per mailbox per
    pass**. Candidates are only mail that may go (``_ready_to_go``), and a mailbox-wide hold
    stops the mailbox's turn. The mailbox's ``hold_reason`` ends as this pass's last hold (also
    listed in ``held``), or cleared when nothing was held. With sending paused, nothing is
    claimed and ``paused`` carries the reason (only when approved mail is waiting)."""
    confirmed, unconfirmed = await reconcile(
        db_factory, mail=mail, settings=settings, config=config, now=now()
    )
    tally = {"sent": 0, "refused": 0, "failed": 0, "retried": 0}
    held: dict[str, str] = {}
    async with db_factory() as db:
        state = await get_state(db)
        waiting = await _waiting(db, now())
        current = now()
        boxes = [
            (b.id, b.address)
            for b in (
                await db.scalars(
                    select(Mailbox)
                    .where(
                        Mailbox.enabled,
                        Mailbox.next_send_at.is_(None) | (Mailbox.next_send_at <= current),
                    )
                    .order_by(Mailbox.address)
                )
            ).all()
        ]
        await db.rollback()
    if state.paused:
        return SendOutcome(
            confirmed=confirmed,
            unconfirmed=unconfirmed,
            paused=state.reason if waiting else None,
        )
    for box_id, address in boxes:
        async with db_factory() as db:
            candidates = await _candidates(
                db, box_id, now=now(), limit=config.sending.candidates_per_pass
            )
        if not candidates:
            await _set_hold(db_factory, box_id, None)
            continue
        last_hold: str | None = None
        claimed: uuid.UUID | None = None
        for message_id, recipient in candidates:
            await _refresh_recipient_mx(db_factory, mx_checker, recipient, now=now(), config=config)
            result = await claim(
                db_factory,
                message_id,
                mailbox=box_id,
                settings=settings,
                config=config,
                facts=facts,
                catalog=catalog,
                now=now(),
                rng=rng,
            )
            if result.kind == "claimed":
                claimed = message_id
                break
            if result.kind == "held":
                last_hold = result.reason
                if result.mailbox_wide:
                    break  # the same hold applies to every other candidate
            elif result.kind == "refused":
                tally["refused"] += 1
        if claimed is None:
            # The mailbox shows this pass's last hold, or nothing when nothing was held.
            if last_hold is not None:
                held[address] = last_hold
            await _set_hold(db_factory, box_id, last_hold)
            continue
        sent = await deliver(
            db_factory,
            claimed,
            mail=mail,
            settings=settings,
            config=config,
            facts=facts,
            now=now(),
        )
        async with db_factory() as db:
            settled = await db.scalar(select(Message.status).where(Message.id == claimed))
        # Counted by where the message ended up (a last retry ends ``failed``).
        if settled == "sent":
            tally["sent"] += 1
        elif settled in ("failed", "bounced"):
            tally["failed"] += 1
        elif sent.outcome == "mailbox_error":
            held[address] = AUTH_FAILED
        elif settled == "approved":
            tally["retried"] += 1
    if any(tally.values()) or held:
        log.info("send_pass", held=len(held), **tally)
    return SendOutcome(
        sent=tally["sent"],
        held=held,
        refused=tally["refused"],
        failed=tally["failed"],
        retried=tally["retried"],
        unconfirmed=unconfirmed,
        confirmed=confirmed,
    )
