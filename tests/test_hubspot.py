"""HubSpot read-only sync: client, sync job, scheduling and health. No network: FakeHubSpot."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fakes.hubspot import FAKE_TOKEN, FakeHubSpot, company, contact, owner
from salesfinder.config import HubSpotConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import HubSpotSync, KnownRelationship, User
from salesfinder.exclusions import check, hubspot, sync

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
CONFIG = HubSpotConfig(sync_interval_hours=24, stale_after_hours=48)


class Sleeper:
    """Records requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _now() -> datetime:
    return NOW


def _companies() -> list[dict[str, Any]]:
    return [
        company("101", "Acme Chemicals", domain="ACME.com", owner="7"),
        company("102", "Beta Industries", website="https://www.beta.co.in/", stage="lead"),
        company("103", "Gamma Labs", domain="gamma.com", additional="gamma.com;gamma-labs.com"),
    ]


def _contacts() -> list[dict[str, Any]]:
    return [
        contact("201", "Ravi@ACME.com", first="Ravi", last="Kumar", owner="7"),
        contact("202", "jane.doe@Beta.co.in", first="Jane"),
        contact("203", "Ops@Gamma.com"),
    ]


def _owners() -> list[dict[str, Any]]:
    return [owner("7", first="Priya", last="Shah", email="priya@elchemy.com")]


async def _run(
    engine: AsyncEngine,
    fake: FakeHubSpot,
    *,
    token: str = FAKE_TOKEN,
    sleeper: Callable[[float], Any] | None = None,
) -> HubSpotSync:
    result = await sync.run_sync(
        make_session_factory(engine),
        token=token,
        now=_now,
        transport=fake.transport,
        sleep=sleeper or Sleeper(),
    )
    fake.assert_read_only()
    return result


async def _rows(engine: AsyncEngine) -> list[KnownRelationship]:
    async with make_session_factory(engine)() as s:
        return list(await s.scalars(select(KnownRelationship).order_by(KnownRelationship.value)))


def _values(rows: Sequence[KnownRelationship]) -> set[tuple[str, str]]:
    return {(r.kind, r.value) for r in rows}


EXPECTED = {
    ("domain", "acme.com"),
    ("domain", "beta.co.in"),
    ("domain", "gamma.com"),
    ("domain", "gamma-labs.com"),
    ("email", "ravi@acme.com"),
    ("email", "jane.doe@beta.co.in"),
    ("email", "ops@gamma.com"),
}


async def test_sync_upserts_and_resolves_owner(engine: AsyncEngine, db: AsyncSession) -> None:
    fake = FakeHubSpot(_companies(), _contacts(), _owners())
    result = await _run(engine, fake)

    assert result.status == "succeeded"
    assert (result.companies_count, result.contacts_count) == (3, 3)
    assert result.started_at == NOW and result.finished_at == NOW
    assert result.error is None

    rows = await _rows(engine)
    assert _values(rows) == EXPECTED
    by_value = {r.value: r for r in rows}
    assert by_value["acme.com"].owner_name == "Priya Shah"
    assert by_value["ravi@acme.com"].owner_name == "Priya Shah"
    assert by_value["beta.co.in"].owner_name is None
    assert by_value["beta.co.in"].name == "Beta Industries"
    assert by_value["beta.co.in"].lifecycle_stage == "lead"
    assert by_value["gamma-labs.com"].hubspot_object == "company"
    assert by_value["gamma-labs.com"].hubspot_id == "103"
    assert by_value["ravi@acme.com"].name == "Ravi Kumar"
    assert by_value["ravi@acme.com"].hubspot_object == "contact"
    assert by_value["ops@gamma.com"].name is None
    assert all(r.source == "hubspot" and r.synced_at == NOW for r in rows)
    for r in rows:
        assert FAKE_TOKEN not in repr(vars(r))


async def test_paging_followed(engine: AsyncEngine, db: AsyncSession) -> None:
    companies = [company(str(i), f"Co {i}", domain=f"co{i}.com") for i in range(5)]
    fake = FakeHubSpot(companies, [], [])
    result = await _run(engine, fake)

    assert result.status == "succeeded" and result.companies_count == 5
    assert {r.value for r in await _rows(engine)} == {f"co{i}.com" for i in range(5)}
    company_calls = fake.calls_to("companies")
    assert len(company_calls) == 3
    assert [c.url.params.get("after") for c in company_calls] == [None, "2", "4"]


