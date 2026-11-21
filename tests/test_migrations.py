import uuid
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import null, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from salesfinder.db.base import Base
from salesfinder.db.migrate import current_and_head, downgrade_to, upgrade_to_head
from salesfinder.db.models import (
    Campaign,
    Cohort,
    Company,
    Conversation,
    DiscoveryDrop,
    DiscoveryJob,
    DomainMx,
    EmailAddress,
    EmailPattern,
    Mailbox,
    MailboxFolder,
    MailboxMessage,
    Message,
    Observation,
    OutboxFeedback,
    Person,
    Run,
    Sequence,
    SignalSetting,
    Suppression,
    UsageEvent,
    User,
)
from salesfinder.db.models.companies import PROFILE_STATUSES
from salesfinder.db.models.contacts import (
    EMAIL_KINDS,
    EMAIL_SOURCES,
    EMAIL_STATUSES,
    MX_STATES,
    TITLE_CATEGORIES,
)
from salesfinder.db.models.mail import (
    CONVERSATION_STATUSES,
    INBOUND_KINDS,
    MAILBOX_FOLDERS,
    MAILBOX_SECURITY,
    MATCHED_BY,
    REPLY_INTENTS,
)
from salesfinder.db.models.ops import RUN_END_REASONS
from salesfinder.db.models.outreach import (
    CAMPAIGN_STATUSES,
    MESSAGE_KINDS,
    MESSAGE_STATUSES,
    SEQUENCE_STATUSES,
    SUPPRESSION_REASONS,
)


async def test_migrations_at_head(database_url: str) -> None:
    current, head = await current_and_head(database_url)
    assert current == head


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    """Columns, types, indexes and server defaults (final-wave M6: a default set only in a
    migration, or only in a model, is a difference too)."""
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(
                MigrationContext.configure(
                    c, opts={"compare_type": True, "compare_server_default": True}
                ),
                Base.metadata,
            )
        )
    assert diff == []


