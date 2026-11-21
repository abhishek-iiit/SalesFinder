"""profile_failures

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07 23:23:54.351233

Consecutive failed / no_site profiles per company, so a transient failure is retried after a
short backoff (1 day, then 7 days, then profiling.refresh_after_days) instead of a full refresh
period. Existing rows start at 0.

profile_retry_at: a company held back by the profiling circuit breaker (our network looked
down) isn't claimed again before this time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("profile_failures", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "companies",
        sa.Column("profile_retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "profile_retry_at")
    op.drop_column("companies", "profile_failures")