async def test_resync_removes_deleted(engine: AsyncEngine, db: AsyncSession) -> None:
    await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))
    first = {r.value: r.id for r in await _rows(engine)}

    renamed = [company("101", "Acme Renamed", domain="acme.com", owner="7"), _companies()[2]]
    second = await _run(engine, FakeHubSpot(renamed, _contacts()[:2], _owners()))
    assert second.status == "succeeded"
    assert (second.companies_count, second.contacts_count) == (2, 2)

    rows = await _rows(engine)
    assert _values(rows) == EXPECTED - {("email", "ops@gamma.com")}
    by_value = {r.value: r for r in rows}
    assert by_value["acme.com"].name == "Acme Renamed"
    assert by_value["acme.com"].id == first["acme.com"]  # updated in place, not re-inserted
    # The Beta company is gone, but its contact still keeps beta.co.in excluded.
    assert by_value["beta.co.in"].hubspot_object == "contact"
    assert by_value["beta.co.in"].hubspot_id == "202"


async def test_duplicate_values_last_one_wins(engine: AsyncEngine, db: AsyncSession) -> None:
    companies = [
        company("1", "First", domain="dup.com"),
        company("2", "Second", website="http://www.DUP.com/about"),
        company("3", "Junk", domain="localhost", additional=" ; not a domain;"),
    ]
    contacts = [contact("8", "a@dup.com"), contact("9", "A@Dup.com"), contact("10", "nope")]
    result = await _run(engine, FakeHubSpot(companies, contacts, []))

    assert result.status == "succeeded"
    assert (result.companies_count, result.contacts_count) == (3, 3)
    rows = await _rows(engine)
    assert _values(rows) == {("domain", "dup.com"), ("email", "a@dup.com")}
    by_value = {r.value: r for r in rows}
    assert (by_value["dup.com"].hubspot_id, by_value["dup.com"].name) == ("2", "Second")
    assert by_value["a@dup.com"].hubspot_id == "9"


async def test_large_portal_is_batched(engine: AsyncEngine, db: AsyncSession) -> None:
    contacts = [contact(str(i), f"person{i}@bulk.com") for i in range(sync.UPSERT_BATCH * 2 + 7)]
    fake = FakeHubSpot([], contacts, [], page_size=100)
    result = await _run(engine, fake)

    assert result.status == "succeeded" and result.contacts_count == len(contacts)
    async with make_session_factory(engine)() as s:
        count = await s.scalar(select(func.count()).select_from(KnownRelationship))
    assert count == len(contacts) + 1  # every email, plus the one domain they share: bulk.com


async def test_failure_keeps_existing_rows(engine: AsyncEngine, db: AsyncSession) -> None:
    await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))
    before = {(r.id, r.value, r.name) for r in await _rows(engine)}

    sleeper = Sleeper()
    fake = FakeHubSpot([_companies()[0]], [], _owners(), fail={"contacts": 500})
    result = await _run(engine, fake, sleeper=sleeper)

    assert result.status == "failed"
    assert result.error is not None
    assert "HubSpot unavailable after 3 retries (HTTP 500)" in result.error
    assert result.finished_at == NOW
    assert len(fake.calls_to("contacts")) == 4  # first try + 3 retries
    assert sleeper.delays == [1, 2, 4]  # no Retry-After on a 500: 2^attempt
    assert {(r.id, r.value, r.name) for r in await _rows(engine)} == before


async def test_db_failure_mid_replace_rolls_back(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))
    before = {(r.id, r.value, r.name, r.updated_at) for r in await _rows(engine)}

    async def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sync, "_delete_unseen", boom)
    renamed = [company("101", "Acme Renamed", domain="acme.com")]
    result = await _run(engine, FakeHubSpot(renamed, [], []))

    assert result.status == "failed"
    assert result.error is not None and "RuntimeError" in result.error
    assert {(r.id, r.value, r.name, r.updated_at) for r in await _rows(engine)} == before
    async with make_session_factory(engine)() as s:
        statuses = list(await s.scalars(select(HubSpotSync.status).order_by(HubSpotSync.status)))
    assert statuses == ["failed", "succeeded"]


