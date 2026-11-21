"""Campaigns: lifecycle and activation checks, choosing one recipient per company, and drafting
(fake Claude only, real database). Nothing is sent."""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import (
    CATALOG,
    CLEAN,
    CONFIG,
    FACTS,
    MARKER,
    NOW,
    PRICED,
    add_address,
    add_person,
    ok_reply,
    seed_campaign,
    seed_cohort,
    seed_company,
    seed_draft,
    seed_mailbox,
    seed_user,
)
from conftest import FakeClaude
from fakes.dns import FakeResolver
from salesfinder.brain.runtime import SIGNED_OUT, UNEXPECTED, ClaudeRuntime
from salesfinder.campaigns import service
from salesfinder.campaigns.service import (
    KEPT_FAILING,
    DraftOutcome,
    draft_batch,
    select_recipient,
)
from salesfinder.catalog import Facts, Persona
from salesfinder.config import AppConfig, BrainConfig, UsageConfig
from salesfinder.contacts.dns import MxChecker
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    Campaign,
    Company,
    DomainMx,
    DoNotContact,
    DraftFailure,
    Message,
    Sequence,
    UsageEvent,
)
from salesfinder.gate.checks import PRICE, recipient_problems
from salesfinder.settings import Settings
from salesfinder.signals.settings import set_state
from salesfinder.writer.compose import WRITE_TASK, footer, signature

INDUSTRY_COND = {"code": "INDUSTRY_MATCH", "op": "present"}


@pytest.fixture
def runtime(test_settings_with_fake_claude: Settings) -> ClaudeRuntime:
    return ClaudeRuntime(test_settings_with_fake_claude, BrainConfig())


def _facts(**changes: Any) -> Facts:
    return FACTS.model_copy(update=changes)


async def _draft(
    engine: AsyncEngine,
    runtime: ClaudeRuntime,
    *,
    limit: int = 10,
    config: AppConfig = CONFIG,
    facts: Facts = FACTS,
    now: Callable[[], datetime] = lambda: NOW,
    campaign_id: uuid.UUID | None = None,
    mx_checker: MxChecker | None = None,
) -> DraftOutcome:
    return await draft_batch(
        make_session_factory(engine),
        runtime=runtime,
        catalog=CATALOG,
        facts=facts,
        config=config,
        now=now,
        limit=limit,
        campaign_id=campaign_id,
        mx_checker=mx_checker,
    )


async def _rows[T](engine: AsyncEngine, model: type[T]) -> list[T]:
    async with make_session_factory(engine)() as s:
        return list((await s.scalars(select(model))).all())


async def _campaign(engine: AsyncEngine, campaign_id: uuid.UUID) -> Campaign:
    async with make_session_factory(engine)() as s:
        row = await s.get(Campaign, campaign_id)
        assert row is not None
        return row


# --------------------------------------------------------------------------- lifecycle


async def test_create_campaign(db: AsyncSession) -> None:
    user = await seed_user(db)
    cohort = await seed_cohort(db, user, side="supplier")
    campaign = await service.create_campaign(
        db,
        name="  Indian SLES makers ",
        side="supplier",
        cohort_id=cohort.id,
        daily_draft_cap=25,
        actor=user,
    )
    assert (campaign.name, campaign.side, campaign.playbook, campaign.status) == (
        "Indian SLES makers",
        "supplier",
        "supplier",
        "draft",
    )
    assert (campaign.daily_draft_cap, campaign.created_by_id, campaign.activated_at) == (
        25,
        user.id,
        None,
    )
    with pytest.raises(ValueError, match=r"^A campaign with that name already exists\.$"):
        await service.create_campaign(
            db,
            name="indian sles MAKERS",
            side="supplier",
            cohort_id=cohort.id,
            daily_draft_cap=25,
            actor=user,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"side": "buyer"}, "That cohort is for the other side."),
        ({"cohort_id": uuid.uuid4()}, "That cohort no longer exists."),
        ({"daily_draft_cap": 0}, "The daily draft cap must be 1 to 200."),
        ({"daily_draft_cap": 201}, "The daily draft cap must be 1 to 200."),
        ({"name": "   "}, "A campaign name must be 1 to 120 characters."),
        ({"name": "a\x00b"}, "A campaign name can't contain control characters."),
        ({"side": "other"}, "Side must be buyer or supplier."),
    ],
)
async def test_create_campaign_refusals(
    db: AsyncSession, changes: dict[str, Any], message: str
) -> None:
    user = await seed_user(db)
    cohort = await seed_cohort(db, user, side="supplier")
    fields: dict[str, Any] = {
        "name": "Camp",
        "side": "supplier",
        "cohort_id": cohort.id,
        "daily_draft_cap": 10,
    }
    fields.update(changes)
    with pytest.raises(ValueError) as exc:
        await service.create_campaign(db, actor=user, **fields)
    assert str(exc.value) == message


