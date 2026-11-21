"""Profiling service: fetch a company's site (FakeWeb), store observations, score. No network."""

import asyncio
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from fakes.web import FakeWeb, Site, Slow
from salesfinder import audit
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Company, Observation, User
from salesfinder.profiling import service
from salesfinder.profiling.fetch import FetchResult, SafeFetcher
from salesfinder.profiling.service import (
    MAX_DETECT_CHARS,
    NETWORK_DOWN,
    NETWORK_RETRY,
    ProfileOutcome,
    change_signal_states,
    profile_batch,
    profile_company,
    request_reprofile,
    rescore_all,
)
from salesfinder.signals import settings as sig
from salesfinder.signals.catalog import for_side
from salesfinder.signals.detect_common import Finding
from salesfinder.signals.latest import latest_values
from salesfinder.worker import outcomes_line

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


class _Shipped:
    """The shipped config, catalog and facts, set per test by ``_shipped`` below: never loaded
    at import, and never merged with a developer's salesfinder.local.yaml (final wave T6)."""

    config: AppConfig
    catalog: Catalog
    facts: Facts


SHIPPED = _Shipped()


@pytest.fixture(autouse=True)
def _shipped(shipped_config: AppConfig, shipped_catalog: Catalog, shipped_facts: Facts) -> None:
    SHIPPED.config, SHIPPED.catalog, SHIPPED.facts = shipped_config, shipped_catalog, shipped_facts


BUYER_CODES = [s.code for s in for_side("buyer")]
PUBLIC = "93.184.216.34"

HOME = """<html><head><title>Acme Personal Care</title></head><body>
<nav><a href="/about">About us</a> <a href="/products">Products</a>
<a href="/contact">Contact</a> <a href="/careers">Careers</a></nav>
<p>We manufacture shampoo and body wash. Acme is a manufacturer of personal care products.</p>
</body></html>"""
BUYER_SITE: dict[str, Site] = {
    "https://acme.com/": HOME,
    "https://acme.com/about": "<p>Founded 1990. Our plant is ISO 9001 certified.</p>",
    "https://acme.com/products": "<p>Our shampoo uses SLES from trusted suppliers.</p>",
    "https://acme.com/contact": "<p>Acme Corp, 100 Main St, Houston, TX 77002</p>",
    "https://acme.com/careers": "<title>Careers</title><p>Open role: Procurement Manager</p>",
}


def _config(**profiling: Any) -> AppConfig:
    return SHIPPED.config.model_copy(
        update={"profiling": SHIPPED.config.profiling.model_copy(update=profiling)}
    )


def _web(sites: dict[str, Site], dns: dict[str, list[str]] | None = None) -> FakeWeb:
    if dns is None:
        hosts = {s.split("/")[2] for s in sites}
        dns = {h: [PUBLIC] for h in hosts}
    return FakeWeb(sites, dns)


async def _confirm(db: AsyncSession, codes: list[str]) -> None:
    for code in codes:
        await sig.set_state(db, code, "confirmed", actor=None)
    await db.commit()


async def _company(db: AsyncSession, domain: str = "acme.com", **fields: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "side": "buyer",
        "name": domain,
        "domain": domain,
        "website_url": f"https://{domain}",
        "status": "new",
        **fields,
    }
    company = Company(**values)
    db.add(company)
    await db.commit()
    return company.id


async def _row(engine: AsyncEngine, company_id: uuid.UUID) -> Company:
    async with make_session_factory(engine)() as s:
        row = await s.get(Company, company_id)
        assert row is not None
        return row


async def _observations(engine: AsyncEngine, company_id: uuid.UUID) -> list[Observation]:
    async with make_session_factory(engine)() as s:
        rows = await s.scalars(select(Observation).where(Observation.company_id == company_id))
        return list(rows.all())


async def _profile(
    engine: AsyncEngine,
    web: FakeWeb,
    company_id: uuid.UUID,
    *,
    config: AppConfig | None = None,
    fetcher: Any = None,
) -> ProfileOutcome:
    config = config or SHIPPED.config
    real = SafeFetcher(config.profiling, transport=web.transport, resolver=web.resolver)
    async with real:
        return await profile_company(
            make_session_factory(engine),
            company_id,
            fetcher=fetcher or real,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=config,
            now=lambda: NOW,
        )


async def _batch(
    engine: AsyncEngine,
    web: FakeWeb,
    *,
    limit: int = 10,
    now: datetime = NOW,
    config: AppConfig | None = None,
) -> list[ProfileOutcome]:
    config = config or SHIPPED.config
    async with SafeFetcher(
        config.profiling, transport=web.transport, resolver=web.resolver
    ) as fetcher:
        return await profile_batch(
            make_session_factory(engine),
            fetcher=fetcher,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=config,
            now=lambda: now,
            limit=limit,
        )