async def test_401_message_has_no_token(engine: AsyncEngine, db: AsyncSession) -> None:
    wrong = "pat-na1-wrong-token-should-never-leak"
    fake = FakeHubSpot(_companies(), _contacts(), _owners())
    result = await _run(engine, fake, token=wrong)

    assert result.status == "failed"
    assert result.error == "HubSpot rejected the token (401). Check SALESFINDER_HUBSPOT_TOKEN."
    assert fake.calls[0].headers["authorization"] == f"Bearer {wrong}"
    async with make_session_factory(engine)() as s:
        stored = (await s.get(HubSpotSync, result.id)) or pytest.fail("sync row missing")
        assert wrong not in repr(vars(stored))
    assert await _rows(engine) == []


async def test_429_retry_then_success(engine: AsyncEngine, db: AsyncSession) -> None:
    sleeper = Sleeper()
    fake = FakeHubSpot(
        _companies(), _contacts(), _owners(), fail={"companies": 429}, fail_times={"companies": 1}
    )
    result = await _run(engine, fake, sleeper=sleeper)

    assert result.status == "succeeded"
    assert sleeper.delays == [1.0]  # honoured Retry-After: 1
    assert len(fake.calls_to("companies")) == 3  # 429, then pages 1 and 2


async def test_owner_scope_missing_is_ok(engine: AsyncEngine, db: AsyncSession) -> None:
    fake = FakeHubSpot(_companies(), _contacts(), _owners(), fail={"owners": 403})
    result = await _run(engine, fake)

    assert result.status == "succeeded"
    assert all(r.owner_name is None for r in await _rows(engine))


async def test_run_sync_claims_queued_request(
    engine: AsyncEngine, db: AsyncSession, admin: User
) -> None:
    queued = await sync.request_sync(db, requested_by=admin)
    await db.commit()
    result = await _run(engine, FakeHubSpot(_companies(), [], []))

    assert result.id == queued.id and result.requested_by_id == admin.id
    assert result.status == "succeeded"
    async with make_session_factory(engine)() as s:
        assert await s.scalar(select(func.count()).select_from(HubSpotSync)) == 1


async def test_request_sync_dedupes(db: AsyncSession, admin: User) -> None:
    first = await sync.request_sync(db, requested_by=admin)
    second = await sync.request_sync(db, requested_by=None)
    assert first.id == second.id and first.status == "queued"
    assert first.requested_by_id == admin.id
    assert await db.scalar(select(func.count()).select_from(HubSpotSync)) == 1

    first.status = "running"
    await db.flush()
    assert (await sync.request_sync(db, requested_by=None)).id == first.id

    first.status = "succeeded"
    await db.flush()
    third = await sync.request_sync(db, requested_by=None)
    assert third.id != first.id and third.status == "queued"


def _finished(status: str, hours_ago: float, error: str | None = None) -> HubSpotSync:
    at = NOW - timedelta(hours=hours_ago)
    return HubSpotSync(status=status, started_at=at, finished_at=at, error=error)


async def _health(db: AsyncSession, *, token_configured: bool = True) -> sync.SyncHealth:
    return await sync.sync_health(db, now=NOW, token_configured=token_configured, config=CONFIG)


async def test_due_for_sync_and_health(db: AsyncSession) -> None:
    assert await sync.due_for_sync(db, now=NOW, config=CONFIG) is True
    health = await _health(db)
    assert (health.state, health.last_success_at) == ("never_synced", None)

    recent = _finished("succeeded", 2)
    db.add(recent)
    await db.flush()
    assert await sync.due_for_sync(db, now=NOW, config=CONFIG) is False
    health = await _health(db)
    assert health.state == "fresh" and health.last_success_at == recent.finished_at

    queued = HubSpotSync(status="queued")
    db.add(queued)
    await db.flush()
    assert await sync.due_for_sync(db, now=NOW, config=CONFIG) is True  # someone asked
    queued.status = "running"
    await db.flush()
    assert await sync.due_for_sync(db, now=NOW, config=CONFIG) is False  # already running
    await db.delete(queued)
    await db.delete(recent)
    await db.flush()

    db.add(_finished("succeeded", 30))
    await db.flush()
    assert await sync.due_for_sync(db, now=NOW, config=CONFIG) is True
    assert (await _health(db)).state == "fresh"

    old = _finished("succeeded", 50)
    for row in await db.scalars(select(HubSpotSync)):
        await db.delete(row)
    db.add(old)
    await db.flush()
    health = await _health(db)
    assert health.state == "stale" and health.last_success_at == old.finished_at
    assert "48" in health.message

    db.add(_finished("failed", 1, error="HubSpot rejected the token (401)."))
    await db.flush()
    health = await _health(db)
    assert health.state == "failing"
    assert "HubSpot rejected the token (401)." in health.message
    assert health.last_success_at == old.finished_at

    health = await _health(db, token_configured=False)
    assert health.state == "not_configured"


