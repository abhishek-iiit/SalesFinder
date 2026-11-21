"""profiling_signals

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07 21:45:21.866106
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cohorts",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("side IN ('buyer','supplier')", name=op.f("ck_cohorts_side")),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_cohorts_created_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cohorts")),
        sa.UniqueConstraint("name", name=op.f("uq_cohorts_name")),
    )
    op.create_table(
        "signal_settings",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('proposed','confirmed','skipped','rejected')",
            name=op.f("ck_signal_settings_state"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["users.id"], name=op.f("fk_signal_settings_updated_by_id_users")
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_signal_settings")),
    )
    op.create_table(
        "observations",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_url", sa.String(length=2048), nullable=True),
        sa.Column("evidence_snippet", sa.String(length=300), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=16), server_default="profiler", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_observations_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observations")),
    )
    op.create_index(
        "ix_observations_company_id_code_observed_at",
        "observations",
        ["company_id", "code", "observed_at"],
        unique=False,
    )
    op.add_column(
        "companies",
        sa.Column("profile_status", sa.String(length=16), server_default="pending", nullable=False),
    )
    op.add_column("companies", sa.Column("profile_error", sa.String(length=300), nullable=True))
    op.add_column("companies", sa.Column("profiled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "companies", sa.Column("pages_fetched", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("companies", sa.Column("score", sa.Integer(), nullable=True))
    op.add_column(
        "companies",
        sa.Column("disqualified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("companies", sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
    # Autogenerate does not emit check constraints added to an existing table.
    op.create_check_constraint(
        "profile_status",
        "companies",
        "profile_status IN ('pending','ok','partial','failed','blocked','no_site')",
    )
    op.create_index("ix_companies_side_score", "companies", ["side", "score"], unique=False)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_companies_profile_status"), "companies", type_="check")
    op.drop_index("ix_companies_side_score", table_name="companies")
    op.drop_column("companies", "scored_at")
    op.drop_column("companies", "disqualified")
    op.drop_column("companies", "score")
    op.drop_column("companies", "pages_fetched")
    op.drop_column("companies", "profiled_at")
    op.drop_column("companies", "profile_error")
    op.drop_column("companies", "profile_status")
    op.drop_index("ix_observations_company_id_code_observed_at", table_name="observations")
    op.drop_table("observations")
    op.drop_table("signal_settings")
    op.drop_table("cohorts")
