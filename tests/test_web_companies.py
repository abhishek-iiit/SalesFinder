"""Companies list/detail, the catalog page, and the last_seen_at throttle."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Company, DiscoveryJob, Observation, User, UserSession
from salesfinder.signals.settings import set_state
from web_helpers import audit_actions, csrf_from, login, text_of

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
ROW_RE = re.compile(r'<tr class="company-row"')


def _company(
    name: str,
    domain: str,
    *,
    side: str = "buyer",
    status: str = "new",
    created_at: datetime = T0,
    **extra: object,
) -> Company:
    return Company(
        side=side,
        name=name,
        domain=domain,
        website_url=f"https://www.{domain}",
        status=status,
        created_at=created_at,
        **extra,
    )


def _rows(resp: httpx.Response) -> int:
    return len(ROW_RE.findall(resp.text))


async def test_companies_tabs_filters_and_paging(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    job = DiscoveryJob(
        side="buyer",
        params={},
        queries=[],
        pages_per_query=1,
        estimated_searches=0,
        estimated_cost_usd=Decimal(0),
        status="succeeded",
        created_by_id=member.id,
    )
    db.add(job)
    await db.flush()
    db.add_all(
        [
            _company(
                "Acme Shampoo",
                "acme.com",
                created_at=T0 + timedelta(minutes=3),
                country="US",
                region="Texas",
                discovered_by_job_id=job.id,
            ),
            _company("Bolt Cleaners", "bolt-cleaning.com", created_at=T0 + timedelta(minutes=2)),
            _company("Cosmo Labs", "acmelabs.io", created_at=T0 + timedelta(minutes=1)),
            _company(
                "Delta Soap",
                "delta.com",
                status="excluded",
                exclusion_reason="existing_relationship",
                exclusion_detail="HubSpot company Delta Soap (owner: Priya)",
            ),
            _company("Gujarat Chem", "gujchem.in", side="supplier"),
        ]
    )
    await db.commit()
    await login(client, member.email, password)

    resp = await client.get("/companies?side=buyer")
    assert resp.status_code == 200
    page = text_of(resp)
    assert _rows(resp) == 4
    assert "Buyers (4)" in page and "Suppliers (1)" in page
    assert "Gujarat Chem" not in page
    # Newest first.
    assert page.index("Acme Shampoo") < page.index("Bolt Cleaners") < page.index("Cosmo Labs")
    # Website links open in a new tab without leaking the opener or referrer.
    assert re.search(
        r'<a href="https://www\.acme\.com"[^>]*target="_blank"[^>]*rel="noopener noreferrer"',
        resp.text,
    )
    assert "Texas" in page
    assert f"/discover/jobs/{job.id}" in page

    # Default side is buyer; the supplier tab shows the supplier only. Tab counts stay totals.
    assert _rows(await client.get("/companies")) == 4
    suppliers = await client.get("/companies?side=supplier")
    assert _rows(suppliers) == 1 and "Gujarat Chem" in suppliers.text

    excluded = await client.get("/companies?side=buyer&status=excluded")
    assert _rows(excluded) == 1
    text = text_of(excluded)
    assert "Delta Soap" in text
    assert "Existing relationship" in text
    assert "HubSpot company Delta Soap (owner: Priya)" in text
    assert "Buyers (4)" in text and "Suppliers (1)" in text

    assert _rows(await client.get("/companies?side=buyer&status=new")) == 3
    assert _rows(await client.get("/companies?side=buyer&status=")) == 4

    # q matches name or domain, case-insensitively; LIKE wildcards are literal.
    searched = await client.get("/companies?side=buyer&q=ACME")
    assert _rows(searched) == 2
    assert "Acme Shampoo" in searched.text and "Cosmo Labs" in searched.text
    assert _rows(await client.get("/companies?side=buyer&q=bolt-clean")) == 1
    assert _rows(await client.get("/companies?side=buyer&q=%25")) == 0
    assert _rows(await client.get("/companies?side=buyer&q=_")) == 0

    by_job = await client.get(f"/companies?side=buyer&job={job.id}")
    assert _rows(by_job) == 1 and "Acme Shampoo" in by_job.text

    # Bad parameters are a 400, never a 500.
    for bad in (
        "side=both",
        "status=maybe",
        "job=not-a-uuid",
        "page=0",
        "page=abc",
        "q=a%00b",
        f"q={'x' * 201}",
    ):
        assert (await client.get(f"/companies?{bad}")).status_code == 400, bad

    # 55 buyer rows -> 50 on page 1, 5 on page 2; paging keeps the filters.
    db.add_all(
        _company(f"Bulk {i:02d}", f"bulk{i:02d}.com", created_at=T0 - timedelta(minutes=i))
        for i in range(51)
    )
    await db.commit()
    first = await client.get("/companies?side=buyer&status=new&q=bulk")
    assert _rows(first) == 50
    assert "/companies?side=buyer&amp;status=new&amp;q=bulk&amp;page=2" in first.text
    second = await client.get("/companies?side=buyer&status=new&q=bulk&page=2")
    assert _rows(second) == 1
    assert "/companies?side=buyer&amp;status=new&amp;q=bulk&amp;page=1" in second.text

    everything = await client.get("/companies?side=buyer&page=2")
    assert _rows(everything) == 5
    assert "Buyers (55)" in text_of(everything)
    assert _rows(await client.get("/companies?side=buyer&page=9")) == 0


async def test_company_detail_escapes_snippet(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    company = _company(
        "Evil <b>Corp</b>",
        "evil.com",
        snippet="<script>x</script>",
        discovery_query='"sles" shampoo manufacturer Texas',
        status="excluded",
        exclusion_reason="do_not_contact",
        exclusion_detail="Do-not-contact: evil.com — asked us to stop",
    )
    db.add(company)
    await db.commit()
    await login(client, member.email, password)

    resp = await client.get(f"/companies/{company.id}")
    assert resp.status_code == 200
    assert "<script>x</script>" not in resp.text
    assert "&lt;script&gt;x&lt;/script&gt;" in resp.text
    assert "<b>Corp</b>" not in resp.text
    page = text_of(resp)
    assert '"sles" shampoo manufacturer Texas' in page
    assert "Do not contact" in page
    assert "Do-not-contact: evil.com — asked us to stop" in page
    assert 'rel="noopener noreferrer"' in resp.text

    listing = await client.get("/companies?side=buyer")
    assert "<b>Corp</b>" not in listing.text

    assert (await client.get(f"/companies/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/companies/nope")).status_code == 400


async def test_catalog_page(client: httpx.AsyncClient, member: User, password: str) -> None:
    await login(client, member.email, password)
    resp = await client.get("/catalog")
    assert resp.status_code == 200
    page = text_of(resp)
    assert "68585-34-2" in page
    assert "Sodium Laureth Sulfate (SLES 70%)" in page
    assert "Personal Care" in page
    assert "Samples dispatched within 24\u201348 hours" in page
    assert (
        "Only confirmed facts will be used in emails. Edit config/catalog.yaml and "
        "config/facts.yaml, then restart."
    ) in page
    assert "✗" in page


async def test_last_seen_throttled(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(client, member.email, password)
    session_row = await db.scalar(select(UserSession))
    assert session_row is not None
    first_seen = session_row.last_seen_at
    await db.commit()

    for _ in range(2):
        assert (await client.get("/companies")).status_code == 200
    await db.refresh(session_row)
    assert session_row.last_seen_at == first_seen
    await db.commit()

    # Older than 60 s: the next request records the activity.
    stale = first_seen - timedelta(seconds=61)
    await db.execute(update(UserSession).values(last_seen_at=stale))
    await db.commit()
    assert (await client.get("/catalog")).status_code == 200
    await db.refresh(session_row)
    assert session_row.last_seen_at > first_seen


async def test_company_detail_shows_signals_evidence_escaped(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    company = _company("Acme Shampoo", "acme.com", profile_status="partial", profile_error="slow")
    db.add(company)
    await db.flush()
    db.add_all(
        [
            Observation(
                company_id=company.id,
                code="USES_CATALOG_CHEMICAL",
                value=["sles"],
                evidence_snippet="<img src=x onerror=alert(1)>",
                evidence_url="https://acme.com/products",
                is_latest=True,
            ),
            Observation(
                company_id=company.id,
                code="US_LOCATION",
                value={"state": "TX", "city": "Houston"},
                evidence_snippet="Houston, TX 77001",
                evidence_url="javascript:alert(1)",
                is_latest=True,
            ),
            # Not confirmed: must not be listed.
            Observation(
                company_id=company.id, code="QUALITY_CERTS", value=["ISO 9001"], is_latest=True
            ),
        ]
    )
    await set_state(db, "USES_CATALOG_CHEMICAL", "confirmed", actor=member)
    await set_state(db, "US_LOCATION", "confirmed", actor=member)
    await db.commit()
    await login(client, member.email, password)

    resp = await client.get(f"/companies/{company.id}")
    assert resp.status_code == 200
    assert "<img src=x" not in resp.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in resp.text
    page = text_of(resp)
    assert "Sodium Laureth Sulfate (SLES 70%)" in page
    assert "Houston, TX" in page
    assert "ISO 9001" not in page
    assert "javascript:" not in page
    assert re.search(
        r'<a href="https://acme.com/products"[^>]*target="_blank"[^>]*rel="noopener noreferrer"',
        resp.text,
    )
    assert "Profile again" in page and "slow" in page
    # Score reasons are recomputed from the latest observations.
    assert "Score" in page and "(+" in page


async def test_reprofile_button(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    ok = _company("Acme", "acme.com", profile_status="ok")
    gone = _company("Delta", "delta.com", status="excluded", exclusion_reason="do_not_contact")
    db.add_all([ok, gone])
    await db.flush()
    ok_id, gone_id = ok.id, gone.id
    await db.commit()
    await login(client, member.email, password)

    page = await client.get(f"/companies/{ok_id}")
    assert "Profile again" in page.text
    token = csrf_from(page.text)
    resp = await client.post(f"/companies/{ok_id}/reprofile", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/companies/{ok_id}?msg=queued"
    flashed = await client.get(resp.headers["location"])
    assert "Queued — the worker will profile it shortly." in text_of(flashed)
    db.expire_all()
    refreshed = await db.get(Company, ok_id)
    assert refreshed is not None and refreshed.profile_status == "pending"
    assert "company.reprofile_requested" in await audit_actions(db)

    excluded_page = await client.get(f"/companies/{gone_id}")
    assert "Profile again" not in excluded_page.text
    bad = await client.post(f"/companies/{gone_id}/reprofile", data={"csrf_token": token})
    assert bad.status_code == 400
    assert "Excluded companies aren't profiled." in text_of(bad)
    missing = await client.post(f"/companies/{uuid.uuid4()}/reprofile", data={"csrf_token": token})
    assert missing.status_code == 404
    forged = await client.post(f"/companies/{ok_id}/reprofile", data={})
    assert forged.status_code == 403


async def test_sort_by_score(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    db.add_all(
        [
            _company("Low", "low.com", score=20, created_at=T0 + timedelta(minutes=3)),
            _company("High", "high.com", score=90, created_at=T0),
            _company("Unscored", "un.com", created_at=T0 + timedelta(minutes=5)),
            _company("Dq", "dq.com", score=0, disqualified=True, created_at=T0 + timedelta(1)),
        ]
    )
    await db.commit()
    await login(client, member.email, password)

    by_score = text_of(await client.get("/companies?sort=score"))
    order = [by_score.index(n) for n in (">High<", ">Low<", ">Dq<", ">Unscored<")]
    assert order == sorted(order)
    assert "✗" in by_score
    newest = text_of(await client.get("/companies?sort=newest"))
    assert newest.index(">Dq<") < newest.index(">Unscored<") < newest.index(">Low<")
    assert (await client.get("/companies?sort=bogus")).status_code == 400
    # Paging links keep the sort.
    db.add_all(_company(f"Bulk {i:02d}", f"bulk{i:02d}.com", score=i) for i in range(55))
    await db.commit()
    assert "sort=score" in text_of(await client.get("/companies?sort=score"))
    assert (
        "sort=" not in text_of(await client.get("/companies?q=bulk")).split('rel="next"')[0][-80:]
    )


async def test_excluded_rows_show_no_score_and_sort_last(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    db.add_all(
        [
            _company(
                "Gone",
                "gone.com",
                status="excluded",
                exclusion_reason="do_not_contact",
                score=97,
                created_at=T0 + timedelta(minutes=9),
            ),
            _company("Good", "good.com", score=40),
            _company("Unscored", "un.com", created_at=T0 + timedelta(minutes=5)),
        ]
    )
    await db.commit()
    await login(client, member.email, password)

    by_score = text_of(await client.get("/companies?sort=score"))
    order = [by_score.index(n) for n in (">Good<", ">Unscored<", ">Gone<")]
    assert order == sorted(order)
    gone_row = by_score[by_score.index(">Gone<") :].split("</tr>", 1)[0]
    assert "97" not in gone_row
    assert '<td class="num"><span class="muted">—</span></td>' in gone_row
