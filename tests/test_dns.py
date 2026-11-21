"""MX checker and its cache. No network: FakeResolver raises real dnspython exception types."""

import asyncio
from datetime import UTC, datetime, timedelta

import dns.asyncresolver
import dns.exception
import dns.resolver
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from conftest import RealDnsInTests
from fakes.dns import FakeResolver
from salesfinder.config import ContactsConfig
from salesfinder.contacts.dns import MxChecker, MxResult, mx_for
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import DomainMx

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
CONFIG = ContactsConfig()


def _checker(resolver: FakeResolver, config: ContactsConfig = CONFIG) -> MxChecker:
    return MxChecker(config, resolve=resolver)


async def test_mx_hosts_sorted() -> None:
    r = FakeResolver({("acme.com", "MX"): [(20, "b.mx."), (10, "a.mx.")]})
    result = await _checker(r).check("acme.com")
    assert result == MxResult("has_mx", ("a.mx", "b.mx"), None)


async def test_domain_is_normalized() -> None:
    r = FakeResolver({("acme.com", "MX"): [(10, "a.mx")]})
    result = await _checker(r).check("  ACME.com. ")
    assert result.state == "has_mx"
    assert r.calls == [("acme.com", "MX")]


async def test_idna_domain() -> None:
    r = FakeResolver({("xn--bcher-kva.example", "MX"): [(10, "a.mx")]})
    result = await _checker(r).check("bücher.example")
    assert result.state == "has_mx"


async def test_invalid_domain_unknown() -> None:
    r = FakeResolver()
    for bad in ("", "a..b", "x" * 70 + ".com"):
        result = await _checker(r).check(bad)
        assert result == MxResult("unknown", (), "Invalid domain")
    assert r.calls == []


async def test_null_mx() -> None:
    r = FakeResolver({("acme.com", "MX"): [(0, ".")]})
    result = await _checker(r).check("acme.com")
    assert result.state == "no_mail"
    assert result.hosts == ()


async def test_nxdomain() -> None:
    r = FakeResolver({("gone.com", "MX"): dns.resolver.NXDOMAIN()})
    result = await _checker(r).check("gone.com")
    assert result.state == "no_mail"
    assert r.calls == [("gone.com", "MX")]


async def test_no_mx_but_a_record() -> None:
    r = FakeResolver(
        {("acme.com", "MX"): dns.resolver.NoAnswer(), ("acme.com", "A"): [(0, "1.2.3.4")]}
    )
    result = await _checker(r).check("acme.com")
    assert result == MxResult("has_mx", ("acme.com",), None)


async def test_no_mx_but_aaaa_record() -> None:
    r = FakeResolver(
        {
            ("acme.com", "MX"): dns.resolver.NoAnswer(),
            ("acme.com", "A"): dns.resolver.NoAnswer(),
            ("acme.com", "AAAA"): [(0, "::1")],
        }
    )
    result = await _checker(r).check("acme.com")
    assert result.state == "has_mx"
    assert result.hosts == ("acme.com",)


async def test_no_mx_no_a() -> None:
    r = FakeResolver({("acme.com", "MX"): dns.resolver.NoAnswer()})
    result = await _checker(r).check("acme.com")
    assert result.state == "no_mail"


async def test_a_lookup_nxdomain_is_no_mail() -> None:
    r = FakeResolver(
        {("acme.com", "MX"): dns.resolver.NoAnswer(), ("acme.com", "A"): dns.resolver.NXDOMAIN()}
    )
    assert (await _checker(r).check("acme.com")).state == "no_mail"


async def test_timeout_and_servfail_unknown() -> None:
    for exc, message in [
        (dns.exception.Timeout(), "DNS timed out"),
        (dns.resolver.LifetimeTimeout(timeout=5, errors=[]), "DNS timed out"),
        (dns.resolver.NoNameservers(), "DNS server failure"),
        (dns.exception.DNSException("boom"), "DNS server failure"),
    ]:
        r = FakeResolver({("acme.com", "MX"): exc})
        result = await _checker(r).check("acme.com")
        assert result == MxResult("unknown", (), message)


async def test_a_lookup_timeout_unknown() -> None:
    r = FakeResolver(
        {("acme.com", "MX"): dns.resolver.NoAnswer(), ("acme.com", "A"): dns.exception.Timeout()}
    )
    result = await _checker(r).check("acme.com")
    assert result == MxResult("unknown", (), "DNS timed out")


async def test_concurrency_bounded() -> None:
    r = FakeResolver({(f"d{i}.com", "MX"): [(10, "mx.d.com")] for i in range(20)}, delay=0.01)
    checker = _checker(r, ContactsConfig(dns_concurrency=3))
    results = await asyncio.gather(*(checker.check(f"d{i}.com") for i in range(20)))
    assert all(x.state == "has_mx" for x in results)
    assert r.max_in_flight == 3


