from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import KnownRelationship
from salesfinder.exclusions import check, dnc
from salesfinder.suppressions import service as suppressions

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def _known(kind: str, value: str, **kw: str) -> KnownRelationship:
    return KnownRelationship(
        source="hubspot",
        kind=kind,
        value=value,
        hubspot_object="company" if kind == "domain" else "contact",
        hubspot_id="1",
        synced_at=NOW,
        **kw,
    )


async def test_add_dnc_normalizes(db: AsyncSession) -> None:
    dom = await dnc.add_entry(db, raw=" https://WWW.Acme.co.in/x ", reason="asked", added_by=None)
    assert (dom.kind, dom.value) == ("domain", "acme.co.in")
    mail = await dnc.add_entry(db, raw="Bob@ACME.com", reason="asked", added_by=None)
    assert (mail.kind, mail.value) == ("email", "bob@acme.com")


async def test_add_dnc_rejects_garbage_and_duplicates(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match=r"Enter an email address or a domain\."):
        await dnc.add_entry(db, raw="not a thing", reason="x", added_by=None)
    await dnc.add_entry(db, raw="acme.com", reason="x", added_by=None)
    with pytest.raises(ValueError, match=r"Already on the list\."):
        await dnc.add_entry(db, raw="HTTP://www.ACME.com/", reason="x", added_by=None)


async def test_dnc_reason_required_and_remove_list(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await dnc.add_entry(db, raw="acme.com", reason="   ", added_by=None)
    with pytest.raises(ValueError):
        await dnc.add_entry(db, raw="acme.com", reason="x" * 201, added_by=None)
    first = await dnc.add_entry(db, raw="a.com", reason=" r ", added_by=None)
    assert first.reason == "r"
    await db.commit()
    second = await dnc.add_entry(db, raw="b.com", reason="r", added_by=None)
    await db.commit()
    assert [e.value for e in await dnc.list_entries(db)] == ["b.com", "a.com"]
    removed = await dnc.remove_entry(db, second.id)
    assert removed.value == "b.com"
    assert [e.value for e in await dnc.list_entries(db)] == ["a.com"]
    with pytest.raises(LookupError):
        await dnc.remove_entry(db, second.id)


async def test_exclusion_dnc_domain_covers_emails_and_subdomains(db: AsyncSession) -> None:
    await dnc.add_entry(db, raw="acme.com", reason="Asked us not to email", added_by=None)
    hit = await check.exclusion_for(db, email="x@sales.acme.com")
    assert hit is not None and hit.reason == "do_not_contact"
    assert hit.detail == "Do-not-contact: acme.com — Asked us not to email"
    other = await check.exclusion_for(db, domain="https://shop.acme.com")
    assert other is not None and other.reason == "do_not_contact"


async def test_exclusion_known_relationship(db: AsyncSession) -> None:
    db.add(_known("domain", "acme.com", name="Acme", owner_name="Priya Shah"))
    await db.flush()
    hit = await check.exclusion_for(db, domain="ACME.com")
    assert hit is not None
    assert hit.reason == "existing_relationship"
    assert "Priya Shah" in hit.detail


async def test_free_mail_domain_not_matched_by_contact_domain(db: AsyncSession) -> None:
    db.add(_known("email", "joe@gmail.com"))
    db.add(_known("domain", "gmail.com"))
    await db.flush()
    assert await check.exclusion_for(db, email="amy@gmail.com") is None
    assert await check.exclusion_for(db, email="Joe@Gmail.com") is not None


async def test_dnc_wins_over_known(db: AsyncSession) -> None:
    db.add(_known("domain", "acme.com", name="Acme"))
    await dnc.add_entry(db, raw="acme.com", reason="no", added_by=None)
    hit = await check.exclusion_for(db, domain="acme.com")
    assert hit is not None and hit.reason == "do_not_contact"


async def test_no_match(db: AsyncSession) -> None:
    assert await check.exclusion_for(db, domain="nothing.com", email="a@nothing.com") is None
    assert await check.exclusion_for(db) is None


# --------------------------------------------------------------------------- Phase 6


async def test_invalid_email_fails_closed(db: AsyncSession) -> None:
    for bad in ("not an email", "", "jane@", "jane@acme..com"):
        hit = await check.exclusion_for(db, email=bad)
        assert hit == check.Exclusion("invalid_address", "Not a valid email address")
    # Even with a clean company domain alongside it.
    hit = await check.exclusion_for(db, domain="acme.com", email="nope")
    assert hit is not None and hit.reason == "invalid_address"


async def test_suppressed_beats_dnc(db: AsyncSession) -> None:
    await dnc.add_entry(db, raw="acme.com", reason="Competitor", added_by=None)
    await dnc.add_entry(db, raw="jane@acme.com", reason="Asked", added_by=None)
    await suppressions.suppress(db, "Jane@Acme.com", reason="unsubscribed", detail="Asked")
    await db.commit()
    row = await suppressions.is_suppressed(db, "jane@acme.com")
    assert row is not None
    day = f"{row.created_at.astimezone(UTC):%Y-%m-%d}"
    hit = await check.exclusion_for(db, domain="acme.com", email="JANE@acme.com")
    assert hit == check.Exclusion("suppressed", f"Unsubscribed on {day}")
    other = await check.exclusion_for(db, email="raj@acme.com")  # not suppressed: DNC domain
    assert other is not None and other.reason == "do_not_contact"


async def test_suppression_details(db: AsyncSession) -> None:
    await suppressions.suppress(db, "b@acme.com", reason="bounced", detail="550 5.1.1 no user")
    await suppressions.suppress(db, "m@acme.com", reason="manual", detail="Wrong person")
    await db.commit()
    row = await suppressions.is_suppressed(db, "b@acme.com")
    assert row is not None
    day = f"{row.created_at.astimezone(UTC):%Y-%m-%d}"
    assert await check.exclusion_for(db, email="b@acme.com") == check.Exclusion(
        "suppressed", f"Bounced on {day}"
    )
    assert await check.exclusion_for(db, email="m@acme.com") == check.Exclusion(
        "suppressed", "Suppressed: Wrong person"
    )


async def test_exclusions_for_emails_agrees_with_exclusion_for(db: AsyncSession) -> None:
    db.add(_known("email", "joe@known.com", name="Joe"))
    db.add(_known("domain", "known-co.com", name="Known Co"))
    await dnc.add_entry(db, raw="dnc@acme.com", reason="Asked", added_by=None)
    await dnc.add_entry(db, raw="blocked.com", reason="Competitor", added_by=None)
    await suppressions.suppress(db, "unsub@acme.com", reason="unsubscribed", detail="x")
    await suppressions.suppress(db, "bounce@blocked.com", reason="bounced", detail="x")
    await db.commit()
    emails = [
        "not an email",
        "Unsub@ACME.com",
        "dnc@acme.com",
        "bounce@blocked.com",  # suppressed beats the domain DNC
        "x@sales.blocked.com",
        "Joe@known.com",
        "amy@known-co.com",
        "clean@nowhere.com",
    ]
    batch = await check.exclusions_for_emails(db, emails)
    single = {e: await check.exclusion_for(db, email=e) for e in emails}
    assert batch == single
    reasons = [hit.reason if hit else None for hit in (batch[e] for e in emails)]
    assert reasons == [
        "invalid_address",
        "suppressed",
        "do_not_contact",
        "suppressed",
        "do_not_contact",
        "existing_relationship",
        "existing_relationship",
        None,
    ]