async def test_activate_pause_resume_archive(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, activate=False)
    with pytest.raises(ValueError, match=r"^Only an active campaign can be paused\.$"):
        await service.pause(db, campaign.id, actor=user)
    with pytest.raises(ValueError, match=r"^Only a paused campaign can be resumed\.$"):
        await service.resume(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)

    active = await service.activate(
        db, campaign.id, actor=user, facts=FACTS, config=CONFIG, now=NOW
    )
    assert (active.status, active.activated_at) == ("active", NOW)
    with pytest.raises(ValueError, match=r"^Only a draft or paused campaign can be activated\.$"):
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)

    assert (await service.pause(db, campaign.id, actor=user)).status == "paused"
    resumed = await service.resume(
        db, campaign.id, actor=user, facts=FACTS, config=CONFIG, now=NOW + timedelta(days=3)
    )
    assert (resumed.status, resumed.activated_at) == ("active", NOW)  # first activation kept

    assert (await service.archive(db, campaign.id, actor=user)).campaign.status == "archived"
    with pytest.raises(ValueError, match=r"^That campaign is already archived\.$"):
        await service.archive(db, campaign.id, actor=user)
    with pytest.raises(ValueError, match=r"^Only a draft or paused campaign can be activated\.$"):
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)
    with pytest.raises(ValueError, match=r"^That campaign no longer exists\.$"):
        await service.pause(db, uuid.uuid4(), actor=user)


async def test_archive_cancels_waiting_drafts_and_stops_their_sequences(
    db: AsyncSession,
) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    ready = await seed_draft(db, campaign, domain="a-soap.com")
    held = await seed_draft(db, campaign, domain="b-soap.com", status="gate_failed")
    approved = await seed_draft(db, campaign, domain="c-soap.com", status="approved")
    rejected = await seed_draft(db, campaign, domain="d-soap.com", status="rejected")
    for message in (approved, rejected):
        seq = await db.get(Sequence, message.sequence_id)
        assert seq is not None
        seq.status = "active" if message is approved else "stopped"
        seq.stop_reason = None if message is approved else "Rejected in the Outbox"
    # As rejecting does: the rejected email's follow-up is already cancelled.
    await db.execute(
        update(Message)
        .where(Message.sequence_id == rejected.sequence_id, Message.step == 2)
        .values(status="cancelled")
    )
    other = await seed_campaign(db, user=user)
    elsewhere = await seed_draft(db, other, domain="e-soap.com")
    await db.commit()

    outcome = await service.archive(db, campaign.id, actor=user)
    await db.commit()

    assert outcome.campaign.status == "archived"
    # Three unfinished sequences (ready, held, approved); the rejected one was already stopped.
    # Their unsent emails: three first emails and three follow-ups.
    assert (outcome.sequences_stopped, outcome.messages_cancelled) == (3, 6)
    stopped = {m.sequence_id for m in (ready, held, approved)}
    rejected_ids = (rejected.id, rejected.sequence_id)
    elsewhere_id = elsewhere.id
    db.expire_all()
    for seq in (await db.scalars(select(Sequence).where(Sequence.id.in_(stopped)))).all():
        assert (seq.status, seq.stop_reason) == ("stopped", "Campaign archived")
    statuses = (
        await db.scalars(select(Message.status).where(Message.sequence_id.in_(stopped)))
    ).all()
    assert sorted(statuses) == ["cancelled"] * 6
    rejected_seq = await db.get(Sequence, rejected_ids[1])
    assert rejected_seq is not None
    assert (rejected_seq.status, rejected_seq.stop_reason) == ("stopped", "Rejected in the Outbox")
    assert await db.scalar(select(Message.status).where(Message.id == rejected_ids[0])) == (
        "rejected"
    )
    # Another campaign's draft is untouched.
    assert await db.scalar(select(Message.status).where(Message.id == elsewhere_id)) == (
        "awaiting_approval"
    )


async def test_archive_frees_the_address_for_other_campaigns(db: AsyncSession) -> None:
    user = await seed_user(db)
    first = await seed_campaign(db, user=user)
    draft = await seed_draft(db, first, domain="acme-soap.com")
    seq = await db.get(Sequence, draft.sequence_id)
    assert seq is not None
    company = await db.get(Company, seq.company_id)
    assert company is not None
    now = datetime.now(UTC)  # seed_draft checked the contacts and MX just now
    assert await select_recipient(db, company, now=now, config=CONFIG) is None  # busy

    await service.archive(db, first.id, actor=user)
    await db.commit()

    picked = await select_recipient(db, company, now=now, config=CONFIG)
    assert picked is not None and picked[1].address == "jane.doe@acme-soap.com"
    # The gate's "already being contacted" check doesn't see it either.
    problems = await recipient_problems(
        db,
        sequence_or_none=None,
        recipient="jane.doe@acme-soap.com",
        company=company,
        address=picked[1],
        now=now,
        config=CONFIG,
    )
    assert problems == []


async def test_select_recipient_skips_a_disqualified_company(db: AsyncSession) -> None:
    company, _, _ = await seed_company(db)
    assert await _pick(db, company) == "jane.doe@acme-soap.com"
    company.disqualified = True
    await db.flush()
    assert await _pick(db, company) is None


