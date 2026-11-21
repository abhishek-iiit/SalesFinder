"""The suppression list: addresses we never email again. Services flush; callers commit.

An address is suppressed when its owner unsubscribes, when mail to it hard-bounces, or when a
teammate decides so (``manual``). The exclusion check (``exclusions.check``) reads this list
first, so the gate refuses a suppressed recipient at drafting, approval and sending.

Unsubscribes are permanent: an admin may remove a ``bounced`` or ``manual`` row (a mailbox that
came back, a mistake), never an ``unsubscribed`` one. ``unsubscribed`` outranks every other
reason (ruling P6-S): an unsubscribe for an address already on the list as ``bounced`` or
``manual`` upgrades that row, so removing it can't lose the opt-out; no other reason ever
replaces an ``unsubscribed`` row.

Adding or removing a row records an audit row and, by default (``refresh=True``), refreshes the
stored addresses it covers (their ``blocked_reason`` and their companies' sendable counts) in
the same transaction, like the do-not-contact list.

Lock rule: that refresh (``contacts.service.refresh_blocks``) takes ``FOR UPDATE`` on the
companies holding the address. Other transactions take a company lock and *then* lock sequence
rows implicitly, through foreign-key actions: the contacts check locks the company and deletes
an ``EmailAddress`` (``sequences.email_address_id ON DELETE SET NULL`` updates the sequence),
and profiling locks the company and deletes a ``Person`` (``sequences.person_id SET NULL``).
So a caller that holds message/sequence locks (``outreach.locks.lock_message``) or the
recipient advisory lock (``lock_recipient``) MUST pass ``refresh=False``, or it can deadlock
with them. Such a caller then runs ``refresh_blocks(email=...)`` after committing, in its own
short transaction holding no outreach locks. Skipping it is safe: the gate and
``exclusions.check`` read suppressions live, and the worker's Sweep step
(``refresh_suppressed_blocks``) brings any missed address up to date."""

import uuid

from sqlalchemy import Boolean, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.auth.emails import normalize_email
from salesfinder.contacts.service import refresh_blocks
from salesfinder.db.models import EmailAddress, Suppression, User
from salesfinder.db.models.outreach import SUPPRESSION_REASONS

__all__ = [
    "INVALID_ADDRESS",
    "NOT_FOUND",
    "UNSUBSCRIBE_PERMANENT",
    "count_suppressions",
    "is_suppressed",
    "list_suppressions",
    "refresh_suppressed_blocks",
    "remove",
    "suppress",
]

INVALID_ADDRESS = "Not a valid email address."
NOT_FOUND = "Suppression not found."
UNSUBSCRIBE_PERMANENT = "Unsubscribes can't be removed."
DETAIL_LIMIT = 300  # suppressions.detail column
UNSUBSCRIBED = "unsubscribed"  # outranks every other reason (P6-S)


def _normalized(address: str) -> str:
    try:
        return normalize_email(address)
    except ValueError:
        raise ValueError(INVALID_ADDRESS) from None


