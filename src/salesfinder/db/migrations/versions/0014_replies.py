"""replies

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-13 18:00:00.000000

Reading the mailboxes: the IMAP cursor per folder, conversations, the mail read
(``mailbox_messages``), and on ``messages`` the inbound mail a reply answers
(``reply_to_id``) and the teammate who wrote a reply themselves (``written_by_id``).
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with the constants in db/models/mail.py.
FOLDER = "folder IN ('inbox','sent')"
CONVERSATION_STATUS = "status IN ('open','needs_you','handled','taken_over')"
INTENT = (
    "intent IN ('interested','objection','not_now','wrong_person','unsubscribe',"
    "'auto_reply','other')"
)
KIND = (
    "kind IN ('reply','auto_reply','bounce','soft_bounce','unclear_bounce','unsubscribe',"
    "'human_sent','unreadable')"
)
MATCHED_BY = "matched_by IN ('header','address')"


def _timestamps() -> list[sa.Column[Any]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "mailbox_folders",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("folder", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_uid", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("failed_uid", sa.BigInteger(), nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(FOLDER, name=op.f("ck_mailbox_folders_folder")),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mailboxes.id"],
            name=op.f("fk_mailbox_folders_mailbox_id_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mailbox_id", "folder", name=op.f("pk_mailbox_folders")),
    )

    op.create_table(
        "conversations",
        sa.Column("sequence_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("needs_you_reason", sa.String(length=200), nullable=True),
        sa.Column("intent", sa.String(length=16), nullable=True),
        sa.Column("summary", sa.String(length=300), nullable=True),
        sa.Column(
            "products",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("assigned_to_id", sa.Uuid(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by_id", sa.Uuid(), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taken_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(CONVERSATION_STATUS, name=op.f("ck_conversations_status")),
        sa.CheckConstraint(INTENT, name=op.f("ck_conversations_intent")),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["sequences.id"],
            name=op.f("fk_conversations_sequence_id_sequences"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mailboxes.id"],
            name=op.f("fk_conversations_mailbox_id_mailboxes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name=op.f("fk_conversations_assigned_to_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["handled_by_id"],
            ["users.id"],
            name=op.f("fk_conversations_handled_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("sequence_id", name=op.f("uq_conversations_sequence_id")),
    )
    op.create_index(
        "ix_conversations_status_last_inbound_at",
        "conversations",
        ["status", "last_inbound_at"],
        unique=False,
    )

    op.create_table(
        "mailbox_messages",
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("folder", sa.String(length=8), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=False),
        sa.Column("uid", sa.BigInteger(), nullable=False),
        sa.Column("message_id_header", sa.String(length=255), nullable=True),
        sa.Column("in_reply_to", sa.String(length=255), nullable=True),
        sa.Column("references_header", sa.Text(), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=True),
        sa.Column(
            "to_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("matched_by", sa.String(length=8), nullable=True),
        sa.Column("sequence_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classify_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(FOLDER, name=op.f("ck_mailbox_messages_folder")),
        sa.CheckConstraint(KIND, name=op.f("ck_mailbox_messages_kind")),
        sa.CheckConstraint(MATCHED_BY, name=op.f("ck_mailbox_messages_matched_by")),
        sa.CheckConstraint(INTENT, name=op.f("ck_mailbox_messages_intent")),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mailboxes.id"],
            name=op.f("fk_mailbox_messages_mailbox_id_mailboxes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["sequences.id"],
            name=op.f("fk_mailbox_messages_sequence_id_sequences"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_mailbox_messages_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_mailbox_messages_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mailbox_messages")),
        sa.UniqueConstraint(
            "mailbox_id",
            "folder",
            "uidvalidity",
            "uid",
            name="uq_mailbox_messages_mailbox_id_folder_uidvalidity_uid",
        ),
    )
    op.create_index(
        "ix_mailbox_messages_mailbox_id_folder_message_id_header",
        "mailbox_messages",
        ["mailbox_id", "folder", "message_id_header"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mailbox_messages_sequence_id"), "mailbox_messages", ["sequence_id"], unique=False
    )
    op.create_index(
        op.f("ix_mailbox_messages_conversation_id"),
        "mailbox_messages",
        ["conversation_id"],
        unique=False,
    )

    op.add_column("messages", sa.Column("reply_to_id", sa.Uuid(), nullable=True))
    op.add_column("messages", sa.Column("written_by_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_messages_reply_to_id_mailbox_messages"),
        "messages",
        "mailbox_messages",
        ["reply_to_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_messages_written_by_id_users"),
        "messages",
        "users",
        ["written_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """DESTRUCTIVE. Never use on a production database: every conversation and every stored
    inbound mail (with the replies' text) is deleted, and replies lose their link to it."""
    op.drop_constraint(op.f("fk_messages_written_by_id_users"), "messages", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_messages_reply_to_id_mailbox_messages"), "messages", type_="foreignkey"
    )
    op.drop_column("messages", "written_by_id")
    op.drop_column("messages", "reply_to_id")
    op.drop_index(op.f("ix_mailbox_messages_conversation_id"), table_name="mailbox_messages")
    op.drop_index(op.f("ix_mailbox_messages_sequence_id"), table_name="mailbox_messages")
    op.drop_index(
        "ix_mailbox_messages_mailbox_id_folder_message_id_header", table_name="mailbox_messages"
    )
    op.drop_table("mailbox_messages")
    op.drop_index("ix_conversations_status_last_inbound_at", table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("mailbox_folders")