async def test_activation_refuses_a_stale_cohort(db: AsyncSession) -> None:
    user = await seed_user(db)
    cohort = await seed_cohort(
        db,
        user,
        definition={
            "side": "buyer",
            "must": [INDUSTRY_COND, {"code": "INDUSTRY_MATCH", "op": "absent"}],
            "must_not": [{"code": "COMPANY_TYPE", "op": "present"}],
        },
    )
    campaign = await service.create_campaign(
        db, name="Stale", side="buyer", cohort_id=cohort.id, daily_draft_cap=5, actor=user
    )
    box = await seed_mailbox(db, user)
    await service.set_mailbox(db, campaign.id, box.id, actor=user)
    await set_state(db, "INDUSTRY_MATCH", "rejected", actor=None)
    await set_state(db, "COMPANY_TYPE", "skipped", actor=None)
    with pytest.raises(ValueError) as exc:
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)
    assert str(exc.value) == (
        "This cohort uses signals that aren't being collected: "
        "Works in our industries, What kind of company they are."
    )
    assert campaign.status == "draft"

    await set_state(db, "INDUSTRY_MATCH", "confirmed", actor=None)
    await set_state(db, "COMPANY_TYPE", "confirmed", actor=None)
    assert (
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)
    ).status == "active"


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        (
            _facts(personas={"buyer": Persona(name="Asha Rao", title="Sales Lead")}),
            "Persona for buyer isn't confirmed in facts.yaml.",
        ),
        (
            _facts(personas={"buyer": Persona(name=" ", title="Lead", confirmed=True)}),
            "Persona for buyer isn't confirmed in facts.yaml.",
        ),
        (_facts(personas={}), "Persona for buyer isn't confirmed in facts.yaml."),
        (
            _facts(postal_address_confirmed=False),
            "Confirm Elchemy's postal address in facts.yaml first.",
        ),
        (
            _facts(postal_address=" "),
            "Confirm Elchemy's postal address in facts.yaml first.",
        ),
    ],
)
async def test_activation_refuses_unconfirmed_facts(
    db: AsyncSession, facts: Facts, message: str
) -> None:
    campaign = await seed_campaign(db, activate=False)
    with pytest.raises(ValueError) as exc:
        await service.activate(db, campaign.id, actor=None, facts=facts, config=CONFIG)
    assert str(exc.value) == message
    problems = await service.activation_problems(db, campaign, facts=facts)
    assert problems == [message]


# --------------------------------------------------------------------------- select_recipient


async def _pick(db: AsyncSession, company: Company, *, now: datetime = NOW) -> str | None:
    picked = await select_recipient(db, company, now=now, config=CONFIG)
    if picked is None:
        return None
    person, address = picked
    assert address.person_id == person.id and person.is_target
    return address.address


