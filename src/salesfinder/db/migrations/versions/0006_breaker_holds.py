"""breaker_holds

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08 00:21:46.001737

Consecutive profiling circuit-breaker holds per company: at the cap a genuinely dead site is
recorded as unreachable instead of being held again. Existing rows start at 0.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("profile_holds", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("companies", "profile_holds")
