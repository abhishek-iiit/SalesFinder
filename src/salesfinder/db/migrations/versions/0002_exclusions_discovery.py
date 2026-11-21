"""exclusions_discovery

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07 17:36:07.670273
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "known_relationships",
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column("hubspot_object", sa.String(length=16), nullable=False),
        sa.Column("hubspot_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("lifecycle_stage", sa.String(length=64), nullable=True),
        sa.Column("owner_name", sa.String(length=200), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
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
            "hubspot_object IN ('company','contact')",
            name=op.f("ck_known_relationships_hubspot_object"),
        ),
        sa.CheckConstraint("kind IN ('domain','email')", name=op.f("ck_known_relationships_kind")),
        sa.CheckConstraint("source IN ('hubspot')", name=op.f("ck_known_relationships_source")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_known_relationships")),
        sa.UniqueConstraint(
            "source", "kind", "value", name="uq_known_relationships_source_kind_value"
        ),
    )
    op.create_index(
        op.f("ix_known_relationships_value"), "known_relationships", ["value"], unique=False
    )
    op.create_table(
        "discovery_jobs",
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("queries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pages_per_query", sa.Integer(), nullable=False),
        sa.Column("estimated_searches", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("searches_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "actual_cost_usd",
            sa.Numeric(precision=10, scale=4),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("results_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("companies_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("companies_existing", sa.Integer(), server_default="0", nullable=False),
        sa.Column("companies_excluded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("results_dropped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint("side IN ('buyer','supplier')", name=op.f("ck_discovery_jobs_side")),
        sa.CheckConstraint(
            "status IN ('estimated','approved','running','succeeded','failed',"
            "'stopped_budget','cancelled')",
            name=op.f("ck_discovery_jobs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"], ["users.id"], name=op.f("fk_discovery_jobs_approved_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_discovery_jobs_created_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_jobs")),
    )
    op.create_index(
        "ix_discovery_jobs_status_approved_at",
        "discovery_jobs",
        ["status", "approved_at"],
        unique=False,
    )
    op.create_table(
        "do_not_contact",
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("value", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("added_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('domain','email')", name=op.f("ck_do_not_contact_kind")),
        sa.ForeignKeyConstraint(
            ["added_by_id"], ["users.id"], name=op.f("fk_do_not_contact_added_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_do_not_contact")),
        sa.UniqueConstraint("kind", "value", name="uq_do_not_contact_kind_value"),
    )
    op.create_table(
        "hubspot_syncs",
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("companies_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("contacts_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed')",
            name=op.f("ck_hubspot_syncs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["users.id"], name=op.f("fk_hubspot_syncs_requested_by_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hubspot_syncs")),
    )
    op.create_index(
        "ix_hubspot_syncs_status_finished_at",
        "hubspot_syncs",
        ["status", "finished_at"],
        unique=False,
    )
    op.create_table(
        "companies",
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("website_url", sa.String(length=2048), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("region", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=16), server_default="serper", nullable=False),
        sa.Column("discovered_by_job_id", sa.Uuid(), nullable=True),
        sa.Column("discovery_query", sa.String(length=500), nullable=True),
        sa.Column("snippet", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=32), nullable=True),
        sa.Column("exclusion_detail", sa.String(length=300), nullable=True),
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
        sa.CheckConstraint("side IN ('buyer','supplier')", name=op.f("ck_companies_side")),
        sa.CheckConstraint("status IN ('new','excluded')", name=op.f("ck_companies_status")),
        sa.ForeignKeyConstraint(
            ["discovered_by_job_id"],
            ["discovery_jobs.id"],
            name=op.f("fk_companies_discovered_by_job_id_discovery_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("side", "domain", name="uq_companies_side_domain"),
    )
    op.create_index("ix_companies_side_status", "companies", ["side", "status"], unique=False)
    op.create_table(
        "discovery_drops",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["discovery_jobs.id"],
            name=op.f("fk_discovery_drops_job_id_discovery_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_drops")),
    )
    op.create_index(op.f("ix_discovery_drops_job_id"), "discovery_drops", ["job_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_discovery_drops_job_id"), table_name="discovery_drops")
    op.drop_table("discovery_drops")
    op.drop_index("ix_companies_side_status", table_name="companies")
    op.drop_table("companies")
    op.drop_index("ix_hubspot_syncs_status_finished_at", table_name="hubspot_syncs")
    op.drop_table("hubspot_syncs")
    op.drop_table("do_not_contact")
    op.drop_index("ix_discovery_jobs_status_approved_at", table_name="discovery_jobs")
    op.drop_table("discovery_jobs")
    op.drop_index(op.f("ix_known_relationships_value"), table_name="known_relationships")
    op.drop_table("known_relationships")