async def suppress(
    db: AsyncSession,
    address: str,
    *,
    reason: str,
    detail: str,
    message_id: uuid.UUID | None = None,
    actor: User | None = None,
    ip: str | None = None,
    refresh: bool = True,
) -> bool:
    """Put ``address`` (normalized) on the list. True if a row was written: a new row
    (audited ``suppression.added``) or, for ``reason="unsubscribed"``, an existing ``bounced``
    or ``manual`` row upgraded to ``unsubscribed`` with this detail and message id (audited
    ``suppression.upgraded`` with the previous reason; ruling P6-S). False if nothing changed:
    the address was already unsubscribed, or this is a lower reason for an address already on
    the list (its row is kept). Audit details never include the detail, which may quote server
    text. Safe under concurrency (``INSERT ... ON CONFLICT``). With ``refresh`` (the default) a
    written row refreshes the stored addresses it covers; pass ``refresh=False`` while holding
    any ``outreach.locks`` lock (see the module docstring).

    ``ValueError`` for an unknown ``reason`` or an address that doesn't normalize (nothing is
    written: such an address can never be sent to anyway, the gate fails it closed)."""
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"Unknown suppression reason: {reason}")
    normalized = _normalized(address)
    clipped = detail.strip()[:DETAIL_LIMIT]
    previous: str | None = None
    if reason == UNSUBSCRIBED:
        # Lock the existing row (if any) to learn the reason being replaced.
        previous = await db.scalar(
            select(Suppression.reason).where(Suppression.address == normalized).with_for_update()
        )
        if previous == UNSUBSCRIBED:
            return False
    stmt = insert(Suppression).values(
        id=uuid.uuid4(),
        address=normalized,
        reason=reason,
        detail=clipped,
        message_id=message_id,
        created_by_id=actor.id if actor else None,
    )
    if reason == UNSUBSCRIBED:
        stmt = stmt.on_conflict_do_update(
            index_elements=[Suppression.address],
            set_={
                "reason": stmt.excluded.reason,
                "detail": stmt.excluded.detail,
                "message_id": stmt.excluded.message_id,
            },
            where=Suppression.reason != UNSUBSCRIBED,
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Suppression.address])
    written = (
        await db.execute(stmt.returning(Suppression.id, literal_column("xmax = 0", type_=Boolean)))
    ).first()
    if written is None:
        return False
    row_id: uuid.UUID = written[0]
    inserted: bool = written[1]
    if inserted:
        action = audit.SUPPRESSION_ADDED
        details: dict[str, str | None] = {"address": normalized, "reason": reason}
    else:
        action = audit.SUPPRESSION_UPGRADED
        details = {"address": normalized, "reason": reason, "previous_reason": previous}
    details["message_id"] = str(message_id) if message_id else None
    await audit.record(
        db,
        action=action,
        actor_id=actor.id if actor else None,
        target_type="suppression",
        target_id=str(row_id),
        details=details,
        ip=ip,
    )
    if refresh:
        await refresh_blocks(db, email=normalized)
    return True


async def is_suppressed(db: AsyncSession, address: str) -> Suppression | None:
    """The suppression row for ``address`` (normalized), or None. An address that doesn't
    normalize is never on the list (``exclusions.check`` fails it closed separately)."""
    try:
        normalized = normalize_email(address)
    except ValueError:
        return None
    return await db.scalar(
        select(Suppression)
        .where(Suppression.address == normalized)
        .execution_options(populate_existing=True)  # an upgrade is a Core write
    )


async def remove(
    db: AsyncSession,
    suppression_id: uuid.UUID,
    *,
    actor: User,
    ip: str | None = None,
    refresh: bool = True,
) -> Suppression:
    """Take a ``bounced`` or ``manual`` row off the list (audited ``suppression.removed``) and,
    with ``refresh`` (the default; pass False while holding ``outreach.locks`` locks), refresh
    the stored addresses it covered. ``LookupError`` if it is gone; ``ValueError``
    (``UNSUBSCRIBE_PERMANENT``) for an unsubscribe, which is never removed."""
    row = await db.scalar(
        select(Suppression)
        .where(Suppression.id == suppression_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise LookupError(NOT_FOUND)
    if row.reason == UNSUBSCRIBED:
        raise ValueError(UNSUBSCRIBE_PERMANENT)
    await db.delete(row)
    await db.flush()
    await audit.record(
        db,
        action=audit.SUPPRESSION_REMOVED,
        actor_id=actor.id,
        target_type="suppression",
        target_id=str(suppression_id),
        details={"address": row.address, "reason": row.reason},
        ip=ip,
    )
    if refresh:
        await refresh_blocks(db, email=row.address)
    return row


async def list_suppressions(
    db: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[Suppression]:
    """Newest first."""
    rows = await db.scalars(
        select(Suppression)
        .order_by(Suppression.created_at.desc(), Suppression.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows)


async def count_suppressions(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(Suppression)) or 0)


async def refresh_suppressed_blocks(db: AsyncSession, *, limit: int = 500) -> int:
    """Self-heal for ``suppress(refresh=False)``: refresh the stored addresses that are
    suppressed but not yet blocked (``blocked_reason`` NULL), at most ``limit`` distinct
    addresses per call. Returns how many stored addresses changed. Takes company locks: call it
    holding no ``outreach.locks`` lock (the worker's Sweep, its own transaction). Flushes."""
    addresses = list(
        (
            await db.scalars(
                select(EmailAddress.address)
                .join(Suppression, Suppression.address == EmailAddress.address)
                .where(EmailAddress.blocked_reason.is_(None))
                .distinct()
                .order_by(EmailAddress.address)
                .limit(limit)
            )
        ).all()
    )
    changed = 0
    for address in addresses:
        changed += await refresh_blocks(db, email=address)
    return changed