async def test_select_recipient_order_by_status(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(db, address=None)
    guess = await add_address(
        db, company, jane, "j.guess@acme-soap.com", status="pattern_guess", override=True
    )
    pattern = await add_address(
        db, company, jane, "j.pattern@acme-soap.com", status="pattern_confirmed"
    )
    verified = await add_address(db, company, jane, "j.verified@acme-soap.com", status="verified")
    pub = await add_address(db, company, jane, "j.published@acme-soap.com")
    assert await _pick(db, company) == "j.published@acme-soap.com"
    pub.status = "invalid"
    assert await _pick(db, company) == "j.verified@acme-soap.com"
    verified.status = "risky"  # held without an override
    assert await _pick(db, company) == "j.pattern@acme-soap.com"
    pattern.status = "invalid"
    assert await _pick(db, company) == "j.guess@acme-soap.com"  # overridden guess last
    guess.override_sendable = None
    assert await _pick(db, company) is None  # held guess / risky without an override


async def test_select_recipient_by_rank_and_targets_only(db: AsyncSession) -> None:
    company, _jane, jane_addr = await seed_company(db)
    assert jane_addr is not None
    raj = await add_person(db, company, "Raj Patel", rank=2)
    await add_address(db, company, raj, "raj@acme-soap.com")
    boss = await add_person(db, company, "Not Target", rank=None, target=False)
    await add_address(db, company, boss, "boss@acme-soap.com")
    await add_address(db, company, None, "sales@acme-soap.com")  # unlinked
    assert await _pick(db, company) == "jane.doe@acme-soap.com"
    jane_addr.status = "invalid"
    assert await _pick(db, company) == "raj@acme-soap.com"  # rank 2 when rank 1 can't
    raj.is_target = False
    assert await _pick(db, company) is None  # never a non-target or an unlinked address


async def test_select_recipient_skips(db: AsyncSession) -> None:
    company, jane, jane_addr = await seed_company(db)
    assert jane_addr is not None
    stale = await add_address(db, company, jane, "a.stale@acme-soap.com", stale=True)
    never = await add_address(db, company, jane, "noreply@acme-soap.com")
    blocked = await add_address(
        db, company, jane, "b.blocked@acme-soap.com", blocked_reason="Do-not-contact: x"
    )
    raj = await add_person(db, company, "Raj Patel", rank=2)
    await add_address(db, company, raj, "raj@acme-soap.com")
    assert await _pick(db, company) == "jane.doe@acme-soap.com"

    # Do-not-contact added live (the cached blocked_reason isn't refreshed yet).
    db.add(DoNotContact(kind="email", value="jane.doe@acme-soap.com", reason="Asked us"))
    await db.flush()
    assert jane_addr.blocked_reason is None
    assert await _pick(db, company) == "raj@acme-soap.com"

    # Already in an in-progress sequence of another campaign (either side): skipped.
    other = await seed_campaign(db, side="supplier")
    seq = Sequence(
        campaign_id=other.id,
        company_id=company.id,
        recipient="raj@acme-soap.com",
        status="awaiting_approval",
    )
    db.add(seq)
    await db.flush()
    assert await _pick(db, company) is None
    seq.status = "stopped"  # a stopped sequence doesn't hold the address
    assert await _pick(db, company) == "raj@acme-soap.com"
    seq.status = "active"
    stale.override_sendable = True  # "allow sending" lifts "no longer on their site"
    assert await _pick(db, company) == "a.stale@acme-soap.com"
    never.override_sendable = True  # never a no-reply mailbox, override or not
    stale.override_sendable = None
    blocked.override_sendable = True  # nor a blocked one
    assert await _pick(db, company) is None


async def test_select_recipient_needs_fresh_mail_server_and_live_company(
    db: AsyncSession,
) -> None:
    company, _, _ = await seed_company(db)
    assert await _pick(db, company) == "jane.doe@acme-soap.com"
    # MX older than contacts.mx_cache_days: the gate would hold it, so don't spend a draft.
    assert await _pick(db, company, now=NOW + timedelta(days=CONFIG.contacts.mx_cache_days)) is None
    mx = await db.get(DomainMx, "acme-soap.com")
    assert mx is not None
    mx.state = "no_mail"
    assert await _pick(db, company) is None
    mx.state = "has_mx"
    db.add(DoNotContact(kind="domain", value="acme-soap.com", reason="No"))
    await db.flush()
    assert await _pick(db, company) is None


async def test_select_recipient_free_mail_needs_no_mx(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(db, address=None)
    await add_address(db, company, jane, "jane.acme@gmail.com")
    mx = await db.get(DomainMx, "acme-soap.com")
    assert mx is not None
    mx.state = "no_mail"
    assert await _pick(db, company) == "jane.acme@gmail.com"
    company.status = "excluded"
    await db.flush()
    assert await _pick(db, company) is None


# --------------------------------------------------------------------------- drafting


async def test_draft_happy_path(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    company, jane, address = await seed_company(db)
    assert address is not None
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    outcome = await _draft(engine, runtime)

    assert outcome == DraftOutcome(drafted=1, gate_failed=0, skipped=0, errors=[])
    [call] = fake_claude_env.calls()
    assert "Jane Doe" in call["prompt"] and "Acme Soap Works" in call["prompt"]
    [seq] = await _rows(engine, Sequence)
    assert (seq.campaign_id, seq.company_id, seq.person_id, seq.email_address_id) == (
        campaign.id,
        company.id,
        jane.id,
        address.id,
    )
    assert (seq.recipient, seq.status, seq.created_at) == (
        "jane.doe@acme-soap.com",
        "awaiting_approval",
        NOW,
    )
    first, second = sorted(await _rows(engine, Message), key=lambda m: m.step)
    tail = signature(FACTS, "buyer") + footer(FACTS, CONFIG.writing)
    assert (first.step, first.status, first.gate_errors, first.subject) == (
        1,
        "awaiting_approval",
        [],
        CLEAN["first_subject"],
    )
    assert first.body == CLEAN["first_body"] + tail
    assert (second.step, second.status, second.gate_errors, second.due_at) == (
        2,
        "pending",
        None,
        None,
    )
    assert second.body == CLEAN["followup_body"] + tail
    async with engine.connect() as conn:
        raw = (
            await conn.execute(
                text("SELECT gate_errors IS NULL FROM messages WHERE id = :i"), {"i": second.id}
            )
        ).scalar_one()
    assert raw is True  # SQL NULL, not JSON null
    assert second.sequence_id == first.sequence_id == seq.id
    [usage] = await _rows(engine, UsageEvent)
    assert (usage.task, usage.ok, usage.campaign_id, usage.at) == (
        WRITE_TASK,
        True,
        campaign.id,
        NOW,
    )


STALE = NOW - timedelta(days=CONFIG.contacts.mx_cache_days)


async def test_draft_refreshes_a_stale_mail_server_check(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    await seed_company(db, checked_at=STALE)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    # Without a checker the stale address is passed over, as the gate would hold it.
    assert await _draft(engine, runtime) == DraftOutcome(skipped=1)
    assert fake_claude_env.calls() == []

    resolver = FakeResolver({("acme-soap.com", "MX"): [(10, "mx2.acme-soap.com.")]})
    checker = MxChecker(CONFIG.contacts, resolve=resolver)
    outcome = await _draft(engine, runtime, mx_checker=checker)

    assert outcome == DraftOutcome(drafted=1, gate_failed=0, skipped=0, errors=[])
    assert resolver.calls == [("acme-soap.com", "MX")]
    [mx] = await _rows(engine, DomainMx)
    assert (mx.state, mx.mx_hosts, mx.checked_at) == ("has_mx", ["mx2.acme-soap.com"], NOW)
    first = next(m for m in await _rows(engine, Message) if m.step == 1)
    assert (first.status, first.gate_errors) == ("awaiting_approval", [])


async def test_draft_mx_refresh_failure_skips_and_is_bounded(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    import dns.exception

    await seed_campaign(db)
    domains = ["a-soap.com", "b-soap.com", "c-soap.com"]
    for i, domain in enumerate(domains):
        await seed_company(db, name=domain, domain=domain, score=50 - i, checked_at=STALE)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    resolver = FakeResolver({(d, "MX"): dns.exception.Timeout() for d in domains})
    checker = MxChecker(CONFIG.contacts, resolve=resolver)

    outcome = await _draft(engine, runtime, limit=1, mx_checker=checker)

    # DNS couldn't answer: the known answer is kept (still stale), nothing is drafted, and at
    # most 2 x limit lookups are made in the pass however many companies are passed over.
    assert outcome == DraftOutcome(skipped=3)
    assert fake_claude_env.calls() == []
    assert resolver.calls == [("a-soap.com", "MX"), ("b-soap.com", "MX")]
    rows = {row.domain: row for row in await _rows(engine, DomainMx)}
    assert {d: (rows[d].state, rows[d].checked_at) for d in domains} == {
        d: ("has_mx", STALE) for d in domains
    }
    assert rows["a-soap.com"].error == "DNS timed out"


async def test_draft_gate_failed_is_stored_with_reasons(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(PRICED))

    outcome = await _draft(engine, runtime)

    assert (outcome.drafted, outcome.gate_failed, outcome.errors) == (1, 1, [])
    [seq] = await _rows(engine, Sequence)
    assert seq.status == "awaiting_approval"
    first, second = sorted(await _rows(engine, Message), key=lambda m: m.step)
    assert first.status == "gate_failed"  # held: not ready in the Outbox
    assert PRICE in first.gate_errors
    assert second.status == "pending" and second.gate_errors is None


async def test_draft_recipient_problem_found_at_draft_time_is_held(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recipient goes on do-not-contact while Claude writes: the step-1 gate (live) holds
    the draft with the reason."""
    await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    real = service.write_sequence

    async def write_then_block(*args: Any, **kwargs: Any) -> Any:
        written = await real(*args, **kwargs)
        async with make_session_factory(engine)() as s:
            s.add(DoNotContact(kind="email", value="jane.doe@acme-soap.com", reason="Asked"))
            await s.commit()
        return written

    monkeypatch.setattr(service, "write_sequence", write_then_block)
    outcome = await _draft(engine, runtime)
    assert (outcome.drafted, outcome.gate_failed) == (1, 1)
    first = next(m for m in await _rows(engine, Message) if m.step == 1)
    assert first.status == "gate_failed"
    assert first.gate_errors == [
        "Recipient is excluded: Do-not-contact: jane.doe@acme-soap.com — Asked."
    ]


async def test_draft_no_transaction_open_while_claude_writes(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    real = service.write_sequence
    seen: list[int] = []

    async def checked(*args: Any, **kwargs: Any) -> Any:
        async with engine.connect() as conn:
            seen.append(
                int(
                    await conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND pid <> pg_backend_pid() "
                            "AND state LIKE 'idle in transaction%'"
                        )
                    )
                    or 0
                )
            )
        return await real(*args, **kwargs)

    monkeypatch.setattr(service, "write_sequence", checked)
    outcome = await _draft(engine, runtime)
    assert outcome.drafted == 1
    assert seen == [0]


async def test_draft_stops_when_budget_runs_out(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db, now=NOW - timedelta(days=1))
    other = await seed_campaign(db, side="supplier")  # nothing to write, still told why
    await seed_company(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await seed_company(db, name="Gamma Co", domain="gamma-co.com", score=30)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    config = CONFIG.model_copy(update={"usage": UsageConfig(max_daily_claude_calls=1)})

    outcome = await _draft(engine, runtime, config=config)

    reason = "Daily Claude budget reached (1 calls)."
    assert outcome == DraftOutcome(
        drafted=1, gate_failed=0, skipped=0, errors=[], stopped_reason=reason
    )
    assert len(fake_claude_env.calls()) == 1
    assert len(await _rows(engine, Sequence)) == 1
    assert (await _campaign(engine, campaign.id)).last_error == reason
    assert (await _campaign(engine, other.id)).last_error == reason  # every active campaign

    # Budget already spent when a pass starts: it stays quiet and writes nothing.
    assert await _draft(engine, runtime, config=config) == DraftOutcome()
    assert len(fake_claude_env.calls()) == 1

    # The next UTC day has a fresh budget: the pass carries on.
    tomorrow = NOW + timedelta(days=1)
    again = await _draft(engine, runtime, config=config, now=lambda: tomorrow)
    assert (again.drafted, again.stopped_reason) == (1, reason)
    assert len(await _rows(engine, Sequence)) == 2


async def test_draft_stops_when_claude_is_unavailable(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = await seed_campaign(db)
    await seed_company(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await db.commit()
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--safe-mode")

    outcome = await _draft(engine, runtime)

    assert outcome.drafted == 0 and outcome.errors == []
    assert outcome.stopped_reason is not None
    assert "missing isolation flags: --safe-mode" in outcome.stopped_reason
    assert fake_claude_env.calls() == []  # nothing was ever asked of Claude
    assert len(fake_claude_env.calls("help")) == 1  # and it stopped after the first company
    assert await _rows(engine, Sequence) == []
    assert await _rows(engine, UsageEvent) == []
    assert (await _campaign(engine, campaign.id)).last_error == outcome.stopped_reason


async def test_signed_out_stops_the_pass(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    for i in range(3):
        await seed_company(db, name=f"Co {i}", domain=f"co{i}.com", score=90 - i)
    await db.commit()
    fake_claude_env.reply(MARKER, {"type": "result", "is_error": True, "result": "Not logged in"})

    outcome = await _draft(engine, runtime)

    assert outcome == DraftOutcome(stopped_reason=SIGNED_OUT)
    assert len(fake_claude_env.calls()) == 1
    assert [u.ok for u in await _rows(engine, UsageEvent)] == [False]  # the call is recorded
    assert await _rows(engine, DraftFailure) == []  # not the company's fault
    assert (await _campaign(engine, campaign.id)).last_error == SIGNED_OUT


async def test_claude_kept_failing_stops_after_three_companies(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    for i in range(5):
        await seed_company(db, name=f"Co {i}", domain=f"co{i}.com", score=90 - i)
    await db.commit()
    fake_claude_env.reply(MARKER, {"type": "result", "is_error": False, "result": "garbage"})

    outcome = await _draft(engine, runtime)

    assert outcome.stopped_reason == KEPT_FAILING == "Claude kept failing — stopped for this pass."
    assert outcome.errors == [f"co{i}.com: {UNEXPECTED}" for i in range(3)]
    assert len(fake_claude_env.calls()) == 6  # each with its one repair retry
    assert len(await _rows(engine, DraftFailure)) == 3
    assert (await _campaign(engine, campaign.id)).last_error == KEPT_FAILING


async def test_every_claude_attempt_counts_toward_the_limit(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=60)
    await seed_company(db)
    await seed_company(db, name="Gamma Co", domain="gamma-co.com", score=30)
    await db.commit()
    fake_claude_env.reply("Beta Labs", {"exit": 1})
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    outcome = await _draft(engine, runtime, limit=2)

    assert (outcome.drafted, len(outcome.errors)) == (1, 1)
    assert len(fake_claude_env.calls()) == 2  # Gamma Co is left for the next pass


async def test_failing_company_backs_off(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    company, _, _ = await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, {"exit": 1})
    day = NOW.replace(hour=0)  # 2026-11-08 00:00 UTC

    async def at(when: datetime) -> DraftOutcome:
        return await _draft(engine, runtime, now=lambda: when)

    async def failure() -> DraftFailure:
        [row] = await _rows(engine, DraftFailure)
        return row

    assert len((await at(day)).errors) == 1
    row = await failure()
    assert (row.campaign_id, row.company_id, row.failures, row.last_failed_at) == (
        campaign.id,
        company.id,
        1,
        day,
    )
    assert row.last_error == "Claude exited with an error (code 1)."
    assert await at(day + timedelta(hours=5, minutes=59)) == DraftOutcome()  # backing off
    assert len(fake_claude_env.calls()) == 1
    assert len((await at(day + timedelta(hours=6, minutes=1))).errors) == 1
    assert len((await at(day + timedelta(hours=12, minutes=2))).errors) == 1
    assert (await failure()).failures == 3
    # Three failures today: left alone for the rest of the UTC day.
    assert await at(day + timedelta(hours=18, minutes=3)) == DraftOutcome()
    assert await at(day + timedelta(hours=23, minutes=59)) == DraftOutcome()
    assert len(fake_claude_env.calls()) == 3

    tomorrow = day + timedelta(days=1, minutes=1)
    assert len((await at(tomorrow)).errors) == 1
    assert ((await failure()).failures, (await failure()).last_failed_at) == (1, tomorrow)

    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    assert (await at(tomorrow + timedelta(hours=6, minutes=1))).drafted == 1
    assert await _rows(engine, DraftFailure) == []  # a stored draft clears the record


async def test_backoff_is_per_campaign(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    first = await seed_campaign(db, now=NOW - timedelta(days=1))
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, {"exit": 1})
    assert len((await _draft(engine, runtime, campaign_id=first.id)).errors) == 1
    second = await seed_campaign(db)
    assert len((await _draft(engine, runtime, campaign_id=second.id)).errors) == 1
    assert len(await _rows(engine, DraftFailure)) == 2


async def test_draft_brain_error_for_one_company_continues(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=60)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply("Beta Labs", {"exit": 1})
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    outcome = await _draft(engine, runtime)

    assert (outcome.drafted, outcome.stopped_reason) == (1, None)
    assert outcome.errors == ["beta-labs.com: Claude exited with an error (code 1)."]
    [seq] = await _rows(engine, Sequence)
    assert seq.recipient == "jane.doe@acme-soap.com"
    usage = await _rows(engine, UsageEvent)
    assert sorted(u.ok for u in usage) == [False, True]
    assert (await _campaign(engine, campaign.id)).last_error is None


async def test_draft_empty_body_is_a_company_error(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    def empty(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        raise ValueError("The email body is empty.")

    monkeypatch.setattr(service, "assemble", empty)
    outcome = await _draft(engine, runtime)
    assert (outcome.drafted, outcome.errors) == (0, ["acme-soap.com: The email body is empty."])
    assert await _rows(engine, Sequence) == []


async def test_daily_cap_and_limit(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db, cap=2)
    for i in range(4):
        await seed_company(db, name=f"Co {i}", domain=f"co{i}.com", score=90 - i)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    first = await _draft(engine, runtime, limit=1)
    assert first.drafted == 1
    second = await _draft(engine, runtime, limit=5)
    assert second.drafted == 1  # the cap (2 a day) is reached
    third = await _draft(engine, runtime, limit=5, now=lambda: NOW + timedelta(hours=11))
    assert third.drafted == 0 and len(fake_claude_env.calls()) == 2  # same UTC day

    tomorrow = NOW + timedelta(hours=12)  # 2026-11-09 00:00 UTC
    fourth = await _draft(engine, runtime, limit=5, now=lambda: tomorrow)
    assert fourth.drafted == 2
    rows = await _rows(engine, Sequence)
    assert sorted(r.created_at for r in rows) == [NOW, NOW, tomorrow, tomorrow]
    assert len({r.company_id for r in rows}) == 4
    assert {r.campaign_id for r in rows} == {campaign.id}
    async with make_session_factory(engine)() as s:
        assert await service.drafted_today(s, campaign.id, now=tomorrow) == 2


async def test_draft_batch_deadline(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    """Writing is time-boxed (follow-up 8): no company is started once the deadline has come,
    so a long drafting pass never holds up the worker's sending."""
    await seed_campaign(db)
    for i in range(3):
        await seed_company(db, name=f"Co {i}", domain=f"co{i}.com", score=90 - i)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    factory = make_session_factory(engine)

    late = await draft_batch(
        factory, runtime=runtime, catalog=CATALOG, facts=FACTS, config=CONFIG,
        now=lambda: NOW, limit=5, deadline=NOW,
    )  # fmt: skip
    assert late.drafted == 0 and fake_claude_env.calls() == []
    assert await _rows(engine, Sequence) == []

    # The clock passes the deadline once Claude has written one: that company finishes (it
    # is stored), no other starts.
    def clock() -> datetime:
        return NOW + timedelta(seconds=301 if fake_claude_env.calls() else 0)

    some = await draft_batch(
        factory, runtime=runtime, catalog=CATALOG, facts=FACTS, config=CONFIG,
        now=clock, limit=5, deadline=NOW + timedelta(seconds=300),
    )  # fmt: skip
    assert some.drafted == 1 and len(fake_claude_env.calls()) == 1


async def test_draft_batch_stop(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    """Ruling P7-G: once the worker is stopping no company is started; one already being
    written finishes (so a planned stop never waits on more than that one)."""
    await seed_campaign(db)
    for i in range(3):
        await seed_company(db, name=f"Co {i}", domain=f"co{i}.com", score=90 - i)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    factory = make_session_factory(engine)

    stop = asyncio.Event()
    stop.set()
    none = await draft_batch(
        factory, runtime=runtime, catalog=CATALOG, facts=FACTS, config=CONFIG,
        now=lambda: NOW, limit=5, stop=stop,
    )  # fmt: skip
    assert none.drafted == 0 and fake_claude_env.calls() == []

    # SIGTERM arrives while Claude writes the first company: it is stored, no other starts.
    stop = asyncio.Event()

    def clock() -> datetime:
        if fake_claude_env.calls():
            stop.set()
        return NOW

    one = await draft_batch(
        factory, runtime=runtime, catalog=CATALOG, facts=FACTS, config=CONFIG,
        now=clock, limit=5, stop=stop,
    )  # fmt: skip
    assert one.drafted == 1 and len(fake_claude_env.calls()) == 1
    assert len(await _rows(engine, Sequence)) == 1


async def test_never_a_second_sequence_for_a_company(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(PRICED))

    assert (await _draft(engine, runtime)).drafted == 1
    again = await _draft(engine, runtime, now=lambda: NOW + timedelta(days=1))
    assert again.drafted == 0
    assert len(fake_claude_env.calls()) == 1  # not even asked
    assert len(await _rows(engine, Sequence)) == 1


async def test_company_taken_while_claude_writes_is_skipped(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = await seed_campaign(db)
    company, _, _ = await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    real = service.write_sequence

    async def racing(*args: Any, **kwargs: Any) -> Any:
        written = await real(*args, **kwargs)
        async with make_session_factory(engine)() as s:  # another drafter got there first
            s.add(
                Sequence(
                    campaign_id=campaign.id,
                    company_id=company.id,
                    recipient="someone@acme-soap.com",
                    status="awaiting_approval",
                )
            )
            await s.commit()
        return written

    monkeypatch.setattr(service, "write_sequence", racing)
    outcome = await _draft(engine, runtime)
    assert (outcome.drafted, outcome.skipped, outcome.errors) == (0, 1, [])
    assert len(await _rows(engine, Sequence)) == 1
    assert await _rows(engine, Message) == []


async def test_address_taken_by_another_campaign_while_claude_writes_is_skipped(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = await seed_campaign(db)
    other = await seed_campaign(db, side="supplier")
    company, _, _ = await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    real = service.write_sequence

    async def racing(*args: Any, **kwargs: Any) -> Any:
        written = await real(*args, **kwargs)
        async with make_session_factory(engine)() as s:  # another campaign's draft lands first
            s.add(
                Sequence(
                    campaign_id=other.id,
                    company_id=company.id,
                    recipient="jane.doe@acme-soap.com",
                    status="awaiting_approval",
                )
            )
            await s.commit()
        return written

    monkeypatch.setattr(service, "write_sequence", racing)
    outcome = await _draft(engine, runtime, campaign_id=campaign.id)
    assert (outcome.drafted, outcome.skipped, outcome.errors) == (0, 1, [])
    [seq] = await _rows(engine, Sequence)
    assert seq.campaign_id == other.id  # no second, held sequence to the same address
    assert await _rows(engine, Message) == []


async def test_campaign_paused_while_claude_writes_stores_nothing(
    engine: AsyncEngine,
    db: AsyncSession,
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = await seed_campaign(db)
    await seed_company(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    real = service.write_sequence

    async def pausing(*args: Any, **kwargs: Any) -> Any:
        written = await real(*args, **kwargs)
        async with make_session_factory(engine)() as s:
            await service.pause(s, campaign.id, actor=None)
            await s.commit()
        return written

    monkeypatch.setattr(service, "write_sequence", pausing)
    outcome = await _draft(engine, runtime)
    assert (outcome.drafted, outcome.skipped, outcome.closed) == (0, 0, 1)
    assert len(fake_claude_env.calls()) == 1  # the campaign's walk stopped
    assert await _rows(engine, Sequence) == []


async def test_only_active_campaigns_oldest_first(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    user = await seed_user(db)
    newer = await seed_campaign(db, user=user, name="Newer", now=NOW)
    older = await seed_campaign(db, user=user, name="Older", now=NOW - timedelta(days=1))
    draft = await seed_campaign(db, user=user, name="Draft", activate=False)
    paused = await seed_campaign(db, user=user, name="Paused")
    await service.pause(db, paused.id, actor=user)
    await seed_company(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    outcome = await _draft(engine, runtime, limit=1)
    assert outcome.drafted == 1
    [seq] = await _rows(engine, Sequence)
    assert seq.campaign_id == older.id

    # One campaign only (the CLI's `draft --campaign`); the address used by "Older" is
    # skipped, so "Newer" writes to the other company.
    only = await _draft(engine, runtime, campaign_id=newer.id)
    assert only.drafted == 1
    rows = await _rows(engine, Sequence)
    assert {r.campaign_id for r in rows} == {older.id, newer.id}
    assert len({r.recipient for r in rows}) == 2
    assert (await _draft(engine, runtime, campaign_id=draft.id)).drafted == 0
    assert (await _draft(engine, runtime, campaign_id=paused.id)).drafted == 0


async def test_unconfirmed_facts_after_activation_skip_the_campaign(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    await seed_company(db)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    facts = _facts(postal_address_confirmed=False)

    outcome = await _draft(engine, runtime, facts=facts)

    message = "Confirm Elchemy's postal address in facts.yaml first."
    assert outcome.drafted == 0
    assert outcome.errors == [f"Campaign {campaign.name}: {message}"]
    assert fake_claude_env.calls() == []  # no Claude call spent on a draft the gate would hold
    assert (await _campaign(engine, campaign.id)).last_error == message
    assert (await _draft(engine, runtime)).drafted == 1
    assert (await _campaign(engine, campaign.id)).last_error is None


async def test_last_error_kept_until_the_campaign_drafts(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    campaign = await seed_campaign(db)
    company, _, _ = await seed_company(db)
    company.sendable_contacts = 0  # no candidates yet
    stored = await db.get(Campaign, campaign.id)
    assert stored is not None
    stored.last_error = "Daily Claude budget reached (150 calls)."
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    assert await _draft(engine, runtime) == DraftOutcome()
    assert (await _campaign(engine, campaign.id)).last_error == stored.last_error
    async with make_session_factory(engine)() as s:
        row = await s.get(Company, company.id)
        assert row is not None
        row.sendable_contacts = 1
        await s.commit()
    assert (await _draft(engine, runtime)).drafted == 1
    assert (await _campaign(engine, campaign.id)).last_error is None


async def test_companies_without_sendable_contacts_are_not_walked(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    company, _, _ = await seed_company(db)
    company.sendable_contacts = 0
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    assert await _draft(engine, runtime) == DraftOutcome()  # not even counted as skipped
    assert fake_claude_env.calls() == []


async def test_companies_without_a_recipient_are_skipped(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    await seed_campaign(db)
    await seed_company(db, address=None)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    outcome = await _draft(engine, runtime)
    assert (outcome.drafted, outcome.skipped) == (1, 1)
    assert len(fake_claude_env.calls()) == 1


async def test_fresh_now_per_company(
    engine: AsyncEngine, db: AsyncSession, runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    """Each company's Claude call and sequence use a fresh clock reading (budget day)."""
    await seed_campaign(db)
    await seed_company(db)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    ticks = iter(NOW + timedelta(minutes=i) for i in range(100))

    outcome = await _draft(engine, runtime, now=lambda: next(ticks))
    assert outcome.drafted == 2
    usage = sorted(u.at for u in await _rows(engine, UsageEvent))
    assert len(usage) == 2 and usage[0] != usage[1]
    async with make_session_factory(engine)() as s:
        assert await s.scalar(select(func.count()).select_from(Sequence)) == 2


# --------------------------------------------------------------------------- mailbox


async def test_set_mailbox_checks_the_side(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, activate=False, mailbox=False)
    other = await seed_mailbox(db, user, side="supplier")
    mine = await seed_mailbox(db, user, side="buyer")
    with pytest.raises(ValueError, match=r"^That mailbox is for the other side\.$"):
        await service.set_mailbox(db, campaign.id, other.id, actor=user)
    chosen = await service.set_mailbox(db, campaign.id, mine.id, actor=user)
    assert chosen.mailbox_id == mine.id
    with pytest.raises(ValueError, match=r"^That mailbox no longer exists\.$"):
        await service.set_mailbox(db, campaign.id, uuid.uuid4(), actor=user)
    await db.commit()
    await service.archive(db, campaign.id, actor=user)
    with pytest.raises(ValueError, match="archived"):
        await service.set_mailbox(db, campaign.id, mine.id, actor=user)


async def test_activation_needs_a_mailbox(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, activate=False, mailbox=False)
    problems = await service.activation_problems(db, campaign, facts=FACTS)
    assert problems == ["Choose a sending mailbox for this campaign."]
    with pytest.raises(ValueError, match=r"^Choose a sending mailbox for this campaign\.$"):
        await service.activate(db, campaign.id, actor=user, facts=FACTS, config=CONFIG)
    box = await seed_mailbox(db, user)
    await service.set_mailbox(db, campaign.id, box.id, actor=user)
    assert await service.activation_problems(db, campaign, facts=FACTS) == []


async def test_an_active_campaign_without_a_mailbox_is_left_alone(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    campaign.mailbox_id = None
    await db.commit()
    assert await service.activation_problems(db, campaign, facts=FACTS) == []
