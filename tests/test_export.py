"""The companies CSV export: which companies and contacts it leaves out (re-checked live), the
formula-injection guard, the file's shape, and the admin-only, CSRF-protected, audited
download."""

import csv
import io
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import (
    CATALOG,
    CONFIG,
    NOW,
    add_address,
    add_person,
    seed_campaign,
    seed_company,
    seed_draft,
)
from salesfinder import export
from salesfinder.config import AppConfig, ContactsConfig
from salesfinder.db.models import (
    AuditLog,
    Company,
    Conversation,
    DoNotContact,
    EmailAddress,
    KnownRelationship,
    Observation,
    Sequence,
    Suppression,
    User,
)
from salesfinder.signals.settings import set_state
from web_helpers import csrf_for, login


def _domains(rows: list[dict[str, str]]) -> list[str]:
    return [r["domain"] for r in rows]


def _known(value: str, kind: str = "domain") -> KnownRelationship:
    return KnownRelationship(
        source="hubspot",
        kind=kind,
        value=value,
        hubspot_object="company" if kind == "domain" else "contact",
        hubspot_id="1",
        synced_at=NOW,
    )


# --------------------------------------------------------------------------- csv_cell


@pytest.mark.parametrize(
    ("raw", "cell"),
    [
        ('=HYPERLINK("x")', '\'=HYPERLINK("x")'),
        ("+1 555", "'+1 555"),
        ("-5", "'-5"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tTab", "' Tab"),
        ("\rCR", "' CR"),
        ("a\nb", "a b"),
        ("a\x0bb\x0cc\x85d\u2028e\u2029f", "a b c d e f"),
        ("x;=cmd", "x;'=cmd"),
        ("x; =cmd", "x; '=cmd"),
        ("x;\t+1", "x; '+1"),
        ("a;b;@SUM(A1)", "a;b;'@SUM(A1)"),
        ("x;\uff20y", "x;'\uff20y"),
        ("a;b", "a;b"),
        ("=a;-b", "'=a;'-b"),
        ("a\r\nb\tc", "a  b c"),
        ("Müller GmbH", "Müller GmbH"),
        ("  =1+1", "'  =1+1"),
        (" @x", "' @x"),
        ("\n=cmd", "' =cmd"),
        ("\uff1d1+1", "'\uff1d1+1"),
        ("a=b", "a=b"),
        ("", ""),
        (None, ""),
        (42, "42"),
        ("plain text", "plain text"),
    ],
)
def test_csv_cell(raw: object, cell: str) -> None:
    assert export.csv_cell(raw) == cell


def test_columns_follow_max_targets() -> None:
    assert export.columns_for(2) == export.COLUMNS
    three = export.columns_for(3)
    assert "contact_3_email_status" in three
    assert three[-3:] == ("outreach_status", "last_sent_at", "reply_intent")
    assert export.columns_for(1).count("contact_1_name") == 1
    assert "contact_2_name" not in export.columns_for(1)


def test_to_csv_bom_header_and_guard() -> None:
    row = dict.fromkeys(export.COLUMNS, "")
    row.update(
        side="buyer",
        company='=HYPERLINK("http://evil")',
        domain="evil.com",
        contact_1_title="Head of\nBuying",
        contact_1_name="Zoë Ångström",
    )
    text = export.to_csv([row])
    assert text.startswith("﻿")
    assert "\r\n" in text
    parsed = list(csv.reader(io.StringIO(text[1:])))
    assert tuple(parsed[0]) == export.COLUMNS
    assert len(parsed) == 2
    out = dict(zip(parsed[0], parsed[1], strict=True))
    assert out["company"] == '\'=HYPERLINK("http://evil")'
    assert out["contact_1_title"] == "Head of Buying"
    assert out["contact_1_name"] == "Zoë Ångström"
    # Every line ends CRLF and no bare newline survives inside a cell.
    assert text.count("\n") == text.count("\r\n") == 2


def test_to_csv_empty_is_header_only() -> None:
    text = export.to_csv([])
    assert text == "﻿" + ",".join(export.COLUMNS) + "\r\n"


# --------------------------------------------------------------------------- company_rows


async def _rows(db: AsyncSession, side: str = "all", config: AppConfig = CONFIG) -> list[dict]:
    await db.commit()  # the export opens its own REPEATABLE READ transaction
    return await export.company_rows(db, side=side, config=config, catalog=CATALOG)  # type: ignore[arg-type]


async def test_excluded_companies_dropped_live(db: AsyncSession) -> None:
    await seed_company(db, domain="keep.com", score=10)
    excluded, _, _ = await seed_company(db, domain="excluded.com")
    excluded.status = "excluded"
    excluded.exclusion_reason = "do_not_contact"
    # On the do-not-contact list, but the sweep hasn't run: its status is still "new".
    await seed_company(db, domain="dnc.com")
    db.add(DoNotContact(kind="domain", value="dnc.com", reason="Asked us to stop"))
    # HubSpot knows the domain (also not swept).
    await seed_company(db, domain="hubspot-known.com")
    db.add(_known("hubspot-known.com"))
    disqualified, _, _ = await seed_company(db, domain="disq.com")
    disqualified.disqualified = True
    await db.commit()

    rows = await _rows(db)
    assert _domains(rows) == ["keep.com"]
    # Only the flag keeps it out.
    await db.execute(
        update(Company).where(Company.id == disqualified.id).values(disqualified=False)
    )
    assert _domains(await _rows(db)) == ["disq.com", "keep.com"]


async def test_company_with_suppressed_address_dropped(db: AsyncSession) -> None:
    company, person, _ = await seed_company(db, domain="unsub.com")
    # A second, non-best address of the company is unsubscribed.
    await add_address(db, company, person, "old@unsub.com", status="pattern_confirmed")
    db.add(Suppression(address="old@unsub.com", reason="unsubscribed", detail="link"))
    await seed_company(db, domain="fine.com")
    await db.commit()
    assert _domains(await _rows(db)) == ["fine.com"]


async def test_company_with_suppressed_sequence_recipient_dropped(db: AsyncSession) -> None:
    campaign = await seed_campaign(db)
    message = await seed_draft(db, campaign, domain="seq.com")
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    # The recipient was emailed at an address no longer stored for the company.
    sequence.recipient = "gone@seq.com"
    sequence.email_address_id = None
    db.add(Suppression(address="gone@seq.com", reason="bounced", detail="5.1.1"))
    await seed_company(db, domain="fine.com")
    await db.commit()
    assert _domains(await _rows(db)) == ["fine.com"]


async def test_dnc_email_blanks_only_that_contact(db: AsyncSession) -> None:
    await seed_company(db, domain="acme.com", address="jane")
    db.add(DoNotContact(kind="email", value="jane@acme.com", reason="Personal request"))
    await db.commit()
    rows = await _rows(db)
    assert _domains(rows) == ["acme.com"]
    row = rows[0]
    for col in ("name", "title", "email", "email_status"):
        assert row[f"contact_1_{col}"] == ""
        assert row[f"contact_2_{col}"] == ""


async def test_hubspot_known_email_drops_contact(db: AsyncSession) -> None:
    company, _, _ = await seed_company(db, domain="acme.com", address="jane")
    bob = await add_person(db, company, "Bob Stone", rank=2, title="Buyer")
    await add_address(db, company, bob, "bob@acme.com")
    db.add(_known("jane@acme.com", kind="email"))
    await db.commit()
    row = (await _rows(db))[0]
    # Jane is dropped; Bob moves up.
    assert row["contact_1_name"] == "Bob Stone"
    assert row["contact_1_email"] == "bob@acme.com"
    assert row["contact_2_name"] == ""


async def test_invalid_dnc_only_address_drops_person(db: AsyncSession) -> None:
    """Ruling P7-E: an invalid address is never shown, but it still counts for exclusions."""
    company, _, _ = await seed_company(db, domain="acme.com", address="jane", status="invalid")
    bob = await add_person(db, company, "Bob Stone", rank=2)
    await add_address(db, company, bob, "bob@acme.com")
    db.add(DoNotContact(kind="email", value="jane@acme.com", reason="Personal request"))
    row = (await _rows(db))[0]
    assert row["contact_1_name"] == "Bob Stone"
    assert row["contact_2_name"] == ""
    assert "Jane" not in str(row)


async def test_any_flagged_address_drops_person(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(db, domain="acme.com", address="jane")
    # Her best address is fine; a lesser one is known to HubSpot as a contact.
    await add_address(db, company, jane, "j.doe@acme.com", status="pattern_confirmed")
    db.add(_known("j.doe@acme.com", kind="email"))
    row = (await _rows(db))[0]
    assert row["contact_1_name"] == ""
    assert "jane@acme.com" not in str(row)


async def test_contact_on_other_dnc_domain_dropped(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(db, domain="acme.com", address=None)
    await add_address(db, company, jane, "jane@acme-group.com")
    db.add(DoNotContact(kind="domain", value="acme-group.com", reason="Parent asked us to stop"))
    rows = await _rows(db)
    assert _domains(rows) == ["acme.com"]
    assert rows[0]["contact_1_name"] == ""


async def test_invalid_address_never_shown(db: AsyncSession) -> None:
    company, _, _ = await seed_company(db, domain="acme.com", address="jane", status="invalid")
    bob = await add_person(db, company, "Bob Stone", rank=2)
    await add_address(db, company, bob, "bob.stone@acme.com", status="invalid")
    await add_address(db, company, bob, "b.stone@acme.com", status="pattern_guess")
    row = (await _rows(db))[0]
    assert row["contact_1_name"] == "Jane Doe"
    assert row["contact_1_email"] == "" and row["contact_1_email_status"] == ""
    # Bob's invalid address would sort after his guess anyway; the guess is shown.
    assert row["contact_2_email"] == "b.stone@acme.com"
    assert "bob.stone@acme.com" not in str(row)


async def test_an_unparseable_linked_address_never_drops_a_person(db: AsyncSession) -> None:
    """Final wave T5: an address that doesn't normalize (``invalid_address``) isn't an opt-out:
    the person stays (shown with their good address); the bad one is never shown."""
    company, jane, _ = await seed_company(db, domain="acme.com", address="jane")
    await add_address(db, company, jane, "jane..doe@@acme.com", status="pattern_guess")
    bob = await add_person(db, company, "Bob Stone", rank=2)
    await add_address(db, company, bob, "not an address", status="pattern_confirmed")
    row = (await _rows(db))[0]
    assert row["contact_1_name"] == "Jane Doe"
    assert row["contact_1_email"] == "jane@acme.com"
    # Bob's only address is unparseable: listed by name, without an email.
    assert row["contact_2_name"] == "Bob Stone"
    assert row["contact_2_email"] == "" and row["contact_2_email_status"] == ""
    assert "jane..doe@@acme.com" not in str(row) and "not an address" not in str(row)


async def test_an_unparseable_address_never_hides_a_real_opt_out(db: AsyncSession) -> None:
    """Ruling P7-E still holds beside an unparseable address."""
    company, jane, _ = await seed_company(db, domain="acme.com", address="jane")
    await add_address(db, company, jane, "not an address", status="pattern_guess")
    db.add(Suppression(address="jane@acme.com", reason="unsubscribed", detail="link"))
    await db.commit()
    rows = await _rows(db)
    assert "Jane" not in str(rows)


async def test_dont_send_and_no_reply_never_shown(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(db, domain="acme.com", address="jane")
    jane_row = await db.scalar(select(EmailAddress).where(EmailAddress.address == "jane@acme.com"))
    assert jane_row is not None
    jane_row.override_sendable = False  # a teammate marked it "don't send"
    await add_address(db, company, jane, "j.doe@acme.com", status="pattern_confirmed")
    bob = await add_person(db, company, "Bob Stone", rank=2)
    await add_address(db, company, bob, "noreply@acme.com")
    row = (await _rows(db))[0]
    # Jane: the next usable address; Bob: listed without an email.
    assert row["contact_1_email"] == "j.doe@acme.com"
    assert row["contact_2_name"] == "Bob Stone"
    assert row["contact_2_email"] == ""
    assert "jane@acme.com" not in str(row) and "noreply" not in str(row)


async def test_export_refuses_open_transaction(db: AsyncSession) -> None:
    await seed_company(db, domain="acme.com")  # flushed, not committed: a transaction is open
    with pytest.raises(RuntimeError):
        await export.company_rows(db, side="all", config=CONFIG, catalog=CATALOG)
    await db.rollback()


async def test_export_reads_one_snapshot(db: AsyncSession) -> None:
    await seed_company(db, domain="acme.com")
    await db.commit()
    seen: list[str] = []

    async def spy() -> None:
        level = await db.scalar(text("SHOW transaction_isolation"))
        seen.append(str(level))

    rows = await _rows(db)
    await spy()
    assert rows and seen == ["repeatable read"]
    await db.commit()


async def test_two_targets_in_rank_order_with_preferred_address(db: AsyncSession) -> None:
    company, jane, _ = await seed_company(
        db, domain="acme.com", address="j.doe", status="pattern_confirmed"
    )
    await add_address(db, company, jane, "jane@acme.com", status="published")
    # Rank 2 added before rank 1's second address; a non-target is never listed.
    bob = await add_person(db, company, "Bob Stone", rank=2, title="Plant Manager")
    await add_address(db, company, bob, "bob@acme.com", status="pattern_confirmed")
    await add_person(db, company, "Not Target", rank=None, target=False)
    await db.commit()

    row = (await _rows(db))[0]
    assert row["contact_1_name"] == "Jane Doe"
    assert row["contact_1_title"] == "Purchasing Manager"
    assert row["contact_1_email"] == "jane@acme.com"
    assert row["contact_1_email_status"] == "published"
    assert row["contact_2_name"] == "Bob Stone"
    assert row["contact_2_title"] == "Plant Manager"
    assert row["contact_2_email"] == "bob@acme.com"
    assert row["contact_2_email_status"] == "pattern_confirmed"


async def test_target_without_address_listed_without_email(db: AsyncSession) -> None:
    await seed_company(db, domain="acme.com", address=None)
    await db.commit()
    row = (await _rows(db))[0]
    assert row["contact_1_name"] == "Jane Doe"
    assert row["contact_1_email"] == ""
    assert row["contact_1_email_status"] == ""


async def test_contacts_capped_at_max_targets(db: AsyncSession) -> None:
    company, _, _ = await seed_company(db, domain="acme.com")
    for rank, name in ((2, "Bob Stone"), (3, "Cara Lee")):
        person = await add_person(db, company, name, rank=rank)
        await add_address(db, company, person, f"{name.split()[0].lower()}@acme.com")
    await db.commit()
    two = (await _rows(db))[0]
    assert set(two) == set(export.COLUMNS)
    three = (await _rows(db, config=AppConfig(contacts=ContactsConfig(max_targets_per_company=3))))[
        0
    ]
    assert three["contact_3_name"] == "Cara Lee"


async def test_row_fields_order_and_side_filter(db: AsyncSession) -> None:
    await seed_company(db, name="Low", domain="b-low.com", score=10)
    await seed_company(db, name="High", domain="z-high.com", score=90)
    await seed_company(db, name="Tie", domain="a-tie.com", score=10)
    unscored, _, _ = await seed_company(db, name="None", domain="a-none.com")
    unscored.score = None
    await seed_company(db, name="Supp", domain="supp.com", side="supplier", score=99)
    await db.commit()

    rows = await _rows(db)
    assert _domains(rows) == ["z-high.com", "a-tie.com", "b-low.com", "a-none.com", "supp.com"]
    assert _domains(await _rows(db, "supplier")) == ["supp.com"]
    assert "supp.com" not in _domains(await _rows(db, "buyer"))
    first = rows[0]
    assert first["side"] == "buyer"
    assert first["company"] == "High"
    assert first["website"] == "https://z-high.com"
    assert first["score"] == "90"
    assert first["profile_status"] == "ok"
    assert first["country"] == "" and first["region"] == ""
    assert first["outreach_status"] == "" and first["last_sent_at"] == ""
    assert first["reply_intent"] == ""
    assert rows[3]["score"] == ""


async def test_fit_reasons_from_confirmed_signals(db: AsyncSession) -> None:
    company, _, _ = await seed_company(db, domain="acme.com")
    await set_state(db, "INDUSTRY_MATCH", "confirmed", actor=None)
    await set_state(db, "COMPANY_TYPE", "confirmed", actor=None)
    for code, value in (
        ("INDUSTRY_MATCH", ["personal_care"]),
        ("COMPANY_TYPE", "contract_manufacturer"),
        ("HIRING_PROCUREMENT", {"hiring": True}),  # not confirmed: left out
    ):
        db.add(Observation(company_id=company.id, code=code, value=value, is_latest=True))
    await db.commit()
    row = (await _rows(db))[0]
    assert row["fit_reasons"] == (
        "Works in our industries: Personal Care; "
        "What kind of company they are: contract manufacturer"
    )


async def test_outreach_fields(db: AsyncSession) -> None:
    campaign = await seed_campaign(db)
    message = await seed_draft(db, campaign, domain="sent.com")
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None and campaign.mailbox_id is not None
    sent_at = datetime(2026, 11, 9, 15, 30, tzinfo=UTC)
    message.status = "sent"
    message.sent_at = sent_at
    sequence.status = "replied"
    db.add(
        Conversation(
            sequence_id=sequence.id,
            mailbox_id=campaign.mailbox_id,
            status="needs_you",
            intent="interested",
        )
    )
    await db.commit()

    row = (await _rows(db))[0]
    assert row["domain"] == "sent.com"
    assert row["outreach_status"] == "replied"
    assert row["last_sent_at"] == "2026-11-09T15:30:00+00:00"
    assert row["reply_intent"] == "interested"


async def test_newest_sequence_wins(db: AsyncSession) -> None:
    old_campaign = await seed_campaign(db)
    message = await seed_draft(db, old_campaign, domain="acme.com")
    old = await db.get(Sequence, message.sequence_id)
    assert old is not None
    old.status = "completed"
    old.created_at = datetime.now(UTC) - timedelta(days=30)
    new_campaign = await seed_campaign(db)
    db.add(
        Sequence(
            campaign_id=new_campaign.id,
            company_id=old.company_id,
            recipient=old.recipient,
            status="awaiting_approval",
        )
    )
    await db.commit()
    row = (await _rows(db))[0]
    assert row["outreach_status"] == "awaiting_approval"


async def test_hostile_company_text_survives_round_trip(db: AsyncSession) -> None:
    await seed_company(db, name='=HYPERLINK("http://evil")', domain="evil.com")
    await db.commit()
    text = export.to_csv(await _rows(db))
    parsed = list(csv.reader(io.StringIO(text[1:])))
    assert parsed[1][1] == '\'=HYPERLINK("http://evil")'


async def test_export_batches(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Companies are read in batches; rows come out the same, in order."""
    monkeypatch.setattr(export, "BATCH", 2)
    for i in range(5):
        await seed_company(db, domain=f"c{i}.com", score=i, address=f"p{i}")
    await db.commit()
    rows = await _rows(db)
    assert _domains(rows) == [f"c{i}.com" for i in range(4, -1, -1)]
    assert [r["contact_1_email"] for r in rows] == [f"p{i}@c{i}.com" for i in range(4, -1, -1)]


# --------------------------------------------------------------------------- web


async def test_member_forbidden(
    client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    assert (await login(client, member.email, password)).status_code == 303
    token = await csrf_for(client, "/")
    resp = await client.post("/admin/export/companies", data={"side": "all", "csrf_token": token})
    assert resp.status_code == 403
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "companies.exported")) is None


async def test_export_requires_csrf(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    assert (await login(client, admin.email, password)).status_code == 303
    for data in ({"side": "all"}, {"side": "all", "csrf_token": "forged"}):
        resp = await client.post("/admin/export/companies", data=data)
        assert resp.status_code == 403
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "companies.exported")) is None


async def test_export_not_a_get(client: httpx.AsyncClient, admin: User, password: str) -> None:
    assert (await login(client, admin.email, password)).status_code == 303
    resp = await client.get("/admin/export/companies?side=all")
    assert resp.status_code == 405


async def test_export_signed_out_redirects(client: httpx.AsyncClient) -> None:
    resp = await client.post("/admin/export/companies", data={"side": "all"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert resp.content == b""


async def test_admin_downloads_csv_audited(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await seed_company(db, name="Acme Soap Works", domain="acme-soap.com")
    await seed_company(db, name="Supplier Co", domain="supplier.com", side="supplier")
    await db.commit()
    admin_id = admin.id
    assert (await login(client, admin.email, password)).status_code == 303
    token = await csrf_for(client, "/admin/settings")
    resp = await client.post("/admin/export/companies", data={"side": "buyer", "csrf_token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = resp.headers["content-disposition"]
    assert re.fullmatch(
        r'attachment; filename="salesfinder-companies-buyer-\d{8}\.csv"', disposition
    )
    assert datetime.now(UTC).strftime("%Y%m%d") in disposition
    assert resp.headers["cache-control"] == "no-store"
    body = resp.content.decode("utf-8")
    assert body.startswith("﻿")
    parsed = list(csv.reader(io.StringIO(body[1:])))
    assert tuple(parsed[0]) == export.COLUMNS
    assert [r[2] for r in parsed[1:]] == ["acme-soap.com"]

    db.expire_all()
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "companies.exported"))
    assert row is not None
    assert row.actor_id == admin_id
    assert row.details == {"side": "buyer", "rows": 1}
    flat = str(row.details) + str(row.target_id) + str(row.target_type)
    for leak in ("acme", "Acme", "jane", "supplier.com"):
        assert leak not in flat


async def test_export_all_sides(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession
) -> None:
    await seed_company(db, domain="acme-soap.com")
    await seed_company(db, domain="supplier.com", side="supplier")
    await db.commit()
    assert (await login(client, admin.email, password)).status_code == 303
    token = await csrf_for(client, "/admin/settings")
    resp = await client.post("/admin/export/companies", data={"side": "all", "csrf_token": token})
    assert resp.status_code == 200
    assert "salesfinder-companies-all-" in resp.headers["content-disposition"]
    assert resp.content.decode("utf-8").count("\r\n") == 3
    db.expire_all()
    row = await db.scalar(select(AuditLog).where(AuditLog.action == "companies.exported"))
    assert row is not None and row.details == {"side": "all", "rows": 2}


@pytest.mark.parametrize("side", ["bogus", "", "buyer\r\nX-Evil: 1", "../../etc"])
async def test_export_unknown_side_400(
    client: httpx.AsyncClient, admin: User, password: str, db: AsyncSession, side: str
) -> None:
    assert (await login(client, admin.email, password)).status_code == 303
    token = await csrf_for(client, "/admin/settings")
    resp = await client.post("/admin/export/companies", data={"side": side, "csrf_token": token})
    assert resp.status_code == 400
    assert "content-disposition" not in resp.headers
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "companies.exported")) is None
