"""contacts_freshness

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08 04:16:30.930536

Published addresses remember when a profile last saw them and whether a later complete profile
no longer did (``stale``: unsendable unless a teammate allows it). Companies record how many
people pages the last profile read and whether extracting people from them crashed. Indexes for
the address -> person join and the verifier's daily-cap count. Existing rows: never stale, not
seen yet (``last_seen_at`` NULL until the next profile), 0 pages read, no crash.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("people_pages_read", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "companies",
        sa.Column(
            "contacts_extract_failed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_addresses",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "email_addresses",
        sa.Column("stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        op.f("ix_email_addresses_person_id"), "email_addresses", ["person_id"], unique=False
    )
    op.create_index(
        op.f("ix_email_addresses_verified_at"), "email_addresses", ["verified_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_addresses_verified_at"), table_name="email_addresses")
    op.drop_index(op.f("ix_email_addresses_person_id"), table_name="email_addresses")
    op.drop_column("email_addresses", "stale")
    op.drop_column("email_addresses", "last_seen_at")
    op.drop_column("companies", "contacts_extract_failed")
    op.drop_column("companies", "people_pages_read")
