from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.cohorts.evaluate import (
    cohort_is_stale,
    condition_holds,
    count_matches,
    create_cohort,
    delete_cohort,
    list_matches,
    matches,
    unconfirmed_conditions,
    update_cohort,
    was_profiled,
)
from salesfinder.cohorts.model import CohortDefinition, Condition
from salesfinder.db.models import Cohort, Company, Observation, User
from salesfinder.signals import settings as sig

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_condition_validation() -> None:
    with pytest.raises(ValidationError):
        Condition(code="USES_CATALOG_CHEMICAL", op="lte", number=1)
    Condition(code="NEAR_WAREHOUSE", op="lte", field="miles", number=500)
    with pytest.raises(ValidationError):
        Condition(code="NEAR_WAREHOUSE", op="lte", field="km", number=500)
    with pytest.raises(ValidationError):
        Condition(code="NEAR_WAREHOUSE", op="lte", field="miles")
    with pytest.raises(ValidationError):
        Condition(code="USES_CATALOG_CHEMICAL", op="contains_any")
    with pytest.raises(ValidationError):
        Condition(code="NOPE", op="present")
    with pytest.raises(ValidationError):
        Condition(code="COMPANY_TYPE", op="present", bogus=1)  # type: ignore[call-arg]
    Condition(code="HIRING_PROCUREMENT", op="is_true")
    Condition(code="STATED_CAPACITY", op="gte", field="mt_per_year", number=100)
    with pytest.raises(ValidationError):
        Condition(code="US_LOCATION", op="is_true")
    with pytest.raises(ValidationError):
        CohortDefinition(side="buyer", must=[Condition(code="EXPORTS", op="is_true")])
    with pytest.raises(ValidationError):
        CohortDefinition(side="buyer", min_score=101)


def test_holds_semantics() -> None:
    any_ = Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles", "capb"])
    assert condition_holds(any_, ["capb"])
    assert not condition_holds(any_, [])
    assert not condition_holds(any_, None)
    all_ = Condition(code="USES_CATALOG_CHEMICAL", op="contains_all", values=["sles", "capb"])
    assert condition_holds(all_, ["CAPB", "sles", "x"])
    assert not condition_holds(all_, ["capb"])
    absent = Condition(code="USES_CATALOG_CHEMICAL", op="absent")
    assert condition_holds(absent, None) and condition_holds(absent, [])
    assert not condition_holds(absent, ["sles"])
    near = Condition(code="NEAR_WAREHOUSE", op="lte", field="miles", number=500)
    assert condition_holds(near, {"warehouse": "Houston, TX", "miles": 320})
    assert not condition_holds(near, {"warehouse": "Houston, TX", "miles": 800})
    assert not condition_holds(near, {"found": False})
    assert not condition_holds(near, None)
    present = Condition(code="COMPANY_TYPE", op="present")
    assert not condition_holds(present, "unknown")
    assert condition_holds(present, "manufacturer")
    assert condition_holds(
        Condition(code="COMPANY_TYPE", op="in", values=["Distributor"]), "distributor"
    )
    hiring_t = Condition(code="HIRING_PROCUREMENT", op="is_true")
    hiring_f = Condition(code="HIRING_PROCUREMENT", op="is_false")
    assert condition_holds(hiring_t, {"hiring": True, "roles": ["buyer"]})
    assert not condition_holds(hiring_t, {"found": False})
    assert condition_holds(hiring_f, {"hiring": False})
    assert not condition_holds(
        Condition(code="HIRING_PROCUREMENT", op="present"), {"hiring": False}
    )
    assert condition_holds(Condition(code="EXPORTS", op="is_true"), True)
    assert condition_holds(Condition(code="EXPORTS", op="is_false"), False)
    assert not condition_holds(Condition(code="EXPORTS", op="is_false"), None)
    cap = Condition(code="STATED_CAPACITY", op="gte", field="mt_per_year", number=1000)
    assert condition_holds(cap, {"text": "x", "mt_per_year": 1200})
    assert not condition_holds(cap, {"text": "x", "mt_per_year": None})


