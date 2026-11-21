"""Read each mailbox's INBOX and Sent folder and act on what arrived.

- INBOX, in this order: a definite 5.x.x bounce for one of our recipients stops the sequence,
  suppresses the address and may trip the kill switch (``sending.bounces``); a soft bounce is
  only recorded; a bounce we can't read is recorded and, when matched, flags the conversation
  "needs you" (nothing is suppressed). Other mail is matched to a sequence
  (``replies.match.match_inbound``). An unsubscribe (Ruling P6-M: checked before the
  auto-reply test) suppresses the sender and the sequence's recipient at once and stops
  everything; unmatched, it still suppresses its sender (Ruling P6-L). Other unmatched mail is
  counted and not stored. An auto-reply is set aside (the sequence continues). Anything else
  is a reply: the sequence is ``replied``, its unsent follow-up cancelled, and its
  conversation opened.
- Sent: mail a teammate wrote from the mailbox (``match_sent``; our own sends and automatic
  messages are passed over) takes the conversation over: automation stops for that person.

Every mailbox whose password is set is read, enabled or not (Ruling P6-J), unless its connection
settings changed and haven't been tested since (Ruling P6-V). Untrusted values
are sanitized before they reach a query (Ruling P6-K): ids and addresses with control
characters are dropped, and a mail that still fails three polls in a row is stored as an
``unreadable`` placeholder so one bad mail can't hold back the opt-outs behind it.

IMAP runs outside any transaction. Each mail gets its own transaction, which takes the
outreach locks (``outreach.locks``: the sequence's messages, the sequence, the recipient)
before anything that refers to the mailbox row, and moves the folder's cursor in the same
transaction, so a crash re-reads at most the mail in flight and never stores it twice.
Bounce bookkeeping that takes company locks (``after_bounce``, ``refresh_blocks``) runs after
the commit, holding no outreach lock.

Untrusted text (subjects, bodies, addresses, From names, server text) is stored for the
dashboard and never logged: logs carry counts, ids and exception types only."""

import dataclasses
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder import audit
from salesfinder.config import AppConfig
from salesfinder.contacts.service import refresh_blocks
from salesfinder.db.models import (
    Conversation,
    Mailbox,
    MailboxFolder,
    MailboxMessage,
    Message,
    Sequence,
)
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.imap import MailReader
from salesfinder.mail.parse import (
    MAX_YEAR,
    MIN_YEAR,
    Bounce,
    ParsedMail,
    bounce_of,
    is_auto_reply,
    is_unsubscribe,
    parse_mail,
)
from salesfinder.outreach.locks import Locked, lock_message, lock_recipient
from salesfinder.outreach.stop import cancel_reply_drafts, reached_them, stop_sequence
from salesfinder.replies.match import (
    SENT_STATUSES,
    Match,
    is_ours,
    match_by_headers,
    match_inbound,
    match_sent,
    sent_to,
)
from salesfinder.sending.bounces import after_bounce, apply_bounce
from salesfinder.sending.mailboxes import credentials, untested
from salesfinder.settings import Settings
from salesfinder.suppressions.service import suppress

__all__ = [
    "DIFFERENT_ADDRESS",
    "NO_PASSWORD",
    "NO_SENT_FOLDER",
    "POSSIBLE_BOUNCE",
    "REPLIED",
    "TAKEOVER",
    "UNSUBSCRIBED",
    "PollOutcome",
    "Unsubscribed",
    "audit_unsubscribe",
    "poll_mailbox",
    "poll_mailboxes",
    "read_error",
    "unsubscribe_locked",
]

log = structlog.get_logger(__name__)

Folder = Literal["inbox", "sent"]
FOLDERS: tuple[Folder, ...] = ("inbox", "sent")  # bounces and unsubscribes first
INBOX = "INBOX"

REPLIED = "Replied"
UNSUBSCRIBED = "Unsubscribed"
TAKEOVER = "A teammate wrote to them from the mailbox"
DIFFERENT_ADDRESS = "Reply came from a different address — answer from the mailbox"
POSSIBLE_BOUNCE = "Possible bounce we couldn't read"
UNSUBSCRIBE_DETAIL = "Asked by email to unsubscribe"
NO_SENT_FOLDER = "Couldn't find the mailbox's Sent folder."
NO_PASSWORD = "The mailbox's password isn't set."  # noqa: S105
UNREADABLE = "Couldn't read a mail — check the mailbox"
# The folder's last_error when even the placeholder couldn't be stored. It stays until a
# teammate has seen it (later clean polls don't clear it; Task 10 shows it).
SKIPPED = "Skipped a mail we couldn't read"
# A mail that fails this many polls in a row is stored as ``unreadable`` and passed over.
MAX_MAIL_FAILURES = 3
# Control characters and lone surrogates: Postgres refuses NUL, and a surrogate can't be
# encoded as UTF-8 at all (either fails the whole transaction).
_CONTROL = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_BODY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
ERROR_LIMIT = 300  # mailbox_folders.last_error
ID_LIMIT = 255  # mailbox_messages.message_id_header / in_reply_to
ADDRESS_LIMIT = 320
# A rescan after a UIDVALIDITY change (or the first read) reaches back this far before the
# last good poll.
RESCAN_OVERLAP = timedelta(days=1)
# At most this many batches (``mail.max_messages_per_poll`` each) per folder per poll.
MAX_BATCHES = 5


def read_error(exc: BaseException) -> str:
    """The folder's ``last_error`` for a reader failure: the exception type only (its message
    may carry server text)."""
    return f"Couldn't read the mailbox: {type(exc).__name__}"


def _process_error(exc: BaseException) -> str:
    return f"Couldn't process a mail: {type(exc).__name__}"


@dataclass(frozen=True)
class PollOutcome:
    replies: int = 0
    auto_replies: int = 0
    bounces: int = 0  # hard bounces acted on
    unclear_bounces: int = 0
    unsubscribes: int = 0
    takeovers: int = 0
    unmatched: int = 0
    # The first error (a folder couldn't be read, or a mail couldn't be processed).
    error: str | None = None
    # The kill switch's reason when a bounce read here paused sending.
    paused: str | None = None
    # Not read at all: its connection settings haven't passed a test (Rulings P6-V/W).
    untested: bool = False


# --------------------------------------------------------------------------- one mail


@dataclass(frozen=True)
class _Ctx:
    """What every mail of one folder batch shares."""

    mailbox_id: uuid.UUID
    mailbox_address: str
    folder: Folder
    name: str
    uidvalidity: int
    known_recipients: frozenset[str]
    now: datetime
    config: AppConfig


