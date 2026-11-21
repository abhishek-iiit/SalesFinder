"""The exclusion check: is this company/contact off-limits, and why?

Precedence for an email address: an address that doesn't normalize (fails closed), then the
suppression list (unsubscribed, bounced, manual), then do-not-contact (address, then domain),
then known relationships (address, then domain; a free-mail domain never matches by domain).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.auth.emails import normalize_email
from salesfinder.db.models import DoNotContact, KnownRelationship, Suppression
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email, normalize_domain

__all__ = [
    "FREE_MAIL_DOMAINS",
    "INVALID_ADDRESS",
    "Exclusion",
    "exclusion_for",
    "exclusions_for_emails",
    "suppressed_detail",
]

INVALID_ADDRESS = "Not a valid email address"


@dataclass(frozen=True)
class Exclusion:
    reason: Literal["existing_relationship", "do_not_contact", "suppressed", "invalid_address"]
    detail: str


def suppressed_detail(row: Suppression) -> str:
    """The exclusion detail for a suppression: ``Unsubscribed on 2026-11-13``, ``Bounced on
    2026-11-13`` (UTC dates) or ``Suppressed: <detail>`` for a teammate's (manual) one."""
    day = f"{row.created_at.astimezone(UTC):%Y-%m-%d}"
    if row.reason == "unsubscribed":
        return f"Unsubscribed on {day}"
    if row.reason == "bounced":
        return f"Bounced on {day}"
    return f"Suppressed: {row.detail}"


def _suppressed(row: Suppression) -> Exclusion:
    return Exclusion("suppressed", suppressed_detail(row))


def _invalid() -> Exclusion:
    return Exclusion("invalid_address", INVALID_ADDRESS)


def _known_detail(row: KnownRelationship) -> str:
    what = row.name or row.value
    obj = "company" if row.hubspot_object == "company" else "contact"
    owner = f" (owner: {row.owner_name})" if row.owner_name else ""
    return f"HubSpot {obj} {what}{owner}"


async def _dnc(db: AsyncSession, kind: str, value: str) -> Exclusion | None:
    row = await db.scalar(
        select(DoNotContact).where(DoNotContact.kind == kind, DoNotContact.value == value)
    )
    if row is None:
        return None
    return Exclusion("do_not_contact", f"Do-not-contact: {row.value} — {row.reason}")


async def _known(db: AsyncSession, kind: str, value: str) -> Exclusion | None:
    row = await db.scalar(
        select(KnownRelationship)
        .where(KnownRelationship.kind == kind, KnownRelationship.value == value)
        .order_by(KnownRelationship.created_at)
        .limit(1)
    )
    if row is None:
        return None
    return Exclusion("existing_relationship", _known_detail(row))


async def exclusion_for(
    db: AsyncSession, *, domain: str | None = None, email: str | None = None
) -> Exclusion | None:
    """Why ``email`` (and/or the company ``domain``) is off-limits, or None. Fails closed: an
    ``email`` that is given but doesn't normalize (including "") is ``invalid_address``."""
    norm_email: str | None = None
    if email is not None:
        try:
            norm_email = normalize_email(email)
        except ValueError:
            return _invalid()
        row = await db.scalar(select(Suppression).where(Suppression.address == norm_email))
        if row is not None:
            return _suppressed(row)
    domains: list[str] = []
    email_domain = domain_of_email(norm_email) if norm_email else None
    for candidate in (normalize_domain(domain) if domain else None, email_domain):
        if candidate and candidate not in domains:
            domains.append(candidate)

    if norm_email and (hit := await _dnc(db, "email", norm_email)):
        return hit
    for d in domains:
        if hit := await _dnc(db, "domain", d):
            return hit
    if norm_email and (hit := await _known(db, "email", norm_email)):
        return hit
    for d in domains:
        if d == email_domain and d != (normalize_domain(domain) if domain else None):
            if d in FREE_MAIL_DOMAINS:
                continue
        if hit := await _known(db, "domain", d):
            return hit
    return None


_CHUNK = 1000  # values per IN (...) list


async def exclusions_for_emails(
    db: AsyncSession, emails: Sequence[str]
) -> dict[str, Exclusion | None]:
    """``exclusion_for(email=e)`` for many addresses in a fixed number of queries (three per
    ``_CHUNK`` distinct values): the same precedence and the same detail strings."""
    norm: dict[str, str | None] = {}
    for email in emails:
        try:
            norm[email] = normalize_email(email)
        except ValueError:
            norm[email] = None
    addresses = {n for n in norm.values() if n}
    domains = {d for a in addresses if (d := domain_of_email(a))}
    keys = [("email", a) for a in sorted(addresses)] + [("domain", d) for d in sorted(domains)]

    dnc: dict[tuple[str, str], DoNotContact] = {}
    known: dict[tuple[str, str], KnownRelationship] = {}
    suppressed: dict[str, Suppression] = {}
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start : start + _CHUNK]
        values = [v for _, v in chunk]
        chunk_addresses = [v for kind, v in chunk if kind == "email"]
        if chunk_addresses:
            for sup_row in await db.scalars(
                select(Suppression).where(Suppression.address.in_(chunk_addresses))
            ):
                suppressed[sup_row.address] = sup_row
        for row in await db.scalars(
            select(DoNotContact)
            .where(DoNotContact.value.in_(values))
            .order_by(DoNotContact.created_at)
        ):
            dnc.setdefault((row.kind, row.value), row)
        for kr in await db.scalars(
            select(KnownRelationship)
            .where(KnownRelationship.value.in_(values))
            .order_by(KnownRelationship.created_at)
        ):
            known.setdefault((kr.kind, kr.value), kr)

    out: dict[str, Exclusion | None] = {}
    for email, address in norm.items():
        out[email] = None
        if address is None:
            out[email] = _invalid()
            continue
        if (hit := suppressed.get(address)) is not None:
            out[email] = _suppressed(hit)
            continue
        host = domain_of_email(address)
        dnc_row = dnc.get(("email", address)) or (dnc.get(("domain", host)) if host else None)
        if dnc_row is not None:
            detail = f"Do-not-contact: {dnc_row.value} — {dnc_row.reason}"
            out[email] = Exclusion("do_not_contact", detail)
            continue
        known_row = known.get(("email", address))
        if known_row is None and host and host not in FREE_MAIL_DOMAINS:
            known_row = known.get(("domain", host))
        if known_row is not None:
            out[email] = Exclusion("existing_relationship", _known_detail(known_row))
    return out
