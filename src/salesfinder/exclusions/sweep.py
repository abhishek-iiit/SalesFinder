"""Re-exclude stored companies when the exclusion lists learn about them after discovery.

A company is checked once, when a discovery job stores it. HubSpot can learn about it later
(a sync adds its domain) and the do-not-contact list can change while a job is running, so
these set-based UPDATEs bring every not-yet-excluded company in line with the current lists.
Services flush; callers commit.

The detail strings must match ``check.exclusion_for`` exactly (see ``check._known_detail``).
"""

from sqlalchemy import ColumnElement, String, case, func, literal, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Company, DoNotContact, KnownRelationship

MAX_DETAIL = 300  # companies.exclusion_detail is String(300)


def _text(value: str) -> ColumnElement[str]:
    return literal(value, String)


def _clip(expr: ColumnElement[str]) -> ColumnElement[str]:
    return func.left(expr, MAX_DETAIL, type_=String)


async def exclude_do_not_contact(db: AsyncSession, *, value: str | None = None) -> int:
    """Exclude every non-excluded company whose domain is on the do-not-contact list (or only
    ``value``'s companies). Returns how many rows changed. Flushes."""
    dnc = (
        select(DoNotContact.value, DoNotContact.reason)
        .where(DoNotContact.kind == "domain")
        .subquery("dnc")
    )
    detail = _text("Do-not-contact: ") + dnc.c.value + _text(" — ") + dnc.c.reason
    stmt = (
        update(Company)
        .where(Company.domain == dnc.c.value, Company.status != "excluded")
        .values(
            status="excluded",
            exclusion_reason="do_not_contact",
            exclusion_detail=_clip(detail),
        )
        .returning(Company.id)
    )
    if value is not None:
        stmt = stmt.where(dnc.c.value == value)
    result = await db.execute(stmt.execution_options(synchronize_session=False))
    return len(result.scalars().all())


async def exclude_known_relationships(db: AsyncSession) -> int:
    """Exclude every non-excluded company whose domain HubSpot knows. Returns how many rows
    changed. Flushes."""
    # One row per domain, the oldest first, like check._known (several sources may know it).
    known = (
        select(
            KnownRelationship.value,
            KnownRelationship.hubspot_object,
            KnownRelationship.name,
            KnownRelationship.owner_name,
        )
        .where(KnownRelationship.kind == "domain")
        .ext(distinct_on(KnownRelationship.value))
        .order_by(KnownRelationship.value, KnownRelationship.created_at)
        .subquery("known")
    )
    # Mirrors check._known_detail: f"HubSpot {obj} {name or value}{f' (owner: {o})' if o}".
    obj = case((known.c.hubspot_object == "company", _text("company")), else_=_text("contact"))
    what = func.coalesce(func.nullif(known.c.name, ""), known.c.value, type_=String)
    owner = func.coalesce(
        _text(" (owner: ") + func.nullif(known.c.owner_name, "") + _text(")"),
        _text(""),
        type_=String,
    )
    detail = _text("HubSpot ") + obj + _text(" ") + what + owner
    result = await db.execute(
        update(Company)
        .where(Company.domain == known.c.value, Company.status != "excluded")
        .values(
            status="excluded",
            exclusion_reason="existing_relationship",
            exclusion_detail=_clip(detail),
        )
        .returning(Company.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.scalars().all())


async def sweep_exclusions(db: AsyncSession) -> int:
    """Apply both lists to stored companies; do-not-contact first, so it takes precedence.
    Returns how many companies changed. Flushes; the caller commits."""
    return await exclude_do_not_contact(db) + await exclude_known_relationships(db)
