# SalesFinder Phase 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable, tested SalesFinder skeleton: settings + YAML config, Postgres schema via Alembic, multi-user auth (admin/member, invites, sessions, CSRF, rate limit), audit log, a dashboard shell with login and admin pages, a `salesfinder` CLI with `doctor`, and CI.

**Architecture:** `src/salesfinder` package. Pure logic (`auth/`, `settings.py`, `config.py`) is separate from I/O (`db/`, `web/`, `cli.py`). FastAPI serves server-rendered Jinja2 pages (HTMX vendored, no JS build). SQLAlchemy 2 async on asyncpg; Alembic owns the schema, and tests always build the test DB by running migrations.

**Tech Stack:** Python 3.13, uv, FastAPI, Uvicorn, Jinja2, HTMX 2.0.4, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2 + pydantic-settings, Typer, structlog, argon2-cffi, PyYAML, pytest + pytest-asyncio + httpx, ruff, mypy, pre-commit, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-07-elchemy-salesfinder-design.md` (§5 Constraints, §6.7 Dashboard multi-user, §8 Engineering practices, §9 milestones 1–2). Roadmap: `docs/superpowers/plans/2026-11-07-salesfinder-roadmap.md`.

## Global Constraints

- **Never run `git commit`, `git push`, or create branches.** The user commits. Leave changes in the working tree.
- Python `>=3.13,<3.14`; manage everything with `uv` (`uv sync`, `uv run …`); `uv.lock` is checked in.
- Postgres 17 (local Homebrew, current OS user, no password). Dev DB `salesfinder`, test DB `salesfinder_test`.
- All timestamps are timezone-aware UTC (`timestamptz`); all primary keys are UUID (`uuid.uuid4`).
- Secrets only in `.env` (git-ignored); `.env.example` documents every variable.
- Dashboard binds `127.0.0.1` by default. Any non-loopback bind **requires** TLS cert + key, else startup refuses (spec §6.7).
- Passwords: argon2id; minimum 12 characters. Session and invite tokens: stored only as SHA-256 hashes.
- Invites expire after 72 hours; sessions after `SALESFINDER_SESSION_TTL_HOURS` (default 12).
- Roles are exactly `admin` and `member`. No public sign-up.
- Every auth-relevant action writes an `audit_log` row (action names listed in Task 4).
- No network access in tests (the Claude CLI is faked with a script).
- ruff (lint + format, line length 100), mypy `strict = true` for `src/`.

## Review Focus

1. **Email typed with different case/whitespace** (`" Alice@Elchemy.com "`) at invite, login, and create-admin must resolve to the same account → tests in Task 4.
2. **Deactivated user who still holds a valid session cookie** must be signed out on the next request and the session revoked → tests in Tasks 4 and 5.
3. **Expired, already-used, or made-up invite links** must show a clear error and create nothing; inviting an email that already has a user must be refused → tests in Task 4 (service) and Task 5 (page).
4. **Requests with a Host not in `allowed_hosts`** (DNS rebinding) get 400; **POSTs with a foreign `Origin`** or a missing/wrong CSRF token get 403 → tests in Task 5.
5. **Binding to a LAN address without TLS files** must refuse to start with a message naming the two env vars → tests in Task 2 (settings) and Task 6 (`doctor`).

---

## File Structure

```
pyproject.toml, uv.lock, .python-version, Makefile, alembic.ini,
.env.example, .pre-commit-config.yaml, README.md
.github/workflows/ci.yml
config/salesfinder.yaml
src/salesfinder/
  __init__.py            # __version__
  settings.py            # env Settings (pydantic-settings) + LAN/TLS rule
  config.py              # YAML AppConfig, deep_merge, load_config
  logging.py             # configure_logging (structlog)
  db/
    base.py              # DeclarativeBase, naming convention, mixins
    engine.py            # make_engine, make_session_factory
    models/__init__.py   # imports all models (for Alembic autogenerate)
    models/auth.py       # User, UserSession, Invite
    models/audit.py      # AuditLog, AppSetting
    migrate.py           # upgrade_to_head(url), current_and_head(url)
    migrations/          # Alembic env.py (async) + versions/0001_initial.py
  auth/
    tokens.py            # new_token, hash_token
    passwords.py         # hash/verify/needs_rehash/password_problems
    emails.py            # normalize_email
    ratelimit.py         # LoginRateLimiter
    csrf.py              # csrf_token, verify_csrf
    sessions.py          # create/resolve/revoke sessions
    users.py             # invites, accept, authenticate, create_admin, set_active
  audit.py               # record()
  brain/cli_flags.py     # REQUIRED_FLAGS, missing_flags(help_text)
  doctor.py              # CheckResult, run_checks
  web/
    app.py               # create_app
    deps.py              # get_db, current_user, require_admin, check_csrf
    security.py          # SecurityHeadersMiddleware, origin check
    routes/auth.py       # /login, /logout, /invite/{token}
    routes/home.py       # /, /healthz
    routes/admin.py      # /admin/users, /admin/invites, /admin/audit
    templates/*.html     # base, login, today, invite, admin_users, admin_audit, error
    static/app.css, static/htmx.min.js
  cli.py                 # Typer app: web, db upgrade, users create-admin, doctor
tests/
  conftest.py            # test DB creation via migrations, truncation, app client
  test_scaffold.py, test_settings.py, test_config.py, test_migrations.py,
  test_auth_core.py, test_users.py, test_web_auth.py, test_web_admin.py,
  test_doctor.py, test_cli.py
  fakes/fake_claude      # executable script printing a --help text
```

---

### Task 1: Project scaffold and tooling

**Files:**
- Create: `pyproject.toml`, `.python-version`, `Makefile`, `.env.example`, `.pre-commit-config.yaml`, `src/salesfinder/__init__.py`, `tests/test_scaffold.py`
- Modify: `.gitignore` (already exists; ensure it contains `.env`, `.venv/`, `config/*.local.yaml`, `data/`, `logs/`, caches)

**Interfaces:**
- Produces: importable package `salesfinder` with `__version__ = "0.1.0"`; console script `salesfinder = "salesfinder.cli:app"` (module created in Task 6); dev commands `make setup|migrate|dev|test|lint|fmt|typecheck|check`.

- [ ] **Step 1: Write the failing test** — `tests/test_scaffold.py`

```python
import salesfinder

def test_version():
    assert salesfinder.__version__ == "0.1.0"
```

- [ ] **Step 2: Create `pyproject.toml`** with hatchling build backend, `src` layout, `requires-python = ">=3.13,<3.14"`, runtime deps: `fastapi>=0.115`, `uvicorn[standard]>=0.32`, `jinja2>=3.1`, `python-multipart>=0.0.12`, `sqlalchemy[asyncio]>=2.0.36`, `asyncpg>=0.30`, `alembic>=1.14`, `pydantic>=2.9`, `pydantic-settings>=2.6`, `email-validator>=2.2`, `typer>=0.12`, `structlog>=24.4`, `httpx>=0.27`, `argon2-cffi>=23.1`, `pyyaml>=6.0.2`. `[dependency-groups] dev`: `pytest>=8.3`, `pytest-asyncio>=1.0`, `ruff>=0.8`, `mypy>=1.13`, `pre-commit>=4.0`, `types-PyYAML`. Tool config: ruff `line-length = 100`, `select = ["E","F","I","UP","B","S","ASYNC","RUF"]` with `S101` ignored under `tests/`; mypy `strict = true`, `plugins = ["pydantic.mypy"]`; pytest `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "session"`, `asyncio_default_test_loop_scope = "session"`, `testpaths = ["tests"]`.

- [ ] **Step 3: Create `src/salesfinder/__init__.py`** (`__version__ = "0.1.0"`), `.python-version` (`3.13`), `.env.example` (every variable from Task 2's `Settings`, with comments; `SALESFINDER_DATABASE_URL=postgresql+asyncpg://localhost/salesfinder`, `SALESFINDER_SECRET_KEY=` with a note to generate via `python -c "import secrets;print(secrets.token_urlsafe(48))"`), `.pre-commit-config.yaml` (ruff, ruff-format, end-of-file-fixer, trailing-whitespace, check-added-large-files, detect-private-key), and `Makefile` (`setup`: `uv sync`, `uv run pre-commit install`, `createdb salesfinder` and `createdb salesfinder_test` if missing, copy `.env.example`→`.env` if missing; `migrate`: `uv run salesfinder db upgrade`; `dev`: `uv run salesfinder web --reload`; `test`, `lint`, `fmt`, `typecheck`, `check` = lint+typecheck+test).

- [ ] **Step 4: Install and run**

Run: `uv sync && uv run pytest tests/test_scaffold.py -v && uv run ruff check . && uv run ruff format --check .`
Expected: 1 passed; ruff reports no errors.

---

### Task 2: Settings and YAML config

**Files:**
- Create: `src/salesfinder/settings.py`, `src/salesfinder/config.py`, `config/salesfinder.yaml`
- Test: `tests/test_settings.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` (env prefix `SALESFINDER_`, reads `.env`): `database_url: str`; `secret_key: SecretStr` (min length 32); `bind_host: str = "127.0.0.1"`; `port: int = 5555`; `tls_certfile: Path | None = None`; `tls_keyfile: Path | None = None`; `allowed_hosts: list[str] = ["127.0.0.1", "localhost"]`; `config_dir: Path = Path("config")`; `log_level: str = "INFO"`; `log_json: bool = False`; `session_ttl_hours: int = 12`; `claude_bin: str = "claude"`. Property `lan_mode: bool` (True when `bind_host` not in `{"127.0.0.1", "::1", "localhost"}`). Property `https: bool` (both TLS files set).
  - `get_settings() -> Settings` (`functools.lru_cache`).
  - `class AppConfig(BaseModel)` (`extra="forbid"`) with `usage: UsageConfig` → `heartbeat_interval_minutes: int = 15`, `max_daily_claude_calls: int = 150`, `quiet_hours: QuietHours(start: time = 22:00, end: time = 07:00, timezone: str = "America/New_York")` (timezone validated with `zoneinfo.ZoneInfo`). Later phases add sections.
  - `class ConfigError(Exception)`.
  - `deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]`.
  - `load_config(config_dir: Path) -> AppConfig` — reads `salesfinder.yaml`, then deep-merges `salesfinder.local.yaml` if present.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py
import pytest
from pydantic import ValidationError
from salesfinder.settings import Settings

KEY = "k" * 40

def test_loopback_without_tls_is_fine(monkeypatch):
    s = Settings(database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert s.lan_mode is False and s.https is False

def test_lan_bind_without_tls_refuses():
    with pytest.raises(ValidationError) as e:
        Settings(database_url="postgresql+asyncpg://x/y", secret_key=KEY, bind_host="192.168.1.20")
    assert "SALESFINDER_TLS_CERTFILE" in str(e.value) and "SALESFINDER_TLS_KEYFILE" in str(e.value)

def test_lan_bind_with_tls_ok(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x"); key.write_text("x")
    s = Settings(database_url="postgresql+asyncpg://x/y", secret_key=KEY,
                 bind_host="0.0.0.0", tls_certfile=cert, tls_keyfile=key)
    assert s.lan_mode and s.https

def test_short_secret_rejected():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://x/y", secret_key="short")
```

```python
# tests/test_config.py
import pytest
from salesfinder.config import ConfigError, deep_merge, load_config

def test_deep_merge_recurses_dicts_and_replaces_lists():
    assert deep_merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}}) == {"a": {"b": 1, "c": [2]}}

def test_load_defaults(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  max_daily_claude_calls: 150\n")
    cfg = load_config(tmp_path)
    assert cfg.usage.max_daily_claude_calls == 150
    assert cfg.usage.heartbeat_interval_minutes == 15

def test_local_overrides(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  max_daily_claude_calls: 150\n")
    (tmp_path / "salesfinder.local.yaml").write_text("usage:\n  max_daily_claude_calls: 60\n")
    assert load_config(tmp_path).usage.max_daily_claude_calls == 60

def test_unknown_key_names_file_and_key(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  bogus: 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(tmp_path)

def test_bad_timezone(tmp_path):
    (tmp_path / "salesfinder.yaml").write_text("usage:\n  quiet_hours:\n    timezone: Mars/Base\n")
    with pytest.raises(ConfigError, match="Mars/Base"):
        load_config(tmp_path)

def test_missing_base_file(tmp_path):
    with pytest.raises(ConfigError, match="salesfinder.yaml"):
        load_config(tmp_path)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_settings.py tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: salesfinder.settings`).

- [ ] **Step 3: Implement `settings.py` and `config.py`** per the Interfaces block. The LAN rule is a `model_validator(mode="after")` raising `ValueError("LAN mode requires HTTPS: set SALESFINDER_TLS_CERTFILE and SALESFINDER_TLS_KEYFILE")`. `load_config` wraps YAML and Pydantic errors in `ConfigError` whose message includes the file name and the offending key/value. Create `config/salesfinder.yaml` containing the `usage` defaults with comments.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_settings.py tests/test_config.py -v`
Expected: all PASS.

---

### Task 3: Database layer, models, and initial migration

**Files:**
- Create: `src/salesfinder/db/base.py`, `db/engine.py`, `db/models/__init__.py`, `db/models/auth.py`, `db/models/audit.py`, `db/migrate.py`, `db/migrations/env.py`, `db/migrations/script.py.mako`, `db/migrations/versions/0001_initial.py`, `alembic.ini`, `tests/conftest.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces:
  - `Base(DeclarativeBase)` with `MetaData(naming_convention={"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"})`; `UUIDPk` mixin (`id: Mapped[uuid.UUID]`, default `uuid.uuid4`); `Timestamps` mixin (`created_at`, `updated_at`, `timestamptz`, server default `now()`, `updated_at` onupdate).
  - `make_engine(url: str) -> AsyncEngine`; `make_session_factory(engine) -> async_sessionmaker[AsyncSession]` (`expire_on_commit=False`).
  - Models (table names exact):
    - `User` → `users`: `email: str` (unique, stored normalized), `name: str`, `role: str` (check `role IN ('admin','member')`), `password_hash: str | None`, `is_active: bool = True`, `last_login_at: datetime | None`, + Timestamps.
    - `UserSession` → `user_sessions`: `user_id` FK `users.id` ON DELETE CASCADE (indexed), `token_hash: str` (unique, 64 hex), `created_at`, `expires_at`, `last_seen_at`, `revoked_at: datetime | None`, `ip: str | None`, `user_agent: str | None`.
    - `Invite` → `invites`: `email`, `role` (same check), `token_hash` (unique), `invited_by_id` FK users, `created_at`, `expires_at`, `accepted_at: datetime | None`.
    - `AuditLog` → `audit_log`: `at` (server default now, indexed), `actor_id` FK users nullable (indexed), `action: str(64)`, `target_type: str | None`, `target_id: str | None`, `details: dict` (JSONB, default `{}`), `ip: str | None`.
    - `AppSetting` → `app_settings`: `key: str` PK, `value` JSONB, `updated_at`, `updated_by_id` FK users nullable.
  - `upgrade_to_head(database_url: str) -> None`; `current_and_head(database_url: str) -> tuple[str | None, str]`.
  - Test fixtures in `conftest.py`: session-scoped `database_url` (from `SALESFINDER_TEST_DATABASE_URL`, default `postgresql+asyncpg://localhost/salesfinder_test`; creates the DB if missing, drops all tables, runs `upgrade_to_head`), session-scoped `engine`, function-scoped `db: AsyncSession` that truncates every table in `Base.metadata` (`TRUNCATE … CASCADE`) after each test.

- [ ] **Step 1: Write the failing tests** — `tests/test_migrations.py`

```python
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from salesfinder.db.base import Base
from salesfinder.db.migrate import current_and_head
from salesfinder.db.models import User

async def test_migrations_at_head(database_url):
    current, head = current_and_head(database_url)
    assert current == head

async def test_models_match_migrations(engine):
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    assert diff == []

async def test_email_unique(db):
    db.add(User(email="a@x.com", name="A", role="member"))
    await db.commit()
    db.add(User(email="a@x.com", name="B", role="member"))
    with pytest.raises(IntegrityError):
        await db.commit()

async def test_role_check(db):
    db.add(User(email="b@x.com", name="B", role="owner"))
    with pytest.raises(IntegrityError):
        await db.commit()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: FAIL (`ModuleNotFoundError: salesfinder.db`).

- [ ] **Step 3: Implement base, engine, models, `migrate.py`, async Alembic `env.py`** (`alembic.ini` sets `script_location = src/salesfinder/db/migrations`; `env.py` takes the URL from `config.attributes["database_url"]` or `SALESFINDER_DATABASE_URL`, `target_metadata = Base.metadata`, `compare_type=True`). `upgrade_to_head` and `current_and_head` build an Alembic `Config` programmatically (they must work from any working directory, so locate `alembic.ini` relative to the package). Then generate `0001_initial.py` with `uv run alembic revision --autogenerate -m initial --rev-id 0001` against an empty `salesfinder` dev DB and review it by hand (enum/check constraints, JSONB server default `'{}'::jsonb`, indexes).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_migrations.py -v`
Expected: 4 PASS.

---

### Task 4: Auth and audit services

**Files:**
- Create: `src/salesfinder/auth/{__init__,tokens,passwords,emails,ratelimit,csrf,sessions,users}.py`, `src/salesfinder/audit.py`
- Test: `tests/test_auth_core.py`, `tests/test_users.py`

**Interfaces:**
- Consumes: models from Task 3; `db` fixture.
- Produces:
  - `tokens.new_token() -> str` (`secrets.token_urlsafe(32)`); `tokens.hash_token(raw: str) -> str` (SHA-256 hex).
  - `passwords.hash_password(plain: str) -> str`; `passwords.verify_password(stored_hash: str, plain: str) -> bool` (never raises on mismatch); `passwords.needs_rehash(stored_hash: str) -> bool`; `passwords.password_problems(plain: str) -> list[str]` (empty = OK; only rule: `"Password must be at least 12 characters."`).
  - `emails.normalize_email(raw: str) -> str` (strip, lowercase, validate with `email_validator.validate_email(..., check_deliverability=False)`; raises `ValueError("Enter a valid email address.")`).
  - `ratelimit.LoginRateLimiter(max_failures: int = 5, window: timedelta = timedelta(minutes=15))` with `is_blocked(key: str, now: datetime) -> bool`, `record_failure(key: str, now: datetime) -> None`, `reset(key: str) -> None`. In-memory; key is `f"{email}|{ip}"`.
  - `csrf.csrf_token(secret: str, session_id: uuid.UUID) -> str` (HMAC-SHA256 hex); `csrf.verify_csrf(secret: str, session_id: uuid.UUID, token: str | None) -> bool` (`hmac.compare_digest`).
  - `sessions.create_session(db, user: User, *, ttl: timedelta, now: datetime, ip: str | None, user_agent: str | None) -> tuple[UserSession, str]` (row, raw token); `sessions.resolve_session(db, raw: str | None, *, now: datetime) -> tuple[UserSession, User] | None` (None when unknown, expired, revoked, or user inactive; an inactive user's session is revoked as a side effect; updates `last_seen_at`); `sessions.revoke_session(db, raw: str, *, now) -> None`; `sessions.revoke_all_for_user(db, user_id: uuid.UUID, *, now) -> int`.
  - `users.InviteError(Exception)` with `.reason: Literal["invalid", "expired", "used", "exists"]`.
  - `users.create_invite(db, *, email: str, role: str, invited_by: User, now: datetime, ttl: timedelta = timedelta(hours=72)) -> str` (raw token; raises `InviteError("exists")` if a user with that email exists).
  - `users.accept_invite(db, raw_token: str, *, name: str, password: str, now: datetime) -> User` (raises `InviteError` for invalid/expired/used; raises `ValueError` with `password_problems` text).
  - `users.create_admin(db, *, email: str, name: str, password: str) -> User`.
  - `users.authenticate(db, email: str, password: str, *, now: datetime) -> User | None` (None for unknown email, wrong password, inactive, or no password yet; runs a dummy `verify_password` when the user is missing; rehashes when `needs_rehash`; sets `last_login_at`).
  - `users.set_active(db, user_id: uuid.UUID, active: bool, *, now: datetime) -> User` (deactivating calls `revoke_all_for_user`).
  - `audit.record(db, *, action: str, actor_id: uuid.UUID | None, target_type: str | None = None, target_id: str | None = None, details: dict[str, Any] | None = None, ip: str | None = None) -> AuditLog`. Phase 1 action names: `user.login`, `user.login_failed`, `user.logout`, `invite.created`, `invite.accepted`, `user.deactivated`, `user.reactivated`, `user.admin_created`. Service functions do **not** commit; callers (routes/CLI) commit, so a service + its audit row share one transaction.

- [ ] **Step 1: Write the failing tests** — pure logic in `tests/test_auth_core.py`:

```python
from datetime import UTC, datetime, timedelta
import uuid, pytest
from salesfinder.auth import csrf, emails, passwords, ratelimit, tokens

def test_hash_token_is_sha256_hex():
    assert len(tokens.hash_token(tokens.new_token())) == 64

def test_password_roundtrip_and_rules():
    h = passwords.hash_password("correct horse battery")
    assert passwords.verify_password(h, "correct horse battery")
    assert not passwords.verify_password(h, "wrong password!!")
    assert passwords.password_problems("short") == ["Password must be at least 12 characters."]

def test_normalize_email():
    assert emails.normalize_email("  Alice@Elchemy.COM ") == "alice@elchemy.com"
    with pytest.raises(ValueError, match="valid email"):
        emails.normalize_email("not-an-email")

def test_rate_limiter_blocks_after_five_then_expires():
    rl, now = ratelimit.LoginRateLimiter(), datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(5):
        rl.record_failure("a|ip", now)
    assert rl.is_blocked("a|ip", now)
    assert not rl.is_blocked("a|ip", now + timedelta(minutes=16))
    assert not rl.is_blocked("b|ip", now)

def test_csrf():
    sid = uuid.uuid4()
    t = csrf.csrf_token("s" * 40, sid)
    assert csrf.verify_csrf("s" * 40, sid, t)
    assert not csrf.verify_csrf("s" * 40, uuid.uuid4(), t)
    assert not csrf.verify_csrf("s" * 40, sid, None)
```

…and DB-backed in `tests/test_users.py` (each uses the `db` fixture and a fixed `NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)`):

```python
async def test_invite_accept_login_with_messy_email(db): ...
    # admin = create_admin(email="Boss@Elchemy.com ")  -> admin.email == "boss@elchemy.com"
    # raw = create_invite(email=" New.User@Elchemy.com", role="member", invited_by=admin, now=NOW)
    # user = accept_invite(raw, name="New", password="long enough pass", now=NOW+1h)
    # assert user.email == "new.user@elchemy.com" and user.role == "member"
    # assert await authenticate(db, "NEW.USER@elchemy.com ", "long enough pass", now=NOW) == user

async def test_invite_expired_used_invalid(db): ...
    # accept at NOW+73h -> InviteError.reason == "expired"
    # accept twice -> second raises reason "used"
    # accept("made-up-token") -> reason "invalid"; no user rows created in any case

async def test_invite_for_existing_user_refused(db): ...
    # create_invite(email of existing user) -> InviteError reason "exists"

async def test_authenticate_rejects_wrong_unknown_inactive(db): ...
    # wrong password -> None; unknown email -> None; after set_active(False) -> None

async def test_session_lifecycle(db): ...
    # create_session(ttl=12h) -> resolve at NOW+11h returns (session, user)
    # resolve at NOW+13h -> None; revoke_session then resolve -> None

async def test_deactivation_revokes_sessions(db): ...
    # session created; set_active(user.id, False) -> resolve_session(raw) is None
    # and the row has revoked_at set

async def test_audit_record(db): ...
    # record(action="user.login", actor_id=u.id, ip="127.0.0.1", details={"a": 1})
    # row persisted with details == {"a": 1}
```

Write these as full test bodies with the assertions shown in the comments.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_auth_core.py tests/test_users.py -v`
Expected: FAIL (`ModuleNotFoundError: salesfinder.auth`).

- [ ] **Step 3: Implement the modules** per the Interfaces block. Use `argon2.PasswordHasher()` defaults (argon2id). Invite/session lookups go through `hash_token(raw)`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_auth_core.py tests/test_users.py -v`
Expected: all PASS.

---

### Task 5: Web app — login, admin users/invites, audit page

**Files:**
- Create: `src/salesfinder/web/{__init__,app,deps,security}.py`, `web/routes/{__init__,auth,home,admin}.py`, `web/templates/{base,login,today,invite,admin_users,admin_audit,error}.html`, `web/static/app.css`, `web/static/htmx.min.js` (download `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` once and vendor it)
- Modify: `tests/conftest.py` (add `settings` and `client` fixtures)
- Test: `tests/test_web_auth.py`, `tests/test_web_admin.py`

**Interfaces:**
- Consumes: Task 2 `Settings`; Task 3 engine/session factory; Task 4 services.
- Produces:
  - `create_app(settings: Settings, *, session_factory: async_sessionmaker[AsyncSession] | None = None, rate_limiter: LoginRateLimiter | None = None) -> FastAPI`.
  - Cookie `sf_session` (HttpOnly, SameSite=Lax, Path=/, `Secure` when `settings.https`, max-age = TTL).
  - Deps: `get_db`, `current_user` (unauthenticated HTML request → 303 to `/login?next=<path>`), `require_admin` (403), `check_csrf` (form field `csrf_token`; 403 on mismatch).
  - Middleware: Starlette `TrustedHostMiddleware(allowed_hosts=settings.allowed_hosts)` (→ 400); origin check on every non-GET: if `Origin` (else `Referer`) is present and its host is not in `allowed_hosts` → 403; `SecurityHeadersMiddleware` sets `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, and `Strict-Transport-Security: max-age=31536000` when `settings.https`.
  - Routes: `GET /healthz` → `{"status": "ok"}` after `SELECT 1` (503 `{"status": "db_unavailable"}` on failure); `GET/POST /login`; `POST /logout`; `GET /` (Today placeholder: "Signed in as {name}", nav); `GET /admin/users`; `POST /admin/invites` (form `email`, `role`; response shows the invite link once: `{request.base_url}invite/{token}`); `POST /admin/users/{user_id}/deactivate`, `/reactivate` (an admin cannot deactivate themselves → 400); `GET /admin/audit` (latest 200 rows); `GET/POST /invite/{token}`.
  - Copy (exact): login failure `"Email or password is incorrect."` (401); rate-limited `"Too many attempts. Try again in 15 minutes."` (429); invite errors: invalid `"This invite link isn't valid."`, expired `"This invite link has expired. Ask an admin for a new one."`, used `"This invite link has already been used."` (all 400).
  - Every route that changes state calls `audit.record` with the Task 4 action names, then commits.

- [ ] **Step 1: Write the failing tests.** `client` fixture: `httpx.AsyncClient(transport=ASGITransport(app=create_app(test_settings, session_factory=...)), base_url="http://127.0.0.1")`. Helper `login(client, email, password)` that GETs `/login` then POSTs. Tests in `tests/test_web_auth.py`:

```python
async def test_healthz(client): ...                 # 200 {"status": "ok"}
async def test_root_redirects_to_login(client): ... # 303, location startswith "/login"
async def test_login_success_sets_secure_cookie_flags(client, member): ...
    # 303 to "/"; "sf_session" set; "httponly" in set-cookie lower; "samesite=lax"
    # GET / -> 200 contains member.name; audit has "user.login"
async def test_login_wrong_password_generic_message(client, member): ...
    # 401, body contains "Email or password is incorrect."; same text for unknown email
async def test_login_rate_limited(client, member): ...
    # 5 bad attempts then correct password -> 429 "Too many attempts. Try again in 15 minutes."
async def test_logout_revokes_session(client, member): ...
    # POST /logout with csrf -> 303 /login; old cookie on GET / -> 303 /login
async def test_deactivated_user_kicked_out(client, member, db): ...
    # logged in; set_active(member.id, False) + commit; GET / -> 303 /login
async def test_bad_host_rejected(client): ...
    # GET / with headers {"host": "evil.example"} -> 400
async def test_foreign_origin_post_rejected(client, member): ...
    # POST /login with headers {"origin": "http://evil.example"} -> 403
async def test_missing_csrf_rejected(client, member): ...
    # logged in; POST /logout without csrf_token -> 403
async def test_security_headers(client): ...
    # GET /login -> "frame-ancestors 'none'" in CSP; X-Content-Type-Options == "nosniff"
```

`tests/test_web_admin.py`:

```python
async def test_member_cannot_open_admin(client, member): ...      # GET /admin/users -> 403
async def test_admin_invites_and_user_accepts(client, admin): ...
    # POST /admin/invites email="Rep@Elchemy.com", role="member" -> 200, page contains "/invite/"
    # extract token; new client GET /invite/{t} -> 200; POST name+password -> 303 "/"
    # audit contains "invite.created" and "invite.accepted"
async def test_invite_link_errors(client, admin): ...
    # made-up token -> 400 "This invite link isn't valid."
    # reused token -> 400 "This invite link has already been used."
async def test_admin_cannot_deactivate_self(client, admin): ...    # 400
async def test_audit_page_lists_actions(client, admin): ...       # contains "user.login"
```

Write full bodies with the assertions in the comments; `admin` and `member` fixtures create users via `create_admin` / invite+accept and return `(user, password)` or the user with a known password.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_web_auth.py tests/test_web_admin.py -v`
Expected: FAIL (`ModuleNotFoundError: salesfinder.web`).

- [ ] **Step 3: Implement the app, deps, middleware, routes, templates and CSS.** Templates extend `base.html`; every POST form includes `<input type="hidden" name="csrf_token">` (login and invite forms are protected by the origin check, since there's no session yet). `app.css`: tokens on `:root` with a `prefers-color-scheme: dark` override; readable at phone width. No inline scripts (CSP).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_web_auth.py tests/test_web_admin.py -v`
Expected: all PASS.

---

### Task 6: CLI, logging, and `doctor`

**Files:**
- Create: `src/salesfinder/cli.py`, `src/salesfinder/logging.py`, `src/salesfinder/brain/__init__.py`, `src/salesfinder/brain/cli_flags.py`, `src/salesfinder/doctor.py`, `tests/fakes/fake_claude` (executable)
- Modify: `tests/conftest.py` (add `test_settings_with_fake_claude`: `test_settings` copy with `claude_bin` = absolute path of `tests/fakes/fake_claude`, and `config_dir` = a tmp dir holding a valid `salesfinder.yaml`)
- Test: `tests/test_doctor.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Settings, `load_config`, `upgrade_to_head`, `current_and_head`, `create_admin`, `create_app`.
- Produces:
  - `brain.cli_flags.REQUIRED_FLAGS: tuple[str, ...] = ("--safe-mode", "--setting-sources", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config", "--tools", "--output-format")`; `missing_flags(help_text: str) -> list[str]` (Phase 5 reuses both).
  - `logging.configure_logging(level: str, json: bool) -> None` (structlog; JSON renderer when `json`, console otherwise).
  - `doctor.CheckResult(name: str, ok: bool, detail: str)` (frozen dataclass); `async run_checks(settings: Settings) -> list[CheckResult]` with checks named exactly: `config`, `database`, `migrations`, `admin_user`, `claude_cli`, `https`. `claude_cli` runs `[settings.claude_bin, "--help"]` (30 s timeout) and fails with the missing flag names, or with `"claude not found on PATH"`. `https` passes with `"loopback only"` when not `lan_mode`; in LAN mode it checks both TLS files exist and are readable.
  - Typer `app` with commands: `web [--reload]` (uvicorn with `ssl_certfile/ssl_keyfile` when `settings.https`), `db upgrade`, `users create-admin --email --name` (password prompted twice, hidden; rejects `password_problems`; audit `user.admin_created`), `doctor` (prints `✓`/`✗ name — detail` per check; exit code 1 if any fail).

- [ ] **Step 1: Write the failing tests.** `tests/fakes/fake_claude` is a `#!/bin/sh` script that prints a help text containing all `REQUIRED_FLAGS` except when env `FAKE_CLAUDE_DROP` names one to leave out.

```python
# tests/test_doctor.py
from salesfinder.brain.cli_flags import REQUIRED_FLAGS, missing_flags
from salesfinder.doctor import run_checks

def test_missing_flags():
    assert missing_flags(" ".join(REQUIRED_FLAGS)) == []
    assert missing_flags("--tools --output-format") == [f for f in REQUIRED_FLAGS
                                                        if f not in ("--tools", "--output-format")]

async def test_doctor_all_green(test_settings_with_fake_claude, admin): ...
    # every result ok; names == ["config","database","migrations","admin_user","claude_cli","https"]
async def test_doctor_flags_missing(test_settings_with_fake_claude, monkeypatch): ...
    # FAKE_CLAUDE_DROP=--safe-mode -> claude_cli not ok, "--safe-mode" in detail
async def test_doctor_claude_absent(test_settings): ...
    # claude_bin="definitely-not-claude" -> detail == "claude not found on PATH"
async def test_doctor_no_admin(test_settings_with_fake_claude): ...
    # admin_user not ok
async def test_doctor_lan_missing_cert_file(tmp_path, ...): ...
    # settings with bind_host="0.0.0.0" and cert path that's then deleted -> https not ok
```

```python
# tests/test_cli.py  (typer.testing.CliRunner; env points at the test DB)
def test_create_admin_and_doctor_exit_codes(...): ...
    # `users create-admin --email " Boss@Elchemy.com" --name Boss` with input "pw...\npw...\n"
    #   -> exit 0, user "boss@elchemy.com" exists with role admin
    # short password -> exit 1, output contains "at least 12 characters"
    # `doctor` with FAKE_CLAUDE_DROP=--tools -> exit 1, output contains "✗ claude_cli"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_doctor.py tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `cli_flags.py`, `logging.py`, `doctor.py`, `cli.py` per the Interfaces block. The CLI calls `configure_logging` from `Settings` on startup and runs async work with `asyncio.run`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_doctor.py tests/test_cli.py -v`
Expected: all PASS.

---

### Task 7: CI, README, and full verification

**Files:**
- Create: `.github/workflows/ci.yml`, `README.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write `.github/workflows/ci.yml`**: trigger on `push` and `pull_request`; `ubuntu-latest`; service `postgres:17` (`POSTGRES_PASSWORD: postgres`, health check `pg_isready`, port 5432); env `SALESFINDER_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/salesfinder_test`; steps: checkout, `astral-sh/setup-uv@v6` with `python-version: "3.13"`, `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -q`.

- [ ] **Step 2: Write `README.md`**: what SalesFinder is (two sentences, link to the spec), prerequisites (Python 3.13, uv, Postgres 17, Claude Code CLI logged in), setup (`make setup` → edit `.env` → `make migrate` → `uv run salesfinder users create-admin …` → `uv run salesfinder doctor` → `make dev`), inviting teammates, LAN mode (`SALESFINDER_BIND_HOST`, TLS via `mkcert`, `SALESFINDER_ALLOWED_HOSTS`), dev commands, project layout.

- [ ] **Step 3: Run the full check**

Run: `make check`
Expected: ruff clean, `mypy src` "Success: no issues found", all tests pass.

- [ ] **Step 4: Manual smoke run**

Run: `make migrate && uv run salesfinder users create-admin --email you@elchemy.com --name You && uv run salesfinder doctor && make dev`
Expected: `doctor` shows six `✓` lines; `http://127.0.0.1:5555/login` signs in and shows "Signed in as You"; `/admin/users` can create an invite link.

- [ ] **Step 5: Hand back** — report `git status --short` to the user for them to commit. Do not commit.
