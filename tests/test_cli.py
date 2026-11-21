import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from conftest import (
    FAKE_CLAUDE,
    TEST_SECRET_KEY,
    VALID_YAML,
    clear_salesfinder_env,
    copy_shipped_config,
)
from fakes.mail import FakeSender, fake_mail
from salesfinder import audit
from salesfinder import cli as cli_module
from salesfinder.auth import sessions, users
from salesfinder.cli import app
from salesfinder.config import AppConfig
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.db.models import AuditLog, HubSpotSync, Run, User, UserSession
from salesfinder.ops import runs as runs_service
from salesfinder.ops.health import NEVER
from salesfinder.ops.runs import PassResult
from salesfinder.ops.version import code_version
from salesfinder.settings import get_settings
from salesfinder.worker import WorkerDeps

runner = CliRunner()


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str, db: AsyncSession
) -> None:
    # cwd is a tmp dir so a developer .env can never be picked up.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "salesfinder.yaml").write_text(VALID_YAML, encoding="utf-8")
    copy_shipped_config(tmp_path)
    # Everything the shell exported goes (SALESFINDER_PUBLIC_URL and SALESFINDER_LOG_FILE
    # included); only what is set below remains.
    clear_salesfinder_env(monkeypatch)
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", database_url)
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("SALESFINDER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SALESFINDER_CLAUDE_BIN", str(FAKE_CLAUDE.resolve()))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _fetch(url: str) -> tuple[list[User], list[AuditLog]]:
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as s:
            return (
                list((await s.scalars(select(User))).all()),
                list((await s.scalars(select(AuditLog))).all()),
            )
    finally:
        await engine.dispose()


async def _seed_fresh_sync(url: str) -> None:
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as s:
            now = datetime.now(UTC)
            s.add(HubSpotSync(status="succeeded", started_at=now, finished_at=now))
            await s.commit()
    finally:
        await engine.dispose()


def test_create_admin_and_doctor_exit_codes(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = "a long enough password"
    result = runner.invoke(
        app,
        ["users", "create-admin", "--email", " Boss@Elchemy.com", "--name", "Boss"],
        input=f"{pw}\n{pw}\n",
    )
    assert result.exit_code == 0, result.output
    users, audits = asyncio.run(_fetch(database_url))
    assert [(u.email, u.role) for u in users] == [("boss@elchemy.com", "admin")]
    assert [a.action for a in audits] == ["user.admin_created"]

    short = runner.invoke(
        app,
        ["users", "create-admin", "--email", "x@elchemy.com", "--name", "X"],
        input="short\nshort\n",
    )
    assert short.exit_code == 1
    assert "at least 12 characters" in short.output

    monkeypatch.setenv("SALESFINDER_HUBSPOT_TOKEN", "tok-test")
    monkeypatch.setenv("SALESFINDER_SERPER_API_KEY", "key-test")
    get_settings.cache_clear()
    asyncio.run(_seed_fresh_sync(database_url))
    ok = runner.invoke(app, ["doctor"])
    # Everything but the mailboxes check passes (none is configured; the real mail transport is
    # never built in tests).
    assert ok.exit_code == 1, ok.output
    assert "✓ claude_cli" in ok.output
    assert "✗ mailboxes — No mailboxes configured — nothing can send." in ok.output
    assert "✓ sending — Sending is paused: Sending hasn't been turned on yet." in ok.output

    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--tools")
    bad = runner.invoke(app, ["doctor"])
    assert bad.exit_code == 1
    assert "✗ claude_cli" in bad.output


def _make_user(url: str, *, active: bool = True) -> str:
    async def _go() -> str:
        engine = make_engine(url)
        try:
            async with make_session_factory(engine)() as s:
                user = await users.create_admin(
                    s,
                    email="boss@elchemy.com",
                    name="Boss",
                    password="the old password",  # noqa: S106  # noqa: S106
                )
                await sessions.create_session(
                    s,
                    user,
                    ttl=timedelta(hours=1),
                    now=datetime.now(UTC),
                    ip=None,
                    user_agent=None,
                )
                user.is_active = active
                await s.commit()
                return str(user.id)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


async def _sessions_and_audit(url: str) -> tuple[list[UserSession], list[AuditLog], list[User]]:
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as s:
            return (
                list((await s.scalars(select(UserSession))).all()),
                list((await s.scalars(select(AuditLog))).all()),
                list((await s.scalars(select(User))).all()),
            )
    finally:
        await engine.dispose()


def test_reset_password(cli_env: None, database_url: str) -> None:
    _make_user(database_url)
    new = "a brand new password"
    result = runner.invoke(
        app, ["users", "reset-password", "--email", "Boss@Elchemy.com"], input=f"{new}\n{new}\n"
    )
    assert result.exit_code == 0, result.output
    sess, audits, _ = asyncio.run(_sessions_and_audit(database_url))
    assert all(x.revoked_at is not None for x in sess) and sess
    assert [a.action for a in audits] == [audit.USER_PASSWORD_RESET]
    assert new not in str(audits[0].details)

    async def _auth(pw: str) -> bool:
        engine = make_engine(database_url)
        try:
            async with make_session_factory(engine)() as s:
                got = await users.authenticate(s, "boss@elchemy.com", pw, now=datetime.now(UTC))
                return got is not None
        finally:
            await engine.dispose()

    assert asyncio.run(_auth("the old password")) is False
    assert asyncio.run(_auth(new)) is True


def test_reset_password_short_and_unknown(cli_env: None, database_url: str) -> None:
    _make_user(database_url)
    short = runner.invoke(
        app, ["users", "reset-password", "--email", "boss@elchemy.com"], input="short\nshort\n"
    )
    assert short.exit_code == 1 and "at least 12 characters" in short.output
    pw = "a brand new password"
    unknown = runner.invoke(
        app, ["users", "reset-password", "--email", "nobody@elchemy.com"], input=f"{pw}\n{pw}\n"
    )
    assert unknown.exit_code == 1 and "No such user" in unknown.output
    _, audits, _ = asyncio.run(_sessions_and_audit(database_url))
    assert audits == []


def test_set_active_toggles(cli_env: None, database_url: str) -> None:
    _make_user(database_url)
    off = runner.invoke(app, ["users", "set-active", "--email", "boss@elchemy.com", "--inactive"])
    assert off.exit_code == 0, off.output
    sess, _, people = asyncio.run(_sessions_and_audit(database_url))
    assert people[0].is_active is False
    assert all(x.revoked_at is not None for x in sess)
    on = runner.invoke(app, ["users", "set-active", "--email", "boss@elchemy.com", "--active"])
    assert on.exit_code == 0, on.output
    _, audits, people = asyncio.run(_sessions_and_audit(database_url))
    assert people[0].is_active is True
    assert [a.action for a in audits] == [audit.USER_DEACTIVATED, audit.USER_REACTIVATED]
    missing = runner.invoke(app, ["users", "set-active", "--email", "x@elchemy.com", "--active"])
    assert missing.exit_code == 1 and "No such user" in missing.output


def _invites_from(url: str, inviter_email: str, emails: list[str]) -> None:
    async def _go() -> None:
        engine = make_engine(url)
        try:
            async with make_session_factory(engine)() as s:
                inviter = await users.find_by_email(s, inviter_email)
                assert inviter is not None
                for email in emails:
                    await users.create_invite(
                        s, email=email, role="member", invited_by=inviter, now=datetime.now(UTC)
                    )
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_go())


