"""Admin settings (sending, budget, HubSpot) and do-not-contact pages; pausing sending (any
teammate) and resuming it (admins, compare-and-set); the Today page's sending summary."""

import html
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import seed_mailbox
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.config import DiscoveryConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Company, DoNotContact, HubSpotSync, User
from salesfinder.discovery.budget import get_budget
from salesfinder.ops import network
from salesfinder.sending import state
from salesfinder.settings import Settings
from salesfinder.web.app import create_app
from web_helpers import audit_actions, csrf_for, login, text_of


async def _as(client: httpx.AsyncClient, user: User, password: str) -> None:
    resp = await login(client, user.email, password)
    assert resp.status_code == 303


async def test_member_forbidden(client: httpx.AsyncClient, member: User, password: str) -> None:
    await _as(client, member, password)
    for path in ("/admin/settings", "/admin/do-not-contact"):
        assert (await client.get(path)).status_code == 403
    resp = await client.post("/admin/settings/budget", data={"budget": "5", "csrf_token": "x"})
    assert resp.status_code == 403


async def test_post_without_csrf_rejected(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    resp = await client.post("/admin/do-not-contact", data={"entry": "a.com", "reason": "x"})
    assert resp.status_code == 403
    assert await db.scalar(select(func.count()).select_from(DoNotContact)) == 0


async def test_budget_update_audited(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/settings")
    resp = await client.post(
        "/admin/settings/budget", data={"budget": "35.50", "csrf_token": token}
    )
    assert resp.status_code == 303
    page = await client.get(resp.headers["location"])
    assert "Budget saved." in text_of(page)
    assert "$35.50" in text_of(page)
    db.expire_all()
    assert await get_budget(db, DiscoveryConfig()) == Decimal("35.50")
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "settings.updated"))
    assert row is not None
    assert row.details["new"] == "35.50"
    assert "old" in row.details


