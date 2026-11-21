"""Discovery jobs end to end: estimate, budget, approve, claim, run. No network: FakeSerper."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fakes.serper import FAKE_KEY, FakeSerper, organic
from salesfinder.catalog import load_catalog
from salesfinder.config import DiscoveryConfig, _read_yaml
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AppSetting, Company, DiscoveryDrop, DiscoveryJob, User
from salesfinder.discovery import budget, jobs
from salesfinder.discovery.serper import SearchResult, SerperClient, SerperError
from salesfinder.exclusions import dnc
from salesfinder.exclusions.check import Exclusion

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CFG = DiscoveryConfig.model_validate(_read_yaml(CONFIG_DIR / "salesfinder.yaml")["discovery"])
CAT = load_catalog(CONFIG_DIR)

Q1, Q2, Q3 = (
    '"sodium laureth sulfate" shampoo manufacturer Texas',
    '"sodium laureth sulfate" skin care manufacturer Texas',
    '"sodium laureth sulfate" cosmetics manufacturer Texas',
)
FIVE_SUPPLIER_REGIONS = ["Gujarat", "Maharashtra", "Tamil Nadu", "Telangana", "Thailand"]


class Sleeper:
    """Records requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _now() -> datetime:
    return NOW


async def _estimate(
    db: AsyncSession,
    user: User,
    *,
    side: str = "buyer",
    products: tuple[str, ...] = ("sles",),
    industries: tuple[str, ...] = ("personal_care",),
    regions: tuple[str, ...] = ("Texas",),
    config: DiscoveryConfig = CFG,
) -> DiscoveryJob:
    job = await jobs.create_estimate(
        db,
        side=side,
        product_ids=list(products),
        industries=list(industries),
        region_names=list(regions),
        created_by=user,
        catalog=CAT,
        config=config,
    )
    await db.commit()
    return job


async def _approved(
    db: AsyncSession,
    user: User,
    *,
    approved_at: datetime = NOW,
    config: DiscoveryConfig = CFG,
    **kw: Any,
) -> DiscoveryJob:
    job = await _estimate(db, user, config=config, **kw)
    await jobs.approve_job(db, job.id, approved_by=user, now=approved_at, config=config)
    await db.commit()
    return job


def _supplier(regions: list[str] = FIVE_SUPPLIER_REGIONS) -> dict[str, Any]:
    return {"side": "supplier", "products": ("capb",), "industries": (), "regions": tuple(regions)}


async def _run(
    engine: AsyncEngine,
    job_id: uuid.UUID,
    fake: FakeSerper,
    *,
    key: str = FAKE_KEY,
    sleeper: Sleeper | None = None,
    config: DiscoveryConfig = CFG,
) -> DiscoveryJob:
    async with SerperClient(key, transport=fake.transport, sleep=sleeper or Sleeper()) as client:
        result = await jobs.run_job(
            make_session_factory(engine),
            job_id,
            client=client,
            catalog=CAT,
            config=config,
            now=_now,
        )
    assert not fake.violations, fake.violations
    return result


async def _claim_and_run(
    engine: AsyncEngine, db: AsyncSession, fake: FakeSerper, **kw: Any
) -> DiscoveryJob:
    claimed = await jobs.claim_next_job(db, now=NOW)
    assert claimed is not None
    await db.commit()
    return await _run(engine, claimed.id, fake, **kw)


async def _fresh_job(engine: AsyncEngine, job_id: uuid.UUID) -> DiscoveryJob:
    async with make_session_factory(engine)() as s:
        job = await s.get(DiscoveryJob, job_id)
        assert job is not None
        return job


async def _companies(engine: AsyncEngine, side: str | None = None) -> dict[str, Company]:
    async with make_session_factory(engine)() as s:
        stmt = select(Company)
        if side:
            stmt = stmt.where(Company.side == side)
        return {(c.domain if side else f"{c.side}:{c.domain}"): c for c in await s.scalars(stmt)}


def _always(results: list[dict[str, Any]]) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    return lambda payload: results


# --- estimate, approve, run --------------------------------------------------------------------


