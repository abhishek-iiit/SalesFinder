"""Campaigns on the dashboard: the list, the new-campaign form (cohorts filtered by side), the
campaign page with its lifecycle buttons (audited, CSRF, refusals shown as 400), and the Today
page's outreach numbers."""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import seed_campaign, seed_cohort, seed_draft
from salesfinder.campaigns import service
from salesfinder.campaigns.service import CAP_RANGE, NAME_TAKEN, NOT_PAUSABLE
from salesfinder.db.models import AuditLog, Campaign, Message, Sequence, UsageEvent, User
from salesfinder.web.deps import CONCURRENT_CHANGE
from web_helpers import audit_actions, csrf_for, login, text_of


def _campaign_id(location: str, msg: str) -> uuid.UUID:
    match = re.fullmatch(rf"/campaigns/([0-9a-f-]{{36}})\?msg={msg}", location)
    assert match, location
    return uuid.UUID(match.group(1))


async def _status(db: AsyncSession, campaign_id: uuid.UUID) -> str:
    campaign = await db.get(Campaign, campaign_id, populate_existing=True)
    assert campaign is not None
    return campaign.status


async def test_new_form_filters_cohorts_by_side(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await seed_cohort(db, member, side="buyer", name="Soap <makers>")
    await seed_cohort(db, member, side="supplier", name="SLES producers")
    await db.commit()
    await login(client, member.email, password)

    buyer = await client.get("/campaigns/new")
    assert buyer.status_code == 200
    assert "Soap &lt;makers&gt;" in buyer.text and "SLES producers" not in buyer.text
    assert 'name="side" value="buyer"' in buyer.text
    supplier = text_of(await client.get("/campaigns/new?side=supplier"))
    assert "SLES producers" in supplier and "Soap <makers>" not in supplier
    assert (await client.get("/campaigns/new?side=nope")).status_code == 400


async def test_create_then_activation_refused_with_reason(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """The shipped facts.yaml has no confirmed persona: activation is refused and says why."""
    cohort = await seed_cohort(db, member, side="buyer", name="Soap makers")
    cohort_id = cohort.id
    await db.commit()
    await login(client, member.email, password)
    token = await csrf_for(client, "/campaigns/new")
    form = {
        "csrf_token": token,
        "name": "  Texas soap  ",
        "side": "buyer",
        "cohort_id": str(cohort_id),
        "daily_draft_cap": "25",
    }
    resp = await client.post("/campaigns/new", data=form)
    assert resp.status_code == 303, resp.text
    campaign_id = _campaign_id(resp.headers["location"], "created")
    page = text_of(await client.get(resp.headers["location"]))
    assert "Campaign created." in page and "Texas soap" in page and "Soap makers" in page
    assert "Persona for buyer isn't confirmed in facts.yaml." in page  # activation problems
    assert "Confirm Elchemy's postal address in facts.yaml first." in page
    assert "0 / 25" in page

    refused = await client.post(f"/campaigns/{campaign_id}/activate", data={"csrf_token": token})
    assert refused.status_code == 400
    assert "Persona for buyer isn't confirmed in facts.yaml." in text_of(refused)
    assert await _status(db, campaign_id) == "draft"

    listing = text_of(await client.get("/campaigns"))
    assert "Texas soap" in listing and "Draft" in listing and "0 / 25" in listing

    actions = [a for a in await audit_actions(db) if a.startswith("campaign.")]
    assert actions == ["campaign.created"]
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "campaign.created"))
    assert log is not None and log.target_id == str(campaign_id)
    assert log.details == {
        "name": "Texas soap",
        "side": "buyer",
        "cohort_id": str(cohort_id),
        "daily_draft_cap": 25,
    }


