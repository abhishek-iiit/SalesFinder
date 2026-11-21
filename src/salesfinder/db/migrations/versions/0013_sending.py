"""sending

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-13 12:00:00.000000

Mailboxes, the suppression list, and the columns sending and replies need on campaigns,
sequences, messages and email patterns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SEQ_STATUS = "status IN ('awaiting_approval','active','completed','stopped','failed')"
NEW_SEQ_STATUS = "status IN ('awaiting_approval','active','completed','stopped','failed','replied')"
OLD_MSG_STATUS = (
    "status IN ('pending','gate_failed','awaiting_approval','approved','rejected',"
    "'cancelled','sent','bounced')"
)
NEW_MSG_STATUS = (
    "status IN ('pending','gate_failed','awaiting_approval','approved','rejected',"
    "'cancelled','sent','bounced','sending','unconfirmed','failed')"
)
NEW_STEP = "(kind='first' AND step=1) OR (kind='followup' AND step=2) OR (kind='reply' AND step>=3)"


def upgrade() -> None:
    op.create_table(
        "mailboxes",
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("username", sa.String(length=320), nullable=False),
        sa.Column("secret_name", sa.String(length=32), nullable=False),
        sa.Column(
            "smtp_host", sa.String(length=255), server_default="smtp.gmail.com", nullable=False
        ),
        sa.Column("smtp_port", sa.Integer(), server_default=sa.text("465"), nullable=False),
        sa.Column("smtp_security", sa.String(length=8), server_default="ssl", nullable=False),
        sa.Column(
            "imap_host", sa.String(length=255), server_default="imap.gmail.com", nullable=False
        ),
        sa.Column("imap_port", sa.Integer(), server_default=sa.text("993"), nullable=False),
        sa.Column("sent_folder", sa.String(length=200), nullable=True),
        sa.Column("dkim_selector", sa.String(length=63), server_default="google", nullable=False),
        sa.Column("warmup_started_on", sa.Date(), nullable=True),
        sa.Column("daily_max", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.String(length=300), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dns_ok", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("dns_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dns_report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('buyer','supplier')", name=op.f("ck_mailboxes_side")),
        sa.CheckConstraint(
            "secret_name ~ '^[a-z0-9_]{1,32}$'", name=op.f("ck_mailboxes_secret_name")
        ),
        sa.CheckConstraint(
            "smtp_security IN ('ssl','starttls')", name=op.f("ck_mailboxes_smtp_security")
        ),
        sa.CheckConstraint("daily_max BETWEEN 1 AND 50", name=op.f("ck_mailboxes_daily_max")),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_mailboxes_created_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mailboxes")),
        sa.UniqueConstraint("address", name=op.f("uq_mailboxes_address")),
        sa.UniqueConstraint("secret_name", name=op.f("uq_mailboxes_secret_name")),
    )

    op.add_column("campaigns", sa.Column("mailbox_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_campaigns_mailbox_id_mailboxes"),
        "campaigns",
        "mailboxes",
        ["mailbox_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("sequences", sa.Column("mailbox_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_sequences_mailbox_id_mailboxes"),
        "sequences",
        "mailboxes",
        ["mailbox_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(op.f("ck_sequences_status"), "sequences", type_="check")
    op.create_check_constraint(op.f("ck_sequences_status"), "sequences", NEW_SEQ_STATUS)

    # messages: kind (backfilled from step), then the new checks.
    op.add_column("messages", sa.Column("kind", sa.String(length=10), nullable=True))
    op.execute("UPDATE messages SET kind = CASE WHEN step = 1 THEN 'first' ELSE 'followup' END")
    op.alter_column("messages", "kind", nullable=False)
    op.alter_column("messages", "gate_errors", nullable=True)
    # A step 2 that has not been gated yet has no gate result.
    op.execute("UPDATE messages SET gate_errors = NULL WHERE step = 2 AND status = 'pending'")
    op.drop_constraint(op.f("ck_messages_step"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_status"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_kind"), "messages", "kind IN ('first','followup','reply')"
    )
    op.create_check_constraint(op.f("ck_messages_step"), "messages", NEW_STEP)
    op.create_check_constraint(op.f("ck_messages_status"), "messages", NEW_MSG_STATUS)
    op.add_column("messages", sa.Column("mailbox_id", sa.Uuid(), nullable=True))
    op.add_column("messages", sa.Column("message_id_header", sa.String(length=255), nullable=True))
    op.add_column("messages", sa.Column("in_reply_to", sa.String(length=255), nullable=True))
    op.add_column("messages", sa.Column("references_header", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "messages",
        sa.Column("send_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "messages", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("messages", sa.Column("last_send_error", sa.String(length=300), nullable=True))
    op.add_column(
        "messages",
        sa.Column("send_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("messages", sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("bounce_detail", sa.String(length=300), nullable=True))
    op.create_foreign_key(
        op.f("fk_messages_mailbox_id_mailboxes"),
        "messages",
        "mailboxes",
        ["mailbox_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_messages_status_kind_next_attempt_at",
        "messages",
        ["status", "kind", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "uq_messages_message_id_header",
        "messages",
        ["message_id_header"],
        unique=True,
        postgresql_where=sa.text("message_id_header IS NOT NULL"),
    )

    op.create_table(
        "suppressions",
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=300), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "reason IN ('unsubscribed','bounced','manual')", name=op.f("ck_suppressions_reason")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_suppressions_created_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_suppressions_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppressions")),
        sa.UniqueConstraint("address", name=op.f("uq_suppressions_address")),
    )

    op.add_column(
        "email_patterns", sa.Column("bounced_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """DESTRUCTIVE. Never use on a production database.

    Permanently deletes every reply message and the whole suppressions table, maps
    sending/unconfirmed -> cancelled, failed -> rejected and sequence replied -> stopped, and
    replaces NULL gate_errors with []. None of this can be restored by upgrading again.
    """
    op.drop_column("email_patterns", "bounced_at")
    op.drop_table("suppressions")

    # Rows that only exist in the new world cannot satisfy the old checks.
    op.execute("DELETE FROM messages WHERE kind = 'reply'")
    op.execute("UPDATE messages SET status = 'cancelled' WHERE status IN ('sending','unconfirmed')")
    op.execute("UPDATE messages SET status = 'rejected' WHERE status = 'failed'")
    op.execute("UPDATE sequences SET status = 'stopped' WHERE status = 'replied'")
    op.execute("UPDATE messages SET gate_errors = '[]'::jsonb WHERE gate_errors IS NULL")
    op.drop_index("uq_messages_message_id_header", table_name="messages")
    op.drop_index("ix_messages_status_kind_next_attempt_at", table_name="messages")
    op.drop_constraint(op.f("fk_messages_mailbox_id_mailboxes"), "messages", type_="foreignkey")
    for column in (
        "bounce_detail",
        "bounced_at",
        "send_snapshot",
        "last_send_error",
        "next_attempt_at",
        "send_attempts",
        "claimed_at",
        "references_header",
        "in_reply_to",
        "message_id_header",
        "mailbox_id",
    ):
        op.drop_column("messages", column)
    op.drop_constraint(op.f("ck_messages_status"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_step"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_kind"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_step"), "messages", "step IN (1,2)")
    op.create_check_constraint(op.f("ck_messages_status"), "messages", OLD_MSG_STATUS)
    op.alter_column("messages", "gate_errors", nullable=False)
    op.drop_column("messages", "kind")

    op.drop_constraint(op.f("ck_sequences_status"), "sequences", type_="check")
    op.create_check_constraint(op.f("ck_sequences_status"), "sequences", OLD_SEQ_STATUS)
    op.drop_constraint(op.f("fk_sequences_mailbox_id_mailboxes"), "sequences", type_="foreignkey")
    op.drop_column("sequences", "mailbox_id")
    op.drop_constraint(op.f("fk_campaigns_mailbox_id_mailboxes"), "campaigns", type_="foreignkey")
    op.drop_column("campaigns", "mailbox_id")
    op.drop_table("mailboxes")
