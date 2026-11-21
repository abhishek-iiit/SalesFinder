"""outreach

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-08 08:00:22.453295

Usage events, campaigns, sequences, messages and outbox feedback.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("cohort_id", sa.Uuid(), nullable=False),
        sa.Column("playbook", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("daily_draft_cap", sa.Integer(), server_default=sa.text("40"), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
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
        sa.CheckConstraint("playbook IN ('buyer','supplier')", name=op.f("ck_campaigns_playbook")),
        sa.CheckConstraint("side IN ('buyer','supplier')", name=op.f("ck_campaigns_side")),
        sa.CheckConstraint(
            "status IN ('draft','active','paused','archived')", name=op.f("ck_campaigns_status")
        ),
        sa.CheckConstraint("playbook = side", name=op.f("ck_campaigns_playbook_matches_side")),
        sa.ForeignKeyConstraint(
            ["cohort_id"],
            ["cohorts.id"],
            name=op.f("fk_campaigns_cohort_id_cohorts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_campaigns_created_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
        sa.UniqueConstraint("name", name=op.f("uq_campaigns_name")),
    )
    op.create_table(
        "usage_events",
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("task", sa.String(length=32), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_usage_events_campaign_id_campaigns"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    op.create_index(op.f("ix_usage_events_at"), "usage_events", ["at"], unique=False)
    op.create_table(
        "sequences",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("email_address_id", sa.Uuid(), nullable=True),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stop_reason", sa.String(length=300), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('awaiting_approval','active','completed','stopped','failed')",
            name=op.f("ck_sequences_status"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_sequences_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_sequences_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_address_id"],
            ["email_addresses.id"],
            name=op.f("fk_sequences_email_address_id_email_addresses"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_sequences_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sequences")),
        sa.UniqueConstraint(
            "campaign_id", "company_id", name="uq_sequences_campaign_id_company_id"
        ),
    )
    op.create_index(op.f("ix_sequences_campaign_id"), "sequences", ["campaign_id"], unique=False)
    op.create_index(
        "ix_sequences_recipient_status", "sequences", ["recipient", "status"], unique=False
    )
    op.create_table(
        "messages",
        sa.Column("sequence_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "gate_errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("edited", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by_id", sa.Uuid(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(length=500), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','gate_failed','awaiting_approval','approved','rejected',"
            "'cancelled','sent','bounced')",
            name=op.f("ck_messages_status"),
        ),
        sa.CheckConstraint("step IN (1,2)", name=op.f("ck_messages_step")),
        sa.ForeignKeyConstraint(
            ["approved_by_id"], ["users.id"], name=op.f("fk_messages_approved_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by_id"], ["users.id"], name=op.f("fk_messages_rejected_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["sequences.id"],
            name=op.f("fk_messages_sequence_id_sequences"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("sequence_id", "step", name="uq_messages_sequence_id_step"),
    )
    op.create_index(op.f("ix_messages_sequence_id"), "messages", ["sequence_id"], unique=False)
    op.create_index(
        "ix_messages_status_created_at", "messages", ["status", "created_at"], unique=False
    )
    op.create_table(
        "outbox_feedback",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_outbox_feedback_campaign_id_campaigns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_outbox_feedback_created_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_outbox_feedback_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_feedback")),
    )
    op.create_index(
        op.f("ix_outbox_feedback_campaign_id"), "outbox_feedback", ["campaign_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_feedback_campaign_id"), table_name="outbox_feedback")
    op.drop_table("outbox_feedback")
    op.drop_index("ix_messages_status_created_at", table_name="messages")
    op.drop_index(op.f("ix_messages_sequence_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_sequences_recipient_status", table_name="sequences")
    op.drop_index(op.f("ix_sequences_campaign_id"), table_name="sequences")
    op.drop_table("sequences")
    op.drop_index(op.f("ix_usage_events_at"), table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_table("campaigns")
