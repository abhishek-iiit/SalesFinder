"""Do-not-contact list: add, remove, list. Services flush; callers commit.

Adding or removing an entry refreshes the stored addresses it covers (their ``blocked_reason``
and their companies' sendable counts) in the same transaction."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.auth.emails import normalize_email
from salesfinder.contacts.service import refresh_blocks
from salesfinder.db.models import DoNotContact, User
from salesfinder.domains import normalize_domain

INVALID_ENTRY = "Enter an email address or a domain."
DUPLICATE_ENTRY = "Already on the list."
REASON_REQUIRED = "Enter a reason."
REASON_TOO_LONG = "Keep the reason under 200 characters."
MAX_REASON = 200


def _classify(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if "@" in text:
        try:
            return "email", normalize_email(text)
        except ValueError:
            raise ValueError(INVALID_ENTRY) from None
    domain = normalize_domain(text)
    if domain is None:
        raise ValueError(INVALID_ENTRY)
    return "domain", domain


async def add_entry(
    db: AsyncSession, *, raw: str, reason: str, added_by: User | None
) -> DoNotContact:
    kind, value = _classify(raw)
    reason = reason.strip()
    if not reason:
        raise ValueError(REASON_REQUIRED)
    if len(reason) > MAX_REASON:
        raise ValueError(REASON_TOO_LONG)
    existing = await db.scalar(
        select(DoNotContact.id).where(DoNotContact.kind == kind, DoNotContact.value == value)
    )
    if existing is not None:
        raise ValueError(DUPLICATE_ENTRY)
    entry = DoNotContact(
        kind=kind,
        value=value,
        reason=reason,
        added_by_id=added_by.id if added_by else None,
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry, ["created_at"])
    await _refresh(db, entry)
    return entry


async def _refresh(db: AsyncSession, entry: DoNotContact) -> None:
    if entry.kind == "email":
        await refresh_blocks(db, email=entry.value)
    else:
        await refresh_blocks(db, domain=entry.value)


async def remove_entry(db: AsyncSession, entry_id: uuid.UUID) -> DoNotContact:
    entry = await db.get(DoNotContact, entry_id)
    if entry is None:
        raise LookupError("Entry not found.")
    await db.delete(entry)
    await db.flush()
    await _refresh(db, entry)
    return entry


async def list_entries(db: AsyncSession) -> list[DoNotContact]:
    rows = await db.scalars(select(DoNotContact).order_by(DoNotContact.created_at.desc()))
    return list(rows)