async def test_cache(engine: AsyncEngine, db: AsyncSession) -> None:
    factory = make_session_factory(engine)
    r = FakeResolver({("acme.com", "MX"): [(10, "a.mx")]})
    checker = _checker(r)

    first = await mx_for(factory, checker, "ACME.com", now=NOW, config=CONFIG)
    assert first.state == "has_mx"
    assert len(r.calls) == 1

    # Fresh: reused.
    again = await mx_for(factory, checker, "acme.com", now=NOW + timedelta(days=6), config=CONFIG)
    assert again == MxResult("has_mx", ("a.mx",), None)
    assert len(r.calls) == 1

    # Stale after 7 days: re-queried and row updated.
    r.answers[("acme.com", "MX")] = [(10, "new.mx")]
    stale = await mx_for(factory, checker, "acme.com", now=NOW + timedelta(days=8), config=CONFIG)
    assert stale.hosts == ("new.mx",)
    assert len(r.calls) == 2
    rows = (await db.execute(select(DomainMx))).scalars().all()
    assert len(rows) == 1
    assert rows[0].mx_hosts == ["new.mx"]


async def test_cache_unknown_retried_after_one_hour(engine: AsyncEngine, db: AsyncSession) -> None:
    factory = make_session_factory(engine)
    r = FakeResolver({("flaky.com", "MX"): dns.exception.Timeout()})
    checker = _checker(r)

    first = await mx_for(factory, checker, "flaky.com", now=NOW, config=CONFIG)
    assert first == MxResult("unknown", (), "DNS timed out")
    assert len(r.calls) == 1

    await mx_for(factory, checker, "flaky.com", now=NOW + timedelta(minutes=30), config=CONFIG)
    assert len(r.calls) == 1

    r.answers[("flaky.com", "MX")] = [(10, "mx.flaky.com")]
    retried = await mx_for(
        factory, checker, "flaky.com", now=NOW + timedelta(hours=1, seconds=1), config=CONFIG
    )
    assert retried.state == "has_mx"
    assert len(r.calls) == 2
    row = (await db.execute(select(DomainMx))).scalar_one()
    assert row.state == "has_mx"
    assert row.error is None


async def test_cache_no_mail_reused(engine: AsyncEngine, db: AsyncSession) -> None:
    factory = make_session_factory(engine)
    r = FakeResolver({("gone.com", "MX"): dns.resolver.NXDOMAIN()})
    checker = _checker(r)
    await mx_for(factory, checker, "gone.com", now=NOW, config=CONFIG)
    result = await mx_for(factory, checker, "gone.com", now=NOW + timedelta(days=1), config=CONFIG)
    assert result.state == "no_mail"
    assert len(r.calls) == 1


async def test_cache_keeps_a_known_answer_through_a_dns_blip(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Ruling AH: a stale has_mx / no_mail answer isn't replaced by "unknown"."""
    factory = make_session_factory(engine)
    r = FakeResolver(
        {("acme.com", "MX"): [(10, "a.mx")], ("gone.com", "MX"): dns.resolver.NXDOMAIN()}
    )
    checker = _checker(r)
    await mx_for(factory, checker, "acme.com", now=NOW, config=CONFIG)
    await mx_for(factory, checker, "gone.com", now=NOW, config=CONFIG)

    r.answers[("acme.com", "MX")] = dns.exception.Timeout()
    r.answers[("gone.com", "MX")] = dns.resolver.NoNameservers()
    later = NOW + timedelta(days=8)
    blip = await mx_for(factory, checker, "acme.com", now=later, config=CONFIG)
    gone = await mx_for(factory, checker, "gone.com", now=later, config=CONFIG)

    assert blip == MxResult("has_mx", ("a.mx",), "DNS timed out")
    assert gone == MxResult("no_mail", (), "DNS server failure")
    rows = {row.domain: row for row in (await db.execute(select(DomainMx))).scalars().all()}
    assert (rows["acme.com"].state, rows["acme.com"].mx_hosts) == ("has_mx", ["a.mx"])
    assert rows["acme.com"].checked_at == NOW  # still stale: DNS is tried again next time
    assert rows["acme.com"].error == "DNS timed out"
    assert rows["gone.com"].state == "no_mail"

    # The next call asks DNS again; an answer replaces everything.
    r.answers[("acme.com", "MX")] = [(10, "b.mx")]
    fresh = await mx_for(factory, checker, "acme.com", now=later, config=CONFIG)
    assert fresh == MxResult("has_mx", ("b.mx",), None)
    await db.refresh(rows["acme.com"])
    assert (rows["acme.com"].checked_at, rows["acme.com"].error) == (later, None)


def test_real_resolver_is_guarded_in_tests() -> None:
    """The autouse guard in conftest: a checker without an injected resolver can't reach DNS."""
    import salesfinder.contacts.dns as dns_module

    with pytest.raises(RealDnsInTests):
        MxChecker(CONFIG)
    with pytest.raises(RealDnsInTests):
        dns_module.default_resolve(CONFIG)
    with pytest.raises(RealDnsInTests):
        dns.asyncresolver.Resolver()
