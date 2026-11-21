"""The worker loop: one pass runs reap -> sweep -> due sync -> sending -> follow-ups -> one
approved job -> profiling -> contacts -> writing. No network."""

import asyncio
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from campaign_helpers import (
    CLEAN,
    MARKER,
    PRICED,
    ok_reply,
    seed_campaign,
    seed_company,
    seed_draft,
)
from campaign_helpers import CONFIG as HELPER_CONFIG
from campaign_helpers import FACTS as CONFIRMED_FACTS
from conftest import FakeClaude
from fakes.dns import FakeResolver
from fakes.hubspot import FAKE_TOKEN, FakeHubSpot, company
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from fakes.serper import FAKE_KEY, FakeSerper, organic
from fakes.verifier import FAKE_KEY as VERIFIER_KEY
from fakes.verifier import FakeMillionVerifier
from fakes.web import FakeWeb
from mail_helpers import reply_to
from salesfinder.campaigns.service import DraftOutcome
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig, QuietHours
from salesfinder.contacts.service import DNS_UNAVAILABLE, EXCLUDED, VerifyOutcome
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    Campaign,
    Company,
    DiscoveryJob,
    DoNotContact,
    EmailAddress,
    HubSpotSync,
    Mailbox,
    Message,
    Run,
    Sequence,
    User,
)
from salesfinder.discovery import jobs
from salesfinder.exclusions import sync
from salesfinder.ops import runs
from salesfinder.ops.quiet import quiet_line
from salesfinder.ops.runs import PassResult
from salesfinder.replies.classify import RepliesOutcome
from salesfinder.replies.ingest import PollOutcome
from salesfinder.sending import state
from salesfinder.sending.sender import SendOutcome
from salesfinder.sending.state import NOT_TURNED_ON
from salesfinder.settings import Settings
from salesfinder.signals import settings as sig
from salesfinder.suppressions import service as suppressions
from salesfinder.worker import (
    BEAT_TASK,
    NO_SEND_LINE,
    NO_VERIFIER_KEY,
    WORKER_LOCK_KEY,
    AnotherWorkerRunning,
    WorkerDeps,
    beat_once,
    contacts_line,
    followups_line,
    lock_check,
    mail_lines,
    outcomes_line,
    profiled_line,
    record,
    replies_lines,
    run_forever,
    run_once,
    run_pass,
    run_single,
    sending_lines,
    writing_lines,
)
from sending_helpers import SEND_NOW, SENT_FOLDER, seed_ready, sending_settings

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
# fake_claude markers: the classification prompt and the drafting prompt.
CLASSIFY_MARKER = "REPLY (untrusted"
DRAFT_MARKER = "Write the reply"
OBJECTION = {
    "intent": "objection",
    "confidence": 0.9,
    "summary": "They already have a glycerin supplier.",
    "products": ["glycerin"],
}
ANSWER = {
    "body": "Hi Jane,\n\nThanks for letting us know. If it would help to compare, we can "
    "send the CoA and SDS for our glycerin.\n\nThanks,"
}


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


# Requests that reached the fail-closed HubSpot/Serper transports (a token or key is set but
# the test gave no fake): checked after every test.
_UNEXPECTED: list[str] = []