async def test_create_validation(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    buyer = await seed_cohort(db, member, side="buyer", name="Buyers")
    supplier = await seed_cohort(db, member, side="supplier", name="Suppliers")
    await db.commit()
    await login(client, member.email, password)
    token = await csrf_for(client, "/campaigns/new")
    base = {
        "csrf_token": token,
        "name": "One",
        "side": "buyer",
        "cohort_id": str(buyer.id),
        "daily_draft_cap": "40",
    }
    assert (await client.post("/campaigns/new", data=base)).status_code == 303

    cases = [
        ({"name": "one"}, NAME_TAKEN),
        ({"name": "Two", "daily_draft_cap": "0"}, CAP_RANGE),
        ({"name": "Two", "daily_draft_cap": "201"}, CAP_RANGE),
        ({"name": "Two", "daily_draft_cap": "abc"}, CAP_RANGE),
        ({"name": "Two", "cohort_id": str(supplier.id)}, "That cohort is for the other side."),
        ({"name": "Two", "cohort_id": "not-a-uuid"}, "Choose a cohort."),
        ({"name": "Two", "cohort_id": str(uuid.uuid4())}, "That cohort no longer exists."),
        ({"name": "   "}, "A campaign name must be 1 to 120 characters."),
    ]
    for change, message in cases:
        resp = await client.post("/campaigns/new", data={**base, **change})
        assert resp.status_code == 400, change
        page = text_of(resp)
        assert message in page, (change, page)
    # The typed name is kept (escaped) in the form.
    kept = await client.post(
        "/campaigns/new", data={**base, "name": "<b>x</b>", "daily_draft_cap": "0"}
    )
    assert kept.status_code == 400 and 'value="&lt;b&gt;x&lt;/b&gt;"' in kept.text
    assert (await client.post("/campaigns/new", data={**base, "side": "x"})).status_code == 400

    db.expire_all()
    assert list(await db.scalars(select(Campaign.name))) == ["One"]
    # CSRF required.
    no_csrf = {k: v for k, v in base.items() if k != "csrf_token"} | {"name": "Three"}
    assert (await client.post("/campaigns/new", data=no_csrf)).status_code == 403


async def test_lifecycle_buttons_are_audited(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    member_id = member.id
    campaign = await seed_campaign(db, user=member, activate=False, name="Life")
    await login(client, member.email, password)
    url = f"/campaigns/{campaign.id}"
    page = text_of(await client.get(url))
    assert "Activate" in page and "Pause" not in page
    assert "Ready to activate." in page
    token = await csrf_for(client, url)

    # CSRF on every button.
    assert (await client.post(f"{url}/activate")).status_code == 403
    assert await _status(db, campaign.id) == "draft"

    steps = [
        ("activate", "active", "activated", "Campaign activated."),
        ("pause", "paused", "paused", "Campaign paused."),
        ("resume", "active", "resumed", "Campaign resumed."),
        ("archive", "archived", "archived", "Campaign archived."),
    ]
    for action, status, msg, flash in steps:
        resp = await client.post(f"{url}/{action}", data={"csrf_token": token})
        assert resp.status_code == 303, (action, resp.text)
        assert resp.headers["location"] == f"{url}?msg={msg}"
        assert await _status(db, campaign.id) == status
        assert flash in text_of(await client.get(resp.headers["location"]))

    refused = await client.post(f"{url}/pause", data={"csrf_token": token})
    assert refused.status_code == 400 and NOT_PAUSABLE in text_of(refused)
    archived = text_of(await client.get(url))
    assert f'action="{url}/activate"' not in archived and f'action="{url}/pause"' not in archived

    # seed_campaign used the service directly, so only the buttons are in the audit log.
    actions = [a for a in await audit_actions(db) if a.startswith("campaign.")]
    assert actions == [
        "campaign.activated",
        "campaign.paused",
        "campaign.resumed",
        "campaign.archived",
    ]
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "campaign.paused"))
    assert log is not None and log.actor_id == member_id
    assert log.details == {"name": "Life", "from": "active", "to": "paused"}
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "campaign.archived"))
    assert log is not None
    assert log.details == {
        "name": "Life",
        "from": "active",
        "to": "archived",
        "sequences_stopped": 0,
        "messages_cancelled": 0,
    }

    missing = uuid.uuid4()
    assert (await client.get(f"/campaigns/{missing}")).status_code == 404
    gone = await client.post(f"/campaigns/{missing}/activate", data={"csrf_token": token})
    assert gone.status_code == 404