@dataclass(frozen=True)
class _Ingested:
    # The PollOutcome counter to bump (None: a duplicate, or our own mail).
    counted: str | None
    bounced_to: str | None = None  # run after_bounce for it after the commit
    refresh: tuple[str, ...] = ()  # refresh_blocks for these after the commit
    paused: str | None = None


def _id(value: str | None) -> str | None:
    """A message id we can store and query: bounded, no control characters (a NUL would make
    Postgres refuse the whole transaction)."""
    if value is None or len(value) > ID_LIMIT or _CONTROL.search(value):
        return None
    return value


def _address(value: str | None) -> str | None:
    if value is None or len(value) > ADDRESS_LIMIT or _CONTROL.search(value):
        return None
    return value


def _clean(parsed: ParsedMail) -> ParsedMail:
    """Defence in depth behind ``parse_mail``'s own sanitizing: nothing with a control
    character reaches a query parameter, and a date outside the usable range counts as
    missing."""
    sender = _address(parsed.from_address)
    date = parsed.date
    if date is not None and not MIN_YEAR <= date.year <= MAX_YEAR:
        date = None
    return dataclasses.replace(
        parsed,
        message_id=_id(parsed.message_id),
        in_reply_to=[i for i in parsed.in_reply_to if _id(i)],
        references=[i for i in parsed.references if _id(i)],
        from_address=sender,
        from_local=sender.rpartition("@")[0] if sender else "",
        to_addresses=[a for a in parsed.to_addresses if _address(a)],
        cc_addresses=[a for a in parsed.cc_addresses if _address(a)],
        subject=" ".join(_CONTROL.sub(" ", parsed.subject).split())[:300],
        date=date,
        text=_BODY_CONTROL.sub("", parsed.text),
        new_text=_BODY_CONTROL.sub("", parsed.new_text),
    )


def _ours(ctx: _Ctx) -> tuple[str, ...]:
    """Lines of our own emails that quote opt-out words: never their unsubscribe."""
    return (ctx.config.writing.opt_out_line,)


def _received_at(parsed: ParsedMail, now: datetime) -> datetime:
    return min(parsed.date, now) if parsed.date is not None else now


def _recipients(parsed: ParsedMail) -> list[str]:
    both = [*parsed.to_addresses, *parsed.cc_addresses]
    return [a for a in dict.fromkeys(both) if _address(a)][:100]


async def _seen(db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail) -> bool:
    same_uid = await db.scalar(
        select(MailboxMessage.id).where(
            MailboxMessage.mailbox_id == ctx.mailbox_id,
            MailboxMessage.folder == ctx.folder,
            MailboxMessage.uidvalidity == ctx.uidvalidity,
            MailboxMessage.uid == uid,
        )
    )
    if same_uid is not None:
        return True
    # Always by Message-ID too: after a UIDVALIDITY change the same mail comes back under new
    # UIDs, in any batch of the re-read.
    if parsed.message_id is None:
        return False
    same_id = await db.scalar(
        select(MailboxMessage.id)
        .where(
            MailboxMessage.mailbox_id == ctx.mailbox_id,
            MailboxMessage.folder == ctx.folder,
            MailboxMessage.message_id_header == parsed.message_id,
        )
        .limit(1)
    )
    return same_id is not None


async def _advance(db: AsyncSession, ctx: _Ctx, cursor_uid: int) -> None:
    """Move the folder's cursor past this mail (in the mail's own transaction). The folder's
    ``last_error`` is left to ``_read_folder``, which clears it after a clean read."""
    await db.execute(
        update(MailboxFolder)
        .where(MailboxFolder.mailbox_id == ctx.mailbox_id, MailboxFolder.folder == ctx.folder)
        .values(
            name=ctx.name,
            uidvalidity=ctx.uidvalidity,
            last_uid=cursor_uid,
            polled_at=ctx.now,
            failed_uid=None,
            failures=0,
        )
    )


async def _lock(db: AsyncSession, match: Match) -> Locked | None:
    """The outreach locks for a matched sequence: its messages and the sequence (through the
    email it refers to, else its lowest message id), then its recipient."""
    message_id = match.message_id or await db.scalar(
        select(Message.id).where(Message.sequence_id == match.sequence_id).order_by(Message.id)
    )
    if message_id is None:
        return None
    locked = await lock_message(db, message_id)
    if locked is None or locked.sequence.id != match.sequence_id:
        return None
    await lock_recipient(db, locked.sequence.recipient)
    return locked


