"""Discover pages: estimate -> job page -> approve/cancel, the polling status fragment."""

import re
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import ConfigError
from salesfinder.db.models import AuditLog, DiscoveryJob, User
from salesfinder.discovery import budget
from salesfinder.settings import Settings
from salesfinder.web.app import create_app
from web_helpers import audit_actions, csrf_for, login, text_of

Q1 = '"sodium laureth sulfate" shampoo manufacturer Texas'
JOB_PATH_RE = re.compile(r"^/discover/jobs/([0-9a-f-]{36})$")
BUYER_FORM = {
    "side": "buyer",
    "products": ["sles"],
    "industries": ["personal_care"],
    "regions": ["Texas"],
}


async def _estimate(client: httpx.AsyncClient, data: dict[str, object] | None = None) -> str:
    token = await csrf_for(client, "/discover")
    resp = await client.post(
        "/discover/estimate", data={**(data or BUYER_FORM), "csrf_token": token}
    )
    assert resp.status_code == 303, resp.text
    match = JOB_PATH_RE.match(resp.headers["location"])
    assert match, resp.headers["location"]
    return match.group(1)


async def _set_budget(db: AsyncSession, value: str, user_id: uuid.UUID) -> None:
    actor = await db.get(User, user_id, populate_existing=True)
    assert actor is not None
    await budget.set_budget(db, Decimal(value), actor=actor)
    await db.commit()


async def _job(db: AsyncSession, job_id: str) -> DiscoveryJob:
    job = await db.scalar(
        select(DiscoveryJob)
        .where(DiscoveryJob.id == uuid.UUID(job_id))
        .execution_options(populate_existing=True)
    )
    assert job is not None
    await db.commit()  # end the read transaction
    return job


async def test_discover_page_lists_side_products_and_budget(
    client: httpx.AsyncClient, member: User, password: str
) -> None:
    await login(client, member.email, password)
    resp = await client.get("/discover")
    assert resp.status_code == 200
    page = text_of(resp)
    assert "Sodium Laureth Sulfate" in page
    assert "Remaining this month" in page
    assert "$20.00" in page  # the configured budget, 2 decimals
    assert "ignored for suppliers" in page
    assert 'value="Texas"' in page and 'value="Gujarat"' not in page
    for nav in ("Today", "Discover", "Companies", "Catalog"):
        assert f">{nav}</a>" in page

    supplier = text_of(await client.get("/discover?side=supplier"))
    assert 'value="Gujarat"' in supplier and 'value="Texas"' not in supplier
    assert re.search(r'value="supplier"[^>]*checked', supplier)

    assert (await client.get("/discover?side=nonsense")).status_code == 400


async def test_estimate_then_job_page_shows_cost_and_run_button(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    member_id = member.id
    await login(client, member.email, password)
    job_id = await _estimate(client)

    resp = await client.get(f"/discover/jobs/{job_id}")
    assert resp.status_code == 200
    page = text_of(resp)
    assert Q1 in page
    assert "Run — spend about $0.0030" in page
    assert f'action="/discover/jobs/{job_id}/approve"' in page
    assert f'action="/discover/jobs/{job_id}/cancel"' in page
    assert "hx-trigger" not in page  # an estimate doesn't poll

    assert "discovery.estimated" in await audit_actions(db)
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "discovery.estimated"))
    assert row is not None
    assert row.actor_id == member_id
    assert row.target_id == job_id
    assert row.details == {"side": "buyer", "searches": 3, "cost": "0.0030"}

    # The new job is listed on the discover page.
    listing = text_of(await client.get("/discover"))
    assert f"/discover/jobs/{job_id}" in listing

    assert (await client.get(f"/discover/jobs/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/discover/jobs/not-a-uuid")).status_code == 400