def test_matches_score_and_disqualified() -> None:
    chem = Condition(code="USES_CATALOG_CHEMICAL", op="present")
    d = CohortDefinition(side="buyer", must=[chem], min_score=40)
    latest = {"USES_CATALOG_CHEMICAL": ["sles"]}
    assert matches(d, latest=latest, score=40, disqualified=False, profiled=True)
    assert not matches(d, latest=latest, score=39, disqualified=False, profiled=True)
    assert not matches(d, latest=latest, score=None, disqualified=False, profiled=True)
    assert not matches(d, latest=latest, score=90, disqualified=True, profiled=True)
    d2 = d.model_copy(update={"include_disqualified": True})
    assert matches(d2, latest=latest, score=90, disqualified=True, profiled=True)
    d3 = CohortDefinition(
        side="buyer",
        must=[chem],
        must_not=[Condition(code="COMPANY_TYPE", op="in", values=["distributor"])],
    )
    assert matches(d3, latest=latest, score=None, disqualified=False, profiled=True)
    assert not matches(
        d3,
        latest={**latest, "COMPANY_TYPE": "distributor"},
        score=1,
        disqualified=False,
        profiled=True,
    )


async def _company(
    db: AsyncSession,
    name: str,
    *,
    side: str = "buyer",
    score: int | None = None,
    status: str = "new",
    profile_status: str = "ok",
    obs: dict[str, Any] | None = None,
) -> Company:
    c = Company(
        side=side,
        name=name,
        domain=f"{name.lower().replace(' ', '')}.com",
        website_url=f"https://{name.lower().replace(' ', '')}.com",
        status=status,
        profile_status=profile_status,
        score=score,
    )
    db.add(c)
    await db.flush()
    for code, value in (obs or {}).items():
        db.add(
            Observation(company_id=c.id, code=code, value=value, observed_at=NOW, is_latest=True)
        )
    await db.flush()
    return c