async def _conversation(db: AsyncSession, locked: Locked) -> Conversation | None:
    """The sequence's conversation, row-locked (after the outreach locks)."""
    return (
        await db.execute(
            select(Conversation)
            .where(Conversation.sequence_id == locked.sequence.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _new_conversation(
    db: AsyncSession, ctx: _Ctx, locked: Locked, *, status: str
) -> Conversation:
    conv = Conversation(
        sequence_id=locked.sequence.id,
        mailbox_id=locked.sequence.mailbox_id or ctx.mailbox_id,
        status=status,
    )
    db.add(conv)
    await db.flush()
    return conv


def _bump_inbound(conv: Conversation, at: datetime) -> None:
    if conv.last_inbound_at is None or conv.last_inbound_at < at:
        conv.last_inbound_at = at


async def _store(
    db: AsyncSession,
    ctx: _Ctx,
    uid: int,
    parsed: ParsedMail,
    *,
    kind: str,
    match: Match | None = None,
    message_id: uuid.UUID | None = None,
    conversation: Conversation | None = None,
    body: bool = False,
) -> MailboxMessage:
    row = MailboxMessage(
        mailbox_id=ctx.mailbox_id,
        folder=ctx.folder,
        uidvalidity=ctx.uidvalidity,
        uid=uid,
        message_id_header=_id(parsed.message_id),
        in_reply_to=next((i for i in parsed.in_reply_to if _id(i)), None),
        references_header=" ".join(i for i in parsed.references if _id(i)) or None,
        from_address=_address(parsed.from_address),
        to_addresses=_recipients(parsed),
        subject=parsed.subject,
        received_at=_received_at(parsed, ctx.now),
        kind=kind,
        matched_by=match.by if match is not None else None,
        sequence_id=match.sequence_id if match is not None else None,
        conversation_id=conversation.id if conversation is not None else None,
        message_id=message_id or (match.message_id if match is not None else None),
        body_text=parsed.new_text if body else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _audit_stop(
    db: AsyncSession, locked: Locked, *, reason: str, cancelled: int, row: MailboxMessage | None
) -> None:
    await audit.record(
        db,
        action=audit.SEQUENCE_STOPPED,
        actor_id=None,
        target_type="sequence",
        target_id=str(locked.sequence.id),
        details={
            "campaign_id": str(locked.sequence.campaign_id),
            "company_id": str(locked.sequence.company_id),
            "reason": reason,
            "cancelled": cancelled,
            "mailbox_message_id": str(row.id) if row is not None else None,
        },
    )


async def _audit_conversation(
    db: AsyncSession,
    action: str,
    conv: Conversation,
    *,
    reason: str,
    row: MailboxMessage | None,
) -> None:
    await audit.record(
        db,
        action=action,
        actor_id=None,
        target_type="conversation",
        target_id=str(conv.id),
        details={
            "sequence_id": str(conv.sequence_id),
            "reason": reason,
            "mailbox_message_id": str(row.id) if row is not None else None,
        },
    )


@dataclass(frozen=True)
class _Kept:
    """``_still_taken_over``'s answer: automation stays away (``kept``), and whether the
    backstop just rewrote an open status back to ``taken_over`` (``rewritten``: the caller
    audits ``conversation.taken_over``, reason ``backstop``, once the mail's row is stored)."""

    kept: bool
    rewritten: bool = False


def _still_taken_over(conv: Conversation) -> _Kept:
    """Ruling P6-Q: a conversation a teammate took over stays out of automation's hands. One
    with ``taken_over_at`` that is somehow open again (``open``/``needs_you``) is put back to
    ``taken_over`` (the backstop); one that was later closed (``handled``, e.g. by an
    unsubscribe) stays handled: not reopened, not flagged, its ``handled_at`` kept."""
    if conv.status == "taken_over":
        return _Kept(True)
    if conv.taken_over_at is None:
        return _Kept(False)
    if conv.status in ("open", "needs_you"):
        conv.status = "taken_over"
        conv.needs_you_reason = None
        return _Kept(True, rewritten=True)
    return _Kept(True)  # handled stays handled


async def _audit_backstop(db: AsyncSession, conv: Conversation, row: MailboxMessage | None) -> None:
    await _audit_conversation(db, audit.CONVERSATION_TAKEN_OVER, conv, reason="backstop", row=row)


def _flag(conv: Conversation, reason: str) -> tuple[bool, bool]:
    """Flag the conversation "needs you" with ``reason`` (one taken over stays out of it:
    ``_still_taken_over``). Returns ``(flagged, rewritten)``: ``flagged`` when that changed it
    (it wasn't already flagged for this reason; the caller audits ``conversation.needs_you``),
    ``rewritten`` when the takeover backstop fired (the caller audits that)."""
    kept = _still_taken_over(conv)
    if kept.kept:
        return False, kept.rewritten
    changed = (conv.status, conv.needs_you_reason) != ("needs_you", reason)
    conv.status = "needs_you"
    conv.needs_you_reason = reason
    return changed, False


# ----------------------------------------------------------------------- bounces


def _diagnostic(parsed: ParsedMail, recipient: str) -> str | None:
    if parsed.report is None:
        return None
    return next(
        (r.diagnostic for r in parsed.report.recipients if r.address == recipient and r.diagnostic),
        None,
    )


async def _bounced(db: AsyncSession, ctx: _Ctx, parsed: ParsedMail, bounce: Bounce) -> Match | None:
    """The email of ours a bounce is about: its quoted original Message-ID, else the DSN's
    threading headers, else (with a recipient) our latest email to that recipient from this
    mailbox. A header match to another recipient than the bounced one isn't trusted."""

    async def fits(message_id: uuid.UUID) -> bool:
        if bounce.recipient is None:
            return True
        recipient = await db.scalar(
            select(Sequence.recipient)
            .join(Message, Message.sequence_id == Sequence.id)
            .where(Message.id == message_id)
        )
        return recipient == bounce.recipient

    if bounce.original_message_id is not None:
        row = (
            await db.execute(
                select(Message.id, Message.sequence_id).where(
                    Message.message_id_header == bounce.original_message_id
                )
            )
        ).first()
        if row is not None and await fits(row.id):
            return Match(row.sequence_id, row.id, "header")
    by_headers = await match_by_headers(db, parsed)
    if by_headers is not None and by_headers.message_id is not None:
        if await fits(by_headers.message_id):
            return by_headers
    if bounce.recipient is not None:
        latest = await sent_to(db, ctx.mailbox_id, bounce.recipient)
        if latest is not None:
            return Match(latest[0], latest[1], "address")
    return None


async def _hard_bounce(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail, bounce: Bounce
) -> _Ingested:
    recipient = bounce.recipient
    assert recipient is not None  # noqa: S101 - a hard bounce always names its recipient
    detail = _diagnostic(parsed, recipient) or (
        f"Bounced: {bounce.status}" if bounce.status else ""
    )
    match = await _bounced(db, ctx, parsed, bounce)
    locked = await _lock(db, match) if match is not None else None
    if match is None or locked is None:
        # One of our recipients, but the email is gone (its campaign deleted): still never
        # mail the address again.
        await lock_recipient(db, recipient)
        try:
            await suppress(
                db, recipient, reason="bounced", detail=detail or "Bounced", refresh=False
            )
        except ValueError:
            log.warning("bounce_unsuppressible", mailbox_id=str(ctx.mailbox_id), uid=uid)
            return _Ingested("bounces")
        await _store(db, ctx, uid, parsed, kind="bounce")
        return _Ingested("bounces", refresh=(recipient,))
    tripped = await apply_bounce(
        db, locked, recipient=recipient, detail=detail, now=ctx.now, config=ctx.config
    )
    conv = await _conversation(db, locked)
    row = await _store(
        db,
        ctx,
        uid,
        parsed,
        kind="bounce",
        match=match,
        message_id=locked.message.id,
        conversation=conv,
    )
    if tripped is not None:
        await audit.record(
            db,
            action=audit.SENDING_AUTO_PAUSED,
            actor_id=None,
            target_type="app_setting",
            target_id="sending",
            details={
                "reason": tripped,
                "message_id": str(locked.message.id),
                "mailbox_message_id": str(row.id),
            },
        )
    return _Ingested("bounces", bounced_to=recipient, paused=tripped)


async def _other_bounce(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail, bounce: Bounce
) -> _Ingested:
    """Soft (recorded, nothing changes) or unclear (recorded; when matched, the conversation
    needs a teammate). Never suppresses."""
    kind = "soft_bounce" if bounce.kind == "soft" else "unclear_bounce"
    counted = None if bounce.kind == "soft" else "unclear_bounces"
    match = await _bounced(db, ctx, parsed, bounce)
    locked = await _lock(db, match) if match is not None else None
    if match is None or locked is None:
        await _store(db, ctx, uid, parsed, kind=kind)
        return _Ingested(counted)
    conv = await _conversation(db, locked)
    flagged = rewritten = False
    if bounce.kind == "unclear":
        if conv is None:
            conv = await _new_conversation(db, ctx, locked, status="open")
        flagged, rewritten = _flag(conv, POSSIBLE_BOUNCE)
        _bump_inbound(conv, _received_at(parsed, ctx.now))
        await db.flush()
    row = await _store(db, ctx, uid, parsed, kind=kind, match=match, conversation=conv)
    if rewritten and conv is not None:
        await _audit_backstop(db, conv, row)
    if flagged and conv is not None:
        await _audit_conversation(
            db, audit.CONVERSATION_NEEDS_YOU, conv, reason="possible_bounce", row=row
        )
    return _Ingested(counted)


# ----------------------------------------------------------------------- INBOX


@dataclass(frozen=True)
class Unsubscribed:
    """What ``unsubscribe_locked`` did: the addresses suppressed (``refresh_blocks`` each after
    the commit), how many unsent messages it cancelled, and the (now handled) conversation."""

    suppressed: tuple[str, ...]
    cancelled: int
    conversation: Conversation


async def unsubscribe_locked(
    db: AsyncSession,
    locked: Locked,
    *,
    from_address: str | None,
    message_id: uuid.UUID | None,
    mailbox_id: uuid.UUID,
    now: datetime,
    received_at: datetime,
) -> Unsubscribed:
    """The unsubscribe actions, under the caller's outreach locks (``lock_message`` then
    ``lock_recipient``): suppress the sender and the sequence's recipient (a colleague in the
    thread may ask for both; ``refresh=False``), stop the sequence ("Unsubscribed"), cancel
    reply drafts, and close the conversation (``handled``, intent ``unsubscribe``; created on
    ``mailbox_id`` if the sequence has none), locked after the outreach locks. Flushes; the
    caller stores its row, audits (``audit_unsubscribe``), commits, then refreshes blocks.

    Unavoidable: a follow-up the sender already claimed (``sending``) when the unsubscribe
    arrives is on the wire and is left alone; that one email may still go out. Nothing after
    it does (the suppression is read live by the gate at every claim)."""
    suppressed: list[str] = []
    for address in dict.fromkeys(a for a in (from_address, locked.sequence.recipient) if a):
        try:
            await suppress(
                db,
                address,
                reason="unsubscribed",
                detail=UNSUBSCRIBE_DETAIL,
                message_id=message_id,
                refresh=False,
            )
        except ValueError:
            # Not a normalizable address (a junk From): it can never be mailed anyway.
            log.warning("unsubscribe_unsuppressible", sequence_id=str(locked.sequence.id))
            continue
        suppressed.append(address)
    cancelled = await stop_sequence(db, locked, reason=UNSUBSCRIBED)
    cancelled += await cancel_reply_drafts(db, locked)
    conv = await _conversation(db, locked)
    if conv is None:
        conv = Conversation(
            sequence_id=locked.sequence.id,
            mailbox_id=locked.sequence.mailbox_id or mailbox_id,
            status="handled",
        )
        db.add(conv)
        await db.flush()
    conv.status = "handled"
    conv.intent = "unsubscribe"
    conv.needs_you_reason = None
    conv.handled_by_id = None
    conv.handled_at = now
    _bump_inbound(conv, received_at)
    await db.flush()
    return Unsubscribed(tuple(suppressed), cancelled, conv)


async def audit_unsubscribe(
    db: AsyncSession, locked: Locked, done: Unsubscribed, *, row: MailboxMessage
) -> None:
    """The audit rows of ``unsubscribe_locked`` (system actor): ``sequence.stopped`` and
    ``conversation.handled``, both naming the mail that asked."""
    await _audit_stop(db, locked, reason="unsubscribed", cancelled=done.cancelled, row=row)
    await _audit_conversation(
        db, audit.CONVERSATION_HANDLED, done.conversation, reason="unsubscribed", row=row
    )


async def _unsubscribe(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail, match: Match, locked: Locked
) -> _Ingested:
    """``unsubscribe_locked`` for a matched opt-out, then its row and audit rows."""
    done = await unsubscribe_locked(
        db,
        locked,
        from_address=parsed.from_address,
        message_id=match.message_id,
        mailbox_id=ctx.mailbox_id,
        now=ctx.now,
        received_at=_received_at(parsed, ctx.now),
    )
    row = await _store(
        db,
        ctx,
        uid,
        parsed,
        kind="unsubscribe",
        match=match,
        conversation=done.conversation,
        body=True,
    )
    await audit_unsubscribe(db, locked, done, row=row)
    return _Ingested("unsubscribes", refresh=done.suppressed)


async def _answered(db: AsyncSession, locked: Locked) -> bool:
    """Whether a reply (or a mail we couldn't read) stops the sequence as ``replied``: it is
    ``active``/``completed``, or it went back to ``awaiting_approval`` (step 1 refused at send
    time) after an email of it may have reached them (``reached_them``): the next step is
    cancelled either way."""
    status = locked.sequence.status
    if status in ("active", "completed"):
        return True
    return status == "awaiting_approval" and await reached_them(db, locked)


async def _reply(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail, match: Match, locked: Locked
) -> _Ingested:
    """The sequence is ``replied`` when ``_answered`` (its unsent follow-up, or a refused
    step 1, cancelled; one already ``sending`` is left to the sender) and the conversation is
    opened. A reply from another address than the recipient needs a teammate; a taken-over
    conversation stays out of automation (``_still_taken_over``); a handled one reopens unless
    it was taken over."""
    sequence = locked.sequence
    cancelled: int | None = None
    if await _answered(db, locked):
        cancelled = await stop_sequence(db, locked, reason=REPLIED, status="replied")
    different = parsed.from_address != sequence.recipient
    conv = await _conversation(db, locked)
    if conv is None:
        conv = await _new_conversation(db, ctx, locked, status="open")
    flagged = False
    kept = _still_taken_over(conv)
    if kept.kept:
        pass  # Ruling P6-Q: never reopened for Claude
    elif different:
        flagged, _ = _flag(conv, DIFFERENT_ADDRESS)
    elif conv.status == "handled":
        conv.status = "open"
        conv.needs_you_reason = None
        conv.handled_by_id = None
        conv.handled_at = None
    _bump_inbound(conv, _received_at(parsed, ctx.now))
    await db.flush()
    row = await _store(
        db, ctx, uid, parsed, kind="reply", match=match, conversation=conv, body=True
    )
    if cancelled is not None:
        await _audit_stop(db, locked, reason="replied", cancelled=cancelled, row=row)
    if kept.rewritten:
        await _audit_backstop(db, conv, row)
    if flagged:
        await _audit_conversation(
            db, audit.CONVERSATION_NEEDS_YOU, conv, reason="different_address", row=row
        )
    return _Ingested("replies")


async def _inbox(db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail) -> _Ingested:
    if parsed.from_address == ctx.mailbox_address:
        return _Ingested(None)  # our own mail (a copy to ourselves): the Sent folder covers it
    # DSNs carry Auto-Submitted: a bounce is recognised before an auto-reply.
    bounce = bounce_of(parsed, known_recipients=ctx.known_recipients)
    if bounce is not None:
        if bounce.kind == "hard":
            return await _hard_bounce(db, ctx, uid, parsed, bounce)
        return await _other_bounce(db, ctx, uid, parsed, bounce)
    match = await match_inbound(db, ctx.mailbox_id, parsed)
    locked = await _lock(db, match) if match is not None else None
    # Ruling P6-M: an opt-out is honoured even when it is flagged automatic.
    unsubscribe = is_unsubscribe(parsed.subject, parsed.new_text, ours=_ours(ctx))
    if match is None or locked is None:
        if unsubscribe and parsed.from_address:
            return await _unmatched_unsubscribe(db, ctx, uid, parsed)
        return _Ingested("unmatched")
    if unsubscribe:
        return await _unsubscribe(db, ctx, uid, parsed, match, locked)
    if is_auto_reply(parsed):
        conv = await _conversation(db, locked)  # linked when there is one; nothing changes
        await _store(db, ctx, uid, parsed, kind="auto_reply", match=match, conversation=conv)
        return _Ingested("auto_replies")
    return await _reply(db, ctx, uid, parsed, match, locked)


async def _unmatched_unsubscribe(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail
) -> _Ingested:
    """Ruling P6-L: an opt-out we can't tie to a sequence (we mailed info@ and Jane pressed
    the unsubscribe button) still suppresses its sender. Stored with no sequence."""
    sender = parsed.from_address
    assert sender is not None  # noqa: S101 - checked by the caller
    await lock_recipient(db, sender)
    refresh: tuple[str, ...] = ()
    try:
        await suppress(db, sender, reason="unsubscribed", detail=UNSUBSCRIBE_DETAIL, refresh=False)
        refresh = (sender,)
    except ValueError:
        log.warning("unsubscribe_unsuppressible", mailbox_id=str(ctx.mailbox_id), uid=uid)
    await _store(db, ctx, uid, parsed, kind="unsubscribe", body=True)
    return _Ingested("unsubscribes", refresh=refresh)


# ----------------------------------------------------------------------- Sent


async def _sent(db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail) -> _Ingested:
    """A teammate's mail from the mailbox to a sequence's recipient: automation stops for
    them (follow-up and reply drafts cancelled; a ``replied`` sequence stays ``replied``) and
    the conversation is ``taken_over``."""
    if parsed.headers_auto:
        return _Ingested(None)  # an automatic message (e.g. a vacation responder), not a person
    if await is_ours(db, ctx.mailbox_id, parsed):
        return _Ingested(None)
    match = await match_sent(db, ctx.mailbox_id, parsed, check_ours=False)
    locked = await _lock(db, match) if match is not None else None
    if match is None or locked is None:
        return _Ingested("unmatched")
    sequence = locked.sequence
    cancelled: int | None = None
    if sequence.status in ("awaiting_approval", "active"):
        cancelled = await stop_sequence(db, locked, reason=TAKEOVER)
    elif sequence.status == "replied":
        cancelled = await stop_sequence(db, locked, reason=TAKEOVER, status="replied")
    else:  # completed, stopped, failed: nothing left but possible reply drafts
        await cancel_reply_drafts(db, locked)
    conv = await _conversation(db, locked)
    if conv is None:
        conv = await _new_conversation(db, ctx, locked, status="taken_over")
        was = None
    else:
        was = conv.status
    conv.status = "taken_over"
    conv.needs_you_reason = None
    conv.taken_over_at = conv.taken_over_at or ctx.now
    await db.flush()
    row = await _store(
        db, ctx, uid, parsed, kind="human_sent", match=match, conversation=conv, body=True
    )
    if cancelled is not None:
        await _audit_stop(db, locked, reason="taken_over", cancelled=cancelled, row=row)
    if was != "taken_over":
        await _audit_conversation(
            db, audit.CONVERSATION_TAKEN_OVER, conv, reason="human_sent", row=row
        )
    return _Ingested("takeovers")


async def _ingest(
    db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail, *, cursor_uid: int
) -> _Ingested:
    """One mail, in the caller's transaction (flushes; the caller commits, then runs
    ``_after``). The folder's cursor moves to ``cursor_uid`` in the same transaction."""
    parsed = _clean(parsed)
    if await _seen(db, ctx, uid, parsed):
        result = _Ingested(None)
    elif ctx.folder == "inbox":
        result = await _inbox(db, ctx, uid, parsed)
    else:
        result = await _sent(db, ctx, uid, parsed)
    await _advance(db, ctx, cursor_uid)
    return result


async def _unreadable(db: AsyncSession, ctx: _Ctx, uid: int, parsed: ParsedMail) -> _Ingested:
    """A mail that failed ``MAX_MAIL_FAILURES`` polls in a row. Nothing here may fail for the
    reason the mail did, so every step that touches its values is isolated:

    - matching runs in a savepoint (no match if it fails);
    - a matched sequence is stopped as answered (Ruling P6-O: we couldn't read it, so assume
      they replied): ``replied`` when ``_answered``, unsent mail cancelled;
    - an INBOX opt-out suppresses its sender (and a matched sequence's recipient, whose
      sequence then stops "Unsubscribed", as ``_unsubscribe`` does) in its own savepoint;
    - a matched conversation needs a teammate ("Couldn't read a mail"; a taken-over one stays
      taken over);
    - the placeholder row (kind ``unreadable``: UID, folder, received time, sanitized subject
      and From only) is stored in its own savepoint, so failing to store it never undoes the
      suppression; if it fails, the folder shows ``SKIPPED`` instead.

    Lock order as everywhere: messages, sequence, recipient, conversation, then the inserts
    that refer to the mailbox."""
    parsed = _clean(parsed)
    match: Match | None = None
    try:
        async with db.begin_nested():
            if ctx.folder == "inbox":
                match = await match_inbound(db, ctx.mailbox_id, parsed)
            else:
                match = await match_sent(db, ctx.mailbox_id, parsed)
    except Exception as exc:
        log.warning("unreadable_match_failed", uid=uid, error_type=type(exc).__name__)
        match = None
    locked = await _lock(db, match) if match is not None else None
    sender = parsed.from_address
    unsubscribe = ctx.folder == "inbox" and is_unsubscribe(
        parsed.subject, parsed.new_text, ours=_ours(ctx)
    )
    if unsubscribe and sender and locked is None:
        await lock_recipient(db, sender)

    counted: str | None = None
    refresh: list[str] = []
    if unsubscribe:
        targets = [sender] if sender else []
        if locked is not None:
            targets.append(locked.sequence.recipient)
        for address in dict.fromkeys(targets):
            try:
                async with db.begin_nested():
                    await suppress(
                        db,
                        address,
                        reason="unsubscribed",
                        detail=UNSUBSCRIBE_DETAIL,
                        message_id=match.message_id if match is not None else None,
                        refresh=False,
                    )
            except Exception as exc:
                log.warning("unsubscribe_unsuppressible", uid=uid, error_type=type(exc).__name__)
                continue
            refresh.append(address)
        if refresh:
            counted = "unsubscribes"

    conv: Conversation | None = None
    stop: tuple[str, int] | None = None
    flagged = rewritten = False
    if locked is not None:
        if unsubscribe:
            cancelled = await stop_sequence(db, locked, reason=UNSUBSCRIBED)
            cancelled += await cancel_reply_drafts(db, locked)
            stop = ("unsubscribed", cancelled)
        elif ctx.folder == "inbox" and await _answered(db, locked):
            cancelled = await stop_sequence(db, locked, reason=REPLIED, status="replied")
            stop = ("replied", cancelled)
        conv = await _conversation(db, locked)
        if conv is None:
            conv = await _new_conversation(db, ctx, locked, status="open")
        flagged, rewritten = _flag(conv, UNREADABLE)
        if ctx.folder == "inbox":
            _bump_inbound(conv, ctx.now)
        await db.flush()

    row: MailboxMessage | None = None
    try:
        async with db.begin_nested():
            row = MailboxMessage(
                mailbox_id=ctx.mailbox_id,
                folder=ctx.folder,
                uidvalidity=ctx.uidvalidity,
                uid=uid,
                from_address=_address(sender),
                subject=parsed.subject,
                received_at=_received_at(parsed, ctx.now),
                kind="unreadable",
                matched_by=match.by if locked is not None and match is not None else None,
                sequence_id=locked.sequence.id if locked is not None else None,
                conversation_id=conv.id if conv is not None else None,
            )
            db.add(row)
            await db.flush()
    except Exception as exc:
        log.warning("unreadable_placeholder_failed", uid=uid, error_type=type(exc).__name__)
        row = None
        # Nothing stored for this mail: leave a trace a teammate will see.
        await db.execute(
            update(MailboxFolder)
            .where(MailboxFolder.mailbox_id == ctx.mailbox_id, MailboxFolder.folder == ctx.folder)
            .values(last_error=SKIPPED)
        )
    if locked is not None and stop is not None:
        await _audit_stop(db, locked, reason=stop[0], cancelled=stop[1], row=row)
    if rewritten and conv is not None:
        await _audit_backstop(db, conv, row)
    if flagged and conv is not None:
        await _audit_conversation(
            db, audit.CONVERSATION_NEEDS_YOU, conv, reason="unreadable", row=row
        )
    return _Ingested(counted, refresh=tuple(refresh))


async def _after(
    db_factory: async_sessionmaker[AsyncSession], result: _Ingested, *, now: datetime
) -> None:
    """After the mail's commit, holding no outreach lock: the second half of a bounce and the
    address refreshes of new suppressions (each in its own short transaction). Skipping them
    is safe: suppressions are read live, and the worker's Sweep catches up."""
    if result.bounced_to is not None:
        async with db_factory() as db:
            try:
                await after_bounce(db, recipient=result.bounced_to, now=now)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                log.warning("after_bounce_failed", error_type=type(exc).__name__)
    for address in result.refresh:
        async with db_factory() as db:
            try:
                await refresh_blocks(db, email=address)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                log.warning("refresh_blocks_failed", error_type=type(exc).__name__)


# --------------------------------------------------------------------------- polling


@dataclass
class _Tally:
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    paused: str | None = None

    def add(self, result: _Ingested) -> None:
        if result.counted is not None:
            self.counts[result.counted] = self.counts.get(result.counted, 0) + 1
        if result.paused is not None:
            self.paused = result.paused

    def fail(self, error: str) -> None:
        self.error = self.error or error

    def outcome(self) -> PollOutcome:
        c = self.counts.get
        return PollOutcome(
            replies=c("replies", 0),
            auto_replies=c("auto_replies", 0),
            bounces=c("bounces", 0),
            unclear_bounces=c("unclear_bounces", 0),
            unsubscribes=c("unsubscribes", 0),
            takeovers=c("takeovers", 0),
            unmatched=c("unmatched", 0),
            error=self.error,
            paused=self.paused,
        )


@dataclass(frozen=True)
class _Box:
    id: uuid.UUID
    address: str
    created_at: datetime
    sent_folder: str | None


async def _set_error(
    db_factory: async_sessionmaker[AsyncSession],
    mailbox_id: uuid.UUID,
    folders: tuple[Folder, ...],
    error: str,
) -> None:
    """Show ``error`` on the folders, except one showing ``SKIPPED``: that stays until a
    teammate dismisses it."""
    async with db_factory() as db:
        await db.execute(
            update(MailboxFolder)
            .where(
                MailboxFolder.mailbox_id == mailbox_id,
                MailboxFolder.folder.in_(folders),
                MailboxFolder.last_error.is_distinct_from(SKIPPED),
            )
            .values(last_error=error[:ERROR_LIMIT])
        )
        await db.commit()


async def _record_failure(
    db_factory: async_sessionmaker[AsyncSession],
    mailbox_id: uuid.UUID,
    folder: Folder,
    uid: int,
    error: str,
) -> int:
    """Count one more failed poll of ``uid`` (a different UID starts over); returns the count."""
    async with db_factory() as db:
        row = (
            await db.execute(
                select(MailboxFolder)
                .where(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.folder == folder)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            await db.rollback()
            return 1
        if row.failed_uid == uid:
            row.failures += 1
        else:
            row.failed_uid, row.failures = uid, 1
        if row.last_error != SKIPPED:  # sticky until dismissed
            row.last_error = error[:ERROR_LIMIT]
        failures = row.failures
        await db.commit()
    return failures


async def _give_up(
    db_factory: async_sessionmaker[AsyncSession],
    ctx: _Ctx,
    uid: int,
    parsed: ParsedMail,
    *,
    cursor_uid: int,
) -> _Ingested:
    """Store the placeholder and move the cursor past the mail, in a fresh transaction
    (``_unreadable``). If even that fails, a last transaction still honours an INBOX opt-out
    from its sender, moves the cursor alone (the folder must never stay stuck) and leaves
    ``SKIPPED`` as the folder's ``last_error`` for a teammate to see."""
    try:
        async with db_factory() as db:
            result = await _unreadable(db, ctx, uid, parsed)
            await _advance(db, ctx, cursor_uid)
            await db.commit()
        log.warning("mail_unreadable_stored", mailbox_id=str(ctx.mailbox_id), uid=uid)
        return result
    except Exception as exc:
        log.warning("mail_unreadable_failed", uid=uid, error_type=type(exc).__name__)
    cleaned = _clean(parsed)
    sender = cleaned.from_address
    refresh: tuple[str, ...] = ()
    async with db_factory() as db:
        if (
            ctx.folder == "inbox"
            and sender
            and is_unsubscribe(cleaned.subject, cleaned.new_text, ours=_ours(ctx))
        ):
            try:
                async with db.begin_nested():
                    await lock_recipient(db, sender)
                    await suppress(
                        db, sender, reason="unsubscribed", detail=UNSUBSCRIBE_DETAIL, refresh=False
                    )
                refresh = (sender,)
            except Exception as exc:
                log.warning("unsubscribe_unsuppressible", uid=uid, error_type=type(exc).__name__)
        await _advance(db, ctx, cursor_uid)
        await db.execute(
            update(MailboxFolder)
            .where(MailboxFolder.mailbox_id == ctx.mailbox_id, MailboxFolder.folder == ctx.folder)
            .values(last_error=SKIPPED)
        )
        await db.commit()
    return _Ingested("unsubscribes" if refresh else None, refresh=refresh)


@dataclass(frozen=True)
class _Cursor:
    name: str
    uidvalidity: int | None
    last_uid: int
    polled_at: datetime | None


async def _ensure_folders(
    db_factory: async_sessionmaker[AsyncSession], box: _Box
) -> dict[str, _Cursor]:
    """Each folder's cursor (created on the first poll)."""
    async with db_factory() as db:
        await db.execute(
            insert(MailboxFolder)
            .values(
                [
                    {"mailbox_id": box.id, "folder": f, "name": INBOX if f == "inbox" else ""}
                    for f in FOLDERS
                ]
            )
            .on_conflict_do_nothing(index_elements=["mailbox_id", "folder"])
        )
        await db.commit()
        rows = (
            await db.scalars(select(MailboxFolder).where(MailboxFolder.mailbox_id == box.id))
        ).all()
        cursors = {r.folder: _Cursor(r.name, r.uidvalidity, r.last_uid, r.polled_at) for r in rows}
        await db.rollback()
    return cursors


async def _known_recipients(
    db_factory: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID
) -> frozenset[str]:
    """Everyone this mailbox has sent to (what a bounce may be about)."""
    async with db_factory() as db:
        rows = await db.scalars(
            select(Sequence.recipient)
            .join(Message, Message.sequence_id == Sequence.id)
            .where(Message.mailbox_id == mailbox_id, Message.status.in_(SENT_STATUSES))
            .distinct()
        )
        found = frozenset(rows.all())
        await db.rollback()
    return found


async def _empty_batch(
    db_factory: async_sessionmaker[AsyncSession],
    mailbox_id: uuid.UUID,
    folder: Folder,
    *,
    name: str,
    rescan: bool,
    now: datetime,
) -> None:
    """Nothing new: record the poll. A rescan that found nothing keeps no UIDVALIDITY, so the
    next poll searches by date again instead of reading the folder from UID 1."""
    values: dict[str, object] = {"name": name, "polled_at": now}
    if rescan:
        values.update(uidvalidity=None, last_uid=0)
    async with db_factory() as db:
        await db.execute(
            update(MailboxFolder)
            .where(MailboxFolder.mailbox_id == mailbox_id, MailboxFolder.folder == folder)
            .values(**values)
        )
        await db.commit()


async def _clear_error(
    db_factory: async_sessionmaker[AsyncSession], mailbox_id: uuid.UUID, folder: Folder
) -> None:
    """After a clean read: the folder's error is history, except ``SKIPPED`` (a mail we never
    stored), which stays for a teammate to see."""
    async with db_factory() as db:
        await db.execute(
            update(MailboxFolder)
            .where(
                MailboxFolder.mailbox_id == mailbox_id,
                MailboxFolder.folder == folder,
                MailboxFolder.last_error.is_not(None),
                MailboxFolder.last_error != SKIPPED,
            )
            .values(last_error=None)
        )
        await db.commit()


async def _read_folder(
    db_factory: async_sessionmaker[AsyncSession],
    reader: MailReader,
    box: _Box,
    folder: Folder,
    cursor: _Cursor,
    *,
    tally: _Tally,
    config: AppConfig,
    now: Callable[[], datetime],
    known: Callable[[], Awaitable[frozenset[str]]],
) -> None:
    """Read one folder's new mail (IMAP outside any transaction) and ingest it mail by mail.
    Reader failures propagate (the caller records them). A mail that can't be processed stops
    the folder without moving its cursor past it; on its ``MAX_MAIL_FAILURES``-th failure in
    a row it is stored as ``unreadable`` (``_unreadable``) and the folder moves on."""
    if folder == "inbox":
        name: str | None = INBOX
    else:
        name = box.sent_folder or await reader.special_folder("sent")
    if name is None:
        tally.fail(NO_SENT_FOLDER)
        await _set_error(db_factory, box.id, (folder,), NO_SENT_FOLDER)
        return
    uidvalidity, last_uid = cursor.uidvalidity, cursor.last_uid
    if cursor.name and cursor.name != name:  # another folder now: its UIDs mean nothing here
        uidvalidity, last_uid = None, 0
    base = cursor.polled_at - RESCAN_OVERLAP if cursor.polled_at else box.created_at
    since = base.date()
    clean = True  # no mail failed in this read: the folder's last error can be cleared
    for _ in range(MAX_BATCHES):
        batch = await reader.fetch_new(
            name,
            uidvalidity=uidvalidity,
            after_uid=last_uid,
            since=since,
            limit=config.mail.max_messages_per_poll,
            max_bytes=config.mail.max_message_bytes,
        )
        rescan = uidvalidity is None or uidvalidity != batch.uidvalidity
        if not batch.mails:
            await _empty_batch(db_factory, box.id, folder, name=name, rescan=rescan, now=now())
            break
        recipients = await known() if folder == "inbox" else frozenset[str]()
        ctx = _Ctx(
            mailbox_id=box.id,
            mailbox_address=box.address,
            folder=folder,
            name=name,
            uidvalidity=batch.uidvalidity,
            known_recipients=recipients,
            now=now(),
            config=config,
        )
        cursor_uid = 0 if rescan else last_uid
        for mail in sorted(batch.mails, key=lambda m: m.uid):
            parsed = parse_mail(mail.raw, max_body_chars=config.mail.max_body_chars)
            cursor_uid = max(cursor_uid, mail.uid)
            ctx = dataclasses.replace(ctx, now=now())
            try:
                async with db_factory() as db:
                    result = await _ingest(db, ctx, mail.uid, parsed, cursor_uid=cursor_uid)
                    await db.commit()
            except Exception as exc:
                log.warning(
                    "mail_ingest_failed",
                    mailbox_id=str(box.id),
                    folder=folder,
                    uid=mail.uid,
                    error_type=type(exc).__name__,
                )
                tally.fail(_process_error(exc))
                failures = await _record_failure(
                    db_factory, box.id, folder, mail.uid, _process_error(exc)
                )
                if failures < MAX_MAIL_FAILURES:
                    return  # retried next poll; the cursor stays before this mail
                clean = False
                result = await _give_up(db_factory, ctx, mail.uid, parsed, cursor_uid=cursor_uid)
            tally.add(result)
            await _after(db_factory, result, now=ctx.now)
        uidvalidity, last_uid = batch.uidvalidity, cursor_uid
        if not batch.more:
            break
    if clean:
        await _clear_error(db_factory, box.id, folder)


async def poll_mailbox(
    db_factory: async_sessionmaker[AsyncSession],
    mailbox_id: uuid.UUID,
    *,
    mail: MailFactory,
    settings: Settings,
    config: AppConfig,
    now: Callable[[], datetime],
) -> PollOutcome:
    """Read the mailbox's INBOX, then its Sent folder (``mailbox.sent_folder``, else the folder
    flagged ``\\Sent``), from each folder's cursor (by date, ``polled_at`` minus a day or the
    mailbox's creation, when there is no usable UIDVALIDITY), and ingest every new mail in
    its own transaction (see the module docstring). A reader failure is stored as the
    folder's ``last_error`` ("Couldn't read the mailbox: <ExceptionType>") and returned as
    the outcome's ``error``; it never raises."""
    async with db_factory() as db:
        row = await db.get(Mailbox, mailbox_id)
        if row is None:
            return PollOutcome(error="Mailbox not found.")
        box = _Box(row.id, row.address, row.created_at, row.sent_folder)
        creds = credentials(row, settings, config)
        await db.rollback()
    cursors = await _ensure_folders(db_factory, box)
    if creds is None:
        await _set_error(db_factory, box.id, FOLDERS, NO_PASSWORD)
        return PollOutcome(error=NO_PASSWORD)
    creds = dataclasses.replace(creds, timeout=config.mail.imap_timeout_seconds)
    tally = _Tally()
    try:
        reader = await mail.open_reader(creds)
    except Exception as exc:
        log.warning("mail_open_failed", mailbox_id=str(box.id), error_type=type(exc).__name__)
        await _set_error(db_factory, box.id, FOLDERS, read_error(exc))
        return PollOutcome(error=read_error(exc))
    cached: list[frozenset[str]] = []

    async def known() -> frozenset[str]:
        if not cached:
            cached.append(await _known_recipients(db_factory, box.id))
        return cached[0]

    try:
        for folder in FOLDERS:
            try:
                await _read_folder(
                    db_factory,
                    reader,
                    box,
                    folder,
                    cursors[folder],
                    tally=tally,
                    config=config,
                    now=now,
                    known=known,
                )
            except Exception as exc:
                log.warning(
                    "mail_read_failed",
                    mailbox_id=str(box.id),
                    folder=folder,
                    error_type=type(exc).__name__,
                )
                tally.fail(read_error(exc))
                await _set_error(db_factory, box.id, (folder,), read_error(exc))
    finally:
        try:
            await reader.aclose()
        except Exception as exc:
            log.warning("mail_close_failed", error_type=type(exc).__name__)
    outcome = tally.outcome()
    log.info(
        "mail_polled",
        mailbox_id=str(box.id),
        replies=outcome.replies,
        auto_replies=outcome.auto_replies,
        bounces=outcome.bounces,
        unclear_bounces=outcome.unclear_bounces,
        unsubscribes=outcome.unsubscribes,
        takeovers=outcome.takeovers,
        unmatched=outcome.unmatched,
        failed=outcome.error is not None,
    )
    return outcome


async def poll_mailboxes(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    mail: MailFactory,
    settings: Settings,
    config: AppConfig,
    now: Callable[[], datetime],
) -> dict[str, PollOutcome]:
    """``poll_mailbox`` for every mailbox whose password is set, in address order, enabled
    or not (Ruling P6-J: ``enabled`` gates sending only; an opt-out sent to a mailbox that is
    turned off is still honoured), except one whose connection settings (new or changed)
    haven't passed a Test connection (Rulings P6-V/W: the password never goes to an untested
    host): that one isn't read and its outcome says so (``untested``). One mailbox's failure
    never stops the others. Mailbox address -> outcome."""
    async with db_factory() as db:
        boxes = [
            (b.id, b.address, untested(b))
            for b in (await db.scalars(select(Mailbox).order_by(Mailbox.address))).all()
            if credentials(b, settings, config) is not None
        ]
        await db.rollback()
    outcomes: dict[str, PollOutcome] = {}
    for box_id, address, held in boxes:
        if held:
            outcomes[address] = PollOutcome(untested=True)
            continue
        try:
            outcomes[address] = await poll_mailbox(
                db_factory, box_id, mail=mail, settings=settings, config=config, now=now
            )
        except Exception as exc:  # a database failure; the reader's are handled inside
            log.warning("mail_poll_failed", mailbox_id=str(box_id), error_type=type(exc).__name__)
            outcomes[address] = PollOutcome(error=read_error(exc))
    return outcomes
