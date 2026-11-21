"""Request dependencies: DB session, config, authentication, admin gate, CSRF; plus cookies."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

import structlog
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.auth.csrf import csrf_token, verify_csrf
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.auth.sessions import resolve_session
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.db.models import User, UserSession
from salesfinder.ops import health
from salesfinder.outbox.service import outbox_counts
from salesfinder.replies.conversations import needs_you_count
from salesfinder.sending.state import get_state as get_sending_state
from salesfinder.settings import Settings

log = structlog.get_logger(__name__)

SESSION_COOKIE = "sf_session"
# Record a session's activity at most once a minute (HTMX polling would write every 3 s).
LAST_SEEN_EVERY = timedelta(seconds=60)


def utcnow() -> datetime:
    return datetime.now(UTC)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    return cast(LoginRateLimiter, request.app.state.rate_limiter)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        yield session


def get_config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def get_catalog(request: Request) -> Catalog:
    return cast(Catalog, request.app.state.catalog)


def get_facts(request: Request) -> Facts:
    return cast(Facts, request.app.state.facts)


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
ConfigDep = Annotated[AppConfig, Depends(get_config)]
CatalogDep = Annotated[Catalog, Depends(get_catalog)]
FactsDep = Annotated[Facts, Depends(get_facts)]


@dataclass(frozen=True)
class AuthContext:
    """The signed-in user, their live session and the session-bound CSRF token."""

    user: User
    session: UserSession
    csrf_token: str
    # First emails in the Outbox, ready and held (one query, on full-page GETs only: the nav
    # and the Today page read them).
    outbox_ready: int = 0
    outbox_held: int = 0
    # Conversations flagged "needs you" (the nav's Conversations count; full-page GETs only).
    needs_you: int = 0
    # Why sending is paused, for the banner on every page (None: sending is on, or not read
    # because this isn't a full-page GET).
    sending_paused: str | None = None
    # What's wrong with the background worker, for the banner on every page (None: it's
    # running, its health couldn't be read, or this isn't a full-page GET).
    worker_problem: str | None = None
    # The worker health read for that banner (None: not read, or the read failed), and
    # whether it was read: Today and Runs reuse it instead of reading it again.
    worker: health.WorkerHealth | None = None
    worker_read: bool = False

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"

    @property
    def outbox_count(self) -> int:
        return self.outbox_ready + self.outbox_held


class LoginRequired(Exception):
    """Raised by ``current_auth``; the app turns it into a 303 to ``/login?next=...``."""

    def __init__(self, next_path: str | None, *, clear_cookie: bool) -> None:
        super().__init__(next_path)
        self.next_path = next_path
        self.clear_cookie = clear_cookie


async def optional_auth(request: Request, db: DbDep, settings: SettingsDep) -> AuthContext | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    resolved = await resolve_session(db, raw, now=utcnow(), touch_after=LAST_SEEN_EVERY)
    counts = {"awaiting_approval": 0, "gate_failed": 0}
    needs_you = 0
    paused: str | None = None
    worker: health.WorkerHealth | None = None
    worker_read = False
    if resolved is not None and _full_page(request):
        counts = await outbox_counts(db)
        needs_you = await needs_you_count(db)
        sending = await get_sending_state(db)
        paused = sending.reason if sending.paused else None
        worker, worker_read = await read_worker_health(request, db), True
    # Persist last_seen_at, or the revocation of an inactive user's session (Ruling E). With
    # nothing changed this only ends the read transaction.
    await db.commit()
    if resolved is None:
        return None
    session, user = resolved
    token = csrf_token(settings.secret_key.get_secret_value(), session.id)
    return AuthContext(
        user=user,
        session=session,
        csrf_token=token,
        outbox_ready=counts["awaiting_approval"],
        outbox_held=counts["gate_failed"],
        needs_you=needs_you,
        sending_paused=paused,
        worker_problem=worker.problem if worker is not None else None,
        worker=worker,
        worker_read=worker_read,
    )


async def read_worker_health(request: Request, db: AsyncSession) -> health.WorkerHealth | None:
    """The worker's health against this app's code version, or None if reading it failed: the
    read runs in a savepoint and any error is logged (type only) and swallowed, so a page (and
    the request's transaction) never fails over the banner."""
    try:
        async with db.begin_nested():
            return await health.worker_health(
                db,
                now=utcnow(),
                config=cast(AppConfig, request.app.state.config),
                code_version=cast(str, request.app.state.code_version),
            )
    except Exception as exc:
        log.warning("worker_health_failed", error_type=type(exc).__name__)
        return None


async def page_worker_health(
    request: Request, db: AsyncSession, auth: "AuthContext"
) -> health.WorkerHealth | None:
    """The worker health this request already read for the banner (a full-page GET), else a
    fresh ``read_worker_health``: one read per page."""
    if auth.worker_read:
        return auth.worker
    return await read_worker_health(request, db)


def _full_page(request: Request) -> bool:
    """A GET that renders the whole page (and so the nav), not an HTMX fragment or a form post."""
    return request.method in ("GET", "HEAD") and request.headers.get("HX-Request") != "true"


OptionalAuthDep = Annotated[AuthContext | None, Depends(optional_auth)]


async def current_auth(request: Request, auth: OptionalAuthDep) -> AuthContext:
    if auth is None:
        next_path: str | None = None
        if request.method in ("GET", "HEAD"):
            next_path = request.url.path
            if request.url.query:
                next_path += f"?{request.url.query}"
        raise LoginRequired(next_path, clear_cookie=SESSION_COOKIE in request.cookies)
    return auth


AuthDep = Annotated[AuthContext, Depends(current_auth)]


async def current_user(auth: AuthDep) -> User:
    return auth.user


async def require_admin(auth: AuthDep) -> AuthContext:
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can open this page.")
    return auth


AdminDep = Annotated[AuthContext, Depends(require_admin)]


CSRF_HEADER = "X-CSRF-Token"


async def check_csrf(request: Request, auth: AuthDep, settings: SettingsDep) -> AuthContext:
    """The session-bound token from the ``csrf_token`` form field or, for HTMX requests, the
    ``X-CSRF-Token`` header; either one valid is enough."""
    form = await request.form()
    candidates = (form.get("csrf_token"), request.headers.get(CSRF_HEADER))
    secret = settings.secret_key.get_secret_value()
    if not any(
        isinstance(token, str) and verify_csrf(secret, auth.session.id, token)
        for token in candidates
    ):
        raise HTTPException(
            status_code=403, detail="Your form expired. Go back, reload the page and try again."
        )
    return auth


CsrfDep = Annotated[AuthContext, Depends(check_csrf)]


async def rollback_keeping_auth(db: AsyncSession, auth: AuthContext) -> None:
    """Roll back a refused change and reload the signed-in user and session (a rollback expires
    them, and the page re-rendered next reads them for the nav)."""
    await db.rollback()
    await db.refresh(auth.user)
    await db.refresh(auth.session)


def set_session_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=settings.https,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.https, httponly=True, samesite="lax"
    )


# Postgres deadlock_detected and serialization_failure: the transaction lost a race with another
# one touching the same rows and was rolled back; running it again usually succeeds.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
CONCURRENT_CHANGE = "Someone else changed this at the same moment — please try again."


def is_concurrent_conflict(exc: BaseException) -> bool:
    """A deadlock or serialization failure reported by the database."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return state in RETRYABLE_SQLSTATES


async def retry_on_conflict[T](
    db: AsyncSession, auth: AuthContext, attempt: Callable[[], Awaitable[T]]
) -> T:
    """Run ``attempt`` (the whole operation, commit included); after a deadlock or
    serialization failure roll back and run it once more. A second failure is a 409 with
    ``CONCURRENT_CHANGE``, never a 500. Any other error propagates."""
    for tries in (1, 2):
        try:
            return await attempt()
        except DBAPIError as exc:
            if not is_concurrent_conflict(exc):
                raise
            log.warning("db_conflict", attempt=tries)
            await rollback_keeping_auth(db, auth)
    raise HTTPException(status_code=409, detail=CONCURRENT_CHANGE)
