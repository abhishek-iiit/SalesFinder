import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from salesfinder.auth import users
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.auth.tokens import hash_token
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Invite, Suppression, User
from salesfinder.ops import network
from salesfinder.settings import Settings
from salesfinder.web.app import create_app
from web_helpers import audit_actions, csrf_for, csrf_from, login, text_of

INVALID = "This invite link isn't valid."
EXPIRED = "This invite link has expired. Ask an admin for a new one."
USED = "This invite link has already been used."
EXISTS = "An account with this email already exists. Sign in instead."
NEW_PASSWORD = "a brand new passphrase"  # noqa: S105

_LINK_RE = re.compile(r"http://127\.0\.0\.1/invite/([A-Za-z0-9_-]+)")


async def _invite(client: httpx.AsyncClient, email: str, role: str = "member") -> str:
    token = await csrf_for(client, "/admin/users")
    resp = await client.post(
        "/admin/invites", data={"email": email, "role": role, "csrf_token": token}
    )
    assert resp.status_code == 200, resp.text
    assert "/invite/" in resp.text
    assert resp.headers["cache-control"] == "no-store"
    match = _LINK_RE.search(resp.text)
    assert match, "invite link not shown"
    return match.group(1)


def _fresh_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def test_member_cannot_open_admin(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    await login(client, member.email, password)
    assert (await client.get("/admin/users")).status_code == 403
    assert (await client.get("/admin/audit")).status_code == 403
    token = await csrf_for(client)
    resp = await client.post(
        "/admin/invites", data={"email": "x@elchemy.com", "role": "admin", "csrf_token": token}
    )
    assert resp.status_code == 403


async def test_admin_invites_and_user_accepts(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str, db: AsyncSession
) -> None:
    await login(client, admin.email, password)
    raw = await _invite(client, "Rep@Elchemy.com")

    # The raw token is never stored, only its hash.
    stored = await db.scalar(select(Invite).where(Invite.email == "rep@elchemy.com"))
    assert stored is not None and stored.token_hash != raw

    # The link is shown once: the users page does not repeat it.
    assert raw not in (await client.get("/admin/users")).text

    async with _fresh_client(app) as newcomer:
        page = await newcomer.get(f"/invite/{raw}")
        assert page.status_code == 200
        assert "rep@elchemy.com" in page.text
        resp = await newcomer.post(
            f"/invite/{raw}",
            data={"name": "Rep", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        home = await newcomer.get("/")
        assert home.status_code == 200
        assert "Rep" in home.text

    actions = await audit_actions(db)
    assert "invite.created" in actions
    assert "invite.accepted" in actions


async def test_invite_form_validation(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str
) -> None:
    await login(client, admin.email, password)
    raw = await _invite(client, "short@elchemy.com")
    async with _fresh_client(app) as newcomer:
        short = await newcomer.post(
            f"/invite/{raw}",
            data={"name": "Short", "password": "short", "password_confirm": "short"},
        )
        assert short.status_code == 400
        assert "at least 12 characters" in short.text
        mismatch = await newcomer.post(
            f"/invite/{raw}",
            data={"name": "Short", "password": NEW_PASSWORD, "password_confirm": "something else!"},
        )
        assert mismatch.status_code == 400
        # The invite is still usable after the rejected attempts.
        assert (await newcomer.get(f"/invite/{raw}")).status_code == 200


async def test_invite_link_errors(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str, db: AsyncSession
) -> None:
    async with _fresh_client(app) as anon:
        made_up = await anon.get("/invite/not-a-real-token")
        assert made_up.status_code == 400
        assert INVALID in text_of(made_up)
        made_up_post = await anon.post(
            "/invite/not-a-real-token", data={"name": "X", "password": NEW_PASSWORD}
        )
        assert made_up_post.status_code == 400
        assert INVALID in text_of(made_up_post)

    await login(client, admin.email, password)
    raw = await _invite(client, "once@elchemy.com")
    data = {"name": "Once", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}
    async with _fresh_client(app) as first:
        assert (await first.post(f"/invite/{raw}", data=data)).status_code == 303
    async with _fresh_client(app) as second:
        reused = await second.get(f"/invite/{raw}")
        assert reused.status_code == 400
        assert USED in text_of(reused)
        reused_post = await second.post(f"/invite/{raw}", data=data)
        assert reused_post.status_code == 400
        assert USED in text_of(reused_post)

    late = await _invite(client, "late@elchemy.com")
    await db.execute(
        update(Invite)
        .where(Invite.email == "late@elchemy.com")
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db.commit()
    async with _fresh_client(app) as third:
        expired = await third.get(f"/invite/{late}")
        assert expired.status_code == 400
        assert EXPIRED in text_of(expired)


async def test_invite_for_existing_account_rejected(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str, db: AsyncSession
) -> None:
    await login(client, admin.email, password)
    token = await csrf_for(client, "/admin/users")
    dup = await client.post(
        "/admin/invites", data={"email": "BOSS@elchemy.com", "role": "member", "csrf_token": token}
    )
    assert dup.status_code == 400
    assert "/invite/" not in dup.text

    # Two invites for the same address: the second one to be accepted hits "exists".
    first = await _invite(client, "twin@elchemy.com")
    second = await _invite(client, "twin@elchemy.com")
    data = {"name": "Twin", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}
    async with _fresh_client(app) as anon:
        assert (await anon.post(f"/invite/{first}", data=data)).status_code == 303
    async with _fresh_client(app) as anon:
        resp = await anon.post(f"/invite/{second}", data=data)
        assert resp.status_code == 400
        assert EXISTS in text_of(resp)


async def test_admin_cannot_deactivate_self(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await login(client, admin.email, password)
    token = await csrf_for(client, "/admin/users")
    resp = await client.post(f"/admin/users/{admin.id}/deactivate", data={"csrf_token": token})
    assert resp.status_code == 400
    await db.refresh(admin)
    assert admin.is_active
    assert (await client.get("/")).status_code == 200


async def test_admin_deactivates_and_reactivates_member(
    client: httpx.AsyncClient,
    app: FastAPI,
    admin: User,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    await login(client, admin.email, password)
    token = await csrf_for(client, "/admin/users")

    no_csrf = await client.post(f"/admin/users/{member.id}/deactivate")
    assert no_csrf.status_code == 403

    off = await client.post(f"/admin/users/{member.id}/deactivate", data={"csrf_token": token})
    assert off.status_code == 303
    assert off.headers["location"] == "/admin/users"
    await db.refresh(member)
    assert not member.is_active
    async with _fresh_client(app) as other:
        assert (await login(other, member.email, password)).status_code == 401

    token = await csrf_for(client, "/admin/users")
    on = await client.post(f"/admin/users/{member.id}/reactivate", data={"csrf_token": token})
    assert on.status_code == 303
    await db.refresh(member)
    assert member.is_active

    actions = await audit_actions(db)
    assert "user.deactivated" in actions
    assert "user.reactivated" in actions


async def test_audit_page_lists_actions(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await login(client, admin.email, password)
    resp = await client.get("/admin/audit")
    assert resp.status_code == 200
    assert "user.login" in resp.text
    assert admin.email in resp.text


# --------------------------------------------------------------------------- suppressions


def audit_actions_suppression(actions: list[str]) -> list[str]:
    return [a for a in actions if a.startswith("suppression.")]


async def _seed_suppressions(db: AsyncSession) -> dict[str, Suppression]:
    rows = {
        "unsub": Suppression(address="gone@acme.com", reason="unsubscribed", detail="Asked"),
        "bounce": Suppression(
            address="bounce@acme.com", reason="bounced", detail="<b>550</b> user unknown"
        ),
        "manual": Suppression(
            address="manual@acme.com", reason="manual", detail="<script>alert(1)</script>"
        ),
    }
    db.add_all(rows.values())
    await db.commit()
    return rows


async def test_suppressions_listed_escaped(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    rows = await _seed_suppressions(db)
    assert (await login(client, admin.email, password)).status_code == 303
    page = await client.get("/admin/do-not-contact")
    assert page.status_code == 200
    for address in ("gone@acme.com", "bounce@acme.com", "manual@acme.com"):
        assert address in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<b>550</b>" not in page.text
    # Only bounced and manual rows can be removed.
    assert f"/admin/suppressions/{rows['bounce'].id}/remove" in page.text
    assert f"/admin/suppressions/{rows['manual'].id}/remove" in page.text
    assert f"/admin/suppressions/{rows['unsub'].id}/remove" not in page.text


async def test_suppression_remove_csrf_protected_and_audited(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    rows = await _seed_suppressions(db)
    bounce_id, unsub_id, admin_id = rows["bounce"].id, rows["unsub"].id, admin.id
    assert (await login(client, admin.email, password)).status_code == 303
    path = f"/admin/suppressions/{bounce_id}/remove"
    assert (await client.post(path)).status_code == 403
    assert (await client.post(path, data={"csrf_token": "forged"})).status_code == 403
    db.expire_all()
    assert await db.get(Suppression, bounce_id) is not None

    token = await csrf_for(client, "/admin/do-not-contact")
    resp = await client.post(path, data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/do-not-contact?msg=suppression_removed"
    db.expire_all()
    assert await db.get(Suppression, bounce_id) is None
    assert audit_actions_suppression(await audit_actions(db)) == ["suppression.removed"]
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "suppression.removed"))
    assert log is not None
    assert (log.actor_id, log.target_id) == (admin_id, str(bounce_id))
    assert log.details == {"address": "bounce@acme.com", "reason": "bounced"}
    done = await client.get(resp.headers["location"])
    assert "Removed from the suppression list." in text_of(done)
    again = await client.post(path, data={"csrf_token": token})
    assert again.status_code == 404

    refused = await client.post(
        f"/admin/suppressions/{unsub_id}/remove", data={"csrf_token": token}
    )
    assert refused.status_code == 400
    assert "Unsubscribes can't be removed." in text_of(refused)
    db.expire_all()
    assert await db.get(Suppression, unsub_id) is not None
    assert audit_actions_suppression(await audit_actions(db)) == ["suppression.removed"]


async def test_member_cannot_remove_suppression(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    bounce_id = (await _seed_suppressions(db))["bounce"].id
    assert (await login(client, member.email, password)).status_code == 303
    token = await csrf_for(client)
    resp = await client.post(f"/admin/suppressions/{bounce_id}/remove", data={"csrf_token": token})
    assert resp.status_code == 403
    db.expire_all()
    assert await db.get(Suppression, bounce_id) is not None


# --------------------------------------------------------------------------- public URL, revoke

PUBLIC = "https://salesfinder.local:5555"
_PUBLIC_LINK_RE = re.compile(r"https://salesfinder\.local:5555/invite/([A-Za-z0-9_-]+)")


def _app_with(test_settings: Settings, engine: AsyncEngine, **update: object) -> FastAPI:
    return create_app(
        test_settings.model_copy(update=update),
        session_factory=make_session_factory(engine),
        rate_limiter=LoginRateLimiter(),
    )


async def _post_invite(client: httpx.AsyncClient, email: str) -> httpx.Response:
    token = await csrf_for(client, "/admin/users")
    resp = await client.post(
        "/admin/invites", data={"email": email, "role": "member", "csrf_token": token}
    )
    assert resp.status_code == 200, resp.text
    return resp


async def test_invite_link_uses_public_url_even_from_loopback(
    test_settings: Settings, engine: AsyncEngine, admin: User, password: str, db: AsyncSession
) -> None:
    app = _app_with(
        test_settings,
        engine,
        public_url=PUBLIC,
        allowed_hosts=["127.0.0.1", "localhost", "salesfinder.local"],
    )
    async with _fresh_client(app) as client:
        await login(client, admin.email, password)
        resp = await _post_invite(client, "lan@elchemy.com")
    assert _PUBLIC_LINK_RE.search(resp.text)
    assert "http://127.0.0.1/invite/" not in resp.text


async def test_invite_link_uses_public_url_in_lan_mode(
    test_settings: Settings,
    engine: AsyncEngine,
    admin: User,
    password: str,
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    app = _app_with(
        test_settings,
        engine,
        bind_host="0.0.0.0",  # noqa: S104
        tls_certfile=cert,
        tls_keyfile=key,
        public_url=PUBLIC,
        allowed_hosts=["127.0.0.1", "localhost", "salesfinder.local"],
    )
    transport = httpx.ASGITransport(app=app)
    # The admin browses on the Mac itself (https://localhost:5555).
    async with httpx.AsyncClient(transport=transport, base_url="https://localhost:5555") as c:
        await login(c, admin.email, password)
        resp = await _post_invite(c, "lan@elchemy.com")
    match = _PUBLIC_LINK_RE.search(resp.text)
    assert match
    assert "localhost:5555/invite/" not in resp.text
    async with _fresh_client(app) as anon:
        # The link works (the token is the one stored).
        assert (await anon.get(f"/invite/{match.group(1)}")).status_code == 200


async def test_invite_link_falls_back_to_the_request_address_in_lan_mode(
    test_settings: Settings,
    engine: AsyncEngine,
    admin: User,
    password: str,
    db: AsyncSession,
    tmp_path: Path,
) -> None:
    """Ruling P7-D: LAN mode without a public URL uses the address the admin browsed from
    (TrustedHost limits it to allowed_hosts; doctor and Settings warn NO_PUBLIC_URL)."""
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    app = _app_with(
        test_settings,
        engine,
        bind_host="0.0.0.0",  # noqa: S104
        tls_certfile=cert,
        tls_keyfile=key,
        public_url=None,
        allowed_hosts=["127.0.0.1", "localhost", "salesfinder.local"],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://salesfinder.local:5555"
    ) as c:
        await login(c, admin.email, password)
        resp = await _post_invite(c, "lan@elchemy.com")
        settings_page = text_of(await c.get("/admin/settings"))
    assert _PUBLIC_LINK_RE.search(resp.text)
    assert network.NO_PUBLIC_URL in settings_page
    async with httpx.AsyncClient(transport=transport, base_url="https://evil.example") as c:
        assert (await c.get("/login")).status_code == 400  # Host refused before any link


async def test_member_cannot_revoke_invite(
    client: httpx.AsyncClient, admin: User, member: User, password: str, db: AsyncSession
) -> None:
    raw = await users.create_invite(
        db, email="p@elchemy.com", role="member", invited_by=admin, now=datetime.now(UTC)
    )
    await db.commit()
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    assert invite is not None
    invite_id = invite.id
    await login(client, member.email, password)
    token = await csrf_for(client)
    resp = await client.post(f"/admin/invites/{invite_id}/revoke", data={"csrf_token": token})
    assert resp.status_code == 403
    db.expire_all()
    still = await db.get(Invite, invite_id)
    assert still is not None and still.expires_at > datetime.now(UTC)
    assert "invite.revoked" not in await audit_actions(db)


async def test_admin_revokes_pending_invite(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str, db: AsyncSession
) -> None:
    await login(client, admin.email, password)
    raw = await _invite(client, "Gone@Elchemy.com", role="admin")
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    assert invite is not None
    invite_id, admin_id = invite.id, admin.id
    path = f"/admin/invites/{invite_id}/revoke"

    users_page = await client.get("/admin/users")
    assert f'action="{path}"' in users_page.text
    assert "Revoke" in users_page.text

    assert (await client.post(path)).status_code == 403
    assert (await client.post(path, data={"csrf_token": "forged"})).status_code == 403
    db.expire_all()
    row = await db.get(Invite, invite_id)
    assert row is not None and row.expires_at > datetime.now(UTC)

    token = csrf_from(users_page.text)
    resp = await client.post(path, data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users?msg=invite_revoked"
    done = await client.get(resp.headers["location"])
    assert "Invite revoked." in text_of(done)
    assert "gone@elchemy.com" not in done.text  # no longer pending
    assert path not in done.text

    log = await db.scalar(select(AuditLog).where(AuditLog.action == "invite.revoked"))
    assert log is not None
    assert (log.actor_id, log.target_type, log.target_id) == (admin_id, "invite", str(invite_id))
    assert log.details == {"email": "gone@elchemy.com", "role": "admin"}

    async with _fresh_client(app) as anon:
        page = await anon.get(f"/invite/{raw}")
        assert page.status_code == 400
        assert EXPIRED in text_of(page)
        data = {"name": "Gone", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}
        post = await anon.post(f"/invite/{raw}", data=data)
        assert post.status_code == 400
        assert EXPIRED in text_of(post)
    assert await db.scalar(select(User).where(User.email == "gone@elchemy.com")) is None

    # Idempotent: a second revoke changes nothing and isn't audited again.
    again = await client.post(path, data={"csrf_token": token})
    assert again.status_code == 303
    assert again.headers["location"] == "/admin/users?msg=invite_not_pending"
    assert "That invite is no longer pending." in text_of(
        await client.get(again.headers["location"])
    )
    actions = await audit_actions(db)
    assert actions.count("invite.revoked") == 1

    missing = await client.post(f"/admin/invites/{uuid.uuid4()}/revoke", data={"csrf_token": token})
    assert missing.status_code == 404


async def test_revoking_accepted_invite_shows_error(
    client: httpx.AsyncClient, app: FastAPI, admin: User, password: str, db: AsyncSession
) -> None:
    await login(client, admin.email, password)
    raw = await _invite(client, "taken@elchemy.com")
    data = {"name": "Taken", "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD}
    async with _fresh_client(app) as newcomer:
        assert (await newcomer.post(f"/invite/{raw}", data=data)).status_code == 303
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    assert invite is not None
    token = await csrf_for(client, "/admin/users")
    resp = await client.post(f"/admin/invites/{invite.id}/revoke", data={"csrf_token": token})
    assert resp.status_code == 400
    assert "That invite was already used." in text_of(resp)
    assert "invite.revoked" not in await audit_actions(db)
    # The page still renders normally for the admin afterwards.
    assert (await client.get("/admin/users")).status_code == 200


async def test_deactivating_admin_expires_their_pending_invites(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    other = await users.create_admin(
        db, email="second@elchemy.com", name="Second", password=password
    )
    for email in ("first@elchemy.com", "second-pick@elchemy.com"):
        await users.create_invite(db, email=email, role="member", invited_by=other, now=now)
    accepted = await users.create_invite(
        db, email="done@elchemy.com", role="member", invited_by=other, now=now
    )
    await users.accept_invite(db, accepted, name="Done", password=password, now=now)
    mine = await users.create_invite(
        db, email="mine@elchemy.com", role="member", invited_by=admin, now=now
    )
    await db.commit()
    other_id = other.id

    await login(client, admin.email, password)
    token = await csrf_for(client, "/admin/users")
    resp = await client.post(f"/admin/users/{other_id}/deactivate", data={"csrf_token": token})
    assert resp.status_code == 303

    page = text_of(await client.get("/admin/users"))
    assert "first@elchemy.com" not in page and "second-pick@elchemy.com" not in page
    assert "mine@elchemy.com" in page
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "user.deactivated"))
    assert log is not None
    assert log.details == {"email": "second@elchemy.com", "invites_revoked": 2}
    mine_row = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(mine)))
    assert mine_row is not None and mine_row.expires_at > datetime.now(UTC)
