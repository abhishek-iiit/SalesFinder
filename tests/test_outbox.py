"""Outbox: approve, edit, reject and bulk approve re-run the gate; nothing is sent."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import (
    CATALOG,
    CLEAN,
    CONFIG,
    FACTS,
    NOW,
    add_address,
    add_person,
    seed_campaign,
    seed_company,
    seed_user,
)
from salesfinder import audit
from salesfinder.campaigns import service as campaigns_service
from salesfinder.catalog import Persona, Side
from salesfinder.db.models import (
    AuditLog,
    Campaign,
    Company,
    DoNotContact,
    EmailAddress,
    Message,
    OutboxFeedback,
    Sequence,
    User,
)
from salesfinder.gate.checks import ALREADY_CONTACTED, ECHO, PRICE
from salesfinder.outbox import service
from salesfinder.writer.compose import assemble, footer, signature
from salesfinder.writer.context import build_context


async def _draft(
    db: AsyncSession,
    campaign: Campaign,
    *,
    domain: str = "acme-soap.com",
    address: str = "jane.doe",
    written: dict[str, str] = CLEAN,
    created_offset: int = 0,
    side: Side = "buyer",
) -> tuple[Message, Message, Sequence, Company]:
    company, person, row = await seed_company(
        db, name=domain, domain=domain, address=address, side=side
    )
    assert row is not None
    sequence = Sequence(
        campaign_id=campaign.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=row.id,
        recipient=row.address,
        status="awaiting_approval",
    )
    db.add(sequence)
    await db.flush()
    first = assemble(
        written["first_subject"],
        written["first_body"],
        facts=FACTS,
        writing=CONFIG.writing,
        side=side,
    )
    second = assemble(
        written["followup_subject"],
        written["followup_body"],
        facts=FACTS,
        writing=CONFIG.writing,
        side=side,
    )
    m1 = Message(
        sequence_id=sequence.id,
        step=1,
        subject=first[0],
        body=first[1],
        status="awaiting_approval",
        gate_errors=[],
        created_at=NOW + timedelta(minutes=created_offset),
    )
    m2 = Message(
        sequence_id=sequence.id,
        step=2,
        subject=second[0],
        body=second[1],
        status="pending",
        gate_errors=[],
    )
    db.add_all([m1, m2])
    await db.flush()
    await db.commit()
    return m1, m2, sequence, company


def _tail(side: Side = "buyer") -> str:
    return signature(FACTS, side) + footer(FACTS, CONFIG.writing)


async def _approve(db: AsyncSession, message: Message, actor: User, **kw: object) -> list[str]:
    problems = await service.approve(
        db,
        message.id,
        actor=actor,
        now=NOW,
        facts=FACTS,
        config=CONFIG,
        catalog=CATALOG,
        **kw,  # type: ignore[arg-type]
    )
    await db.flush()
    return problems


async def _fresh[T](db: AsyncSession, model: type[T], pk: uuid.UUID) -> T:
    row = await db.get(model, pk, populate_existing=True)
    assert row is not None
    return row


# --------------------------------------------------------------------------- approve


async def test_approve_clean(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    assert await _approve(db, m1, user) == []
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.approved_by_id, m.approved_at, m.gate_errors) == (
        "approved",
        user.id,
        NOW,
        [],
    )
    assert (await _fresh(db, Sequence, seq.id)).status == "active"


async def test_explicit_prompt_text_is_what_the_echo_check_uses(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    # A prompt that contains the draft's own words makes it look like an echo of our prompt.
    echoing = m1.body.split("\n\n—\n")[0]
    assert await _approve(db, m1, user, prompt_text=echoing) == [ECHO]
    assert (await _fresh(db, Message, m1.id)).status == "gate_failed"


async def test_no_catalog_and_no_prompt_text_is_refused_untouched(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    subject, body = m1.subject, m1.body
    calls = (
        service.approve(db, m1.id, actor=user, now=NOW, facts=FACTS, config=CONFIG),
        service.edit(
            db, m1.id, subject="New", body="Hi,\n\nShort.", actor=user, now=NOW,
            facts=FACTS, config=CONFIG,
        ),
        service.recheck(db, m1.id, by=user, now=NOW, facts=FACTS, config=CONFIG),
    )  # fmt: skip
    for call in calls:
        with pytest.raises(ValueError, match="A catalog is needed"):
            await call
    m = await _fresh(db, Message, m1.id)
    assert (m.subject, m.body, m.status, m.edited) == (subject, body, "awaiting_approval", False)
    assert not db.dirty
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"


async def test_approve_after_recipient_became_dnc(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    db.add(
        DoNotContact(
            kind="email", value="jane.doe@acme-soap.com", reason="asked", added_by_id=user.id
        )
    )
    await db.flush()
    problems = await _approve(db, m1, user)
    assert any(p.startswith("Recipient is excluded") for p in problems)
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.gate_errors, m.approved_by_id) == ("gate_failed", problems, None)
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"


async def test_approve_fails_when_persona_unconfirmed(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    facts = FACTS.model_copy(
        update={"personas": {**FACTS.personas, "buyer": Persona(name="A", title="B")}}
    )
    problems = await service.approve(
        db, m1.id, actor=user, now=NOW, facts=facts, config=CONFIG, catalog=CATALOG
    )
    assert problems == ["Persona for buyer isn't confirmed in facts.yaml."]
    assert (await _fresh(db, Message, m1.id)).status == "gate_failed"


async def test_wrong_status_is_refused(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, m2, _, _ = await _draft(db, campaign)
    assert await _approve(db, m1, user) == []
    with pytest.raises(ValueError, match=r"^This email is no longer waiting for approval\.$"):
        await _approve(db, m1, user)
    with pytest.raises(ValueError, match=r"^This email is no longer waiting for approval\.$"):
        await _approve(db, m2, user)
    with pytest.raises(ValueError, match=r"^This email can't be edited any more\.$"):
        await service.edit(
            db, m1.id, subject="s", body="b", actor=user, now=NOW, facts=FACTS,
            config=CONFIG, catalog=CATALOG,
        )  # fmt: skip
    with pytest.raises(ValueError, match=r"^This email can't be rejected any more\.$"):
        await service.reject(db, m1.id, reason="no", actor=user, now=NOW)
    with pytest.raises(ValueError, match=r"^Email not found\.$"):
        await service.reject(db, uuid.uuid4(), reason="no", actor=user, now=NOW)


async def test_approve_after_archive_is_refused(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    held, _, _, _ = await _draft(db, campaign, domain="b-soap.com")
    held.status = "gate_failed"
    await db.commit()
    archived = await campaigns_service.archive(db, campaign.id, actor=user)
    await db.commit()
    assert (archived.sequences_stopped, archived.messages_cancelled) == (2, 4)

    with pytest.raises(ValueError, match=r"^This campaign is archived\.$"):
        await _approve(db, m1, user)
    assert (await _fresh(db, Message, m1.id)).status == "cancelled"
    assert (await _fresh(db, Sequence, seq.id)).status == "stopped"
    assert await service.bulk_approve(
        db, [m1.id], actor=user, now=NOW, facts=FACTS, config=CONFIG, catalog=CATALOG
    ) == {m1.id: [service.ARCHIVED]}
    assert service.ARCHIVED in service.REFUSALS
    assert await service.list_outbox(db) == ([], 0)


async def test_archived_campaign_refuses_every_outbox_action(db: AsyncSession) -> None:
    """Even a message still waiting (archived before this fix, say) is refused for approve,
    re-check, edit and reject, and left exactly as it was."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    held, _, _, _ = await _draft(db, campaign, domain="b-soap.com")
    held.status = "gate_failed"
    campaign.status = "archived"
    await db.commit()
    ready_id, held_id = m1.id, held.id
    calls = (
        service.approve(
            db, m1.id, actor=user, now=NOW, facts=FACTS, config=CONFIG, catalog=CATALOG
        ),
        service.recheck(
            db, held.id, by=user, now=NOW, facts=FACTS, config=CONFIG, catalog=CATALOG
        ),
        service.edit(
            db, m1.id, subject="New", body="Hi,\n\nShort.", actor=user, now=NOW,
            facts=FACTS, config=CONFIG, catalog=CATALOG,
        ),
        service.reject(db, held.id, reason="No", actor=user, now=NOW),
    )  # fmt: skip
    for call in calls:
        with pytest.raises(ValueError, match=r"^This campaign is archived\.$"):
            await call
        await db.rollback()
    assert (await _fresh(db, Message, ready_id)).status == "awaiting_approval"
    assert (await _fresh(db, Message, held_id)).status == "gate_failed"


