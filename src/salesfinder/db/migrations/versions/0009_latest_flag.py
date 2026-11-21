"""latest_flag

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08 07:54:03.051732

``observations.is_latest`` marks the newest observation per (company, code) so readers filter on
a partial index instead of running DISTINCT ON. Existing rows are backfilled: the newest by
``observed_at`` per (company, code) is latest, ties broken by the higher id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "observations",
        sa.Column("is_latest", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        """
        UPDATE observations SET is_latest = true
        WHERE id IN (
            SELECT DISTINCT ON (company_id, code) id
            FROM observations
            ORDER BY company_id, code, observed_at DESC, id DESC
        )
        """
    )
    op.create_index(
        "ix_observations_latest",
        "observations",
        ["company_id", "code"],
        unique=False,
        postgresql_where=sa.text("is_latest"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observations_latest",
        table_name="observations",
        postgresql_where=sa.text("is_latest"),
    )
    op.drop_column("observations", "is_latest")
