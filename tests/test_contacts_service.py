"""Contacts service: people/addresses stored by profiling, MX + pattern verification, the
sendable rule, overrides and the batch. Real DB, FakeWeb, FakeResolver, a fake verifier."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import dns.exception
import dns.resolver
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fakes.dns import FakeResolver
from fakes.web import FakeWeb, Site
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.contacts import service as contacts
from salesfinder.contacts.dns import MxChecker
from salesfinder.contacts.patterns import LearnedPattern
from salesfinder.contacts.service import (
    DNS_UNAVAILABLE,
    EXCLUDED,
    NOT_A_MAILBOX,
    VerifyOutcome,
    is_sendable,
    set_override,
    unsendable_reason,
    verify_batch,
    verify_company,
)
from salesfinder.contacts.verifier import VerifyResult, daily_count
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    Company,
    DomainMx,
    DoNotContact,
    EmailAddress,
    EmailPattern,
    Person,
    User,
)
from salesfinder.profiling.fetch import SafeFetcher
from salesfinder.profiling.service import ProfileOutcome, profile_company
from salesfinder.signals import settings as sig

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
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


HOME = """<html><head><title>{name} Personal Care</title></head><body>
<nav><a href="/team">Our team</a> <a href="/contact">Contact</a></nav>
<p>We manufacture shampoo. Our plant is ISO 9001 certified.</p></body></html>"""
TEAM = """<html><head><title>Our Team</title></head><body><ul>
<li>Priya Nair | Head of Procurement</li>
<li>Anil Mehta \u2013 Owner</li>
</ul></body></html>"""
TEAM_PRIYA_ONLY = """<html><head><title>Our Team</title></head><body><ul>
<li>Priya Nair | Head of Procurement</li>
</ul></body></html>"""
CONTACT_PUBLISHED = """<html><head><title>Contact us</title></head><body>
<p>Write to priya.nair@{domain} or info@{domain}</p>
<form><input type="email" name="from"><textarea name="message"></textarea></form>
</body></html>"""
CONTACT_ROLE_ONLY = """<html><head><title>Contact us</title></head><body>
<p>Write to info@{domain}</p></body></html>"""


def _site(
    domain: str = "acme.com", *, contact: str = CONTACT_PUBLISHED, team: Site | None = TEAM
) -> dict[str, Site]:
    sites: dict[str, Site] = {
        f"https://{domain}/": HOME.format(name=domain),
        f"https://{domain}/contact": contact.format(domain=domain),
    }
    if team is not None:
        sites[f"https://{domain}/team"] = team
    return sites


def _web(*sites: dict[str, Site]) -> FakeWeb:
    merged: dict[str, Site] = {}
    for s in sites:
        merged.update(s)
    hosts = {url.split("/")[2] for url in merged}
    return FakeWeb(merged, {h: [PUBLIC] for h in hosts})


def _has_mx(*domains: str) -> FakeResolver:
    return FakeResolver({(d, "MX"): [(10, f"mx.{d}.")] for d in domains})


def _config(**contacts_fields: Any) -> AppConfig:
    return SHIPPED.config.model_copy(
        update={"contacts": SHIPPED.config.contacts.model_copy(update=contacts_fields)}
    )


class FakeVerifier:
    """``results[address]`` is the answer; None means the call failed (like MillionVerifier on
    a bad key, no credits or an HTTP error)."""

    def __init__(self, results: dict[str, VerifyResult | None] | None = None) -> None:
        self.results = results or {}
        self.calls: list[str] = []
        self.consecutive_failures = 0

    async def verify(self, address: str) -> VerifyResult | None:
        self.calls.append(address)
        result = self.results.get(address, "verified")
        self.consecutive_failures = 0 if result is not None else self.consecutive_failures + 1
        return result


async def _company(db: AsyncSession, domain: str = "acme.com", **fields: Any) -> uuid.UUID:
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
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


async def _profile(engine: AsyncEngine, web: FakeWeb, cid: uuid.UUID) -> ProfileOutcome:
    async with SafeFetcher(
        SHIPPED.config.profiling, transport=web.transport, resolver=web.resolver
    ) as fetcher:
        return await profile_company(
            make_session_factory(engine),
            cid,
            fetcher=fetcher,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=SHIPPED.config,
            now=lambda: NOW,
        )


async def _verify(
    engine: AsyncEngine,
    cid: uuid.UUID,
    resolver: FakeResolver,
    *,
    verifier: FakeVerifier | None = None,
    config: AppConfig | None = None,
    now: datetime = NOW,
) -> VerifyOutcome:
    config = config or SHIPPED.config
    return await verify_company(
        make_session_factory(engine),
        cid,
        checker=MxChecker(config.contacts, resolve=resolver),
        verifier=verifier,
        config=config,
        now=now,
    )


async def _people(engine: AsyncEngine, cid: uuid.UUID) -> dict[str, Person]:
    async with make_session_factory(engine)() as s:
        rows = await s.scalars(select(Person).where(Person.company_id == cid))
        return {p.full_name: p for p in rows.all()}


async def _emails(engine: AsyncEngine, cid: uuid.UUID) -> dict[str, EmailAddress]:
    async with make_session_factory(engine)() as s:
        rows = await s.scalars(select(EmailAddress).where(EmailAddress.company_id == cid))
        return {e.address: e for e in rows.all()}


async def _row(engine: AsyncEngine, cid: uuid.UUID) -> Company:
    async with make_session_factory(engine)() as s:
        row = await s.get(Company, cid)
        assert row is not None
        return row


async def _profiled(engine: AsyncEngine, db: AsyncSession, **site: Any) -> uuid.UUID:
    cid = await _company(db)
    outcome = await _profile(engine, _web(_site(**site)), cid)
    assert outcome.profile_status == "ok"
    return cid


# --- profiling stores what the site says ---------------------------------------------------------


async def test_profile_stores_people_emails_form(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _company(db)
    web = _web(_site())

    outcome = await _profile(engine, web, cid)

    assert outcome.profile_status == "ok"
    assert web.violations == []
    people = await _people(engine, cid)
    assert set(people) == {"Priya Nair", "Anil Mehta"}
    priya, anil = people["Priya Nair"], people["Anil Mehta"]
    assert (priya.is_target, priya.rank, priya.title_category) == (True, 1, "procurement")
    assert (anil.is_target, anil.rank, anil.title_category) == (True, 2, "owner_exec")
    assert (priya.name_key, priya.first_name, priya.last_name) == ("priya nair", "Priya", "Nair")
    assert priya.title == "Head of Procurement"
    assert priya.source_url == "https://acme.com/team"
    emails = await _emails(engine, cid)
    assert set(emails) == {"priya.nair@acme.com", "info@acme.com"}
    personal, role = emails["priya.nair@acme.com"], emails["info@acme.com"]
    assert (personal.kind, personal.status, personal.source) == ("personal", "published", "page")
    assert personal.person_id == priya.id
    assert personal.evidence_url == "https://acme.com/contact"
    assert (role.kind, role.status, role.person_id) == ("role", "published", None)
    row = await _row(engine, cid)
    assert row.contact_form_url == "https://acme.com/contact"
    assert row.contacts_checked_at is None  # due for verification
    assert row.contacts_found == 2


async def test_max_targets_per_company(engine: AsyncEngine, db: AsyncSession) -> None:
    team = """<html><head><title>Our Team</title></head><body><ul>
    <li>Priya Nair | Head of Procurement</li><li>Anil Mehta \u2013 Owner</li>
    <li>Ravi Kumar | R&amp;D Manager</li><li>Sunita Rao | Accountant</li></ul></body></html>"""
    cid = await _profiled(engine, db, team=team)

    people = await _people(engine, cid)
    assert len(people) == 4
    targets = sorted((p.rank, name) for name, p in people.items() if p.is_target)
    assert targets == [(1, "Priya Nair"), (2, "Ravi Kumar")]
    assert people["Sunita Rao"].rank is None and people["Anil Mehta"].rank is None


# --- verification ---------------------------------------------------------------------------------


async def test_verify_learns_pattern_and_confirms(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db)
    resolver = _has_mx("acme.com")

    outcome = await _verify(engine, cid, resolver)

    assert outcome == VerifyOutcome(cid, 2, 3, 2, "has_mx", None)
    assert resolver.calls == [("acme.com", "MX")]
    people = await _people(engine, cid)
    emails = await _emails(engine, cid)
    anil = emails["anil.mehta@acme.com"]
    assert (anil.status, anil.source, anil.pattern) == (
        "pattern_confirmed",
        "pattern",
        "{first}.{last}",
    )
    assert anil.person_id == people["Anil Mehta"].id
    assert emails["priya.nair@acme.com"].status == "published"
    assert all(is_sendable(e, mx_state="has_mx") for e in emails.values())
    row = await _row(engine, cid)
    assert (row.contacts_found, row.sendable_contacts, row.contacts_checked_at) == (2, 2, NOW)
    async with make_session_factory(engine)() as s:
        pattern = await s.get(EmailPattern, "acme.com")
    assert pattern is not None
    assert (pattern.pattern, pattern.support, pattern.examples) == (
        "{first}.{last}",
        1,
        ["priya.nair@acme.com"],
    )

    # Checking again changes nothing.
    again = await _verify(engine, cid, resolver, now=NOW + timedelta(days=1))
    assert again == VerifyOutcome(cid, 2, 3, 2, "has_mx", None)
    after = await _emails(engine, cid)
    assert {a: (e.id, e.status) for a, e in after.items()} == {
        a: (e.id, e.status) for a, e in emails.items()
    }


async def test_guess_is_held(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)

    outcome = await _verify(engine, cid, _has_mx("acme.com"))

    assert (outcome.targets, outcome.addresses, outcome.sendable) == (2, 3, 0)
    emails = await _emails(engine, cid)
    assert {a: e.status for a, e in emails.items()} == {
        "info@acme.com": "published",
        "priya.nair@acme.com": "pattern_guess",
        "anil.mehta@acme.com": "pattern_guess",
    }
    guess = emails["priya.nair@acme.com"]
    assert (guess.source, guess.pattern) == ("guess", "{first}.{last}")
    assert not is_sendable(guess, mx_state="has_mx")
    assert unsendable_reason(guess, mx_state="has_mx") == "Held: guess"
    assert is_sendable(emails["info@acme.com"], mx_state="has_mx")  # role: not a target
    row = await _row(engine, cid)
    assert (row.contacts_found, row.sendable_contacts) == (2, 0)
    async with make_session_factory(engine)() as s:
        assert await s.get(EmailPattern, "acme.com") is None


async def test_no_mail_domain_invalidates(engine: AsyncEngine, db: AsyncSession) -> None:
    contact = CONTACT_PUBLISHED.replace("or info", "or anilmehta@gmail.com or info")
    cid = await _profiled(engine, db, contact=contact)
    emails = await _emails(engine, cid)
    assert emails["anilmehta@gmail.com"].kind == "personal"

    outcome = await _verify(
        engine, cid, FakeResolver({("acme.com", "MX"): dns.resolver.NXDOMAIN()})
    )

    assert (outcome.mx_state, outcome.error) == ("no_mail", None)
    emails = await _emails(engine, cid)
    assert {a: e.status for a, e in emails.items()} == {
        "priya.nair@acme.com": "invalid",
        "info@acme.com": "invalid",
        "anilmehta@gmail.com": "published",  # free mail: has MX without a lookup
    }
    assert "anil.mehta@acme.com" not in emails  # a published address beats a guess
    assert unsendable_reason(emails["info@acme.com"], mx_state="no_mail") == "Won't deliver"
    assert is_sendable(emails["anilmehta@gmail.com"], mx_state="no_mail")
    assert outcome.sendable == 1
    assert (await _row(engine, cid)).sendable_contacts == 1

    # The domain gets a mail server: the published addresses come back.
    later = NOW + timedelta(days=SHIPPED.config.contacts.mx_cache_days + 1)
    outcome = await _verify(engine, cid, _has_mx("acme.com"), now=later)
    assert (outcome.mx_state, outcome.sendable) == ("has_mx", 2)
    emails = await _emails(engine, cid)
    assert emails["priya.nair@acme.com"].status == "published"
    assert emails["info@acme.com"].status == "published"


async def test_dns_blip_keeps_the_known_answer(engine: AsyncEngine, db: AsyncSession) -> None:
    """Ruling AH: once the domain had MX, a DNS timeout after the cache expired changes
    nothing: no flapping to zero sendable contacts."""
    cid = await _profiled(engine, db)
    await _verify(engine, cid, _has_mx("acme.com"))
    before = await _emails(engine, cid)

    later = NOW + timedelta(days=SHIPPED.config.contacts.mx_cache_days + 1)
    resolver = FakeResolver({("acme.com", "MX"): dns.exception.Timeout()})
    outcome = await _verify(engine, cid, resolver, now=later)

    assert outcome == VerifyOutcome(cid, 2, 3, 2, "has_mx", None)
    assert resolver.calls == [("acme.com", "MX")]
    after = await _emails(engine, cid)
    assert {a: e.status for a, e in after.items()} == {a: e.status for a, e in before.items()}
    assert after["anil.mehta@acme.com"].status == "pattern_confirmed"
    row = await _row(engine, cid)
    assert (row.sendable_contacts, row.contacts_checked_at) == (2, later)


async def test_dns_unknown_on_first_check_creates_nothing(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    cid = await _profiled(engine, db)
    resolver = FakeResolver({("acme.com", "MX"): dns.resolver.LifetimeTimeout()})

    outcome = await _verify(engine, cid, resolver)

    assert (outcome.mx_state, outcome.error) == ("unknown", DNS_UNAVAILABLE)
    assert set(await _emails(engine, cid)) == {"priya.nair@acme.com", "info@acme.com"}
    assert {e.status for e in (await _emails(engine, cid)).values()} == {"published"}
    assert (await _row(engine, cid)).contacts_checked_at is None  # retried


async def test_verifier_cap_and_never_published(engine: AsyncEngine, db: AsyncSession) -> None:
    config = _config(verifier_max_per_day=1)
    guessed = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)
    verifier = FakeVerifier()

    await _verify(engine, guessed, _has_mx("acme.com"), verifier=verifier, config=config)

    assert verifier.calls == ["priya.nair@acme.com"]  # cap 1: the top target only
    emails = await _emails(engine, guessed)
    priya = emails["priya.nair@acme.com"]
    assert (priya.status, priya.verifier_result, priya.verified_at) == ("verified", "verified", NOW)
    assert is_sendable(priya, mx_state="has_mx")
    anil = emails["anil.mehta@acme.com"]
    assert (anil.status, anil.verifier_result, anil.verified_at) == ("pattern_guess", None, None)
    assert (await _row(engine, guessed)).sendable_contacts == 1

    # A second company the same day: the cap is used up.
    published = await _company(db, "beta.com")
    assert (await _profile(engine, _web(_site("beta.com")), published)).profile_status == "ok"
    resolver = _has_mx("beta.com")
    await _verify(engine, published, resolver, verifier=verifier, config=config)
    assert verifier.calls == ["priya.nair@acme.com"]

    # Next day: only the pattern address is sent; a published one never is.
    verifier.results["anil.mehta@beta.com"] = "risky"
    tomorrow = NOW + timedelta(days=1)
    outcome = await _verify(
        engine, published, resolver, verifier=verifier, config=config, now=tomorrow
    )
    assert verifier.calls == ["priya.nair@acme.com", "anil.mehta@beta.com"]
    emails = await _emails(engine, published)
    assert emails["anil.mehta@beta.com"].status == "risky"
    assert unsendable_reason(emails["anil.mehta@beta.com"], mx_state="has_mx") == (
        "Held: risky (catch-all)"
    )
    assert emails["priya.nair@beta.com"].verifier_result is None
    assert outcome.sendable == 1

    # A definite result is never asked for again.
    await _verify(
        engine,
        published,
        resolver,
        verifier=verifier,
        config=_config(verifier_max_per_day=10),
        now=tomorrow + timedelta(days=1),
    )
    assert verifier.calls == ["priya.nair@acme.com", "anil.mehta@beta.com"]


async def test_verifier_unknown_keeps_status(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)
    verifier = FakeVerifier({"priya.nair@acme.com": "unknown", "anil.mehta@acme.com": "invalid"})

    await _verify(engine, cid, _has_mx("acme.com"), verifier=verifier)

    emails = await _emails(engine, cid)
    priya, anil = emails["priya.nair@acme.com"], emails["anil.mehta@acme.com"]
    assert (priya.status, priya.verifier_result, priya.verified_at) == (
        "pattern_guess",
        "unknown",
        NOW,
    )
    assert (anil.status, anil.verifier_result) == ("invalid", "invalid")


async def test_no_verifier_calls_without_mx(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)
    verifier = FakeVerifier()
    await _verify(
        engine,
        cid,
        FakeResolver({("acme.com", "MX"): dns.resolver.NXDOMAIN()}),
        verifier=verifier,
    )
    assert verifier.calls == []


async def test_override_and_invalid(engine: AsyncEngine, db: AsyncSession, admin: User) -> None:
    cid = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)
    await _verify(
        engine,
        cid,
        _has_mx("acme.com"),
        verifier=FakeVerifier({"priya.nair@acme.com": "unknown", "anil.mehta@acme.com": "invalid"}),
    )
    emails = await _emails(engine, cid)
    guess, invalid = emails["priya.nair@acme.com"], emails["anil.mehta@acme.com"]

    row = await set_override(db, guess.id, sendable=True, actor=admin)
    await db.commit()
    assert (row.override_sendable, row.override_by_id) == (True, admin.id)
    assert row.override_at is not None
    assert is_sendable(row, mx_state="has_mx")
    assert (await _row(engine, cid)).sendable_contacts == 1

    row = await set_override(db, invalid.id, sendable=True, actor=admin)
    await db.commit()
    assert not is_sendable(row, mx_state="has_mx")  # invalid: never, override or not
    assert (await _row(engine, cid)).sendable_contacts == 1

    row = await set_override(db, guess.id, sendable=None, actor=admin)
    await db.commit()
    assert (row.override_sendable, row.override_by_id, row.override_at) == (None, None, None)
    assert unsendable_reason(row, mx_state="has_mx") == "Held: guess"
    assert (await _row(engine, cid)).sendable_contacts == 0

    published = emails["info@acme.com"]
    row = await set_override(db, published.id, sendable=False, actor=admin)
    await db.commit()
    assert unsendable_reason(row, mx_state="has_mx") == "Overridden: don't send"

    # Overrides survive a re-check.
    await _verify(engine, cid, _has_mx("acme.com"), now=NOW + timedelta(days=31))
    after = await _emails(engine, cid)
    assert after["info@acme.com"].override_sendable is False
    assert after["anil.mehta@acme.com"].override_sendable is True
    assert after["anil.mehta@acme.com"].status == "invalid"

    with pytest.raises(LookupError):
        await set_override(db, uuid.uuid4(), sendable=True, actor=admin)


async def test_exclusion_blocks_address(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db)
    db.add(DoNotContact(kind="email", value="priya.nair@acme.com", reason="Asked us to stop"))
    await db.commit()
    resolver = _has_mx("acme.com")

    outcome = await _verify(engine, cid, resolver)

    emails = await _emails(engine, cid)
    priya = emails["priya.nair@acme.com"]
    assert priya.blocked_reason == "Do-not-contact: priya.nair@acme.com — Asked us to stop"
    assert not is_sendable(priya, mx_state="has_mx")
    assert unsendable_reason(priya, mx_state="has_mx") == priya.blocked_reason
    assert emails["anil.mehta@acme.com"].blocked_reason is None
    assert outcome.sendable == 1

    await db.execute(delete(DoNotContact))
    await db.commit()
    outcome = await _verify(engine, cid, resolver, now=NOW + timedelta(days=1))
    assert (await _emails(engine, cid))["priya.nair@acme.com"].blocked_reason is None
    assert outcome.sendable == 2


async def test_published_address_on_the_dnc_list_is_blocked_when_stored(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    db.add(DoNotContact(kind="email", value="info@acme.com", reason="Bounced"))
    await db.commit()

    cid = await _profiled(engine, db)

    info = (await _emails(engine, cid))["info@acme.com"]
    assert info.blocked_reason == "Do-not-contact: info@acme.com — Bounced"
    assert not is_sendable(info, mx_state="has_mx")


async def test_partial_profile_keeps_people(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    cid = await _profiled(engine, db)
    await _verify(engine, cid, _has_mx("acme.com"))

    # The team and contact pages can't be read: nothing found earlier is dropped.
    broken = _site(contact="", team=(500, {}, b"down"))
    broken["https://acme.com/contact"] = (500, {}, b"down")
    outcome = await _profile(engine, _web(broken), cid)
    assert outcome.profile_status == "partial"
    assert set(await _people(engine, cid)) == {"Priya Nair", "Anil Mehta"}
    emails = await _emails(engine, cid)
    assert set(emails) == {"priya.nair@acme.com", "info@acme.com", "anil.mehta@acme.com"}
    assert all(p.is_target for p in (await _people(engine, cid)).values())
    assert (await _row(engine, cid)).contact_form_url == "https://acme.com/contact"

    # A full read without Anil drops him, unless one of his addresses has an override.
    anil_address = emails["anil.mehta@acme.com"]
    await set_override(db, anil_address.id, sendable=True, actor=admin)
    await db.commit()
    outcome = await _profile(engine, _web(_site(team=TEAM_PRIYA_ONLY)), cid)
    assert outcome.profile_status == "ok"
    people = await _people(engine, cid)
    assert set(people) == {"Priya Nair", "Anil Mehta"}
    assert people["Anil Mehta"].is_target is False  # kept for the override, not a target now

    await set_override(db, anil_address.id, sendable=None, actor=admin)
    await db.commit()
    await _profile(engine, _web(_site(team=TEAM_PRIYA_ONLY)), cid)
    assert set(await _people(engine, cid)) == {"Priya Nair"}
    # Published addresses no longer seen are kept.
    assert "info@acme.com" in await _emails(engine, cid)

    # The re-check removes the orphaned pattern address.
    outcome = await _verify(engine, cid, _has_mx("acme.com"), now=NOW + timedelta(days=1))
    assert set(await _emails(engine, cid)) == {"priya.nair@acme.com", "info@acme.com"}
    assert (outcome.targets, outcome.sendable) == (1, 1)


async def test_excluded_company_skipped(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db)
    await db.execute(
        update(Company)
        .where(Company.id == cid)
        .values(status="excluded", exclusion_reason="do_not_contact")
    )
    await db.commit()
    resolver = _has_mx("acme.com")

    outcome = await _verify(engine, cid, resolver)

    assert outcome.error == EXCLUDED
    assert resolver.calls == []
    assert set(await _emails(engine, cid)) == {"priya.nair@acme.com", "info@acme.com"}
    assert (await _row(engine, cid)).contacts_checked_at is None
    assert await _batch(engine, resolver) == []


class _ExcludingResolver(FakeResolver):
    """Excludes the company while its MX lookup is in flight."""

    def __init__(self, engine: AsyncEngine, cid: uuid.UUID) -> None:
        super().__init__({("acme.com", "MX"): [(10, "mx.acme.com.")]})
        self.engine, self.cid = engine, cid

    async def __call__(self, qname: str, rdtype: str) -> list[tuple[int, str]]:
        async with make_session_factory(self.engine)() as s:
            await s.execute(
                update(Company)
                .where(Company.id == self.cid)
                .values(status="excluded", exclusion_reason="do_not_contact")
            )
            await s.commit()
        return await super().__call__(qname, rdtype)


async def test_company_excluded_during_check_gets_nothing_written(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    cid = await _profiled(engine, db)

    outcome = await _verify(engine, cid, _ExcludingResolver(engine, cid))

    assert outcome.error == EXCLUDED
    assert set(await _emails(engine, cid)) == {"priya.nair@acme.com", "info@acme.com"}
    assert (await _row(engine, cid)).contacts_checked_at is None


# --- the batch ------------------------------------------------------------------------------------


async def _batch(
    engine: AsyncEngine,
    resolver: FakeResolver,
    *,
    now: datetime = NOW,
    limit: int = 10,
    config: AppConfig | None = None,
) -> list[VerifyOutcome]:
    config = config or SHIPPED.config
    return await verify_batch(
        make_session_factory(engine),
        checker=MxChecker(config.contacts, resolve=resolver),
        verifier=None,
        config=config,
        now=now,
        limit=limit,
    )


async def test_batch_isolates_crash(engine: AsyncEngine, db: AsyncSession) -> None:
    ids = {}
    for domain in ("acme.com", "beta.com"):
        ids[domain] = await _company(db, domain)
        assert (await _profile(engine, _web(_site(domain)), ids[domain])).profile_status == "ok"
    resolver = _has_mx("beta.com")
    resolver.answers[("acme.com", "MX")] = RuntimeError("boom")  # not a DNS error: a crash

    outcomes = {o.company_id: o for o in await _batch(engine, resolver)}

    assert outcomes[ids["acme.com"]].error == "Contact check error (RuntimeError)."
    assert outcomes[ids["beta.com"]] == VerifyOutcome(ids["beta.com"], 2, 3, 2, "has_mx", None)
    assert (await _row(engine, ids["beta.com"])).sendable_contacts == 2
    # Ruling AI: the crashed one is due again after an hour, the checked one after
    # recheck_after_days.
    recheck = timedelta(days=SHIPPED.config.contacts.recheck_after_days)
    crashed = await _row(engine, ids["acme.com"])
    assert crashed.contacts_checked_at == NOW - recheck + timedelta(hours=1)
    assert (await _row(engine, ids["beta.com"])).contacts_checked_at == NOW
    assert await _batch(engine, resolver, now=NOW + timedelta(minutes=30)) == []
    fixed = _has_mx("acme.com", "beta.com")
    [retried] = await _batch(engine, fixed, now=NOW + timedelta(hours=1, seconds=1))
    assert (retried.company_id, retried.error, retried.sendable) == (ids["acme.com"], None, 2)
    assert await _batch(engine, fixed, now=NOW + timedelta(days=1)) == []
    assert len(await _batch(engine, fixed, now=NOW + recheck + timedelta(hours=2))) == 2


async def test_batch_picks_due_companies_oldest_first(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    never = await _company(db, "never.com", profile_status="ok")
    old = await _company(
        db, "old.com", profile_status="partial", contacts_checked_at=NOW - timedelta(days=40)
    )
    await _company(db, "fresh.com", profile_status="ok", contacts_checked_at=NOW)
    await _company(db, "pending.com")  # never profiled
    await _company(db, "failed.com", profile_status="failed")
    await _company(db, "gone.com", profile_status="ok", status="excluded")
    resolver = _has_mx("never.com", "old.com")

    first = await _batch(engine, resolver, limit=1)
    assert [o.company_id for o in first] == [never]
    second = await _batch(engine, resolver)
    assert [o.company_id for o in second] == [old]
    assert await _batch(engine, resolver) == []


async def test_batch_dns_unknown_is_due_again(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db)
    resolver = FakeResolver({("acme.com", "MX"): dns.exception.Timeout()})

    [outcome] = await _batch(engine, resolver)

    assert outcome.error == DNS_UNAVAILABLE
    assert (await _row(engine, cid)).contacts_checked_at is None
    later = NOW + timedelta(hours=2)  # past the cached "unknown"
    [outcome] = await _batch(engine, _has_mx("acme.com"), now=later)
    assert (outcome.error, outcome.sendable) == (None, 2)


async def test_dns_unknown_domains_cannot_starve_the_batch(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Domains whose DNS just failed are left out of the claim until the cached "unknown"
    expires, so other companies still get checked."""
    flaky = [await _company(db, f"flaky{i}.com", profile_status="ok") for i in range(3)]
    resolver = FakeResolver({(f"flaky{i}.com", "MX"): dns.exception.Timeout() for i in range(3)})
    first = await _batch(engine, resolver, limit=3)
    assert sorted(o.company_id for o in first) == sorted(flaky)
    assert {o.error for o in first} == {DNS_UNAVAILABLE}
    for cid in flaky:
        assert (await _row(engine, cid)).contacts_checked_at is None

    fresh = await _company(db, "fresh.com", profile_status="ok")
    resolver.answers[("fresh.com", "MX")] = [(10, "mx.fresh.com.")]
    second = await _batch(engine, resolver, limit=3, now=NOW + timedelta(minutes=5))
    assert [o.company_id for o in second] == [fresh]
    assert len(resolver.calls) == 4  # no new lookups for the flaky domains

    for i in range(3):
        resolver.answers[(f"flaky{i}.com", "MX")] = [(10, f"mx.flaky{i}.com.")]
    third = await _batch(engine, resolver, limit=3, now=NOW + timedelta(hours=1, minutes=1))
    assert sorted(o.company_id for o in third) == sorted(flaky)
    assert {(o.error, o.mx_state) for o in third} == {(None, "has_mx")}


