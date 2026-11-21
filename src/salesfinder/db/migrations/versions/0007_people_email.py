"""people_email

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08 02:45:56.463254

People found on company pages, their email addresses, learned address patterns, and the MX cache.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "domain_mx",
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column(
            "mx_hosts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "state IN ('has_mx','no_mail','unknown')", name=op.f("ck_domain_mx_state")
        ),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_domain_mx")),
    )
    op.create_table(
        "email_patterns",
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("pattern", sa.String(length=32), nullable=False),
        sa.Column("support", sa.Integer(), nullable=False),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_email_patterns")),
    )
    op.create_table(
        "people",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("name_key", sa.String(length=200), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("title_category", sa.String(length=24), nullable=False),
        sa.Column("is_target", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("source_snippet", sa.String(length=300), nullable=True),
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
            "title_category IN ('procurement','supply_chain','rnd','owner_exec',"
            "'export','business_dev','marketing','other')",
            name=op.f("ck_people_title_category"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_people_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
        sa.UniqueConstraint("company_id", "name_key", name="uq_people_company_id_name_key"),
    )
    op.create_index(op.f("ix_people_company_id"), "people", ["company_id"], unique=False)
    op.create_table(
        "email_addresses",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("pattern", sa.String(length=32), nullable=True),
        sa.Column("evidence_url", sa.String(length=2048), nullable=True),
        sa.Column("evidence_snippet", sa.String(length=300), nullable=True),
        sa.Column("verifier_result", sa.String(length=32), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.String(length=300), nullable=True),
        sa.Column("override_sendable", sa.Boolean(), nullable=True),
        sa.Column("override_by_id", sa.Uuid(), nullable=True),
        sa.Column("override_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("kind IN ('personal','role')", name=op.f("ck_email_addresses_kind")),
        sa.CheckConstraint(
            "source IN ('page','pattern','guess')", name=op.f("ck_email_addresses_source")
        ),
        sa.CheckConstraint(
            "status IN ('published','pattern_confirmed','pattern_guess',"
            "'verified','risky','invalid')",
            name=op.f("ck_email_addresses_status"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_email_addresses_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["override_by_id"], ["users.id"], name=op.f("fk_email_addresses_override_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_email_addresses_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_addresses")),
        sa.UniqueConstraint("company_id", "address", name="uq_email_addresses_company_id_address"),
    )
    op.create_index(
        op.f("ix_email_addresses_company_id"), "email_addresses", ["company_id"], unique=False
    )
    op.add_column("companies", sa.Column("contact_form_url", sa.String(length=2048), nullable=True))
    op.add_column(
        "companies", sa.Column("contacts_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "companies", sa.Column("contacts_found", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "companies",
        sa.Column("sendable_contacts", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("companies", "sendable_contacts")
    op.drop_column("companies", "contacts_found")
    op.drop_column("companies", "contacts_checked_at")
    op.drop_column("companies", "contact_form_url")
    op.drop_index(op.f("ix_email_addresses_company_id"), table_name="email_addresses")
    op.drop_table("email_addresses")
    op.drop_index(op.f("ix_people_company_id"), table_name="people")
    op.drop_table("people")
    op.drop_table("email_patterns")
    op.drop_table("domain_mx")
