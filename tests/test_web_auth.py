from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from salesfinder.auth import users
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.db.models import AuditLog, User, UserSession
from salesfinder.settings import Settings
from salesfinder.web.app import create_app
from salesfinder.web.security import safe_next
from web_helpers import audit_actions, csrf_for, login, text_of

LOGIN_FAILED = "Email or password is incorrect."
RATE_LIMITED = "Too many attempts. Try again in 15 minutes."


async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    # The worker's state too (Phase 7): a fresh database has never seen it run.
    assert resp.json() == {"status": "ok", "worker": "never"}


async def test_root_redirects_to_login(client: httpx.AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_login_success_sets_secure_cookie_flags(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    resp = await login(client, "Member@Elchemy.com ", password)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith("sf_session=")
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "max-age=43200" in lowered
    assert "secure" not in lowered  # plain-HTTP test settings; Secure only when settings.https

    home = await client.get("/")
    assert home.status_code == 200
    assert member.name in home.text
    assert "Signed in as" in home.text
    assert "user.login" in await audit_actions(db)

    # Only the hash is stored server-side, never the raw cookie token.
    raw = client.cookies["sf_session"]
    hashes = list(await db.scalars(select(UserSession.token_hash)))
    assert raw not in hashes and len(hashes) == 1


async def test_login_redirects_to_safe_next_only(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    first = await client.get("/admin/audit")
    assert first.status_code == 303
    assert first.headers["location"] == "/login?next=%2Fadmin%2Faudit"

    resp = await login(client, member.email, password, next_path="/admin/users")
    assert resp.headers["location"] == "/admin/users"

    for evil in ("//evil.example/x", "https://evil.example/", "/\\evil.example", "javascript:x"):
        client.cookies.clear()
        resp = await login(client, member.email, password, next_path=evil)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/", evil


async def test_login_wrong_password_generic_message(
    client: httpx.AsyncClient, member: User, db: AsyncSession
) -> None:
    wrong = await login(client, member.email, "not the right password")
    assert wrong.status_code == 401
    assert LOGIN_FAILED in text_of(wrong)
    assert "sf_session" not in wrong.headers.get("set-cookie", "")

    unknown = await login(client, "nobody@elchemy.com", "not the right password")
    assert unknown.status_code == 401
    assert LOGIN_FAILED in text_of(unknown)

    assert (await audit_actions(db)).count("user.login_failed") == 2


async def test_login_rate_limited(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    for _ in range(5):
        resp = await login(client, member.email, "not the right password")
        assert resp.status_code == 401
    blocked = await login(client, "MEMBER@elchemy.com", password)
    assert blocked.status_code == 429
    assert RATE_LIMITED in text_of(blocked)
    assert "sf_session" not in client.cookies
    assert "user.login" not in await audit_actions(db)


async def test_successful_login_resets_rate_limit(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    for _ in range(4):
        await login(client, member.email, "not the right password")
    assert (await login(client, member.email, password)).status_code == 303
    client.cookies.clear()
    for _ in range(4):
        await login(client, member.email, "not the right password")
    assert (await login(client, member.email, password)).status_code == 303


async def test_logout_revokes_session(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    old_cookie = client.cookies["sf_session"]
    token = await csrf_for(client)

    resp = await client.post("/logout", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "user.logout" in await audit_actions(db)

    client.cookies.set("sf_session", old_cookie)
    again = await client.get("/")
    assert again.status_code == 303
    assert again.headers["location"].startswith("/login")


async def test_deactivated_user_kicked_out(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    assert (await client.get("/")).status_code == 200

    await users.set_active(db, member.id, False, now=datetime.now(UTC))
    await db.commit()

    resp = await client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


async def test_bad_host_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.get("/", headers={"host": "evil.example"})
    assert resp.status_code == 400


async def test_foreign_origin_post_rejected(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    data = {"email": member.email, "password": password}
    resp = await client.post("/login", data=data, headers={"origin": "http://evil.example"})
    assert resp.status_code == 403
    referer = await client.post(
        "/login", data=data, headers={"referer": "http://evil.example/page"}
    )
    assert referer.status_code == 403
    null_origin = await client.post("/login", data=data, headers={"origin": "null"})
    assert null_origin.status_code == 403
    same = await client.post("/login", data=data, headers={"origin": "http://127.0.0.1:5555"})
    assert same.status_code == 303


async def test_missing_csrf_rejected(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    await login(client, member.email, password)
    assert (await client.post("/logout")).status_code == 403
    assert (await client.post("/logout", data={"csrf_token": "forged"})).status_code == 403
    # Still logged in: the rejected logout changed nothing.
    assert (await client.get("/")).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/login")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in resp.headers  # only when settings.https
    assert "<script>" not in resp.text  # CSP forbids inline scripts


async def test_unauthenticated_post_redirects_to_login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/logout", data={"csrf_token": "x"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_session_activity_and_revocation_persist(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """current_user commits after resolve_session (Ruling E)."""
    await login(client, member.email, password)
    session_row = await db.scalar(select(UserSession))
    assert session_row is not None
    first_seen = session_row.last_seen_at
    # Activity is recorded at most once a minute: make the last one older than that.
    await db.execute(update(UserSession).values(last_seen_at=first_seen - timedelta(seconds=61)))
    await db.commit()  # end the transaction so the next read sees new commits

    assert (await client.get("/")).status_code == 200
    await db.refresh(session_row)
    assert session_row.last_seen_at > first_seen
    await db.commit()

    # Deactivate behind the app's back (no revoke_all): resolving must revoke and persist it.
    await db.execute(update(User).where(User.id == member.id).values(is_active=False))
    await db.commit()
    resp = await client.get("/")
    assert resp.status_code == 303
    assert "sf_session" in resp.headers.get("set-cookie", "")  # stale cookie is cleared
    await db.refresh(session_row)
    assert session_row.revoked_at is not None


async def test_https_mode_secure_cookie_and_hsts(
    test_settings: Settings, engine: AsyncEngine, member: User, password: str
) -> None:
    settings = test_settings.model_copy(
        update={"tls_certfile": Path("cert.pem"), "tls_keyfile": Path("key.pem")}
    )
    assert settings.https
    app = create_app(settings, session_factory=make_session_factory(engine))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://127.0.0.1") as c:
        page = await c.get("/login")
        assert page.headers["strict-transport-security"] == "max-age=31536000"
        resp = await login(c, member.email, password)
        assert resp.status_code == 303
        lowered = resp.headers["set-cookie"].lower()
        assert "; secure" in lowered and "httponly" in lowered


async def test_healthz_reports_db_unavailable(test_settings: Settings) -> None:
    broken = make_engine("postgresql+asyncpg://127.0.0.1:1/nope")
    app = create_app(test_settings, session_factory=make_session_factory(broken))
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "db_unavailable"}
    finally:
        await broken.dispose()


def test_safe_next() -> None:
    assert safe_next("/admin/users?x=1") == "/admin/users?x=1"
    for bad in (
        None,
        "",
        "admin",
        "//evil.example",
        "/\\evil.example",
        "https://evil.example/",
        "javascript:alert(1)",
        "/ok\r\nSet-Cookie: x=y",
    ):
        assert safe_next(bad) == "/", bad


async def test_static_assets_served(client: httpx.AsyncClient) -> None:
    js = await client.get("/static/htmx.min.js")
    assert js.status_code == 200
    assert "htmx" in js.text[:200]
    css = await client.get("/static/app.css")
    assert css.status_code == 200
    assert "prefers-color-scheme: dark" in css.text
    page = await client.get("/login")
    assert 'src="/static/htmx.min.js"' in page.text
    assert "style=" not in page.text and " on" + "click=" not in page.text


async def test_failed_login_audit_never_stores_non_email_input(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    resp = await login(client, "hunter2-not-an-email", "whatever password")
    assert resp.status_code == 401
    rows = (await db.scalars(select(AuditLog))).all()
    assert [r.action for r in rows] == ["user.login_failed"]
    assert "hunter2" not in str(rows[0].details)
    assert rows[0].details["email"] == "<invalid>"
