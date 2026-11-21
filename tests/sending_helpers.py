"""Seeding for sending tests: an active buyer campaign with a ready mailbox (enabled, password
set, fresh DNS, warmed up), a fresh HubSpot sync, sending resumed, and an approved first email."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import seed_campaign, seed_draft, seed_user
from salesfinder.db.models import Campaign, HubSpotSync, Mailbox, Message, Sequence
from salesfinder.sending import state
from salesfinder.settings import Settings

# Tuesday 13:00 in New York, 10:00 in Los Angeles: inside a buyer's office hours.
SEND_NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
BOX = "sales@send-domain.com"
SECRET = "sales_box"  # noqa: S105
SUPPLIER_SECRET = "supplier_box"  # noqa: S105
RECIPIENT = "jane.doe@acme-soap.com"
SENT_FOLDER = "[Gmail]/Sent Mail"


@dataclass(frozen=True)
class Ready:
    user_id: uuid.UUID
    campaign_id: uuid.UUID
    mailbox_id: uuid.UUID
    message_id: uuid.UUID
    sequence_id: uuid.UUID
    step2_id: uuid.UUID
    company_id: uuid.UUID


def sending_settings(test_settings: Settings) -> Settings:
    """``test_settings`` plus both mailboxes' passwords and a HubSpot token."""
    return test_settings.model_copy(
        update={
            "mailbox_passwords": {
                SECRET: SecretStr("app-password"),
                SUPPLIER_SECRET: SecretStr("x"),
            },
            "hubspot_token": SecretStr("hs-token"),
        }
    )


async def ready_mailbox(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    address: str = BOX,
    side: str = "buyer",
    secret: str = SECRET,
) -> Mailbox:
    box = Mailbox(
        address=address,
        side=side,
        username=address,
        secret_name=secret,
        enabled=True,
        created_by_id=user_id,
        dns_ok=True,
        dns_checked_at=SEND_NOW - timedelta(hours=1),
        warmup_started_on=SEND_NOW.date() - timedelta(days=60),
        daily_max=30,
    )
    db.add(box)
    await db.flush()
    return box


async def approve(db: AsyncSession, message: Message, user_id: uuid.UUID) -> None:
    message.status = "approved"
    message.approved_by_id = user_id
    message.approved_at = SEND_NOW - timedelta(hours=1)
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    sequence.status = "active"
    await db.flush()


async def seed_ready(
    db: AsyncSession,
    *,
    resume: bool = True,
    campaign_mailbox: bool = True,
    domain: str = "acme-soap.com",
    approved: bool = True,
) -> Ready:
    """An active buyer campaign sending from an enabled, warmed-up mailbox with fresh DNS, a
    fresh HubSpot sync, sending resumed, and one approved first email to Jane Doe."""
    user = await seed_user(db)
    campaign = await seed_campaign(db, user=user, mailbox=False, activate=False)
    box = await ready_mailbox(db, user.id)
    row = await db.get(Campaign, campaign.id)
    assert row is not None
    row.status = "active"
    row.activated_at = SEND_NOW - timedelta(days=2)
    if campaign_mailbox:
        row.mailbox_id = box.id
    db.add(
        HubSpotSync(
            status="succeeded",
            started_at=SEND_NOW - timedelta(hours=2),
            finished_at=SEND_NOW - timedelta(hours=1),
        )
    )
    await db.flush()
    message = await seed_draft(db, campaign, domain=domain, checked_at=SEND_NOW)
    if approved:
        await approve(db, message, user.id)
    if resume:
        await state.resume(db, actor=None, now=SEND_NOW - timedelta(days=1))
    sequence = await db.get(Sequence, message.sequence_id)
    assert sequence is not None
    step2 = await db.scalar(
        select(Message).where(Message.sequence_id == sequence.id, Message.step == 2)
    )
    assert step2 is not None
    await db.commit()
    return Ready(
        user.id, campaign.id, box.id, message.id, sequence.id, step2.id, sequence.company_id
    )
