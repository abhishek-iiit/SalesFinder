"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.catalog import load_catalog, load_facts
from salesfinder.config import load_config
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.ops.version import code_version
from salesfinder.settings import Settings
from salesfinder.web.deps import AuthContext, LoginRequired, clear_session_cookie, optional_auth
from salesfinder.web.routes import (
    admin,
    auth,
    campaigns,
    catalog,
    cohorts,
    companies,
    conversations,
    discover,
    home,
    mailboxes,
    outbox,
    runs,
    signals,
)
from salesfinder.web.security import OriginCheckMiddleware, SecurityHeadersMiddleware
from salesfinder.web.templating import STATIC_DIR, render

_STATUS_TITLES = {
    400: "Bad request",
    403: "Not allowed",
    404: "Not found",
    405: "Not allowed",
    409: "Try again",
}


def create_app(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    rate_limiter: LoginRateLimiter | None = None,
) -> FastAPI:
    """Build the app. A supplied ``session_factory`` must use ``expire_on_commit=False``.

    The YAML config, catalog and facts are read once, here; a ``ConfigError`` propagates, so an
    app with a broken config never starts (edit the files, then restart).
    """
    config = load_config(settings.config_dir)
    product_catalog = load_catalog(settings.config_dir)
    facts = load_facts(settings.config_dir)

    owned_engine: AsyncEngine | None = None
    if session_factory is None:
        owned_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(owned_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_engine is not None:
            await owned_engine.dispose()

    app = FastAPI(
        title="SalesFinder", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.config = config
    app.state.catalog = product_catalog
    app.state.facts = facts
    app.state.session_factory = session_factory
    app.state.rate_limiter = rate_limiter or LoginRateLimiter()
    # This process's code fingerprint; the worker banner compares the worker's with it (tests
    # set another value to show the version-mismatch banner).
    app.state.code_version = code_version()
    # The Outbox's MX checker, built on first use (tests put one with a fake resolver here).
    app.state.mx_checker = None
    # The mail transport (a MailFactory) and DNS resolver the Mailboxes page uses, built on first
    # use (tests put fakes here).
    app.state.mail = None
    app.state.dns_resolve = None

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(home.router)
    app.include_router(auth.router)
    app.include_router(discover.router)
    app.include_router(companies.router)
    app.include_router(catalog.router)
    app.include_router(signals.router)
    app.include_router(cohorts.router)
    app.include_router(campaigns.router)
    app.include_router(outbox.router)
    app.include_router(conversations.router)
    app.include_router(admin.router)
    app.include_router(mailboxes.router)
    app.include_router(runs.router)

    async def login_required(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, LoginRequired):  # pragma: no cover - registered for this type
            raise exc
        if request.headers.get("HX-Request") == "true":
            # An HTMX request (e.g. status polling) would swap the login page into a fragment;
            # ask HTMX to navigate the whole window instead.
            response: Response = Response(status_code=200, headers={"HX-Redirect": "/login"})
            if exc.clear_cookie:
                clear_session_cookie(response, settings)
            return response
        target = "/login"
        if exc.next_path and exc.next_path != "/":
            target += "?" + urlencode({"next": exc.next_path})
        response = RedirectResponse(target, status_code=303)
        if exc.clear_cookie:
            clear_session_cookie(response, settings)
        return response

    async def _signed_in(request: Request) -> AuthContext | None:
        """The requester's auth context, so error pages can show the nav; None if not signed in
        (or if looking it up fails: an error page must always render)."""
        try:
            async with session_factory() as db:
                return await optional_auth(request, db, settings)
        except Exception:
            return None

    async def http_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
            raise exc
        context = {
            "title": _STATUS_TITLES.get(exc.status_code, "Something went wrong"),
            "message": exc.detail,
            "auth": await _signed_in(request),
        }
        return render(
            request, "error.html", context, status_code=exc.status_code, headers=exc.headers
        )

    async def validation_error(request: Request, exc: Exception) -> Response:
        context = {
            "title": "Bad request",
            "message": "That request wasn't valid.",
            "auth": await _signed_in(request),
        }
        return render(request, "error.html", context, status_code=400)

    app.add_exception_handler(LoginRequired, login_required)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

    # Added innermost first: SecurityHeaders wraps TrustedHost wraps OriginCheck wraps the app,
    # so even the 400/403 rejections carry the security headers.
    app.add_middleware(OriginCheckMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware, https=settings.https)
    return app


def create_app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory`` (used by ``salesfinder web --reload``)."""
    from salesfinder.settings import get_settings

    return create_app(get_settings())
