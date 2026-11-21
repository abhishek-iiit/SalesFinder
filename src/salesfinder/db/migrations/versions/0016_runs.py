"""runs

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14 10:00:00.000000

The worker's heartbeat log: one ``runs`` row per heartbeat window of passes (what they did,
counts, failures) with the liveness beat. ``ix_runs_open`` finds the unfinished row a new
worker closes as ``crashed``; ``uq_runs_one_open`` allows at most one unfinished row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with RUN_END_REASONS in db/models/ops.py.
END_REASON = "end_reason IN ('window','shutdown','crashed','once')"
FINISHED_HAS_REASON = "(finished_at IS NULL) = (end_reason IS NULL)"


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_pass_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=12), nullable=True),
        sa.Column("passes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_steps", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "lines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("lines_dropped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "failures",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("quiet", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=True),
        sa.Column("step_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("code_version", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(END_REASON, name=op.f("ck_runs_end_reason")),
        sa.CheckConstraint(FINISHED_HAS_REASON, name=op.f("ck_runs_finished_has_reason")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_started_at", "runs", ["started_at"], unique=False)
    op.create_index(
        "ix_runs_open",
        "runs",
        ["started_at"],
        unique=False,
        postgresql_where=sa.text("finished_at IS NULL"),
    )
    # At most one open run (one worker at a time).
    op.create_index(
        "uq_runs_one_open",
        "runs",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    """Drops the heartbeat log (operational history only; no outreach state)."""
    op.drop_index(
        "uq_runs_one_open", table_name="runs", postgresql_where=sa.text("finished_at IS NULL")
    )
    op.drop_index(
        "ix_runs_open", table_name="runs", postgresql_where=sa.text("finished_at IS NULL")
    )
    op.drop_index("ix_runs_started_at", table_name="runs")
    op.drop_table("runs")
