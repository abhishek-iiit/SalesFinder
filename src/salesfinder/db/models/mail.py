"""Mail: the sending mailboxes (Gmail/Workspace accounts; app passwords live in .env only),
the IMAP read cursor per folder, the mail read from them, and the conversations it starts."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, Timestamps, UUIDPk
from salesfinder.db.models.companies import SIDE_CHECK

MAILBOX_SECURITY = ("ssl", "starttls")
SECRET_NAME_CHECK = "secret_name ~ '^[a-z0-9_]{1,32}$'"  # noqa: S105
MAILBOX_FOLDERS = ("inbox", "sent")
CONVERSATION_STATUSES = ("open", "needs_you", "handled", "taken_over")
REPLY_INTENTS = (
    "interested",
    "objection",
    "not_now",
    "wrong_person",
    "unsubscribe",
    "auto_reply",
    "other",
)
INBOUND_KINDS = (
    "reply",
    "auto_reply",
    "bounce",
    "soft_bounce",
    "unclear_bounce",
    "unsubscribe",
    "human_sent",
    # A mail that failed to process three times: a placeholder so the folder moves on.
    "unreadable",
)
MATCHED_BY = ("header", "address")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


class Mailbox(UUIDPk, Timestamps, Base):
    __tablename__ = "mailboxes"
    __table_args__ = (
        CheckConstraint(SIDE_CHECK, name="side"),
        CheckConstraint(SECRET_NAME_CHECK, name="secret_name"),
        CheckConstraint("smtp_security IN ('ssl','starttls')", name="smtp_security"),
        CheckConstraint("daily_max BETWEEN 1 AND 50", name="daily_max"),
    )

    # Lowercase.
    address: Mapped[str] = mapped_column(String(320), unique=True)
    side: Mapped[str] = mapped_column(String(16))
    username: Mapped[str] = mapped_column(String(320))
    # Key into SALESFINDER_MAILBOX_PASSWORDS; the password itself is never stored.
    secret_name: Mapped[str] = mapped_column(String(32), unique=True)
    smtp_host: Mapped[str] = mapped_column(
        String(255), default="smtp.gmail.com", server_default="smtp.gmail.com"
    )
    smtp_port: Mapped[int] = mapped_column(default=465, server_default=text("465"))
    smtp_security: Mapped[str] = mapped_column(String(8), default="ssl", server_default="ssl")
    imap_host: Mapped[str] = mapped_column(
        String(255), default="imap.gmail.com", server_default="imap.gmail.com"
    )
    imap_port: Mapped[int] = mapped_column(default=993, server_default=text("993"))
    # None means auto-detect the folder flagged \Sent.
    sent_folder: Mapped[str | None] = mapped_column(String(200), default=None)
    dkim_selector: Mapped[str] = mapped_column(
        String(63), default="google", server_default="google"
    )
    warmup_started_on: Mapped[date | None] = mapped_column(Date, default=None)
    daily_max: Mapped[int] = mapped_column(default=30, server_default=text("30"))
    enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    hold_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    dns_ok: Mapped[bool] = mapped_column(default=False, server_default=false())
    dns_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    dns_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class MailboxFolder(Base):
    """The IMAP read cursor of one of a mailbox's folders (its INBOX, its Sent folder)."""

    __tablename__ = "mailbox_folders"
    __table_args__ = (CheckConstraint(_in("folder", MAILBOX_FOLDERS), name="folder"),)

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(String(8), primary_key=True)
    # The server's name for it (INBOX, or e.g. "[Gmail]/Sent Mail").
    name: Mapped[str] = mapped_column(String(200))
    # None until a mail has been read under the folder's current UIDVALIDITY.
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, default=None)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    # The last poll that read the folder without an error.
    polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    # The UID that failed to process and how many polls in a row it failed; after
    # MAX_MAIL_FAILURES it is stored as ``unreadable`` and the cursor moves past it.
    failed_uid: Mapped[int | None] = mapped_column(BigInteger, default=None)
    failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))


class Conversation(UUIDPk, Timestamps, Base):
    """A sequence's exchange with its recipient once they (or a teammate) wrote outside our
    automated emails. One per sequence; never deleted while the sequence exists."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_in("status", CONVERSATION_STATUSES), name="status"),
        CheckConstraint(_in("intent", REPLY_INTENTS), name="intent"),
        Index("ix_conversations_status_last_inbound_at", "status", "last_inbound_at"),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequences.id", ondelete="CASCADE"), unique=True
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mailboxes.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(12))
    needs_you_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    intent: Mapped[str | None] = mapped_column(String(16), default=None)
    summary: Mapped[str | None] = mapped_column(String(300), default=None)
    products: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    handled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    taken_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class MailboxMessage(UUIDPk, Base):
    """One mail read from a mailbox folder that we acted on or keep (replies, auto-replies,
    bounces, unsubscribes, a teammate's mail from the mailbox). Unmatched mail isn't stored.

    ``subject``, ``body_text`` and the addresses are untrusted: shown escaped, never logged."""

    __tablename__ = "mailbox_messages"
    __table_args__ = (
        CheckConstraint(_in("folder", MAILBOX_FOLDERS), name="folder"),
        CheckConstraint(_in("kind", INBOUND_KINDS), name="kind"),
        CheckConstraint(_in("matched_by", MATCHED_BY), name="matched_by"),
        CheckConstraint(_in("intent", REPLY_INTENTS), name="intent"),
        UniqueConstraint(
            "mailbox_id",
            "folder",
            "uidvalidity",
            "uid",
            name="uq_mailbox_messages_mailbox_id_folder_uidvalidity_uid",
        ),
        Index(
            "ix_mailbox_messages_mailbox_id_folder_message_id_header",
            "mailbox_id",
            "folder",
            "message_id_header",
        ),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mailboxes.id", ondelete="RESTRICT"))
    folder: Mapped[str] = mapped_column(String(8))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    # Message ids keep their angle brackets.
    message_id_header: Mapped[str | None] = mapped_column(String(255), default=None)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), default=None)
    references_header: Mapped[str | None] = mapped_column(Text, default=None)
    from_address: Mapped[str | None] = mapped_column(String(320), default=None)
    # To and Cc, normalized lowercase.
    to_addresses: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    subject: Mapped[str] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))
    matched_by: Mapped[str | None] = mapped_column(String(8), default=None)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sequences.id", ondelete="SET NULL"), default=None, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), default=None, index=True
    )
    # Our outbound email it refers to.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    # The new (unquoted) text; kept for reply, unsubscribe and human_sent only.
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    intent: Mapped[str | None] = mapped_column(String(16), default=None)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), default=None)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    classify_failures: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
