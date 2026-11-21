"""Evaluating cohort definitions over latest observations, and cohort CRUD.

Services flush; the caller commits."""

import uuid
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.cohorts.model import NUMERIC_FIELDS, CohortDefinition, Condition
from salesfinder.db.models import Cohort, Company, User
from salesfinder.profiling.scoring import is_present
from salesfinder.signals.latest import latest_values
from salesfinder.signals.settings import confirmed_codes

# Profile statuses of companies whose website was read.
PROFILED_STATUSES = frozenset({"ok", "partial"})


def was_profiled(company: Company) -> bool:
    """Whether profiling has read the company's website: it is ``ok`` / ``partial`` now, or was
    read earlier (it has a ``scored_at``) and is only being re-profiled (``pending``) or its
    last re-read failed, which keeps the earlier findings. A company never read has no
    findings at all, so a "must not" condition can't rule it out."""
    return company.profile_status in PROFILED_STATUSES or company.scored_at is not None


def _fold(items: list[Any]) -> set[str]:
    return {str(i).casefold() for i in items}


def _number(code: str, value: Any) -> float | None:
    if not isinstance(value, dict) or value.get("found") is False:
        return None
    n = value.get(NUMERIC_FIELDS[code])
    if isinstance(n, bool) or not isinstance(n, int | float):
        return None
    try:
        return float(n)
    except OverflowError:  # an int too big for a float: stored JSON, so be defensive
        return None


def condition_holds(cond: Condition, value: Any | None) -> bool:
    """Whether one condition holds for a latest value (``None`` = never observed / unconfirmed)."""
    present = value is not None and is_present(cond.code, value)
    match cond.op:
        case "absent":
            return not present
        case "present":
            return present
        case "contains_any" | "contains_all":
            if not isinstance(value, list):
                return False
            have, want = _fold(value), _fold(cond.values)
            return bool(have & want) if cond.op == "contains_any" else want <= have
        case "in":
            return isinstance(value, str) and value.casefold() in _fold(cond.values)
        case "is_true" | "is_false":
            want_true = cond.op == "is_true"
            flag = value.get("hiring") if isinstance(value, dict) else value
            return isinstance(flag, bool) and flag is want_true
        case "lte" | "gte":
            n = _number(cond.code, value)
            if n is None or cond.number is None:
                return False
            return n <= cond.number if cond.op == "lte" else n >= cond.number
    return False  # pragma: no cover


def matches(
    defn: CohortDefinition,
    *,
    latest: Mapping[str, Any],
    score: int | None,
    disqualified: bool,
    profiled: bool,
    sendable_contacts: int = 0,
) -> bool:
    """Whether a company matches. ``profiled``: see ``was_profiled``; companies not profiled
    only match a definition with ``include_unprofiled``."""
    if not profiled and not defn.include_unprofiled:
        return False
    if defn.require_sendable_contact and sendable_contacts <= 0:
        return False
    if disqualified and not defn.include_disqualified:
        return False
    if defn.min_score is not None and (score is None or score < defn.min_score):
        return False
    if not all(condition_holds(c, latest.get(c.code)) for c in defn.must):
        return False
    return not any(condition_holds(c, latest.get(c.code)) for c in defn.must_not)


def unconfirmed_conditions(defn: CohortDefinition, confirmed: Collection[str]) -> list[Condition]:
    """The definition's conditions (must, then must not) on signals that aren't ``confirmed``.
    Nothing collects those now, so each is evaluated as if never observed: a "must" one
    matches nobody (or everybody), a "must not" one excludes nobody (or everybody)."""
    return [c for c in [*defn.must, *defn.must_not] if c.code not in confirmed]


def cohort_is_stale(defn: CohortDefinition, confirmed: Collection[str]) -> bool:
    """Whether the cohort uses a signal that isn't confirmed now, so it doesn't select what it
    says. Activation (Phase 5) refuses a stale cohort."""
    return bool(unconfirmed_conditions(defn, confirmed))


async def load_latest(
    db: AsyncSession, *, side: str, confirmed: frozenset[str]
) -> dict[uuid.UUID, dict[str, Any]]:
    return await latest_values(db, confirmed, side=side)


async def _matching(db: AsyncSession, defn: CohortDefinition) -> list[Company]:
    confirmed = await confirmed_codes(db, defn.side)
    latest = await load_latest(db, side=defn.side, confirmed=confirmed)
    companies = (
        await db.scalars(
            select(Company)
            .where(Company.side == defn.side, Company.status != "excluded")
            .order_by(Company.score.desc().nulls_last(), Company.name, Company.id)
        )
    ).all()
    return [
        c
        for c in companies
        if matches(
            defn,
            latest=latest.get(c.id, {}),
            score=c.score,
            disqualified=c.disqualified,
            profiled=was_profiled(c),
            sendable_contacts=c.sendable_contacts,
        )
    ]


async def count_matches(db: AsyncSession, defn: CohortDefinition) -> int:
    return len(await _matching(db, defn))


async def list_matches(
    db: AsyncSession, defn: CohortDefinition, *, limit: int = 50, offset: int = 0
) -> tuple[list[Company], int]:
    found = await _matching(db, defn)
    return found[offset : offset + limit], len(found)


# --- cohort CRUD ---------------------------------------------------------------------------------


NAME_TAKEN = "A cohort with that name already exists."


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("A cohort name must be 1 to 120 characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("A cohort name can't contain control characters.")
    return name


@asynccontextmanager
async def _name_race() -> AsyncIterator[None]:
    """Another request can take the name between the pre-check and the flush: the unique
    constraint then fires inside the caller's savepoint (rolled back, the outer transaction
    stays usable) and is reported like the pre-check."""
    try:
        yield
    except IntegrityError as exc:
        if "uq_cohorts_name" in str(exc.orig):
            raise ValueError(NAME_TAKEN) from None
        raise


async def _name_taken(db: AsyncSession, name: str, *, except_id: uuid.UUID | None = None) -> bool:
    stmt = select(func.count()).select_from(Cohort).where(func.lower(Cohort.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(Cohort.id != except_id)
    return bool(await db.scalar(stmt))


async def create_cohort(
    db: AsyncSession, *, name: str, definition: CohortDefinition, actor: User
) -> Cohort:
    name = _clean_name(name)
    if await _name_taken(db, name):
        raise ValueError(NAME_TAKEN)
    cohort = Cohort(
        name=name,
        side=definition.side,
        definition=definition.model_dump(mode="json"),
        created_by_id=actor.id,
    )
    async with _name_race():
        async with db.begin_nested():
            db.add(cohort)
            await db.flush()
    return cohort


async def update_cohort(
    db: AsyncSession,
    cohort_id: uuid.UUID,
    *,
    name: str,
    definition: CohortDefinition,
    actor: User,
) -> Cohort:
    cohort = await db.get(Cohort, cohort_id)
    if cohort is None:
        raise ValueError("That cohort no longer exists.")
    name = _clean_name(name)
    if await _name_taken(db, name, except_id=cohort_id):
        raise ValueError(NAME_TAKEN)
    async with _name_race():
        async with db.begin_nested():
            cohort.name = name
            cohort.side = definition.side
            cohort.definition = definition.model_dump(mode="json")
            await db.flush()
    return cohort


async def delete_cohort(db: AsyncSession, cohort_id: uuid.UUID) -> None:
    cohort = await db.get(Cohort, cohort_id)
    if cohort is not None:
        await db.delete(cohort)
        await db.flush()