async def test_recent_success_stays_fresh_despite_later_failure(db: AsyncSession) -> None:
    db.add(_finished("succeeded", 3))
    db.add(_finished("failed", 1, error="Could not reach HubSpot: ConnectError."))
    await db.flush()
    assert (await _health(db)).state == "fresh"


async def test_never_succeeded_but_failed_is_failing(db: AsyncSession) -> None:
    db.add(_finished("failed", 1, error="nope"))
    await db.flush()
    health = await _health(db)
    assert health.state == "failing" and health.last_success_at is None


# --- client-level error mapping -------------------------------------------------------------


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> hubspot.HubSpotClient:
    return hubspot.HubSpotClient(
        FAKE_TOKEN, transport=httpx.MockTransport(handler), sleep=Sleeper()
    )


async def _first_company(client: hubspot.HubSpotClient) -> None:
    async with client:
        async for _ in client.iter_companies():
            break


async def test_client_403_on_objects_names_the_scope() -> None:
    client = _client(lambda r: httpx.Response(403, json={"category": "MISSING_SCOPES"}))
    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(client)
    assert info.value.reason == (
        "HubSpot token lacks a required scope (403): "
        "crm.objects.companies.read / crm.objects.contacts.read."
    )


async def test_client_timeout_and_connection_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(_client(timeout))
    assert info.value.reason == "Could not reach HubSpot: ReadTimeout."

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(_client(refused))
    assert info.value.reason == "Could not reach HubSpot: ConnectError."
    assert FAKE_TOKEN not in str(info.value) and info.value.__cause__ is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"results": "nope"}),
    ],
)
async def test_client_unexpected_response(response: httpx.Response) -> None:
    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(_client(lambda r: response))
    assert info.value.reason == "HubSpot returned an unexpected response."


async def test_client_429_without_retry_after_backs_off_exponentially() -> None:
    sleeper = Sleeper()
    client = hubspot.HubSpotClient(
        FAKE_TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(429)), sleep=sleeper
    )
    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(client)
    assert info.value.reason == "HubSpot unavailable after 3 retries (HTTP 429)."
    assert sleeper.delays == [1, 2, 4]


async def test_client_owners_names_and_fallback() -> None:
    fake = FakeHubSpot(
        [],
        [],
        [
            owner("1", first="Priya", last="Shah", email="p@elchemy.com"),
            owner("2", email="solo@elchemy.com"),
            owner("3", first="Ana"),
            owner("4"),
        ],
    )
    async with hubspot.HubSpotClient(FAKE_TOKEN, transport=fake.transport) as client:
        names = await client.owners()
    assert names == {"1": "Priya Shah", "2": "solo@elchemy.com", "3": "Ana"}
    assert [c.url.params.get("after") for c in fake.calls_to("owners")] == [None, "2"]
    fake.assert_read_only()


def test_fake_rejects_writes() -> None:
    fake = FakeHubSpot([], [], [])
    with httpx.Client(transport=fake.transport) as c, pytest.raises(AssertionError):
        c.post("https://api.hubapi.com/crm/v3/objects/companies", json={})
    assert fake.violations


async def test_empty_portal_does_not_wipe_existing_rows(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))
    before = _values(await _rows(engine))

    result = await _run(engine, FakeHubSpot([], [contact("1", "not-an-email")], []))
    assert result.status == "failed"
    assert result.error is not None and "kept the existing 7 rows" in result.error
    assert _values(await _rows(engine)) == before


async def test_empty_portal_on_first_sync_succeeds(engine: AsyncEngine, db: AsyncSession) -> None:
    result = await _run(engine, FakeHubSpot([], [], []))
    assert (result.status, result.companies_count, result.contacts_count) == ("succeeded", 0, 0)


