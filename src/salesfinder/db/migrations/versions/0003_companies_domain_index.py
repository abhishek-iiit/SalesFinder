"""companies_domain_index

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07 18:46:23.675491

The exclusion sweep joins companies to do_not_contact / known_relationships by domain alone
(the existing unique index is on (side, domain)).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_companies_domain", "companies", ["domain"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_companies_domain", table_name="companies")
