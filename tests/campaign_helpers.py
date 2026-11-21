"""Shared seeding for campaign / drafting tests: a confirmed-facts sheet, a cohort, companies
with a target person, a published address and fresh MX, and canned fake-Claude replies."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.campaigns import service
from salesfinder.catalog import Catalog, Claim, Facts, Persona, Warehouse
from salesfinder.config import AppConfig, UsageConfig
from salesfinder.db.models import (
    Campaign,
    Cohort,
    Company,
    DomainMx,
    EmailAddress,
    Mailbox,
    Message,
    Person,
    Sequence,
    User,
)
from salesfinder.writer.compose import assemble

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
# Every writer prompt has this heading; the fake replies to any prompt containing it.
MARKER = "ELCHEMY FACTS (the only claims you may make)"

FACTS = Facts(
    company_name="Elchemy",
    website="https://elchemy.com",
    postal_address="1 Main Street, Suite 4, Bridgewater, NJ 08807",
    postal_address_confirmed=True,
    warehouses=[
        Warehouse(city="Houston", state="TX", label="Houston, TX", lat=29.76, lon=-95.37),
    ],
    claims=[Claim(id="docs", text="CoA and SDS supplied with every order", confirmed=True)],
    personas={
        "buyer": Persona(name="Asha Rao", title="Sales Lead", confirmed=True),
        "supplier": Persona(name="Vikram Shah", title="Sourcing Manager", confirmed=True),
    },
)
CATALOG = Catalog(products=[])
CONFIG = AppConfig(usage=UsageConfig(max_daily_claude_calls=50))

CLEAN = {
    "first_subject": "Surfactants for your body wash line",
    "first_body": (
        "Hi there,\n\nDo you buy surfactants for your body wash line? Elchemy sends the CoA "
        "and SDS with every order.\n\nWould a sample help your next production run?\n\nThanks,"
    ),
    "followup_subject": "Re: Surfactants for your body wash line",
    "followup_body": (
        "Hi there,\n\nA quick follow-up: would a sample of our surfactants be useful for "
        "your team?\n\nThanks,"
    ),
}
PRICED = {
    **CLEAN,
    "first_body": "Hi there,\n\nWe can offer surfactants at $1.20 per kg.\n\nThanks,",
}


def ok_reply(written: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(written),
    }


async def seed_user(db: AsyncSession) -> User:
    user = User(email=f"u-{uuid.uuid4().hex[:8]}@elchemy.com", name="Mira", role="member")
    db.add(user)
    await db.flush()
    return user


async def seed_cohort(
    db: AsyncSession,
    user: User,
    *,
    side: str = "buyer",
    definition: dict[str, Any] | None = None,
    name: str | None = None,
) -> Cohort:
    cohort = Cohort(
        name=name or f"cohort-{uuid.uuid4().hex[:6]}",
        side=side,
        definition=definition if definition is not None else {"side": side},
        created_by_id=user.id,
    )
    db.add(cohort)
    await db.flush()
    return cohort


async def seed_company(
    db: AsyncSession,
    *,
    name: str = "Acme Soap Works",
    domain: str = "acme-soap.com",
    side: str = "buyer",
    score: int = 50,
    address: str | None = "jane.doe",
    status: str = "published",
    checked_at: datetime = NOW,
) -> tuple[Company, Person, EmailAddress | None]:
    """A profiled company (contacts checked at ``checked_at``) with one target, Jane Doe
    (rank 1), her ``address``@``domain`` (None: no address) and the domain's fresh MX."""
    company = Company(
        side=side,
        name=name,
        domain=domain,
        website_url=f"https://{domain}",
        status="new",
        profile_status="ok",
        profiled_at=checked_at,
        scored_at=checked_at,
        score=score,
        contacts_checked_at=checked_at,
        contacts_found=1,
        sendable_contacts=1,
    )
    db.add(company)
    await db.flush()
    person = await add_person(db, company, "Jane Doe", rank=1)
    row = None
    if address is not None:
        row = await add_address(db, company, person, f"{address}@{domain}", status=status)
    db.add(
        DomainMx(domain=domain, state="has_mx", mx_hosts=[f"mx.{domain}"], checked_at=checked_at)
    )
    await db.flush()
    return company, person, row


