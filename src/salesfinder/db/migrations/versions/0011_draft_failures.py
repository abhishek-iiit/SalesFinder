"""draft_failures

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13 00:16:08.074986

Per-company drafting failures per campaign, so drafting backs off from a company it keeps
failing on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "draft_failures",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("failures", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name="fk_draft_failures_campaign_id_campaigns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_draft_failures_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("campaign_id", "company_id", name="pk_draft_failures"),
    )
    op.create_index("ix_draft_failures_company_id", "draft_failures", ["company_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_draft_failures_company_id", table_name="draft_failures")
    op.drop_table("draft_failures")
