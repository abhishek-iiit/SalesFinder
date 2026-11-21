"""The Outbox on the dashboard: cards (escaped body and gate problems), approve, re-check,
reject, edit (footer never editable) and bulk approve — each with CSRF, audit rows that never
hold the email text, and service refusals shown as 400."""

import html
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import dns.exception
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import CLEAN, CONFIG, FACTS, seed_campaign, seed_draft
from fakes.dns import FakeResolver
from salesfinder.campaigns import service as campaigns_service
from salesfinder.contacts.dns import MxChecker
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    Conversation,
    DomainMx,
    DoNotContact,
    Mailbox,
    MailboxMessage,
    Message,
    OutboxFeedback,
    Sequence,
    User,
)
from salesfinder.gate.checks import MX_OUT_OF_DATE
from salesfinder.outbox import service as outbox_service
from salesfinder.outbox.service import (
    ARCHIVED,
    NO_PERSONA,
    NOT_APPROVED,
    NOT_UNCONFIRMED,
    NOT_WAITING,
    REASON_BAD,
    STALE_EDIT,
)
from salesfinder.web.deps import CONCURRENT_CHANGE, is_concurrent_conflict
from salesfinder.web.routes.outbox import BULK_LIMIT, UNCONFIRMED_COPY, written_part
from salesfinder.writer.compose import EMPTY_BODY, assemble, footer, signature
from web_helpers import audit_actions, csrf_for, csrf_from, login, text_of


async def _fresh(db: AsyncSession, message_id: uuid.UUID) -> Message:
    message = await db.get(Message, message_id, populate_existing=True)
    assert message is not None
    return message


async def _logs(db: AsyncSession, prefix: str = "message.") -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.action.like(f"{prefix}%"))
            .order_by(AuditLog.at)
            .execution_options(populate_existing=True)
        )
    )


def _version(page: str) -> str:
    match = re.search(r'name="version" value="([^"]+)"', page)
    assert match, "no version field"
    return html.unescape(match.group(1))


async def _dnc(db: AsyncSession, user: User, address: str) -> None:
    db.add(DoNotContact(kind="email", value=address, reason="asked", added_by_id=user.id))
    await db.commit()


async def test_list_escapes_body_and_gate_problems(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member, name="Soap")
    written = {**CLEAN, "first_body": "Hi there,\n\n<script>alert(1)</script> & more\n\nThanks,"}
    ready = await seed_draft(db, campaign, domain="a-soap.com", written=written)
    held = await seed_draft(
        db, campaign, domain="b-soap.com", status="gate_failed", gate_errors=["<b>Bad</b> link"]
    )
    await login(client, member.email, password)

    resp = await client.get("/outbox")
    assert resp.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in resp.text
    assert "<script>alert" not in resp.text and "<b>Bad</b>" not in resp.text
    assert "&lt;b&gt;Bad&lt;/b&gt; link" in resp.text
    page = text_of(resp)
    assert "Ready" in page and "Held by the gate" in page
    assert "jane.doe@a-soap.com" in page and "Jane Doe" in page and "Purchasing Manager" in page
    assert "Soap" in page and "Outbox (2)" in page
    # Approve and the bulk checkbox only on the ready one; Re-check only on the held one.
    assert f'action="/outbox/{ready.id}/approve"' in page
    assert f'action="/outbox/{held.id}/approve"' not in page
    assert f'action="/outbox/{held.id}/recheck"' in page
    assert f'action="/outbox/{ready.id}/recheck"' not in page
    assert f'name="ids" value="{ready.id}"' in page and f'name="ids" value="{held.id}"' not in page
    assert f'href="/outbox/{ready.id}"' in page and f'href="/outbox/{held.id}"' in page

    # The campaign filter.
    other = await seed_campaign(db, user=member, name="Other")
    await seed_draft(db, other, domain="c-soap.com")
    only_other = text_of(await client.get(f"/outbox?campaign={other.id}"))
    assert "jane.doe@c-soap.com" in only_other and "jane.doe@a-soap.com" not in only_other
    assert (await client.get("/outbox?campaign=nope")).status_code == 400
    assert (await client.get("/outbox?page=0")).status_code == 400


