"""Worker health on the web (Phase 7 Task 3): the banner on every page, the Today Worker card
and not-read alert, the Runs page and ``/healthz``'s ``worker``."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import seed_mailbox
from salesfinder.config import AppConfig
from salesfinder.db.models import Run, UsageEvent, User
from salesfinder.ops import health, runs
from salesfinder.ops.health import MISMATCH, NEVER
from salesfinder.ops.runs import PassResult
from salesfinder.ops.version import code_version
from salesfinder.sending.mailboxes import NOT_TESTED, SETTINGS_CHANGED
from salesfinder.settings import Settings
from web_helpers import login, text_of

CONFIG = AppConfig()
BANNER = 'class="alert alert-error worker-banner"'
NOT_READ = (
    "Not reading {}: its connection hasn't passed a test, so replies and opt-outs there wait "
    "unread. An admin should press Test connection on the Mailboxes page."
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _start(db: AsyncSession, now: datetime, *, version: str | None = None) -> Run:
    run = await runs.start_run(
        db, now=now, host="mac", pid=1, code_version=version or code_version()
    )
    await db.commit()
    return run


async def _signed_in(client: httpx.AsyncClient, user: User, password: str) -> None:
    resp = await login(client, user.email, password)
    assert resp.status_code == 303


# --------------------------------------------------------------------------- banner


async def test_fresh_install_shows_the_never_banner(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _signed_in(client, admin, password)
    page = await client.get("/companies")
    assert page.status_code == 200
    assert BANNER in page.text and NEVER in text_of(page)


async def test_banner_when_stale_and_not_when_running(
    client: httpx.AsyncClient, db: AsyncSession, member: User, password: str
) -> None:
    await _signed_in(client, member, password)
    run = await _start(db, _now() - timedelta(minutes=10))
    await runs.beat(db, run.id, now=_now() - timedelta(minutes=10), step="Writing",
                    step_started_at=None)  # fmt: skip
    await db.commit()
    page = await client.get("/companies")
    assert BANNER in page.text
    assert "The worker stopped responding 10 min ago (during Writing)." in text_of(page)

    await runs.beat(db, run.id, now=_now(), step=None, step_started_at=None)
    await db.commit()
    page = await client.get("/companies")
    assert BANNER not in page.text and "worker-banner" not in page.text


async def test_mismatch_banner(
    client: httpx.AsyncClient, app: FastAPI, db: AsyncSession, admin: User, password: str
) -> None:
    await _signed_in(client, admin, password)
    await _start(db, _now(), version="old000000000")
    page = await client.get("/campaigns")
    assert BANNER in page.text and MISMATCH in text_of(page)
    app.state.code_version = "old000000000"
    assert BANNER not in (await client.get("/campaigns")).text


async def test_banner_not_on_htmx_fragments_or_signed_out_pages(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    assert "worker-banner" not in (await client.get("/login")).text
    await _signed_in(client, admin, password)
    page = await client.get("/companies", headers={"HX-Request": "true"})
    assert "worker-banner" not in page.text


async def test_a_failing_health_read_never_breaks_a_page(
    client: httpx.AsyncClient,
    admin: User,
    password: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _signed_in(client, admin, password)

    async def broken(db: AsyncSession, **_: object) -> health.WorkerHealth:
        await db.execute(text("SELECT 1/0"))  # a real database error, mid-transaction
        raise AssertionError("unreachable")

    monkeypatch.setattr(health, "worker_health", broken)
    for path in ("/companies", "/campaigns", "/runs", "/"):
        page = await client.get(path)
        assert page.status_code == 200, path
        assert BANNER not in page.text
    # The session still works afterwards (the savepoint was rolled back, the request committed).
    assert (await client.get("/companies")).status_code == 200


# --------------------------------------------------------------------------- Today


async def test_today_worker_card_shows_failures(
    client: httpx.AsyncClient, db: AsyncSession, admin: User, password: str
) -> None:
    await _signed_in(client, admin, password)
    now = _now()
    run = await _start(db, now - timedelta(minutes=2))
    await runs.record_pass(
        db,
        run.id,
        PassResult(
            ["Mail step failed: <TimeoutError>"], ["Mail step failed: <TimeoutError>"], False
        ),
        now=now - timedelta(minutes=1),
        config=CONFIG,
    )
    await db.commit()
    page = await client.get("/")
    body = text_of(page)
    assert "Running — last pass 1 min ago" in body
    assert "Failures in the last 24 h: 1" in body
    assert "Mail step failed: <TimeoutError>" in body
    assert "<TimeoutError>" not in page.text  # escaped
    assert 'href="/runs"' in page.text
    assert BANNER not in page.text


async def test_today_worker_card_shows_the_problem_once(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _signed_in(client, admin, password)
    page = await client.get("/")
    assert text_of(page).count(NEVER) == 1
    assert "Failures in the last 24 h: 0" in text_of(page)


@pytest.mark.parametrize("hold", [NOT_TESTED, SETTINGS_CHANGED])
async def test_today_not_read_alert(
    client: httpx.AsyncClient,
    app: FastAPI,
    test_settings: Settings,
    db: AsyncSession,
    admin: User,
    password: str,
    hold: str,
) -> None:
    held = await seed_mailbox(db, admin, address="held@send.example.org", secret_name="held")  # noqa: S106
    held.hold_reason = hold
    nopw = await seed_mailbox(db, admin, address="nopw@send.example.org", secret_name="nopw")  # noqa: S106
    nopw.hold_reason = NOT_TESTED
    tested = await seed_mailbox(db, admin, address="ok@send.example.org", secret_name="ok")  # noqa: S106
    await db.commit()
    app.state.settings = test_settings.model_copy(
        update={"mailbox_passwords": {"held": SecretStr("pw"), "ok": SecretStr("pw2")}}
    )
    await _signed_in(client, admin, password)
    body = text_of(await client.get("/"))
    assert NOT_READ.format("held@send.example.org") in body
    assert "Not reading nopw@" not in body  # no password: never read anyway, no alert
    assert f"Not reading {tested.address}" not in body
    assert "pw2" not in body


# --------------------------------------------------------------------------- Runs page


async def _seed_runs(db: AsyncSession, count: int, *, start: datetime) -> list[Run]:
    made = []
    for i in range(count):
        at = start + timedelta(minutes=20 * i)
        run = Run(
            started_at=at,
            last_beat_at=at + timedelta(minutes=15),
            finished_at=at + timedelta(minutes=15),
            end_reason="window",
            passes=i,
            host="mac",
            pid=1,
            code_version=code_version(),
            lines=[{"at": at.isoformat(), "text": f"line of run {i}"}],
        )
        db.add(run)
        made.append(run)
    await db.commit()
    return made


async def test_runs_page_lists_newest_first_and_escapes(
    client: httpx.AsyncClient, db: AsyncSession, member: User, password: str
) -> None:
    now = _now()
    t0 = now - timedelta(hours=2)
    old = Run(
        started_at=t0,
        last_beat_at=t0 + timedelta(minutes=15),
        finished_at=t0 + timedelta(minutes=15),
        end_reason="crashed",
        passes=7,
        failed_steps=2,
        host="mac",
        pid=1,
        code_version=code_version(),
        lines=[{"at": t0.isoformat(), "text": "Writing step failed: <script>alert(1)</script>"}],
        lines_dropped=50,
        quiet=True,
    )
    db.add(old)
    db.add(UsageEvent(at=t0 + timedelta(minutes=1), task="write", ok=True, duration_ms=1,
                      cost_usd=Decimal("0.0150")))  # fmt: skip
    db.add(UsageEvent(at=t0 + timedelta(minutes=2), task="write", ok=False, duration_ms=1))
    await db.commit()
    await _start(db, now - timedelta(seconds=5))
    await _signed_in(client, member, password)

    page = await client.get("/runs")
    assert page.status_code == 200
    body = text_of(page)
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "+50 more lines" in body
    assert body.index("Running") < body.index("Crashed")
    assert "Quiet hours" in body
    row = body[body.index("Crashed") :]
    assert "2 calls · $0.02" in row  # two calls in its window, cost rounded to cents
    assert 'aria-current="page"' in page.text and 'href="/runs"' in page.text


async def test_runs_page_summary(
    client: httpx.AsyncClient, db: AsyncSession, admin: User, password: str
) -> None:
    now = _now()
    run = await _start(db, now - timedelta(minutes=1))
    await runs.record_pass(
        db, run.id, PassResult(["x"], ["Sweep step failed: KeyError"], False), now=now,
        config=CONFIG,
    )  # fmt: skip
    db.add(UsageEvent(at=now, task="write", ok=True, duration_ms=1))
    await db.commit()
    await _signed_in(client, admin, password)
    body = text_of(await client.get("/runs"))
    assert "Running — last pass less than a minute ago" in body
    assert f"1 / {CONFIG.usage.max_daily_claude_calls}" in body
    assert "$0.00 / $" in body
    assert "Failures in the last 24 h: 1" in body


async def test_runs_page_pages(
    client: httpx.AsyncClient, db: AsyncSession, admin: User, password: str
) -> None:
    await _seed_runs(db, 26, start=_now() - timedelta(days=1))
    await _signed_in(client, admin, password)
    first = text_of(await client.get("/runs"))
    assert "line of run 25" in first and "line of run 1" in first
    assert "line of run 0" not in first
    assert "/runs?page=2" in first
    second = text_of(await client.get("/runs?page=2"))
    assert "line of run 0" in second and "line of run 1" not in second
    assert "/runs?page=1" in second
    empty = await client.get("/runs?page=10000")
    assert empty.status_code == 200


@pytest.mark.parametrize("page", ["0", "-1", "10001", "99999999999999999999", "x"])
async def test_runs_page_bounds(
    client: httpx.AsyncClient, admin: User, password: str, page: str
) -> None:
    await _signed_in(client, admin, password)
    assert (await client.get(f"/runs?page={page}")).status_code == 400


async def test_runs_page_needs_sign_in(client: httpx.AsyncClient, db: AsyncSession) -> None:
    resp = await client.get("/runs")
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


# --------------------------------------------------------------------------- healthz


async def test_healthz_reports_the_worker(client: httpx.AsyncClient, db: AsyncSession) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "worker": "never"}
    await _start(db, _now())
    assert (await client.get("/healthz")).json() == {"status": "ok", "worker": "running"}


async def test_healthz_worker_unknown_when_only_its_read_fails(
    client: httpx.AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(db: AsyncSession, **_: object) -> health.WorkerHealth:
        await db.execute(text("SELECT 1/0"))
        raise AssertionError("unreachable")

    monkeypatch.setattr(health, "worker_health", broken)
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "worker": "unknown"}


# --------------------------------------------------------------------------- final fix wave


async def test_a_stale_open_row_is_not_a_green_running(
    client: httpx.AsyncClient, db: AsyncSession, admin: User, password: str
) -> None:
    """Final wave T3: the open row of a worker that stopped beating reads "No beat"."""
    await _start(db, _now() - timedelta(minutes=10))
    await _signed_in(client, admin, password)
    page = await client.get("/runs")
    assert '<span class="badge badge-off">No beat</span>' in page.text
    assert '<span class="badge badge-ok">Running</span>' not in page.text


async def test_a_no_send_worker_shows_on_today_and_runs(
    client: httpx.AsyncClient, db: AsyncSession, admin: User, password: str
) -> None:
    """Final review M1 / Ruling P7-J: a leftover --no-send worker is visible."""
    from salesfinder.ops.runs import NO_SEND_LINE

    now = _now()
    run = await _start(db, now - timedelta(minutes=2))
    await runs.record_pass(
        db, run.id, PassResult([NO_SEND_LINE], [], False), now=now - timedelta(minutes=1),
        config=CONFIG,
    )  # fmt: skip
    await db.commit()
    await _signed_in(client, admin, password)
    for path in ("/", "/runs"):
        page = await client.get(path)
        assert "Running with --no-send (nothing will be sent) — last pass 1 min ago" in text_of(
            page
        ), path
        assert "badge-warn" in page.text
    assert (
        '<span class="badge badge-warn">Running with --no-send</span>'
        in (await client.get("/runs")).text
    )


def _count_health_reads(app_engine: AsyncEngine) -> tuple[list[str], Callable[[], None]]:
    """Statements that read worker health (its query labels a ``no_send`` column)."""
    from sqlalchemy import event

    engine = app_engine.sync_engine
    seen: list[str] = []

    def before(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if "AS no_send" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine, "before_cursor_execute", before)


@pytest.mark.parametrize("path", ["/", "/runs", "/companies", "/campaigns"])
async def test_one_health_read_per_page(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    db: AsyncSession,
    admin: User,
    password: str,
    path: str,
) -> None:
    """Final wave T3: the banner costs one statement per page, and Today and Runs reuse the
    health the request already read for it."""
    await _start(db, _now())
    await _signed_in(client, admin, password)
    seen, stop = _count_health_reads(engine)
    try:
        page = await client.get(path)
    finally:
        stop()
    assert page.status_code == 200
    assert len(seen) == 1, path


async def test_banner_on_error_pages(
    client: httpx.AsyncClient, engine: AsyncEngine, db: AsyncSession, admin: User, password: str
) -> None:
    """Final wave T3: a 404 page (a GET) carries the worker banner; a form post's error page
    reads no health (the banner returns with the next page)."""
    import uuid

    from web_helpers import csrf_for

    await _start(db, _now() - timedelta(minutes=10))
    await _signed_in(client, admin, password)
    missing = await client.get(f"/outbox/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert BANNER in missing.text and "The worker stopped responding" in text_of(missing)

    token = await csrf_for(client, "/companies")
    seen, stop = _count_health_reads(engine)
    try:
        failed = await client.post(f"/outbox/{uuid.uuid4()}/approve", data={"csrf_token": token})
    finally:
        stop()
    assert failed.status_code == 404
    assert BANNER not in failed.text and seen == []


async def test_not_read_matches_what_poll_mailboxes_skips(
    engine: AsyncEngine,
    db: AsyncSession,
    test_settings: Settings,
    admin: User,
) -> None:
    """Final wave T3: Today's "Not reading …" alert names exactly the mailboxes the worker's
    mail step skips as untested; every other mailbox with a password is read."""
    from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
    from salesfinder.db.engine import make_session_factory
    from salesfinder.replies.ingest import poll_mailboxes
    from salesfinder.sending.mailboxes import not_read

    states = {
        "tested@send.example.org": (None, True, True),
        "fresh@send.example.org": (NOT_TESTED, True, False),
        "moved@send.example.org": (SETTINGS_CHANGED, True, True),
        "nopw@send.example.org": (NOT_TESTED, False, False),
        "bounced@send.example.org": ("Paused: too many bounces.", True, False),
        "off@send.example.org": (None, True, False),
    }
    passwords = {}
    for address, (hold, has_password, enabled) in states.items():
        name = address.split("@")[0]
        box = await seed_mailbox(db, admin, address=address, secret_name=name, enabled=enabled)
        box.hold_reason = hold
        if has_password:
            passwords[name] = SecretStr("pw")
    await db.commit()
    settings = test_settings.model_copy(update={"mailbox_passwords": passwords})

    listed = await not_read(db, settings, CONFIG)
    reader = FakeReader({"INBOX": FakeFolder(1), "[Gmail]/Sent Mail": FakeFolder(1)})
    outcomes = await poll_mailboxes(
        make_session_factory(engine),
        mail=fake_mail(FakeSender(), reader),
        settings=settings,
        config=CONFIG,
        now=_now,
    )

    assert listed == sorted(a for a, o in outcomes.items() if o.untested)
    assert listed == ["fresh@send.example.org", "moved@send.example.org"]
    read = {a for a, o in outcomes.items() if not o.untested}
    assert read == {"tested@send.example.org", "bounced@send.example.org", "off@send.example.org"}