async def test_list_escapes_and_shows_counts(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    campaign = await seed_campaign(db, user=member, name="<i>Soap</i>", cap=30)
    campaign.last_error = "<script>alert(1)</script> Claude kept failing"
    await db.commit()
    await login(confirmed_client, member.email, password)
    resp = await confirmed_client.get("/campaigns")
    assert resp.status_code == 200
    assert "&lt;i&gt;Soap&lt;/i&gt;" in resp.text and "<i>Soap</i>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Claude kept failing" in resp.text
    assert "<script>alert" not in resp.text
    page = text_of(resp)
    assert "Active" in page and "0 / 30" in page and "Buyer" in page


async def test_today_shows_outreach_numbers(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    campaign = await seed_campaign(db, user=member, name="Today")
    campaign.last_error = "<b>Daily Claude budget reached (150 calls).</b>"
    await db.commit()
    await seed_draft(db, campaign, domain="a-soap.com")
    await seed_draft(db, campaign, domain="b-soap.com", status="gate_failed")
    now = datetime.now(UTC)
    for ok in (True, False, True):
        db.add(UsageEvent(task="write_sequence", ok=ok, duration_ms=10, at=now))
    await db.commit()

    await login(confirmed_client, member.email, password)
    resp = await confirmed_client.get("/")
    page = text_of(resp)
    assert re.search(r"Ready to approve</dt>\s*<dd>1</dd>", page), page
    assert re.search(r"Held by the gate</dt>\s*<dd>1</dd>", page)
    assert re.search(r"Drafted today</dt>\s*<dd>2</dd>", page)
    assert re.search(r"Claude calls today</dt>\s*<dd>3 / 50</dd>", page)
    assert "&lt;b&gt;Daily Claude budget reached (150 calls).&lt;/b&gt;" in resp.text
    assert "Outbox (2)" in page


async def test_list_count_uses_the_outbox_filter(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """Ruling AZ: a first email whose sequence was stopped elsewhere isn't counted on
    /campaigns (nor is a follow-up), the same as the Outbox and the nav."""
    campaign = await seed_campaign(db, user=member, name="Counted")
    await seed_draft(db, campaign, domain="a-soap.com")
    stopped = await seed_draft(db, campaign, domain="b-soap.com")
    await login(confirmed_client, member.email, password)
    link = f'<a href="/outbox?campaign={campaign.id}">'
    assert f"{link}2</a>" in text_of(await confirmed_client.get("/campaigns"))

    sequence = await db.get(Sequence, stopped.sequence_id)
    assert sequence is not None
    sequence.status = "stopped"
    follow_up = await db.scalar(
        select(Message).where(Message.sequence_id == stopped.sequence_id, Message.step == 2)
    )
    assert follow_up is not None
    follow_up.status = "awaiting_approval"  # a step-2 email never counts in Phase 5
    await db.commit()
    page = text_of(await confirmed_client.get("/campaigns"))
    assert f"{link}1</a>" in page and "Outbox (1)" in page


async def test_lifecycle_deadlock_retries_then_409(
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Orig(Exception):
        sqlstate = "40P01"

    client = confirmed_client
    campaign = await seed_campaign(db, user=member, name="Racy")
    await login(client, member.email, password)
    url = f"/campaigns/{campaign.id}"
    token = await csrf_for(client, url)
    real, calls = service.pause, []

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise DBAPIError("UPDATE campaigns", None, Orig())
        return await real(*args, **kwargs)

    monkeypatch.setattr(service, "pause", flaky)
    resp = await client.post(f"{url}/pause", data={"csrf_token": token})
    assert resp.status_code == 303 and len(calls) == 2
    assert await _status(db, campaign.id) == "paused"

    async def always(*_args: Any, **_kwargs: Any) -> Any:
        raise DBAPIError("UPDATE campaigns", None, Orig())

    monkeypatch.setattr(service, "archive", always)
    resp = await client.post(f"{url}/archive", data={"csrf_token": token})
    assert resp.status_code == 409 and CONCURRENT_CHANGE in text_of(resp)
    assert await _status(db, campaign.id) == "paused"
    assert [a for a in await audit_actions(db) if a.startswith("campaign.")] == ["campaign.paused"]