async def add_person(
    db: AsyncSession,
    company: Company,
    full_name: str,
    *,
    rank: int | None,
    target: bool = True,
    title: str = "Purchasing Manager",
) -> Person:
    first, _, last = full_name.partition(" ")
    person = Person(
        company_id=company.id,
        full_name=full_name,
        name_key=full_name.lower(),
        first_name=first,
        last_name=last or None,
        title=title,
        title_category="procurement",
        is_target=target,
        rank=rank,
        source_url=f"https://{company.domain}/team",
    )
    db.add(person)
    await db.flush()
    return person


async def add_address(
    db: AsyncSession,
    company: Company,
    person: Person | None,
    address: str,
    *,
    status: str = "published",
    override: bool | None = None,
    stale: bool = False,
    blocked_reason: str | None = None,
) -> EmailAddress:
    row = EmailAddress(
        company_id=company.id,
        person_id=person.id if person else None,
        address=address,
        kind="personal",
        status=status,
        source="page" if status == "published" else "pattern",
        override_sendable=override,
        stale=stale,
        blocked_reason=blocked_reason,
    )
    db.add(row)
    await db.flush()
    return row


async def seed_mailbox(
    db: AsyncSession,
    user: User,
    *,
    side: str = "buyer",
    address: str | None = None,
    enabled: bool = False,
    secret_name: str | None = None,
) -> Mailbox:
    """A mailbox on the separate sending domain (flushed, not committed)."""
    address = address or f"{side}-{uuid.uuid4().hex[:6]}@send.example.org"
    mailbox = Mailbox(
        address=address,
        side=side,
        username=address,
        secret_name=secret_name or f"box_{uuid.uuid4().hex[:8]}",
        enabled=enabled,
        created_by_id=user.id,
    )
    db.add(mailbox)
    await db.flush()
    return mailbox


async def seed_campaign(
    db: AsyncSession,
    *,
    user: User | None = None,
    side: str = "buyer",
    cap: int = 40,
    name: str | None = None,
    activate: bool = True,
    now: datetime = NOW,
    mailbox: bool = True,
) -> Campaign:
    """A campaign over a no-condition cohort of ``side`` (activated unless ``activate`` is
    False; given a mailbox of its side unless ``mailbox`` is False). Commits."""
    user = user or await seed_user(db)
    cohort = await seed_cohort(db, user, side=side)
    campaign = await service.create_campaign(
        db,
        name=name or f"campaign-{uuid.uuid4().hex[:6]}",
        side=side,
        cohort_id=cohort.id,
        daily_draft_cap=cap,
        actor=user,
    )
    if mailbox:
        box = await seed_mailbox(db, user, side=side)
        await service.set_mailbox(db, campaign.id, box.id, actor=user)
    if activate:
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG, now=now)
    await db.commit()
    return campaign


async def seed_draft(
    db: AsyncSession,
    campaign: Campaign,
    *,
    domain: str = "acme-soap.com",
    address: str = "jane.doe",
    written: dict[str, str] = CLEAN,
    status: str = "awaiting_approval",
    gate_errors: list[str] | None = None,
    checked_at: datetime | None = None,
) -> Message:
    """A stored first email (and its pending follow-up) of ``campaign`` to Jane Doe at a new
    company on ``domain``, as drafting would store it; contacts and MX checked at
    ``checked_at`` (default: the real now, so live gate checks pass). Commits."""
    side = "supplier" if campaign.side == "supplier" else "buyer"
    company, person, row = await seed_company(
        db,
        name=domain,
        domain=domain,
        address=address,
        side=side,
        checked_at=checked_at or datetime.now(UTC),
    )
    assert row is not None
    sequence = Sequence(
        campaign_id=campaign.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=row.id,
        recipient=row.address,
        status="awaiting_approval",
    )
    db.add(sequence)
    await db.flush()
    first = assemble(
        written["first_subject"],
        written["first_body"],
        facts=FACTS,
        writing=CONFIG.writing,
        side=side,
    )
    second = assemble(
        written["followup_subject"], written["followup_body"], facts=FACTS,
        writing=CONFIG.writing, side=side,
    )  # fmt: skip
    message = Message(
        sequence_id=sequence.id,
        step=1,
        subject=first[0],
        body=first[1],
        status=status,
        gate_errors=gate_errors or [],
    )
    db.add_all(
        [
            message,
            Message(
                sequence_id=sequence.id,
                step=2,
                subject=second[0],
                body=second[1],
                status="pending",
                gate_errors=[],
            ),
        ]
    )
    await db.commit()
    return message