async def test_dont_send_override_is_never_verified(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    cid = await _profiled(engine, db, contact=CONTACT_ROLE_ONLY)
    await _verify(
        engine,
        cid,
        _has_mx("acme.com"),
        verifier=FakeVerifier({"priya.nair@acme.com": "unknown", "anil.mehta@acme.com": "unknown"}),
    )
    emails = await _emails(engine, cid)
    await set_override(db, emails["priya.nair@acme.com"].id, sendable=False, actor=admin)
    await db.commit()
    verifier = FakeVerifier()

    await _verify(engine, cid, _has_mx("acme.com"), verifier=verifier, now=NOW + timedelta(days=1))

    assert verifier.calls == ["anil.mehta@acme.com"]


# --- the sendable rule ----------------------------------------------------------------------------


def _address(**fields: Any) -> EmailAddress:
    values: dict[str, Any] = {
        "address": "a.b@acme.com",
        "kind": "personal",
        "status": "published",
        "source": "page",
        "blocked_reason": None,
        "override_sendable": None,
        **fields,
    }
    return EmailAddress(**values)


@pytest.mark.parametrize(
    ("fields", "mx_state", "reason"),
    [
        ({"status": "published"}, "has_mx", None),
        ({"status": "pattern_confirmed"}, "has_mx", None),
        ({"status": "verified"}, "has_mx", None),
        ({"status": "pattern_guess"}, "has_mx", "Held: guess"),
        ({"status": "risky"}, "has_mx", "Held: risky (catch-all)"),
        ({"status": "invalid"}, "has_mx", "Won't deliver"),
        ({"status": "pattern_guess", "override_sendable": True}, "has_mx", None),
        ({"status": "risky", "override_sendable": True}, "has_mx", None),
        ({"status": "invalid", "override_sendable": True}, "has_mx", "Won't deliver"),
        ({"status": "published", "override_sendable": False}, "has_mx", "Overridden: don't send"),
        ({"status": "verified", "override_sendable": False}, "has_mx", "Overridden: don't send"),
        ({"status": "published"}, "no_mail", "No mail server"),
        ({"status": "published"}, "unknown", "Mail server not checked yet"),
        ({"status": "pattern_guess", "override_sendable": True}, "no_mail", "No mail server"),
        ({"status": "published", "address": "a.b@gmail.com"}, "no_mail", None),
        ({"status": "published", "address": "a.b@gmail.com"}, "unknown", None),
        ({"blocked_reason": "Do-not-contact: x"}, "has_mx", "Do-not-contact: x"),
        # item 7: no-reply-style mailboxes, override or not
        ({"address": "noreply@acme.com"}, "has_mx", "Not a person's mailbox"),
        ({"address": "no-reply@acme.com", "override_sendable": True}, "has_mx", NOT_A_MAILBOX),
        ({"address": "postmaster+x@acme.com"}, "has_mx", NOT_A_MAILBOX),
        ({"address": "mailer-daemon@gmail.com"}, "has_mx", NOT_A_MAILBOX),
        ({"address": "bounces@acme.com", "kind": "role"}, "has_mx", NOT_A_MAILBOX),
        # item 2: a published address the site no longer shows
        ({"stale": True}, "has_mx", "No longer on their site"),
        ({"stale": True, "override_sendable": True}, "has_mx", None),
        ({"stale": True, "override_sendable": True}, "no_mail", "No mail server"),
        ({"stale": True, "override_sendable": False}, "has_mx", "Overridden: don't send"),
        (
            {"blocked_reason": "Do-not-contact: x", "override_sendable": True},
            "has_mx",
            "Do-not-contact: x",
        ),
    ],
)
def test_sendable_rule(fields: dict[str, Any], mx_state: str, reason: str | None) -> None:
    address = _address(**fields)
    assert unsendable_reason(address, mx_state=mx_state) == reason
    assert is_sendable(address, mx_state=mx_state) is (reason is None)


def test_service_module_imports_the_verifier_normally() -> None:
    # The verifier module installs the httpx log filter that keeps the API key out of logs.
    import salesfinder.contacts.verifier as verifier_module

    assert contacts.Verifier is verifier_module.Verifier


# --- final review fixes ---------------------------------------------------------------------------

CONTACT_FIRST_ONLY = """<html><head><title>Contact us</title></head><body>
<p>Write to priya@{domain} or info@{domain}</p></body></html>"""


async def test_address_dropped_from_the_site_is_stale(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    """Item 2: the site replaces priya.nair@ with priya@. The old address is kept but is no
    longer sendable, linked or pattern evidence; the new one is published and linked."""
    cid = await _profiled(engine, db)
    await _verify(engine, cid, _has_mx("acme.com"))
    old = (await _emails(engine, cid))["priya.nair@acme.com"]
    assert (old.last_seen_at, old.stale) == (NOW, False)

    later = NOW + timedelta(days=1)
    async with SafeFetcher(
        SHIPPED.config.profiling, **_fetch(_web(_site(contact=CONTACT_FIRST_ONLY)))
    ) as f:
        outcome = await profile_company(
            make_session_factory(engine),
            cid,
            fetcher=f,
            catalog=SHIPPED.catalog,
            facts=SHIPPED.facts,
            config=SHIPPED.config,
            now=lambda: later,
        )
    assert outcome.profile_status == "ok"

    people = await _people(engine, cid)
    emails = await _emails(engine, cid)
    old, new = emails["priya.nair@acme.com"], emails["priya@acme.com"]
    assert (old.stale, old.person_id, old.status, old.last_seen_at) == (
        True,
        None,
        "published",
        NOW,
    )
    assert unsendable_reason(old, mx_state="has_mx") == "No longer on their site"
    assert (new.stale, new.status, new.last_seen_at) == (False, "published", later)
    assert new.person_id == people["Priya Nair"].id
    assert is_sendable(new, mx_state="has_mx")

    outcome2 = await _verify(engine, cid, _has_mx("acme.com"), now=later)
    emails = await _emails(engine, cid)
    # The stale first.last address taught nothing: {first} from one address is no pattern.
    async with make_session_factory(engine)() as s:
        assert await s.get(EmailPattern, "acme.com") is None
    assert emails["anil.mehta@acme.com"].status == "pattern_guess"
    assert emails["priya.nair@acme.com"].person_id is None
    assert outcome2.sendable == 1  # Priya, by priya@

    # A teammate can still allow the old address.
    await set_override(db, old.id, sendable=True, actor=admin)
    await db.commit()
    assert is_sendable((await _emails(engine, cid))["priya.nair@acme.com"], mx_state="has_mx")

    # It comes back on the site: fresh again.
    await set_override(db, old.id, sendable=None, actor=admin)
    await db.commit()
    assert (await _profile(engine, _web(_site()), cid)).profile_status == "ok"
    assert (await _emails(engine, cid))["priya.nair@acme.com"].stale is False


def _fetch(web: FakeWeb) -> dict[str, Any]:
    return {"transport": web.transport, "resolver": web.resolver}


async def test_partial_profile_marks_nothing_stale(engine: AsyncEngine, db: AsyncSession) -> None:
    cid = await _profiled(engine, db)
    broken = _site(team=(500, {}, b"down"))
    broken["https://acme.com/contact"] = CONTACT_FIRST_ONLY.format(domain="acme.com")
    assert (await _profile(engine, _web(broken), cid)).profile_status == "partial"
    emails = await _emails(engine, cid)
    assert emails["priya.nair@acme.com"].stale is False
    assert emails["priya@acme.com"].stale is False


SHAH_TEAM = """<html><head><title>Our Team</title></head><body><ul>
<li>Amit Shah | Purchase Manager</li>
<li>Anil Shah | Head of Purchasing</li>
<li>Priya Nair | Owner</li>
<li>Ravi Kumar | Director</li>
</ul></body></html>"""
SHAH_CONTACT = """<html><head><title>Contact us</title></head><body>
<p>pnair@{domain} rkumar@{domain} ashah@{domain}</p></body></html>"""


async def test_ambiguous_pattern_address_is_a_guess_and_stays_unlinked(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Item 3: {f}{last} is learned from pnair@ and rkumar@; for Amit Shah and Anil Shah it
    gives the same ashah@, so that is only a guess, and the published ashah@ (which could be
    either of them) is linked to neither."""
    cid = await _profiled(engine, db, team=SHAH_TEAM, contact=SHAH_CONTACT)
    assert (await _emails(engine, cid))["ashah@acme.com"].person_id is None

    outcome = await _verify(engine, cid, _has_mx("acme.com"))

    async with make_session_factory(engine)() as s:
        pattern = await s.get(EmailPattern, "acme.com")
    assert pattern is not None and (pattern.pattern, pattern.support) == ("{f}{last}", 2)
    people = await _people(engine, cid)
    assert {n for n, p in people.items() if p.is_target} == {"Amit Shah", "Anil Shah"}
    emails = await _emails(engine, cid)
    ashah = emails["ashah@acme.com"]
    assert (ashah.source, ashah.person_id) == ("page", None)
    assert (outcome.targets, outcome.sendable) == (2, 0)
    assert (await _row(engine, cid)).sendable_contacts == 0

    # A published address is never sent to the paid verifier, even as someone's guess.
    verifier = FakeVerifier()
    await _verify(engine, cid, _has_mx("acme.com"), verifier=verifier, now=NOW + timedelta(days=1))
    assert verifier.calls == []


async def test_dnc_entry_blocks_stored_addresses_at_once(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Item 4: adding (and removing) a do-not-contact entry refreshes stored addresses and the
    sendable count in the same transaction, without waiting for the next contact check."""
    from salesfinder.exclusions import dnc

    cid = await _profiled(engine, db)
    await _verify(engine, cid, _has_mx("acme.com"))
    assert (await _row(engine, cid)).sendable_contacts == 2

    entry = await dnc.add_entry(db, raw="Priya.Nair@acme.com", reason="Asked us", added_by=None)
    await db.commit()
    priya = (await _emails(engine, cid))["priya.nair@acme.com"]
    assert priya.blocked_reason == "Do-not-contact: priya.nair@acme.com — Asked us"
    assert not is_sendable(priya, mx_state="has_mx")
    assert (await _row(engine, cid)).sendable_contacts == 1

    await dnc.remove_entry(db, entry.id)
    await db.commit()
    assert (await _emails(engine, cid))["priya.nair@acme.com"].blocked_reason is None
    assert (await _row(engine, cid)).sendable_contacts == 2


async def test_refresh_blocks_by_domain_matches_exclusion_for(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    from salesfinder.contacts.service import refresh_blocks
    from salesfinder.db.models import KnownRelationship
    from salesfinder.exclusions.check import exclusion_for

    contact = CONTACT_PUBLISHED.replace("or info", "or anil.mehta@gmail.com or info")
    cid = await _profiled(engine, db, contact=contact)
    db.add_all(
        [
            DoNotContact(kind="domain", value="acme.com", reason="Legal hold"),
            KnownRelationship(
                source="hubspot",
                kind="email",
                value="anil.mehta@gmail.com",
                hubspot_object="contact",
                hubspot_id="9",
                name="Anil Mehta",
                synced_at=NOW,
            ),
        ]
    )
    await db.flush()
    assert await refresh_blocks(db, domain="acme.com") == 2
    assert await refresh_blocks(db) == 1  # the gmail one
    await db.commit()
    for row in (await _emails(engine, cid)).values():
        hit = await exclusion_for(db, email=row.address)
        assert row.blocked_reason == (hit.detail if hit else None), row.address
    assert (await _row(engine, cid)).sendable_contacts == 0


async def test_verifier_failures_stop_the_batch_and_use_no_cap(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Item 5: failed calls record nothing (no verified_at, so no daily cap used), and after
    3 in a row the verifier isn't called again in the batch."""
    for domain in ("acme.com", "beta.com"):
        cid = await _company(db, domain)
        site = _site(domain, contact=CONTACT_ROLE_ONLY)
        assert (await _profile(engine, _web(site), cid)).profile_status == "ok"
    verifier = FakeVerifier()
    verifier.results = {
        f"{p}@{d}": None for p in ("priya.nair", "anil.mehta") for d in ("acme.com", "beta.com")
    }

    outcomes = await verify_batch(
        make_session_factory(engine),
        checker=MxChecker(SHIPPED.config.contacts, resolve=_has_mx("acme.com", "beta.com")),
        verifier=verifier,
        config=SHIPPED.config,
        now=NOW,
        limit=10,
    )

    assert len(outcomes) == 2 and {o.error for o in outcomes} == {None}
    assert len(verifier.calls) == 3
    async with make_session_factory(engine)() as s:
        rows = (await s.scalars(select(EmailAddress))).all()
        assert await daily_count(s, now=NOW) == 0
    guesses = [r for r in rows if r.source == "guess"]
    assert len(guesses) == 4
    assert {(r.status, r.verifier_result, r.verified_at) for r in guesses} == {
        ("pattern_guess", None, None)
    }


async def test_people_pages_and_extraction_failure_are_recorded(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 6."""
    cid = await _profiled(engine, db)
    row = await _row(engine, cid)
    assert (row.people_pages_read, row.contacts_extract_failed) == (2, False)  # contact, team

    bare = await _company(db, "bare.com")
    home = HOME.format(name="bare.com").replace('<a href="/team">Our team</a>', "")
    home = home.replace('<a href="/contact">Contact</a>', "")
    assert (await _profile(engine, _web({"https://bare.com/": home}), bare)).profile_status == "ok"
    row = await _row(engine, bare)
    assert (row.people_pages_read, row.contacts_extract_failed) == (0, False)

    def boom(*_a: object, **_k: object) -> list[object]:
        raise RuntimeError("bad page")

    monkeypatch.setattr("salesfinder.profiling.service.extract_people", boom)
    assert (await _profile(engine, _web(_site()), cid)).profile_status == "ok"
    row = await _row(engine, cid)
    assert (row.people_pages_read, row.contacts_extract_failed) == (2, True)
    assert set(await _people(engine, cid)) == {"Priya Nair", "Anil Mehta"}  # nothing dropped


async def test_invalid_domains_cannot_starve_the_batch(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Item 8: a domain that isn't a valid DNS name is recorded like a DNS failure (so the
    claim holds it back) and waits for the ordinary recheck."""
    bad = [await _company(db, f"bad{i}..com", profile_status="ok") for i in range(3)]
    resolver = _has_mx("fresh.com")
    first = await _batch(engine, resolver, limit=3)
    assert sorted(o.company_id for o in first) == sorted(bad)
    assert {o.error for o in first} == {DNS_UNAVAILABLE}
    async with make_session_factory(engine)() as s:
        mx = (await s.scalars(select(DomainMx).order_by(DomainMx.domain))).all()
    assert [(m.domain, m.state, m.error) for m in mx] == [
        (f"bad{i}..com", "unknown", "Invalid domain") for i in range(3)
    ]
    for cid in bad:
        assert (await _row(engine, cid)).contacts_checked_at == NOW

    fresh = await _company(db, "fresh.com", profile_status="ok")
    for later in (NOW + timedelta(minutes=5), NOW + timedelta(hours=2)):
        claimed = await _batch(engine, resolver, limit=3, now=later)
        assert [o.company_id for o in claimed] == (
            [fresh] if later < NOW + timedelta(hours=1) else []
        )
    assert resolver.calls == [("fresh.com", "MX")]


async def test_published_addresses_capped_per_company(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """Item 10: the first 50 published addresses, in page order."""
    listed = " ".join(f"buyer{i:02d}@acme.com" for i in range(60))
    contact = f"<html><head><title>Contact us</title></head><body><p>{listed}</p></body></html>"
    cid = await _profiled(engine, db, contact=contact)
    assert set(await _emails(engine, cid)) == {f"buyer{i:02d}@acme.com" for i in range(50)}


async def test_a_bounced_pattern_is_remembered(db: AsyncSession) -> None:
    """``drop_pattern`` marks the pattern; relearning the same pattern keeps the mark, learning
    nothing keeps the row, and a different pattern (new evidence) starts clean."""
    now = datetime(2026, 11, 10, tzinfo=UTC)
    db.add(
        EmailPattern(
            domain="acme.com", pattern="{first}.{last}", support=2, examples=[], learned_at=now
        )
    )
    await db.flush()
    await contacts.drop_pattern(db, "acme.com", now=now)

    async def stored() -> EmailPattern | None:
        return await db.scalar(
            select(EmailPattern)
            .where(EmailPattern.domain == "acme.com")
            .execution_options(populate_existing=True)
        )

    await contacts._save_pattern(
        db, "acme.com", LearnedPattern("{first}.{last}", 3, ("a.b@acme.com",)), now=now
    )
    row = await stored()
    assert row is not None and (row.support, row.bounced_at) == (3, now)
    await contacts._save_pattern(db, "acme.com", None, now=now)
    row = await stored()
    assert row is not None and row.bounced_at == now
    await contacts._save_pattern(
        db, "acme.com", LearnedPattern("{f}{last}", 2, ("ab@acme.com",)), now=now
    )
    row = await stored()
    assert row is not None and (row.pattern, row.bounced_at) == ("{f}{last}", None)
    await contacts._save_pattern(db, "acme.com", None, now=now)
    assert await stored() is None