async def test_estimate_error_keeps_form(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    token = await csrf_for(client, "/discover?side=supplier")
    resp = await client.post(
        "/discover/estimate",
        data={"side": "supplier", "regions": ["Gujarat"], "csrf_token": token},
    )
    assert resp.status_code == 400
    page = text_of(resp)
    assert "Pick at least one product." in page
    assert re.search(r'value="supplier"[^>]*checked', page)
    assert re.search(r'value="Gujarat"[^>]*checked', page)

    # A selection that produces no searches keeps the chosen product checked too.
    resp = await client.post(
        "/discover/estimate",
        data={
            "side": "buyer",
            "products": ["labsa"],
            "industries": ["personal_care"],
            "regions": ["Texas"],
            "csrf_token": token,
        },
    )
    assert resp.status_code == 400
    page = text_of(resp)
    assert "Those choices produce no searches." in page
    assert re.search(r'value="labsa"[^>]*checked', page)
    assert re.search(r'value="personal_care"[^>]*checked', page)

    # Unknown side: message shown, nothing written.
    resp = await client.post(
        "/discover/estimate", data={**BUYER_FORM, "side": "both", "csrf_token": token}
    )
    assert resp.status_code == 400
    assert "Unknown side: both." in text_of(resp)
    assert await db.scalar(select(DiscoveryJob.id)) is None
    assert "discovery.estimated" not in await audit_actions(db)


async def test_approve_and_budget_refusal(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    member_id = member.id
    await login(client, member.email, password)
    job_id = await _estimate(client)
    await _set_budget(db, "0.00", member_id)

    token = await csrf_for(client, f"/discover/jobs/{job_id}")
    resp = await client.post(f"/discover/jobs/{job_id}/approve", data={"csrf_token": token})
    assert resp.status_code == 400
    page = text_of(resp)
    assert "budget is left" in page
    assert "This job needs $0.0030" in page
    assert (await _job(db, job_id)).status == "estimated"
    assert "discovery.approved" not in await audit_actions(db)

    await _set_budget(db, "1", member_id)
    resp = await client.post(f"/discover/jobs/{job_id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/discover/jobs/{job_id}"
    job = await _job(db, job_id)
    assert job.status == "approved"
    assert job.approved_by_id == member_id
    assert "discovery.approved" in await audit_actions(db)

    # Approving twice is refused with the service's message.
    resp = await client.post(f"/discover/jobs/{job_id}/approve", data={"csrf_token": token})
    assert resp.status_code == 400
    assert "Only an estimated job can be approved (this one is approved)." in text_of(resp)

    resp = await client.post(f"/discover/jobs/{uuid.uuid4()}/approve", data={"csrf_token": token})
    assert resp.status_code == 404


async def test_cancel(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    job_id = await _estimate(client)
    token = await csrf_for(client, f"/discover/jobs/{job_id}")
    resp = await client.post(f"/discover/jobs/{job_id}/cancel", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/discover/jobs/{job_id}"
    job = await _job(db, job_id)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert "discovery.cancelled" in await audit_actions(db)

    page = text_of(await client.get(f"/discover/jobs/{job_id}"))
    assert "Run — spend about" not in page
    assert "hx-trigger" not in page

    resp = await client.post(f"/discover/jobs/{job_id}/cancel", data={"csrf_token": token})
    assert resp.status_code == 400
    assert "Only a job that hasn't started can be cancelled" in text_of(resp)


async def test_status_fragment_and_htmx_redirect(
    client: httpx.AsyncClient, app: FastAPI, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    job_id = await _estimate(client)
    status_url = f"/discover/jobs/{job_id}/status"

    fragment = await client.get(status_url)
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert "hx-trigger" not in fragment.text  # estimated: nothing to poll for

    token = await csrf_for(client, f"/discover/jobs/{job_id}")
    resp = await client.post(f"/discover/jobs/{job_id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303

    page = await client.get(f"/discover/jobs/{job_id}")
    assert 'hx-trigger="every 3s"' in page.text
    assert f'hx-get="{status_url}"' in page.text
    fragment = await client.get(status_url)
    assert fragment.status_code == 200
    assert "<html" not in fragment.text
    assert 'hx-trigger="every 3s"' in fragment.text
    assert f'hx-get="{status_url}"' in fragment.text

    await db.execute(
        update(DiscoveryJob)
        .where(DiscoveryJob.id == uuid.UUID(job_id))
        .values(status="running", searches_done=1)
    )
    await db.commit()
    assert 'hx-trigger="every 3s"' in (await client.get(status_url)).text

    await db.execute(
        update(DiscoveryJob)
        .where(DiscoveryJob.id == uuid.UUID(job_id))
        .values(
            status="succeeded",
            searches_done=3,
            companies_new=7,
            companies_excluded=2,
            results_dropped=4,
        )
    )
    await db.commit()
    fragment = await client.get(status_url)
    assert fragment.status_code == 200
    assert "hx-trigger" not in fragment.text
    body = text_of(fragment)
    assert "succeeded" in body
    assert "7" in body and "Excluded" in body
    assert f"/companies?side=buyer&job={job_id}" in body

    await db.execute(
        update(DiscoveryJob)
        .where(DiscoveryJob.id == uuid.UUID(job_id))
        .values(status="failed", error="Serper said <no>.")
    )
    await db.commit()
    fragment = await client.get(status_url)
    assert "hx-trigger" not in fragment.text
    assert "Serper said &lt;no&gt;." in fragment.text

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as anon:
        resp = await anon.get(status_url, headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert resp.headers["hx-redirect"] == "/login"
        assert resp.text == ""
        plain = await anon.get(status_url)
        assert plain.status_code == 303
        assert plain.headers["location"].startswith("/login?next=")


async def test_csrf_required(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    resp = await client.post("/discover/estimate", data=BUYER_FORM)
    assert resp.status_code == 403
    resp = await client.post("/discover/estimate", data={**BUYER_FORM, "csrf_token": "forged"})
    assert resp.status_code == 403
    assert await db.scalar(select(DiscoveryJob.id)) is None

    job_id = await _estimate(client)
    for action in ("approve", "cancel"):
        resp = await client.post(f"/discover/jobs/{job_id}/{action}", data={})
        assert resp.status_code == 403
    assert (await _job(db, job_id)).status == "estimated"


async def test_discover_requires_login(client: httpx.AsyncClient) -> None:
    for path in ("/discover", "/companies", "/catalog"):
        resp = await client.get(path)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")
    resp = await client.post("/discover/estimate", data=BUYER_FORM)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


async def test_admin_can_use_discover(
    client: httpx.AsyncClient, admin: User, password: str
) -> None:
    await login(client, admin.email, password)
    job_id = await _estimate(client)
    page = text_of(await client.get(f"/discover/jobs/{job_id}"))
    assert "Run — spend about $0.0030" in page
    assert ">Users</a>" in page


def test_create_app_loads_config_once_and_refuses_a_broken_one(
    test_settings: Settings, tmp_path: Path
) -> None:
    app = create_app(test_settings)
    assert app.state.config.discovery.monthly_budget_usd == Decimal("20.00")
    assert app.state.catalog.by_id("sles").cas[0] == "68585-34-2"
    assert app.state.facts.company_name == "Elchemy"

    (tmp_path / "salesfinder.yaml").write_text("discovery:\n  pages_per_query: 9\n", "utf-8")
    broken = test_settings.model_copy(update={"config_dir": tmp_path})
    with pytest.raises(ConfigError, match="pages_per_query"):
        create_app(broken)