def _fail_closed(service: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        _UNEXPECTED.append(f"{service} {request.method} {request.url.host}")
        raise httpx.ConnectError(f"test: no real {service}", request=request)

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def _no_unexpected_api_calls() -> Iterator[None]:
    _UNEXPECTED.clear()
    yield
    hits = list(_UNEXPECTED)
    _UNEXPECTED.clear()
    if hits:
        pytest.fail(f"A worker test called an API it gave no fake for: {hits}")


def _deps(
    engine: AsyncEngine,
    settings: Settings,
    *,
    hubspot: FakeHubSpot | None = None,
    serper: FakeSerper | None = None,
    token: bool = True,
    key: bool = True,
    poll_seconds: int = 10,
    web: FakeWeb | None = None,
    resolver: FakeResolver | None = None,
    verifier: FakeMillionVerifier | None = None,
    verifier_key: bool = False,
    config: AppConfig | None = None,
    facts: Facts | None = None,
    now: Callable[[], datetime] = lambda: NOW,
) -> WorkerDeps:
    settings = settings.model_copy(
        update={
            "hubspot_token": SecretStr(FAKE_TOKEN) if token else None,
            "serper_api_key": SecretStr(FAKE_KEY) if key else None,
            "email_verifier_key": SecretStr(VERIFIER_KEY) if verifier_key else None,
        }
    )
    config = config or SHIPPED.config
    facts = facts or SHIPPED.facts
    config = config.model_copy(
        update={"worker": config.worker.model_copy(update={"poll_seconds": poll_seconds})}
    )
    return WorkerDeps(
        session_factory=make_session_factory(engine),
        settings=settings,
        config=config,
        catalog=SHIPPED.catalog,
        facts=facts,
        now=now,
        # A token/key without a fake gets a fail-closed transport, never the real API.
        hubspot_transport=(
            hubspot.transport if hubspot else _fail_closed("HubSpot") if token else None
        ),
        serper_transport=serper.transport if serper else _fail_closed("Serper") if key else None,
        web_transport=web.transport if web else FakeWeb({}, {}).transport,
        web_resolver=web.resolver if web else FakeWeb({}, {}).resolver,
        # Never real DNS in tests: unknown names get no answer (no mail server).
        dns_resolve=resolver if resolver is not None else FakeResolver(),
        verifier_transport=(
            verifier.transport()
            if verifier
            else _fail_closed("MillionVerifier")
            if verifier_key
            else None
        ),
    )


def test_mail_lines_name_each_mailbox_held_untested() -> None:
    """Final wave: a mailbox whose settings haven't passed a test isn't read; the worker says
    so (our own text and the address only)."""
    lines = mail_lines(
        {
            "a@send-domain.com": PollOutcome(untested=True),
            "b@send-domain.com": PollOutcome(replies=1),
        }
    )
    assert lines == [
        "Read mail: 1 repl(ies), 0 bounce(s), 0 unsubscribe(s), 0 takeover(s).",
        "Not reading a@send-domain.com: test its connection first.",
    ]


def _hubspot() -> FakeHubSpot:
    return FakeHubSpot([company("1", "Acme", domain="acme.com")], [], [])


def _serper() -> FakeSerper:
    return FakeSerper(lambda _p: [organic("https://newco-cosmetics.com", "NewCo - Home")])


async def _job(db: AsyncSession, user: User, *, approve: bool) -> DiscoveryJob:
    job = await jobs.create_estimate(
        db,
        side="buyer",
        product_ids=["sles"],
        industries=["personal_care"],
        region_names=["Texas"],
        created_by=user,
        catalog=SHIPPED.catalog,
        config=SHIPPED.config.discovery,
    )
    if approve:
        await jobs.approve_job(
            db, job.id, approved_by=user, now=NOW, config=SHIPPED.config.discovery
        )
    await db.commit()
    return job


async def _status(engine: AsyncEngine, job_id: object) -> DiscoveryJob:
    async with make_session_factory(engine)() as s:
        row = await s.get(DiscoveryJob, job_id)
        assert row is not None
        return row


async def _syncs(engine: AsyncEngine) -> list[HubSpotSync]:
    async with make_session_factory(engine)() as s:
        return list((await s.scalars(select(HubSpotSync))).all())


async def test_run_once_runs_due_sync_and_approved_job(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    job = await _job(db, admin, approve=True)
    hs, sp = _hubspot(), _serper()
    lines = await run_once(_deps(engine, test_settings, hubspot=hs, serper=sp))
    text = "\n".join(lines)
    assert "HubSpot sync succeeded" in text
    assert "Discovery job" in text
    assert (await _status(engine, job.id)).status == "succeeded"
    assert [s.status for s in await _syncs(engine)] == ["succeeded"]
    assert not sp.violations


async def test_run_once_never_starts_unapproved_jobs(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    job = await _job(db, admin, approve=False)
    sp = _serper()
    lines = await run_once(_deps(engine, test_settings, hubspot=_hubspot(), serper=sp))
    assert not any("Discovery job" in line for line in lines)
    assert (await _status(engine, job.id)).status == "estimated"
    assert sp.calls == []


async def test_missing_serper_key_fails_approved_job(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    job = await _job(db, admin, approve=True)
    lines = await run_once(_deps(engine, test_settings, hubspot=_hubspot(), key=False))
    row = await _status(engine, job.id)
    assert row.status == "failed"
    assert row.error == "SALESFINDER_SERPER_API_KEY is not set."
    assert row.finished_at is not None
    assert any("SALESFINDER_SERPER_API_KEY is not set." in line for line in lines)


async def test_no_hubspot_token_skips_sync(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    lines = await run_once(_deps(engine, test_settings, token=False, key=False))
    assert lines == []
    assert await _syncs(engine) == []


async def test_one_step_failing_does_not_block_next(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    job = await _job(db, admin, approve=True)
    hs = FakeHubSpot([], [], [], fail={"companies": 401})
    lines = await run_once(_deps(engine, test_settings, hubspot=hs, serper=_serper()))
    assert [s.status for s in await _syncs(engine)] == ["failed"]
    assert any("HubSpot sync failed" in line for line in lines)
    assert (await _status(engine, job.id)).status == "succeeded"


async def test_unexpected_exception_in_step_is_reported(
    engine: AsyncEngine,
    db: AsyncSession,
    admin: User,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db, admin, approve=True)

    async def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("salesfinder.worker.due_for_sync", boom)
    lines = await run_once(_deps(engine, test_settings, hubspot=_hubspot(), serper=_serper()))
    # The exception type only: its message could carry page text or a provider's reply.
    assert "HubSpot step failed: RuntimeError" in lines
    assert not any("kaboom" in line for line in lines)
    assert (await _status(engine, job.id)).status == "succeeded"


async def test_run_once_reaps_interrupted_jobs_and_syncs(
    engine: AsyncEngine, db: AsyncSession, admin: User, test_settings: Settings
) -> None:
    job = await _job(db, admin, approve=True)
    claimed = await jobs.claim_next_job(db, now=NOW - timedelta(hours=3))
    assert claimed is not None
    db.add(HubSpotSync(status="running", started_at=NOW - timedelta(hours=3)))
    await db.commit()
    lines = await run_once(_deps(engine, test_settings, hubspot=_hubspot(), serper=_serper()))
    row = await _status(engine, job.id)
    assert row.status == "failed" and row.error == jobs.INTERRUPTED
    statuses = sorted(s.status for s in await _syncs(engine))
    assert statuses == ["failed", "succeeded"]
    assert any("interrupted" in line.lower() for line in lines)


async def test_fail_interrupted_syncs(db: AsyncSession) -> None:
    db.add(HubSpotSync(status="running", started_at=NOW - timedelta(hours=2)))
    db.add(HubSpotSync(status="running", started_at=NOW - timedelta(minutes=5)))
    db.add(HubSpotSync(status="queued"))
    await db.flush()
    assert await sync.fail_interrupted_syncs(db, now=NOW) == 1
    rows = (await db.scalars(select(HubSpotSync).order_by(HubSpotSync.started_at))).all()
    failed = [r for r in rows if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].error == "Interrupted (worker stopped mid-sync)."
    assert failed[0].finished_at == NOW
    assert sorted(r.status for r in rows) == ["failed", "queued", "running"]


async def test_run_forever_stops(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    stop = asyncio.Event()
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=30)
    task = asyncio.create_task(run_forever(deps, stop=stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_run_once_sweeps_new_exclusions_into_stored_companies(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    # A do-not-contact entry that landed after the company was stored (e.g. while the job that
    # stored it was still running) is applied on the worker's next pass.
    db.add(
        Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
        )
    )
    db.add(DoNotContact(kind="domain", value="acme.com", reason="Asked us to stop"))
    await db.commit()

    lines = await run_once(_deps(engine, test_settings, token=False, key=False))

    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(Company))
    assert row is not None
    assert (row.status, row.exclusion_reason) == ("excluded", "do_not_contact")
    assert row.exclusion_detail == "Do-not-contact: acme.com — Asked us to stop"
    assert "Excluded 1 stored company(ies) now on an exclusion list." in lines
    assert await run_once(_deps(engine, test_settings, token=False, key=False)) == []


def _acme(**fields: object) -> Company:
    values: dict[str, object] = {
        "side": "buyer",
        "name": "Acme",
        "domain": "acme.com",
        "website_url": "https://acme.com",
        "status": "new",
        **fields,
    }
    return Company(**values)


async def test_run_once_profiles_pending_companies(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
    db.add(_acme())
    await db.commit()
    web = FakeWeb(
        {"https://acme.com/": "<p>ISO 9001 certified</p>"}, {"acme.com": ["93.184.216.34"]}
    )

    lines = await run_once(_deps(engine, test_settings, token=False, key=False, web=web))

    assert lines == [
        "Profiled 1 company (1 ok, 0 failed).",
        "Checked contacts for 1 company (0 sendable addresses).",
    ]
    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(Company))
    assert row is not None
    assert (row.profile_status, row.score) == ("ok", 10)
    assert web.violations == []
    # Nothing left to do on the next pass.
    assert await run_once(_deps(engine, test_settings, token=False, key=False, web=web)) == []


async def test_run_once_never_profiles_without_confirmed_signals(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    db.add(_acme())
    await db.commit()
    web = FakeWeb({"https://acme.com/": "<p>hi</p>"}, {"acme.com": ["93.184.216.34"]})
    assert await run_once(_deps(engine, test_settings, token=False, key=False, web=web)) == []
    assert web.calls == []


def test_profiled_line_pluralizes() -> None:
    assert profiled_line(["ok"]) == "Profiled 1 company (1 ok, 0 failed)."
    statuses = ["ok", "partial", "ok", "ok", "ok", "ok", "no_site"]
    assert profiled_line(statuses) == "Profiled 7 companies (6 ok, 1 failed)."
    assert profiled_line(["failed", "blocked"]) == "Profiled 2 companies (0 ok, 2 failed)."
    # Ruling V: skipped outcomes are not counted, only noted.
    assert profiled_line(["ok", "skipped"]) == (
        "Profiled 1 company (1 ok, 0 failed). 1 skipped (excluded meanwhile)."
    )
    assert profiled_line(["skipped", "failed", "skipped"]) == (
        "Profiled 1 company (0 ok, 1 failed). 2 skipped (excluded meanwhile)."
    )


async def test_sweep_failure_does_not_prevent_reaping(
    engine: AsyncEngine,
    db: AsyncSession,
    admin: User,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _job(db, admin, approve=True)
    assert await jobs.claim_next_job(db, now=NOW - timedelta(hours=3)) is not None
    await db.commit()

    async def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("salesfinder.worker.sweep_exclusions", boom)
    lines = await run_once(_deps(engine, test_settings, token=False, key=False))

    row = await _status(engine, job.id)
    assert row.status == "failed" and row.error == jobs.INTERRUPTED
    assert "Failed 1 interrupted discovery job(s)." in lines
    assert any(line.startswith("Sweep step failed: RuntimeError") for line in lines)


async def test_reaper_failure_does_not_prevent_sweeping(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.add(_acme())
    db.add(DoNotContact(kind="domain", value="acme.com", reason="Asked us to stop"))
    await db.commit()

    async def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr("salesfinder.worker.fail_interrupted_syncs", boom)
    lines = await run_once(_deps(engine, test_settings, token=False, key=False))

    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(Company))
    assert row is not None and row.status == "excluded"
    assert any(line.startswith("Reaper step failed: RuntimeError") for line in lines)
    assert "Excluded 1 stored company(ies) now on an exclusion list." in lines


def test_outcomes_line_reports_a_network_hold() -> None:
    from salesfinder.profiling.service import NETWORK_DOWN, ProfileOutcome

    held = ProfileOutcome(uuid.uuid4(), "skipped", 0, 0, None, NETWORK_DOWN)
    ok = ProfileOutcome(uuid.uuid4(), "ok", 1, 1, 10, None)
    assert outcomes_line([held, held, held]) == "Network looks down — will retry."
    assert outcomes_line([ok]) == "Profiled 1 company (1 ok, 0 failed)."


TEAM_SITE = {
    "https://acme.com/": '<p>ISO 9001 certified.</p><a href="/team">Our team</a>',
    "https://acme.com/team": (
        "<title>Our Team</title><ul><li>Priya Nair | Head of Procurement</li>"
        "<li>Anil Mehta \u2013 Owner</li></ul><p>Mail priya.nair@acme.com</p>"
    ),
}


async def test_run_once_checks_contacts_after_profiling(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
    db.add(_acme())
    await db.commit()
    web = FakeWeb(TEAM_SITE, {"acme.com": ["93.184.216.34"]})
    resolver = FakeResolver({("acme.com", "MX"): [(10, "mx.acme.com.")]})

    lines = await run_once(
        _deps(engine, test_settings, token=False, key=False, web=web, resolver=resolver)
    )

    assert lines == [
        "Profiled 1 company (1 ok, 0 failed).",
        "Checked contacts for 1 company (2 sendable addresses).",
    ]
    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(Company))
    assert row is not None
    assert (row.contacts_found, row.sendable_contacts, row.contacts_checked_at) == (2, 2, NOW)
    again = await run_once(
        _deps(engine, test_settings, token=False, key=False, web=web, resolver=resolver)
    )
    assert again == []


def _verifier_config() -> AppConfig:
    return SHIPPED.config.model_copy(
        update={
            "contacts": SHIPPED.config.contacts.model_copy(update={"verifier": "millionverifier"})
        }
    )


async def test_verifier_configured_without_key_is_noted(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    db.add(_acme(profile_status="ok"))
    await db.commit()
    fake = FakeMillionVerifier()

    lines = await run_once(
        _deps(
            engine,
            test_settings,
            token=False,
            key=False,
            verifier=fake,
            config=_verifier_config(),
        )
    )

    assert lines == [
        "Checked contacts for 1 company (0 sendable addresses).",
        "Email verifier configured but SALESFINDER_EMAIL_VERIFIER_KEY is not set.",
    ]
    assert NO_VERIFIER_KEY == lines[1]
    assert fake.requests == []
    # Nothing due: the note isn't repeated on every idle pass.
    assert await run_once(_deps(engine, test_settings, token=False, key=False)) == []


async def test_verifier_with_key_checks_guesses(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
    db.add(_acme())
    await db.commit()
    site = {
        "https://acme.com/": TEAM_SITE["https://acme.com/"],
        "https://acme.com/team": str(TEAM_SITE["https://acme.com/team"]).replace(
            "<p>Mail priya.nair@acme.com</p>", ""
        ),
    }
    web = FakeWeb(site, {"acme.com": ["93.184.216.34"]})
    resolver = FakeResolver({("acme.com", "MX"): [(10, "mx.acme.com.")]})
    fake = FakeMillionVerifier({"result": "ok"})

    lines = await run_once(
        _deps(
            engine,
            test_settings,
            token=False,
            key=False,
            web=web,
            resolver=resolver,
            verifier=fake,
            verifier_key=True,
            config=_verifier_config(),
        )
    )

    assert lines[-1] == "Checked contacts for 1 company (2 sendable addresses)."
    emails = sorted(r.url.params["email"] for r in fake.requests)
    assert emails == ["anil.mehta@acme.com", "priya.nair@acme.com"]


def test_contacts_line_pluralizes() -> None:
    def outcome(sendable: int, error: str | None = None) -> VerifyOutcome:
        return VerifyOutcome(uuid.uuid4(), 2, 3, sendable, "has_mx", error)

    assert contacts_line([outcome(2)]) == "Checked contacts for 1 company (2 sendable addresses)."
    assert contacts_line([outcome(1)]) == "Checked contacts for 1 company (1 sendable address)."
    assert contacts_line([outcome(0), outcome(3), outcome(1)]) == (
        "Checked contacts for 3 companies (4 sendable addresses)."
    )
    # Ruling AJ: DNS-unknown and crashed checks are noted; excluded ones aren't counted.
    unchecked = [
        outcome(2),
        outcome(0, DNS_UNAVAILABLE),
        outcome(0, "Contact check error (RuntimeError)."),
        outcome(0, EXCLUDED),
    ]
    assert contacts_line(unchecked) == (
        "Checked contacts for 3 companies (2 sendable addresses). (2 couldn't be checked)"
    )
    assert contacts_line([outcome(0, DNS_UNAVAILABLE)]) == (
        "Checked contacts for 1 company (0 sendable addresses). (1 couldn't be checked)"
    )


async def test_verifier_failure_is_reported(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    """Item 5: a bad key / no credits is said on the worker line, not silently "unknown"."""
    await sig.set_state(db, "QUALITY_CERTS", "confirmed", actor=None)
    db.add(_acme())
    await db.commit()
    site = {
        "https://acme.com/": TEAM_SITE["https://acme.com/"],
        "https://acme.com/team": str(TEAM_SITE["https://acme.com/team"]).replace(
            "<p>Mail priya.nair@acme.com</p>", ""
        ),
    }
    web = FakeWeb(site, {"acme.com": ["93.184.216.34"]})
    resolver = FakeResolver({("acme.com", "MX"): [(10, "mx.acme.com.")]})
    fake = FakeMillionVerifier({"error": "Insufficient credits", "result": ""})

    lines = await run_once(
        _deps(
            engine,
            test_settings,
            token=False,
            key=False,
            web=web,
            resolver=resolver,
            verifier=fake,
            verifier_key=True,
            config=_verifier_config(),
        )
    )

    assert lines[-1] == (
        "Checked contacts for 1 company (0 sendable addresses)."
        " Email verifier failed — check the key and credits."
    )
    assert len(fake.requests) == 2
    assert all("Insufficient" not in line for line in lines)


def test_contacts_line_notes_verifier_failure() -> None:
    outcome = VerifyOutcome(uuid.uuid4(), 2, 3, 1, "has_mx", None)
    assert contacts_line([outcome], verifier_failed=True) == (
        "Checked contacts for 1 company (1 sendable address)."
        " Email verifier failed — check the key and credits."
    )


# --- writing ------------------------------------------------------------------------------------


def test_writing_lines() -> None:
    assert writing_lines(DraftOutcome()) == []
    assert writing_lines(DraftOutcome(skipped=3, errors=["x.com: boom"])) == []
    assert writing_lines(DraftOutcome(drafted=1)) == [
        "Drafted 1 email for review (0 held by the gate)."
    ]
    assert writing_lines(DraftOutcome(drafted=3, gate_failed=2)) == [
        "Drafted 3 emails for review (2 held by the gate)."
    ]
    assert writing_lines(DraftOutcome(stopped_reason="Daily Claude budget reached (5 calls).")) == [
        "Drafted 0 emails for review (0 held by the gate).",
        "Writing stopped: Daily Claude budget reached (5 calls).",
    ]


async def test_run_once_drafts_for_active_campaigns(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    await seed_campaign(db, now=NOW)
    await seed_company(db, checked_at=NOW)
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40, checked_at=NOW)
    await db.commit()
    fake_claude_env.reply("Beta Labs", ok_reply(PRICED))
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    deps = _deps(
        engine, test_settings_with_fake_claude, token=False, key=False, facts=CONFIRMED_FACTS
    )

    lines = await run_once(deps)

    assert lines == ["Drafted 2 emails for review (1 held by the gate)."]
    assert len(fake_claude_env.calls()) == 2
    runtime = deps.brain
    assert runtime is not None  # built once, kept for the worker's lifetime
    assert await run_once(deps) == []  # nothing left to write
    assert deps.brain is runtime
    assert len(fake_claude_env.calls("help")) == 1  # isolation flags checked once


async def test_run_once_reports_writing_stopped(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_campaign(db, now=NOW)
    await seed_company(db, checked_at=NOW)
    await db.commit()
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--safe-mode")
    deps = _deps(
        engine, test_settings_with_fake_claude, token=False, key=False, facts=CONFIRMED_FACTS
    )

    assert await run_once(deps) == [
        "Drafted 0 emails for review (0 held by the gate).",
        "Writing stopped: Claude CLI is missing isolation flags: --safe-mode. Run `claude update`.",
    ]
    assert fake_claude_env.calls() == []


async def test_writing_runs_at_most_once_per_interval(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    await seed_campaign(db, now=NOW)
    await seed_company(db, checked_at=NOW)
    await db.commit()
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    clock = [NOW]
    deps = _deps(
        engine,
        test_settings_with_fake_claude,
        token=False,
        key=False,
        facts=CONFIRMED_FACTS,
        now=lambda: clock[0],
    )
    assert SHIPPED.config.writing.min_interval_minutes == 5

    assert await run_once(deps) == ["Drafted 1 email for review (0 held by the gate)."]
    await seed_company(db, name="Beta Labs", domain="beta-labs.com", score=40, checked_at=NOW)
    await db.commit()
    clock[0] = NOW + timedelta(minutes=4, seconds=59)
    assert await run_once(deps) == []  # too soon: the writing step doesn't run
    assert len(fake_claude_env.calls()) == 1
    clock[0] = NOW + timedelta(minutes=5)
    assert await run_once(deps) == ["Drafted 1 email for review (0 held by the gate)."]


async def test_sweep_stops_sequences_now_excluded(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    """Follow-up 9: a recipient put on do-not-contact after approval has its sequence stopped
    by the next Sweep, its unsent emails cancelled."""
    campaign = await seed_campaign(db, now=NOW)
    message = await seed_draft(db, campaign)
    rows = (
        await db.scalars(select(Message).where(Message.sequence_id == message.sequence_id))
    ).all()
    for m in rows:
        m.status = "approved" if m.step == 1 else "pending"
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    sequence.status = "active"
    db.add(DoNotContact(kind="email", value="jane.doe@acme-soap.com", reason="Asked us to stop"))
    await db.commit()
    deps = _deps(
        engine, test_settings_with_fake_claude, token=False, key=False, facts=CONFIRMED_FACTS
    )

    lines = await run_once(deps)

    assert "Stopped 1 sequence(s) now excluded." in lines
    assert fake_claude_env.calls() == []  # nothing new to draft
    async with make_session_factory(engine)() as s:
        seq = await s.get(Sequence, message.sequence_id)
        assert seq is not None
        assert (seq.status, seq.stop_reason) == (
            "stopped",
            "Excluded: Do-not-contact: jane.doe@acme-soap.com — Asked us to stop",
        )
        statuses = set(
            (await s.scalars(select(Message.status).where(Message.sequence_id == seq.id))).all()
        )
    assert statuses == {"cancelled"}
    assert not any(line.startswith("Stopped") for line in await run_once(deps))


async def test_sweep_blocks_addresses_suppressed_without_refresh(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    """A suppression written with refresh=False (its caller held outreach locks) is applied to
    the stored address by the next Sweep."""
    campaign = await seed_campaign(db, now=NOW)
    await seed_draft(db, campaign)
    await suppressions.suppress(
        db, "jane.doe@acme-soap.com", reason="bounced", detail="550", refresh=False
    )
    await db.commit()
    deps = _deps(
        engine, test_settings_with_fake_claude, token=False, key=False, facts=CONFIRMED_FACTS
    )

    await run_once(deps)

    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(EmailAddress))
        assert row is not None and row.blocked_reason is not None
        assert row.blocked_reason.startswith("Bounced on ")
    assert fake_claude_env.calls() == []


# --------------------------------------------------------------------------- sending


async def test_run_once_step_order(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §6.1 heartbeat order: Mail and Sending run after HubSpot (fresh exclusions first),
    mail before sending (a reply or bounce stops what would go next), Replies right after Mail
    (Ruling P6-B), then Follow-ups, before the long steps (discovery, profiling, contacts,
    writing)."""
    import salesfinder.worker as worker_module

    order: list[str] = []
    steps = {
        "Reaper": "_reap",
        "Sweep": "_sweep",
        "HubSpot": "_hubspot",
        "Mail": "_mail",
        "Replies": "_replies",
        "Sending": "_sending",
        "Follow-ups": "_followups",
        "Discovery": "_discovery",
        "Profiling": "_profiling",
        "Contacts": "_contacts",
        "Writing": "_writing",
    }
    for label, attr in steps.items():

        async def step(_deps: WorkerDeps, label: str = label) -> list[str]:
            order.append(label)
            return []

        monkeypatch.setattr(worker_module, attr, step)

    assert await run_once(_deps(engine, test_settings, token=False, key=False)) == []
    assert order == list(steps)


def test_replies_lines() -> None:
    assert replies_lines(RepliesOutcome()) == []
    assert replies_lines(RepliesOutcome(classified=3, drafted=2, held=1, needs_you=1)) == [
        "Classified 3 repl(ies); drafted 2 answer(s) for review (1 held by the gate); 1 need you."
    ]
    assert replies_lines(
        RepliesOutcome(classified=1, stopped_reason="Daily Claude budget reached (50 calls).")
    ) == [
        "Classified 1 repl(ies); drafted 0 answer(s) for review (0 held by the gate); 0 need you.",
        "Replies stopped: Daily Claude budget reached (50 calls).",
    ]
    assert replies_lines(RepliesOutcome(stopped_reason="Claude CLI not found on PATH.")) == [
        "Replies stopped: Claude CLI not found on PATH."
    ]


def _empty_reader() -> FakeReader:
    return FakeReader({"INBOX": FakeFolder(1), "[Gmail]/Sent Mail": FakeFolder(1)})


def test_sending_lines() -> None:
    assert sending_lines(SendOutcome()) == []
    assert sending_lines(
        SendOutcome(
            sent=2,
            held={"sales@send-domain.com": "Outside the recipient's office hours."},
            unconfirmed=1,
            paused="Paused automatically: 3 of the last 50 emails bounced (6%).",
        )
    ) == [
        "Sent 2 email(s).",
        "Sending is paused: Paused automatically: 3 of the last 50 emails bounced (6%).",
        "Held: sales@send-domain.com — Outside the recipient's office hours.",
        "1 send(s) couldn't be confirmed — check the Outbox.",
    ]
    assert sending_lines(SendOutcome(paused=NOT_TURNED_ON)) == [NOT_TURNED_ON]


async def test_run_once_sends_approved_mail(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await seed_ready(db, resume=False)
    sender = FakeSender()
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    deps.mail = fake_mail(sender, _empty_reader())
    deps.last_writing_at = SEND_NOW  # writing isn't due: no Claude in this test

    # Paused by default: said once, nothing sent.
    assert await run_once(deps) == [NOT_TURNED_ON]
    async with make_session_factory(engine)() as s:
        await state.resume(s, actor=None, now=SEND_NOW - timedelta(hours=1))
        await s.commit()

    assert await run_once(deps) == ["Sent 1 email(s)."]
    assert len(sender.sent) == 1
    assert await run_once(deps) == []  # the mailbox waits out its spacing; nothing else waits


async def test_sending_step_does_nothing_without_mail(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await seed_ready(db)
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    deps.last_writing_at = SEND_NOW
    assert deps.mail is None
    assert await run_once(deps) == []


# --------------------------------------------------------------------------- follow-ups


def test_followups_line() -> None:
    assert followups_line(2, 1) == "Queued 2 follow-up(s) for review (1 held by the gate)."


async def test_run_once_queues_a_due_followup_and_never_sends_it(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    ready = await seed_ready(db)
    due = datetime(2026, 11, 16, 14, tzinfo=UTC)
    async with make_session_factory(engine)() as s:
        step1 = await s.get(Message, ready.message_id)
        step2 = await s.get(Message, ready.step2_id)
        assert step1 is not None and step2 is not None
        step1.status = "sent"
        step1.sent_at = SEND_NOW
        step1.message_id_header = "<first@send-domain.com>"
        step2.due_at = due
        await s.commit()
    sender = FakeSender()
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        token=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: due,
    )
    deps.mail = fake_mail(sender, _empty_reader())
    deps.last_writing_at = due  # writing isn't due: no Claude in this test

    assert await run_once(deps) == ["Queued 1 follow-up(s) for review (0 held by the gate)."]
    assert await run_once(deps) == []
    assert sender.sent == []
    async with make_session_factory(engine)() as s:
        stored = await s.get(Message, ready.step2_id)
        assert stored is not None and stored.status == "awaiting_approval"


# --------------------------------------------------------------------------- mail


def test_mail_lines() -> None:
    assert mail_lines({}) == []
    assert mail_lines({"a@x.com": PollOutcome(auto_replies=2, unmatched=3)}) == []
    reason = "Paused automatically: 3 of the last 50 emails bounced (6%)."
    assert mail_lines(
        {
            "a@x.com": PollOutcome(replies=2, takeovers=1),
            "b@x.com": PollOutcome(bounces=1, unsubscribes=1, paused=reason),
            "c@x.com": PollOutcome(error="Couldn't read the mailbox: TimeoutError"),
        }
    ) == [
        "Read mail: 2 repl(ies), 1 bounce(s), 1 unsubscribe(s), 1 takeover(s).",
        "Sending paused automatically: 3 of the last 50 emails bounced (6%).",
        "Mail read error for c@x.com: Couldn't read the mailbox: TimeoutError",
    ]


async def test_run_once_says_when_a_mailbox_cant_be_read(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    await seed_ready(db)
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        token=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    deps.mail = fake_mail(FakeSender(), FakeReader({}, fail=TimeoutError("server text")))
    deps.last_writing_at = SEND_NOW

    lines = await run_once(deps)

    assert (
        "Mail read error for sales@send-domain.com: Couldn't read the mailbox: TimeoutError"
        in lines
    )
    assert not any("server text" in line for line in lines)


async def test_run_once_says_which_mailbox_it_wont_read_until_tested(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    from salesfinder.sending.mailboxes import NOT_TESTED

    ready = await seed_ready(db)
    async with make_session_factory(engine)() as s:
        box = await s.get(Mailbox, ready.mailbox_id)
        assert box is not None
        box.enabled, box.hold_reason = False, NOT_TESTED
        await s.commit()
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        token=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    reader = FakeReader({}, fail=AssertionError("the reader must not open"))
    deps.mail = fake_mail(FakeSender(), reader)
    deps.last_writing_at = SEND_NOW

    lines = await run_once(deps)

    assert "Not reading sales@send-domain.com: test its connection first." in lines
    assert not any(line.startswith("Mail read error") for line in lines)
    assert reader.fetches == []


async def test_run_once_reads_mail_before_sending_on_its_own_cadence(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    fake_claude_env.reply(CLASSIFY_MARKER, ok_reply(OBJECTION))
    fake_claude_env.reply(DRAFT_MARKER, ok_reply(ANSWER))
    ready = await seed_ready(db)
    async with make_session_factory(engine)() as s:
        step1 = await s.get(Message, ready.message_id)
        sequence = await s.get(Sequence, ready.sequence_id)
        assert step1 is not None and sequence is not None
        step1.status, step1.sent_at = "sent", SEND_NOW - timedelta(days=1)
        step1.message_id_header = "<first@send-domain.com>"
        step1.mailbox_id = sequence.mailbox_id = ready.mailbox_id
        await s.commit()
    inbox = FakeFolder(1, [(1, reply_to("<first@send-domain.com>", date=SEND_NOW))])
    reader = FakeReader({"INBOX": inbox, "[Gmail]/Sent Mail": FakeFolder(1)})
    sender = FakeSender()
    clock = [SEND_NOW]
    deps = _deps(
        engine,
        sending_settings(test_settings_with_fake_claude),
        key=False,
        token=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: clock[0],
    )
    deps.mail = fake_mail(sender, reader)
    deps.last_writing_at = SEND_NOW  # writing isn't due

    # Mail, then Replies: the reply read here is classified and answered in the same pass
    # (the answer waits in the Outbox, so Sending sends nothing).
    assert await run_once(deps) == [
        "Read mail: 1 repl(ies), 0 bounce(s), 0 unsubscribe(s), 0 takeover(s).",
        "Classified 1 repl(ies); drafted 1 answer(s) for review (0 held by the gate); 0 need you.",
    ]
    assert len(fake_claude_env.calls()) == 2
    async with make_session_factory(engine)() as s:
        assert (await s.get(Sequence, ready.sequence_id)).status == "replied"  # type: ignore[union-attr]
    assert reader.fetches == ["INBOX", "[Gmail]/Sent Mail"]

    # Not due again until mail.poll_interval_minutes have passed.
    inbox.add(reply_to(None, sender="x@unrelated.example", subject="Hi", date=SEND_NOW))
    clock[0] = SEND_NOW + timedelta(minutes=HELPER_CONFIG.mail.poll_interval_minutes - 1)
    assert await run_once(deps) == []
    assert len(reader.fetches) == 2
    clock[0] = SEND_NOW + timedelta(minutes=HELPER_CONFIG.mail.poll_interval_minutes)
    assert await run_once(deps) == []  # the stranger's mail is counted, not reported
    assert len(reader.fetches) == 4
    assert sender.sent == []
    assert len(fake_claude_env.calls()) == 2  # nothing new to classify


# --------------------------------------------------------------------------- Phase 7: the loop

# Quiet 12:00-14:00 New York: SEND_NOW (Tuesday 13:00 EST) falls inside it.
QUIET_NOON = QuietHours(start=time(12, 0), end=time(14, 0), timezone="America/New_York")


def _quiet(config: AppConfig) -> AppConfig:
    usage = config.usage.model_copy(update={"quiet_hours": QUIET_NOON})
    return config.model_copy(update={"usage": usage})


async def _runs(engine: AsyncEngine) -> list[Run]:
    async with make_session_factory(engine)() as s:
        return list((await s.scalars(select(Run).order_by(Run.started_at, Run.id))).all())


def _patch_run_pass(
    monkeypatch: pytest.MonkeyPatch,
    stop: asyncio.Event,
    *,
    passes: int,
    before: Callable[[int, WorkerDeps], Awaitable[None]] | None = None,
) -> list[int]:
    """Wrap the real run_pass: ``before(n, deps)`` runs ahead of pass n; ``stop`` is set after
    pass ``passes``. Returns the list of pass numbers run."""
    import salesfinder.worker as worker_module

    real = worker_module.run_pass
    done: list[int] = []

    async def wrapped(deps: WorkerDeps) -> PassResult:
        n = len(done) + 1
        if before is not None:
            await before(n, deps)
        result = await real(deps)
        done.append(n)
        if n >= passes:
            stop.set()
        return result

    monkeypatch.setattr(worker_module, "run_pass", wrapped)
    return done


async def test_run_pass_collects_failures(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import salesfinder.worker as worker_module

    seen: list[tuple[str | None, datetime | None]] = []

    async def boom(_deps: WorkerDeps) -> list[str]:
        raise RuntimeError("page text from a hostile site")

    async def contacts(deps: WorkerDeps) -> list[str]:
        seen.append((deps.current_step, deps.step_started_at))
        return ["Contacts ran."]

    monkeypatch.setattr(worker_module, "_profiling", boom)
    monkeypatch.setattr(worker_module, "_contacts", contacts)
    deps = _deps(engine, test_settings, token=False, key=False)

    result = await run_pass(deps)

    assert result.failures == ["Profiling step failed: RuntimeError"]
    assert result.lines == ["Profiling step failed: RuntimeError", "Contacts ran."]
    assert result.quiet is False
    assert not any("hostile" in line for line in result.lines)
    assert seen == [("Contacts", NOW)]  # the step being run, for the beat
    assert (deps.current_step, deps.step_started_at) == (None, None)  # between passes


async def test_quiet_hours_skip_claude_steps_only(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings_with_fake_claude: Settings,
    fake_claude_env: FakeClaude,
) -> None:
    """Review focus 2: quiet hours stop only Replies and Writing; mail is read and approved
    email is sent as always."""
    fake_claude_env.reply(CLASSIFY_MARKER, ok_reply(OBJECTION))
    fake_claude_env.reply(DRAFT_MARKER, ok_reply(ANSWER))
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))
    ready = await seed_ready(db)  # resumed, one approved email to Jane
    # A reply waiting to be read: Bob answers our earlier email (another sequence).
    campaign = await db.get(Campaign, ready.campaign_id)
    assert campaign is not None
    earlier = await seed_draft(db, campaign, domain="beta-soap.com", address="bob")
    sequence = await db.get(Sequence, earlier.sequence_id)
    assert sequence is not None
    earlier.status, earlier.sent_at = "sent", SEND_NOW - timedelta(days=1)
    earlier.message_id_header = "<first@send-domain.com>"
    earlier.mailbox_id = sequence.mailbox_id = ready.mailbox_id
    sequence.status = "active"
    # And a company of the active campaign waiting to be drafted for.
    await seed_company(db, name="Gamma Labs", domain="gamma-labs.com", checked_at=SEND_NOW)
    await db.commit()
    inbox = FakeFolder(
        1,
        [
            (
                1,
                reply_to(
                    "<first@send-domain.com>",
                    sender="Bob <bob@beta-soap.com>",
                    date=SEND_NOW,
                ),
            )
        ],
    )
    sender = FakeSender()
    clock = [SEND_NOW]
    deps = _deps(
        engine,
        sending_settings(test_settings_with_fake_claude),
        key=False,
        config=_quiet(HELPER_CONFIG),
        facts=CONFIRMED_FACTS,
        now=lambda: clock[0],
    )
    deps.mail = fake_mail(sender, FakeReader({"INBOX": inbox, SENT_FOLDER: FakeFolder(1)}))
    line = quiet_line(QUIET_NOON)

    first = await run_pass(deps)
    clock[0] = SEND_NOW + timedelta(minutes=1)
    second = await run_pass(deps)

    assert first.quiet and second.quiet
    assert (
        first.lines[0]
        == line
        == "Quiet hours (12:00\u201314:00 America/New_York): Claude steps wait until 14:00."
    )
    assert "Read mail: 1 repl(ies), 0 bounce(s), 0 unsubscribe(s), 0 takeover(s)." in first.lines
    assert "Sent 1 email(s)." in first.lines
    assert line not in second.lines  # once per quiet period
    assert first.failures == second.failures == []
    assert len(sender.sent) == 1
    assert fake_claude_env.calls(None) == []  # not even the isolation-flag check
    assert deps.brain is None and deps.last_writing_at is None

    # Control: at 14:00 (end exclusive) the same reply is classified and Gamma is drafted.
    clock[0] = SEND_NOW + timedelta(hours=1)
    after = await run_pass(deps)
    assert not after.quiet and line not in after.lines
    assert fake_claude_env.calls(), "the waiting work should reach Claude once quiet hours end"
    assert any(text.startswith("Classified 1 repl(ies)") for text in after.lines)
    assert any(text.startswith("Drafted ") for text in after.lines)


async def test_quiet_line_again_after_quiet_ends(
    engine: AsyncEngine, test_settings: Settings
) -> None:
    clock = [datetime(2026, 11, 7, 18, 0, tzinfo=UTC)]  # 13:00 EST: quiet
    deps = _deps(
        engine,
        test_settings,
        token=False,
        key=False,
        config=_quiet(SHIPPED.config),
        now=lambda: clock[0],
    )
    deps.last_writing_at = clock[0] + timedelta(days=2)  # no writing in this test
    line = quiet_line(QUIET_NOON)
    seen: list[str] = []
    quiet: list[bool] = []
    for at in (
        datetime(2026, 11, 7, 18, 0, tzinfo=UTC),  # 13:00 quiet: said
        datetime(2026, 11, 7, 18, 30, tzinfo=UTC),  # 13:30 quiet: not again
        datetime(2026, 11, 7, 19, 0, tzinfo=UTC),  # 14:00 (end, exclusive): over
        datetime(2026, 11, 8, 17, 0, tzinfo=UTC),  # next day 12:00 (start): said again
        datetime(2026, 11, 8, 17, 10, tzinfo=UTC),
    ):
        clock[0] = at
        result = await run_pass(deps)
        seen += result.lines
        quiet.append(result.quiet)
    assert seen.count(line) == 2
    assert quiet == [True, True, False, True, True]


async def test_no_send_never_calls_send_pass(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import salesfinder.worker as worker_module

    ready = await seed_ready(db)  # resumed, with an approved email: it would go out
    sender = FakeSender()
    reader = _empty_reader()
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    deps.mail = fake_mail(sender, reader)
    deps.last_writing_at = SEND_NOW  # no Claude in this test
    deps.send_enabled = False

    async def no_send_pass(*_a: object, **_k: object) -> SendOutcome:
        raise AssertionError("send_pass must never run with --no-send")

    queued: list[datetime] = []
    from salesfinder.followups.service import queue_due as real_queue_due

    async def queue_due(*args: object, **kwargs: object) -> tuple[int, int]:
        queued.append(SEND_NOW)
        return await real_queue_due(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "send_pass", no_send_pass)
    monkeypatch.setattr(worker_module, "queue_due", queue_due)

    first = await run_pass(deps)
    second = await run_pass(deps)

    assert first.lines == [NO_SEND_LINE] == ["Sending skipped: this worker runs with --no-send."]
    assert second.lines == []
    assert first.failures == second.failures == []  # send_pass was never reached
    assert sender.sent == []
    assert reader.fetches == ["INBOX", "[Gmail]/Sent Mail"]  # Mail still ran
    assert len(queued) == 2  # Follow-ups still ran, both passes
    async with make_session_factory(engine)() as s:
        stored = await s.get(Message, ready.message_id)
        assert stored is not None and stored.status == "approved"


async def test_run_forever_records_and_finishes(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    done = _patch_run_pass(monkeypatch, stop, passes=1)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert done == [1]
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason, row.finished_at) == (1, "shutdown", NOW)
    assert row.code_version == deps.code_version
    assert (row.host, row.pid) == (socket.gethostname()[:255], os.getpid())
    assert row.last_pass_at == NOW
    assert deps.run_id is None


async def test_run_forever_closes_crashed_run(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review focus 1: a worker that died without shutting down left its row open; the next
    start closes it as crashed (at its last beat) before opening its own."""
    dead = await runs.start_run(
        db, now=NOW - timedelta(hours=1), host="old-mac", pid=1, code_version="old"
    )
    await db.commit()
    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=1)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    old, new = await _runs(engine)
    assert old.id == dead.id
    assert (old.end_reason, old.finished_at) == ("crashed", NOW - timedelta(hours=1))
    assert old.lines[-1]["text"] == runs.CRASHED_LINE
    assert (new.end_reason, new.passes) == ("shutdown", 1)


async def test_rollover_across_window(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [NOW]

    async def advance(n: int, _deps: WorkerDeps) -> None:
        if n == 2:
            clock[0] = NOW + timedelta(minutes=16)

    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=2, before=advance)
    deps = _deps(
        engine, test_settings, token=False, key=False, poll_seconds=0, now=lambda: clock[0]
    )

    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    first, second = await _runs(engine)
    assert (first.end_reason, first.passes, first.finished_at) == (
        "window",
        2,
        NOW + timedelta(minutes=16),
    )
    assert (second.end_reason, second.started_at) == ("shutdown", NOW + timedelta(minutes=16))


@pytest.mark.parametrize("error", [RuntimeError, KeyError, IndexError])
async def test_record_failure_does_not_stop_worker(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    """Review focus 1: a failed runs write never stops the worker, and only its type is
    logged. A KeyError or IndexError is no "run not open" (I2): no reopen, no second row."""

    async def broken(*_a: object, **_k: object) -> uuid.UUID:
        raise error("server said: secret reply text")

    monkeypatch.setattr("salesfinder.worker.record_pass", broken)
    stop = asyncio.Event()
    done = _patch_run_pass(monkeypatch, stop, passes=2)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    with capture_logs() as logs:
        await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert done == [1, 2]
    failed = [e for e in logs if e["event"] == "run_record_failed"]
    assert [e["error_type"] for e in failed] == [error.__name__, error.__name__]
    assert not any(e["event"] == "run_reopened" for e in logs)
    assert "secret reply text" not in repr(logs)
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason) == (0, "shutdown")  # opened and closed all the same


@pytest.mark.parametrize(("lock", "crash_close"), [(None, False), (True, True), (False, False)])
async def test_start_failure_is_retried_when_recording(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    lock: bool | None,
    crash_close: bool,
) -> None:
    """A runs write failing at start (say the database blinked) doesn't stop the worker;
    the first record opens the run, crash-closing a dead worker's run only while the lock
    check passes (Ruling P7-C)."""
    import salesfinder.worker as worker_module

    real_start = runs.start_run
    attempts: list[bool] = []

    async def flaky(db: AsyncSession, **kwargs: object) -> Run:
        attempts.append(bool(kwargs["close_crashed"]))
        if len(attempts) == 1:
            raise OSError("connection reset")
        return await real_start(db, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "start_run", flaky)
    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=1)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)
    if lock is not None:

        async def lock_alive() -> bool:
            return lock

        deps.lock_alive = lock_alive

    with capture_logs() as logs:
        await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert [e["error_type"] for e in logs if e["event"] == "run_start_failed"] == ["OSError"]
    assert "connection reset" not in repr(logs)
    assert attempts == [True, crash_close]
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason) == (1, "shutdown")


async def test_record_reopens_a_run_closed_under_it(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    """Carry-forward: Postgres restarted, the session lock dropped, and a `worker --once`
    crash-closed this worker's run, ran and finished. The next record opens a new run without
    crash-closing anything and records the pass there."""
    deps = _deps(engine, test_settings, token=False, key=False)
    async with make_session_factory(engine)() as s:
        mine = await runs.start_run(s, now=NOW, host="mac", pid=1, code_version="v")
        await s.commit()
    deps.run_id = mine.id
    async with make_session_factory(engine)() as s:  # the other process
        other = await runs.start_run(s, now=NOW, host="mac", pid=2, code_version="v")
        await runs.finish_run(s, other.id, now=NOW, reason="once")
        await s.commit()

    with capture_logs() as logs:
        await record(deps, PassResult(lines=["Sent 1 email(s)."], failures=[], quiet=False))

    assert [e["event"] for e in logs] == ["run_reopened"]
    rows = {row.id: row for row in await _runs(engine)}
    assert len(rows) == 3
    assert rows[mine.id].end_reason == "crashed"
    assert rows[other.id].end_reason == "once"
    assert deps.run_id is not None and deps.run_id not in (mine.id, other.id)
    new = rows[deps.run_id]
    assert new.finished_at is None and new.passes == 1
    assert [item["text"] for item in new.lines] == ["Sent 1 email(s)."]


async def test_a_failed_reopen_is_retried_without_crash_closing(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Our run was closed under us and the first reopen failed (the database blinked): the
    next record reopens again, still with close_crashed=False."""
    import salesfinder.worker as worker_module

    deps = _deps(engine, test_settings, token=False, key=False)
    async with make_session_factory(engine)() as s:
        mine = await runs.start_run(s, now=NOW, host="mac", pid=1, code_version="v")
        await runs.finish_run(s, mine.id, now=NOW, reason="once")
        await s.commit()
    deps.run_id = mine.id
    modes: list[bool] = []

    async def flaky(db: AsyncSession, **kwargs: object) -> Run:
        modes.append(bool(kwargs["close_crashed"]))
        if len(modes) == 1:
            raise OSError("connection reset")
        return await runs.start_run(db, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "start_run", flaky)
    result = PassResult(lines=[], failures=[], quiet=False)

    await record(deps, result)
    assert deps.run_id == mine.id  # still the lost run: never "no run yet"
    await record(deps, result)

    assert modes == [False, False]
    assert deps.run_id is not None and deps.run_id != mine.id
    rows = {row.id: row for row in await _runs(engine)}
    assert rows[deps.run_id].passes == 1 and rows[mine.id].end_reason == "once"


async def test_run_forever_stops_when_another_worker_took_over(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry-forward: mid-loop, another worker started (this one's lock dropped with a
    Postgres restart) and crash-closed this run. Reopening is refused by its open run
    (uq_runs_one_open): this worker stops, leaving the other's run alone."""
    taken: list[uuid.UUID] = []

    async def other_worker_starts(n: int, _deps: WorkerDeps) -> None:
        if n == 2:
            async with make_session_factory(engine)() as s:
                run = await runs.start_run(s, now=NOW, host="other", pid=2, code_version="v")
                await s.commit()
                taken.append(run.id)

    stop = asyncio.Event()
    done = _patch_run_pass(monkeypatch, stop, passes=99, before=other_worker_starts)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    with pytest.raises(AnotherWorkerRunning):
        await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert done == [1, 2]  # stopped after the pass it couldn't record
    assert deps.run_id is None
    rows = {row.id: row for row in await _runs(engine)}
    assert len(rows) == 2
    other = rows.pop(taken[0])
    [mine] = rows.values()
    assert (other.finished_at, other.end_reason) == (None, None)  # untouched, still open
    assert (mine.end_reason, mine.passes) == ("crashed", 1)


async def test_run_forever_refuses_to_start_beside_another_open_run(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry-forward: a concurrent start won the race (its run opened after this one's
    crash-close looked): IntegrityError on uq_runs_one_open, rolled back, and the worker
    doesn't start."""
    theirs = await runs.start_run(db, now=NOW, host="other", pid=2, code_version="v")
    await db.commit()

    async def raced(_db: AsyncSession) -> None:
        return None  # the crash-close ran before their row existed

    monkeypatch.setattr("salesfinder.ops.runs._close_crashed", raced)
    stop = asyncio.Event()
    done = _patch_run_pass(monkeypatch, stop, passes=1)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    with pytest.raises(AnotherWorkerRunning):
        await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert done == []
    [row] = await _runs(engine)
    assert row.id == theirs.id and row.finished_at is None


async def test_beat_once_updates_step(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    run = await runs.start_run(db, now=NOW, host="mac", pid=1, code_version="v")
    await db.commit()
    later = NOW + timedelta(minutes=1)
    deps = _deps(engine, test_settings, token=False, key=False, now=lambda: later)
    deps.run_id = run.id
    deps.current_step, deps.step_started_at = "Discovery", NOW + timedelta(seconds=30)

    await beat_once(deps)

    [row] = await _runs(engine)
    assert (row.step, row.step_started_at, row.last_beat_at) == (
        "Discovery",
        NOW + timedelta(seconds=30),
        later,
    )


async def test_beat_once_without_a_run_does_nothing(
    engine: AsyncEngine, test_settings: Settings
) -> None:
    deps = _deps(engine, test_settings, token=False, key=False)
    await beat_once(deps)
    assert await _runs(engine) == []


async def test_beat_loop_beats_until_stopped(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    from salesfinder.worker import _beat_loop

    run = await runs.start_run(db, now=NOW, host="mac", pid=1, code_version="v")
    await db.commit()
    later = NOW + timedelta(minutes=2)
    config = SHIPPED.config.model_copy(
        update={"ops": SHIPPED.config.ops.model_copy(update={"beat_seconds": 0})}
    )
    deps = _deps(engine, test_settings, token=False, key=False, config=config, now=lambda: later)
    deps.run_id = run.id
    deps.current_step = "Writing"
    stop = asyncio.Event()

    task = asyncio.create_task(_beat_loop(deps, stop))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if (await _runs(engine))[0].step == "Writing":
            break
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    [row] = await _runs(engine)
    assert (row.step, row.last_beat_at) == ("Writing", later)


async def test_run_single_records_one_pass_and_finishes_once(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    deps = _deps(engine, test_settings, token=False, key=False)

    result = await run_single(deps)

    assert result.failures == []
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason, row.finished_at) == (1, "once", NOW)
    assert deps.run_id is None


# --------------------------------------------------------------------------- fix round 1


def _lock(value: bool, calls: list[bool] | None = None) -> Callable[[], Awaitable[bool]]:
    async def lock_alive() -> bool:
        if calls is not None:
            calls.append(value)
        return value

    return lock_alive


def _beat_every_tick(config: AppConfig | None = None) -> AppConfig:
    config = config or SHIPPED.config
    return config.model_copy(update={"ops": config.ops.model_copy(update={"beat_seconds": 0})})


async def _wait_for(check: Callable[[], Awaitable[bool]], *, seconds: float = 3) -> bool:
    for _ in range(int(seconds / 0.01)):
        if await check():
            return True
        await asyncio.sleep(0.01)
    return False


def _beat_tasks() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t.get_name() == BEAT_TASK and not t.done()]


async def test_beats_continue_while_the_pass_drains(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: SIGTERM mid-pass stops the loop, not the beat: the pass finishes beating (a long
    drain never reads as "stopped responding")."""
    import salesfinder.worker as worker_module

    clock = [NOW]
    later = NOW + timedelta(minutes=3)
    stop = asyncio.Event()
    seen: list[str | None] = []

    async def draining_pass(deps: WorkerDeps) -> PassResult:
        stop.set()  # SIGTERM arrives during the pass
        clock[0] = later
        deps.current_step, deps.step_started_at = "Discovery", later

        async def beaten() -> bool:
            row = (await _runs(engine))[0]
            if row.last_beat_at == later:
                seen.append(row.step)
                return True
            return False

        await _wait_for(beaten)
        deps.current_step = deps.step_started_at = None
        return PassResult(lines=[], failures=[], quiet=False)

    monkeypatch.setattr(worker_module, "run_pass", draining_pass)
    deps = _deps(
        engine,
        test_settings,
        token=False,
        key=False,
        config=_beat_every_tick(),
        now=lambda: clock[0],
    )

    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert seen == ["Discovery"]
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason) == (1, "shutdown")
    assert _beat_tasks() == []


async def test_an_exception_escaping_a_pass_leaves_the_run_open(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1/M8: the worker dies of it; its run stays open (never "shutdown") and the beat is
    stopped, so the run goes stale and the next start closes it as crashed."""
    import salesfinder.worker as worker_module

    async def broken_pass(_deps: WorkerDeps) -> PassResult:
        assert _beat_tasks() != []  # beating during the pass
        raise RuntimeError("bug")

    monkeypatch.setattr(worker_module, "run_pass", broken_pass)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(run_forever(deps, stop=asyncio.Event()), timeout=10)

    assert _beat_tasks() == []
    [row] = await _runs(engine)
    assert (row.finished_at, row.end_reason) == (None, None)
    async with make_session_factory(engine)() as s:
        await runs.start_run(s, now=NOW + timedelta(hours=1), host="h", pid=2, code_version="v")
        await s.commit()
    assert (await _runs(engine))[0].end_reason == "crashed"


async def test_run_single_leaves_the_run_open_when_the_pass_raises(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import salesfinder.worker as worker_module

    async def broken_pass(_deps: WorkerDeps) -> PassResult:
        raise RuntimeError("bug")

    monkeypatch.setattr(worker_module, "run_pass", broken_pass)
    deps = _deps(engine, test_settings, token=False, key=False)

    with pytest.raises(RuntimeError):
        await run_single(deps)

    assert _beat_tasks() == []
    [row] = await _runs(engine)
    assert row.finished_at is None


async def test_run_single_finishes_its_pass_on_a_signal(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4: `--once` installs the same SIGINT/SIGTERM handling as the loop (removed after)."""
    import salesfinder.worker as worker_module

    events: list[str] = []
    real_install = worker_module._install_signal_handlers

    def install(stop: asyncio.Event) -> Callable[[], None]:
        events.append("installed")
        remove = real_install(stop)

        def removed() -> None:
            events.append("removed")
            remove()

        return removed

    async def a_pass(_deps: WorkerDeps) -> PassResult:
        events.append("pass")
        return PassResult(lines=[], failures=[], quiet=False)

    monkeypatch.setattr(worker_module, "_install_signal_handlers", install)
    monkeypatch.setattr(worker_module, "run_pass", a_pass)

    await run_single(_deps(engine, test_settings, token=False, key=False))

    assert events == ["installed", "pass", "removed"]
    [row] = await _runs(engine)
    assert row.end_reason == "once"


async def test_a_rollover_whose_commit_ack_was_lost_is_adopted(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I2b: the rollover committed but the acknowledgement never came back, so the worker
    still points at the finished run. The next record finds its own open row (same host and
    pid) in the way and adopts it instead of stopping."""
    import salesfinder.worker as worker_module

    clock = [NOW]
    deps = _deps(engine, test_settings, token=False, key=False, now=lambda: clock[0])
    await worker_module.start(deps)
    first = deps.run_id
    assert first is not None
    real = worker_module._record_pass

    async def ack_lost(deps: WorkerDeps, run_id: uuid.UUID, result: PassResult) -> uuid.UUID:
        await real(deps, run_id, result)
        raise ConnectionResetError("connection lost after COMMIT")

    monkeypatch.setattr(worker_module, "_record_pass", ack_lost)
    clock[0] = NOW + timedelta(minutes=16)
    empty = PassResult(lines=[], failures=[], quiet=False)
    await record(deps, empty)
    assert deps.run_id == first
    monkeypatch.setattr(worker_module, "_record_pass", real)

    with capture_logs() as logs:
        await record(deps, empty)

    assert [e["event"] for e in logs] == ["run_reopened", "run_adopted"]
    old, new = await _runs(engine)
    assert (old.id, old.end_reason) == (first, "window")
    assert deps.run_id == new.id and new.finished_at is None and new.passes == 1


async def test_lost_lock_stops_the_worker_after_the_pass(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I3 (Ruling P7-C): a beat finds the worker lock gone: the worker stops (never runs
    unlocked), its run closed, ``lock_lost`` set for the CLI's exit 1."""
    stop = asyncio.Event()
    done = _patch_run_pass(monkeypatch, stop, passes=99)
    checks: list[bool] = []
    deps = _deps(
        engine, test_settings, token=False, key=False, poll_seconds=30, config=_beat_every_tick()
    )
    deps.lock_alive = _lock(False, checks)

    with capture_logs() as logs:
        await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert done == [1]  # stopped in its poll wait, long before 30 s
    assert deps.lock_lost is True
    assert checks == [False]  # checked until lost, then no more
    assert any(e["event"] == "worker_lock_lost" for e in logs)
    [row] = await _runs(engine)
    assert row.end_reason == "shutdown"


async def test_beat_loop_checks_the_lock_every_beat(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    from salesfinder.worker import _beat_loop

    run = await runs.start_run(db, now=NOW, host="mac", pid=1, code_version="v")
    await db.commit()
    deps = _deps(engine, test_settings, token=False, key=False, config=_beat_every_tick())
    deps.run_id = run.id
    checks: list[bool] = []
    deps.lock_alive = _lock(True, checks)
    stop, shutdown = asyncio.Event(), asyncio.Event()

    task = asyncio.create_task(_beat_loop(deps, stop, shutdown=shutdown))

    async def checked_twice() -> bool:
        return len(checks) >= 2

    assert await _wait_for(checked_twice)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert not deps.lock_lost and not shutdown.is_set()


async def test_stop_beating_lets_an_in_flight_beat_finish(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: stopping the beat waits for a beat already writing instead of cancelling it."""
    import salesfinder.worker as worker_module

    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_beat(_deps: WorkerDeps) -> None:
        started.set()
        await asyncio.sleep(0.1)
        finished.set()

    monkeypatch.setattr(worker_module, "beat_once", slow_beat)
    deps = _deps(engine, test_settings, token=False, key=False, config=_beat_every_tick())
    beating = worker_module._start_beating(deps, shutdown=None)
    await asyncio.wait_for(started.wait(), timeout=5)

    await worker_module._stop_beating(beating)

    assert finished.is_set()
    assert beating.task.done() and not beating.task.cancelled()


async def test_lock_check_follows_the_session_lock(engine: AsyncEngine) -> None:
    """I3: the CLI's check is True while its session holds the lock, re-takes a free lock,
    and is False when another session holds it or Postgres dropped the session."""
    key = {"k": WORKER_LOCK_KEY}
    async with engine.connect() as mine, engine.connect() as other:
        assert await mine.scalar(text("SELECT pg_try_advisory_lock(:k)"), key)
        await mine.commit()
        alive = lock_check(mine)
        assert await alive()
        await mine.execute(text("SELECT pg_advisory_unlock(:k)"), key)
        await mine.commit()
        assert await alive()  # free: re-taken
        await mine.execute(text("SELECT pg_advisory_unlock(:k)"), key)
        await mine.commit()
        assert await other.scalar(text("SELECT pg_try_advisory_lock(:k)"), key)
        await other.commit()
        assert not await alive()  # another worker has it
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), key)
        await other.commit()
        assert await alive()
        # Postgres restarted under it: the session, and its lock, are gone.
        pid = await mine.scalar(text("SELECT pg_backend_pid()"))
        await mine.commit()
        await other.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        await other.commit()
        with capture_logs() as logs:
            assert not await alive()
        assert [e["event"] for e in logs] == ["worker_lock_check_failed"]
        assert await other.scalar(text("SELECT pg_try_advisory_lock(:k)"), key)  # it's free
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), key)
        await other.commit()


async def test_quiet_line_for_each_quiet_period(
    engine: AsyncEngine, test_settings: Settings
) -> None:
    """M5: two quiet periods with no pass between them (the Mac slept) each get the line."""
    clock = [datetime(2026, 11, 7, 18, 0, tzinfo=UTC)]  # 13:00 EST
    deps = _deps(
        engine,
        test_settings,
        token=False,
        key=False,
        config=_quiet(SHIPPED.config),
        now=lambda: clock[0],
    )
    deps.last_writing_at = clock[0] + timedelta(days=2)
    first = await run_pass(deps)
    clock[0] = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)  # next day 13:00, still quiet
    second = await run_pass(deps)
    line = quiet_line(QUIET_NOON)
    assert line in first.lines and line in second.lines


async def test_no_send_line_once_per_run_row(engine: AsyncEngine, test_settings: Settings) -> None:
    """M6: each runs row says why nothing is sent (said again after a rollover)."""
    deps = _deps(engine, test_settings, token=False, key=False)
    deps.send_enabled = False
    deps.last_writing_at = NOW + timedelta(days=1)
    deps.run_id = uuid.uuid4()
    assert (await run_pass(deps)).lines == [NO_SEND_LINE]
    assert (await run_pass(deps)).lines == []
    deps.run_id = uuid.uuid4()  # rolled over
    assert (await run_pass(deps)).lines == [NO_SEND_LINE]
    assert (await run_pass(deps)).lines == []


async def test_pause_line_once_per_run_row(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    """Task 6 fix: with sending paused and approved mail waiting, the pause line is said once
    per runs row (again after a rollover, and again for a new pause reason), not every pass."""
    await seed_ready(db, resume=False)  # an approved email waits; sending never turned on
    sender = FakeSender()
    deps = _deps(
        engine,
        sending_settings(test_settings),
        key=False,
        token=False,
        config=HELPER_CONFIG,
        facts=CONFIRMED_FACTS,
        now=lambda: SEND_NOW,
    )
    deps.mail = fake_mail(sender, _empty_reader())
    deps.last_writing_at = SEND_NOW + timedelta(days=1)  # no Claude in this test
    deps.run_id = uuid.uuid4()

    assert (await run_pass(deps)).lines == [NOT_TURNED_ON]
    assert (await run_pass(deps)).lines == []
    deps.run_id = uuid.uuid4()  # rolled over
    assert (await run_pass(deps)).lines == [NOT_TURNED_ON]
    assert (await run_pass(deps)).lines == []
    async with make_session_factory(engine)() as s:
        await state.pause(s, reason="Checking the copy", actor=None, now=SEND_NOW)
        await s.commit()
    assert (await run_pass(deps)).lines == ["Sending is paused: Checking the copy"]
    assert (await run_pass(deps)).lines == []
    assert sender.sent == []


async def test_a_stopping_worker_starts_no_claude_work_and_no_discovery_job(
    engine: AsyncEngine,
    db: AsyncSession,
    admin: User,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling P7-G: SIGTERM mid-pass (here during the Sweep) lets the pass finish, but Replies,
    Writing and Discovery start nothing new: no Claude call, the approved job isn't claimed
    (it waits for the next start), so launchd never has to SIGKILL a long pass."""
    import salesfinder.worker as worker_module

    job = await _job(db, admin, approve=True)
    stop = asyncio.Event()
    real_sweep = worker_module._sweep
    asked: list[str] = []

    async def sweep_then_sigterm(deps: WorkerDeps) -> list[str]:
        lines = await real_sweep(deps)
        stop.set()
        return lines

    async def never(*_a: object, **_k: object) -> object:
        asked.append("claude step")
        raise AssertionError("a stopping worker started Claude work")

    monkeypatch.setattr(worker_module, "_sweep", sweep_then_sigterm)
    monkeypatch.setattr(worker_module, "handle_replies", never)
    monkeypatch.setattr(worker_module, "draft_batch", never)
    serper = _serper()
    deps = _deps(engine, test_settings, token=False, serper=serper)

    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)

    assert deps.stopping is stop
    assert asked == []
    assert (await _status(engine, job.id)).status == "approved"
    assert serper.calls == []
    [row] = await _runs(engine)
    assert (row.passes, row.end_reason, row.failed_steps) == (1, "shutdown", 0)
    assert not row.quiet  # the Claude steps would have run: only the stop held them back


async def test_run_single_stops_starting_claude_work_on_a_signal(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--once` shares the stop event with the steps (deps.stopping)."""
    import salesfinder.worker as worker_module

    seen: list[bool] = []

    async def pass_seeing_stop(deps: WorkerDeps) -> PassResult:
        assert deps.stopping is not None
        deps.stopping.set()
        seen.append(worker_module._stopping(deps))
        return PassResult(lines=[], failures=[], quiet=False)

    monkeypatch.setattr(worker_module, "run_pass", pass_seeing_stop)
    deps = _deps(engine, test_settings, token=False, key=False)
    await asyncio.wait_for(worker_module.run_single(deps), timeout=10)
    assert seen == [True]
    assert await worker_module._replies(deps) == []
    assert await worker_module._writing(deps) == []
    assert await worker_module._discovery(deps) == []


# --------------------------------------------------------------------------- final fix wave


async def test_lock_checks_never_overlap_on_the_lock_connection() -> None:
    """Final wave T2: concurrent checks share one connection; they run one at a time (two
    overlapping queries on one connection would fail and read as a lost lock)."""
    active = [0]
    peak = [0]

    class Conn:
        async def scalar(self, *_a: object, **_k: object) -> bool:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0.01)
            active[0] -= 1
            return True

        async def commit(self) -> None:
            await asyncio.sleep(0)

    alive = lock_check(Conn())  # type: ignore[arg-type]
    assert await asyncio.gather(alive(), alive(), alive()) == [True, True, True]
    assert peak[0] == 1


async def test_lock_check_that_truly_hangs_counts_as_lost(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final wave T2: a check stuck on its connection (it never answers) times out as lost."""
    never = asyncio.Event()

    async def hang(*_a: object, **_k: object) -> object:
        await never.wait()
        raise AssertionError("unreachable")

    async with engine.connect() as conn:
        # AsyncConnection has slots: patch its class, only while this check runs.
        with monkeypatch.context() as patch, capture_logs() as logs:
            patch.setattr(AsyncConnection, "scalar", hang)
            assert not await lock_check(conn, timeout=0.05)()
    assert [(e["event"], e["error_type"]) for e in logs] == [
        ("worker_lock_check_failed", "TimeoutError")
    ]


async def test_an_open_row_from_before_this_process_is_not_adopted(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings
) -> None:
    """Final wave T2: after a reboot the same pid can come back; an open row with our host and
    pid that started before this process did is someone else's (never adopted)."""
    import salesfinder.worker as worker_module

    host, pid = socket.gethostname()[:255], os.getpid()
    db.add(Run(started_at=NOW - timedelta(hours=1), last_beat_at=NOW - timedelta(hours=1),
               host=host, pid=pid, code_version="x"))  # fmt: skip
    await db.commit()
    deps = _deps(engine, test_settings, token=False, key=False)
    assert deps.process_started_at == NOW
    with pytest.raises(AnotherWorkerRunning):
        await worker_module._open_run(deps, close_crashed=False)

    await db.execute(text("DELETE FROM runs"))
    db.add(Run(started_at=NOW, last_beat_at=NOW, host=host, pid=pid, code_version="x"))
    await db.commit()
    own = await worker_module._open_run(deps, close_crashed=False)
    assert own == deps.run_id and own is not None


async def test_a_refusing_sender_returns_a_retry() -> None:
    """Final wave T2: the --no-send sender never raises, so a send that somehow reaches it
    puts the message back to approved (never stuck in ``sending``)."""
    from email.message import EmailMessage

    from salesfinder.mail.factory import RefusingSender

    result = await RefusingSender().send(
        None,  # type: ignore[arg-type]
        EmailMessage(),
        envelope_from="a@send.example.org",
        envelope_to="b@example.com",
    )
    assert result.outcome == "retry"
    assert result.detail == "This worker runs with --no-send."


async def test_a_lost_lock_is_recorded_in_the_run(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final wave T3: the row a lock-lost worker closes ends with LOCK_LOST, so health reads
    "lost its database lock"."""
    from salesfinder.ops import health
    from salesfinder.worker import LOCK_LOST

    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=99)
    deps = _deps(
        engine, test_settings, token=False, key=False, poll_seconds=30, config=_beat_every_tick()
    )
    deps.lock_alive = _lock(False)
    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)
    [row] = await _runs(engine)
    assert row.end_reason == "shutdown" and row.lines[-1]["text"] == LOCK_LOST
    async with make_session_factory(engine)() as s:
        h = await health.worker_health(
            s, now=NOW, config=SHIPPED.config, code_version=deps.code_version
        )
    assert h.problem is not None and "lost its database lock" in h.problem


async def test_a_clean_stop_adds_no_lock_line(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from salesfinder.worker import LOCK_LOST

    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=1)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)
    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)
    [row] = await _runs(engine)
    assert row.end_reason == "shutdown"
    assert LOCK_LOST not in [line["text"] for line in row.lines]


def _send_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[SendOutcome]
) -> list[SendOutcome]:
    """The Sending step's send_pass answers ``outcomes`` in turn."""
    import salesfinder.worker as worker_module

    left = list(outcomes)

    async def send_pass(*_a: object, **_k: object) -> SendOutcome:
        return left.pop(0)

    monkeypatch.setattr(worker_module, "send_pass", send_pass)
    return left


def _sending_only(deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step but Sending does nothing."""
    import salesfinder.worker as worker_module

    async def nothing(_deps: WorkerDeps) -> list[str]:
        return []

    for name in ("_reap", "_sweep", "_hubspot", "_mail", "_replies", "_followups",
                 "_discovery", "_profiling", "_contacts", "_writing"):  # fmt: skip
        monkeypatch.setattr(worker_module, name, nothing)
    deps.mail = fake_mail(FakeSender(), _empty_reader())


async def test_held_lines_once_per_run_row_and_reason(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final wave T6: a mailbox's "Held: … — <reason>" is said once per runs row and reason,
    like the pause line: again after a rollover, for a new reason, or after the hold lifted."""
    deps = _deps(engine, test_settings, token=False, key=False)
    _sending_only(deps, monkeypatch)
    a, b = "a@send.example.org", "b@send.example.org"
    _send_outcomes(
        monkeypatch,
        [
            SendOutcome(held={a: "Daily cap reached.", b: "Turned off."}),
            SendOutcome(held={a: "Daily cap reached.", b: "Turned off."}),
            SendOutcome(held={a: "Daily cap reached.", b: "Untested."}),
            SendOutcome(held={a: "Daily cap reached."}),  # rolled over before this pass
            SendOutcome(held={}),
            SendOutcome(held={a: "Daily cap reached."}),
        ],
    )
    deps.run_id = uuid.uuid4()
    assert (await run_pass(deps)).lines == [
        f"Held: {a} — Daily cap reached.",
        f"Held: {b} — Turned off.",
    ]
    assert (await run_pass(deps)).lines == []
    assert (await run_pass(deps)).lines == [f"Held: {b} — Untested."]
    deps.run_id = uuid.uuid4()
    assert (await run_pass(deps)).lines == [f"Held: {a} — Daily cap reached."]
    assert (await run_pass(deps)).lines == []  # the hold lifted
    assert (await run_pass(deps)).lines == [f"Held: {a} — Daily cap reached."]


async def test_pause_line_again_after_the_pause_lifts(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final wave T6: the pause_noted reset path: with no mail waiting (or sending resumed)
    the pause isn't said; when the same pause holds mail again, it is said again."""
    deps = _deps(engine, test_settings, token=False, key=False)
    _sending_only(deps, monkeypatch)
    _send_outcomes(
        monkeypatch,
        [
            SendOutcome(paused="Checking the copy"),
            SendOutcome(paused="Checking the copy"),
            SendOutcome(),  # nothing waiting
            SendOutcome(paused="Checking the copy"),
        ],
    )
    deps.run_id = uuid.uuid4()
    line = "Sending is paused: Checking the copy"
    assert (await run_pass(deps)).lines == [line]
    assert (await run_pass(deps)).lines == []
    assert (await run_pass(deps)).lines == []
    assert (await run_pass(deps)).lines == [line]


async def test_no_duplicate_lines_in_the_first_row_after_a_failed_start(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final wave T6: the start's runs write failed, so the first pass ran with no row and the
    first record opened one: lines said then belong to that row, never said twice in it."""
    import salesfinder.worker as worker_module

    real_start = runs.start_run
    attempts: list[int] = []

    async def flaky(db: AsyncSession, **kwargs: object) -> Run:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("connection reset")
        return await real_start(db, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "start_run", flaky)
    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=3)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)
    _sending_only(deps, monkeypatch)
    deps.send_enabled = False
    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)
    [row] = await _runs(engine)
    assert [line["text"] for line in row.lines] == [NO_SEND_LINE]

    # The same for the pause line and a mailbox hold.
    await db.execute(text("DELETE FROM runs"))
    await db.commit()
    attempts.clear()
    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=3)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)
    _sending_only(deps, monkeypatch)
    _send_outcomes(
        monkeypatch, [SendOutcome(paused="Checking", held={"a@x.org": "Off."}) for _ in range(3)]
    )
    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)
    [row] = await _runs(engine)
    assert [line["text"] for line in row.lines] == [
        "Sending is paused: Checking",
        "Held: a@x.org — Off.",
    ]


async def test_a_no_send_worker_starts_each_new_row_with_its_line(
    engine: AsyncEngine, db: AsyncSession, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M1: the rollover row starts with NO_SEND_LINE (health reads the open row),
    and the next pass doesn't say it again there."""
    clock = [NOW]
    deps = _deps(engine, test_settings, token=False, key=False, now=lambda: clock[0])
    _sending_only(deps, monkeypatch)
    deps.send_enabled = False
    await worker_module_start(deps)
    await record(deps, await run_pass(deps))
    clock[0] = NOW + timedelta(minutes=16)
    await record(deps, await run_pass(deps))  # rolls over
    _, new = await _runs(engine)
    assert (new.passes, [line["text"] for line in new.lines]) == (0, [NO_SEND_LINE])
    clock[0] = NOW + timedelta(minutes=17)
    await record(deps, await run_pass(deps))
    old, new = await _runs(engine)
    assert [line["text"] for line in old.lines] == [NO_SEND_LINE]
    assert [line["text"] for line in new.lines] == [NO_SEND_LINE]
    assert new.passes == 1


async def worker_module_start(deps: WorkerDeps) -> None:
    import salesfinder.worker as worker_module

    await worker_module.start(deps)


async def test_a_stopping_worker_starts_no_batch_steps(
    engine: AsyncEngine, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final wave T7: once stopping, Profiling, Contacts and HubSpot return at once (they are
    due-based and resume at the next start), so the pass ends within launchd's ExitTimeOut."""
    import salesfinder.worker as worker_module

    called: list[str] = []

    def never(name: str) -> Callable[..., Awaitable[object]]:
        async def fail(*_a: object, **_k: object) -> object:
            called.append(name)
            raise AssertionError(f"a stopping worker started {name}")

        return fail

    for name in ("profile_batch", "verify_batch", "run_sync", "due_for_sync"):
        monkeypatch.setattr(worker_module, name, never(name))
    deps = _deps(engine, test_settings, token=True, key=False)
    deps.stopping = asyncio.Event()
    deps.stopping.set()
    for step in (worker_module._profiling, worker_module._contacts, worker_module._hubspot):
        assert await step(deps) == []
    assert called == []


async def test_once_per_row_lines_are_said_again_after_a_failed_record(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final wave T6: a pass whose record failed lost its lines, so the next pass says the
    once-per-row lines again (the row still gets them once)."""
    import salesfinder.worker as worker_module

    real = runs.record_pass
    calls: list[int] = []

    async def flaky(*args: object, **kwargs: object) -> uuid.UUID:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("connection reset")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_module, "record_pass", flaky)
    stop = asyncio.Event()
    _patch_run_pass(monkeypatch, stop, passes=3)
    deps = _deps(engine, test_settings, token=False, key=False, poll_seconds=0)
    _sending_only(deps, monkeypatch)
    deps.send_enabled = False
    _send_outcomes(monkeypatch, [])
    await asyncio.wait_for(run_forever(deps, stop=stop), timeout=10)
    [row] = await _runs(engine)
    assert row.passes == 2
    assert [line["text"] for line in row.lines] == [NO_SEND_LINE]