async def test_email_unique(db: AsyncSession) -> None:
    db.add(User(email="a@x.com", name="A", role="member"))
    await db.commit()
    db.add(User(email="a@x.com", name="B", role="member"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_role_check(db: AsyncSession) -> None:
    db.add(User(email="b@x.com", name="B", role="owner"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_round_trip(database_url: str, engine: AsyncEngine) -> None:
    try:
        await downgrade_to(database_url, "0001")
        current, _ = await current_and_head(database_url)
        assert current == "0001"
    finally:
        await upgrade_to_head(database_url)
    current, head = await current_and_head(database_url)
    assert current == head == "0016"


async def _user(db: AsyncSession) -> User:
    user = User(email="j@x.com", name="J", role="member")
    db.add(user)
    await db.flush()
    return user


def _job(user: User, **kw: object) -> DiscoveryJob:
    fields: dict[str, object] = {
        "side": "buyer",
        "params": {"products": [], "industries": [], "regions": []},
        "queries": [],
        "pages_per_query": 1,
        "estimated_searches": 0,
        "estimated_cost_usd": 0,
        "status": "estimated",
        "created_by_id": user.id,
    }
    fields.update(kw)
    return DiscoveryJob(**fields)


def _company(side: str, domain: str = "acme.com", **kw: object) -> Company:
    fields: dict[str, object] = {
        "side": side,
        "name": "Acme",
        "domain": domain,
        "website_url": f"https://{domain}",
        "status": "new",
    }
    fields.update(kw)
    return Company(**fields)


async def test_company_unique_per_side(db: AsyncSession) -> None:
    db.add(_company("buyer"))
    await db.commit()
    db.add(_company("supplier"))
    await db.commit()
    db.add(_company("buyer"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_status_checks(db: AsyncSession) -> None:
    user = await _user(db)
    db.add(_job(user, status="bogus"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(_company("buyer", status="bogus"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_drops_cascade_with_job(db: AsyncSession) -> None:
    user = await _user(db)
    job = _job(user)
    db.add(job)
    await db.flush()
    company = _company("buyer", discovered_by_job_id=job.id)
    db.add(company)
    db.add(DiscoveryDrop(job_id=job.id, url="https://x.com", reason="junk"))
    await db.commit()
    await db.execute(text("DELETE FROM discovery_jobs"))
    await db.commit()
    assert (await db.scalars(select(DiscoveryDrop))).all() == []
    await db.refresh(company)
    assert company.discovered_by_job_id is None


async def test_observation_cascades_with_company(db: AsyncSession) -> None:
    company = _company("buyer")
    db.add(company)
    await db.flush()
    db.add(Observation(company_id=company.id, code="EXPORTS", value={"found": False}))
    await db.commit()
    obs = (await db.scalars(select(Observation))).one()
    assert obs.source == "profiler" and obs.observed_at is not None
    await db.execute(text("DELETE FROM companies"))
    await db.commit()
    assert (await db.scalars(select(Observation))).all() == []


async def test_profile_defaults_and_check(db: AsyncSession) -> None:
    company = _company("buyer")
    db.add(company)
    await db.commit()
    await db.refresh(company)
    assert company.profile_status == "pending"
    assert (company.pages_fetched, company.disqualified, company.score) == (0, False, None)
    company.profile_status = "bogus"
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_cohort_name_unique_and_side_check(db: AsyncSession) -> None:
    user = await _user(db)
    fields: dict[str, object] = {"side": "buyer", "definition": {}, "created_by_id": user.id}
    db.add(Cohort(name="Hot", **fields))
    await db.commit()
    db.add(Cohort(name="Hot", **fields))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(Cohort(name="Other", **{**fields, "side": "bogus"}))
    with pytest.raises(IntegrityError):
        await db.commit()


@pytest.mark.parametrize("status", [*PROFILE_STATUSES.replace("'", "").split(","), "bogus"])
async def test_profile_status_check_matches_model(db: AsyncSession, status: str) -> None:
    # compare_metadata ignores check constraints, so pin the migration to the model's values.
    db.add(_company("buyer", profile_status=status))
    if status == "bogus":
        with pytest.raises(IntegrityError):
            await db.commit()
    else:
        await db.commit()


@pytest.mark.parametrize("state", ["proposed", "confirmed", "skipped", "rejected", "bogus"])
async def test_signal_setting_state_check_matches_model(db: AsyncSession, state: str) -> None:
    db.add(SignalSetting(code="EXPORTS", state=state))
    if state == "bogus":
        with pytest.raises(IntegrityError):
            await db.commit()
    else:
        await db.commit()


def _person(company: Company, name: str = "Jane Doe", **kw: object) -> Person:
    fields: dict[str, object] = {
        "company_id": company.id,
        "full_name": name,
        "name_key": name.lower(),
        "title_category": "other",
        "source_url": f"https://{company.domain}/team",
    }
    fields.update(kw)
    return Person(**fields)


def _email(company: Company, address: str = "jane@acme.com", **kw: object) -> EmailAddress:
    fields: dict[str, object] = {
        "company_id": company.id,
        "address": address,
        "kind": "personal",
        "status": "published",
        "source": "page",
    }
    fields.update(kw)
    return EmailAddress(**fields)


async def _saved_company(db: AsyncSession) -> Company:
    company = _company("buyer")
    db.add(company)
    await db.flush()
    return company


async def test_people_defaults_and_company_cascade(db: AsyncSession) -> None:
    company = await _saved_company(db)
    person = _person(company)
    db.add(person)
    db.add(_email(company))
    db.add(_email(company, "info@acme.com", kind="role"))
    await db.commit()
    await db.refresh(person)
    await db.refresh(company)
    assert person.is_target is False and person.rank is None
    assert (company.contacts_found, company.sendable_contacts) == (0, 0)
    assert company.contact_form_url is None and company.contacts_checked_at is None
    await db.execute(text("DELETE FROM companies"))
    await db.commit()
    assert (await db.scalars(select(Person))).all() == []
    assert (await db.scalars(select(EmailAddress))).all() == []


async def test_person_name_key_unique_per_company(db: AsyncSession) -> None:
    company = await _saved_company(db)
    other = _company("supplier")
    db.add(other)
    db.add(_person(company))
    await db.commit()
    db.add(_person(other))
    await db.commit()
    db.add(_person(company, "Jane  Doe", name_key="jane doe"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_email_person_set_null_on_person_delete(db: AsyncSession) -> None:
    company = await _saved_company(db)
    person = _person(company)
    db.add(person)
    await db.flush()
    email = _email(company, person_id=person.id)
    db.add(email)
    await db.commit()
    await db.execute(text("DELETE FROM people"))
    await db.commit()
    await db.refresh(email)
    assert email.person_id is None


async def test_email_unique_per_company(db: AsyncSession) -> None:
    company = await _saved_company(db)
    other = _company("supplier")
    db.add(other)
    db.add(_email(company))
    await db.commit()
    db.add(_email(other))
    await db.commit()
    db.add(_email(company))
    with pytest.raises(IntegrityError):
        await db.commit()


async def _accepts(db: AsyncSession, obj: object, ok: bool) -> None:
    db.add(obj)
    if ok:
        await db.commit()
    else:
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.parametrize("value", [*TITLE_CATEGORIES, "bogus"])
async def test_title_category_check_matches_model(db: AsyncSession, value: str) -> None:
    company = await _saved_company(db)
    await _accepts(db, _person(company, title_category=value), value != "bogus")


@pytest.mark.parametrize("value", [*EMAIL_STATUSES, "bogus"])
async def test_email_status_check_matches_model(db: AsyncSession, value: str) -> None:
    company = await _saved_company(db)
    await _accepts(db, _email(company, status=value), value != "bogus")


@pytest.mark.parametrize("value", [*EMAIL_KINDS, "bogus"])
async def test_email_kind_check_matches_model(db: AsyncSession, value: str) -> None:
    company = await _saved_company(db)
    await _accepts(db, _email(company, kind=value), value != "bogus")


@pytest.mark.parametrize("value", [*EMAIL_SOURCES, "bogus"])
async def test_email_source_check_matches_model(db: AsyncSession, value: str) -> None:
    company = await _saved_company(db)
    await _accepts(db, _email(company, source=value), value != "bogus")


@pytest.mark.parametrize("value", [*MX_STATES, "bogus"])
async def test_mx_state_check_matches_model(db: AsyncSession, value: str) -> None:
    from datetime import UTC, datetime

    row = DomainMx(domain="acme.com", state=value, checked_at=datetime.now(UTC))
    await _accepts(db, row, value != "bogus")
    if value != "bogus":
        await db.refresh(row)
        assert row.mx_hosts == []


async def test_email_pattern_roundtrip(db: AsyncSession) -> None:
    from datetime import UTC, datetime

    db.add(
        EmailPattern(
            domain="acme.com",
            pattern="first.last",
            support=2,
            examples=["a.b@acme.com"],
            learned_at=datetime.now(UTC),
        )
    )
    await db.commit()
    row = (await db.scalars(select(EmailPattern))).one()
    assert row.examples == ["a.b@acme.com"]


async def test_latest_flag_backfill_marks_only_the_newest(
    database_url: str, engine: AsyncEngine
) -> None:
    try:
        await downgrade_to(database_url, "0008")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO companies (id, side, name, domain, website_url, status)"
                    " VALUES ('00000000-0000-0000-0000-0000000000c1', 'buyer', 'A', 'a.com',"
                    " 'https://a.com', 'new')"
                )
            )
            for oid, code, at in [
                ("00000000-0000-0000-0000-0000000000a1", "EXPORTS", "2026-01-01"),
                ("00000000-0000-0000-0000-0000000000a2", "EXPORTS", "2026-02-01"),
                ("00000000-0000-0000-0000-0000000000a3", "QUALITY_CERTS", "2026-01-01"),
                # same instant: the higher id wins
                ("00000000-0000-0000-0000-0000000000a4", "QUALITY_CERTS", "2026-01-01"),
            ]:
                await conn.execute(
                    text(
                        "INSERT INTO observations (id, company_id, code, value, observed_at,"
                        " source) VALUES (CAST(:id AS uuid),"
                        " '00000000-0000-0000-0000-0000000000c1',"
                        " :code, '1'::jsonb, :at, 'profiler')"
                    ),
                    {"id": oid, "code": code, "at": datetime.fromisoformat(at).replace(tzinfo=UTC)},
                )
    finally:
        await upgrade_to_head(database_url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id::text FROM observations WHERE is_latest"))).all()
    assert sorted(r[0][-2:] for r in rows) == ["a2", "a4"]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM companies"))


async def _campaign(db: AsyncSession, name: str = "C1", **kw: object) -> Campaign:
    user = User(email=f"{name}@x.com", name="U", role="member")
    db.add(user)
    await db.flush()
    cohort = Cohort(name=f"co-{name}", side="buyer", definition={}, created_by_id=user.id)
    db.add(cohort)
    await db.flush()
    fields: dict[str, object] = {
        "name": name,
        "side": "buyer",
        "playbook": "buyer",
        "cohort_id": cohort.id,
        "status": "draft",
        "created_by_id": user.id,
    }
    fields.update(kw)
    campaign = Campaign(**fields)
    db.add(campaign)
    await db.flush()
    return campaign


async def _sequence(db: AsyncSession, campaign: Campaign, **kw: object) -> Sequence:
    company = _company("buyer", domain=f"{campaign.name.lower()}-{len(kw)}.com")
    db.add(company)
    await db.flush()
    fields: dict[str, object] = {
        "campaign_id": campaign.id,
        "company_id": company.id,
        "recipient": "jane@acme.com",
        "status": "awaiting_approval",
    }
    fields.update(kw)
    seq = Sequence(**fields)
    db.add(seq)
    await db.flush()
    return seq


def _message(seq: Sequence, **kw: object) -> Message:
    return _message_for(seq.id, **kw)


def _message_for(sequence_id: uuid.UUID, **kw: object) -> Message:
    fields: dict[str, object] = {
        "sequence_id": sequence_id,
        "step": 1,
        "subject": "Hi",
        "body": "Body",
        "status": "pending",
    }
    fields.update(kw)
    return Message(**fields)


async def test_campaign_defaults(db: AsyncSession) -> None:
    campaign = await _campaign(db)
    await db.commit()
    await db.refresh(campaign)
    assert campaign.daily_draft_cap == 40
    assert campaign.activated_at is None and campaign.last_error is None


async def test_campaign_name_unique(db: AsyncSession) -> None:
    campaign = await _campaign(db)
    db.add(
        Campaign(
            name="C1",
            side="buyer",
            playbook="buyer",
            cohort_id=campaign.cohort_id,
            status="draft",
            created_by_id=campaign.created_by_id,
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_campaign_name_unique_regardless_of_case(db: AsyncSession) -> None:
    campaign = await _campaign(db, "Texas Soap")
    await db.commit()
    db.add(
        Campaign(
            name="TEXAS soap",
            side="buyer",
            playbook="buyer",
            cohort_id=campaign.cohort_id,
            status="draft",
            created_by_id=campaign.created_by_id,
        )
    )
    with pytest.raises(IntegrityError, match="uq_campaigns_lower_name"):
        await db.commit()


async def _insert_campaign_raw(conn: AsyncConnection, suffix: str, name: str) -> None:
    user_id = f"00000000-0000-0000-0000-0000000000{suffix}"
    await conn.execute(
        text(
            "INSERT INTO users (id, email, name, role) VALUES (CAST(:id AS uuid), :email, 'U',"
            " 'member')"
        ),
        {"id": user_id, "email": f"u{suffix}@x.com"},
    )
    await conn.execute(
        text(
            "INSERT INTO cohorts (id, name, side, definition, created_by_id) VALUES"
            " (CAST(:id AS uuid), :name, 'buyer', '{}'::jsonb, CAST(:id AS uuid))"
        ),
        {"id": user_id, "name": f"co-{suffix}"},
    )
    await conn.execute(
        text(
            "INSERT INTO campaigns (id, name, side, playbook, cohort_id, status, created_by_id)"
            " VALUES (CAST(:id AS uuid), :name, 'buyer', 'buyer', CAST(:id AS uuid), 'draft',"
            " CAST(:id AS uuid))"
        ),
        {"id": user_id, "name": name},
    )


async def test_campaign_name_index_refuses_existing_case_duplicates(
    database_url: str, engine: AsyncEngine
) -> None:
    try:
        await downgrade_to(database_url, "0011")
        async with engine.begin() as conn:
            await _insert_campaign_raw(conn, "d1", "Texas Soap")
            await _insert_campaign_raw(conn, "d2", "texas soap")
            await _insert_campaign_raw(conn, "d3", "Other")
        with pytest.raises(RuntimeError) as exc:
            await upgrade_to_head(database_url)
        assert str(exc.value) == (
            "Can't make campaign names unique regardless of case: these campaigns differ only "
            "in case: 'Texas Soap', 'texas soap'. Rename them (e.g. on the Campaigns page), "
            "then run the upgrade again."
        )
        current, _ = await current_and_head(database_url)
        assert current == "0011"  # the failed upgrade left nothing half-done
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE campaigns SET name = 'Texas Soap 2' WHERE name = 'texas soap'")
            )
    finally:
        await upgrade_to_head(database_url)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE users, cohorts, campaigns CASCADE"))
    current, head = await current_and_head(database_url)
    assert current == head == "0016"


@pytest.mark.parametrize("value", ["buyer", "supplier", "bogus"])
async def test_campaign_side_and_playbook_checks(db: AsyncSession, value: str) -> None:
    # side/playbook values: both checked, and they must be equal
    await _accepts_campaign(db, side=value, playbook=value, ok=value != "bogus")


async def _accepts_campaign(db: AsyncSession, ok: bool, **kw: object) -> None:
    if ok:
        await _campaign(db, **kw)
        await db.commit()
    else:
        with pytest.raises(IntegrityError):
            await _campaign(db, **kw)


async def test_campaign_playbook_must_equal_side(db: AsyncSession) -> None:
    await _accepts_campaign(db, side="buyer", playbook="supplier", ok=False)


@pytest.mark.parametrize("value", [*CAMPAIGN_STATUSES, "bogus"])
async def test_campaign_status_check_matches_model(db: AsyncSession, value: str) -> None:
    await _accepts_campaign(db, status=value, ok=value != "bogus")


@pytest.mark.parametrize("value", [*SEQUENCE_STATUSES, "bogus"])
async def test_sequence_status_check_matches_model(db: AsyncSession, value: str) -> None:
    campaign = await _campaign(db)
    await _accepts(
        db,
        Sequence(
            campaign_id=campaign.id,
            company_id=(await _saved_company(db)).id,
            recipient="a@b.com",
            status=value,
        ),
        value != "bogus",
    )


@pytest.mark.parametrize("value", [*MESSAGE_STATUSES, "bogus"])
async def test_message_status_check_matches_model(db: AsyncSession, value: str) -> None:
    seq = await _sequence(db, await _campaign(db))
    await _accepts(db, _message(seq, status=value), value != "bogus")


@pytest.mark.parametrize(
    ("kind", "step", "ok"),
    [
        ("first", 1, True),
        ("first", 2, False),
        ("followup", 2, True),
        ("followup", 1, False),
        ("followup", 3, False),
        ("reply", 3, True),
        ("reply", 9, True),
        ("reply", 2, False),
        ("reply", 0, False),
        ("first", 0, False),
    ],
)
async def test_message_kind_step_check(db: AsyncSession, kind: str, step: int, ok: bool) -> None:
    seq = await _sequence(db, await _campaign(db))
    await _accepts(db, _message(seq, kind=kind, step=step), ok)


@pytest.mark.parametrize("kind", [*MESSAGE_KINDS, "bogus"])
async def test_message_kind_check_matches_model(db: AsyncSession, kind: str) -> None:
    seq = await _sequence(db, await _campaign(db))
    step = {"first": 1, "followup": 2, "reply": 3}.get(kind, 1)
    await _accepts(db, _message(seq, kind=kind, step=step), kind != "bogus")


async def test_message_kind_defaults_from_step(db: AsyncSession) -> None:
    seq = await _sequence(db, await _campaign(db))
    first, second, third = (_message(seq, step=n) for n in (1, 2, 3))
    db.add_all([first, second, third])
    await db.flush()
    assert (first.kind, second.kind, third.kind) == ("first", "followup", "reply")


async def test_message_id_header_unique_when_set(db: AsyncSession) -> None:
    seq = await _sequence(db, await _campaign(db))
    db.add_all(
        [
            _message(seq, step=1, message_id_header="<a@x>"),
            _message(seq, step=2),  # NULL headers never collide
            _message(seq, step=3),
        ]
    )
    await db.flush()
    seq2 = await _sequence(db, await _campaign(db, "C2"))
    await _accepts(db, _message(seq2, step=1, message_id_header="<a@x>"), False)


async def test_gate_errors_nullable(db: AsyncSession) -> None:
    seq = await _sequence(db, await _campaign(db))
    msg = _message(seq, step=2, gate_errors=null())
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    assert msg.gate_errors is None and msg.send_attempts == 0 and msg.send_snapshot is None
    row = (
        await db.execute(
            text("SELECT gate_errors IS NULL, send_snapshot IS NULL FROM messages WHERE id = :i"),
            {"i": msg.id},
        )
    ).one()
    assert tuple(row) == (True, True)  # SQL NULL, not JSON null


async def _mailbox(db: AsyncSession, address: str = "sales@elchemy.io", **kw: object) -> Mailbox:
    user = User(email=f"mb-{address}", name="U", role="admin")
    db.add(user)
    await db.flush()
    fields: dict[str, object] = {
        "address": address,
        "side": "buyer",
        "username": address,
        "secret_name": "sales",
        "created_by_id": user.id,
    }
    fields.update(kw)
    mailbox = Mailbox(**fields)
    db.add(mailbox)
    await db.flush()
    return mailbox


async def test_mailbox_defaults(db: AsyncSession) -> None:
    mb = await _mailbox(db)
    await db.refresh(mb)
    assert (mb.smtp_host, mb.smtp_port, mb.smtp_security) == ("smtp.gmail.com", 465, "ssl")
    assert (mb.imap_host, mb.imap_port, mb.dkim_selector) == ("imap.gmail.com", 993, "google")
    assert (mb.daily_max, mb.enabled, mb.dns_ok, mb.dns_report) == (30, False, False, {})
    assert mb.sent_folder is None and mb.warmup_started_on is None


async def test_mailbox_address_unique(db: AsyncSession) -> None:
    await _mailbox(db)
    with pytest.raises(IntegrityError):
        await _mailbox(db)


@pytest.mark.parametrize("value", [*MAILBOX_SECURITY, "bogus"])
async def test_mailbox_security_check_matches_model(db: AsyncSession, value: str) -> None:
    user = User(email="sec@x.com", name="U", role="admin")
    db.add(user)
    await db.flush()
    await _accepts(
        db,
        Mailbox(
            address="a@b.com",
            side="buyer",
            username="a@b.com",
            secret_name="a",  # noqa: S106
            smtp_security=value,
            created_by_id=user.id,
        ),
        value != "bogus",
    )


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("daily_max", 1, True),
        ("daily_max", 50, True),
        ("daily_max", 0, False),
        ("daily_max", 51, False),
        ("secret_name", "sales_2", True),
        ("secret_name", "Sales", False),
        ("secret_name", "has space", False),
        ("secret_name", "", False),
        ("side", "supplier", True),
        ("side", "bogus", False),
    ],
)
async def test_mailbox_checks(db: AsyncSession, field: str, value: object, ok: bool) -> None:
    user = User(email="chk@x.com", name="U", role="admin")
    db.add(user)
    await db.flush()
    fields: dict[str, object] = {
        "address": "a@b.com",
        "side": "buyer",
        "username": "a@b.com",
        "secret_name": "a",
        "created_by_id": user.id,
    }
    fields[field] = value
    await _accepts(db, Mailbox(**fields), ok)


async def test_mailbox_delete_restricted_while_referenced(db: AsyncSession) -> None:
    mb = await _mailbox(db)
    campaign = await _campaign(db, mailbox_id=mb.id)
    seq = await _sequence(db, campaign, mailbox_id=mb.id)
    db.add(_message(seq, mailbox_id=mb.id))
    await db.flush()
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM mailboxes"))
    await db.rollback()


@pytest.mark.parametrize("table", ["campaigns", "sequences", "messages"])
async def test_mailbox_delete_restricted_by_each_referrer(db: AsyncSession, table: str) -> None:
    mb = await _mailbox(db)
    campaign = await _campaign(db, mailbox_id=mb.id if table == "campaigns" else None)
    seq = await _sequence(db, campaign, mailbox_id=mb.id if table == "sequences" else None)
    db.add(_message(seq, mailbox_id=mb.id if table == "messages" else None))
    await db.flush()
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM mailboxes"))
    await db.rollback()


@pytest.mark.parametrize("reason", [*SUPPRESSION_REASONS, "bogus"])
async def test_suppression_reason_check_matches_model(db: AsyncSession, reason: str) -> None:
    await _accepts(db, Suppression(address="x@y.com", reason=reason, detail="d"), reason != "bogus")


async def test_suppression_address_unique_and_message_set_null(db: AsyncSession) -> None:
    seq = await _sequence(db, await _campaign(db))
    msg = _message(seq)
    db.add(msg)
    await db.flush()
    sup = Suppression(address="x@y.com", reason="bounced", detail="5.1.1", message_id=msg.id)
    db.add(sup)
    await db.flush()
    await db.execute(text("DELETE FROM messages"))
    await db.refresh(sup)
    assert sup.message_id is None and sup.created_at is not None
    with pytest.raises(IntegrityError):
        db.add(Suppression(address="x@y.com", reason="manual", detail="d"))
        await db.flush()
    await db.rollback()


async def test_backfill_of_step_two_pending_row(database_url: str, engine: AsyncEngine) -> None:
    """Rows from before 0013 get a kind from their step, and a waiting step 2 loses its
    (meaningless) empty gate result."""
    ids = {name: str(uuid.uuid4()) for name in ("user", "cohort", "campaign", "company", "seq")}
    try:
        await downgrade_to(database_url, "0012")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, name, role) "
                    "VALUES (:user, 'bf@x.com', 'U', 'member')"
                ),
                ids,
            )
            await conn.execute(
                text(
                    "INSERT INTO cohorts (id, name, side, definition, created_by_id) "
                    "VALUES (:cohort, 'bf', 'buyer', '{}', :user)"
                ),
                ids,
            )
            await conn.execute(
                text(
                    "INSERT INTO campaigns (id, name, side, cohort_id, playbook, status, "
                    "created_by_id) VALUES (:campaign, 'bf', 'buyer', :cohort, 'buyer', "
                    "'draft', :user)"
                ),
                ids,
            )
            await conn.execute(
                text(
                    "INSERT INTO companies (id, side, name, domain, website_url, status) "
                    "VALUES (:company, 'buyer', 'BF', 'bf.com', 'https://bf.com', 'new')"
                ),
                ids,
            )
            await conn.execute(
                text(
                    "INSERT INTO sequences (id, campaign_id, company_id, recipient, status) "
                    "VALUES (:seq, :campaign, :company, 'a@bf.com', 'awaiting_approval')"
                ),
                ids,
            )
            for step, status in ((1, "awaiting_approval"), (2, "pending")):
                await conn.execute(
                    text(
                        "INSERT INTO messages (id, sequence_id, step, subject, body, status) "
                        "VALUES (gen_random_uuid(), :seq, :step, 's', 'b', :status)"
                    ),
                    {"seq": ids["seq"], "step": step, "status": status},
                )
    finally:
        await upgrade_to_head(database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT step, kind, gate_errors IS NULL, send_attempts FROM messages "
                        "ORDER BY step"
                    )
                )
            ).all()
        assert [tuple(r) for r in rows] == [(1, "first", False, 0), (2, "followup", True, 0)]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE users, cohorts, campaigns, companies CASCADE"))


async def test_message_defaults_and_unique_step(db: AsyncSession) -> None:
    seq = await _sequence(db, await _campaign(db))
    seq_id = seq.id
    msg = _message(seq)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    assert msg.gate_errors == [] and msg.edited is False
    assert msg.approved_at is None and msg.due_at is None and msg.sent_at is None
    db.add(_message_for(seq_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(_message_for(seq_id, step=2))
    await db.commit()


async def test_sequence_unique_per_campaign_company(db: AsyncSession) -> None:
    campaign = await _campaign(db)
    seq = await _sequence(db, campaign)
    db.add(
        Sequence(
            campaign_id=campaign.id,
            company_id=seq.company_id,
            recipient="x@y.com",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_cascades_and_set_nulls(db: AsyncSession) -> None:
    campaign = await _campaign(db)
    seq = await _sequence(db, campaign)
    msg = _message(seq)
    db.add(msg)
    await db.flush()
    db.add(
        OutboxFeedback(
            campaign_id=campaign.id,
            message_id=msg.id,
            reason="r",
            created_by_id=campaign.created_by_id,
        )
    )
    db.add(UsageEvent(task="writer", ok=True, duration_ms=5, campaign_id=campaign.id))
    await db.commit()
    event = (await db.scalars(select(UsageEvent))).one()
    assert event.at is not None and event.cost_usd is None
    await db.execute(text("DELETE FROM messages"))
    await db.commit()
    fb = (await db.scalars(select(OutboxFeedback))).one()
    assert fb.message_id is None and fb.created_at is not None
    await db.execute(text("DELETE FROM campaigns"))
    await db.commit()
    assert (await db.scalars(select(Sequence))).all() == []
    assert (await db.scalars(select(OutboxFeedback))).all() == []
    await db.refresh(event)
    assert event.campaign_id is None


async def test_sequence_person_and_email_set_null(db: AsyncSession) -> None:
    campaign = await _campaign(db)
    company = await _saved_company(db)
    person = _person(company)
    db.add(person)
    await db.flush()
    email = _email(company, person_id=person.id)
    db.add(email)
    await db.flush()
    seq = Sequence(
        campaign_id=campaign.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=email.id,
        recipient="jane@acme.com",
        status="active",
    )
    db.add(seq)
    await db.commit()
    await db.execute(text("DELETE FROM email_addresses"))
    await db.execute(text("DELETE FROM people"))
    await db.commit()
    await db.refresh(seq)
    assert seq.person_id is None and seq.email_address_id is None


async def test_cohort_delete_restricted_by_campaign(db: AsyncSession) -> None:
    await _campaign(db)
    await db.commit()
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM cohorts"))
        await db.commit()


# --------------------------------------------------------------------------- 0014 replies


async def _thread(db: AsyncSession) -> tuple[Mailbox, Sequence, Message]:
    mb = await _mailbox(db)
    seq = await _sequence(db, await _campaign(db), mailbox_id=mb.id)
    msg = _message(seq, step=1)
    db.add(msg)
    await db.flush()
    return mb, seq, msg


def _inbound(mb: Mailbox, **kw: object) -> MailboxMessage:
    fields: dict[str, object] = {
        "mailbox_id": mb.id,
        "folder": "inbox",
        "uidvalidity": 1,
        "uid": 1,
        "subject": "Re: Hi",
        "received_at": datetime(2026, 11, 13, tzinfo=UTC),
        "kind": "reply",
    }
    fields.update(kw)
    return MailboxMessage(**fields)


@pytest.mark.parametrize("value", [*MAILBOX_FOLDERS, "bogus"])
async def test_mailbox_folder_check_matches_model(db: AsyncSession, value: str) -> None:
    mb = await _mailbox(db)
    await _accepts(
        db, MailboxFolder(mailbox_id=mb.id, folder=value, name="INBOX"), value != "bogus"
    )


async def test_mailbox_folder_defaults_and_cascade(db: AsyncSession) -> None:
    mb = await _mailbox(db)
    row = MailboxFolder(mailbox_id=mb.id, folder="inbox", name="INBOX", uidvalidity=2**40)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    assert (row.uidvalidity, row.last_uid, row.polled_at, row.last_error) == (2**40, 0, None, None)
    await db.execute(text("DELETE FROM mailboxes"))
    assert (await db.scalar(text("SELECT count(*) FROM mailbox_folders"))) == 0
    await db.rollback()


@pytest.mark.parametrize("value", [*CONVERSATION_STATUSES, "bogus"])
async def test_conversation_status_check_matches_model(db: AsyncSession, value: str) -> None:
    mb, seq, _ = await _thread(db)
    await _accepts(
        db, Conversation(sequence_id=seq.id, mailbox_id=mb.id, status=value), value != "bogus"
    )


@pytest.mark.parametrize("value", [*REPLY_INTENTS, "bogus"])
async def test_intent_checks_match_model(db: AsyncSession, value: str) -> None:
    mb, seq, _ = await _thread(db)
    db.add(_inbound(mb, intent=value if value != "bogus" else None))
    await db.flush()
    await _accepts(
        db,
        Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open", intent=value),
        value != "bogus",
    )
    if value == "bogus":
        await db.rollback()
        mb, _, _ = await _thread(db)
        await _accepts(db, _inbound(mb, intent="bogus"), False)


async def test_conversation_one_per_sequence_cascades_and_defaults(db: AsyncSession) -> None:
    mb, seq, _ = await _thread(db)
    conv = Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open")
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    assert (conv.products, conv.intent, conv.assigned_to_id) == ([], None, None)
    with pytest.raises(IntegrityError):
        db.add(Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open"))
        await db.flush()
    await db.rollback()

    mb, seq, _ = await _thread(db)
    db.add(Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open"))
    await db.flush()
    with pytest.raises(IntegrityError):  # RESTRICT: a mailbox with conversations stays
        await db.execute(text("DELETE FROM mailboxes WHERE id = :i"), {"i": mb.id})
    await db.rollback()

    mb, seq, _ = await _thread(db)
    db.add(Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open"))
    await db.flush()
    await db.execute(text("DELETE FROM sequences"))
    assert (await db.scalar(text("SELECT count(*) FROM conversations"))) == 0
    await db.rollback()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "bogus"),
        ("folder", "bogus"),
        ("matched_by", "bogus"),
        *[("kind", k) for k in INBOUND_KINDS],
        *[("matched_by", m) for m in MATCHED_BY],
    ],
)
async def test_mailbox_message_checks(db: AsyncSession, field: str, value: str) -> None:
    mb = await _mailbox(db)
    await _accepts(db, _inbound(mb, **{field: value}), value != "bogus")


async def test_mailbox_message_unique_uid_and_set_nulls(db: AsyncSession) -> None:
    mb, seq, msg = await _thread(db)
    conv = Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open")
    db.add(conv)
    await db.flush()
    row = _inbound(
        mb, sequence_id=seq.id, conversation_id=conv.id, message_id=msg.id, to_addresses=["a@b.c"]
    )
    db.add_all([row, _inbound(mb, uid=2), _inbound(mb, folder="sent"), _inbound(mb, uidvalidity=2)])
    await db.flush()
    await db.refresh(row)
    assert (row.classify_failures, row.confidence, row.to_addresses) == (0, None, ["a@b.c"])
    # The inbound mail a reply answers, and the teammate who wrote it: both SET NULL.
    writer = User(email="writer@x.com", name="W", role="member")
    db.add(writer)
    await db.flush()
    reply = _message(seq, step=3, kind="reply", reply_to_id=row.id, written_by_id=writer.id)
    db.add(reply)
    await db.flush()
    await db.execute(text("DELETE FROM mailbox_messages WHERE id = :i"), {"i": row.id})
    await db.execute(text("DELETE FROM users WHERE id = :i"), {"i": writer.id})
    await db.refresh(reply)
    assert (reply.reply_to_id, reply.written_by_id) == (None, None)
    with pytest.raises(IntegrityError):
        db.add(_inbound(mb, uid=2))
        await db.flush()
    await db.rollback()

    mb, seq, msg = await _thread(db)
    conv = Conversation(sequence_id=seq.id, mailbox_id=mb.id, status="open")
    db.add(conv)
    await db.flush()
    row = _inbound(mb, sequence_id=seq.id, conversation_id=conv.id, message_id=msg.id)
    db.add(row)
    await db.flush()
    await db.execute(text("DELETE FROM sequences"))  # cascades the message and conversation
    await db.refresh(row)
    assert (row.sequence_id, row.conversation_id, row.message_id) == (None, None, None)
    await db.rollback()


# --------------------------------------------------------------------------- final fix wave


async def _indexes(engine: AsyncEngine, table: str) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def test_0015_indexes_audit_log_by_action_and_target(
    database_url: str, engine: AsyncEngine
) -> None:
    """The sender's "may have gone" lookup (``message.unconfirmed`` for one message) and the
    MX-wait markers read ``audit_log`` by (action, target_id)."""
    assert (
        "(action, target_id)"
        in (await _indexes(engine, "audit_log"))["ix_audit_log_action_target_id"]
    )
    try:
        await downgrade_to(database_url, "0014")
        assert "ix_audit_log_action_target_id" not in await _indexes(engine, "audit_log")
    finally:
        await upgrade_to_head(database_url)
    assert "ix_audit_log_action_target_id" in await _indexes(engine, "audit_log")


async def test_round_trip_through_0013_with_rows_present(
    database_url: str, engine: AsyncEngine
) -> None:
    """Final-wave M6: 0015 -> 0012 -> head with Phase 6 rows in every new table and every new
    status, so the data half of 0013's (and 0014's) downgrade actually runs."""
    from salesfinder.db.engine import make_session_factory
    from sending_helpers import seed_ready

    factory = make_session_factory(engine)
    async with factory() as s:
        ready = await seed_ready(s)
        await s.commit()
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE messages SET status = 'unconfirmed', gate_errors = NULL,"
                " message_id_header = '<a@send-domain.com>', mailbox_id = :b WHERE id = :m"
            ),
            {"m": ready.message_id, "b": ready.mailbox_id},
        )
        await s.execute(
            text("UPDATE messages SET status = 'failed' WHERE id = :m"), {"m": ready.step2_id}
        )
        await s.execute(
            text("UPDATE sequences SET status = 'replied' WHERE id = :q"), {"q": ready.sequence_id}
        )
        conv = Conversation(
            sequence_id=ready.sequence_id, mailbox_id=ready.mailbox_id, status="open"
        )
        s.add(conv)
        await s.flush()
        inbound = MailboxMessage(
            mailbox_id=ready.mailbox_id,
            folder="inbox",
            uidvalidity=1,
            uid=1,
            from_address="jane.doe@acme-soap.com",
            to_addresses=[],
            subject="Re: hi",
            received_at=datetime(2026, 11, 13, tzinfo=UTC),
            kind="reply",
            sequence_id=ready.sequence_id,
            conversation_id=conv.id,
        )
        s.add(inbound)
        await s.flush()
        s.add(
            Message(
                sequence_id=ready.sequence_id,
                step=3,
                kind="reply",
                subject="Re: hi",
                body="Thanks.",
                status="sending",
                reply_to_id=inbound.id,
                written_by_id=ready.user_id,
            )
        )
        s.add(Suppression(address="gone@acme-soap.com", reason="unsubscribed", detail="Asked"))
        s.add(MailboxFolder(mailbox_id=ready.mailbox_id, folder="inbox", name="INBOX"))
        await s.commit()
    try:
        await downgrade_to(database_url, "0012")
        async with engine.connect() as conn:
            statuses = sorted((await conn.execute(text("SELECT step, status FROM messages"))).all())
            seq = (await conn.execute(text("SELECT status FROM sequences"))).scalar_one()
            gate = (
                await conn.execute(
                    text("SELECT gate_errors FROM messages WHERE id = :m"),
                    {"m": ready.message_id},
                )
            ).scalar_one()
            tables = {
                r[0]
                for r in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        assert statuses == [(1, "cancelled"), (2, "rejected")]  # the reply row is deleted
        assert seq == "stopped" and gate == []
        assert not {"mailboxes", "suppressions", "conversations", "mailbox_messages"} & tables
    finally:
        await upgrade_to_head(database_url)
        async with engine.begin() as conn:
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {names} CASCADE"))
    current, head = await current_and_head(database_url)
    assert current == head == "0016"


# --------------------------------------------------------------------------- 0016 runs


def _run(**kw: object) -> Run:
    values: dict[str, object] = {
        "started_at": datetime(2026, 11, 14, 14, 0, tzinfo=UTC),
        "last_beat_at": datetime(2026, 11, 14, 14, 0, tzinfo=UTC),
        "host": "office-mac",
        "pid": 1,
        "code_version": "abc123def456",
    }
    values.update(kw)
    return Run(**values)


async def test_run_defaults(db: AsyncSession) -> None:
    db.add(_run())
    await db.commit()
    row = (
        await db.execute(
            text(
                "SELECT passes, failed_steps, lines, lines_dropped, failures, quiet, end_reason,"
                " finished_at, step FROM runs"
            )
        )
    ).one()
    assert tuple(row) == (0, 0, [], 0, [], False, None, None, None)


@pytest.mark.parametrize("reason", [*RUN_END_REASONS, "bogus"])
async def test_run_end_reason_check_matches_model(db: AsyncSession, reason: str) -> None:
    finished = datetime(2026, 11, 14, 14, 15, tzinfo=UTC)
    await _accepts(db, _run(finished_at=finished, end_reason=reason), reason != "bogus")


@pytest.mark.parametrize(
    ("finished", "reason", "ok"),
    [
        (True, None, False),  # finished without a reason
        (False, "window", False),  # a reason on an open run
        (False, None, True),
        (True, "crashed", True),
    ],
)
async def test_run_finished_iff_end_reason(
    db: AsyncSession, finished: bool, reason: str | None, ok: bool
) -> None:
    at = datetime(2026, 11, 14, 14, 15, tzinfo=UTC) if finished else None
    await _accepts(db, _run(finished_at=at, end_reason=reason), ok)


async def test_0016_runs_indexes(engine: AsyncEngine) -> None:
    indexes = await _indexes(engine, "runs")
    assert "(started_at)" in indexes["ix_runs_started_at"]
    assert "WHERE (finished_at IS NULL)" in indexes["ix_runs_open"]
    one_open = indexes["uq_runs_one_open"]
    assert "UNIQUE" in one_open and "WHERE (finished_at IS NULL)" in one_open


async def test_run_at_most_one_open(db: AsyncSession) -> None:
    finished = datetime(2026, 11, 14, 14, 15, tzinfo=UTC)
    db.add_all(
        [
            _run(finished_at=finished, end_reason="window"),
            _run(finished_at=finished, end_reason="crashed"),
            _run(),
        ]
    )
    await db.commit()  # many finished runs, one open: fine
    await _accepts(db, _run(), ok=False)


async def test_0016_round_trip(database_url: str, engine: AsyncEngine) -> None:
    """0016 -> 0015 -> head with a row present: the downgrade drops the table (and its
    indexes); the upgrade brings it back empty."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, started_at, last_beat_at, host, pid, code_version)"
                " VALUES (gen_random_uuid(), now(), now(), 'h', 1, 'v')"
            )
        )
    try:
        await downgrade_to(database_url, "0015")
        current, _ = await current_and_head(database_url)
        assert current == "0015"
        async with engine.connect() as conn:
            tables = {
                r[0]
                for r in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        assert "runs" not in tables
        assert await _indexes(engine, "runs") == {}
    finally:
        await upgrade_to_head(database_url)
    current, head = await current_and_head(database_url)
    assert current == head == "0016"
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM runs"))).scalar_one() == 0
    assert set(await _indexes(engine, "runs")) == {
        "pk_runs",
        "ix_runs_started_at",
        "ix_runs_open",
        "uq_runs_one_open",
    }
