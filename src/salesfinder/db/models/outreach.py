"""Outreach: Claude usage events, campaigns, per-company sequences, drafted messages, feedback,
and per-company drafting failures (backoff)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
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

SIDES = ("buyer", "supplier")
CAMPAIGN_STATUSES = ("draft", "active", "paused", "archived")
SEQUENCE_STATUSES = (
    "awaiting_approval",
    "active",
    "completed",
    "stopped",
    "failed",
    "replied",
)
MESSAGE_STATUSES = (
    "pending",
    "gate_failed",
    "awaiting_approval",
    "approved",
    "rejected",
    "cancelled",
    "sent",
    "bounced",
    "sending",
    "unconfirmed",
    "failed",
)
MESSAGE_KINDS = ("first", "followup", "reply")
SUPPRESSION_REASONS = ("unsubscribed", "bounced", "manual")
# Keep in sync with NEW_STEP in migration 0013_sending.py.
KIND_STEP_CHECK = (
    "(kind='first' AND step=1) OR (kind='followup' AND step=2) OR (kind='reply' AND step>=3)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


class UsageEvent(UUIDPk, Base):
    """One attempted Claude call (success or failure); the daily budget counts these."""

    __tablename__ = "usage_events"

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    task: Mapped[str] = mapped_column(String(32))
    ok: Mapped[bool]
    duration_ms: Mapped[int]
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), default=None)
    error: Mapped[str | None] = mapped_column(String(200), default=None)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), default=None
    )


class Campaign(UUIDPk, Timestamps, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(SIDE_CHECK, name="side"),
        CheckConstraint(_in("playbook", SIDES), name="playbook"),
        CheckConstraint("playbook = side", name="playbook_matches_side"),
        CheckConstraint(_in("status", CAMPAIGN_STATUSES), name="status"),
        # Names are unique regardless of case (migration 0012).
        Index("uq_campaigns_lower_name", text("lower(name)"), unique=True),
    )

    name: Mapped[str] = mapped_column(String(120), unique=True)
    side: Mapped[str] = mapped_column(String(16))
    cohort_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cohorts.id", ondelete="RESTRICT"))
    # Always equal to side (table check); the playbook file the writer loads.
    playbook: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12))
    daily_draft_cap: Mapped[int] = mapped_column(default=40, server_default=text("40"))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="RESTRICT"), default=None
    )


class Sequence(UUIDPk, Timestamps, Base):
    __tablename__ = "sequences"
    __table_args__ = (
        CheckConstraint(_in("status", SEQUENCE_STATUSES), name="status"),
        UniqueConstraint("campaign_id", "company_id", name="uq_sequences_campaign_id_company_id"),
        Index("ix_sequences_recipient_status", "recipient", "status"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), default=None
    )
    email_address_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_addresses.id", ondelete="SET NULL"), default=None
    )
    # Snapshot of the address at draft time, lowercase.
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20))
    stop_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="RESTRICT"), default=None
    )


def _kind_from_step(context: Any) -> str:
    """Insert-time default for ``messages.kind`` when the caller gave only the step."""
    step: int = context.get_current_parameters()["step"]
    return "first" if step == 1 else "followup" if step == 2 else "reply"


class Message(UUIDPk, Timestamps, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_in("kind", MESSAGE_KINDS), name="kind"),
        CheckConstraint(KIND_STEP_CHECK, name="step"),
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="status"),
        UniqueConstraint("sequence_id", "step", name="uq_messages_sequence_id_step"),
        Index("ix_messages_status_created_at", "status", "created_at"),
        Index("ix_messages_status_kind_next_attempt_at", "status", "kind", "next_attempt_at"),
        Index(
            "uq_messages_message_id_header",
            "message_id_header",
            unique=True,
            postgresql_where=text("message_id_header IS NOT NULL"),
        ),
    )

    sequence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sequences.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[int]
    kind: Mapped[str] = mapped_column(String(10), default=_kind_from_step)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    # None until the gate has run on it (a step 2 waiting for its due date).
    # none_as_null: an explicit None is SQL NULL (not JSON null).
    gate_errors: Mapped[list[Any] | None] = mapped_column(
        JSONB(none_as_null=True), default=list, server_default=text("'[]'::jsonb")
    )
    edited: Mapped[bool] = mapped_column(default=False, server_default=false())
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    rejected_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    reject_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="RESTRICT"), default=None
    )
    message_id_header: Mapped[str | None] = mapped_column(String(255), default=None)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), default=None)
    references_header: Mapped[str | None] = mapped_column(Text, default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    send_attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_send_error: Mapped[str | None] = mapped_column(String(300), default=None)
    send_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    bounce_detail: Mapped[str | None] = mapped_column(String(300), default=None)
    # A reply: the inbound mail it answers (use_alter: mailbox_messages refers back here).
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mailbox_messages.id", ondelete="SET NULL", use_alter=True), default=None
    )
    # A reply a teammate wrote themselves (Ruling P6-D); None for Claude's drafts.
    written_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class Suppression(UUIDPk, Base):
    """An address we never email again (unsubscribe, hard bounce, or a teammate's decision)."""

    __tablename__ = "suppressions"
    __table_args__ = (CheckConstraint(_in("reason", SUPPRESSION_REASONS), name="reason"),)

    # Normalized (lowercase) address.
    address: Mapped[str] = mapped_column(String(320), unique=True)
    reason: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(300))
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxFeedback(UUIDPk, Base):
    """A reviewer's rejection reason, fed back into later drafts for the campaign."""

    __tablename__ = "outbox_feedback"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    reason: Mapped[str] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DraftFailure(Base):
    """Drafting for a company in a campaign failed (Claude errored, or its reply or the context
    couldn't be used): the company is skipped for a while, and for the rest of the UTC day
    after repeated failures. Deleted when a draft for it is stored."""

    __tablename__ = "draft_failures"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Failures on the UTC day of ``last_failed_at`` (reset on a new day).
    failures: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(200))
