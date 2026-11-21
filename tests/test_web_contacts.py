"""People & email on the company page, address overrides, and the list's Contacts column."""

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import (
    AuditLog,
    Company,
    DomainMx,
    DoNotContact,
    EmailAddress,
    EmailPattern,
    Person,
    User,
)
from web_helpers import audit_actions, csrf_from, login, text_of

NOW = datetime(2026, 11, 1, tzinfo=UTC)


async def _seed(db: AsyncSession, *, status: str = "new", mx: str | None = "has_mx") -> uuid.UUID:
    company = Company(
        side="buyer",
        name="Acme",
        domain="acme.com",
        website_url="https://www.acme.com",
        status=status,
        profile_status="ok",
        contact_form_url="https://www.acme.com/contact",
        contacts_found=1,
        sendable_contacts=1,
        contacts_checked_at=NOW,
    )
    db.add(company)
    await db.flush()
    priya = Person(
        company_id=company.id,
        full_name="Priya Nair",
        name_key="priya nair",
        first_name="Priya",
        last_name="Nair",
        title="Head of Procurement",
        title_category="procurement",
        is_target=True,
        rank=1,
        source_url="https://www.acme.com/team",
        source_snippet="Priya Nair | Head of Procurement <script>alert(1)</script>",
    )
    sam = Person(
        company_id=company.id,
        full_name="Sam Other",
        name_key="sam other",
        title="Receptionist",
        title_category="other",
        is_target=False,
        source_url="https://www.acme.com/about",
    )
    db.add_all([priya, sam])
    await db.flush()
    for address, st, source, kind, person in (
        ("priya.nair@acme.com", "published", "page", "personal", priya),
        ("sam.other@acme.com", "pattern_guess", "guess", "personal", sam),
        ("bad@acme.com", "invalid", "guess", "personal", None),
        ("jo.risky@acme.com", "risky", "guess", "personal", None),
    ):
        db.add(
            EmailAddress(
                company_id=company.id,
                person_id=person.id if person else None,
                address=address,
                kind=kind,
                status=st,
                source=source,
                evidence_url="https://www.acme.com/team" if st == "published" else None,
                evidence_snippet="<script>alert(2)</script>" if st == "published" else None,
            )
        )
    if mx:
        db.add(DomainMx(domain="acme.com", state=mx, mx_hosts=[], checked_at=NOW))
    db.add(
        EmailPattern(
            domain="acme.com", pattern="{first}.{last}", support=2, examples=[], learned_at=NOW
        )
    )
    cid = company.id
    await db.commit()
    return cid


