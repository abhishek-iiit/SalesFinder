"""The admin Mailboxes page: admin only, CSRF on every POST, audited without the password, DNS
and connection checks through fakes, and the campaign's mailbox select."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import seed_campaign, seed_mailbox
from fakes.dns import FakeResolver
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Campaign, Mailbox, User
from salesfinder.sending.mailboxes import SETTINGS_CHANGED
from salesfinder.settings import Settings
from salesfinder.web.app import create_app
from test_readiness import DOMAIN, GOOD
from web_helpers import audit_actions, csrf_for, login, text_of

PASSWORD_VALUE = "super-secret-app-password"  # noqa: S105


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(GOOD)


@pytest.fixture
def mbx_app(
    test_settings: Settings,
    engine: AsyncEngine,
    sender: FakeSender,
    resolver: FakeResolver,
) -> FastAPI:
    settings = test_settings.model_copy(
        update={"mailbox_passwords": {"asha": SecretStr(PASSWORD_VALUE)}}
    )
    app = create_app(
        settings, session_factory=make_session_factory(engine), rate_limiter=LoginRateLimiter()
    )
    app.state.mail = fake_mail(sender, FakeReader({"[Gmail]/Sent Mail": FakeFolder(1)}))
    app.state.dns_resolve = resolver
    return app


@pytest_asyncio.fixture
async def mbx(mbx_app: FastAPI, db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=mbx_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


def _data(token: str, **over: str) -> dict[str, str]:
    data = {
        "csrf_token": token,
        "address": f"asha@{DOMAIN}",
        "side": "buyer",
        "username": f"asha@{DOMAIN}",
        "secret_name": "asha",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "465",
        "smtp_security": "ssl",
        "imap_host": "imap.gmail.com",
        "imap_port": "993",
        "sent_folder": "",
        "dkim_selector": "google",
        "daily_max": "30",
        "warmup_started_on": "2026-11-01",
    }
    return {**data, **over}


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    db.expire_all()
    return list(await db.scalars(select(AuditLog).where(AuditLog.action == action)))


async def _create(client: httpx.AsyncClient, db: AsyncSession) -> Mailbox:
    token = await csrf_for(client, "/admin/mailboxes/new")
    resp = await client.post("/admin/mailboxes/new", data=_data(token))
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/admin/mailboxes?msg=created"
    db.expire_all()
    box = await db.scalar(select(Mailbox))
    assert box is not None
    return box


async def test_member_gets_403(mbx: httpx.AsyncClient, member: User, password: str) -> None:
    await login(mbx, member.email, password)
    token = await csrf_for(mbx)
    box_id = uuid.uuid4()
    for path in ("", "/new", f"/{box_id}"):
        assert (await mbx.get(f"/admin/mailboxes{path}")).status_code == 403
    for path in ("/new", f"/{box_id}", f"/{box_id}/enable", f"/{box_id}/disable",
                 f"/{box_id}/check-dns", f"/{box_id}/test"):  # fmt: skip
        resp = await mbx.post(f"/admin/mailboxes{path}", data={"csrf_token": token})
        assert resp.status_code == 403, path


async def test_posts_need_csrf(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await login(mbx, admin.email, password)
    box_id = (await _create(mbx, db)).id
    for path in ("/new", f"/{box_id}", f"/{box_id}/enable", f"/{box_id}/disable",
                 f"/{box_id}/check-dns", f"/{box_id}/test"):  # fmt: skip
        assert (await mbx.post(f"/admin/mailboxes{path}", data=_data(""))).status_code == 403, path
    assert len(list((await db.scalars(select(Mailbox))).all())) == 1


async def test_create_edit_enable_disable_are_audited_without_the_secret(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    admin_id = admin.id
    await login(mbx, admin.email, password)
    box = await _create(mbx, db)
    box_id = box.id
    assert (box.enabled, box.address) == (False, f"asha@{DOMAIN}")
    assert box.created_by_id == admin_id
    token = await csrf_for(mbx, "/admin/mailboxes")

    page = text_of(await mbx.get(f"/admin/mailboxes/{box_id}"))
    assert f"asha@{DOMAIN}" in page and PASSWORD_VALUE not in page
    resp = await mbx.post(f"/admin/mailboxes/{box_id}", data=_data(token, daily_max="12"))
    assert resp.status_code == 303
    db.expire_all()
    assert (await db.get(Mailbox, box_id)).daily_max == 12  # type: ignore[union-attr]

    # Ruling P6-W: a new mailbox can't be enabled before its first passing Test connection.
    listing = text_of(await mbx.get("/admin/mailboxes"))
    assert "Not tested yet" in listing
    assert "Replies and opt-outs aren't read until it passes." in listing
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/enable", data={"csrf_token": token})
    assert resp.status_code == 400 and "Test the connection first" in text_of(resp)
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/test", data={"csrf_token": token})
    assert resp.status_code == 200 and "passed" in text_of(resp)

    resp = await mbx.post(f"/admin/mailboxes/{box_id}/enable", data={"csrf_token": token})
    assert resp.headers["location"] == "/admin/mailboxes?msg=enabled"
    db.expire_all()
    assert (await db.get(Mailbox, box_id)).enabled  # type: ignore[union-attr]
    assert "Mailbox enabled." in text_of(await mbx.get("/admin/mailboxes?msg=enabled"))
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/disable", data={"csrf_token": token})
    assert resp.headers["location"] == "/admin/mailboxes?msg=disabled"

    actions = await audit_actions(db)
    assert [a for a in actions if a.startswith("mailbox.")] == [
        "mailbox.created",
        "mailbox.updated",
        "mailbox.tested",
        "mailbox.enabled",
        "mailbox.disabled",
    ]
    [tested] = await _audit_rows(db, "mailbox.tested")
    assert (tested.details["ok"], tested.details["hold_cleared"]) == (True, True)
    everything = " ".join(str(r.details) for r in await db.scalars(select(AuditLog)))
    assert PASSWORD_VALUE not in everything
    assert "asha" in everything  # the secret's name is fine


async def test_refusals_are_shown(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await login(mbx, admin.email, password)
    token = await csrf_for(mbx, "/admin/mailboxes/new")
    resp = await mbx.post("/admin/mailboxes/new", data=_data(token, address="a@elchemy.com"))
    assert resp.status_code == 400
    assert "Cold email is never sent from elchemy.com" in text_of(resp)
    resp = await mbx.post("/admin/mailboxes/new", data=_data(token, daily_max="99"))
    assert resp.status_code == 400
    assert "at most 50" in text_of(resp)
    resp = await mbx.post("/admin/mailboxes/new", data=_data(token, smtp_port="abc"))
    assert resp.status_code == 400 and "SMTP port" in text_of(resp)
    await _create(mbx, db)
    resp = await mbx.post("/admin/mailboxes/new", data=_data(token))
    assert resp.status_code == 400 and "That mailbox already exists." in text_of(resp)
    assert (await mbx.get(f"/admin/mailboxes/{uuid.uuid4()}")).status_code == 404

    # No password set for this secret name: enabling is refused, and nothing is audited.
    await db.refresh(admin)
    other = await seed_mailbox(db, admin, secret_name="missing", address=f"x@{DOMAIN}")  # noqa: S106
    await db.commit()
    resp = await mbx.post(f"/admin/mailboxes/{other.id}/enable", data={"csrf_token": token})
    assert resp.status_code == 400
    assert 'Set SALESFINDER_MAILBOX_PASSWORDS for "missing" in .env and restart.' in text_of(resp)
    assert await _audit_rows(db, "mailbox.enabled") == []


async def test_check_dns_stores_the_report_and_audits(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession, resolver: FakeResolver
) -> None:
    await login(mbx, admin.email, password)
    box_id = (await _create(mbx, db)).id
    token = await csrf_for(mbx, "/admin/mailboxes")
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/check-dns", data={"csrf_token": token})
    assert resp.headers["location"] == "/admin/mailboxes?msg=dns_ok"
    db.expire_all()
    row = await db.get(Mailbox, box_id)
    assert row is not None and row.dns_ok and row.dns_checked_at is not None
    page = text_of(await mbx.get("/admin/mailboxes"))
    for name in ("SPF ✓", "DKIM ✓", "DMARC ✓", "MX ✓"):
        assert name in page

    resolver.answers[(f"_dmarc.{DOMAIN}", "TXT")] = []
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/check-dns", data={"csrf_token": token})
    assert resp.headers["location"] == "/admin/mailboxes?msg=dns_failed"
    page = text_of(await mbx.get("/admin/mailboxes"))
    assert "DMARC ✗" in page and 'title="No DMARC record."' in page
    rows = await _audit_rows(db, "mailbox.tested")
    assert [(r.details["check"], r.details["ok"]) for r in rows] == [("dns", True), ("dns", False)]


async def test_test_connection(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession, sender: FakeSender
) -> None:
    await login(mbx, admin.email, password)
    box_id = (await _create(mbx, db)).id
    token = await csrf_for(mbx, "/admin/mailboxes")
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/test", data={"csrf_token": token})
    assert resp.status_code == 200
    assert "passed" in text_of(resp) and PASSWORD_VALUE not in resp.text
    assert sender.checked == [f"asha@{DOMAIN}"]

    sender.check_error = "couldn't sign in."
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/test", data={"csrf_token": token})
    assert "SMTP: couldn't sign in." in text_of(resp)
    rows = await _audit_rows(db, "mailbox.tested")
    assert [(r.details["check"], r.details["ok"]) for r in rows] == [
        ("connection", True),
        ("connection", False),
    ]
    assert PASSWORD_VALUE not in " ".join(str(r.details) for r in rows)

    # Without a password in settings the test is refused before any connection.
    await db.refresh(admin)
    other = await seed_mailbox(db, admin, secret_name="missing", address=f"x@{DOMAIN}")  # noqa: S106
    await db.commit()
    before = len(sender.checked)
    resp = await mbx.post(f"/admin/mailboxes/{other.id}/test", data={"csrf_token": token})
    assert resp.status_code == 400 and len(sender.checked) == before


async def test_a_passing_test_clears_the_settings_changed_hold(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession, sender: FakeSender
) -> None:
    """Ruling P6-V: only a successful Test connection clears "settings changed" (audited with
    the test); a failing one keeps it, and enabling is refused until then."""
    await login(mbx, admin.email, password)
    box_id = (await _create(mbx, db)).id
    await db.execute(
        update(Mailbox).where(Mailbox.id == box_id).values(hold_reason=SETTINGS_CHANGED)
    )
    await db.commit()
    token = await csrf_for(mbx, "/admin/mailboxes")

    resp = await mbx.post(f"/admin/mailboxes/{box_id}/enable", data={"csrf_token": token})
    assert resp.status_code == 400 and "Test the connection first" in text_of(resp)

    sender.check_error = "couldn't sign in."
    await mbx.post(f"/admin/mailboxes/{box_id}/test", data={"csrf_token": token})
    box = await db.scalar(
        select(Mailbox).where(Mailbox.id == box_id).execution_options(populate_existing=True)
    )
    assert box is not None and box.hold_reason is not None

    sender.check_error = None
    await mbx.post(f"/admin/mailboxes/{box_id}/test", data={"csrf_token": token})
    box = await db.scalar(
        select(Mailbox).where(Mailbox.id == box_id).execution_options(populate_existing=True)
    )
    assert box is not None and box.hold_reason is None
    rows = await _audit_rows(db, "mailbox.tested")
    assert [(r.details["ok"], r.details["hold_cleared"]) for r in rows] == [
        (False, False),
        (True, True),
    ]
    resp = await mbx.post(f"/admin/mailboxes/{box_id}/enable", data={"csrf_token": token})
    assert resp.status_code == 303


async def test_page_escapes_address_and_hold_reason(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await login(mbx, admin.email, password)
    box = await seed_mailbox(db, admin, address='"<i>x</i>"@send.example.org')
    box.hold_reason = "<script>alert(1)</script>"
    box.last_error = "<b>boom</b>"
    await db.commit()
    page = (await mbx.get("/admin/mailboxes")).text
    for raw in ("<script>alert(1)</script>", "<b>boom</b>", "<i>x</i>"):
        assert raw not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


async def test_page_lists_cap_and_sent_today(
    mbx: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await login(mbx, admin.email, password)
    box = await seed_mailbox(db, admin, address=f"asha@{DOMAIN}", enabled=True)
    box.daily_max = 12
    await db.commit()
    page = text_of(await mbx.get("/admin/mailboxes"))
    assert "0 / 12" in page and "not checked" in page
    assert "Mailboxes" in page  # the nav link


async def test_campaign_page_chooses_a_mailbox(
    mbx_app: FastAPI, mbx: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    from campaign_helpers import CONFIG, FACTS

    mbx_app.state.facts, mbx_app.state.config = FACTS, CONFIG
    campaign = await seed_campaign(db, user=member, activate=False, mailbox=False)
    mine_id = (await seed_mailbox(db, member, address=f"a@{DOMAIN}")).id
    other_id = (await seed_mailbox(db, member, side="supplier", address=f"s@{DOMAIN}")).id
    campaign_id = campaign.id
    await db.commit()
    await login(mbx, member.email, password)
    url = f"/campaigns/{campaign_id}"
    page = text_of(await mbx.get(url))
    assert "Choose a sending mailbox for this campaign." in page
    assert f"a@{DOMAIN}" in page and f"s@{DOMAIN}" not in page
    token = await csrf_for(mbx, url)

    assert (await mbx.post(f"{url}/mailbox", data={"mailbox_id": str(mine_id)})).status_code == 403
    resp = await mbx.post(f"{url}/mailbox", data={"csrf_token": token, "mailbox_id": str(other_id)})
    assert resp.status_code == 400 and "That mailbox is for the other side." in text_of(resp)
    resp = await mbx.post(f"{url}/mailbox", data={"csrf_token": token, "mailbox_id": ""})
    assert resp.status_code == 400
    resp = await mbx.post(f"{url}/mailbox", data={"csrf_token": token, "mailbox_id": str(mine_id)})
    assert resp.status_code == 303 and resp.headers["location"] == f"{url}?msg=mailbox"
    db.expire_all()
    row = await db.get(Campaign, campaign_id)
    assert row is not None and row.mailbox_id == mine_id
    page = text_of(await mbx.get(f"{url}?msg=mailbox"))
    assert "Sending mailbox saved." in page and "Ready to activate." in page
    rows = await _audit_rows(db, "campaign.mailbox_set")
    assert len(rows) == 1 and rows[0].details["address"] == f"a@{DOMAIN}"


_ = datetime, UTC


# --- mail reading: folder errors (dismiss) and mail we couldn't place --------------------------


async def test_folder_errors_show_and_dismiss(
    mbx: httpx.AsyncClient, admin: User, member: User, password: str, db: AsyncSession
) -> None:
    from salesfinder.db.models import MailboxFolder, MailboxMessage
    from salesfinder.replies.ingest import SKIPPED
    from salesfinder.web.routes.mailboxes import ERROR_CHANGED

    admin_id = admin.id
    box = await seed_mailbox(db, admin, address="sales@send.example.org")
    box_id = box.id
    db.add_all(
        [
            MailboxFolder(mailbox_id=box_id, folder="inbox", name="INBOX", last_error=SKIPPED),
            MailboxFolder(
                mailbox_id=box_id,
                folder="sent",
                name="[Gmail]/Sent Mail",
                last_error="Couldn't read the mailbox: TimeoutError",
            ),
            MailboxMessage(
                mailbox_id=box_id,
                folder="inbox",
                uidvalidity=1,
                uid=9,
                from_address="postmaster@<script>.example",
                subject="Undeliverable: <b>hello</b>",
                received_at=datetime.now(UTC),
                kind="unclear_bounce",
            ),
        ]
    )
    await db.commit()
    resp = await login(mbx, admin.email, password)
    assert resp.status_code == 303
    page = await mbx.get("/admin/mailboxes")
    text = text_of(page)
    assert SKIPPED in text and "Couldn't read the mailbox: TimeoutError" in text
    assert "Possible bounce we couldn't read" in text
    assert "Undeliverable: &lt;b&gt;hello&lt;/b&gt;" in page.text and "<b>hello" not in page.text
    assert "postmaster@&lt;script&gt;.example" in page.text
    # The Today page shows the read errors too.
    assert SKIPPED in text_of(await mbx.get("/"))

    url = f"/admin/mailboxes/{box_id}/folders/inbox/dismiss"
    assert (await mbx.post(url)).status_code == 403
    token = await csrf_for(mbx, "/admin/mailboxes")
    # Compare-and-set: an error other than the one shown isn't cleared.
    stale = await mbx.post(url, data={"csrf_token": token, "error": "Couldn't read the mailbox: X"})
    assert stale.status_code == 409 and ERROR_CHANGED in text_of(stale)
    db.expire_all()
    still = await db.get(MailboxFolder, (box_id, "inbox"))
    assert still is not None and still.last_error == SKIPPED
    resp = await mbx.post(url, data={"csrf_token": token, "error": SKIPPED})
    assert resp.status_code == 303
    assert "dismissed" in text_of(await mbx.get(resp.headers["location"]))
    db.expire_all()
    inbox = await db.get(MailboxFolder, (box_id, "inbox"))
    sent = await db.get(MailboxFolder, (box_id, "sent"))
    assert inbox is not None and inbox.last_error is None
    assert sent is not None and sent.last_error is not None
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "mailbox.error_dismissed"))
    assert row is not None and row.actor_id == admin_id
    assert row.details == {"folder": "inbox", "error": SKIPPED}
    # Again: nothing to dismiss, nothing audited; unknown folders are 404.
    again = await mbx.post(url, data={"csrf_token": token, "error": SKIPPED})
    assert again.status_code == 303
    assert (await audit_actions(db)).count("mailbox.error_dismissed") == 1
    bad = await mbx.post(
        f"/admin/mailboxes/{box_id}/folders/drafts/dismiss", data={"csrf_token": token}
    )
    assert bad.status_code == 404
    missing = await mbx.post(
        f"/admin/mailboxes/{uuid.uuid4()}/folders/inbox/dismiss", data={"csrf_token": token}
    )
    assert missing.status_code == 404
