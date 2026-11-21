"""Latest observed value per (company, signal code): read from the ``is_latest`` flag."""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Company, Observation


async def latest_values(
    db: AsyncSession,
    codes: Iterable[str],
    company_ids: Sequence[uuid.UUID] | None = None,
    *,
    side: str | None = None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """company id -> {code: latest observed value} for ``codes`` (``WHERE is_latest``) over
    non-excluded companies (or only ``company_ids``), optionally only one ``side``."""
    codes = list(codes)
    if not codes:
        return {}
    stmt = select(Observation.company_id, Observation.code, Observation.value).where(
        Observation.code.in_(codes), Observation.is_latest
    )
    if company_ids is not None:
        stmt = stmt.where(Observation.company_id.in_(company_ids))
        if side is not None:
            stmt = stmt.join(Company, Company.id == Observation.company_id).where(
                Company.side == side
            )
    else:
        stmt = stmt.join(Company, Company.id == Observation.company_id).where(
            Company.status != "excluded"
        )
        if side is not None:
            stmt = stmt.where(Company.side == side)
    latest: dict[uuid.UUID, dict[str, Any]] = defaultdict(dict)
    for company_id, code, value in (await db.execute(stmt)).all():
        latest[company_id][code] = value
    return latest
