import ipaddress
import json
import logging
import os
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from salesfinder.auth import users
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.catalog import Catalog, Facts, load_catalog, load_facts
from salesfinder.config import AppConfig, load_config
from salesfinder.db.base import Base
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.db.migrate import upgrade_to_head
from salesfinder.db.models import User
from salesfinder.logging import QUIET_LOGGERS
from salesfinder.settings import Settings
from salesfinder.web.app import create_app

DEFAULT_TEST_DB_URL = "postgresql+asyncpg://localhost/salesfinder_test"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """CLI tests configure structlog, and the stdlib root logger (``configure_logging``:
    ``basicConfig(force=True)``), to print to pytest's captured stderr, which is closed after
    the test. Reset both, so later code that logs (e.g. aiosmtpd's server thread in test_smtp)
    never writes to a closed file ("--- Logging error ---")."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    yield
    structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, quiet_level in quiet.items():
        logging.getLogger(name).setLevel(quiet_level)


class RealDnsInTests(RuntimeError):
    """A test reached the real DNS resolver: inject a ``FakeResolver`` instead."""


def _no_real_dns(*_args: object, **_kwargs: object) -> NoReturn:
    raise RealDnsInTests("Tests never use real DNS: pass a FakeResolver (resolve=...).")


@pytest.fixture(autouse=True)
def _guard_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real MX lookups in tests: building the real resolver raises unless the test injects
    one (``MxChecker(config, resolve=FakeResolver(...))`` or its own monkeypatch)."""
    monkeypatch.setattr("salesfinder.contacts.dns.default_resolve", _no_real_dns)
    monkeypatch.setattr("dns.asyncresolver.Resolver", _no_real_dns)


class RealNetworkInTests(OSError):
    """A test opened (or resolved) a non-loopback address: inject the matching fake instead.
    An ``OSError`` so the code under test sees an ordinary network failure; every hit is also
    recorded and fails the test at teardown, so code that swallows the error can't hide it."""


_LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost"})


def _is_loopback(host: object) -> bool:
    if host is None:  # getaddrinfo(None, port): the local passive/loopback address
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _address_is_local(address: object) -> bool:
    """A ``connect``/``sendto`` address: (host, port[, flow, scope]) for IP sockets; a path for
    Unix sockets (always local)."""
    if isinstance(address, tuple):
        return bool(address) and _is_loopback(address[0])
    return isinstance(address, (str, bytes))


@pytest.fixture(autouse=True)
def _guard_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No network in tests: only 127.0.0.1, ::1 and localhost may be resolved or connected to
    (the test database, test_smtp's aiosmtpd and test_imap's TLS server). Covers every
    ``socket.socket`` (asyncio, httpx, imaplib, aiosmtplib and asyncpg all go through it) and
    ``socket.getaddrinfo`` (which asyncio's ``loop.getaddrinfo`` calls) and
    ``gethostbyname``/``gethostbyname_ex``; the profiling
    fetcher's real resolver raises too. Yields the list of hits (the guard's own tests clear
    it after checking)."""
    hits: list[str] = []

    def refuse(what: str, target: object) -> NoReturn:
        hits.append(f"{what} {target!r}")
        raise RealNetworkInTests(f"Tests never use the network ({what}): inject a fake.")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if not _address_is_local(address):
            refuse("connect", address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _address_is_local(address):
            refuse("connect", address)
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        address = args[-1] if args else None
        if not _address_is_local(address):
            refuse("sendto", address)
        return real_sendto(self, data, *args)  # type: ignore[call-overload,no-any-return]

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            refuse("getaddrinfo", host)
        return real_getaddrinfo(host, *args, **kwargs)

    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex

    def gethostbyname(host: Any) -> str:
        if not _is_loopback(host):
            refuse("gethostbyname", host)
        return real_gethostbyname(host)

    def gethostbyname_ex(host: Any) -> tuple[str, list[str], list[str]]:
        if not _is_loopback(host):
            refuse("gethostbyname_ex", host)
        return real_gethostbyname_ex(host)

    async def no_resolver(host: str) -> list[str]:
        refuse("profiling resolver", host)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)
    monkeypatch.setattr("salesfinder.profiling.fetch.default_resolver", no_resolver)
    yield hits
    if hits:
        pytest.fail(f"A test tried to use the network: {hits}")


class RealMailInTests(RuntimeError):
    """A test reached a real SMTP/IMAP server: inject ``fake_mail(...)`` instead."""


def _no_real_mail(*_args: object, **_kwargs: object) -> NoReturn:
    raise RealMailInTests("Tests never use real mail: inject tests.fakes.mail.fake_mail(...).")


@pytest.fixture(autouse=True)
def _guard_real_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real SMTP/IMAP in tests. test_smtp.py re-enables ``AioSmtpSender`` for 127.0.0.1 only;
    test_imap.py swaps in a stand-in ``IMAP4_SSL`` (and the real one only against its own TLS
    server on 127.0.0.1). ``_guard_real_network`` backs both up."""
    monkeypatch.setattr("salesfinder.mail.factory.default_mail", _no_real_mail)
    monkeypatch.setattr("salesfinder.mail.smtp.AioSmtpSender.send", _no_real_mail)
    monkeypatch.setattr("salesfinder.mail.smtp.AioSmtpSender.check", _no_real_mail)
    monkeypatch.setattr("imaplib.IMAP4_SSL", _no_real_mail)


