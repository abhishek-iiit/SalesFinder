"""audit log index by action and target

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14 08:00:00.000000

The sender reads ``audit_log`` by (action, target_id): whether a message was ever
``message.unconfirmed`` (it may have gone) and when its MX wait started. Index it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_log_action_target_id", "audit_log", ["action", "target_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_action_target_id", table_name="audit_log")
