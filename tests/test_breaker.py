"""Circuit-breaker hold cap: genuinely dead sites are recorded after repeated holds."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fakes.web import FakeWeb
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import Company
from salesfinder.profiling.fetch import SafeFetcher
from salesfinder.profiling.service import NETWORK_DOWN, ProfileOutcome, profile_batch
from salesfinder.signals import settings as sig
from salesfinder.worker import outcomes_line

T0 = datetime(2026, 11, 7, 12, tzinfo=UTC)
STEP = timedelta(minutes=16)
PUBLIC = "93.184.216.34"


class _Shipped:
    """The shipped config, catalog and facts, set per test by ``_shipped`` below: never loaded
    at import, and never merged with a developer's salesfinder.local.yaml."""

    config: AppConfig
    catalog: Catalog
    facts: Facts


SHIPPED = _Shipped()


@pytest.fixture(autouse=True)
def _shipped(shipped_config: AppConfig, shipped_catalog: Catalog, shipped_facts: Facts) -> None:
    SHIPPED.config, SHIPPED.catalog, SHIPPED.facts = shipped_config, shipped_catalog, shipped_facts


HELD_LINE = "Network looks down — will retry."


async def _confirm(db: AsyncSession) -> None:
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
    await db.commit()


async def _company(db: AsyncSession, domain: str, **fields: Any) -> uuid.UUID:
    company = Company(
        side="buyer",
        name=domain,
        domain=domain,
        website_url=f"https://{domain}",
        status="new",
        **fields,
    )
    db.add(company)
    await db.commit()
    return company.id


async def _row(engine: AsyncEngine, company_id: uuid.UUID) -> Company:
    async with make_session_factory(engine)() as s:
        row = await s.get(Company, company_id)
        assert row is not None
        return row


async def _batch(engine: AsyncEngine, web: FakeWeb, now: datetime) -> list[ProfileOutcome]:
    async with SafeFetcher(
        SHIPPED.config.profiling, transport=web.transport, resolver=web.resolver
    ) as fetcher:
        return await profile_batch(
            make_session_factory(engine),
            fetcher=fetcher,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=SHIPPED.config,
            now=lambda: now,
            limit=10,
        )


def _dead() -> FakeWeb:
    return FakeWeb({}, {})


async def test_dead_sites_recorded_after_three_holds(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db)
    ids = [await _company(db, d) for d in ("a.com", "b.com", "c.com")]
    web = _dead()

    for n in (1, 2):
        outcomes = await _batch(engine, web, T0 + STEP * (n - 1))
        assert outcomes_line(outcomes) == HELD_LINE
        for cid in ids:
            row = await _row(engine, cid)
            assert (row.profile_status, row.profile_holds, row.profile_failures) == (
                "pending",
                n,
                0,
            )

    outcomes = await _batch(engine, web, T0 + STEP * 2)
    assert [o.profile_status for o in outcomes] == ["no_site"] * 3
    assert outcomes_line(outcomes) == "Profiled 3 companies (0 ok, 3 failed)."
    for cid in ids:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profile_failures, row.profile_holds) == ("no_site", 1, 0)
        assert row.profile_retry_at is None


async def test_live_read_resets_holds(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db)
    flaky = await _company(db, "flaky.com")
    others = [await _company(db, d) for d in ("a.com", "b.com")]
    await _batch(engine, _dead(), T0)
    assert (await _row(engine, flaky)).profile_holds == 1

    up = FakeWeb({"https://flaky.com/": "<p>ISO 9001</p>"}, {"flaky.com": [PUBLIC]})
    outcomes = await _batch(engine, up, T0 + STEP)
    by_id = {o.company_id: o.profile_status for o in outcomes}
    assert by_id[flaky] == "ok"
    row = await _row(engine, flaky)
    assert (row.profile_status, row.profile_holds) == ("ok", 0)
    # The others failed to connect while a site was read: recorded, holds reset.
    for cid in others:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profile_holds) == ("no_site", 0)


async def test_cap_shrinks_breaker_below_three(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db)
    capped = [await _company(db, d, profile_holds=2) for d in ("old1.com", "old2.com")]
    fresh = [await _company(db, d) for d in ("new1.com", "new2.com")]

    outcomes = await _batch(engine, _dead(), T0)

    assert {o.profile_status for o in outcomes} == {"no_site"}
    assert NETWORK_DOWN not in outcomes_line(outcomes)
    for cid in [*capped, *fresh]:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profile_failures, row.profile_holds) == ("no_site", 1, 0)


async def test_real_outage_still_held(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db)
    ids = [await _company(db, f"s{i}.com") for i in range(5)]

    outcomes = await _batch(engine, _dead(), T0)

    assert outcomes_line(outcomes) == HELD_LINE
    for cid in ids:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profile_failures, row.profile_holds) == ("pending", 0, 1)


async def test_cap_records_dead_ones_while_outage_holds_the_rest(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db)
    dead = await _company(db, "dead.com", profile_holds=2)
    fresh = [await _company(db, f"s{i}.com") for i in range(3)]

    outcomes = await _batch(engine, _dead(), T0)

    assert (
        outcomes_line(outcomes) == f"{HELD_LINE} 1 recorded as unreachable after repeated checks."
    )
    row = await _row(engine, dead)
    assert (row.profile_status, row.profile_failures, row.profile_holds) == ("no_site", 1, 0)
    for cid in fresh:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profile_holds) == ("pending", 1)