async def _address(db: AsyncSession, address: str) -> EmailAddress:
    row = await db.scalar(
        select(EmailAddress)
        .where(EmailAddress.address == address)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def test_company_page_shows_people_and_statuses(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    await login(client, member.email, password)
    page = text_of(await client.get(f"/companies/{cid}"))
    assert "People &amp; email" in page or "People & email" in page
    assert "★" in page and "Priya Nair" in page and "Head of Procurement" in page
    assert "Procurement" in page and "Sam Other" in page
    for label in (
        "Published on their site",
        "Our best guess — held",
        "Won't deliver",
        "Risky (catch-all) — held",
    ):
        assert label in page
    assert "Held: guess" in page
    assert "Pattern: first.last@acme.com (from 2 addresses)" in page
    assert "Mail server" in page
    assert 'href="https://www.acme.com/contact"' in page
    assert 'href="https://www.acme.com/team"' in page
    assert 'rel="noopener noreferrer"' in page
    assert "Allow sending" in page


async def test_no_pattern_and_no_mx_copy(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db, mx="no_mail")
    await db.execute(EmailPattern.__table__.delete())
    await db.commit()
    await login(client, member.email, password)
    page = text_of(await client.get(f"/companies/{cid}"))
    assert "No email pattern learned yet." in page
    assert "No mail server" in page


async def test_excluded_company_has_no_contacts_section(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db, status="excluded")
    await login(client, member.email, password)
    page = text_of(await client.get(f"/companies/{cid}"))
    assert "People & email" not in page and "priya.nair@acme.com" not in page
    token = csrf_from((await client.get("/companies")).text)
    row_id = (await _address(db, "sam.other@acme.com")).id
    resp = await client.post(
        f"/companies/{cid}/emails/{row_id}/override",
        data={"csrf_token": token, "decision": "allow"},
    )
    assert resp.status_code == 400
    assert "Excluded companies aren't contacted." in text_of(resp)


async def test_override_flow_audited(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    row_id = (await _address(db, "sam.other@acme.com")).id
    await login(client, member.email, password)
    url = f"/companies/{cid}/emails/{row_id}/override"
    token = csrf_from((await client.get(f"/companies/{cid}")).text)

    resp = await client.post(url, data={"csrf_token": token, "decision": "allow"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/companies/{cid}?msg=")
    page = text_of(await client.get(resp.headers["location"]))
    assert "Overridden by Mira Member" in page
    assert "Sending to this address is now allowed." in page
    refreshed = await _address(db, "sam.other@acme.com")
    assert refreshed.override_sendable is True and refreshed.override_by_id == member.id

    resp = await client.post(url, data={"csrf_token": token, "decision": "clear"})
    assert resp.status_code == 303
    assert "Held: guess" in text_of(await client.get(f"/companies/{cid}"))
    assert (await _address(db, "sam.other@acme.com")).override_sendable is None

    resp = await client.post(url, data={"csrf_token": token, "decision": "block"})
    assert resp.status_code == 303
    assert "Overridden by Mira Member: don't send" in text_of(await client.get(f"/companies/{cid}"))

    actions = await audit_actions(db)
    assert actions.count("email.override_set") == 2
    assert actions.count("email.override_cleared") == 1
    rows = list(
        await db.scalars(
            select(AuditLog).where(AuditLog.action == "email.override_set").order_by(AuditLog.at)
        )
    )
    assert rows[0].details == {"address": "sam.other@acme.com", "old": None, "new": True}
    assert rows[1].details == {"address": "sam.other@acme.com", "old": None, "new": False}
    assert rows[0].target_id == str(row_id)

    bad = await client.post(url, data={"csrf_token": token, "decision": "nonsense"})
    assert bad.status_code == 400


async def test_invalid_has_no_allow_button_and_route_rejects(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    row_id = (await _address(db, "bad@acme.com")).id
    await login(client, member.email, password)
    page = (await client.get(f"/companies/{cid}")).text
    token = csrf_from(page)
    # only the held ones (guess, risky): not the invalid one, nor the sendable published one
    assert page.count("Allow sending") == 2
    invalid_row = page.split("bad@acme.com")[1].split("</tr>")[0]
    assert "Allow sending" not in invalid_row and "Won&#39;t deliver" in invalid_row
    resp = await client.post(
        f"/companies/{cid}/emails/{row_id}/override",
        data={"csrf_token": token, "decision": "allow"},
    )
    assert resp.status_code == 400
    assert "Addresses that won't deliver can't be allowed." in text_of(resp)
    assert (await _address(db, "bad@acme.com")).override_sendable is None


async def test_override_unknown_or_foreign_address_404(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    other = Company(
        side="buyer",
        status="new",
        name="Other",
        domain="other.com",
        website_url="https://other.com",
    )
    db.add(other)
    await db.flush()
    other_id = other.id
    await db.commit()
    row_id = (await _address(db, "sam.other@acme.com")).id
    await login(client, member.email, password)
    token = csrf_from((await client.get(f"/companies/{cid}")).text)
    data = {"csrf_token": token, "decision": "allow"}
    for path in (
        f"/companies/{cid}/emails/{uuid.uuid4()}/override",
        f"/companies/{other_id}/emails/{row_id}/override",
        f"/companies/{uuid.uuid4()}/emails/{row_id}/override",
    ):
        assert (await client.post(path, data=data)).status_code == 404
    assert (await _address(db, "sam.other@acme.com")).override_sendable is None


async def test_admin_can_override(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    row_id = (await _address(db, "jo.risky@acme.com")).id
    await login(client, admin.email, password)
    token = csrf_from((await client.get(f"/companies/{cid}")).text)
    resp = await client.post(
        f"/companies/{cid}/emails/{row_id}/override",
        data={"csrf_token": token, "decision": "allow"},
    )
    assert resp.status_code == 303


async def test_override_csrf_and_escaping(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    cid = await _seed(db)
    row_id = (await _address(db, "sam.other@acme.com")).id
    await login(client, member.email, password)
    url = f"/companies/{cid}/emails/{row_id}/override"
    assert (await client.post(url, data={"decision": "allow"})).status_code == 403
    assert (
        await client.post(url, data={"csrf_token": "nope", "decision": "allow"})
    ).status_code == 403
    assert (await _address(db, "sam.other@acme.com")).override_sendable is None
    raw = (await client.get(f"/companies/{cid}")).text
    assert "<script>alert" not in raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in raw


async def test_override_requires_login(client: httpx.AsyncClient, db: AsyncSession) -> None:
    resp = await client.post(
        f"/companies/{uuid.uuid4()}/emails/{uuid.uuid4()}/override",
        data={"decision": "allow"},
    )
    assert resp.status_code in (303, 401, 403)


async def test_list_contacts_column(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    db.add_all(
        [
            Company(
                side="buyer",
                status="new",
                name="Checked Co",
                domain="checked.com",
                website_url="https://checked.com",
                contacts_found=2,
                sendable_contacts=1,
                contacts_checked_at=NOW,
            ),
            Company(
                side="buyer",
                status="new",
                name="Fresh Co",
                domain="fresh.com",
                website_url="https://fresh.com",
            ),
        ]
    )
    await db.commit()
    await login(client, member.email, password)
    page = text_of(await client.get("/companies"))
    assert "<th>Contacts</th>" in page
    assert ">1 / 2<" in page
    fresh = page.split("Fresh Co")[1].split("</tr>")[0]
    assert "/ " not in fresh.split("</td>")[-2]


# --- final review fixes ---------------------------------------------------------------------------


async def test_dnc_email_added_after_a_check_shows_at_once(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    """Item 4: the page and the count reflect a new do-not-contact entry immediately."""
    cid = await _seed(db)
    await login(client, admin.email, password)
    token = csrf_from((await client.get("/admin/do-not-contact")).text)

    resp = await client.post(
        "/admin/do-not-contact",
        data={"entry": "priya.nair@acme.com", "reason": "Asked us to stop", "csrf_token": token},
    )
    assert resp.status_code == 303
    page = text_of(await client.get(f"/companies/{cid}"))
    row = page.split('<td class="break mono">priya.nair@acme.com</td>')[1].split("</tr>")[0]
    assert "Do-not-contact: priya.nair@acme.com — Asked us to stop" in row
    assert "<strong>Yes</strong>" not in row
    company = await db.get(Company, cid, populate_existing=True)
    assert company is not None and company.sendable_contacts == 0

    entry_id = await db.scalar(select(DoNotContact.id))
    resp = await client.post(f"/admin/do-not-contact/{entry_id}/remove", data={"csrf_token": token})
    assert resp.status_code == 303
    company = await db.get(Company, cid, populate_existing=True)
    assert company is not None and company.sendable_contacts == 1
    assert (await _address(db, "priya.nair@acme.com")).blocked_reason is None


async def test_allow_only_where_it_lifts_something(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """Item 9: no Allow on a sendable address (the route says so), nor on a no-reply mailbox;
    Allow on a published address the site dropped."""
    cid = await _seed(db)
    db.add_all(
        [
            EmailAddress(
                company_id=cid,
                address="old.name@acme.com",
                kind="personal",
                status="published",
                source="page",
                stale=True,
            ),
            EmailAddress(
                company_id=cid,
                address="noreply@acme.com",
                kind="role",
                status="published",
                source="page",
            ),
        ]
    )
    await db.commit()
    await login(client, member.email, password)
    page = text_of(await client.get(f"/companies/{cid}"))
    token = csrf_from(page)

    def row_of(address: str) -> str:
        return page.split(address)[1].split("</tr>")[0]

    assert "Allow sending" not in row_of("priya.nair@acme.com")
    assert "Allow sending" not in row_of("noreply@acme.com")
    assert "Not a person's mailbox" in row_of("noreply@acme.com")
    assert "Allow sending" in row_of("old.name@acme.com")
    assert "No longer on their site" in row_of("old.name@acme.com")

    for address, detail in (
        ("priya.nair@acme.com", "This address is already sendable."),
        ("noreply@acme.com", "Allowing sending can't lift what holds this address back."),
    ):
        row_id = (await _address(db, address)).id
        resp = await client.post(
            f"/companies/{cid}/emails/{row_id}/override",
            data={"csrf_token": token, "decision": "allow"},
        )
        assert resp.status_code == 400, address
        assert detail in text_of(resp)
        assert (await _address(db, address)).override_sendable is None

    stale_id = (await _address(db, "old.name@acme.com")).id
    resp = await client.post(
        f"/companies/{cid}/emails/{stale_id}/override",
        data={"csrf_token": token, "decision": "allow"},
    )
    assert resp.status_code == 303
    after = text_of(await client.get(f"/companies/{cid}")).split("old.name@acme.com")[1]
    assert "<strong>Yes</strong>" in after.split("</tr>")[0]


async def test_people_notes(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """Item 6: why there are no people."""
    company = Company(
        side="buyer",
        name="Bare",
        domain="bare.com",
        website_url="https://bare.com",
        status="new",
        profile_status="pending",
    )
    db.add(company)
    await db.commit()
    cid = company.id
    await login(client, member.email, password)

    async def note(**fields: object) -> str:
        for key, value in fields.items():
            setattr(company, key, value)
        await db.commit()
        return text_of(await client.get(f"/companies/{cid}"))

    assert "No people found on the company's own pages yet." in await note()
    profiled = {"profile_status": "ok", "profiled_at": NOW}
    assert "No team, about or contact page found." in await note(**profiled)
    assert "Read 1 people page — no named people with titles." in await note(people_pages_read=1)
    assert "Read 3 people pages — no named people with titles." in await note(people_pages_read=3)
    assert "Couldn't read people from the pages (error)." in await note(
        contacts_extract_failed=True
    )
    # with people kept from an earlier read, the error still shows
    cid2 = await _seed(db)
    seeded = await db.get(Company, cid2)
    assert seeded is not None
    seeded.contacts_extract_failed = True
    await db.commit()
    page = text_of(await client.get(f"/companies/{cid2}"))
    assert "Couldn't read people from the pages (error)." in page and "Priya Nair" in page
