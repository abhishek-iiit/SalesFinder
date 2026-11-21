"""campaign_name_ci

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13 12:00:00.000000

Campaign names unique regardless of case: a unique index on lower(name). The upgrade refuses,
naming the clashing names, if campaigns that differ only in case already exist; rename them
and run it again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_campaigns_lower_name"


def case_duplicates_message(names: Sequence[str]) -> str:
    listed = ", ".join(repr(name) for name in names)
    return (
        "Can't make campaign names unique regardless of case: these campaigns differ only in "
        f"case: {listed}. Rename them (e.g. on the Campaigns page), then run the upgrade again."
    )


def upgrade() -> None:
    clashes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT name FROM campaigns WHERE lower(name) IN ("
                " SELECT lower(name) FROM campaigns GROUP BY lower(name) HAVING count(*) > 1"
                ') ORDER BY lower(name), name COLLATE "C"'
            )
        )
        .scalars()
        .all()
    )
    if clashes:
        raise RuntimeError(case_duplicates_message(list(clashes)))
    op.create_index(INDEX, "campaigns", [sa.text("lower(name)")], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX, table_name="campaigns")