async def test_paused_campaign_drafts_can_still_be_approved(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    await campaigns_service.pause(db, campaign.id, actor=user)
    await db.commit()
    [item], _ = await service.list_outbox(db)
    assert item.campaign_paused and item.campaign_status == "paused"
    assert await _approve(db, m1, user) == []
    assert (await _fresh(db, Message, m1.id)).status == "approved"
    assert (await _fresh(db, Sequence, seq.id)).status == "active"


async def test_gate_failed_cannot_be_approved_directly(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    (await _fresh(db, Message, m1.id)).status = "gate_failed"
    await db.flush()
    with pytest.raises(ValueError, match="no longer waiting"):
        await _approve(db, m1, user)


# --------------------------------------------------------------------------- edit


async def _edit(
    db: AsyncSession, message: Message, actor: User, *, subject: str, body: str
) -> list[str]:
    problems = await service.edit(
        db,
        message.id,
        subject=subject,
        body=body,
        actor=actor,
        now=NOW,
        facts=FACTS,
        config=CONFIG,
        catalog=CATALOG,
    )
    await db.flush()
    return problems


async def test_edit_readds_footer_and_signature(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    assert (
        await _edit(db, m1, user, subject="  New\nsubject ", body="Hi,\n\nShort note.\n\nThanks,")
        == []
    )
    m = await _fresh(db, Message, m1.id)
    assert m.subject == "New subject"
    assert m.body == "Hi,\n\nShort note.\n\nThanks," + signature(FACTS, "buyer") + footer(
        FACTS, CONFIG.writing
    )
    assert (m.edited, m.status, m.gate_errors) == (True, "awaiting_approval", [])


async def test_edit_does_not_duplicate_pasted_signature_and_footer(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    full = (
        "Hi,\n\nShort note.\n\nThanks," + signature(FACTS, "buyer") + footer(FACTS, CONFIG.writing)
    )
    assert await _edit(db, m1, user, subject="Hello", body=full.replace("\n", "\r\n")) == []
    m = await _fresh(db, Message, m1.id)
    assert m.body == full


async def test_edit_strips_pasted_footer_alone_and_signature_alone(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    full = "Hi,\n\nShort note.\n\nThanks," + _tail()
    for pasted in (
        "Hi,\n\nShort note.\n\nThanks," + footer(FACTS, CONFIG.writing),
        "Hi,\n\nShort note.\n\nThanks," + signature(FACTS, "buyer"),
    ):
        assert await _edit(db, m1, user, subject="Hello", body=pasted) == []
        assert (await _fresh(db, Message, m1.id)).body == full


async def test_edit_supplier_side_signature(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, side="supplier")
    m1, _, _, _ = await _draft(db, campaign, written=CLEAN, side="supplier")
    pasted = "Hi,\n\nShort note.\n\nThanks," + signature(FACTS, "supplier")
    assert await _edit(db, m1, user, subject="Hello", body=pasted) == []
    body = (await _fresh(db, Message, m1.id)).body
    assert body == "Hi,\n\nShort note.\n\nThanks," + _tail("supplier")
    assert "Vikram Shah" in body and "Asha Rao" not in body


async def test_edit_with_price_fails_gate_and_can_be_fixed(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    problems = await _edit(db, m1, user, subject="Hello", body="Hi,\n\nOnly $5 per kg.\n\nThanks,")
    assert problems == [PRICE]
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.gate_errors, m.edited) == ("gate_failed", [PRICE], True)
    assert "Mentions a price" in PRICE
    # never straight to approved; a clean edit from gate_failed goes back to awaiting_approval
    assert await _edit(db, m1, user, subject="Hello", body="Hi,\n\nShort.\n\nThanks,") == []
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.gate_errors) == ("awaiting_approval", [])
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"


async def test_edit_empty_body_raises_and_changes_nothing(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    before = m1.body
    with pytest.raises(ValueError, match=r"^The email body is empty\.$"):
        await _edit(db, m1, user, subject="Hello", body="  \n ")
    sig_only = signature(FACTS, "buyer") + footer(FACTS, CONFIG.writing)
    with pytest.raises(ValueError, match=r"^The email body is empty\.$"):
        await _edit(db, m1, user, subject="Hello", body=sig_only)
    m = await _fresh(db, Message, m1.id)
    assert (m.body, m.edited) == (before, False)


async def test_edit_subject_too_long_raises(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    with pytest.raises(ValueError, match=r"^Subject is longer than 200 characters\.$"):
        await _edit(db, m1, user, subject="x" * 201, body="Hi,\n\nShort.\n\nThanks,")


# --------------------------------------------------------------------------- reject


async def test_reject(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, m2, seq, _ = await _draft(db, campaign)
    for bad in ("", "   ", "x" * 501):
        with pytest.raises(ValueError, match=r"^Give a reason of 1 to 500 characters\.$"):
            await service.reject(db, m1.id, reason=bad, actor=user, now=NOW)
    await service.reject(db, m1.id, reason="  Too pushy  ", actor=user, now=NOW)
    await db.flush()
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.rejected_by_id, m.rejected_at, m.reject_reason) == (
        "rejected",
        user.id,
        NOW,
        "Too pushy",
    )
    s = await _fresh(db, Sequence, seq.id)
    assert (s.status, s.stop_reason) == ("stopped", "Rejected in the Outbox")
    assert (await _fresh(db, Message, m2.id)).status == "cancelled"
    fb = (await db.scalars(select(OutboxFeedback))).all()
    assert [(f.campaign_id, f.message_id, f.reason, f.created_by_id) for f in fb] == [
        (campaign.id, m1.id, "Too pushy", user.id)
    ]

    # the feedback appears in the next WriterInput for the campaign
    other, person, _ = await seed_company(db, name="Other", domain="other.com", address="bob")
    inp = await build_context(
        db,
        campaign=await _fresh(db, Campaign, campaign.id),
        company=other,
        person=person,
        catalog=CATALOG,
        facts=FACTS,
        config=CONFIG,
    )
    assert inp.feedback == ["Too pushy"]


async def test_reject_from_gate_failed(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    (await _fresh(db, Message, m1.id)).status = "gate_failed"
    await db.flush()
    await service.reject(db, m1.id, reason="bad fit", actor=user, now=NOW)
    assert (await _fresh(db, Message, m1.id)).status == "rejected"


# --------------------------------------------------------------------------- bulk


async def test_bulk_approve_one_bad_does_not_block_others(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    good1, _, s1, _ = await _draft(db, campaign, domain="a.com")
    bad, _, s2, _ = await _draft(db, campaign, domain="b.com")
    good2, _, s3, _ = await _draft(db, campaign, domain="c.com")
    done, _, _, _ = await _draft(db, campaign, domain="d.com")
    assert await _approve(db, done, user) == []
    db.add(DoNotContact(kind="email", value="jane.doe@b.com", reason="asked", added_by_id=user.id))
    await db.flush()
    ghost = uuid.uuid4()
    result = await service.bulk_approve(
        db,
        [good1.id, bad.id, good2.id, done.id, ghost, good1.id],
        actor=user,
        now=NOW,
        facts=FACTS,
        config=CONFIG,
        catalog=CATALOG,
    )
    assert set(result) == {good1.id, bad.id, good2.id, done.id, ghost}
    assert result[good1.id] == result[good2.id] == []
    assert result[bad.id] and result[bad.id][0].startswith("Recipient is excluded")
    assert result[done.id] == ["This email is no longer waiting for approval."]
    assert result[ghost] == ["Email not found."]
    assert (await _fresh(db, Message, good1.id)).status == "approved"
    assert (await _fresh(db, Message, good2.id)).status == "approved"
    assert (await _fresh(db, Message, bad.id)).status == "gate_failed"
    assert (await _fresh(db, Sequence, s1.id)).status == "active"
    assert (await _fresh(db, Sequence, s2.id)).status == "awaiting_approval"
    assert (await _fresh(db, Sequence, s3.id)).status == "active"


# --------------------------------------------------------------------------- list


async def test_list_outbox(db: AsyncSession) -> None:
    user = await seed_user(db)
    c1 = await seed_campaign(db, user=user, name="One")
    c2 = await seed_campaign(db, user=user, name="Two")
    late, _, _, _ = await _draft(db, c1, domain="late.com", created_offset=5)
    early, _, _, _ = await _draft(db, c2, domain="early.com", created_offset=1)
    failed, _, _, _ = await _draft(db, c1, domain="failed.com", created_offset=3)
    approved, _, _, _ = await _draft(db, c1, domain="done.com", created_offset=2)
    (await _fresh(db, Message, failed.id)).status = "gate_failed"
    (await _fresh(db, Message, approved.id)).status = "approved"
    await db.flush()

    items, total = await service.list_outbox(db)
    assert total == 3
    assert [i.message.id for i in items] == [early.id, failed.id, late.id]
    first = items[0]
    assert (first.campaign_name, first.company.domain, first.recipient) == (
        "Two",
        "early.com",
        "jane.doe@early.com",
    )
    assert first.person is not None and first.person.full_name == "Jane Doe"
    assert first.sequence.id == first.message.sequence_id

    items, total = await service.list_outbox(db, campaign_id=c1.id)
    assert (total, [i.message.id for i in items]) == (2, [failed.id, late.id])
    items, total = await service.list_outbox(db, limit=1, offset=1)
    assert (total, [i.message.id for i in items]) == (3, [failed.id])


async def test_deleted_address_row_fails_the_gate_instead_of_crashing(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    row = await db.get(EmailAddress, (await _fresh(db, Sequence, seq.id)).email_address_id)
    assert row is not None
    await db.delete(row)
    await db.flush()
    problems = await _approve(db, m1, user)
    assert problems
    assert (await _fresh(db, Message, m1.id)).status == "gate_failed"


async def test_reject_reason_whitespace_and_controls_collapse(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    await service.reject(db, m1.id, reason=" Too \n\n pushy\x00\ttone\x1b[0m ", actor=user, now=NOW)
    assert (await _fresh(db, Message, m1.id)).reject_reason == "Too pushy tone [0m"
    m2, _, _, _ = await _draft(db, campaign, domain="x.com")
    with pytest.raises(ValueError, match="Give a reason"):
        await service.reject(db, m2.id, reason="\x00 \n\t", actor=user, now=NOW)


# --------------------------------------------------------------------------- recheck


async def _recheck(db: AsyncSession, message: Message, actor: User) -> list[str]:
    problems = await service.recheck(
        db, message.id, by=actor, now=NOW, facts=FACTS, config=CONFIG, catalog=CATALOG
    )
    await db.flush()
    return problems


async def test_recheck_after_hold_lifted(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    hold = DoNotContact(
        kind="email", value="jane.doe@acme-soap.com", reason="a", added_by_id=user.id
    )
    db.add(hold)
    await db.flush()
    assert await _approve(db, m1, user)  # -> gate_failed
    assert await _recheck(db, m1, user)  # hold persists: still gate_failed, fresh errors
    m = await _fresh(db, Message, m1.id)
    assert m.status == "gate_failed" and m.gate_errors[0].startswith("Recipient is excluded")
    await db.delete(hold)
    await db.flush()
    assert await _recheck(db, m1, user) == []
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.gate_errors, m.edited, m.approved_by_id) == (
        "awaiting_approval",
        [],
        False,
        None,
    )
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"


async def test_recheck_only_from_gate_failed(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    with pytest.raises(ValueError, match=r"^This email is no longer waiting for approval\.$"):
        await _recheck(db, m1, user)


async def test_list_hides_messages_of_stopped_sequences(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    stopped, _, seq, _ = await _draft(db, campaign, domain="s.com")
    shown, _, _, _ = await _draft(db, campaign, domain="t.com")
    (await _fresh(db, Sequence, seq.id)).status = "stopped"
    await db.flush()
    items, total = await service.list_outbox(db)
    assert (total, [i.message.id for i in items]) == (1, [shown.id])
    assert stopped.id not in [i.message.id for i in items]


async def test_bulk_results_follow_the_callers_order(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    ids = [(await _draft(db, campaign, domain=f"{n}.com"))[0].id for n in "abcd"]
    asked = [ids[2], uuid.UUID(int=1), ids[0], ids[2], ids[3], ids[1]]
    result = await service.bulk_approve(
        db, asked, actor=user, now=NOW, facts=FACTS, config=CONFIG, catalog=CATALOG
    )
    assert list(result) == [ids[2], uuid.UUID(int=1), ids[0], ids[3], ids[1]]
    assert result[uuid.UUID(int=1)] == ["Email not found."]


async def test_a_side_without_a_persona_is_refused_untouched(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    facts = FACTS.model_copy(update={"personas": {"supplier": FACTS.personas["supplier"]}})
    calls = (
        service.approve(
            db, m1.id, actor=user, now=NOW, facts=facts, config=CONFIG, catalog=CATALOG
        ),
        service.edit(
            db, m1.id, subject="New", body="Hi,\n\nShort.", actor=user, now=NOW,
            facts=facts, config=CONFIG, catalog=CATALOG,
        ),
    )  # fmt: skip
    for call in calls:
        with pytest.raises(ValueError, match="No sender persona"):
            await call
    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.edited) == ("awaiting_approval", False)
    assert (await service.bulk_approve(
        db, [m1.id], actor=user, now=NOW, facts=facts, config=CONFIG, catalog=CATALOG
    )) == {m1.id: [service.NO_PERSONA]}  # fmt: skip
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"


async def test_same_address_on_both_sides_is_already_contacted_at_approval(
    db: AsyncSession,
) -> None:
    """Cross-side dedupe (Phase 4 #6): one person at a company that is both a buyer and a
    supplier is emailed by one campaign at a time."""
    user = await seed_user(db)
    buyer = await seed_campaign(db, user=user)
    supplier = await seed_campaign(db, user=user, side="supplier")
    m1, _, _, _ = await _draft(db, buyer)
    assert await _approve(db, m1, user) == []
    await db.commit()

    company = Company(
        side="supplier",
        name="Acme Soap Works",
        domain="acme-soap.com",
        website_url="https://acme-soap.com",
        status="new",
        profile_status="ok",
        profiled_at=NOW,
        scored_at=NOW,
        score=50,
        contacts_checked_at=NOW,
        contacts_found=1,
        sendable_contacts=1,
    )
    db.add(company)
    await db.flush()
    person = await add_person(db, company, "Jane Doe", rank=1)
    row = await add_address(db, company, person, "jane.doe@acme-soap.com")
    sequence = Sequence(
        campaign_id=supplier.id,
        company_id=company.id,
        person_id=person.id,
        email_address_id=row.id,
        recipient=row.address,
        status="awaiting_approval",
    )
    db.add(sequence)
    await db.flush()
    subject, body = assemble(
        CLEAN["first_subject"], CLEAN["first_body"], facts=FACTS, writing=CONFIG.writing,
        side="supplier",
    )  # fmt: skip
    second = Message(
        sequence_id=sequence.id, step=1, subject=subject, body=body, status="awaiting_approval"
    )
    db.add(second)
    await db.commit()

    problems = await _approve(db, second, user)
    assert problems == [ALREADY_CONTACTED]
    assert (await _fresh(db, Message, second.id)).status == "gate_failed"
    assert (await _fresh(db, Sequence, sequence.id)).status == "awaiting_approval"


# --------------------------------------------------------------------------- follow-ups (Task 7)

FIRST_ID = "<first@send-domain.com>"


async def _due_followup(
    db: AsyncSession, campaign: Campaign, *, domain: str = "acme-soap.com"
) -> tuple[Message, Message, Sequence]:
    """Step 1 sent six days ago, its sequence active, step 2 fallen due and queued for
    review (as ``followups.service.queue_due`` leaves it)."""
    m1, m2, seq, _ = await _draft(db, campaign, domain=domain)
    m1 = await _fresh(db, Message, m1.id)
    m1.status = "sent"
    m1.sent_at = NOW - timedelta(days=6)
    m1.message_id_header = f"<first@{domain}>"
    seq = await _fresh(db, Sequence, seq.id)
    seq.status = "active"
    m2 = await _fresh(db, Message, m2.id)
    m2.status = "awaiting_approval"
    m2.subject = f"Re: {m1.subject}"
    m2.due_at = NOW - timedelta(hours=1)
    await db.commit()
    return m1, m2, seq


async def _reply_draft(
    db: AsyncSession, campaign: Campaign, *, status: str = "awaiting_approval"
) -> tuple[Message, Sequence]:
    """They replied to step 1 (the sequence is ``replied``, step 2 cancelled) and an answer
    was drafted."""
    m1, m2, seq = await _due_followup(db, campaign)
    m2.status = "cancelled"
    seq.status = "replied"
    subject, body = assemble(
        f"Re: {m1.subject}",
        "Hi Jane,\n\nThanks for your note. Happy to send a sample next week.\n\nThanks,",
        facts=FACTS,
        writing=CONFIG.writing,
        side="buyer",
    )
    reply = Message(
        sequence_id=seq.id,
        step=3,
        kind="reply",
        subject=subject,
        body=body,
        status=status,
        gate_errors=[],
    )
    db.add(reply)
    await db.commit()
    return reply, seq


async def test_followup_approve_keeps_the_sequence_active(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    _, m2, seq = await _due_followup(db, campaign)
    assert await _approve(db, m2, user) == []
    m = await _fresh(db, Message, m2.id)
    assert (m.status, m.approved_by_id, m.approved_at) == ("approved", user.id, NOW)
    assert (await _fresh(db, Sequence, seq.id)).status == "active"


async def test_followup_actions_need_an_active_sequence(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    _, m2, seq = await _due_followup(db, campaign)
    (await _fresh(db, Sequence, seq.id)).status = "stopped"
    await db.commit()
    with pytest.raises(ValueError, match=r"^This email is no longer waiting for approval\.$"):
        await _approve(db, m2, user)
    with pytest.raises(ValueError, match=r"^This email can't be rejected any more\.$"):
        await service.reject(db, m2.id, reason="no", actor=user, now=NOW)
    assert await service.list_outbox(db) == ([], 0)


async def test_followup_edit_uses_the_followup_word_limit(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign, domain="first.com")
    _, m2, _ = await _due_followup(db, campaign)
    # 120 words: over the follow-up's 100, under the first email's 140.
    words = "Hi,\n\n" + " ".join(["sample"] * 118) + "\n\nThanks,"
    assert CONFIG.writing.followup_max_words < 120 < CONFIG.writing.first_email_max_words
    followup_problems = await _edit(db, m2, user, subject="Re: Samples", body=words)
    assert "Email is longer than 100 words." in followup_problems
    first_problems = await _edit(db, m1, user, subject="Samples", body=words)
    assert not any("longer than" in p for p in first_problems)
    m = await _fresh(db, Message, m2.id)
    assert (m.status, m.edited) == ("gate_failed", True)


async def test_followup_recheck(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    _, m2, _ = await _due_followup(db, campaign)
    (await _fresh(db, Message, m2.id)).status = "gate_failed"
    await db.commit()
    assert await _recheck(db, m2, user) == []
    assert (await _fresh(db, Message, m2.id)).status == "awaiting_approval"


async def test_followup_reject_stops_the_sequence(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, m2, seq = await _due_followup(db, campaign)
    await service.reject(db, m2.id, reason="Too soon", actor=user, now=NOW)
    await db.flush()
    m = await _fresh(db, Message, m2.id)
    assert (m.status, m.reject_reason, m.rejected_by_id) == ("rejected", "Too soon", user.id)
    s = await _fresh(db, Sequence, seq.id)
    assert (s.status, s.stop_reason) == ("stopped", "Follow-up rejected in the Outbox")
    assert (await _fresh(db, Message, m1.id)).status == "sent"
    fb = (await db.scalars(select(OutboxFeedback))).all()
    assert [(f.message_id, f.reason) for f in fb] == [(m2.id, "Too soon")]


# --------------------------------------------------------------------------- replies (generic)


async def test_reply_draft_is_reviewed_while_replied(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    reply, seq = await _reply_draft(db, campaign)
    [item], total = await service.list_outbox(db)
    assert (total, item.kind, item.message.id, item.in_reply_to_text) == (
        1,
        "reply",
        reply.id,
        None,
    )
    assert item.step1_sent_at == NOW - timedelta(days=6)

    # Gated with the reply prompt (replies.draft.instructions_for).
    assert await _approve(db, reply, user) == []
    assert (await _fresh(db, Message, reply.id)).status == "approved"
    assert (await _fresh(db, Sequence, seq.id)).status == "replied"

    await service.withdraw(db, reply.id, actor=user, now=NOW)
    assert (await _fresh(db, Message, reply.id)).status == "awaiting_approval"
    assert (await _fresh(db, Sequence, seq.id)).status == "replied"


async def test_reply_reject_only_rejects_the_draft(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    reply, seq = await _reply_draft(db, campaign)
    await service.reject(db, reply.id, reason="Wrong tone", actor=user, now=NOW)
    await db.flush()
    assert (await _fresh(db, Message, reply.id)).status == "rejected"
    assert (await _fresh(db, Sequence, seq.id)).status == "replied"
    assert (await db.scalars(select(OutboxFeedback))).all() == []


async def test_counts_include_due_followups_and_reply_drafts(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    await _draft(db, campaign, domain="first.com")
    await _due_followup(db, campaign, domain="follow.com")
    reply, _ = await _reply_draft(db, campaign, status="gate_failed")
    # Each draft's step 2 that isn't due (pending) is not in the Outbox: two first emails,
    # one follow-up, one reply.
    await _draft(db, campaign, domain="second.com")
    assert await service.outbox_counts(db) == {"awaiting_approval": 3, "gate_failed": 1}
    assert await service.outbox_counts(db, campaign_id=campaign.id) == {
        "awaiting_approval": 3,
        "gate_failed": 1,
    }
    kinds = {i.kind for i in (await service.list_outbox(db))[0]}
    assert kinds == {"first", "followup", "reply"}
    for kind in ("first", "followup", "reply"):
        items, total = await service.list_outbox(db, kind=kind)  # type: ignore[arg-type]
        assert (total, {i.kind for i in items}) == (len(items), {kind})
    assert (await service.list_outbox(db, kind="reply"))[0][0].message.id == reply.id


# --------------------------------------------------------------------------- withdraw


async def test_withdraw_puts_a_first_email_back_in_the_outbox(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    assert await _approve(db, m1, user) == []
    m = await _fresh(db, Message, m1.id)
    m.send_attempts = 1
    m.next_attempt_at = NOW + timedelta(minutes=5)
    m.message_id_header = FIRST_ID
    await db.commit()
    [item], _ = await service.list_outbox(db, view="approved")
    assert item.message.id == m1.id and not item.held_by_campaign_pause

    await service.withdraw(db, m1.id, actor=user, now=NOW)
    await db.flush()

    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.approved_by_id, m.approved_at) == ("awaiting_approval", None, None)
    assert (m.send_attempts, m.next_attempt_at, m.message_id_header) == (0, None, FIRST_ID)
    assert (await _fresh(db, Sequence, seq.id)).status == "awaiting_approval"
    [item], _ = await service.list_outbox(db)
    assert item.message.id == m1.id
    assert await service.list_outbox(db, view="approved") == ([], 0)
    with pytest.raises(ValueError, match=r"^This email isn't waiting to send any more\.$"):
        await service.withdraw(db, m1.id, actor=user, now=NOW)


async def test_withdraw_a_followup_keeps_the_sequence_active(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    _, m2, seq = await _due_followup(db, campaign)
    assert await _approve(db, m2, user) == []
    await service.withdraw(db, m2.id, actor=user, now=NOW)
    assert (await _fresh(db, Message, m2.id)).status == "awaiting_approval"
    assert (await _fresh(db, Sequence, seq.id)).status == "active"
    [item], _ = await service.list_outbox(db)
    assert item.message.id == m2.id


async def test_withdraw_while_sending_is_refused(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    assert await _approve(db, m1, user) == []
    (await _fresh(db, Message, m1.id)).status = "sending"
    await db.commit()
    with pytest.raises(ValueError, match=r"^This email isn't waiting to send any more\.$"):
        await service.withdraw(db, m1.id, actor=user, now=NOW)
    assert (await _fresh(db, Message, m1.id)).status == "sending"
    assert (await _fresh(db, Sequence, seq.id)).status == "active"


# --------------------------------------------------------------------------- problems


async def _unconfirmed(db: AsyncSession, message: Message, *, status: str = "unconfirmed") -> None:
    m = await _fresh(db, Message, message.id)
    m.status = status
    m.claimed_at = NOW - timedelta(hours=2)
    m.message_id_header = FIRST_ID
    m.send_attempts = 2
    m.next_attempt_at = NOW - timedelta(hours=1)
    m.last_send_error = "TimeoutError"
    seq = await _fresh(db, Sequence, m.sequence_id)
    seq.status = "active"
    db.add(
        AuditLog(
            action=audit.MESSAGE_UNCONFIRMED,
            actor_id=None,
            target_type="message",
            target_id=str(m.id),
            details={"reason": "not_in_sent"},
        )
    )
    await db.commit()


async def test_mark_sent_schedules_the_followup(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, m2, seq, _ = await _draft(db, campaign)
    await _unconfirmed(db, m1)
    [item], total = await service.list_outbox(db, view="problems")
    assert (total, item.message.id) == (1, m1.id)

    await service.mark_sent(db, m1.id, actor=user, now=NOW, config=CONFIG)
    await db.commit()

    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.sent_at, m.message_id_header) == (
        "sent",
        NOW - timedelta(hours=2),
        FIRST_ID,
    )
    follow = await _fresh(db, Message, m2.id)
    assert follow.status == "pending" and follow.due_at is not None
    assert follow.due_at > NOW
    assert (await _fresh(db, Sequence, seq.id)).status == "active"
    assert await service.list_outbox(db, view="problems") == ([], 0)
    sent_rows = (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.MESSAGE_SENT))
    ).all()
    assert [r.target_id for r in sent_rows] == [str(m1.id)]
    # The "may have gone" marker stays.
    assert (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.MESSAGE_UNCONFIRMED))
    ).all()
    with pytest.raises(ValueError, match=r"^This email isn't waiting for a check any more\.$"):
        await service.mark_sent(db, m1.id, actor=user, now=NOW, config=CONFIG)


async def test_mark_sent_without_a_claim_time_uses_now(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign)
    await _unconfirmed(db, m1)
    (await _fresh(db, Message, m1.id)).claimed_at = None
    await db.commit()
    await service.mark_sent(db, m1.id, actor=user, now=NOW, config=CONFIG)
    assert (await _fresh(db, Message, m1.id)).sent_at == NOW


async def test_mark_sent_followup_completes_the_sequence(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    _, m2, seq = await _due_followup(db, campaign)
    await _unconfirmed(db, m2)
    await service.mark_sent(db, m2.id, actor=user, now=NOW, config=CONFIG)
    assert (await _fresh(db, Message, m2.id)).status == "sent"
    assert (await _fresh(db, Sequence, seq.id)).status == "completed"


@pytest.mark.parametrize("status", ["unconfirmed", "failed"])
async def test_resend_approves_again_and_keeps_the_message_id(
    db: AsyncSession, status: str
) -> None:
    user = await seed_user(db)
    other = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    await _unconfirmed(db, m1, status=status)
    later = NOW + timedelta(days=1)

    await service.resend(db, m1.id, actor=other, now=later)
    await db.commit()

    m = await _fresh(db, Message, m1.id)
    assert (m.status, m.approved_by_id, m.approved_at) == ("approved", other.id, later)
    assert (m.send_attempts, m.next_attempt_at, m.last_send_error) == (0, None, None)
    assert m.message_id_header == FIRST_ID
    assert (await _fresh(db, Sequence, seq.id)).status == "active"
    assert (
        await db.scalars(select(AuditLog).where(AuditLog.action == audit.MESSAGE_UNCONFIRMED))
    ).all()
    with pytest.raises(ValueError, match=r"^This email can't be sent again\.$"):
        await service.resend(db, m1.id, actor=user, now=later)


async def test_resend_refused_once_the_sequence_moved_on(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, seq, _ = await _draft(db, campaign)
    await _unconfirmed(db, m1)
    (await _fresh(db, Sequence, seq.id)).status = "replied"
    await db.commit()
    with pytest.raises(ValueError, match=r"^Its sequence has moved on, so it can't be sent"):
        await service.resend(db, m1.id, actor=user, now=NOW)
    assert (await _fresh(db, Message, m1.id)).status == "unconfirmed"


async def test_approved_view_shows_a_campaign_pause_on_cold_mail_only(db: AsyncSession) -> None:
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user)
    m1, _, _, _ = await _draft(db, campaign, domain="first.com")
    reply, _ = await _reply_draft(db, campaign)
    assert await _approve(db, m1, user) == []
    assert await _approve(db, reply, user) == []
    await campaigns_service.pause(db, campaign.id, actor=user)
    await db.commit()
    items, total = await service.list_outbox(db, view="approved")
    assert total == 2
    held = {i.kind: i.held_by_campaign_pause for i in items}
    assert held == {"first": True, "reply": False}