async def test_error_after_commit_cannot_flip_success(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingLog:
        def info(self, event: str, **kw: object) -> None:
            if event == "hubspot_sync_succeeded":
                raise OSError("stderr closed")

        def warning(self, event: str, **kw: object) -> None:
            raise AssertionError("must not be treated as a failure")

    monkeypatch.setattr(sync, "log", ExplodingLog())
    with pytest.raises(OSError, match="stderr closed"):
        await sync.run_sync(
            make_session_factory(engine),
            token=FAKE_TOKEN,
            now=_now,
            transport=FakeHubSpot(_companies(), [], []).transport,
            sleep=Sleeper(),
        )
    async with make_session_factory(engine)() as s:
        assert list(await s.scalars(select(HubSpotSync.status))) == ["succeeded"]
    assert len(await _rows(engine)) == 4


async def test_200_without_results_fails_and_keeps_rows(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))
    before = {(r.id, r.value, r.name) for r in await _rows(engine)}

    fake = FakeHubSpot(_companies(), _contacts(), _owners(), bodies={"companies": {"message": "x"}})
    result = await _run(engine, fake)

    assert result.status == "failed"
    assert result.error == "HubSpot returned an unexpected response."
    assert {(r.id, r.value, r.name) for r in await _rows(engine)} == before