SLES_NOT_DIST = CohortDefinition(
    side="buyer",
    must=[Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"])],
    must_not=[Condition(code="COMPANY_TYPE", op="in", values=["distributor"])],
)


async def test_count_and_list_live(db: AsyncSession, admin: User) -> None:
    await sig.confirm_free(db, actor=admin)
    chem = {"USES_CATALOG_CHEMICAL": ["sles"]}
    await _company(db, "Alpha", score=50, obs={**chem, "COMPANY_TYPE": "manufacturer"})
    await _company(db, "Bravo", score=80, obs=chem)
    await _company(db, "Charlie", score=90, obs={**chem, "COMPANY_TYPE": "distributor"})
    await _company(db, "Delta", score=70, obs={"USES_CATALOG_CHEMICAL": ["capb"]})
    await _company(db, "Echo", score=99, status="excluded", obs=chem)
    await _company(
        db, "Foxtrot", side="supplier", score=99, obs={"MAKES_CATALOG_CHEMICAL": ["sles"]}
    )
    assert await count_matches(db, SLES_NOT_DIST) == 2
    rows, total = await list_matches(db, SLES_NOT_DIST)
    assert [c.name for c in rows] == ["Bravo", "Alpha"] and total == 2
    page, total = await list_matches(db, SLES_NOT_DIST, limit=1, offset=1)
    assert [c.name for c in page] == ["Alpha"] and total == 2
    # Unscored sorts last, then by name.
    await _company(db, "Golf", obs=chem)
    await _company(db, "Able", obs=chem)
    rows, total = await list_matches(db, SLES_NOT_DIST)
    assert [c.name for c in rows] == ["Bravo", "Alpha", "Able", "Golf"]
    assert total == await count_matches(db, SLES_NOT_DIST) == 4


async def test_unconfirmed_code_counts_as_absent(db: AsyncSession, admin: User) -> None:
    await sig.set_state(db, "USES_CATALOG_CHEMICAL", "confirmed", actor=admin)
    await _company(
        db, "Alpha", obs={"USES_CATALOG_CHEMICAL": ["sles"], "COMPANY_TYPE": "distributor"}
    )
    # COMPANY_TYPE is not confirmed, so the must_not never sees the distributor.
    assert await count_matches(db, SLES_NOT_DIST) == 1
    absent = CohortDefinition(side="buyer", must=[Condition(code="COMPANY_TYPE", op="absent")])
    assert await count_matches(db, absent) == 1
    await sig.set_state(db, "USES_CATALOG_CHEMICAL", "skipped", actor=admin)
    assert await count_matches(db, SLES_NOT_DIST) == 0


async def test_latest_observation_wins(db: AsyncSession, admin: User) -> None:
    await sig.confirm_free(db, actor=admin)
    c = await _company(db, "Alpha")
    db.add(
        Observation(
            company_id=c.id,
            code="USES_CATALOG_CHEMICAL",
            value=["sles"],
            observed_at=NOW,
            is_latest=True,
        )
    )
    await db.flush()
    assert await count_matches(db, SLES_NOT_DIST) == 1
    await db.execute(
        update(Observation).where(Observation.company_id == c.id).values(is_latest=False)
    )
    db.add(
        Observation(
            company_id=c.id,
            code="USES_CATALOG_CHEMICAL",
            value=[],
            observed_at=NOW + timedelta(days=1),
            is_latest=True,
        )
    )
    await db.flush()
    assert await count_matches(db, SLES_NOT_DIST) == 0


async def test_cohort_crud_unique_name(db: AsyncSession, admin: User) -> None:
    a = await create_cohort(db, name="  SLES users ", definition=SLES_NOT_DIST, actor=admin)
    assert a.name == "SLES users" and a.side == "buyer"
    assert CohortDefinition.model_validate(a.definition) == SLES_NOT_DIST
    with pytest.raises(ValueError, match="already exists"):
        await create_cohort(db, name="sles users", definition=SLES_NOT_DIST, actor=admin)
    for bad in ("   ", "x" * 121):
        with pytest.raises(ValueError, match="1 to 120"):
            await create_cohort(db, name=bad, definition=SLES_NOT_DIST, actor=admin)
    b = await create_cohort(db, name="Other", definition=SLES_NOT_DIST, actor=admin)
    with pytest.raises(ValueError, match="already exists"):
        await update_cohort(db, b.id, name="SLES users", definition=SLES_NOT_DIST, actor=admin)
    new = SLES_NOT_DIST.model_copy(update={"min_score": 10})
    await update_cohort(db, a.id, name="SLES users", definition=new, actor=admin)
    assert a.definition["min_score"] == 10
    await delete_cohort(db, b.id)
    assert await db.get(Cohort, b.id) is None


def test_numeric_condition_with_a_number_too_big_for_a_float() -> None:
    cap = Condition(code="STATED_CAPACITY", op="gte", field="mt_per_year", number=1000)
    assert not condition_holds(cap, {"text": "x", "mt_per_year": 10**400})


def test_unconfirmed_conditions_and_stale() -> None:
    chem = Condition(code="USES_CATALOG_CHEMICAL", op="contains_any", values=["sles"])
    dist = Condition(code="COMPANY_TYPE", op="in", values=["distributor"])
    certs = Condition(code="QUALITY_CERTS", op="present")
    d = CohortDefinition(side="buyer", must=[chem, certs], must_not=[dist])
    assert unconfirmed_conditions(d, frozenset({"USES_CATALOG_CHEMICAL"})) == [certs, dist]
    assert cohort_is_stale(d, frozenset({"USES_CATALOG_CHEMICAL"}))
    every = frozenset({"USES_CATALOG_CHEMICAL", "COMPANY_TYPE", "QUALITY_CERTS"})
    assert unconfirmed_conditions(d, every) == []
    assert not cohort_is_stale(d, every)
    assert not cohort_is_stale(CohortDefinition(side="buyer", min_score=10), frozenset())


def test_matches_only_profiled_companies_by_default() -> None:
    d = CohortDefinition(side="buyer")
    assert matches(d, latest={}, score=None, disqualified=False, profiled=True)
    assert not matches(d, latest={}, score=None, disqualified=False, profiled=False)
    wide = d.model_copy(update={"include_unprofiled": True})
    assert matches(wide, latest={}, score=None, disqualified=False, profiled=False)


def test_stored_definitions_without_the_new_flag_still_load() -> None:
    stored = {"side": "buyer", "must": [], "must_not": [], "min_score": None}
    stored["include_disqualified"] = False
    assert CohortDefinition.model_validate(stored).include_unprofiled is False


async def test_unprofiled_companies_excluded_unless_asked(db: AsyncSession, admin: User) -> None:
    await sig.confirm_free(db, actor=admin)
    for status in ("ok", "partial", "pending", "failed", "blocked", "no_site"):
        await _company(db, f"Co {status}", profile_status=status)
    not_dist = CohortDefinition(
        side="buyer", must_not=[Condition(code="COMPANY_TYPE", op="in", values=["distributor"])]
    )
    rows, total = await list_matches(db, not_dist)
    assert sorted(c.name for c in rows) == ["Co ok", "Co partial"] and total == 2
    wide = not_dist.model_copy(update={"include_unprofiled": True})
    assert await count_matches(db, wide) == 6


async def test_companies_read_before_keep_matching_while_reprofiled(
    db: AsyncSession, admin: User
) -> None:
    """Confirming a signal queues every profiled company again (pending), and a failed re-read
    keeps the earlier findings: neither drops a company that was read before out of cohorts."""
    await sig.confirm_free(db, actor=admin)
    for status in ("pending", "failed", "no_site", "blocked"):
        c = await _company(db, f"Read {status}", profile_status=status)
        c.scored_at = NOW
    await _company(db, "Never read", profile_status="pending")
    await db.flush()
    rows, total = await list_matches(db, CohortDefinition(side="buyer"))
    assert total == 4 and "Never read" not in {c.name for c in rows}
    assert was_profiled(rows[0])


def test_require_sendable_contact_filters() -> None:
    d = CohortDefinition(side="buyer", require_sendable_contact=True)
    kw: dict[str, Any] = {"latest": {}, "score": None, "disqualified": False, "profiled": True}
    assert not matches(d, **kw)
    assert not matches(d, sendable_contacts=0, **kw)
    assert matches(d, sendable_contacts=1, **kw)
    # Off by default: contact counts are irrelevant.
    assert matches(CohortDefinition(side="buyer"), sendable_contacts=0, **kw)


def test_require_sendable_contact_defaults_off_for_stored_definitions() -> None:
    stored = {"side": "buyer", "must": [], "must_not": [], "include_unprofiled": False}
    assert CohortDefinition.model_validate(stored).require_sendable_contact is False


async def test_require_sendable_contact_counts(db: AsyncSession) -> None:
    for name, sendable in (("a", 0), ("b", 2), ("c", 1)):
        db.add(
            Company(
                side="buyer",
                status="new",
                name=name,
                domain=f"{name}.com",
                website_url=f"https://{name}.com",
                profile_status="ok",
                sendable_contacts=sendable,
            )
        )
    await db.flush()
    assert await count_matches(db, CohortDefinition(side="buyer")) == 3
    only = CohortDefinition(side="buyer", require_sendable_contact=True)
    assert await count_matches(db, only) == 2
    # The "read at least once" default still applies on top.
    db.add(
        Company(
            side="buyer",
            status="new",
            name="d",
            domain="d.com",
            website_url="https://d.com",
            profile_status="pending",
            sendable_contacts=3,
        )
    )
    await db.flush()
    assert await count_matches(db, only) == 2