async def test_profile_buyer_site_end_to_end(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web(BUYER_SITE)

    outcome = await _profile(engine, web, cid)

    assert web.violations == []
    assert outcome.profile_status == "ok"
    assert outcome.pages_fetched == 5
    assert outcome.error is None
    assert outcome.findings == 7
    row = await _row(engine, cid)
    assert (row.profile_status, row.profile_error, row.pages_fetched) == ("ok", None, 5)
    assert row.profiled_at == NOW and row.scored_at == NOW
    assert row.score == outcome.score == 100
    assert row.disqualified is False
    obs = {o.code: o for o in await _observations(engine, cid)}
    assert set(obs) == set(BUYER_CODES)
    assert all(o.evidence_url and o.evidence_snippet for o in obs.values())
    assert all(o.observed_at == NOW for o in obs.values())
    assert obs["USES_CATALOG_CHEMICAL"].value == ["sles"]
    assert obs["USES_CATALOG_CHEMICAL"].evidence_url == "https://acme.com/products"
    assert obs["QUALITY_CERTS"].value == ["ISO 9001"]
    assert obs["US_LOCATION"].value == {"state": "TX", "city": "Houston"}
    assert obs["COMPANY_TYPE"].value == "manufacturer"
    assert obs["HIRING_PROCUREMENT"].value["hiring"] is True
    assert obs["HIRING_PROCUREMENT"].evidence_url == "https://acme.com/careers"


async def test_excluded_company_never_fetched(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db, status="excluded", exclusion_reason="do_not_contact")
    web = _web(BUYER_SITE)

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "skipped"
    assert web.calls == []
    row = await _row(engine, cid)
    assert (row.profile_status, row.profiled_at, row.score) == ("pending", None, None)
    assert await _observations(engine, cid) == []


async def test_private_address_blocked(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web(BUYER_SITE, dns={"acme.com": ["10.0.0.1"]})

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "blocked"
    assert web.calls == []
    row = await _row(engine, cid)
    assert row.profile_status == "blocked"
    assert row.profile_error == "acme.com resolves to non-public 10.0.0.1"
    assert row.profiled_at == NOW
    assert row.score is None
    assert await _observations(engine, cid) == []


async def test_robots_disallowing_the_homepage_blocks(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    robots = (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /\n")
    web = _web({**BUYER_SITE, "https://acme.com/robots.txt": robots})

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "blocked"
    row = await _row(engine, cid)
    assert row.profile_error == "robots.txt disallows https://acme.com"
    assert await _observations(engine, cid) == []


async def test_homepage_errors(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    gone = await _company(db, "gone.com")
    broken = await _company(db, "broken.com")
    web = _web(
        {"https://broken.com/": (500, {"content-type": "text/html"}, b"oops")},
        dns={"broken.com": [PUBLIC]},
    )

    no_site = await _profile(engine, web, gone)
    failed = await _profile(engine, web, broken)

    assert (no_site.profile_status, no_site.error) == ("no_site", "Could not connect")
    assert (failed.profile_status, failed.error) == ("failed", "HTTP 500")
    assert (await _row(engine, gone)).profile_status == "no_site"
    row = await _row(engine, broken)
    assert (row.profile_status, row.profile_error, row.pages_fetched) == ("failed", "HTTP 500", 0)
    assert await _observations(engine, broken) == []


async def test_non_html_homepage_fails(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web({"https://acme.com/": (200, {"content-type": "application/pdf"}, b"%PDF")})

    outcome = await _profile(engine, web, cid)

    assert (outcome.profile_status, outcome.error) == (
        "failed",
        "The homepage is not a web page.",
    )


async def test_partial_when_subpage_fails(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web({**BUYER_SITE, "https://acme.com/about": (500, {}, b"")})

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "partial"
    assert outcome.pages_fetched == 4
    row = await _row(engine, cid)
    assert row.profile_status == "partial"
    assert row.profile_error == "1 of 5 pages could not be read."
    assert len(await _observations(engine, cid)) == 7
    assert row.score is not None and row.score > 0


async def test_subpage_blocked_by_robots_is_partial_not_blocked(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    robots = (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /careers\n")
    web = _web({**BUYER_SITE, "https://acme.com/robots.txt": robots})

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "partial"
    assert outcome.pages_fetched == 4
    assert "https://acme.com/careers" not in web.logical_urls()
    obs = {o.code: o for o in await _observations(engine, cid)}
    assert obs["HIRING_PROCUREMENT"].value["hiring"] is False


async def test_negative_findings_recorded(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web({"https://acme.com/": "<p>Hello there.</p>"})

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "ok"
    assert outcome.findings == 0
    obs = {o.code: o for o in await _observations(engine, cid)}
    assert set(obs) == set(BUYER_CODES)
    assert obs["USES_CATALOG_CHEMICAL"].value == []
    assert obs["QUALITY_CERTS"].value == []
    assert obs["US_LOCATION"].value == {"found": False}
    assert obs["NEAR_WAREHOUSE"].value == {"found": False}
    assert obs["COMPANY_TYPE"].value == "unknown"
    assert all(o.evidence_url is None and o.evidence_snippet is None for o in obs.values())
    row = await _row(engine, cid)
    assert (row.score, row.disqualified) == (0, False)


async def test_only_confirmed_codes_are_observed(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, ["QUALITY_CERTS", "EXPORTS"])  # EXPORTS is a supplier signal
    cid = await _company(db)
    web = _web(BUYER_SITE)

    outcome = await _profile(engine, web, cid)

    assert [o.code for o in await _observations(engine, cid)] == ["QUALITY_CERTS"]
    assert outcome.score == 10


async def test_retailer_is_disqualified(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web({"https://acme.com/": "<p>Shop now! Add to cart. Free shipping on shampoo.</p>"})

    await _profile(engine, web, cid)

    row = await _row(engine, cid)
    assert (row.score, row.disqualified) == (0, True)


async def test_no_confirmed_signals_does_nothing(engine: AsyncEngine, db: AsyncSession) -> None:
    await _company(db)
    web = _web(BUYER_SITE)
    assert await _batch(engine, web) == []
    assert web.calls == []


async def test_batch_skips_sides_without_confirmed_signals(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["SUPPLIER_CERTS"])
    buyer = await _company(db)
    web = _web(BUYER_SITE)
    assert await _batch(engine, web) == []
    assert (await _row(engine, buyer)).profiled_at is None


async def test_batch_isolates_failures_and_claims(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _confirm(db, BUYER_CODES)
    ids = {d: await _company(db, d) for d in ("a.com", "b.com", "c.com")}
    web = _web({f"https://{d}/": "<p>ISO 9001</p>" for d in ids})
    real = service.run_detectors

    def flaky(pages: Any, **kwargs: Any) -> list[Finding]:
        if "b.com" in pages[0].url:
            raise RuntimeError("detector blew up with page text: secret")
        return real(pages, **kwargs)

    monkeypatch.setattr(service, "run_detectors", flaky)
    with capture_logs() as logs:
        outcomes = await _batch(engine, web)

    [crash_log] = [e for e in logs if e["event"] == "profile_company_failed"]
    assert crash_log["error_type"] == "RuntimeError"
    assert crash_log["frames"][-1].startswith("test_profiling.py:")
    assert crash_log["frames"][-1].endswith(":flaky")
    assert any(f.startswith("service.py:") for f in crash_log["frames"])
    assert "secret" not in repr(logs)

    by_id = {o.company_id: o for o in outcomes}
    assert set(by_id) == set(ids.values())
    assert by_id[ids["a.com"]].profile_status == "ok"
    assert by_id[ids["c.com"]].profile_status == "ok"
    crashed = by_id[ids["b.com"]]
    assert (crashed.profile_status, crashed.error) == ("failed", "Profiling error (RuntimeError).")
    row = await _row(engine, ids["b.com"])
    assert (row.profile_status, row.profile_error) == ("failed", "Profiling error (RuntimeError).")
    assert row.profiled_at == NOW
    assert [(await _row(engine, i)).profiled_at for i in ids.values()] == [NOW] * 3
    # Nothing is due any more.
    assert await _batch(engine, web) == []


async def test_batch_limit_and_order(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    stale = await _company(
        db, "stale.com", profile_status="ok", profiled_at=NOW - timedelta(days=40)
    )
    staler = await _company(
        db, "staler.com", profile_status="ok", profiled_at=NOW - timedelta(days=90)
    )
    pending = await _company(db, "pending.com")
    web = _web({f"https://{d}/": "<p>hi</p>" for d in ("stale.com", "staler.com", "pending.com")})

    first = await _batch(engine, web, limit=2)
    second = await _batch(engine, web, limit=2)

    assert [o.company_id for o in first] == [pending, staler]
    assert [o.company_id for o in second] == [stale]


async def test_concurrent_batches_never_profile_a_company_twice(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    domains = [f"c{i}.com" for i in range(6)]
    for d in domains:
        await _company(db, d)
    web = _web({f"https://{d}/": "<p>ISO 9001</p>" for d in domains})

    a, b = await asyncio.gather(_batch(engine, web, limit=4), _batch(engine, web, limit=4))

    counts = Counter(o.company_id for o in [*a, *b])
    assert len(counts) == 6
    assert set(counts.values()) == {1}


async def test_rescore_after_reject(engine: AsyncEngine, db: AsyncSession, admin: User) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db)
    other = await _company(db, "never-profiled.com")
    web = _web({"https://acme.com/": "<p>ISO 9001 certified.</p>"})
    await _profile(engine, web, cid)
    assert (await _row(engine, cid)).score == 10

    await sig.set_state(db, "QUALITY_CERTS", "rejected", actor=admin)
    later = NOW + timedelta(hours=1)
    assert await rescore_all(db, config=SHIPPED.config, now=later) == 1
    await db.commit()
    row = await _row(engine, cid)
    assert (row.score, row.scored_at) == (0, later)
    assert (await _row(engine, other)).score is None

    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=admin)
    await rescore_all(db, config=SHIPPED.config, now=later)
    await db.commit()
    assert (await _row(engine, cid)).score == 10


async def test_rescore_uses_latest_observation_and_skips_excluded(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["QUALITY_CERTS", "USES_CATALOG_CHEMICAL"])
    cid = await _company(db)
    gone = await _company(db, "gone.com", status="excluded", score=55)
    db.add_all(
        [
            Observation(company_id=cid, code="QUALITY_CERTS", value=["GMP"], observed_at=NOW),
            Observation(
                company_id=cid,
                code="QUALITY_CERTS",
                value=[],
                observed_at=NOW + timedelta(days=1),
                is_latest=True,
            ),
            Observation(
                company_id=cid,
                code="USES_CATALOG_CHEMICAL",
                value=["sles"],
                observed_at=NOW,
                is_latest=True,
            ),
            Observation(
                company_id=gone,
                code="QUALITY_CERTS",
                value=["GMP"],
                observed_at=NOW,
                is_latest=True,
            ),
        ]
    )
    await db.commit()

    assert await rescore_all(db, config=SHIPPED.config, now=NOW) == 1
    await db.commit()
    assert (await _row(engine, cid)).score == 30
    assert (await _row(engine, gone)).score == 55


async def test_refresh_after_days(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    old = await _company(db, "old.com", profile_status="ok", profiled_at=NOW - timedelta(days=31))
    await _company(db, "fresh.com", profile_status="ok", profiled_at=NOW - timedelta(days=5))
    web = _web({"https://old.com/": "<p>hi</p>", "https://fresh.com/": "<p>hi</p>"})

    outcomes = await _batch(engine, web)

    assert [o.company_id for o in outcomes] == [old]


async def test_crashed_claim_is_retried_after_an_hour(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Ruling N: a pending company claimed by a run that died waits an hour, then is retried."""
    await _confirm(db, ["QUALITY_CERTS"])
    recent = await _company(db, "recent.com", profiled_at=NOW - timedelta(minutes=10))
    old = await _company(db, "old.com", profiled_at=NOW - timedelta(hours=2))
    web = _web({"https://recent.com/": "<p>hi</p>", "https://old.com/": "<p>hi</p>"})

    assert [o.company_id for o in await _batch(engine, web)] == [old]
    later = NOW + timedelta(minutes=51)
    assert [o.company_id for o in await _batch(engine, web, now=later)] == [recent]


async def test_request_reprofile_is_picked_up_at_once(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW - timedelta(days=5))
    web = _web({"https://acme.com/": "<p>hi</p>"})
    assert await _batch(engine, web) == []

    await request_reprofile(db, cid, actor=admin)
    await db.commit()
    row = await _row(engine, cid)
    assert (row.profile_status, row.profiled_at) == ("pending", None)
    assert [o.company_id for o in await _batch(engine, web)] == [cid]


async def test_request_reprofile_refuses_excluded_and_unknown(
    db: AsyncSession, admin: User
) -> None:
    cid = await _company(db, status="excluded")
    with pytest.raises(ValueError, match="excluded"):
        await request_reprofile(db, cid, actor=admin)
    with pytest.raises(LookupError):
        await request_reprofile(db, uuid.uuid4(), actor=admin)


class _HangingFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, url: str, *, site_domain: str) -> FetchResult:
        self.calls += 1
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


async def test_per_company_deadline(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    config = _config(page_timeout_seconds=0.05, max_pages_per_company=1)
    hanging = _HangingFetcher()

    async with asyncio.timeout(5):
        outcome = await _profile(engine, _web({}), cid, config=config, fetcher=hanging)

    assert hanging.calls == 1
    assert (outcome.profile_status, outcome.error) == (
        "failed",
        "Timed out while reading the website.",
    )
    row = await _row(engine, cid)
    assert (row.profile_status, row.profile_error) == (
        "failed",
        "Timed out while reading the website.",
    )
    assert await _observations(engine, cid) == []


class _ExcludingFetcher:
    """Excludes the company (as the exclusion sweep would) while its site is being read."""

    def __init__(self, inner: SafeFetcher, engine: AsyncEngine, company_id: uuid.UUID) -> None:
        self.inner = inner
        self.engine = engine
        self.company_id = company_id

    async def get(self, url: str, *, site_domain: str) -> FetchResult:
        async with make_session_factory(self.engine)() as s:
            await s.execute(
                update(Company)
                .where(Company.id == self.company_id)
                .values(status="excluded", exclusion_reason="do_not_contact")
            )
            await s.commit()
        return await self.inner.get(url, site_domain=site_domain)


async def test_company_excluded_while_fetching_gets_nothing_written(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    web = _web(BUYER_SITE)
    async with SafeFetcher(
        SHIPPED.config.profiling, transport=web.transport, resolver=web.resolver
    ) as real:
        outcome = await _profile(engine, web, cid, fetcher=_ExcludingFetcher(real, engine, cid))

    assert outcome.profile_status == "skipped"
    assert web.calls  # it was fetched
    row = await _row(engine, cid)
    assert (row.status, row.profile_status, row.score, row.pages_fetched) == (
        "excluded",
        "pending",
        None,
        0,
    )
    assert await _observations(engine, cid) == []


async def test_nul_characters_are_stripped_before_storing(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _confirm(db, ["STATED_CAPACITY"])
    cid = await _company(db, side="supplier")
    web = _web({"https://acme.com/": "<p>hi</p>"})

    def nul(_pages: Any, **_kwargs: Any) -> list[Finding]:
        from salesfinder.signals.text import Evidence

        return [
            Finding(
                "STATED_CAPACITY",
                {"text": "500\x00 MT", "mt_per_year": 6000},
                Evidence(url="https://acme.com/", snippet="cap\x00acity"),
            )
        ]

    monkeypatch.setattr(service, "run_detectors", nul)
    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "ok"
    [obs] = await _observations(engine, cid)
    assert obs.value == {"text": "500 MT", "mt_per_year": 6000}
    assert obs.evidence_snippet == "capacity"


async def test_detectors_run_off_the_event_loop(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow (CPU-bound) analysis of one company doesn't stall the others."""
    await _confirm(db, ["QUALITY_CERTS"])
    for d in ("a.com", "b.com"):
        await _company(db, d)
    web = _web({"https://a.com/": "<p>hi</p>", "https://b.com/": "<p>hi</p>"})

    def slow(_pages: Any, **_kwargs: Any) -> list[Finding]:
        time.sleep(0.5)
        return []

    monkeypatch.setattr(service, "run_detectors", slow)
    config = _config(concurrency=2)
    started = time.monotonic()
    async with SafeFetcher(config.profiling, transport=web.transport, resolver=web.resolver) as f:
        outcomes = await profile_batch(
            make_session_factory(engine),
            fetcher=f,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=config,
            now=lambda: NOW,
            limit=10,
        )
    elapsed = time.monotonic() - started

    assert [o.profile_status for o in outcomes] == ["ok", "ok"]
    assert elapsed < 0.9


async def test_detectors_see_at_most_200k_chars_per_page(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db)
    filler = "word " * 200_000  # 1 MB of visible text
    web = _web({"https://acme.com/": f"<p>ISO 9001 certified. {filler} GMP</p>"})
    seen: list[int] = []
    real = service.run_detectors

    def spy(pages: Any, **kwargs: Any) -> list[Finding]:
        seen.extend(len(p.text) for p in pages)
        return real(pages, **kwargs)

    monkeypatch.setattr(service, "run_detectors", spy)
    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "ok"
    assert seen == [MAX_DETECT_CHARS]
    [obs] = await _observations(engine, cid)
    assert obs.value == ["ISO 9001"]  # GMP sits past the cut
    assert obs.evidence_snippet is not None and "ISO 9001" in obs.evidence_snippet


async def test_confirming_a_signal_queues_profiled_companies_again(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    """Ruling W: a newly confirmed signal fills in as the worker re-reads the side's sites."""
    await _confirm(db, ["QUALITY_CERTS"])
    profiled = await _company(db)
    web = _web({"https://acme.com/": "<p>ISO 9001. We use SLES daily.</p>"})
    await _profile(engine, web, profiled)
    never = await _company(db, "never.com")
    excluded = await _company(
        db, "gone.com", status="excluded", profile_status="ok", profiled_at=NOW
    )
    supplier = await _company(
        db, "maker.com", side="supplier", profile_status="ok", profiled_at=NOW
    )

    result = await change_signal_states(
        db,
        {"USES_CATALOG_CHEMICAL": "confirmed", "QUALITY_CERTS": "confirmed"},
        actor=admin,
        config=SHIPPED.config,
        now=NOW,
        ip="10.1.2.3",
    )
    await db.commit()

    assert result.changed == {"confirmed": ["USES_CATALOG_CHEMICAL"]}
    assert (result.queued, result.rescored) == (1, 1)
    row = await _row(engine, profiled)
    assert (row.profile_status, row.profiled_at, row.score) == ("pending", None, 10)
    assert (await _row(engine, never)).profile_status == "pending"
    assert (await _row(engine, excluded)).profile_status == "ok"
    assert (await _row(engine, supplier)).profile_status == "ok"
    [entry] = (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.SIGNAL_STATE_CHANGED))
    ).all()
    assert entry.details == {"code": "USES_CATALOG_CHEMICAL", "old": "proposed", "new": "confirmed"}
    assert (entry.actor_id, entry.ip, entry.target_id) == (
        admin.id,
        "10.1.2.3",
        "USES_CATALOG_CHEMICAL",
    )

    # The worker re-reads it and the new signal fills in.
    outcomes = await _batch(engine, web)
    assert profiled in [o.company_id for o in outcomes]
    assert (await _row(engine, profiled)).score == 40


async def test_rejecting_or_skipping_never_queues(db: AsyncSession, admin: User) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW)
    result = await change_signal_states(
        db,
        {"QUALITY_CERTS": "rejected", "INDUSTRY_MATCH": "skipped"},
        actor=None,
        config=SHIPPED.config,
        now=NOW,
    )
    assert result.queued == 0
    assert result.changed == {"rejected": ["QUALITY_CERTS"], "skipped": ["INDUSTRY_MATCH"]}
    row = await db.get(Company, cid)
    assert row is not None and row.profile_status == "ok"
    no_change = await change_signal_states(
        db, {"QUALITY_CERTS": "rejected"}, actor=None, config=SHIPPED.config, now=NOW
    )
    assert no_change == service.SignalChanges({}, 0, 0)
    with pytest.raises(ValueError, match="NOPE"):
        await change_signal_states(
            db, {"NOPE": "confirmed"}, actor=None, config=SHIPPED.config, now=NOW
        )


async def test_rescore_never_writes_onto_a_company_excluded_meanwhile(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, score=55, scored_at=NOW)
    db.add(
        Observation(
            company_id=cid, code="QUALITY_CERTS", value=["GMP"], observed_at=NOW, is_latest=True
        )
    )
    await db.commit()
    real = service._latest_values

    async def exclude_then_read(*args: Any, **kwargs: Any) -> Any:
        # The sweep commits an exclusion after rescore_all chose its companies.
        async with make_session_factory(engine)() as s:
            await s.execute(update(Company).where(Company.id == cid).values(status="excluded"))
            await s.commit()
        return await real(*args, **kwargs)

    monkeypatch.setattr(service, "_latest_values", exclude_then_read)
    await rescore_all(db, config=SHIPPED.config, now=NOW + timedelta(hours=1))
    await db.commit()

    row = await _row(engine, cid)
    assert (row.status, row.score, row.scored_at) == ("excluded", 55, NOW)


# --- final review fixes: network outage, transient-failure backoff, partial re-profiles ------


async def test_batch_all_unreachable_records_nothing(engine: AsyncEngine, db: AsyncSession) -> None:
    """Circuit breaker: 3+ companies and every homepage unreachable (no DNS / no connection or a
    timeout) looks like our network, not their sites: nothing is recorded, claims released and
    retried after NETWORK_RETRY."""
    await _confirm(db, ["QUALITY_CERTS"])
    pending = [await _company(db, d) for d in ("a.com", "b.com")]
    stale_at = NOW - timedelta(days=40)
    stale = await _company(db, "c.com", profile_status="ok", profiled_at=stale_at, score=10)
    slow = await _company(db, "slow.com")
    web = _web(
        {"https://slow.com/": Slow(5)}, dns={"slow.com": [PUBLIC]}
    )  # a.com, b.com, c.com don't resolve
    config = _config(page_timeout_seconds=0.05)

    with capture_logs() as logs:
        outcomes = await _batch(engine, web, config=config)

    assert [(o.profile_status, o.error) for o in outcomes] == [("skipped", NETWORK_DOWN)] * 4
    assert outcomes_line(outcomes) == "Network looks down — will retry."
    assert [e["event"] for e in logs if e["event"] == "profiling_network_down"] == [
        "profiling_network_down"
    ]
    retry_at = NOW + NETWORK_RETRY
    for cid in [*pending, slow]:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profiled_at, row.profile_failures) == ("pending", None, 0)
        assert (row.profile_error, row.profile_retry_at) == (None, retry_at)
    row = await _row(engine, stale)
    assert (row.profile_status, row.profiled_at, row.score) == ("ok", stale_at, 10)
    assert row.profile_retry_at == retry_at
    # Released: not claimed again until NETWORK_RETRY has passed (so they can't take every
    # pass), then due exactly as before.
    assert await _batch(engine, web, config=config, now=retry_at - timedelta(seconds=1)) == []
    again = await _batch(engine, web, config=config, now=retry_at)
    assert {o.company_id for o in again} == {*pending, stale, slow}
    assert {o.error for o in again} == {NETWORK_DOWN}  # still down: held back again
    later = retry_at + NETWORK_RETRY
    assert (await _row(engine, stale)).profile_retry_at == later
    # Back up: recorded as usual, and the hold is cleared.
    up = _web({f"https://{d}/": "<p>ISO 9001</p>" for d in ("a.com", "b.com", "c.com", "slow.com")})
    back = await _batch(engine, up, config=config, now=later)
    assert {(o.company_id, o.profile_status) for o in back} == {
        (c, "ok") for c in (*pending, stale, slow)
    }
    assert (await _row(engine, stale)).profile_retry_at is None


async def test_already_failing_sites_are_no_evidence_of_an_outage(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Retries of sites that were already unreachable failing again are expected: recorded."""
    await _confirm(db, ["QUALITY_CERTS"])
    two_days = NOW - timedelta(days=2)
    ids = [
        await _company(db, d, profile_status="no_site", profile_failures=1, profiled_at=two_days)
        for d in ("a.com", "b.com", "c.com")
    ]

    outcomes = await _batch(engine, _web({}, dns={}))

    assert [o.profile_status for o in outcomes] == ["no_site"] * 3
    assert [(await _row(engine, c)).profile_failures for c in ids] == [2, 2, 2]


async def test_reprofile_request_overrides_a_network_hold(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_retry_at=NOW + timedelta(minutes=10))
    web = _web({"https://acme.com/": "<p>hi</p>"})
    assert await _batch(engine, web) == []

    await request_reprofile(db, cid, actor=admin)
    await db.commit()
    assert [o.company_id for o in await _batch(engine, web)] == [cid]


async def test_breaker_needs_three_unreachable_companies(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    ids = [await _company(db, d) for d in ("a.com", "b.com")]
    web = _web({}, dns={})

    outcomes = await _batch(engine, web)

    assert [(o.profile_status, o.error) for o in outcomes] == [("no_site", "Could not connect")] * 2
    assert outcomes_line(outcomes) == "Profiled 2 companies (0 ok, 2 failed)."
    for cid in ids:
        row = await _row(engine, cid)
        assert (row.profile_status, row.profiled_at, row.profile_failures) == ("no_site", NOW, 1)


async def test_breaker_off_when_any_site_answered(engine: AsyncEngine, db: AsyncSession) -> None:
    """One reachable site (even an HTTP error) means the network is up: every outcome counts."""
    await _confirm(db, ["QUALITY_CERTS"])
    gone = [await _company(db, d) for d in ("a.com", "b.com", "c.com")]
    broken = await _company(db, "broken.com")
    web = _web(
        {"https://broken.com/": (500, {"content-type": "text/html"}, b"oops")},
        dns={"broken.com": [PUBLIC]},
    )

    outcomes = await _batch(engine, web)

    by_id = {o.company_id: o.profile_status for o in outcomes}
    assert [by_id[c] for c in gone] == ["no_site"] * 3
    assert by_id[broken] == "failed"
    assert [(await _row(engine, c)).profile_status for c in gone] == ["no_site"] * 3


async def test_failure_count_grows_and_resets(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_status="no_site", profile_failures=1)
    down = _web({}, dns={})
    up = _web({"https://acme.com/": "<p>ISO 9001</p>"})

    await _profile(engine, down, cid)
    assert (await _row(engine, cid)).profile_failures == 2
    await _profile(engine, up, cid)
    row = await _row(engine, cid)
    assert (row.profile_status, row.profile_failures) == ("ok", 0)


async def test_crash_counts_as_a_failure(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_status="failed", profile_failures=1)
    await db.execute(update(Company).values(profiled_at=NOW - timedelta(days=2)))
    await db.commit()

    def boom(pages: Any, **kwargs: Any) -> list[Finding]:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "run_detectors", boom)
    [outcome] = await _batch(engine, _web({"https://acme.com/": "<p>hi</p>"}))
    assert outcome.profile_status == "failed"
    assert (await _row(engine, cid)).profile_failures == 2


@pytest.mark.parametrize(
    ("status", "failures", "age", "due"),
    [
        ("no_site", 1, timedelta(hours=23), False),
        ("no_site", 1, timedelta(hours=25), True),
        ("failed", 0, timedelta(hours=25), True),  # rows from before the counter existed
        ("failed", 1, timedelta(days=2), True),
        ("failed", 2, timedelta(days=6), False),
        ("failed", 2, timedelta(days=8), True),
        ("no_site", 3, timedelta(days=8), False),
        ("no_site", 3, timedelta(days=29), False),
        ("no_site", 3, timedelta(days=31), True),
        ("blocked", 1, timedelta(days=2), False),  # not transient: the full refresh period
        ("ok", 0, timedelta(days=2), False),
        ("partial", 1, timedelta(days=2), False),
    ],
)
async def test_transient_failures_are_retried_with_backoff(
    engine: AsyncEngine,
    db: AsyncSession,
    status: str,
    failures: int,
    age: timedelta,
    due: bool,
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(
        db, profile_status=status, profile_failures=failures, profiled_at=NOW - age
    )
    web = _web({"https://acme.com/": "<p>hi</p>"})

    outcomes = await _batch(engine, web)

    assert [o.company_id for o in outcomes] == ([cid] if due else [])


async def test_backoff_never_exceeds_refresh_after_days(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(
        db, profile_status="failed", profile_failures=2, profiled_at=NOW - timedelta(days=4)
    )
    web = _web({"https://acme.com/": "<p>hi</p>"})
    config = SHIPPED.config.model_copy(
        update={"profiling": SHIPPED.config.profiling.model_copy(update={"refresh_after_days": 3})}
    )

    assert [o.company_id for o in await _batch(engine, web, config=config)] == [cid]


async def _observe(db: AsyncSession, cid: uuid.UUID, **values: Any) -> None:
    earlier = NOW - timedelta(days=40)
    db.add_all(
        Observation(company_id=cid, code=code, value=value, observed_at=earlier, is_latest=True)
        for code, value in values.items()
    )
    await db.commit()


async def _latest(engine: AsyncEngine, cid: uuid.UUID) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for o in sorted(await _observations(engine, cid), key=lambda o: o.observed_at):
        latest[o.code] = o.value
    return latest


async def test_partial_reprofile_keeps_positive_findings(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """A page that couldn't be read may be the one that said it: a partial run never replaces
    a positive finding with a negative one (company type included), but does record new and
    changed positives."""
    await _confirm(db, ["USES_CATALOG_CHEMICAL", "COMPANY_TYPE", "QUALITY_CERTS", "US_LOCATION"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW - timedelta(days=40))
    await _observe(
        db,
        cid,
        USES_CATALOG_CHEMICAL=["sles"],
        COMPANY_TYPE="manufacturer",
        QUALITY_CERTS=["GMP"],
    )
    web = _web(
        {
            "https://acme.com/": '<a href="/products">Products</a><p>ISO 9001 certified.</p>',
            "https://acme.com/products": (500, {}, b""),
        }
    )

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "partial"
    latest = await _latest(engine, cid)
    assert latest["USES_CATALOG_CHEMICAL"] == ["sles"]
    assert latest["COMPANY_TYPE"] == "manufacturer"
    assert latest["QUALITY_CERTS"] == ["ISO 9001"]  # a changed positive is written
    assert latest["US_LOCATION"] == {"found": False}  # nothing before: the negative is written
    row = await _row(engine, cid)
    assert row.score == 30 + 20 + 10  # chemical + producer + certs, all still counted


async def test_partial_reprofile_keeps_disqualifying_company_type(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, ["COMPANY_TYPE"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW - timedelta(days=40))
    await _observe(db, cid, COMPANY_TYPE="retailer")
    web = _web(
        {
            "https://acme.com/": '<a href="/about">About</a><p>Hello.</p>',
            "https://acme.com/about": (500, {}, b""),
        }
    )

    assert (await _profile(engine, web, cid)).profile_status == "partial"

    assert (await _latest(engine, cid))["COMPANY_TYPE"] == "retailer"
    row = await _row(engine, cid)
    assert (row.score, row.disqualified) == (0, True)


async def test_full_reprofile_records_negatives(engine: AsyncEngine, db: AsyncSession) -> None:
    """An ok run read every page: what is gone now is recorded as gone."""
    await _confirm(db, ["USES_CATALOG_CHEMICAL"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW - timedelta(days=40))
    await _observe(db, cid, USES_CATALOG_CHEMICAL=["sles"])

    await _profile(engine, _web({"https://acme.com/": "<p>Hello.</p>"}), cid)

    assert (await _latest(engine, cid))["USES_CATALOG_CHEMICAL"] == []
    assert (await _row(engine, cid)).score == 0


async def test_failed_reprofile_keeps_prior_score_and_observations(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _confirm(db, BUYER_CODES)
    cid = await _company(db)
    await _profile(engine, _web(BUYER_SITE), cid)
    before = await _row(engine, cid)
    observations = len(await _observations(engine, cid))
    assert before.score == 100

    broken = _web({"https://acme.com/": (500, {"content-type": "text/html"}, b"oops")})
    outcome = await _profile(engine, broken, cid)

    assert (outcome.profile_status, outcome.score) == ("failed", 100)
    row = await _row(engine, cid)
    assert (row.profile_status, row.profile_error) == ("failed", "HTTP 500")
    assert (row.score, row.disqualified, row.scored_at) == (100, False, before.scored_at)
    assert len(await _observations(engine, cid)) == observations


async def test_profile_write_flips_the_latest_flag(engine: AsyncEngine, db: AsyncSession) -> None:
    await _confirm(db, ["QUALITY_CERTS"])
    cid = await _company(db, profile_status="ok", profiled_at=NOW - timedelta(days=40))
    await _observe(db, cid, QUALITY_CERTS=["GMP"])
    old = (await _observations(engine, cid))[0]
    assert old.is_latest is True

    web = _web({"https://acme.com/": "<p>We hold ISO 9001 certification.</p>"})
    await _profile(engine, web, cid)

    rows = await _observations(engine, cid)
    assert len(rows) == 2
    latest = [o for o in rows if o.is_latest]
    assert len(latest) == 1
    assert latest[0].id != old.id and latest[0].observed_at == NOW
    assert [o.value for o in rows if o.id == old.id] == [["GMP"]]
    async with make_session_factory(engine)() as s:
        assert (await latest_values(s, ["QUALITY_CERTS"]))[cid]["QUALITY_CERTS"] == ["ISO 9001"]