class RealClaudeInTests(RuntimeError):
    """A test resolved a Claude CLI that isn't ``tests/fakes/fake_claude``: use the
    ``test_settings_with_fake_claude`` / ``fake_claude_env`` fixtures (or a ``runtime`` built on
    them) instead."""


@pytest.fixture(autouse=True)
def _guard_real_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Never run the real ``claude`` in tests: resolving any CLI other than the fake (or a
    stand-in a test wrote under pytest's base temp directory) raises before anything is
    spawned. Each hit is also recorded and fails the test at teardown, so code that catches
    the error (doctor isolates every check) can't hide it."""
    import salesfinder.brain.runtime as runtime_module

    original = runtime_module._resolve
    fake = os.path.realpath(FAKE_CLAUDE)
    base = os.path.realpath(tmp_path_factory.getbasetemp()) + os.sep
    hits: list[str] = []

    def guarded(claude_bin: str) -> str:
        found = original(claude_bin)
        real = os.path.realpath(found)
        if real != fake and not real.startswith(base):
            hits.append(real)
            raise RealClaudeInTests(
                "Tests never run the real claude: use the fake_claude fixtures."
            )
        return found

    monkeypatch.setattr(runtime_module, "_resolve", guarded)
    yield
    if hits:
        pytest.fail(f"A test tried to run a real Claude CLI: {hits}")


# What ``test_settings`` names as the Claude CLI: no such program, so anything that reaches
# for Claude without the fake fixtures fails closed ("Claude CLI not found on PATH.").
NO_CLAUDE = "salesfinder-test-no-claude"


def clear_salesfinder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every ``SALESFINDER_*`` variable the shell exported (except the test-database
    override), so a CLI test sees only what it sets itself: a new setting never leaks in."""
    for var in [v for v in os.environ if v.startswith("SALESFINDER_")]:
        if var != "SALESFINDER_TEST_DATABASE_URL":
            monkeypatch.delenv(var)


def assert_safe_test_database(url: str) -> None:
    """Refuse to run destructive fixtures (DROP SCHEMA / TRUNCATE) against a non-test database."""
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run destructive tests: database name {name!r} must end with '_test'."
        )
    real = os.environ.get("SALESFINDER_DATABASE_URL")
    if real and make_url(real).database == name:
        raise RuntimeError(
            f"Refusing to run destructive tests: {name!r} is the SALESFINDER_DATABASE_URL database."
        )


async def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    name = parsed.database
    assert name
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


@pytest_asyncio.fixture(scope="session")
async def database_url() -> str:
    url = os.environ.get("SALESFINDER_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)
    assert_safe_test_database(url)
    await _ensure_database(url)
    eng = create_async_engine(url)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await eng.dispose()
    await upgrade_to_head(url)
    return url


@pytest_asyncio.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(database_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))


TEST_PASSWORD = "correct horse battery"  # noqa: S105
TEST_SECRET_KEY = "test-secret-key-0123456789-abcdefghijklmnop"  # noqa: S105


@pytest.fixture
def password() -> str:
    return TEST_PASSWORD


@pytest.fixture(scope="session")
def shipped_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An absolute copy of the repo's shipped YAML config (never a developer's *.local.yaml),
    so tests don't depend on the working directory or on local overrides."""
    dest = tmp_path_factory.mktemp("config")
    copy_shipped_config(dest, include_app_config=True)
    return dest


@pytest.fixture(scope="session")
def shipped_config(shipped_config_dir: Path) -> AppConfig:
    """The shipped salesfinder.yaml only: a developer's salesfinder.local.yaml (git-ignored,
    and the README tells operators to make one) never reaches a test."""
    return load_config(shipped_config_dir)


@pytest.fixture(scope="session")
def shipped_catalog(shipped_config_dir: Path) -> Catalog:
    return load_catalog(shipped_config_dir)


@pytest.fixture(scope="session")
def shipped_facts(shipped_config_dir: Path) -> Facts:
    return load_facts(shipped_config_dir)


@pytest.fixture
def test_settings(database_url: str, shipped_config_dir: Path) -> Settings:
    """Hermetic settings: never reads a developer .env; every relevant field is explicit
    (``mailbox_passwords={}`` replaces an exported ``SALESFINDER_MAILBOX_PASSWORDS`` as a
    whole: Ruling P6-U, no merge)."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=database_url,
        config_dir=shipped_config_dir,
        secret_key=TEST_SECRET_KEY,
        bind_host="127.0.0.1",
        port=5555,
        tls_certfile=None,
        tls_keyfile=None,
        allowed_hosts=["127.0.0.1", "localhost"],
        public_url=None,
        session_ttl_hours=12,
        hubspot_token=None,
        serper_api_key=None,
        email_verifier_key=None,
        mailbox_passwords={},
        log_level="INFO",
        log_json=False,
        log_file=None,
        claude_bin=NO_CLAUDE,
    )


@pytest.fixture
def app(test_settings: Settings, engine: AsyncEngine) -> FastAPI:
    return create_app(
        test_settings,
        session_factory=make_session_factory(engine),
        rate_limiter=LoginRateLimiter(),
    )


@pytest_asyncio.fixture
async def client(app: FastAPI, db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    # Depends on ``db`` so the tables are truncated after each web test.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


@pytest_asyncio.fixture
async def admin(db: AsyncSession, password: str) -> User:
    user = await users.create_admin(db, email="boss@elchemy.com", name="Boss", password=password)
    await db.commit()
    return user


@pytest_asyncio.fixture
async def member(db: AsyncSession, admin: User, password: str) -> User:
    now = datetime.now(UTC)
    raw = await users.create_invite(
        db, email="member@elchemy.com", role="member", invited_by=admin, now=now
    )
    user = await users.accept_invite(db, raw, name="Mira Member", password=password, now=now)
    await db.commit()
    return user


SHIPPED_CONFIG = Path(__file__).resolve().parent.parent / "config"


def copy_shipped_config(dest: Path, *, include_app_config: bool = False) -> None:
    """catalog.yaml and facts.yaml (which doctor's config check also loads); with
    ``include_app_config`` also salesfinder.yaml. Never salesfinder.local.yaml."""
    names = ["catalog.yaml", "facts.yaml"]
    if include_app_config:
        names.append("salesfinder.yaml")
    for name in names:
        (dest / name).write_text((SHIPPED_CONFIG / name).read_text(encoding="utf-8"), "utf-8")


FAKE_CLAUDE = Path(__file__).parent / "fakes" / "fake_claude"
VALID_YAML = 'usage:\n  heartbeat_interval_minutes: 15\n  quiet_hours:\n    start: "22:00"\n'


@pytest.fixture
def test_settings_with_fake_claude(test_settings: Settings, tmp_path: Path) -> Settings:
    (tmp_path / "salesfinder.yaml").write_text(VALID_YAML, encoding="utf-8")
    copy_shipped_config(tmp_path)
    return test_settings.model_copy(
        update={"claude_bin": str(FAKE_CLAUDE.resolve()), "config_dir": tmp_path}
    )


@dataclass(frozen=True)
class FakeClaude:
    """Drives ``tests/fakes/fake_claude``: canned ``-p`` replies keyed by a prompt marker
    substring (first match in insertion order wins) and the JSON-lines log of every call."""

    log: Path
    replies: Path

    def reply(self, marker: str, reply: dict[str, Any]) -> None:
        current = json.loads(self.replies.read_text("utf-8")) if self.replies.exists() else {}
        current[marker] = reply
        self.replies.write_text(json.dumps(current), "utf-8")

    def calls(self, mode: str | None = "print") -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]
        return [e for e in entries if mode is None or e["mode"] == mode]


@pytest.fixture
def fake_claude_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude(log=tmp_path / "fake_claude.log", replies=tmp_path / "fake_claude.json")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_CLAUDE_REPLIES", str(fake.replies))
    monkeypatch.delenv("FAKE_CLAUDE_DROP", raising=False)
    return fake


@pytest_asyncio.fixture
async def client_with_hubspot_token(
    test_settings: Settings, engine: AsyncEngine, db: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    settings = test_settings.model_copy(update={"hubspot_token": SecretStr("test-token")})
    app = create_app(
        settings, session_factory=make_session_factory(engine), rate_limiter=LoginRateLimiter()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


@pytest.fixture
def confirmed_app(test_settings: Settings, engine: AsyncEngine) -> FastAPI:
    """An app whose facts, config and catalog are the campaign helpers' (confirmed personas and
    postal address, which the shipped facts.yaml lacks), so drafts seeded with those helpers
    pass the gate's exact signature + footer check."""
    from campaign_helpers import CATALOG, CONFIG, FACTS

    app = create_app(
        test_settings, session_factory=make_session_factory(engine), rate_limiter=LoginRateLimiter()
    )
    app.state.facts = FACTS
    app.state.config = CONFIG
    app.state.catalog = CATALOG
    return app


@pytest_asyncio.fixture
async def confirmed_client(
    confirmed_app: FastAPI, db: AsyncSession
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=confirmed_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c