async def test_failure_in_started_log_marks_sync_failed(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingLog:
        def info(self, event: str, **kw: object) -> None:
            raise OSError("stderr closed")

        def warning(self, event: str, **kw: object) -> None:
            pass

    monkeypatch.setattr(sync, "log", ExplodingLog())
    result = await sync.run_sync(
        make_session_factory(engine),
        token=FAKE_TOKEN,
        now=_now,
        transport=FakeHubSpot(_companies(), [], []).transport,
        sleep=Sleeper(),
    )
    assert result.status == "failed"
    assert result.error is not None and "OSError" in result.error
    async with make_session_factory(engine)() as s:
        assert list(await s.scalars(select(HubSpotSync.status))) == ["failed"]


# --- contacts-only customers exclude their domain (final review item 1) ---------------------


async def _exclusion(engine: AsyncEngine, **kw: str) -> check.Exclusion | None:
    async with make_session_factory(engine)() as s:
        return await check.exclusion_for(s, **kw)


async def test_contact_only_customer_excludes_its_domain(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    contacts = [contact("301", "Joe@ACME.com", first="Joe", company_name="Acme Corp", owner="7")]
    result = await _run(engine, FakeHubSpot([], contacts, _owners()))

    assert result.status == "succeeded"
    rows = {(r.kind, r.value): r for r in await _rows(engine)}
    assert set(rows) == {("email", "joe@acme.com"), ("domain", "acme.com")}
    domain_row = rows[("domain", "acme.com")]
    assert (domain_row.hubspot_object, domain_row.hubspot_id) == ("contact", "301")
    assert (domain_row.name, domain_row.owner_name) == ("Acme Corp", "Priya Shah")

    by_domain = await _exclusion(engine, domain="https://www.acme.com/")
    assert by_domain is not None and by_domain.reason == "existing_relationship"
    assert by_domain.detail == "HubSpot contact Acme Corp (owner: Priya Shah)"
    colleague = await _exclusion(engine, email="amy@acme.com")
    assert colleague is not None and colleague.reason == "existing_relationship"


async def test_contact_domain_row_falls_back_to_the_email(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    await _run(engine, FakeHubSpot([], [contact("1", "ops@delta.com")], []))
    hit = await _exclusion(engine, domain="delta.com")
    assert hit is not None and hit.detail == "HubSpot contact ops@delta.com"


@pytest.mark.parametrize("email", ["x@yahoo.co.in", "y@googlemail.com", "z@163.com"])
async def test_free_mail_contact_does_not_exclude_the_provider(
    engine: AsyncEngine, db: AsyncSession, email: str
) -> None:
    result = await _run(engine, FakeHubSpot([], [contact("1", email)], []))

    assert result.status == "succeeded"
    assert _values(await _rows(engine)) == {("email", email)}
    domain = email.split("@")[1]
    assert await _exclusion(engine, domain=domain) is None
    assert await _exclusion(engine, email=f"someone.else@{domain}") is None
    assert await _exclusion(engine, email=email) is not None


def test_free_mail_list_is_shared() -> None:
    assert check.FREE_MAIL_DOMAINS is sync.FREE_MAIL_DOMAINS
    assert {"yahoo.co.in", "googlemail.com", "qq.com", "indiatimes.com"} <= check.FREE_MAIL_DOMAINS


async def test_company_row_wins_over_contact_on_same_domain(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    companies = [company("101", "Acme Chemicals", domain="acme.com")]
    contacts = [
        contact("201", "a@acme.com", company_name="Acme (contact)"),
        contact("202", "b@acme.com"),
    ]
    await _run(engine, FakeHubSpot(companies, contacts, []))

    rows = {(r.kind, r.value): r for r in await _rows(engine)}
    domain_row = rows[("domain", "acme.com")]
    assert (domain_row.hubspot_object, domain_row.hubspot_id) == ("company", "101")
    assert domain_row.name == "Acme Chemicals"


def test_collect_company_precedence_is_order_independent() -> None:
    collected = sync._collect(
        [company("101", "Acme", domain="acme.com")],
        [contact("201", "a@acme.com"), contact("202", "b@beta.com"), contact("203", "c@beta.com")],
        {},
        synced_at=NOW,
    )
    assert collected.rows[("domain", "acme.com")]["hubspot_object"] == "company"
    # Between contacts the later record wins, as for every other duplicate.
    assert collected.rows[("domain", "beta.com")]["hubspot_id"] == "203"


# --- failure backoff (final review item 3) --------------------------------------------------


def _failed_minutes_ago(minutes: float, error: str = "HubSpot unavailable.") -> HubSpotSync:
    at = NOW - timedelta(minutes=minutes)
    return HubSpotSync(status="failed", started_at=at, finished_at=at, error=error)


async def _due(db: AsyncSession) -> bool:
    return await sync.due_for_sync(db, now=NOW, config=CONFIG)


async def test_one_recent_failure_backs_off_five_minutes(db: AsyncSession) -> None:
    row = _failed_minutes_ago(1)
    db.add(row)
    await db.flush()
    assert await _due(db) is False
    row.finished_at = NOW - timedelta(minutes=6)
    await db.flush()
    assert await _due(db) is True


async def test_consecutive_failures_back_off_exponentially(db: AsyncSession) -> None:
    db.add_all([_failed_minutes_ago(120), _failed_minutes_ago(60), _failed_minutes_ago(15)])
    await db.flush()
    assert await _due(db) is False  # third failure in a row: 5 * 2^2 = 20 minutes
    assert sync.failure_backoff(3, CONFIG) == timedelta(minutes=20)


async def test_backoff_capped_at_sync_interval(db: AsyncSession) -> None:
    assert sync.failure_backoff(50, CONFIG) == timedelta(hours=24)
    assert sync.failure_backoff(10_000, CONFIG) == timedelta(hours=24)
    db.add_all([_failed_minutes_ago(60 * 25 + i) for i in range(12)])
    await db.flush()
    assert await _due(db) is True  # 12 failures, the latest 25h ago: the 24h cap has passed


async def test_failures_before_the_last_success_do_not_count(db: AsyncSession) -> None:
    db.add_all([_failed_minutes_ago(60 * 40), _failed_minutes_ago(60 * 35)])
    db.add(_finished("succeeded", 30))
    db.add(_failed_minutes_ago(10))
    await db.flush()
    assert await _due(db) is True  # one failure since the success, 10 min ago: past 5 min


async def test_queued_request_is_due_despite_backoff(db: AsyncSession) -> None:
    db.add(_failed_minutes_ago(1))
    db.add(HubSpotSync(status="queued"))
    await db.flush()
    assert await _due(db) is True


async def test_interrupted_sync_does_not_back_off(db: AsyncSession) -> None:
    db.add(_failed_minutes_ago(1, error=sync.INTERRUPTED))
    db.add(_failed_minutes_ago(0.5, error=sync.CANCELLED))
    await db.flush()
    assert await _due(db) is True


async def test_transport_error_is_retried_then_succeeds(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    fake = FakeHubSpot(_companies(), _contacts(), _owners())
    blips = {"left": 2}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/crm/v3/objects/companies" and blips["left"]:
            blips["left"] -= 1
            raise httpx.ConnectError("connection reset", request=request)
        return fake.transport.handle_request(request)

    sleeper = Sleeper()
    result = await sync.run_sync(
        make_session_factory(engine),
        token=FAKE_TOKEN,
        now=_now,
        transport=httpx.MockTransport(flaky),
        sleep=sleeper,
    )
    fake.assert_read_only()
    assert result.status == "succeeded"
    assert sleeper.delays == [1.0, 2.0]
    assert _values(await _rows(engine)) == EXPECTED


async def test_transport_errors_exhaust_the_retry_budget() -> None:
    sleeper = Sleeper()
    calls: list[httpx.Request] = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = hubspot.HubSpotClient(FAKE_TOKEN, transport=httpx.MockTransport(down), sleep=sleeper)
    with pytest.raises(hubspot.HubSpotError) as info:
        await _first_company(client)
    assert info.value.reason == "Could not reach HubSpot: ReadTimeout."
    assert len(calls) == 4 and sleeper.delays == [1, 2, 4]


# --- health shows a failing latest attempt even while fresh (final review item 5) -----------


async def test_fresh_health_mentions_latest_failed_attempt(db: AsyncSession) -> None:
    db.add(_finished("succeeded", 3))
    db.add(_finished("failed", 1, error="Could not reach HubSpot: ConnectError."))
    await db.flush()
    health = await _health(db)
    assert health.state == "fresh"
    assert health.message == (
        "HubSpot last synced 2026-11-07 09:00 UTC; the latest attempt failed "
        "2026-11-07 11:00 UTC: Could not reach HubSpot: ConnectError."
    )


async def test_fresh_health_after_success_has_no_failure_note(db: AsyncSession) -> None:
    db.add(_finished("failed", 3, error="nope"))
    db.add(_finished("succeeded", 1))
    await db.flush()
    health = await _health(db)
    assert health.state == "fresh"
    assert health.message == "HubSpot last synced 2026-11-07 11:00 UTC."


# --- no PII from database errors in the stored error (final review item 7) ------------------


async def test_db_error_parameters_are_not_stored(
    engine: AsyncEngine, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    pii = "victim.person@private-customer.example"

    async def bad_sql(session: AsyncSession, keep_ids: object) -> int:
        await session.execute(text("SELECT CAST(:v AS integer)"), {"v": pii})
        return 0

    monkeypatch.setattr(sync, "_delete_unseen", bad_sql)
    result = await _run(engine, FakeHubSpot(_companies(), _contacts(), _owners()))

    assert result.status == "failed"
    assert result.error == "Sync failed unexpectedly (DBAPIError)."
    async with make_session_factory(engine)() as s:
        stored = (await s.get(HubSpotSync, result.id)) or pytest.fail("sync row missing")
    assert pii not in (stored.error or "")
    assert "SELECT" not in (stored.error or "")


async def test_sync_blocks_stored_addresses_at_once(engine: AsyncEngine, db: AsyncSession) -> None:
    """Item 4: a HubSpot contact's address stored for a company is blocked by the sync itself,
    and the company's sendable count drops, without waiting for its next contact check."""
    from salesfinder.db.models import Company, EmailAddress, Person

    acme = Company(
        side="buyer",
        name="Acme",
        domain="acme.com",
        website_url="https://acme.com",
        status="new",
        profile_status="ok",
        contacts_found=1,
        sendable_contacts=1,
        contacts_checked_at=NOW,
    )
    db.add(acme)
    await db.flush()
    priya = Person(
        company_id=acme.id,
        full_name="Priya Nair",
        name_key="priya nair",
        title_category="procurement",
        is_target=True,
        rank=1,
        source_url="https://acme.com/team",
    )
    db.add(priya)
    await db.flush()
    db.add(
        EmailAddress(
            company_id=acme.id,
            person_id=priya.id,
            address="priya.nair@gmail.com",
            kind="personal",
            status="published",
            source="page",
        )
    )
    await db.commit()

    result = await _run(engine, FakeHubSpot([], [contact("1", "Priya.Nair@gmail.com")], []))

    assert result.status == "succeeded"
    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(EmailAddress))
        stored = await s.scalar(select(Company))
    assert row is not None and stored is not None
    assert row.blocked_reason == "HubSpot contact priya.nair@gmail.com"
    assert (stored.status, stored.sendable_contacts) == ("new", 0)

    # HubSpot drops the contact: the next sync unblocks it.
    await _run(engine, FakeHubSpot([company("2", "Other", domain="other.com")], [], []))
    async with make_session_factory(engine)() as s:
        row = await s.scalar(select(EmailAddress))
        stored = await s.scalar(select(Company))
    assert row is not None and stored is not None
    assert (row.blocked_reason, stored.sendable_contacts) == (None, 1)