async def test_budget_validation(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/settings")
    for bad in ("-1", "abc", "10000.01", "1.234", ""):
        resp = await client.post(
            "/admin/settings/budget", data={"budget": bad, "csrf_token": token}
        )
        assert resp.status_code == 400, bad
        assert "Enter an amount between $0 and $10,000." in text_of(resp)
    assert "settings.updated" not in await audit_actions(db)


async def test_hubspot_panel_without_token(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _as(client, admin, password)
    page = text_of(await client.get("/admin/settings"))
    assert "SALESFINDER_HUBSPOT_TOKEN is not set" in page
    assert "Sync now" not in page


async def test_sync_now_queues_once(
    client_with_hubspot_token: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    c = client_with_hubspot_token
    await _as(c, admin, password)
    page = await c.get("/admin/settings")
    assert "Sync now" in page.text
    token = await csrf_for(c, "/admin/settings")
    for _ in range(2):
        resp = await c.post("/admin/hubspot/sync", data={"csrf_token": token})
        assert resp.status_code == 303
    follow = await c.get(resp.headers["location"])
    assert "queued" in text_of(follow)
    assert await db.scalar(select(func.count()).select_from(HubSpotSync)) == 1
    assert (await audit_actions(db)).count("hubspot.sync_requested") == 1


async def test_sync_without_token_is_400(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/settings")
    resp = await client.post("/admin/hubspot/sync", data={"csrf_token": token})
    assert resp.status_code == 400
    assert await db.scalar(select(func.count()).select_from(HubSpotSync)) == 0


async def test_dnc_add_remove_and_reexclude(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    def co(side: str, domain: str, status: str) -> Company:
        return Company(
            side=side, name=domain, domain=domain, website_url=f"https://{domain}", status=status
        )

    db.add_all(
        [
            co("buyer", "acme.com", "new"),
            co("supplier", "acme.com", "new"),
            co("buyer", "other.com", "new"),
        ]
    )
    await db.commit()
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/do-not-contact")

    resp = await client.post(
        "/admin/do-not-contact",
        data={"entry": "https://www.Acme.com/x", "reason": "Asked us to stop", "csrf_token": token},
    )
    assert resp.status_code == 303
    page = text_of(await client.get(resp.headers["location"]))
    assert "Added. 2 companies now excluded." in page
    assert "acme.com" in page
    db.expire_all()
    rows = list(await db.scalars(select(Company).order_by(Company.domain, Company.side)))
    by = {(r.side, r.domain): r for r in rows}
    assert by[("buyer", "acme.com")].status == "excluded"
    assert by[("buyer", "acme.com")].exclusion_reason == "do_not_contact"
    assert "acme.com" in (by[("buyer", "acme.com")].exclusion_detail or "")
    assert by[("buyer", "other.com")].status == "new"

    bad = await client.post(
        "/admin/do-not-contact", data={"entry": "garbage", "reason": "x", "csrf_token": token}
    )
    assert bad.status_code == 400
    assert "Enter an email address or a domain." in text_of(bad)

    entry = await db.scalar(select(DoNotContact))
    assert entry is not None
    gone = await client.post(f"/admin/do-not-contact/{entry.id}/remove", data={"csrf_token": token})
    assert gone.status_code == 303
    db.expire_all()
    assert await db.scalar(select(func.count()).select_from(DoNotContact)) == 0
    assert (await db.scalar(select(Company).where(Company.domain == "acme.com"))) is not None
    actions = await audit_actions(db)
    assert "dnc.added" in actions
    assert "dnc.removed" in actions


async def test_dnc_email_entry_excludes_nothing(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/do-not-contact")
    resp = await client.post(
        "/admin/do-not-contact",
        data={"entry": "Bob@Acme.com", "reason": "unsubscribed", "csrf_token": token},
    )
    assert resp.status_code == 303
    assert "Added." in text_of(await client.get(resp.headers["location"]))


async def test_unknown_msg_key_not_echoed(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _as(client, admin, password)
    page = await client.get("/admin/settings?msg=<script>alert(1)</script>")
    assert "<script>alert(1)" not in page.text


async def test_error_page_keeps_nav_when_signed_in(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    await _as(client, member, password)
    resp = await client.get("/admin/users")
    assert resp.status_code == 403
    assert "Sign out" in resp.text


async def test_dnc_add_leaves_already_excluded_companies_alone(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    db.add_all(
        [
            Company(
                side="buyer",
                name="Acme",
                domain="acme.com",
                website_url="https://acme.com",
                status="new",
            ),
            Company(
                side="supplier",
                name="Acme",
                domain="acme.com",
                website_url="https://acme.com",
                status="excluded",
                exclusion_reason="existing_relationship",
                exclusion_detail="HubSpot company Acme",
            ),
        ]
    )
    await db.commit()
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/do-not-contact")
    resp = await client.post(
        "/admin/do-not-contact",
        data={"entry": "acme.com", "reason": "Stop", "csrf_token": token},
    )
    assert resp.status_code == 303
    assert "Added. 1 company now excluded." in text_of(await client.get(resp.headers["location"]))
    db.expire_all()
    by_side = {r.side: r for r in await db.scalars(select(Company))}
    assert by_side["buyer"].exclusion_detail == "Do-not-contact: acme.com — Stop"
    assert by_side["supplier"].exclusion_reason == "existing_relationship"
    assert by_side["supplier"].exclusion_detail == "HubSpot company Acme"


# --- sending: pause (any teammate), resume (admin), banners, Today ----------------------------


@pytest_asyncio.fixture
async def second(app: FastAPI, db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser on the same app (another teammate signed in at once)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


def _shown_changed_at(page: str) -> str:
    match = re.search(r'name="changed_at" value="([^"]*)"', page)
    assert match, "no resume form"
    return html.unescape(match.group(1))


async def _sending_audits(db: AsyncSession) -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.action.like("sending.%"))
            .order_by(AuditLog.at)
            .execution_options(populate_existing=True)
        )
    )


async def test_today_on_a_fresh_db_says_sending_is_off(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    await _as(client, member, password)
    page = text_of(await client.get("/"))
    assert "Sending is paused: Sending hasn't been turned on yet." in page
    for label in (
        "Ready to approve",
        "Follow-ups due for review",
        "Approved, waiting to send",
        "Replies (last 24 h)",
        "Mine: 0",
        "All: 0",
        "Nothing needs you right now.",
        "No mailboxes yet",
    ):
        assert label in page, label
    assert "Resume sending" not in page  # members can't resume
    assert "Conversations" in page and "Conversations (" not in page


async def test_member_pauses_admin_resumes(
    client: httpx.AsyncClient,
    second: httpx.AsyncClient,
    admin: User,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    admin_id, member_id, member_email = admin.id, member.id, member.email
    await _as(client, admin, password)
    page = text_of(await client.get("/admin/settings"))
    assert "Sending is paused: Sending hasn't been turned on yet." in page
    assert "Bounce rate: 0 of last 50 (0%)." in page
    assert 'href="/admin/do-not-contact"' in page and 'href="/admin/mailboxes"' in page
    token = await csrf_for(client, "/admin/settings")
    assert (await client.post("/admin/sending/resume", data={"changed_at": ""})).status_code == 403
    resp = await client.post(
        "/admin/sending/resume", data={"csrf_token": token, "changed_at": _shown_changed_at(page)}
    )
    assert resp.status_code == 303
    assert "Sending is on" in text_of(await client.get(resp.headers["location"]))
    db.expire_all()
    assert not (await state.get_state(db)).paused

    # A member: the emergency stop, from Today.
    resp = await login(second, member_email, password)
    assert resp.status_code == 303
    today = text_of(await second.get("/"))
    assert "Sending is on." in today and "Pause all sending" in today
    member_token = await csrf_for(second, "/")
    assert (await second.post("/sending/pause", data={"reason": "x"})).status_code == 403
    resp = await second.post(
        "/sending/pause", data={"csrf_token": member_token, "reason": "Odd <b>bounces</b>"}
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/?msg=paused"
    landing = await second.get(resp.headers["location"])
    assert landing.status_code == 200
    assert "Sending is paused: Odd &lt;b&gt;bounces&lt;/b&gt;" in landing.text
    db.expire_all()
    current = await state.get_state(db)
    assert current.paused and current.changed_by_id == member_id
    # A second pause changes nothing and isn't audited.
    resp = await second.post("/sending/pause", data={"csrf_token": member_token})
    assert resp.headers["location"] == "/?msg=already_paused"
    # Members can't resume.
    resume = await second.post(
        "/admin/sending/resume",
        data={"csrf_token": member_token, "changed_at": current.changed_at.isoformat()},  # type: ignore[union-attr]
    )
    assert resume.status_code == 403
    db.expire_all()
    assert (await state.get_state(db)).paused

    audits = await _sending_audits(db)
    assert [(a.action, a.actor_id) for a in audits] == [
        ("sending.resumed", admin_id),
        ("sending.paused", member_id),
    ]
    assert audits[1].details == {"source": "web", "reason": "Odd <b>bounces</b>"}

    # The admin resumes what the page shows.
    page = text_of(await client.get("/admin/settings"))
    assert "Sending is paused: Odd <b>bounces</b>" in page and "Mira Member" in page
    resp = await client.post(
        "/admin/sending/resume", data={"csrf_token": token, "changed_at": _shown_changed_at(page)}
    )
    assert resp.status_code == 303
    db.expire_all()
    assert not (await state.get_state(db)).paused


async def test_resume_refuses_a_state_that_changed_meanwhile(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    admin_id = admin.id
    await _as(client, admin, password)
    page = text_of(await client.get("/admin/settings"))
    shown = _shown_changed_at(page)
    assert shown == ""  # never written
    token = await csrf_for(client, "/admin/settings")
    # Meanwhile: the kill switch (or anyone) pauses.
    await state.pause(db, reason="Paused automatically: 3 of the last 50 emails bounced (6%).",
                      actor=None, now=datetime.now(UTC))  # fmt: skip
    await db.commit()

    for stale in (shown, "not-a-date", "2026-11-13T10:00:00"):
        resp = await client.post(
            "/admin/sending/resume", data={"csrf_token": token, "changed_at": stale}
        )
        assert resp.status_code == 409
        assert state.STATE_CHANGED in text_of(resp)
        assert "Paused automatically: 3 of the last 50" in text_of(resp)
    db.expire_all()
    assert (await state.get_state(db)).paused
    assert not [a for a in await _sending_audits(db) if a.actor_id == admin_id]


async def test_pause_reason_must_be_one_line(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await _as(client, member, password)
    token = await csrf_for(client, "/")
    resp = await client.post("/sending/pause", data={"csrf_token": token, "reason": "a" * 201})
    assert resp.status_code == 400
    db.expire_all()
    assert (await state.get_state(db)).changed_at is None
    # An unknown "back" goes home.
    resp = await client.post(
        "/sending/pause", data={"csrf_token": token, "back": "https://evil.example/"}
    )
    assert resp.headers["location"] == "/?msg=paused"


async def test_settings_lists_mailboxes_with_cap_and_hold(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    box = await seed_mailbox(db, admin, address="sales@send.example.org", enabled=True)
    box.hold_reason = "Outside office hours for every recipient."
    await seed_mailbox(db, admin, address="supply@send.example.org", side="supplier")
    await db.commit()
    await _as(client, admin, password)
    page = text_of(await client.get("/admin/settings"))
    assert "sales@send.example.org" in page and "Outside office hours for every recipient." in page
    assert "supply@send.example.org" in page and "Disabled." in page
    assert "0 / 0" in page  # warm-up not started: cap 0
    today = text_of(await client.get("/"))
    assert "sales@send.example.org" in today and "Outside office hours" in today


async def test_every_page_says_sending_is_paused_once(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    paused = "Sending is paused: Sending hasn't been turned on yet."
    for path in (
        "/",
        "/outbox",
        "/outbox?view=approved",
        "/conversations",
        "/admin/mailboxes",
        "/admin/settings",
        "/campaigns",
    ):
        page = await client.get(path)
        assert page.status_code == 200, path
        assert text_of(page).count(paused) == 1, path
    await state.resume(db, actor=None, now=datetime.now(UTC))
    await db.commit()
    for path in ("/conversations", "/admin/mailboxes", "/outbox"):
        assert "Sending is paused" not in text_of(await client.get(path)), path


async def test_resume_when_already_on_changes_nothing(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await _as(client, admin, password)
    token = await csrf_for(client, "/admin/settings")
    page = text_of(await client.get("/admin/settings"))
    resp = await client.post(
        "/admin/sending/resume", data={"csrf_token": token, "changed_at": _shown_changed_at(page)}
    )
    assert resp.status_code == 303
    db.expire_all()
    first = await state.get_state(db)
    assert not first.paused and first.changed_at is not None
    # A second resume posted from a page that showed this state: a no-op, not audited, and
    # the bounce window (resumed_at) is kept.
    resp = await client.post(
        "/admin/sending/resume",
        data={"csrf_token": token, "changed_at": first.changed_at.isoformat()},
    )
    assert resp.status_code == 303 and resp.headers["location"].endswith("msg=already_on")
    assert "Sending was already on." in text_of(await client.get(resp.headers["location"]))
    db.expire_all()
    assert (await state.get_state(db)) == first
    assert [a.action for a in await _sending_audits(db)] == ["sending.resumed"]


# --------------------------------------------------------------------------- network section


async def test_settings_network_section_on_loopback(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _as(client, admin, password)
    page = text_of(await client.get("/admin/settings"))
    assert "Network" in page
    assert "Loopback only (127.0.0.1)" in page
    assert "Public URL: not set" in page
    assert "alert-warn network-warning" not in page


async def test_settings_network_section_escapes_hosts_and_shows_warnings(
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
    hostile = "<script>alert(1)</script>"
    settings = test_settings.model_copy(
        update={
            "bind_host": "0.0.0.0",  # noqa: S104
            "tls_certfile": cert,
            "tls_keyfile": key,
            "allowed_hosts": ["127.0.0.1", "localhost", hostile],
            "public_url": "https://salesfinder.local:5555",
        }
    )
    app = create_app(
        settings, session_factory=make_session_factory(engine), rate_limiter=LoginRateLimiter()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://127.0.0.1") as c:
        await _as(c, admin, password)
        resp = await c.get("/admin/settings")
    assert resp.status_code == 200
    assert hostile not in resp.text
    assert html.escape(hostile) in resp.text
    page = text_of(resp)
    assert "LAN over HTTPS on 0.0.0.0:5555" in page
    assert "https://salesfinder.local:5555" in page
    warning = network.PUBLIC_URL_HOST.format(host="salesfinder.local")
    assert warning in page
    assert re.search(r'class="alert alert-warn network-warning"[^>]*>', resp.text)
    assert test_settings.secret_key.get_secret_value() not in resp.text


async def test_settings_shows_export_form(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await _as(client, admin, password)
    page = await client.get("/admin/settings")
    assert page.status_code == 200
    body = page.text
    assert 'action="/admin/export/companies"' in body
    assert 'method="post"' in body
    for side in ("buyer", "supplier", "all"):
        assert f'value="{side}"' in body
    assert "Download CSV" in text_of(page)