async def test_estimate_approve_run_happy_path(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await dnc.add_entry(db, raw="beta.com", reason="Asked us to stop", added_by=admin)
    job = await _estimate(db, admin)
    assert job.status == "estimated"
    assert job.side == "buyer"
    assert job.params == {
        "products": ["sles"],
        "industries": ["personal_care"],
        "regions": ["Texas"],
    }
    assert job.queries[0] == {"q": Q1, "gl": "us", "region": "Texas", "country": "US"}
    assert [q["q"] for q in job.queries] == [Q1, Q2, Q3]
    assert (job.pages_per_query, job.estimated_searches) == (1, 3)
    assert job.estimated_cost_usd == Decimal("0.0030")
    assert job.created_by_id == admin.id

    approved = await jobs.approve_job(db, job.id, approved_by=admin, now=NOW, config=CFG)
    await db.commit()
    assert approved.status == "approved"
    assert (approved.approved_by_id, approved.approved_at) == (admin.id, NOW)

    fake = FakeSerper(
        {
            Q1: [
                organic(
                    "https://www.acme.com/products/sles",
                    "Acme Chemicals - Surfactants | Home",
                    "We make shampoo.",
                    1,
                ),
                organic("https://www.indiamart.com/proddetail/sles.html", "SLES best price", "", 2),
                organic("https://acme.com/about", "About Acme", "", 3),
                organic("https://shop.beta.com/", "Beta Corp \u2013 Cosmetics", "Beta snippet", 4),
            ],
            Q2: [],
            Q3: [],
        }
    )
    result = await _claim_and_run(engine, db, fake)

    assert result.status == "succeeded"
    assert result.error is None
    assert (result.started_at, result.finished_at) == (NOW, NOW)
    assert result.companies_new == 1
    assert result.companies_excluded == 1
    assert result.results_dropped == 1
    assert result.companies_existing == 1
    assert result.results_seen == 4
    assert result.searches_done == 3
    assert result.actual_cost_usd == result.searches_done * Decimal("0.001")

    stored = await _fresh_job(engine, job.id)
    assert stored.status == "succeeded"
    assert stored.actual_cost_usd == Decimal("0.0030")
    assert (stored.companies_new, stored.companies_existing) == (1, 1)

    companies = await _companies(engine, "buyer")
    assert set(companies) == {"acme.com", "beta.com"}
    acme = companies["acme.com"]
    assert acme.status == "new"
    assert acme.name == "Acme Chemicals"
    assert acme.website_url == "https://www.acme.com"
    assert (acme.country, acme.region) == ("US", "Texas")
    assert acme.discovery_query == Q1
    assert acme.snippet == "We make shampoo."
    assert acme.source == "serper"
    assert acme.discovered_by_job_id == job.id
    assert acme.exclusion_reason is None
    beta = companies["beta.com"]
    assert beta.status == "excluded"
    assert beta.name == "Beta Corp"
    assert beta.website_url == "https://shop.beta.com"
    assert beta.exclusion_reason == "do_not_contact"
    assert beta.exclusion_detail == "Do-not-contact: beta.com — Asked us to stop"

    async with make_session_factory(engine)() as s:
        drops = list(await s.scalars(select(DiscoveryDrop)))
    assert [(d.reason, d.domain, d.job_id) for d in drops] == [
        ("marketplace", "indiamart.com", job.id)
    ]
    assert drops[0].url == "https://www.indiamart.com/proddetail/sles.html"

    assert fake.payloads == [
        {"q": Q1, "gl": "us", "num": 10, "page": 1},
        {"q": Q2, "gl": "us", "num": 10, "page": 1},
        {"q": Q3, "gl": "us", "num": 10, "page": 1},
    ]
    assert all(c.headers["x-api-key"] == FAKE_KEY for c in fake.calls)


async def test_pages_per_query_requests_each_page(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    two = CFG.model_copy(update={"pages_per_query": 2})
    job = await _approved(db, admin, config=two, **_supplier(["Gujarat"]))
    assert (job.pages_per_query, job.estimated_searches) == (2, 2)
    fake = FakeSerper(_always([]))
    result = await _claim_and_run(engine, db, fake, config=two)
    assert result.status == "succeeded"
    assert [p["page"] for p in fake.payloads] == [1, 2]
    assert result.searches_done == 2
    assert result.actual_cost_usd == Decimal("0.0020")


async def test_existing_company_same_side_not_duplicated_other_side_separate(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    db.add(
        Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
        )
    )
    await db.commit()

    await _approved(db, admin)  # 3 buyer queries
    hits = [organic("https://acme.com/x", "Acme"), organic("https://gamma.com/", "Gamma Inc")]
    buyer = await _claim_and_run(engine, db, FakeSerper(_always(hits)))
    assert buyer.status == "succeeded"
    # acme: existing in all 3 searches; gamma: new once, then existing within the same job.
    assert (buyer.companies_new, buyer.companies_existing) == (1, 5)
    assert buyer.results_seen == 6
    buyers = await _companies(engine, "buyer")
    assert set(buyers) == {"acme.com", "gamma.com"}
    assert buyers["acme.com"].name == "Acme"  # untouched

    await _approved(db, admin, **_supplier(["Gujarat"]))
    supplier = await _claim_and_run(engine, db, FakeSerper(_always(hits[:1])))
    assert (supplier.companies_new, supplier.companies_existing) == (1, 0)
    suppliers = await _companies(engine, "supplier")
    assert suppliers["acme.com"].country == "IN"
    assert suppliers["acme.com"].region == "Gujarat"
    async with make_session_factory(engine)() as s:
        total = await s.scalar(select(func.count()).select_from(Company))
    assert total == 3


async def test_concurrent_insert_of_same_domain_counts_as_existing(
    engine: AsyncEngine, db: AsyncSession, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another job inserts the same (side, domain) between our check and our insert."""

    async def racing_exclusion_for(session: AsyncSession, *, domain: str) -> Exclusion | None:
        async with make_session_factory(engine)() as other, other.begin():
            other.add(
                Company(
                    side="buyer",
                    name="Racer",
                    domain=domain,
                    website_url=f"https://{domain}",
                    status="new",
                )
            )
        return None

    monkeypatch.setattr(jobs, "exclusion_for", racing_exclusion_for)
    await _approved(db, admin, regions=("Texas",))
    fake = FakeSerper({Q1: [organic("https://delta.com", "Delta")], Q2: [], Q3: []})
    result = await _claim_and_run(engine, db, fake)
    assert result.status == "succeeded"
    assert (result.companies_new, result.companies_existing) == (0, 1)
    companies = await _companies(engine, "buyer")
    assert [c.name for c in companies.values()] == ["Racer"]


async def test_name_parsing_and_field_limits(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    long_snippet = "s" * 1500
    hits = [
        organic("https://one.com", "  One Co | Products - More  ", long_snippet),
        organic("https://two.com", "Two Co \u2013 Home - Ignored"),
        organic("https://three.com", "", ""),
        organic("https://four.com", "F" * 400),
    ]
    await _approved(db, admin, **_supplier(["Gujarat"]))
    result = await _claim_and_run(engine, db, FakeSerper(_always(hits)))
    assert result.companies_new == 4
    companies = await _companies(engine, "supplier")
    assert companies["one.com"].name == "One Co"
    assert companies["one.com"].snippet == "s" * 1000
    assert companies["two.com"].name == "Two Co"
    assert companies["three.com"].name == "three.com"  # empty title falls back to the domain
    assert companies["three.com"].snippet is None
    assert companies["four.com"].name == "F" * 300


# --- budget ------------------------------------------------------------------------------------


async def _job_row(
    db: AsyncSession,
    user: User,
    status: str,
    *,
    estimate: str = "0",
    actual: str = "0",
    started_at: datetime | None = None,
) -> DiscoveryJob:
    job = DiscoveryJob(
        side="buyer",
        params={},
        queries=[],
        pages_per_query=1,
        estimated_searches=1,
        estimated_cost_usd=Decimal(estimate),
        actual_cost_usd=Decimal(actual),
        status=status,
        created_by_id=user.id,
        started_at=started_at,
    )
    db.add(job)
    await db.flush()
    return job


async def test_budget_settings_and_month_spend(db: AsyncSession, admin: User) -> None:
    assert await budget.get_budget(db, CFG) == Decimal("20.00")
    await budget.set_budget(db, Decimal("12.5"), actor=admin)
    assert await budget.get_budget(db, CFG) == Decimal("12.5")
    await budget.set_budget(db, Decimal("7"), actor=admin)  # upsert
    await db.commit()
    assert await budget.get_budget(db, CFG) == Decimal("7")
    row = await db.get(AppSetting, budget.BUDGET_KEY)
    assert row is not None and row.updated_by_id == admin.id
    assert budget.BUDGET_KEY == "discovery.monthly_budget_usd"
    await budget.set_budget(db, Decimal("0"), actor=admin)
    await budget.set_budget(db, Decimal("10000"), actor=admin)
    for bad in (Decimal("-0.01"), Decimal("10000.01"), Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError) as info:
            await budget.set_budget(db, bad, actor=admin)
        assert str(info.value) == "Enter a budget between $0 and $10,000."
    await budget.set_budget(db, Decimal("7"), actor=admin)

    this_month = NOW - timedelta(days=3)
    last_month = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    await _job_row(db, admin, "running", actual="0.0010", started_at=this_month)
    await _job_row(db, admin, "succeeded", actual="0.0020", started_at=this_month)
    await _job_row(db, admin, "failed", actual="0.0005", started_at=this_month)
    await _job_row(db, admin, "stopped_budget", actual="0.0003", started_at=this_month)
    await _job_row(db, admin, "approved", estimate="0.0100")
    await _job_row(db, admin, "estimated", estimate="1.0000")
    await _job_row(db, admin, "cancelled", estimate="1.0000")
    await _job_row(db, admin, "succeeded", actual="5.0000", started_at=last_month)
    await db.commit()

    spend = await budget.month_spend(db, now=NOW)
    assert spend == Decimal("0.0138")
    assert isinstance(spend, Decimal)
    assert await budget.remaining_budget(db, now=NOW, config=CFG) == Decimal("6.9862")
    # December starts a new month: only the approved (not yet run) job still counts.
    assert await budget.month_spend(db, now=datetime(2026, 12, 1, tzinfo=UTC)) == Decimal("0.01")

    await budget.set_budget(db, Decimal("0.01"), actor=admin)
    assert await budget.remaining_budget(db, now=NOW, config=CFG) == Decimal("0")


async def test_approve_over_budget_refused(db: AsyncSession, admin: User) -> None:
    await budget.set_budget(db, Decimal("0.05"), actor=admin)
    await db.commit()
    pricey = CFG.model_copy(update={"serper_price_per_1000_usd": Decimal("60")})
    job = await _estimate(db, admin, config=pricey, **_supplier(["Gujarat"]))
    assert job.estimated_cost_usd == Decimal("0.0600")
    job_id = job.id
    with pytest.raises(ValueError) as info:
        await jobs.approve_job(db, job_id, approved_by=admin, now=NOW, config=pricey)
    assert str(info.value) == "This job needs $0.0600; $0.05 of this month's $0.05 budget is left."
    await db.rollback()
    stored = await db.get(DiscoveryJob, job_id, populate_existing=True)
    assert stored is not None
    assert (stored.status, stored.approved_at) == ("estimated", None)


async def test_two_approvals_cannot_overspend(db: AsyncSession, admin: User) -> None:
    await budget.set_budget(db, Decimal("0.10"), actor=admin)
    await db.commit()
    pricey = CFG.model_copy(update={"serper_price_per_1000_usd": Decimal("60")})
    first = await _estimate(db, admin, config=pricey, **_supplier(["Gujarat"]))
    second = await _estimate(db, admin, config=pricey, **_supplier(["Maharashtra"]))
    first_id = first.id
    await jobs.approve_job(db, first_id, approved_by=admin, now=NOW, config=pricey)
    await db.commit()
    with pytest.raises(ValueError) as info:
        await jobs.approve_job(db, second.id, approved_by=admin, now=NOW, config=pricey)
    assert str(info.value) == "This job needs $0.0600; $0.04 of this month's $0.10 budget is left."
    await db.rollback()
    await db.refresh(admin)
    # Approving again, or approving a job that isn't waiting for approval, is refused.
    with pytest.raises(ValueError) as info:
        await jobs.approve_job(db, first_id, approved_by=admin, now=NOW, config=pricey)
    assert str(info.value) == "Only an estimated job can be approved (this one is approved)."
    await db.rollback()
    await db.refresh(admin)
    with pytest.raises(LookupError):
        await jobs.approve_job(db, uuid.uuid4(), approved_by=admin, now=NOW, config=pricey)
    await db.rollback()


async def test_concurrent_approvals_are_serialized(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await budget.set_budget(db, Decimal("0.10"), actor=admin)
    await db.commit()
    pricey = CFG.model_copy(update={"serper_price_per_1000_usd": Decimal("60")})
    a = await _estimate(db, admin, config=pricey, **_supplier(["Gujarat"]))
    b = await _estimate(db, admin, config=pricey, **_supplier(["Maharashtra"]))
    factory = make_session_factory(engine)

    async def approve(job_id: uuid.UUID) -> str:
        async with factory() as s:
            try:
                await jobs.approve_job(s, job_id, approved_by=admin, now=NOW, config=pricey)
                await asyncio.sleep(0.05)  # hold the lock while the other approval waits
                await s.commit()
            except ValueError as exc:
                await s.rollback()
                return str(exc)
            return "approved"

    outcomes = await asyncio.gather(approve(a.id), approve(b.id))
    assert sorted(outcomes) == [
        "This job needs $0.0600; $0.04 of this month's $0.10 budget is left.",
        "approved",
    ]


async def test_run_stops_at_budget(engine: AsyncEngine, db: AsyncSession, admin: User) -> None:
    job = await _approved(db, admin, **_supplier())  # 5 searches, approved under $20
    assert job.estimated_searches == 5
    await budget.set_budget(db, Decimal("0.002"), actor=admin)  # lowered after approval
    await db.commit()
    fake = FakeSerper(_always([organic("https://acme.com", "Acme")]))
    result = await _claim_and_run(engine, db, fake)
    assert result.status == "stopped_budget"
    assert result.searches_done == 2
    assert len(fake.calls) == 2
    assert result.actual_cost_usd == Decimal("0.0020")
    assert result.finished_at == NOW
    assert result.error == (
        "Stopped after 2 of 5 searches: the next one would exceed this month's budget "
        "($0.0020 was available for this job)."
    )
    stored = await _fresh_job(engine, job.id)
    assert (stored.status, stored.searches_done) == ("stopped_budget", 2)
    assert stored.actual_cost_usd == Decimal("0.0020")
    assert (stored.companies_new, stored.companies_existing) == (1, 1)


async def test_run_budget_counts_other_spend_not_own_estimate(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    """Other jobs' spend this month counts; this job's own estimate is never held against it."""
    await _job_row(db, admin, "succeeded", actual="0.0010", started_at=NOW)
    await budget.set_budget(db, Decimal("0.0065"), actor=admin)
    await db.commit()
    job = await _approved(db, admin, **_supplier())  # needs 0.0050; 0.0055 left
    # Run it straight from "approved" (unclaimed), where month_spend counts its estimate:
    # the run must not hold that estimate against itself (that would allow only 0 searches).
    fake = FakeSerper(_always([]))
    result = await _run(engine, job.id, fake)
    assert result.status == "succeeded"
    assert result.searches_done == 5
    assert result.started_at == NOW

    # A second identical job now has 0.0005 left -> stops before its first search.
    await budget.set_budget(db, Decimal("0.0110"), actor=admin)  # 0.0110 - 0.0060 = 0.0050
    await db.commit()
    second = await _approved(db, admin, **_supplier())
    await budget.set_budget(db, Decimal("0.0080"), actor=admin)  # 0.0080 - 0.0060 = 0.0020
    await db.commit()
    stopped = await _claim_and_run(engine, db, FakeSerper(_always([])))
    assert stopped.id == second.id
    assert (stopped.status, stopped.searches_done) == ("stopped_budget", 2)


# --- Serper failures ---------------------------------------------------------------------------


async def test_serper_401_fails_job_with_reason(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    job = await _approved(db, admin)
    fake = FakeSerper(_always([]))
    result = await _claim_and_run(engine, db, fake, key="wrong-key-do-not-store")
    assert result.status == "failed"
    assert result.error == "Serper rejected the API key."
    assert result.finished_at == NOW
    assert (result.searches_done, result.actual_cost_usd) == (0, Decimal("0"))
    assert len(fake.calls) == 1  # never retried
    stored = await _fresh_job(engine, job.id)
    assert stored.status == "failed"
    assert "wrong-key" not in (stored.error or "")


async def test_serper_500_retries_then_fails_keeps_spend(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    job = await _approved(db, admin)
    fake = FakeSerper(
        _always([organic("https://acme.com", "Acme")]), status_by_call=[200, 500, 500, 500, 500]
    )
    sleeper = Sleeper()
    result = await _claim_and_run(engine, db, fake, sleeper=sleeper)
    assert result.status == "failed"
    assert result.error == "Serper unavailable after 3 retries (HTTP 500)."
    assert result.searches_done == 1
    assert result.actual_cost_usd == Decimal("0.0010")
    assert result.companies_new == 1
    assert result.finished_at == NOW
    assert len(fake.calls) == 5
    assert sleeper.delays == [1, 2, 4]
    stored = await _fresh_job(engine, job.id)
    assert (stored.status, stored.actual_cost_usd) == ("failed", Decimal("0.0010"))


async def test_malformed_and_empty_organic(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await _approved(db, admin)
    bad = FakeSerper(lambda payload: httpx.Response(200, text="<html>maintenance</html>"))
    failed = await _claim_and_run(engine, db, bad)
    assert failed.status == "failed"
    assert failed.error == "Serper returned an unexpected response."
    assert failed.searches_done == 0
    assert failed.finished_at == NOW

    await _approved(db, admin)
    empty = FakeSerper(lambda payload: httpx.Response(200, json={}))
    ok = await _claim_and_run(engine, db, empty)
    assert ok.status == "succeeded"
    assert (ok.results_seen, ok.searches_done) == (0, 3)
    assert ok.actual_cost_usd == Decimal("0.0030")


async def test_unexpected_error_fails_job_and_keeps_spend(
    engine: AsyncEngine, db: AsyncSession, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def boom(session: AsyncSession, *, domain: str) -> Exclusion | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("secret detail that must not be stored")
        return None

    monkeypatch.setattr(jobs, "exclusion_for", boom)
    job = await _approved(db, admin)
    fake = FakeSerper(
        {Q1: [organic("https://a1.com", "A1")], Q2: [organic("https://a2.com", "A2")], Q3: []}
    )
    result = await _claim_and_run(engine, db, fake)
    assert result.status == "failed"
    assert result.error == "Discovery job failed unexpectedly (RuntimeError)."
    assert result.finished_at == NOW
    stored = await _fresh_job(engine, job.id)
    # Both searches were paid for; only the first search's companies were saved.
    assert (stored.searches_done, stored.actual_cost_usd) == (2, Decimal("0.0020"))
    assert set(await _companies(engine, "buyer")) == {"a1.com"}
    assert stored.companies_new == 1


async def test_run_job_refuses_jobs_that_are_not_runnable(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    job = await _estimate(db, admin)
    fake = FakeSerper(_always([]))
    with pytest.raises(ValueError) as info:
        await _run(engine, job.id, fake)
    assert str(info.value) == "Only an approved job can run (this one is estimated)."
    assert fake.calls == []
    with pytest.raises(LookupError):
        await _run(engine, uuid.uuid4(), fake)


async def test_run_stops_when_job_ended_elsewhere(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    """If the reaper marks the job failed mid-run, the run records spend and stops."""
    job = await _approved(db, admin)

    def reaped_after_first(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    fake = FakeSerper(reaped_after_first)
    claimed = await jobs.claim_next_job(db, now=NOW)
    assert claimed is not None
    await db.commit()

    class ReapingClient(SerperClient):
        async def search(self, q: str, *, gl: str, page: int) -> list[SearchResult]:
            results = await super().search(q, gl=gl, page=page)
            async with make_session_factory(engine)() as s, s.begin():
                await s.execute(
                    update(DiscoveryJob)
                    .where(DiscoveryJob.id == job.id)
                    .values(status="failed", error="Interrupted (worker stopped mid-job).")
                )
            return results

    async with ReapingClient(FAKE_KEY, transport=fake.transport, sleep=Sleeper()) as client:
        result = await jobs.run_job(
            make_session_factory(engine), job.id, client=client, catalog=CAT, config=CFG, now=_now
        )
    assert result.status == "failed"
    assert result.error == "Interrupted (worker stopped mid-job)."
    assert len(fake.calls) == 1
    stored = await _fresh_job(engine, job.id)
    assert (stored.searches_done, stored.actual_cost_usd) == (1, Decimal("0.0010"))


async def test_run_stops_starting_searches_when_the_worker_stops(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    """Ruling P7-G: once the worker is stopping no new search starts; the job ends failed
    with the reason, keeping the search already paid for."""
    job = await _approved(db, admin)
    fake = FakeSerper(lambda payload: [])
    stop = asyncio.Event()

    class StoppingClient(SerperClient):
        async def search(self, q: str, *, gl: str, page: int) -> list[SearchResult]:
            results = await super().search(q, gl=gl, page=page)
            stop.set()  # SIGTERM arrives during the first search
            return results

    async with StoppingClient(FAKE_KEY, transport=fake.transport, sleep=Sleeper()) as client:
        result = await jobs.run_job(
            make_session_factory(engine),
            job.id,
            client=client,
            catalog=CAT,
            config=CFG,
            now=_now,
            stop=stop,
        )
    assert result.status == "failed"
    assert result.error == jobs.STOPPED_FOR_SHUTDOWN.format(done=1, total=3)
    assert len(fake.calls) == 1
    stored = await _fresh_job(engine, job.id)
    assert (stored.searches_done, stored.actual_cost_usd) == (1, Decimal("0.0010"))


# --- claim, reaper, cancel ----------------------------------------------------------------------


async def test_claim_skip_locked_and_interrupted(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await _estimate(db, admin)  # never claimed: not approved
    newer = await _approved(db, admin, approved_at=NOW)
    older = await _approved(db, admin, approved_at=NOW - timedelta(minutes=5))
    factory = make_session_factory(engine)
    async with factory() as s1, factory() as s2:
        first = await jobs.claim_next_job(s1, now=NOW)
        second = await jobs.claim_next_job(s2, now=NOW)  # the oldest is locked by s1: skipped
        assert first is not None and second is not None
        assert (first.id, second.id) == (older.id, newer.id)
        assert (first.status, first.started_at) == ("running", NOW)
        await s1.commit()
        await s2.commit()
    async with factory() as s3:
        assert await jobs.claim_next_job(s3, now=NOW) is None

    async with factory() as s, s.begin():
        await s.execute(
            update(DiscoveryJob)
            .where(DiscoveryJob.id == older.id)
            .values(started_at=NOW - timedelta(hours=2))
        )
        await s.execute(
            update(DiscoveryJob)
            .where(DiscoveryJob.id == newer.id)
            .values(started_at=NOW - timedelta(minutes=10))
        )
    assert await jobs.fail_interrupted_jobs(db, now=NOW) == 1
    await db.commit()
    reaped = await _fresh_job(engine, older.id)
    assert reaped.status == "failed"
    assert reaped.error == "Interrupted (worker stopped mid-job)."
    assert reaped.finished_at == NOW
    assert (await _fresh_job(engine, newer.id)).status == "running"
    assert await jobs.fail_interrupted_jobs(db, now=NOW, older_than=timedelta(minutes=5)) == 1
    await db.commit()
    assert (await _fresh_job(engine, newer.id)).status == "failed"


async def test_cancel_only_before_running(db: AsyncSession, admin: User) -> None:
    estimated = await _estimate(db, admin)
    cancelled = await jobs.cancel_job(db, estimated.id, actor=admin, now=NOW)
    await db.commit()
    assert (cancelled.status, cancelled.finished_at) == ("cancelled", NOW)

    approved = await _approved(db, admin)
    assert await budget.month_spend(db, now=NOW) == Decimal("0.0030")
    await jobs.cancel_job(db, approved.id, actor=admin, now=NOW)
    await db.commit()
    assert await budget.month_spend(db, now=NOW) == Decimal("0")

    running = await _approved(db, admin)
    claimed = await jobs.claim_next_job(db, now=NOW)
    assert claimed is not None and claimed.id == running.id
    await db.commit()
    for job_id, status in ((running.id, "running"), (estimated.id, "cancelled")):
        with pytest.raises(ValueError) as info:
            await jobs.cancel_job(db, job_id, actor=admin, now=NOW)
        assert str(info.value) == (
            f"Only a job that hasn't started can be cancelled (this one is {status})."
        )
        await db.rollback()
    with pytest.raises(LookupError):
        await jobs.cancel_job(db, uuid.uuid4(), actor=admin)


# --- Serper client -------------------------------------------------------------------------------


def _client(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> SerperClient:
    return SerperClient(FAKE_KEY, transport=httpx.MockTransport(handler), **kw)


async def test_client_request_and_parsing() -> None:
    items: list[Any] = [
        organic("https://a.com/x", "A", "snip a", 1),
        "not a dict",
        {"title": "no link"},
        {"link": 42},
        {"link": "https://b.com"},
        {"link": "https://c.com", "title": None, "snippet": 7, "position": "x"},
    ]
    fake = FakeSerper({"q": items})
    async with SerperClient(FAKE_KEY, transport=fake.transport) as client:
        results = await client.search("q", gl="in", page=2)
        assert FAKE_KEY not in repr(client)
    assert fake.payloads == [{"q": "q", "gl": "in", "num": 10, "page": 2}]
    assert fake.calls[0].headers["x-api-key"] == FAKE_KEY
    assert results == [
        SearchResult(title="A", url="https://a.com/x", snippet="snip a", position=1),
        SearchResult(title="", url="https://b.com", snippet="", position=5),
        SearchResult(title="", url="https://c.com", snippet="", position=6),
    ]
    missing = FakeSerper(lambda payload: httpx.Response(200, json={"searchParameters": {}}))
    async with SerperClient(FAKE_KEY, transport=missing.transport) as client:
        assert await client.search("q", gl="us", page=1) == []


@pytest.mark.parametrize("status", [401, 403])
async def test_client_rejected_key(status: int) -> None:
    async with _client(lambda r: httpx.Response(status)) as client:
        with pytest.raises(SerperError) as info:
            await client.search("q", gl="us", page=1)
    assert info.value.reason == "Serper rejected the API key."
    assert info.value.retryable is False
    assert FAKE_KEY not in str(info.value)


async def test_client_other_status() -> None:
    async with _client(lambda r: httpx.Response(400, json={"message": "bad"})) as client:
        with pytest.raises(SerperError) as info:
            await client.search("q", gl="us", page=1)
    assert info.value.reason == "Serper request failed (HTTP 400)."


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"organic": "nope"}),
    ],
)
async def test_client_unexpected_response(response: httpx.Response) -> None:
    async with _client(lambda r: response) as client:
        with pytest.raises(SerperError) as info:
            await client.search("q", gl="us", page=1)
    assert info.value.reason == "Serper returned an unexpected response."
    assert info.value.__cause__ is None


@pytest.mark.parametrize(
    ("retry_after", "delays"), [("120", [60, 60]), ("2", [2, 2]), ("soon", [1, 2]), (None, [1, 2])]
)
async def test_client_429_retry_after(retry_after: str | None, delays: list[float]) -> None:
    fake = FakeSerper(_always([organic("https://a.com")]), [429, 429], retry_after=retry_after)
    sleeper = Sleeper()
    async with SerperClient(FAKE_KEY, transport=fake.transport, sleep=sleeper) as client:
        results = await client.search("q", gl="us", page=1)
    assert [r.url for r in results] == ["https://a.com"]
    assert sleeper.delays == delays


async def test_client_429_exhausted() -> None:
    sleeper = Sleeper()
    async with _client(lambda r: httpx.Response(429), sleep=sleeper) as client:
        with pytest.raises(SerperError) as info:
            await client.search("q", gl="us", page=1)
    assert info.value.reason == "Serper unavailable after 3 retries (HTTP 429)."
    assert sleeper.delays == [1, 2, 4]


async def test_client_transport_errors_retried() -> None:
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"organic": [organic("https://a.com")]})

    sleeper = Sleeper()
    async with _client(flaky, sleep=sleeper) as client:
        results = await client.search("q", gl="us", page=1)
    assert len(results) == 1
    assert sleeper.delays == [1, 2]

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    sleeper = Sleeper()
    async with _client(timeout, sleep=sleeper) as client:
        with pytest.raises(SerperError) as info:
            await client.search("q", gl="us", page=1)
    assert info.value.reason == "Could not reach Serper: ReadTimeout."
    assert info.value.retryable is False
    assert info.value.__cause__ is None
    assert sleeper.delays == [1, 2, 4]
    assert FAKE_KEY not in str(info.value)
