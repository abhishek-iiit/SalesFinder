"""The exclusion sweep: stored companies are re-excluded when the lists learn about them."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fakes.hubspot import FAKE_TOKEN, FakeHubSpot, company, contact, owner
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import Company, DoNotContact, KnownRelationship
from salesfinder.exclusions import check, sweep, sync

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def _now() -> datetime:
    return NOW


def _co(domain: str, *, side: str = "buyer", status: str = "new", **kw: str) -> Company:
    return Company(
        side=side,
        name=domain,
        domain=domain,
        website_url=f"https://{domain}",
        status=status,
        **kw,
    )


def _known(value: str, *, obj: str = "company", **kw: str | None) -> KnownRelationship:
    return KnownRelationship(
        source="hubspot",
        kind="domain",
        value=value,
        hubspot_object=obj,
        hubspot_id="1",
        synced_at=NOW,
        **kw,
    )


async def _company(engine: AsyncEngine, domain: str, side: str = "buyer") -> Company:
    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(Company).where(Company.domain == domain, Company.side == side))
        assert row is not None
        return row


async def _expected_detail(db: AsyncSession, domain: str) -> str:
    hit = await check.exclusion_for(db, domain=domain)
    assert hit is not None
    return hit.detail


async def test_hubspot_sync_excludes_already_stored_company(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    db.add_all([_co("acme.com"), _co("acme.com", side="supplier"), _co("other.com")])
    await db.commit()

    fake = FakeHubSpot(
        [company("101", "Acme Chemicals", domain="acme.com", owner="7")],
        [],
        [owner("7", first="Priya", last="Shah")],
    )
    result = await sync.run_sync(
        make_session_factory(engine), token=FAKE_TOKEN, now=_now, transport=fake.transport
    )
    fake.assert_read_only()

    assert result.status == "succeeded"
    for side in ("buyer", "supplier"):
        row = await _company(engine, "acme.com", side)
        assert (row.status, row.exclusion_reason) == ("excluded", "existing_relationship")
        assert row.exclusion_detail == "HubSpot company Acme Chemicals (owner: Priya Shah)"
    assert (await _company(engine, "other.com")).status == "new"


async def test_contact_only_sync_excludes_stored_company(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    db.add(_co("acme.com"))
    await db.commit()
    fake = FakeHubSpot([], [contact("201", "joe@acme.com")], [])
    await sync.run_sync(
        make_session_factory(engine), token=FAKE_TOKEN, now=_now, transport=fake.transport
    )
    row = await _company(engine, "acme.com")
    assert (row.status, row.exclusion_reason) == ("excluded", "existing_relationship")
    assert row.exclusion_detail == "HubSpot contact joe@acme.com"


async def test_sweep_details_match_the_exclusion_check(db: AsyncSession) -> None:
    db.add_all(
        [
            _co("dnc.com"),
            _co("named.com"),
            _co("nameless.com"),
            _co("contact.com"),
            _co("empty-owner.com"),
        ]
    )
    db.add(DoNotContact(kind="domain", value="dnc.com", reason="Asked us not to email"))
    db.add_all(
        [
            _known("named.com", name="Named Inc", owner_name="Priya Shah"),
            _known("nameless.com", name=None, owner_name=None),
            _known("contact.com", obj="contact", name="Ops Team", owner_name="Ana"),
            _known("empty-owner.com", name="", owner_name=""),
        ]
    )
    await db.flush()

    assert await sweep.sweep_exclusions(db) == 5
    db.expire_all()
    rows = {r.domain: r for r in await db.scalars(select(Company))}
    assert rows["dnc.com"].exclusion_detail == "Do-not-contact: dnc.com — Asked us not to email"
    assert rows["named.com"].exclusion_detail == "HubSpot company Named Inc (owner: Priya Shah)"
    assert rows["nameless.com"].exclusion_detail == "HubSpot company nameless.com"
    assert rows["contact.com"].exclusion_detail == "HubSpot contact Ops Team (owner: Ana)"
    assert rows["empty-owner.com"].exclusion_detail == "HubSpot company empty-owner.com"
    for domain, row in rows.items():
        assert row.status == "excluded"
        assert row.exclusion_detail == await _expected_detail(db, domain)
    assert rows["dnc.com"].exclusion_reason == "do_not_contact"
    assert rows["named.com"].exclusion_reason == "existing_relationship"


async def test_do_not_contact_takes_precedence(db: AsyncSession) -> None:
    db.add(_co("acme.com"))
    db.add(DoNotContact(kind="domain", value="acme.com", reason="Legal hold"))
    db.add(_known("acme.com", name="Acme"))
    await db.flush()

    assert await sweep.sweep_exclusions(db) == 1
    db.expire_all()
    row = await db.scalar(select(Company))
    assert row is not None
    assert (row.exclusion_reason, row.exclusion_detail) == (
        "do_not_contact",
        "Do-not-contact: acme.com — Legal hold",
    )


async def test_already_excluded_company_is_untouched(db: AsyncSession) -> None:
    db.add(
        _co(
            "acme.com",
            status="excluded",
            exclusion_reason="existing_relationship",
            exclusion_detail="HubSpot company Old Acme",
        )
    )
    db.add(DoNotContact(kind="domain", value="acme.com", reason="Later request"))
    await db.commit()  # a new transaction for the sweep, so a touched row gets a new now()
    row = await db.scalar(select(Company))
    assert row is not None
    before = row.updated_at

    assert await sweep.sweep_exclusions(db) == 0
    db.expire_all()
    row = await db.scalar(select(Company))
    assert row is not None
    assert (row.exclusion_reason, row.exclusion_detail, row.updated_at) == (
        "existing_relationship",
        "HubSpot company Old Acme",
        before,
    )


async def test_email_entries_and_unrelated_domains_exclude_nothing(db: AsyncSession) -> None:
    db.add(_co("acme.com"))
    db.add(DoNotContact(kind="email", value="bob@acme.com", reason="unsubscribed"))
    db.add(
        KnownRelationship(
            source="hubspot",
            kind="email",
            value="ann@acme.com",
            hubspot_object="contact",
            hubspot_id="2",
            synced_at=NOW,
        )
    )
    db.add(_known("beta.com"))
    await db.flush()
    assert await sweep.sweep_exclusions(db) == 0


async def test_long_detail_is_clipped_to_the_column(db: AsyncSession) -> None:
    domain = ("a" * 60 + ".") * 3 + "com"
    db.add(_co(domain))
    db.add(DoNotContact(kind="domain", value=domain, reason="r" * 200))
    await db.flush()
    assert await sweep.exclude_do_not_contact(db, value=domain) == 1
    db.expire_all()
    row = await db.scalar(select(Company))
    assert row is not None and row.exclusion_detail is not None
    assert len(row.exclusion_detail) == 300
    assert row.exclusion_detail.startswith(f"Do-not-contact: {domain} — rrr")


async def test_exclude_do_not_contact_for_one_value(db: AsyncSession) -> None:
    db.add_all([_co("a.com"), _co("b.com")])
    db.add_all(
        [
            DoNotContact(kind="domain", value="a.com", reason="x"),
            DoNotContact(kind="domain", value="b.com", reason="y"),
        ]
    )
    await db.flush()
    assert await sweep.exclude_do_not_contact(db, value="a.com") == 1
    db.expire_all()
    statuses = {r.domain: r.status for r in await db.scalars(select(Company))}
    assert statuses == {"a.com": "excluded", "b.com": "new"}