async def test_approve_and_csrf(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    member_id = member.id
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    assert (await client.post(f"/outbox/{message.id}/approve")).status_code == 403
    assert (await _fresh(db, message.id)).status == "awaiting_approval"

    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/outbox?")
    assert "msg=approved" in resp.headers["location"]
    assert "Approved." in text_of(await client.get(resp.headers["location"]))
    approved = await _fresh(db, message.id)
    assert (approved.status, approved.approved_by_id) == ("approved", member_id)

    again = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert again.status_code == 400 and NOT_WAITING in text_of(again)
    missing = await client.post(f"/outbox/{uuid.uuid4()}/approve", data={"csrf_token": token})
    assert missing.status_code == 404

    [log] = await _logs(db)
    assert (log.action, log.actor_id, log.target_type, log.target_id) == (
        "message.approved",
        member_id,
        "message",
        str(message.id),
    )
    assert log.details == {"campaign_id": str(campaign.id), "recipient": "jane.doe@acme-soap.com"}


async def test_approve_after_dnc_shows_reason_and_stays_held(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await _dnc(db, member, "jane.doe@acme-soap.com")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303 and "msg=held" in resp.headers["location"]
    page = text_of(await client.get(resp.headers["location"]))
    assert "Not approved: the gate held this email." in page
    assert "Recipient is excluded" in page
    held = await _fresh(db, message.id)
    assert held.status == "gate_failed" and held.approved_by_id is None
    assert any(p.startswith("Recipient is excluded") for p in held.gate_errors)
    [log] = await _logs(db)
    assert log.action == "message.held"
    assert log.details["problems"] == len(held.gate_errors)


async def test_recheck_moves_a_cleared_hold_back_to_ready(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, status="gate_failed", gate_errors=["Stale reason"])
    ready = await seed_draft(db, campaign, domain="z-soap.com")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    assert (await client.post(f"/outbox/{message.id}/recheck")).status_code == 403
    resp = await client.post(f"/outbox/{message.id}/recheck", data={"csrf_token": token})
    assert resp.status_code == 303 and "msg=ready" in resp.headers["location"]
    assert "passed the gate" in text_of(await client.get(resp.headers["location"]))
    fresh = await _fresh(db, message.id)
    assert (fresh.status, fresh.gate_errors) == ("awaiting_approval", [])

    # Only a held email can be re-checked.
    refused = await client.post(f"/outbox/{ready.id}/recheck", data={"csrf_token": token})
    assert refused.status_code == 400 and NOT_WAITING in text_of(refused)
    missing = await client.post(f"/outbox/{uuid.uuid4()}/recheck", data={"csrf_token": token})
    assert missing.status_code == 404

    [log] = await _logs(db)
    assert (log.action, log.target_id) == ("message.rechecked", str(message.id))
    assert log.details["result"] == "ready" and log.details["problems"] == 0


async def test_reject_needs_a_reason_and_keeps_it_out_of_the_audit(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    url = f"/outbox/{message.id}/reject"

    assert (await client.post(url, data={"reason": "Too pushy"})).status_code == 403
    blank = await client.post(url, data={"csrf_token": token, "reason": "   "})
    assert blank.status_code == 400 and REASON_BAD in text_of(blank)
    assert (await _fresh(db, message.id)).status == "awaiting_approval"

    resp = await client.post(url, data={"csrf_token": token, "reason": "Too pushy <b>tone</b>"})
    assert resp.status_code == 303 and "msg=rejected" in resp.headers["location"]
    assert "Rejected." in text_of(await client.get(resp.headers["location"]))
    assert (await _fresh(db, message.id)).status == "rejected"
    feedback = await db.scalar(select(OutboxFeedback))
    assert feedback is not None and feedback.reason == "Too pushy <b>tone</b>"
    [log] = await _logs(db)
    assert log.action == "message.rejected"
    assert log.details == {
        "campaign_id": str(campaign.id),
        "recipient": "jane.doe@acme-soap.com",
        "reason_length": len("Too pushy <b>tone</b>"),
    }
    assert "pushy" not in str(log.details)


async def test_edit_page_never_offers_the_footer_and_saving_regates(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    url = f"/outbox/{message.id}"

    page = await client.get(url)
    assert page.status_code == 200
    textarea = re.search(r"<textarea[^>]*name=\"body\"[^>]*>(.*?)</textarea>", page.text, re.S)
    assert textarea, page.text
    editable = textarea.group(1)
    assert "Do you buy surfactants" in editable
    assert FACTS.postal_address not in editable and CONFIG.writing.opt_out_line not in editable
    assert "Asha Rao" not in editable
    # Shown read-only as "Added automatically", and in no other form field.
    text = text_of(page)
    assert "Added automatically" in text and FACTS.postal_address in text
    assert len(re.findall(r"<textarea", page.text)) == 1
    assert 'value="1 Main Street' not in page.text
    token = await csrf_for(client, url)
    form = {"csrf_token": token, "version": _version(page.text)}

    no_csrf = await client.post(url, data={"subject": "x", "body": "Hi"})
    assert no_csrf.status_code == 403
    empty = await client.post(url, data={**form, "subject": "S", "body": "  "})
    assert empty.status_code == 400 and EMPTY_BODY in text_of(empty)
    long = await client.post(url, data={**form, "subject": "x" * 201, "body": "Hi"})
    assert long.status_code == 400 and "Subject is longer than 200 characters." in text_of(long)
    assert "x" * 201 in long.text  # the teammate's text is kept in the form
    assert (await _fresh(db, message.id)).edited is False

    body = "Hi Jane,\n\nWould a sample of our surfactants help your body wash line?\n\nThanks,"
    resp = await client.post(url, data={**form, "subject": "Surfactant samples", "body": body})
    assert resp.status_code == 303 and "msg=saved" in resp.headers["location"]
    assert "Saved." in text_of(await client.get(resp.headers["location"]))
    saved = await _fresh(db, message.id)
    tail = signature(FACTS, "buyer") + footer(FACTS, CONFIG.writing)
    assert saved.body == body + tail
    assert (saved.subject, saved.edited, saved.status) == (
        "Surfactant samples",
        True,
        "awaiting_approval",
    )

    # A price is held by the gate: back on the edit page with the problems shown.
    priced = "Hi Jane,\n\nOur surfactants are $1.20 per kg.\n\nThanks,"
    form["version"] = _version((await client.get(url)).text)
    held = await client.post(url, data={**form, "subject": "Price", "body": priced})
    assert held.status_code == 303 and held.headers["location"] == f"{url}?msg=saved_held"
    held_page = text_of(await client.get(held.headers["location"]))
    assert "the gate holds this email" in held_page
    assert (await _fresh(db, message.id)).status == "gate_failed"
    assert (await _fresh(db, message.id)).gate_errors

    logs = await _logs(db)
    assert [log.action for log in logs] == ["message.edited", "message.edited"]
    assert logs[0].details["result"] == "ready" and logs[1].details["result"] == "held"
    assert all("surfactant" not in str(log.details).lower() for log in logs)

    assert (await client.get(f"/outbox/{uuid.uuid4()}")).status_code == 404
    gone = await client.post(f"/outbox/{uuid.uuid4()}", data={**form, "subject": "S", "body": "Hi"})
    assert gone.status_code == 404


async def test_edit_refuses_a_stale_form(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    """Two teammates open the same email; the second save is refused and changes nothing."""
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    url = f"/outbox/{message.id}"
    page = await client.get(url)
    form = {"csrf_token": await csrf_for(client, url), "version": _version(page.text)}

    first = await client.post(
        url, data={**form, "subject": "First", "body": "Hi,\n\nOne.\n\nThanks,"}
    )
    assert first.status_code == 303
    after_first = await _fresh(db, message.id)
    kept = (after_first.subject, after_first.body, after_first.updated_at)

    second = await client.post(url, data={**form, "subject": "Second", "body": "Hi,\n\nTwo."})
    assert second.status_code == 400
    assert STALE_EDIT in text_of(second)
    assert "Two." in text_of(second)  # the teammate's text is kept to copy
    assert _version(second.text) == form["version"]  # still stale: resubmitting is refused too
    again = await client.post(url, data={**form, "subject": "Second", "body": "Hi,\n\nTwo."})
    assert again.status_code == 400
    for bad in ("", "not-a-date", "2026-11-13T10:00:00"):  # missing, garbage, naive
        resp = await client.post(url, data={**form, "version": bad, "subject": "S", "body": "Hi"})
        assert resp.status_code == 400 and STALE_EDIT in text_of(resp)
    fresh = await _fresh(db, message.id)
    assert (fresh.subject, fresh.body, fresh.updated_at) == kept
    assert [log.action for log in await _logs(db)] == ["message.edited"]


async def test_edit_keeps_the_list_filter_and_page(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    listing = text_of(await client.get(f"/outbox?campaign={campaign.id}"))
    assert f'href="/outbox/{message.id}?campaign={campaign.id}"' in listing

    url = f"/outbox/{message.id}?campaign={campaign.id}&page=2"
    page = await client.get(url)
    assert f'name="campaign" value="{campaign.id}"' in page.text
    assert 'name="page" value="2"' in page.text
    assert f'href="/outbox?campaign={campaign.id}&amp;page=2"' in page.text  # back / cancel
    # Junk in the URL is dropped, never echoed.
    junk = await client.get(f"/outbox/{message.id}?campaign=<x>&page=abc")
    assert "<x>" not in junk.text and 'name="page"' not in junk.text

    form = {
        "csrf_token": await csrf_for(client, url),
        "version": _version(page.text),
        "campaign": str(campaign.id),
        "page": "2",
    }
    body = "Hi Jane,\n\nWould a sample of our surfactants help?\n\nThanks,"
    saved = await client.post(
        f"/outbox/{message.id}", data={**form, "subject": "Samples", "body": body}
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/outbox?campaign={campaign.id}&page=2&msg=saved"

    form["version"] = _version((await client.get(url)).text)
    priced = "Hi Jane,\n\nOur surfactants are $1.20 per kg.\n\nThanks,"
    held = await client.post(f"/outbox/{message.id}", data={**form, "subject": "P", "body": priced})
    assert held.headers["location"] == (
        f"/outbox/{message.id}?campaign={campaign.id}&page=2&msg=saved_held"
    )


async def test_edit_page_of_a_finished_email_has_no_form(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, status="approved")
    await login(confirmed_client, member.email, password)
    page = text_of(await confirmed_client.get(f"/outbox/{message.id}"))
    assert "can't be edited any more" in page and "<textarea" not in page


async def test_bulk_approve(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    one = await seed_draft(db, campaign, domain="a-soap.com")
    two = await seed_draft(db, campaign, domain="b-soap.com")
    blocked = await seed_draft(db, campaign, domain="c-soap.com")
    done = await seed_draft(db, campaign, domain="d-soap.com", status="approved")
    await _dnc(db, member, "jane.doe@c-soap.com")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    ids = [str(m.id) for m in (one, two, blocked, done)]

    assert (await client.post("/outbox/bulk-approve", data={"ids": ids})).status_code == 403
    resp = await client.post(
        "/outbox/bulk-approve", data={"csrf_token": token, "ids": ids, "campaign": ""}
    )
    assert resp.status_code == 303, resp.text
    page = text_of(await client.get(resp.headers["location"]))
    assert (
        "Approved 2; 1 held — see below. 1 skipped (no longer waiting, or couldn't be checked)."
        in page
    )
    assert "Recipient is excluded" in page
    statuses = [(await _fresh(db, m.id)).status for m in (one, two, blocked, done)]
    assert statuses == ["approved", "approved", "gate_failed", "approved"]

    logs = await _logs(db)
    by_target = {log.target_id: log.action for log in logs if log.target_id}
    assert by_target == {
        str(one.id): "message.approved",
        str(two.id): "message.approved",
        str(blocked.id): "message.held",
    }
    summary = [log for log in logs if log.action == "message.bulk_approved"]
    assert [s.details for s in summary] == [{"approved": 2, "held": 1, "skipped": 1}]

    nothing = await client.post("/outbox/bulk-approve", data={"csrf_token": token})
    assert nothing.status_code == 303
    assert "Select at least one email" in text_of(await client.get(nothing.headers["location"]))
    too_many = [str(uuid.uuid4()) for _ in range(BULK_LIMIT + 1)]
    over = await client.post("/outbox/bulk-approve", data={"csrf_token": token, "ids": too_many})
    assert over.status_code == 400 and f"at most {BULK_LIMIT}" in text_of(over)
    bad = await client.post("/outbox/bulk-approve", data={"csrf_token": token, "ids": ["x"]})
    assert bad.status_code == 400


async def test_flash_ignores_unknown_keys_and_counts(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(confirmed_client, member.email, password)
    resp = await confirmed_client.get("/outbox?msg=<script>x</script>&ok=<b>&held=9999999")
    assert (
        resp.status_code == 200 and "<script>x" not in resp.text and "&lt;script" not in resp.text
    )
    weird = await confirmed_client.get("/outbox?msg=bulk&ok=abc&held=-1&skipped=99999")
    assert weird.status_code == 200 and "alert-success" not in weird.text
    assert "Approved 0" not in text_of(weird)
    assert "Nothing waiting for approval." in text_of(weird)


async def test_nav_count_follows_the_outbox(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    await login(confirmed_client, member.email, password)
    page = text_of(await confirmed_client.get("/campaigns"))
    assert ">Outbox</a>" in page and "Outbox (" not in page
    campaign = await seed_campaign(db, user=member)
    await seed_draft(db, campaign, domain="a-soap.com")
    message = await seed_draft(db, campaign, domain="b-soap.com", status="gate_failed")
    assert "Outbox (2)" in text_of(await confirmed_client.get("/campaigns"))
    # A first email whose sequence was stopped elsewhere isn't counted (AZ filter).
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    sequence.status = "stopped"
    await db.commit()
    assert "Outbox (1)" in text_of(await confirmed_client.get("/campaigns"))
    actions = await audit_actions(db)
    assert not [a for a in actions if a.startswith("message.")]


def test_written_part_strips_the_tail_even_after_facts_changed() -> None:
    _, body = assemble(
        "S", "Hi Jane,\n\n— a dash line\n\nThanks,", facts=FACTS, writing=CONFIG.writing,
        side="buyer",
    )  # fmt: skip
    assert written_part(body, facts=FACTS, config=CONFIG, side="buyer") == (
        "Hi Jane,\n\n— a dash line\n\nThanks,"
    )
    moved = FACTS.model_copy(update={"postal_address": "9 Other Road, Edison, NJ 08817"})
    stripped = written_part(body, facts=moved, config=CONFIG, side="buyer")
    assert stripped == "Hi Jane,\n\n— a dash line\n\nThanks,"
    assert FACTS.postal_address not in stripped


async def test_missing_persona_is_a_400_not_a_500(
    confirmed_app: FastAPI,
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    """facts.yaml without a buyer persona: approve, re-check, edit and bulk refuse with the
    reason and change nothing (the edit page still opens)."""
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    ready = await seed_draft(db, campaign, domain="a-soap.com")
    held = await seed_draft(db, campaign, domain="b-soap.com", status="gate_failed")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    page = await client.get(f"/outbox/{ready.id}")
    version = _version(page.text)
    confirmed_app.state.facts = FACTS.model_copy(
        update={"personas": {"supplier": FACTS.personas["supplier"]}}
    )

    assert (await client.get(f"/outbox/{ready.id}")).status_code == 200
    calls = [
        (f"/outbox/{ready.id}/approve", {}),
        (f"/outbox/{held.id}/recheck", {}),
        (f"/outbox/{ready.id}", {"version": version, "subject": "S", "body": "Hi,\n\nNew."}),
    ]
    for url, data in calls:
        resp = await client.post(url, data={"csrf_token": token, **data})
        assert resp.status_code == 400, url
        assert NO_PERSONA in text_of(resp)
    bulk = await client.post(
        "/outbox/bulk-approve", data={"csrf_token": token, "ids": [str(ready.id)]}
    )
    assert bulk.status_code == 303
    assert "Approved 0. 1 skipped" in text_of(await client.get(bulk.headers["location"]))

    first, second = await _fresh(db, ready.id), await _fresh(db, held.id)
    assert (first.status, first.edited, second.status) == (
        "awaiting_approval",
        False,
        "gate_failed",
    )
    assert [log.action for log in await _logs(db)] == ["message.bulk_approved"]


async def test_recheck_when_the_hold_is_still_there(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, status="gate_failed", gate_errors=["Old reason"])
    await _dnc(db, member, "jane.doe@acme-soap.com")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    resp = await client.post(f"/outbox/{message.id}/recheck", data={"csrf_token": token})
    assert resp.status_code == 303 and "msg=still_held" in resp.headers["location"]
    page = text_of(await client.get(resp.headers["location"]))
    assert "Re-checked: the gate still holds this email." in page
    assert "Recipient is excluded" in page and "Old reason" not in page
    fresh = await _fresh(db, message.id)
    assert fresh.status == "gate_failed"
    assert any(p.startswith("Recipient is excluded") for p in fresh.gate_errors)
    [log] = await _logs(db)
    assert log.action == "message.rechecked" and log.details["result"] == "held"
    assert log.details["problems"] == len(fresh.gate_errors)


async def test_bulk_approve_with_duplicate_ids(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    one = await seed_draft(db, campaign, domain="a-soap.com")
    two = await seed_draft(db, campaign, domain="b-soap.com")
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    ids = [str(one.id), str(two.id), str(one.id), str(one.id)]

    resp = await client.post("/outbox/bulk-approve", data={"csrf_token": token, "ids": ids})
    assert resp.status_code == 303
    page = text_of(await client.get(resp.headers["location"]))
    assert "Approved 2." in page and "skipped" not in page
    assert [(await _fresh(db, m.id)).status for m in (one, two)] == ["approved", "approved"]
    logs = await _logs(db)
    assert sorted(log.target_id for log in logs if log.action == "message.approved") == sorted(
        [str(one.id), str(two.id)]
    )
    summary = [log.details for log in logs if log.action == "message.bulk_approved"]
    assert summary == [{"approved": 2, "held": 0, "skipped": 0}]


async def test_signed_out_pages_redirect_to_login(client: httpx.AsyncClient) -> None:
    for path in ("/outbox", "/campaigns", "/campaigns/new", f"/outbox/{uuid.uuid4()}"):
        resp = await client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"].startswith("/login?next="), path


# --------------------------------------------------------------------------- paused / archived


async def test_paused_campaign_shows_a_badge_and_can_still_be_approved(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    paused = await seed_campaign(db, user=member, name="Paused one")
    message = await seed_draft(db, paused, domain="a-soap.com")
    running = await seed_campaign(db, user=member, name="Running one")
    await seed_draft(db, running, domain="b-soap.com")
    await campaigns_service.pause(db, paused.id, actor=member)
    await db.commit()
    await login(client, member.email, password)

    page = (await client.get("/outbox")).text
    assert page.count("Campaign paused</span>") == 1
    card = page[page.index(f'id="m-{message.id}"') :]
    assert "Campaign paused" in card[: card.index("</article>")]

    token = await csrf_for(client, "/outbox")
    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303 and "msg=approved" in resp.headers["location"]
    assert (await _fresh(db, message.id)).status == "approved"


async def test_archived_campaign_approve_is_refused(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    campaign.status = "archived"  # without the archive cleanup: the Outbox refuses anyway
    await db.commit()

    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert resp.status_code == 400 and ARCHIVED in text_of(resp)
    assert (await _fresh(db, message.id)).status == "awaiting_approval"
    bulk = await client.post(
        "/outbox/bulk-approve", data={"csrf_token": token, "ids": [str(message.id)]}
    )
    assert bulk.status_code == 303 and "skipped=1" in bulk.headers["location"]
    assert (await _fresh(db, message.id)).status == "awaiting_approval"


# --------------------------------------------------------------------------- stale MX


def _stale() -> datetime:
    return datetime.now(UTC) - timedelta(days=CONFIG.contacts.mx_cache_days, hours=1)


def _fake_dns(app: FastAPI, answers: dict[tuple[str, str], Any]) -> FakeResolver:
    resolver = FakeResolver(answers)
    app.state.mx_checker = MxChecker(CONFIG.contacts, resolve=resolver)
    return resolver


async def test_stale_mx_at_approve_is_refreshed_and_the_draft_approves(
    confirmed_app: FastAPI,
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, checked_at=_stale())
    resolver = _fake_dns(confirmed_app, {("acme-soap.com", "MX"): [(10, "mx.acme-soap.com.")]})
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})

    assert resp.status_code == 303 and "msg=approved" in resp.headers["location"]
    assert (await _fresh(db, message.id)).status == "approved"
    assert resolver.calls == [("acme-soap.com", "MX")]
    mx = await db.get(DomainMx, "acme-soap.com", populate_existing=True)
    assert mx is not None and datetime.now(UTC) - mx.checked_at < timedelta(minutes=1)


async def test_stale_mx_dns_failure_stays_held(
    confirmed_app: FastAPI,
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, checked_at=_stale())
    resolver = _fake_dns(confirmed_app, {("acme-soap.com", "MX"): dns.exception.Timeout()})
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")

    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})

    assert resp.status_code == 303 and "msg=held" in resp.headers["location"]
    held = await _fresh(db, message.id)
    assert (held.status, held.gate_errors) == ("gate_failed", [MX_OUT_OF_DATE])
    assert resolver.calls == [("acme-soap.com", "MX")]
    page = text_of(await client.get(resp.headers["location"]))
    assert "Couldn't confirm the recipient's mail server just now — try Re-check later." in page

    # DNS answers again: Re-check refreshes it and the email is ready.
    resolver.answers[("acme-soap.com", "MX")] = [(10, "mx.acme-soap.com.")]
    again = await client.post(f"/outbox/{message.id}/recheck", data={"csrf_token": token})
    assert again.status_code == 303 and "msg=ready" in again.headers["location"]
    ready = await _fresh(db, message.id)
    assert (ready.status, ready.gate_errors) == ("awaiting_approval", [])


async def test_bulk_approve_refreshes_each_stale_domain_once(
    confirmed_app: FastAPI,
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    one = await seed_draft(db, campaign, domain="a-soap.com", checked_at=_stale())
    two = await seed_draft(db, campaign, domain="b-soap.com", checked_at=_stale())
    fresh = await seed_draft(db, campaign, domain="c-soap.com")
    resolver = _fake_dns(
        confirmed_app,
        {
            ("a-soap.com", "MX"): [(10, "mx.a-soap.com.")],
            ("b-soap.com", "MX"): [(10, "mx.b-soap.com.")],
        },
    )
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    ids = [str(m.id) for m in (one, two, fresh, one)]

    resp = await client.post("/outbox/bulk-approve", data={"csrf_token": token, "ids": ids})

    assert resp.status_code == 303 and "ok=3" in resp.headers["location"]
    assert sorted(resolver.calls) == [("a-soap.com", "MX"), ("b-soap.com", "MX")]
    assert [(await _fresh(db, m.id)).status for m in (one, two, fresh)] == ["approved"] * 3


async def test_edit_refreshes_a_stale_mx_before_the_gate(
    confirmed_app: FastAPI,
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign, checked_at=_stale())
    resolver = _fake_dns(confirmed_app, {("acme-soap.com", "MX"): [(10, "mx.acme-soap.com.")]})
    await login(client, member.email, password)
    page = await client.get(f"/outbox/{message.id}")
    form = {
        "csrf_token": csrf_from(page.text),
        "version": _version(page.text),
        "subject": "Surfactants for your line",
        "body": "Hi there,\n\nWould a sample of our surfactants help?\n\nThanks,",
    }
    resp = await client.post(f"/outbox/{message.id}", data=form)
    assert resp.status_code == 303 and "msg=saved" in resp.headers["location"]
    saved = await _fresh(db, message.id)
    assert (saved.status, saved.gate_errors) == ("awaiting_approval", [])
    assert resolver.calls == [("acme-soap.com", "MX")]


# --------------------------------------------------------------------------- deadlock retry


class _FakeOrig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("conflict")
        self.sqlstate = sqlstate


def _conflict(sqlstate: str = "40P01") -> DBAPIError:
    return DBAPIError("UPDATE messages", None, _FakeOrig(sqlstate))


def test_concurrent_conflict_detection() -> None:
    assert is_concurrent_conflict(_conflict("40P01"))
    assert is_concurrent_conflict(_conflict("40001"))
    psycopg_style = DBAPIError("x", None, Exception())
    psycopg_style.orig.pgcode = "40P01"  # type: ignore[union-attr]
    assert is_concurrent_conflict(psycopg_style)
    assert not is_concurrent_conflict(_conflict("23505"))  # a unique violation isn't retried
    assert not is_concurrent_conflict(RuntimeError("40P01"))


async def test_approve_retries_once_after_a_deadlock(
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    real, calls = outbox_service.approve, []

    async def flaky(*args: Any, **kwargs: Any) -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise _conflict()
        return await real(*args, **kwargs)

    monkeypatch.setattr(outbox_service, "approve", flaky)
    resp = await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert resp.status_code == 303 and "msg=approved" in resp.headers["location"]
    assert len(calls) == 2
    assert (await _fresh(db, message.id)).status == "approved"
    assert [log.action for log in await _logs(db)] == ["message.approved"]


async def test_a_second_deadlock_is_a_409_not_a_500(
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    calls: list[str] = []

    async def always(*_args: Any, **_kwargs: Any) -> None:
        calls.append("x")
        raise _conflict("40001")

    version = _version((await client.get(f"/outbox/{message.id}")).text)
    for name in ("approve", "recheck", "reject", "edit", "bulk_approve"):
        monkeypatch.setattr(outbox_service, name, always)
    posts = [
        (f"/outbox/{message.id}/approve", {}),
        (f"/outbox/{message.id}/recheck", {}),
        (f"/outbox/{message.id}/reject", {"reason": "No"}),
        (f"/outbox/{message.id}", {"subject": "S", "body": "B", "version": version}),
        ("/outbox/bulk-approve", {"ids": [str(message.id)]}),
    ]
    for url, data in posts:
        calls.clear()
        resp = await client.post(url, data={"csrf_token": token, **data})
        assert resp.status_code == 409, (url, resp.status_code)
        assert CONCURRENT_CHANGE in text_of(resp)
        assert "Try again" in text_of(resp)
        assert len(calls) == 2, url  # the first try and one retry
    assert (await _fresh(db, message.id)).status == "awaiting_approval"
    assert await _logs(db) == []


async def test_other_database_errors_are_not_retried(
    confirmed_client: httpx.AsyncClient,
    member: User,
    password: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = confirmed_client
    campaign = await seed_campaign(db, user=member)
    message = await seed_draft(db, campaign)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox")
    calls: list[str] = []

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        calls.append("x")
        raise _conflict("23505")

    monkeypatch.setattr(outbox_service, "approve", broken)
    with pytest.raises(DBAPIError):
        await client.post(f"/outbox/{message.id}/approve", data={"csrf_token": token})
    assert calls == ["x"]


# --------------------------------------------------------------------------- tabs (Task 10)


@dataclass(frozen=True)
class Kinds:
    first: uuid.UUID
    followup: uuid.UUID
    reply: uuid.UUID
    approved: uuid.UUID
    unconfirmed: uuid.UUID
    failed: uuid.UUID
    conversation: uuid.UUID


async def _sent(db: AsyncSession, message: Message, *, at: datetime) -> Sequence:
    """Step 1 went out at ``at``; returns its sequence."""
    message.status = "sent"
    message.sent_at = message.claimed_at = at
    message.message_id_header = f"<{uuid.uuid4().hex[:10]}@send.example.org>"
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    return sequence


async def _step2(db: AsyncSession, sequence_id: uuid.UUID) -> Message:
    step2 = await db.scalar(
        select(Message).where(Message.sequence_id == sequence_id, Message.step == 2)
    )
    assert step2 is not None
    return step2


async def _seed_kinds(db: AsyncSession, user: User) -> Kinds:
    """One email of each kind and Outbox tab in one campaign. Commits."""
    campaign = await seed_campaign(db, user=user, name="Kinds")
    row = await db.get(Campaign, campaign.id)
    assert row is not None and row.mailbox_id is not None
    sent_at = datetime(2026, 11, 2, 15, tzinfo=UTC)

    first = await seed_draft(db, campaign, domain="a-soap.com")

    f1 = await seed_draft(db, campaign, domain="b-soap.com")
    seq = await _sent(db, f1, at=sent_at)
    seq.status = "active"
    followup = await _step2(db, seq.id)
    followup.status = "awaiting_approval"
    followup.due_at = sent_at + timedelta(days=6)

    r1 = await seed_draft(db, campaign, domain="c-soap.com")
    seq = await _sent(db, r1, at=sent_at)
    seq.status = "replied"
    (await _step2(db, seq.id)).status = "cancelled"
    conv = Conversation(sequence_id=seq.id, mailbox_id=row.mailbox_id, status="open")
    db.add(conv)
    await db.flush()
    theirs = MailboxMessage(
        mailbox_id=row.mailbox_id,
        folder="inbox",
        uidvalidity=1,
        uid=7,
        from_address=seq.recipient,
        subject="Re: hi",
        received_at=sent_at + timedelta(days=1),
        kind="reply",
        sequence_id=seq.id,
        conversation_id=conv.id,
        body_text="<script>alert(2)</script> please quote",
    )
    db.add(theirs)
    await db.flush()
    reply = Message(
        sequence_id=seq.id,
        step=3,
        kind="reply",
        subject="Re: hi",
        body=r1.body,
        status="awaiting_approval",
        gate_errors=[],
        reply_to_id=theirs.id,
        mailbox_id=row.mailbox_id,
    )
    db.add(reply)

    a1 = await seed_draft(db, campaign, domain="d-soap.com")
    a1.status, a1.approved_by_id, a1.approved_at = "approved", user.id, sent_at
    (await db.get(Sequence, a1.sequence_id)).status = "active"  # type: ignore[union-attr]

    u1 = await seed_draft(db, campaign, domain="e-soap.com")
    u1.status, u1.claimed_at = "unconfirmed", sent_at
    u1.message_id_header = "<unconfirmed@send.example.org>"
    u1.mailbox_id = row.mailbox_id
    (await db.get(Sequence, u1.sequence_id)).status = "active"  # type: ignore[union-attr]

    x1 = await seed_draft(db, campaign, domain="f-soap.com")
    x1.status, x1.claimed_at = "failed", sent_at
    x1.last_send_error = "<b>550</b> mailbox unavailable"
    (await db.get(Sequence, x1.sequence_id)).status = "active"  # type: ignore[union-attr]

    mailbox = await db.get(Mailbox, row.mailbox_id)
    assert mailbox is not None
    mailbox.enabled = True
    mailbox.hold_reason = "Warming up: cold email starts on 2026-11-20."
    await db.commit()
    return Kinds(first.id, followup.id, reply.id, a1.id, u1.id, x1.id, conv.id)


async def test_tabs_list_the_right_kinds(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    kinds = await _seed_kinds(db, member)
    await login(client, member.email, password)

    review = await client.get("/outbox")
    assert review.status_code == 200
    page = text_of(review)
    assert "To review (3)" in page and "Approved — waiting to send (1)" in page
    assert "Problems (2)" in page
    for message_id in (kinds.first, kinds.followup, kinds.reply):
        assert f'id="m-{message_id}"' in page
    for message_id in (kinds.approved, kinds.unconfirmed, kinds.failed):
        assert f'id="m-{message_id}"' not in page
    assert "First email" in page and "Follow-up" in page and "Reply" in page
    assert "First email sent 2026-11-02 15:00 UTC" in page
    assert "&lt;script&gt;alert(2)&lt;/script&gt; please quote" in review.text
    assert "<script>alert(2)" not in review.text
    assert f'href="/conversations/{kinds.conversation}"' in page
    # Bulk approve accepts every kind: each ready card has its checkbox.
    for message_id in (kinds.first, kinds.followup, kinds.reply):
        assert f'name="ids" value="{message_id}"' in page

    only = text_of(await client.get("/outbox?kind=followup"))
    assert f'id="m-{kinds.followup}"' in only and f'id="m-{kinds.first}"' not in only
    assert (await client.get("/outbox?kind=nope")).status_code == 400
    assert (await client.get("/outbox?view=nope")).status_code == 400

    approved = text_of(await client.get("/outbox?view=approved"))
    assert f'id="m-{kinds.approved}"' in approved and f'id="m-{kinds.first}"' not in approved
    assert "Warming up: cold email starts on 2026-11-20." in approved
    assert f'action="/outbox/{kinds.approved}/withdraw"' in approved
    assert "Sending is paused: Sending hasn't been turned on yet." in approved

    problems = await client.get("/outbox?view=problems")
    page = text_of(problems)
    assert f'id="m-{kinds.unconfirmed}"' in page and f'id="m-{kinds.failed}"' in page
    assert UNCONFIRMED_COPY in page
    assert f'action="/outbox/{kinds.unconfirmed}/mark-sent"' in page
    assert f'action="/outbox/{kinds.unconfirmed}/resend"' in page
    assert f'action="/outbox/{kinds.failed}/resend"' in page
    assert f'action="/outbox/{kinds.failed}/mark-sent"' not in page
    assert "&lt;b&gt;550&lt;/b&gt; mailbox unavailable" in problems.text
    assert "It was sent" in page and "Send again" in page


async def test_withdraw_mark_sent_and_resend_round_trip(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    client = confirmed_client
    member_id = member.id
    kinds = await _seed_kinds(db, member)
    await login(client, member.email, password)
    token = await csrf_for(client, "/outbox?view=approved")
    for action, message_id in (
        ("withdraw", kinds.approved),
        ("mark-sent", kinds.unconfirmed),
        ("resend", kinds.failed),
    ):
        assert (await client.post(f"/outbox/{message_id}/{action}")).status_code == 403
    assert not [row for row in await _logs(db) if row.action != "message.approved"]

    resp = await client.post(
        f"/outbox/{kinds.approved}/withdraw", data={"csrf_token": token, "view": "approved"}
    )
    assert resp.status_code == 303 and "view=approved" in resp.headers["location"]
    assert "Approval withdrawn" in text_of(await client.get(resp.headers["location"]))
    withdrawn = await _fresh(db, kinds.approved)
    assert (withdrawn.status, withdrawn.approved_at) == ("awaiting_approval", None)
    sequence = await db.get(Sequence, withdrawn.sequence_id, populate_existing=True)
    assert sequence is not None and sequence.status == "awaiting_approval"

    resp = await client.post(
        f"/outbox/{kinds.unconfirmed}/mark-sent", data={"csrf_token": token, "view": "problems"}
    )
    assert resp.status_code == 303 and "view=problems" in resp.headers["location"]
    marked = await _fresh(db, kinds.unconfirmed)
    assert marked.status == "sent" and marked.message_id_header == "<unconfirmed@send.example.org>"
    follow = await _step2(db, marked.sequence_id)
    await db.refresh(follow)
    assert follow.due_at is not None  # what follows a send happened (config was passed)

    resp = await client.post(f"/outbox/{kinds.failed}/resend", data={"csrf_token": token})
    assert resp.status_code == 303
    again = await _fresh(db, kinds.failed)
    assert (again.status, again.approved_by_id, again.last_send_error) == (
        "approved",
        member_id,
        None,
    )

    logs = {row.action: row for row in await _logs(db)}
    for action, message_id in (
        ("message.withdrawn", kinds.approved),
        ("message.marked_sent", kinds.unconfirmed),
        ("message.resend", kinds.failed),
    ):
        row = logs[action]
        assert row.actor_id == member_id and row.target_id == str(message_id)
        assert set(row.details) == {"campaign_id", "recipient"}

    # Refusals: no longer in the right status.
    resp = await client.post(f"/outbox/{kinds.approved}/withdraw", data={"csrf_token": token})
    assert resp.status_code == 400 and NOT_APPROVED in text_of(resp)
    resp = await client.post(f"/outbox/{kinds.unconfirmed}/mark-sent", data={"csrf_token": token})
    assert resp.status_code == 400 and NOT_UNCONFIRMED in text_of(resp)
    resp = await client.post(f"/outbox/{uuid.uuid4()}/resend", data={"csrf_token": token})
    assert resp.status_code == 404


async def test_edit_page_is_kind_aware(
    confirmed_client: httpx.AsyncClient, member: User, password: str, db: AsyncSession
) -> None:
    kinds = await _seed_kinds(db, member)
    await login(confirmed_client, member.email, password)
    for message_id in (kinds.followup, kinds.reply):
        page = text_of(await confirmed_client.get(f"/outbox/{message_id}"))
        assert f'action="/outbox/{message_id}"' in page, message_id
        assert "can't be edited any more" not in page
    page = text_of(await confirmed_client.get(f"/outbox/{kinds.approved}"))
    assert "can't be edited any more" in page
