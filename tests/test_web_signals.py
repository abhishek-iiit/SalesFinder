"""The Signals page: states, rescoring, audit, confirm-all, CSRF."""

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import AuditLog, Company, Observation, User
from salesfinder.signals.catalog import SIGNALS
from salesfinder.signals.settings import confirmed_codes, states
from web_helpers import audit_actions, csrf_for, login, text_of


async def test_signals_page_lists_both_sides(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    db.add(
        Company(
            side="buyer",
            name="Acme",
            domain="acme.com",
            website_url="https://acme.com",
            status="new",
        )
    )
    await db.flush()
    company = await db.scalar(select(Company))
    assert company is not None
    db.add_all(
        [
            Observation(
                company_id=company.id, code="QUALITY_CERTS", value=["ISO 9001"], is_latest=True
            ),
            Observation(
                company_id=company.id, code="US_LOCATION", value={"found": False}, is_latest=True
            ),
        ]
    )
    await db.commit()
    await login(client, member.email, password)
    resp = await client.get("/signals")
    assert resp.status_code == 200
    page = text_of(resp)
    assert "Buyer signals" in page and "Supplier signals" in page
    assert "Mentions chemicals we sell" in page and "Makes it themselves" in page
    assert "Profiling only reads a company's website for confirmed signals." in page
    assert "Free" in page and "Confirm all free signals" in page
    # Only the present observation counts as observed.
    assert 'data-code="QUALITY_CERTS"' in page
    row = page.split('data-code="QUALITY_CERTS"')[1].split("</tr>")[0]
    assert '<td class="num">1</td>' in row
    row = page.split('data-code="US_LOCATION"')[1].split("</tr>")[0]
    assert '<td class="num">0</td>' in row
    assert 'href="/signals"' in page


async def test_change_state_audits_and_rescores(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    company = Company(
        side="buyer", name="Acme", domain="acme.com", website_url="https://acme.com", status="new"
    )
    db.add(company)
    await db.flush()
    company_id, member_id = company.id, member.id
    db.add(
        Observation(company_id=company_id, code="QUALITY_CERTS", value=["ISO 9001"], is_latest=True)
    )
    await db.commit()
    await login(client, member.email, password)
    token = await csrf_for(client, "/signals")

    resp = await client.post(
        "/signals/QUALITY_CERTS/state", data={"csrf_token": token, "state": "confirmed"}
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/signals?msg=saved"
    followed = await client.get(resp.headers["location"])
    assert "Saved. Scores updated." in text_of(followed)

    db.expire_all()
    scored = await db.get(Company, company_id)
    assert scored is not None and scored.score == 10
    assert (await states(db))["QUALITY_CERTS"] == "confirmed"
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "signal.state_changed"))
    assert log is not None
    assert log.actor_id == member_id and log.target_id == "QUALITY_CERTS"
    assert log.details["old"] == "proposed" and log.details["new"] == "confirmed"

    # Unknown msg keys are never echoed.
    junk = await client.get("/signals?msg=<script>x</script>")
    assert "<script>x" not in junk.text and "Saved." not in junk.text


async def test_confirm_free(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    token = await csrf_for(client, "/signals")
    resp = await client.post("/signals/confirm-free", data={"csrf_token": token})
    assert resp.status_code == 303
    assert "Saved. Scores updated." in text_of(await client.get(resp.headers["location"]))
    assert len(await confirmed_codes(db)) == len(SIGNALS) == 14
    assert "signal.state_changed" in await audit_actions(db)


async def test_bad_state_400_and_csrf_403(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    token = await csrf_for(client, "/signals")
    for bad in ("maybe", ""):
        resp = await client.post(
            "/signals/QUALITY_CERTS/state", data={"csrf_token": token, "state": bad}
        )
        assert resp.status_code == 400, bad
    missing = await client.post("/signals/QUALITY_CERTS/state", data={"csrf_token": token})
    assert missing.status_code == 400
    unknown = await client.post(
        "/signals/NOPE/state", data={"csrf_token": token, "state": "confirmed"}
    )
    assert unknown.status_code == 404
    forged = await client.post("/signals/QUALITY_CERTS/state", data={"state": "confirmed"})
    assert forged.status_code == 403
    assert (await client.post("/signals/confirm-free", data={"csrf_token": "x"})).status_code == 403
    assert "signal.state_changed" not in await audit_actions(db)


async def test_signals_requires_login(client: httpx.AsyncClient) -> None:
    resp = await client.get("/signals")
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