def test_set_active_inactive_expires_invites_and_audits_the_count(
    cli_env: None, database_url: str
) -> None:
    _make_user(database_url)
    _invites_from(database_url, "boss@elchemy.com", ["a@elchemy.com", "b@elchemy.com"])
    off = runner.invoke(app, ["users", "set-active", "--email", "boss@elchemy.com", "--inactive"])
    assert off.exit_code == 0, off.output
    _, audits, _ = asyncio.run(_sessions_and_audit(database_url))
    assert [(a.action, a.details) for a in audits] == [
        (audit.USER_DEACTIVATED, {"email": "boss@elchemy.com", "invites_revoked": 2})
    ]
    on = runner.invoke(app, ["users", "set-active", "--email", "boss@elchemy.com", "--active"])
    assert on.exit_code == 0, on.output
    _, audits, _ = asyncio.run(_sessions_and_audit(database_url))
    reactivated = [a.details for a in audits if a.action == audit.USER_REACTIVATED]
    assert reactivated == [{"email": "boss@elchemy.com"}]


def test_empty_secret_key_is_a_clean_error(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", "")
    get_settings.cache_clear()
    for args in (["db", "upgrade"], ["doctor"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert "Configuration error" in result.output
        assert "SALESFINDER_SECRET_KEY" in result.output
        assert "Traceback" not in result.output


def test_unreachable_database_is_a_clean_error(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql+asyncpg://localhost:1/nope_test")
    get_settings.cache_clear()
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 1, result.output
    assert "Database error" in result.output and "salesfinder doctor" in result.output
    assert "Traceback" not in result.output
    pw = "a long enough password"
    admin = runner.invoke(
        app,
        ["users", "create-admin", "--email", "a@elchemy.com", "--name", "A"],
        input=f"{pw}\n{pw}\n",
    )
    assert admin.exit_code == 1 and "Database error" in admin.output


def test_create_admin_duplicate_is_clean(cli_env: None, database_url: str) -> None:
    pw = "a long enough password"
    args = ["users", "create-admin", "--email", "a@elchemy.com", "--name", "A"]
    assert runner.invoke(app, args, input=f"{pw}\n{pw}\n").exit_code == 0
    again = runner.invoke(app, args, input=f"{pw}\n{pw}\n")
    assert again.exit_code == 1
    assert "already exists" in again.output and "Traceback" not in again.output


def test_hubspot_sync_without_token_exits_1(cli_env: None) -> None:
    result = runner.invoke(app, ["hubspot", "sync"])
    assert result.exit_code == 1
    assert "SALESFINDER_HUBSPOT_TOKEN" in result.output


def test_hubspot_sync_refuses_while_one_is_running(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SALESFINDER_HUBSPOT_TOKEN", "tok-test")
    get_settings.cache_clear()

    async def _seed() -> None:
        engine = make_engine(database_url)
        try:
            async with make_session_factory(engine)() as s:
                s.add(HubSpotSync(status="running", started_at=datetime.now(UTC)))
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    result = runner.invoke(app, ["hubspot", "sync"])
    assert result.exit_code == 1
    assert "A HubSpot sync is already running." in result.output


def test_discover_jobs_empty(cli_env: None) -> None:
    result = runner.invoke(app, ["discover", "jobs"])
    assert result.exit_code == 0, result.output
    assert "No discovery jobs yet." in result.output


@pytest.mark.parametrize("args", [["web"], ["web", "--reload"]])
def test_web_with_broken_config_is_a_clean_error(
    cli_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    def no_server(*_: object, **__: object) -> None:
        pytest.fail("uvicorn.run must not be reached with a broken config")

    monkeypatch.setattr(cli_module.uvicorn, "run", no_server)
    (tmp_path / "catalog.yaml").write_text("products:\n  - id: Bad Id\n", encoding="utf-8")
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "Configuration error" in result.output
    assert "catalog.yaml" in result.output
    assert "Traceback" not in result.output


def test_web_hands_a_built_app_to_uvicorn(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        cli_module.uvicorn, "run", lambda target, **options: calls.append((target, options))
    )
    result = runner.invoke(app, ["web"])
    assert result.exit_code == 0, result.output
    [(target, options)] = calls
    assert isinstance(target, FastAPI)
    assert target.state.catalog.by_id("sles").name.startswith("Sodium Laureth Sulfate")
    assert options["access_log"] is False


def _fake_run_forever(calls: list[str], mails: list[object] | None = None) -> object:
    async def run_forever(deps: object, *, stop: asyncio.Event) -> None:
        calls.append("started")  # stands in for a loop that would never stop on its own
        if mails is not None:
            mails.append(getattr(deps, "mail", None))

    return run_forever


async def test_second_worker_refuses_to_start(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from salesfinder import worker as worker_module

    calls: list[str] = []
    mails: list[object] = []
    monkeypatch.setattr(worker_module, "run_forever", _fake_run_forever(calls, mails))
    # The worker sends through the real mail factory; here a stand-in (no SMTP, no IMAP).
    fake_factory = fake_mail(FakeSender(), None)
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", lambda: fake_factory)
    engine = make_engine(database_url)
    try:
        async with engine.connect() as held:  # "another worker" holding the lock
            assert await held.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await held.commit()
            # The CLI runs its own event loop, so invoke it from a thread.
            result = await asyncio.to_thread(runner.invoke, app, ["worker"])
            assert result.exit_code == 1, result.output
            assert "Another SalesFinder worker is already running." in result.output
            assert calls == []
            await held.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await held.commit()

        result = await asyncio.to_thread(runner.invoke, app, ["worker"])
        assert result.exit_code == 0, result.output
        assert calls == ["started"]
        assert mails == [fake_factory]  # the Sending step gets the mail factory
        async with engine.connect() as conn:  # the worker released the lock on exit
            assert await conn.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await conn.commit()
    finally:
        await engine.dispose()


def _stub_pass(
    monkeypatch: pytest.MonkeyPatch, result: PassResult, seen: list[WorkerDeps] | None = None
) -> None:
    """``worker --once`` with a stub pass (no steps run) and a stand-in mail factory."""

    async def run_pass(deps: WorkerDeps) -> PassResult:
        if seen is not None:
            seen.append(deps)
        return result

    monkeypatch.setattr("salesfinder.worker.run_pass", run_pass)
    fake_factory = fake_mail(FakeSender(), None)
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", lambda: fake_factory)


async def _run_rows(url: str) -> list[Run]:
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as s:
            return list((await s.scalars(select(Run).order_by(Run.started_at))).all())
    finally:
        await engine.dispose()


async def test_worker_once_records_one_pass(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[WorkerDeps] = []
    _stub_pass(monkeypatch, PassResult(lines=["Stub line."], failures=[], quiet=False), seen)

    result = await asyncio.to_thread(runner.invoke, app, ["worker", "--once"])

    assert result.exit_code == 0, result.output
    assert "Stub line." in result.output
    [row] = await _run_rows(database_url)
    assert (row.end_reason, row.passes) == ("once", 1)
    [deps] = seen
    assert deps.send_enabled is True


async def test_worker_once_exits_1_when_a_step_failed(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = "Profiling step failed: RuntimeError"
    _stub_pass(monkeypatch, PassResult(lines=[failure], failures=[failure], quiet=False))

    result = await asyncio.to_thread(runner.invoke, app, ["worker", "--once"])

    assert result.exit_code == 1, result.output
    assert failure in result.output
    [row] = await _run_rows(database_url)
    assert (row.end_reason, row.failed_steps) == ("once", 1)


async def test_worker_no_send_turns_sending_off(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[WorkerDeps] = []
    _stub_pass(monkeypatch, PassResult(lines=[], failures=[], quiet=False), seen)

    result = await asyncio.to_thread(runner.invoke, app, ["worker", "--once", "--no-send"])

    assert result.exit_code == 0, result.output
    [deps] = seen
    assert deps.send_enabled is False


async def test_worker_once_takes_the_worker_lock(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from salesfinder import worker as worker_module

    seen: list[WorkerDeps] = []
    _stub_pass(monkeypatch, PassResult(lines=["Stub line."], failures=[], quiet=False), seen)
    engine = make_engine(database_url)
    try:
        async with engine.connect() as held:  # "another worker" holding the lock
            assert await held.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await held.commit()
            result = await asyncio.to_thread(runner.invoke, app, ["worker", "--once"])
            await held.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await held.commit()
    finally:
        await engine.dispose()

    assert result.exit_code == 1, result.output
    assert "Another SalesFinder worker is already running." in result.output
    assert seen == []
    assert await _run_rows(database_url) == []


async def test_worker_stops_when_another_worker_took_over(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop gave up (another worker's run is open): the same line, exit 1."""
    from salesfinder import worker as worker_module

    async def run_forever(deps: object, *, stop: asyncio.Event) -> None:
        raise worker_module.AnotherWorkerRunning

    monkeypatch.setattr(worker_module, "run_forever", run_forever)
    fake_factory = fake_mail(FakeSender(), None)
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", lambda: fake_factory)

    result = await asyncio.to_thread(runner.invoke, app, ["worker"])

    assert result.exit_code == 1, result.output
    assert "Another SalesFinder worker is already running." in result.output
    engine = make_engine(database_url)
    try:
        async with engine.connect() as conn:  # the lock was released on the way out
            assert await conn.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": worker_module.WORKER_LOCK_KEY}
            )
            await conn.commit()
    finally:
        await engine.dispose()


async def test_worker_no_send_loop_gets_a_refusing_sender(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3/M8: `worker --no-send` (the loop): sending off, and a sender that refuses anyway;
    mail is still read through the usual reader."""
    from salesfinder import worker as worker_module
    from salesfinder.mail.factory import RefusingSender, SendingDisabledError

    seen: list[WorkerDeps] = []

    async def run_forever(deps: WorkerDeps, *, stop: asyncio.Event) -> None:
        seen.append(deps)

    monkeypatch.setattr(worker_module, "run_forever", run_forever)
    fake_factory = fake_mail(FakeSender(), None)
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", lambda: fake_factory)

    result = await asyncio.to_thread(runner.invoke, app, ["worker", "--no-send"])

    assert result.exit_code == 0, result.output
    [deps] = seen
    assert deps.send_enabled is False
    assert deps.mail is not None
    assert isinstance(deps.mail.sender, RefusingSender)
    assert deps.mail.open_reader is fake_factory.open_reader
    with pytest.raises(SendingDisabledError):
        await deps.mail.sender.check(None)  # type: ignore[arg-type]


async def test_worker_stops_when_its_lock_is_lost(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3 (Ruling P7-C): Postgres drops the lock connection (a restart); the next lock check
    says so, the worker stops with the lost-lock line and exit 1, without a traceback."""
    from salesfinder import worker as worker_module

    checks: list[bool] = []

    async def run_forever(deps: WorkerDeps, *, stop: asyncio.Event) -> None:
        assert deps.lock_alive is not None
        assert await deps.lock_alive()
        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                pid = await conn.scalar(
                    text(
                        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND objid::bigint = :k"
                    ),
                    {"k": worker_module.WORKER_LOCK_KEY},
                )
                await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                await conn.commit()
        finally:
            await engine.dispose()
        checks.append(await deps.lock_alive())
        deps.lock_lost = True  # what the beat does on a failed check

    monkeypatch.setattr(worker_module, "run_forever", run_forever)
    fake_factory = fake_mail(FakeSender(), None)
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", lambda: fake_factory)

    result = await asyncio.to_thread(runner.invoke, app, ["worker"])

    assert checks == [False]
    assert result.exit_code == 1, result.output
    assert (
        "Lost the worker lock (database restarted?) — stopping so launchd restarts me."
        in result.output
    )
    assert result.exception is None or isinstance(result.exception, SystemExit)


async def _signal_db(url: str, work: object) -> object:
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as s:
            return await work(s)  # type: ignore[operator]
    finally:
        await engine.dispose()


def test_signals_lists_the_catalog(cli_env: None) -> None:
    result = runner.invoke(app, ["signals"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["code", "side", "state", "observed", "label"]
    assert len(lines) == 15
    assert lines[1].split()[:4] == ["INDUSTRY_MATCH", "buyer", "proposed", "0"]
    assert "Works in our industries" in lines[1]


def test_signals_confirm_free_audits_each_change(cli_env: None, database_url: str) -> None:
    from salesfinder.signals import settings as sig

    result = runner.invoke(app, ["signals", "--confirm", "free"])
    assert result.exit_code == 0, result.output
    assert "Confirmed 14 signal(s)." in result.output

    async def _check(s: AsyncSession) -> tuple[frozenset[str], list[AuditLog]]:
        rows = await s.scalars(
            select(AuditLog).where(AuditLog.action == audit.SIGNAL_STATE_CHANGED)
        )
        return await sig.confirmed_codes(s), list(rows.all())

    confirmed, rows = asyncio.run(_signal_db(database_url, _check))  # type: ignore[misc]
    assert len(confirmed) == 14
    assert len(rows) == 14
    assert {r.details["new"] for r in rows} == {"confirmed"}
    assert {r.details["old"] for r in rows} == {"proposed"}
    assert all(r.target_type == "signal" and r.actor_id is None for r in rows)

    again = runner.invoke(app, ["signals", "--confirm", "free"])
    assert again.exit_code == 0, again.output
    assert "No changes." in again.output


def test_signals_unknown_code_exits_1(cli_env: None, database_url: str) -> None:
    result = runner.invoke(app, ["signals", "--confirm", "EXPORTS", "--reject", "NOPE"])
    assert result.exit_code == 1
    assert "Unknown signal NOPE" in result.output

    async def _states(s: AsyncSession) -> dict[str, str]:
        from salesfinder.signals import settings as sig

        return await sig.states(s)

    states = asyncio.run(_signal_db(database_url, _states))  # type: ignore[misc]
    assert states["EXPORTS"] == "proposed"  # nothing applied


def test_signals_state_change_rescores(cli_env: None, database_url: str) -> None:
    from salesfinder.db.models import Company, Observation

    async def _seed(s: AsyncSession) -> object:
        company = Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
        )
        s.add(company)
        await s.flush()
        s.add(
            Observation(company_id=company.id, code="QUALITY_CERTS", value=["GMP"], is_latest=True)
        )
        await s.commit()
        return company.id

    cid = asyncio.run(_signal_db(database_url, _seed))

    async def _score(s: AsyncSession) -> int | None:
        row = await s.get(Company, cid)
        assert row is not None
        return row.score

    result = runner.invoke(app, ["signals", "--confirm", "quality_certs"])
    assert result.exit_code == 0, result.output
    assert "Confirmed 1 signal(s)." in result.output
    assert "Rescored 1 company(ies)." in result.output
    assert asyncio.run(_signal_db(database_url, _score)) == 10  # type: ignore[misc]
    listing = runner.invoke(app, ["signals"])
    quality = next(line for line in listing.output.splitlines() if "QUALITY_CERTS" in line)
    assert quality.split()[:4] == ["QUALITY_CERTS", "buyer", "confirmed", "1"]

    assert "Queued" not in result.output  # the company was never profiled

    result = runner.invoke(app, ["signals", "--skip", "QUALITY_CERTS"])
    assert result.exit_code == 0, result.output
    assert "Skipped 1 signal(s)." in result.output
    assert asyncio.run(_signal_db(database_url, _score)) == 0  # type: ignore[misc]


def test_profile_without_confirmed_signals(cli_env: None) -> None:
    result = runner.invoke(app, ["profile", "--limit", "3"])
    assert result.exit_code == 0, result.output
    assert "No signals are confirmed" in result.output


def test_profile_runs_one_batch(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakes.web import FakeWeb
    from salesfinder.db.models import Company
    from salesfinder.signals import settings as sig

    web = FakeWeb({}, {})  # nothing resolves, so nothing is ever connected to
    monkeypatch.setattr("salesfinder.profiling.fetch.default_resolver", web.resolver)

    async def _seed(s: AsyncSession) -> None:
        await sig.set_state(s, "QUALITY_CERTS", "confirmed", actor=None)
        s.add(
            Company(
                side="buyer",
                name="Acme",
                domain="acme.com",
                website_url="https://acme.com",
                status="new",
            )
        )
        await s.commit()

    asyncio.run(_signal_db(database_url, _seed))
    result = runner.invoke(app, ["profile", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert set(web.lookups) == {"acme.com"}  # robots.txt, then the homepage
    assert "acme.com" in result.output
    assert "no_site" in result.output
    assert "Could not connect" in result.output
    assert "Profiled 1 company (0 ok, 1 failed)." in result.output
    again = runner.invoke(app, ["profile"])
    assert "Nothing to profile right now." in again.output


def test_confirming_queues_profiled_companies_again(cli_env: None, database_url: str) -> None:
    from salesfinder.db.models import Company

    async def _seed(s: AsyncSession) -> object:
        company = Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
            profile_status="ok",
            profiled_at=datetime.now(UTC) - timedelta(days=1),
        )
        s.add(company)
        await s.commit()
        return company.id

    cid = asyncio.run(_signal_db(database_url, _seed))
    result = runner.invoke(app, ["signals", "--confirm", "QUALITY_CERTS"])
    assert result.exit_code == 0, result.output
    assert "Queued 1 company(ies) to be profiled again." in result.output

    async def _row(s: AsyncSession) -> tuple[str, object]:
        row = await s.get(Company, cid)
        assert row is not None
        return row.profile_status, row.profiled_at

    assert asyncio.run(_signal_db(database_url, _row)) == ("pending", None)


def test_signals_rescore_recomputes_scores(cli_env: None, database_url: str) -> None:
    """After a scoring-weight edit: `signals --rescore` recomputes every stored score."""
    from salesfinder.db.models import Company, Observation
    from salesfinder.signals import settings as sig

    async def _seed(s: AsyncSession) -> object:
        await sig.set_state(s, "QUALITY_CERTS", "confirmed", actor=None)
        company = Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
            score=99,  # computed with old weights
            scored_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        s.add(company)
        await s.flush()
        s.add(
            Observation(company_id=company.id, code="QUALITY_CERTS", value=["GMP"], is_latest=True)
        )
        await s.commit()
        return company.id

    cid = asyncio.run(_signal_db(database_url, _seed))

    async def _score(s: AsyncSession) -> int | None:
        row = await s.get(Company, cid)
        assert row is not None
        return row.score

    result = runner.invoke(app, ["signals", "--rescore"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Rescored 1 company(ies)."
    assert asyncio.run(_signal_db(database_url, _score)) == 10  # type: ignore[misc]

    again = runner.invoke(app, ["signals", "--confirm", "QUALITY_CERTS", "--rescore"])
    assert again.exit_code == 0, again.output
    assert again.output.splitlines() == ["No changes.", "Rescored 1 company(ies)."]


def _seed_contacts(database_url: str) -> None:
    from salesfinder.db.models import Company, DomainMx, EmailAddress, Person

    async def _seed(s: AsyncSession) -> None:
        company = Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
            profile_status="ok",
            contact_form_url="https://acme.com/contact",
            contacts_checked_at=datetime(2026, 11, 8, 12, tzinfo=UTC),
            contacts_found=2,
            sendable_contacts=1,
        )
        s.add(company)
        await s.flush()
        people = [
            ("Priya Nair", "Head of Procurement", "procurement", True, 1),
            ("Anil Mehta", "Owner", "owner_exec", True, 2),
            ("Sunita Rao", "Accountant", "other", False, None),
        ]
        rows = {}
        for name, title, category, target, rank in people:
            first, last = name.split()
            rows[name] = Person(
                company_id=company.id,
                full_name=name,
                name_key=name.lower(),
                first_name=first,
                last_name=last,
                title=title,
                title_category=category,
                is_target=target,
                rank=rank,
                source_url="https://acme.com/team",
            )
            s.add(rows[name])
        await s.flush()
        for address, status, source, person in (
            ("priya.nair@acme.com", "published", "page", "Priya Nair"),
            ("anil.mehta@acme.com", "pattern_guess", "guess", "Anil Mehta"),
            ("info@acme.com", "published", "page", None),
        ):
            s.add(
                EmailAddress(
                    company_id=company.id,
                    person_id=rows[person].id if person else None,
                    address=address,
                    kind="role" if person is None else "personal",
                    status=status,
                    source=source,
                )
            )
        s.add(
            DomainMx(
                domain="acme.com",
                state="has_mx",
                mx_hosts=["mx.acme.com"],
                checked_at=datetime(2026, 11, 8, 12, tzinfo=UTC),
            )
        )
        await s.commit()

    asyncio.run(_signal_db(database_url, _seed))


def test_contacts_for_a_company(cli_env: None, database_url: str) -> None:
    _seed_contacts(database_url)

    result = runner.invoke(app, ["contacts", "--company", "https://www.ACME.com/about"])

    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in result.output.splitlines()]
    assert lines[0] == "Acme (acme.com, buyer)"
    assert "Mail server: has_mx" in lines[1]
    assert "2 targets, 1 sendable" in lines[1]
    assert "Contact form: https://acme.com/contact" in lines
    assert "  \u2605 Priya Nair \u2014 Head of Procurement (procurement)" in lines
    assert "  \u2605 Anil Mehta \u2014 Owner (owner_exec)" in lines
    assert "    Sunita Rao \u2014 Accountant (other)" in lines
    by_address = {line.split()[0]: line.split(None, 2)[1:] for line in lines if "@" in line}
    assert by_address["priya.nair@acme.com"] == ["published", "sendable: yes"]
    assert by_address["anil.mehta@acme.com"] == ["pattern_guess", "sendable: no (Held: guess)"]
    assert by_address["info@acme.com"] == ["published", "sendable: yes"]


def test_contacts_unknown_domain_exits_1(cli_env: None) -> None:
    result = runner.invoke(app, ["contacts", "--company", "nope.com"])
    assert result.exit_code == 1
    assert "No company with domain nope.com." in result.output


def test_contacts_needs_an_option(cli_env: None) -> None:
    result = runner.invoke(app, ["contacts"])
    assert result.exit_code == 2
    assert "Give --company <domain> or --verify." in result.output


def test_contacts_verify_runs_one_batch(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakes.dns import FakeResolver
    from salesfinder.db.models import Company

    resolver = FakeResolver({("acme.com", "MX"): [(10, "mx.acme.com.")]})
    monkeypatch.setattr("salesfinder.contacts.dns.default_resolve", lambda _config: resolver)

    nothing = runner.invoke(app, ["contacts", "--verify"])
    assert nothing.exit_code == 0, nothing.output
    assert "No companies due for a contact check." in nothing.output

    async def _seed(s: AsyncSession) -> None:
        s.add(
            Company(
                side="buyer",
                name="Acme",
                domain="acme.com",
                website_url="https://acme.com",
                status="new",
                profile_status="ok",
            )
        )
        await s.commit()

    asyncio.run(_signal_db(database_url, _seed))
    result = runner.invoke(app, ["contacts", "--verify", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert resolver.calls == [("acme.com", "MX")]
    assert "acme.com" in result.output and "has_mx" in result.output
    assert "Checked contacts for 1 company (0 sendable addresses)." in result.output


def test_contacts_verify_reports_verifier_failure(
    cli_env: None, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: `contacts --verify` prints the same verifier-failed note as the worker."""
    from fakes.dns import FakeResolver
    from fakes.verifier import FAKE_KEY, FakeMillionVerifier
    from salesfinder.contacts.verifier import MillionVerifier
    from salesfinder.db.models import Company, Person

    resolver = FakeResolver({("acme.com", "MX"): [(10, "mx.acme.com.")]})
    monkeypatch.setattr("salesfinder.contacts.dns.default_resolve", lambda _config: resolver)
    fake = FakeMillionVerifier({"error": "Invalid API key"})
    monkeypatch.setattr(
        "salesfinder.worker.build_verifier",
        lambda *_a, **_k: (MillionVerifier(FAKE_KEY, transport=fake.transport()), None),
    )

    async def _seed(s: AsyncSession) -> None:
        company = Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
            profile_status="ok",
        )
        s.add(company)
        await s.flush()
        s.add(
            Person(
                company_id=company.id,
                full_name="Priya Nair",
                name_key="priya nair",
                first_name="Priya",
                last_name="Nair",
                title_category="procurement",
                is_target=True,
                rank=1,
                source_url="https://acme.com/team",
            )
        )
        await s.commit()

    asyncio.run(_signal_db(database_url, _seed))
    result = runner.invoke(app, ["contacts", "--verify"])
    assert result.exit_code == 0, result.output
    assert len(fake.requests) == 1
    assert (
        "Checked contacts for 1 company (0 sendable addresses)."
        " Email verifier failed — check the key and credits." in result.output
    )
    assert FAKE_KEY not in result.output and "Invalid API key" not in result.output


# --- campaigns ----------------------------------------------------------------------------------


def _confirmed_facts_yaml(config_dir: Path) -> None:
    import yaml

    from campaign_helpers import FACTS

    (config_dir / "facts.yaml").write_text(yaml.safe_dump(FACTS.model_dump()), "utf-8")


def _seed_campaigns(database_url: str) -> None:
    from campaign_helpers import seed_campaign, seed_company
    from salesfinder.campaigns import service
    from salesfinder.db.models import Message, Sequence

    async def _seed(s: AsyncSession) -> None:
        now = datetime.now(UTC)
        live = await seed_campaign(s, name="Texas soap makers", cap=25, now=now)
        paused = await seed_campaign(s, name="Gujarat SLES", side="supplier", now=now)
        await service.pause(s, paused.id, actor=None)
        paused.last_error = "Daily Claude budget reached (150 calls)."
        company, _, _ = await seed_company(s, name="Old Co", domain="old-co.com", checked_at=now)
        seq = Sequence(
            campaign_id=live.id,
            company_id=company.id,
            recipient="jane.doe@old-co.com",
            status="awaiting_approval",
            created_at=now,
        )
        s.add(seq)
        await s.flush()
        s.add(
            Message(sequence_id=seq.id, step=1, subject="S", body="B", status="awaiting_approval")
        )
        s.add(Message(sequence_id=seq.id, step=2, subject="S", body="B", status="pending"))
        await seed_company(s, name="Acme Soap Works", domain="acme-soap.com", checked_at=now)
        await s.commit()

    asyncio.run(_signal_db(database_url, _seed))


def test_campaigns_lists_them(cli_env: None, database_url: str) -> None:
    empty = runner.invoke(app, ["campaigns"])
    assert empty.exit_code == 0, empty.output
    assert empty.output.strip() == "No campaigns yet."

    _seed_campaigns(database_url)
    result = runner.invoke(app, ["campaigns"])
    assert result.exit_code == 0, result.output
    lines = [line.rstrip() for line in result.output.splitlines()]
    assert lines[0].split() == ["Name", "Side", "Status", "Cohort", "Today/cap", "Awaiting"]
    rows = {line.split("  ")[0]: line.split() for line in lines[1:] if not line.startswith(" ")}
    assert rows["Texas soap makers"][3:4] == ["buyer"]
    assert rows["Texas soap makers"][4] == "active"
    assert rows["Texas soap makers"][-2:] == ["1/25", "1"]
    assert rows["Gujarat SLES"][2:4] == ["supplier", "paused"]
    assert rows["Gujarat SLES"][-2:] == ["0/40", "0"]
    assert "  Last error: Daily Claude budget reached (150 calls)." in lines


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> object:
    """`salesfinder draft` refreshes stale mail-server checks with real DNS: fake it."""
    from fakes.dns import FakeResolver

    resolver = FakeResolver()
    monkeypatch.setattr("salesfinder.contacts.dns.default_resolve", lambda _config: resolver)
    return resolver


def test_draft_runs_one_pass_for_a_campaign(
    cli_env: None, database_url: str, tmp_path: Path, fake_claude_env: object, fake_dns: object
) -> None:
    from campaign_helpers import CLEAN, MARKER, ok_reply
    from conftest import FakeClaude

    assert isinstance(fake_claude_env, FakeClaude)
    _confirmed_facts_yaml(tmp_path)
    _seed_campaigns(database_url)
    fake_claude_env.reply(MARKER, ok_reply(CLEAN))

    result = runner.invoke(app, ["draft", "--campaign", "texas SOAP makers", "--limit", "5"])

    assert result.exit_code == 0, result.output
    # Old Co is already in the campaign; Acme Soap Works gets a draft.
    assert result.stdout.splitlines() == ["Drafted 1 email for review (0 held by the gate)."]
    assert len(fake_claude_env.calls()) == 1

    again = runner.invoke(app, ["draft", "--campaign", "Texas soap makers"])
    assert again.exit_code == 0, again.output
    assert again.stdout.splitlines() == ["Drafted 0 emails for review (0 held by the gate)."]


def test_draft_refusals(
    cli_env: None,
    database_url: str,
    tmp_path: Path,
    fake_claude_env: object,
    monkeypatch: pytest.MonkeyPatch,
    fake_dns: object,
) -> None:
    _confirmed_facts_yaml(tmp_path)
    _seed_campaigns(database_url)

    unknown = runner.invoke(app, ["draft", "--campaign", "Nope"])
    assert unknown.exit_code == 1
    assert "No campaign named “Nope”." in unknown.output

    paused = runner.invoke(app, ["draft", "--campaign", "Gujarat SLES"])
    assert paused.exit_code == 1
    assert "Campaign “Gujarat SLES” isn't active (it's paused)." in paused.output

    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--safe-mode")
    stopped = runner.invoke(app, ["draft", "--campaign", "Texas soap makers"])
    assert stopped.exit_code == 1
    assert stopped.stdout.splitlines() == [
        "Drafted 0 emails for review (0 held by the gate).",
        "Writing stopped: Claude CLI is missing isolation flags: --safe-mode. Run `claude update`.",
    ]


def test_draft_lists_skips_and_errors(
    cli_env: None, database_url: str, tmp_path: Path, fake_claude_env: object, fake_dns: object
) -> None:
    from conftest import FakeClaude

    assert isinstance(fake_claude_env, FakeClaude)
    _confirmed_facts_yaml(tmp_path)
    _seed_campaigns(database_url)
    fake_claude_env.reply("Acme Soap Works", {"exit": 3})

    result = runner.invoke(app, ["draft", "--campaign", "Texas soap makers"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "acme-soap.com: Claude exited with an error (code 3).",
        "Drafted 0 emails for review (0 held by the gate).",
    ]


def test_sending_status_pause_and_resume(cli_env: None, database_url: str) -> None:
    result = runner.invoke(app, ["sending", "status"])
    assert result.exit_code == 0, result.output
    assert "Sending is paused: Sending hasn't been turned on yet." in result.output

    refused = runner.invoke(app, ["sending", "resume"])
    assert refused.exit_code == 1
    assert "--yes" in refused.output

    resumed = runner.invoke(app, ["sending", "resume", "--yes"])
    assert resumed.exit_code == 0, resumed.output
    assert "Sending is on." in resumed.output
    again = runner.invoke(app, ["sending", "resume", "--yes"])
    assert again.exit_code == 0, again.output
    assert "Already on. Sending is on." in again.output

    paused = runner.invoke(app, ["sending", "pause", "--reason", "Checking DNS"])
    assert paused.exit_code == 0, paused.output
    assert "Sending is paused: Checking DNS" in paused.output
    default = runner.invoke(app, ["sending", "pause"])
    assert default.exit_code == 0, default.output
    assert "Already paused. Sending is paused: Checking DNS" in default.output

    async def _audit(s: AsyncSession) -> list[AuditLog]:
        rows = await s.scalars(
            select(AuditLog)
            .where(AuditLog.action.like("sending.%"))
            .order_by(AuditLog.at, AuditLog.action)
        )
        return list(rows.all())

    rows = asyncio.run(_signal_db(database_url, _audit))  # type: ignore[misc]
    assert sorted(r.action for r in rows) == ["sending.paused", "sending.resumed"]
    assert all(r.actor_id is None and r.details["source"] == "cli" for r in rows)


def test_runs_on_an_empty_database(cli_env: None) -> None:
    result = runner.invoke(app, ["runs"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == NEVER


def test_runs_lists_the_health_line_and_runs(cli_env: None, database_url: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    t0 = now - timedelta(hours=1)

    async def _seed() -> None:
        engine = make_engine(database_url)
        try:
            async with make_session_factory(engine)() as s:
                old = await runs_service.start_run(
                    s, now=t0, host="mac", pid=1, code_version=code_version()
                )
                await runs_service.record_pass(
                    s,
                    old.id,
                    PassResult(lines=["a"], failures=["Mail step failed: OSError"], quiet=False),
                    now=t0 + timedelta(minutes=1),
                    config=AppConfig(),
                )
                await runs_service.finish_run(
                    s, old.id, now=t0 + timedelta(minutes=5), reason="shutdown"
                )
                await runs_service.start_run(
                    s, now=now - timedelta(seconds=30), host="mac", pid=2,
                    code_version=code_version(),
                )  # fmt: skip
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    result = runner.invoke(app, ["runs", "--limit", "5"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "Worker running, first pass under way."
    stamp = "%Y-%m-%d %H:%M UTC"
    started = (now - timedelta(seconds=30)).strftime(stamp)
    assert lines[1] == f"{started} → running  Running  passes=0  failed=0"
    assert lines[2] == (
        f"{t0.strftime(stamp)} → {(t0 + timedelta(minutes=5)).strftime(stamp)}  Shut down  "
        "passes=1  failed=1"
    )
    assert len(lines) == 3

    one = runner.invoke(app, ["runs", "--limit", "1"])
    assert len(one.output.splitlines()) == 2
    assert runner.invoke(app, ["runs", "--limit", "0"]).exit_code == 2


def test_runs_shows_a_no_send_worker_and_a_stale_open_row(cli_env: None, database_url: str) -> None:
    """Final review M1 / final wave T3: a --no-send worker says so; an open row with no beat
    reads "No beat", never "Running"."""
    now = datetime.now(UTC).replace(microsecond=0)

    async def _seed(started: datetime, lines: list[str]) -> None:
        engine = make_engine(database_url)
        try:
            async with make_session_factory(engine)() as s:
                await s.execute(text("DELETE FROM runs"))
                run = await runs_service.start_run(
                    s, now=started, host="mac", pid=1, code_version=code_version()
                )
                await runs_service.record_pass(
                    s, run.id, PassResult(lines, [], False), now=started, config=AppConfig()
                )
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed(now - timedelta(seconds=30), [runs_service.NO_SEND_LINE]))
    lines = runner.invoke(app, ["runs"]).output.splitlines()
    assert lines[0] == (
        "Worker running with --no-send (nothing will be sent), last pass less than a minute ago."
    )
    assert "  Running with --no-send  passes=1" in lines[1]

    asyncio.run(_seed(now - timedelta(hours=1), ["a"]))
    lines = runner.invoke(app, ["runs"]).output.splitlines()
    assert lines[0].startswith("The worker stopped responding 60 min ago.")
    assert "  No beat  passes=1" in lines[1]


def test_ops_group_lists_plists() -> None:
    result = runner.invoke(app, ["ops", "--help"])
    assert result.exit_code == 0
    assert "plists" in result.output
