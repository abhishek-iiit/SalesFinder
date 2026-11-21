"""The companies CSV export (Settings → Export, admins only): one row per company with its
fit, up to ``contacts.max_targets_per_company`` contacts, and where outreach stands.

What it leaves out is re-checked live, never trusted from stored flags alone:

- a company that is ``excluded``, ``disqualified``, whose domain is on the do-not-contact list
  or known to HubSpot (``NOT EXISTS`` against the lists, as in ``exclusions.sweep``, so a
  company the sweep hasn't reached yet is still left out), or that has **any** suppressed
  address (one of its stored addresses or a recipient of one of its sequences);
- a contact any of whose linked addresses ``exclusions.check.exclusions_for_emails`` flags
  (the batched form of the check the gate runs at drafting, approval and sending; Ruling
  P7-E), except as ``invalid_address`` (an address that doesn't normalize says nothing about
  the person). A contact's shown email is never one the gate refuses outright (invalid,
  unparseable, no-reply, "don't send").

The whole read runs in one REPEATABLE READ transaction, so the company filter and every batch
lookup see one snapshot.

The file is meant for Excel: UTF-8 with a BOM, CRLF line ends, and every cell passed through
``csv_cell``, which neutralizes spreadsheet formulas (scraped company names and titles are
untrusted text). Read only; the caller audits (in the same transaction) and commits."""

import csv
import io
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.campaigns.service import address_preference
from salesfinder.catalog import Catalog
from salesfinder.config import AppConfig
from salesfinder.contacts.service import (
    NOT_A_MAILBOX,
    OVERRIDDEN_OFF,
    WONT_DELIVER,
    unsendable_reason,
)
from salesfinder.db.models import (
    Company,
    Conversation,
    DoNotContact,
    EmailAddress,
    KnownRelationship,
    Message,
    Person,
    Suppression,
)
from salesfinder.db.models import Sequence as OutreachSequence
from salesfinder.exclusions.check import exclusions_for_emails
from salesfinder.signals.catalog import SIGNALS
from salesfinder.signals.format import format_value
from salesfinder.signals.latest import latest_values
from salesfinder.signals.settings import confirmed_codes

__all__ = [
    "COLUMNS",
    "SIDES",
    "ExportSide",
    "build_csv",
    "columns_for",
    "company_rows",
    "csv_cell",
    "iter_company_rows",
    "to_csv",
]

ExportSide = Literal["buyer", "supplier", "all"]
SIDES: tuple[ExportSide, ...] = ("buyer", "supplier", "all")

BOM = "﻿"
# Companies per batch: the per-batch lookups (people, addresses, sequences, conversations,
# signals, exclusions) use IN lists of at most this many ids.
BATCH = 500

_HEAD = (
    "side",
    "company",
    "domain",
    "website",
    "country",
    "region",
    "score",
    "fit_reasons",
    "profile_status",
)
_CONTACT_FIELDS = ("name", "title", "email", "email_status")
_TAIL = ("outreach_status", "last_sent_at", "reply_intent")


def columns_for(max_targets: int) -> tuple[str, ...]:
    """The header for ``max_targets`` contact slots."""
    contacts = tuple(
        f"contact_{n}_{field}" for n in range(1, max_targets + 1) for field in _CONTACT_FIELDS
    )
    return _HEAD + contacts + _TAIL


COLUMNS = columns_for(2)  # the header with the default contacts.max_targets_per_company

# A spreadsheet treats a cell starting with one of these as a formula (or, for TAB/CR, may
# drop the character and then read what follows as one). Full-width forms are included: some
# spreadsheet apps normalize them to their ASCII equivalents.
_FORMULA_START = frozenset("=+-@\t\r\uff1d\uff0b\uff0d\uff20")  # + full-width = + - @
# Line and tab breaks a spreadsheet may honour inside a cell: all become a space.
_LINE_BREAKS = str.maketrans(dict.fromkeys("\r\n\t\x0b\x0c\x85\u2028\u2029", " "))
# Excel in locales whose list separator is ";" splits a CSV line on ";" too, so a formula can
# also start right after one (after optional whitespace).
_AFTER_SEMICOLON = re.compile(r";(\s*)(?=[=+\-@\uff1d\uff0b\uff0d\uff20])")


def csv_cell(value: object) -> str:
    """One cell's text, safe to open in a spreadsheet: None is empty; CR, LF and TAB become
    spaces (so do VT, FF, NEL, U+2028 and U+2029); a value starting with ``=``, ``+``, ``-``,
    ``@``, TAB or CR (or any of those after leading whitespace, which some spreadsheet apps
    trim; full-width forms too) gets a leading ``'`` so it is shown as text, never evaluated,
    and so does any such start right after a ``;`` (a list separator in some Excel locales)."""
    if value is None:
        return ""
    raw = value if isinstance(value, str) else str(value)
    cell = _AFTER_SEMICOLON.sub(r";\1'", raw.translate(_LINE_BREAKS))
    if raw[:1] in _FORMULA_START or cell.lstrip()[:1] in _FORMULA_START:
        return "'" + cell
    return cell


def _write(writer: Any, rows: Iterable[dict[str, str]], columns: Sequence[str]) -> int:
    count = 0
    for row in rows:
        writer.writerow([csv_cell(row.get(col)) for col in columns])
        count += 1
    return count


def _start(columns: Sequence[str]) -> tuple[io.StringIO, Any]:
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([csv_cell(col) for col in columns])
    return buffer, writer


def to_csv(rows: Iterable[dict[str, str]], *, columns: Sequence[str] = COLUMNS) -> str:
    """The file: a UTF-8 BOM, the header, then one CRLF-terminated line per row, every cell
    through ``csv_cell``."""
    buffer, writer = _start(columns)
    _write(writer, rows, columns)
    return buffer.getvalue()


async def build_csv(
    db: AsyncSession, *, side: ExportSide, config: AppConfig, catalog: Catalog
) -> tuple[str, int]:
    """The export file for ``side`` and how many companies it lists, written batch by batch
    (only one batch of row dicts is held at a time)."""
    columns = columns_for(config.contacts.max_targets_per_company)
    buffer, writer = _start(columns)
    count = 0
    async for batch in iter_company_rows(db, side=side, config=config, catalog=catalog):
        count += _write(writer, batch, columns)
    return buffer.getvalue(), count


async def company_rows(
    db: AsyncSession, *, side: ExportSide, config: AppConfig, catalog: Catalog
) -> list[dict[str, str]]:
    """Every exported row for ``side``, ordered by side, then score (highest first, unscored
    last), then domain. Keys are ``columns_for(config.contacts.max_targets_per_company)``."""
    rows: list[dict[str, str]] = []
    async for batch in iter_company_rows(db, side=side, config=config, catalog=catalog):
        rows.extend(batch)
    return rows


# --------------------------------------------------------------------------- queries


def _companies_stmt(side: ExportSide) -> Any:
    dnc_domain = exists().where(DoNotContact.kind == "domain", DoNotContact.value == Company.domain)
    known_domain = exists().where(
        KnownRelationship.kind == "domain", KnownRelationship.value == Company.domain
    )
    suppressed_address = exists().where(
        EmailAddress.company_id == Company.id,
        exists().where(Suppression.address == EmailAddress.address),
    )
    suppressed_recipient = exists().where(
        OutreachSequence.company_id == Company.id,
        exists().where(Suppression.address == OutreachSequence.recipient),
    )
    stmt = select(
        Company.id,
        Company.side,
        Company.name,
        Company.domain,
        Company.website_url,
        Company.country,
        Company.region,
        Company.score,
        Company.profile_status,
    ).where(
        Company.status != "excluded",
        Company.disqualified.is_(False),
        ~dnc_domain,
        ~known_domain,
        ~suppressed_address,
        ~suppressed_recipient,
    )
    if side != "all":
        stmt = stmt.where(Company.side == side)
    return stmt.order_by(
        Company.side, Company.score.desc().nulls_last(), Company.domain, Company.id
    )


# Why the gate refuses an address in a way no exclusion check or override changes: it doesn't
# exist, no person reads it (no-reply, postmaster, ...), or a teammate marked it "don't send".
# Such an address is never handed out as a contact's email.
_NEVER_SHOWN = frozenset({WONT_DELIVER, NOT_A_MAILBOX, OVERRIDDEN_OFF})


def _shown(addr: EmailAddress) -> bool:
    # The MX state is left out (stated as "has_mx"): it never makes an address unfit to list.
    return unsendable_reason(addr, mx_state="has_mx") not in _NEVER_SHOWN


async def _contacts(
    db: AsyncSession, ids: list[uuid.UUID], limit: int
) -> dict[uuid.UUID, list[tuple[Person, EmailAddress | None]]]:
    """company id -> up to ``limit`` (target, address to show) pairs in rank order.

    A target is left out when ANY of their linked addresses (invalid ones included) is
    suppressed, on do-not-contact or known to HubSpot (Ruling P7-E): an opt-out must never
    reach sales by name. An address that doesn't even normalize (``invalid_address``) says
    nothing about the person, so it never drops them, and it is never shown. Otherwise the
    address shown is their best one by ``address_preference`` that the gate doesn't refuse
    outright (``_shown``), or none."""
    people = (
        await db.scalars(
            select(Person)
            .where(Person.company_id.in_(ids), Person.is_target)
            .order_by(
                Person.company_id, Person.rank.asc().nulls_last(), Person.full_name, Person.id
            )
        )
    ).all()
    addresses = (
        await db.scalars(
            select(EmailAddress).where(
                EmailAddress.company_id.in_(ids),
                EmailAddress.person_id.in_([p.id for p in people]),
            )
        )
    ).all()
    by_person: dict[uuid.UUID, list[EmailAddress]] = defaultdict(list)
    for addr in addresses:
        if addr.person_id is not None:
            by_person[addr.person_id].append(addr)
    # Suppressions first, then do-not-contact, then HubSpot (the gate's precedence).
    flagged = await exclusions_for_emails(db, [a.address for a in addresses])
    out: dict[uuid.UUID, list[tuple[Person, EmailAddress | None]]] = defaultdict(list)
    for person in people:
        linked = by_person.get(person.id, [])
        reasons = [flag.reason for a in linked if (flag := flagged.get(a.address)) is not None]
        if any(reason != "invalid_address" for reason in reasons):
            continue
        # Any flag left is ``invalid_address``: such an address is never shown.
        usable = sorted(
            (a for a in linked if flagged.get(a.address) is None and _shown(a)),
            key=lambda a: (address_preference(a), a.address),
        )
        mine = out[person.company_id]
        if len(mine) < limit:
            mine.append((person, usable[0] if usable else None))
    return out


async def _outreach(
    db: AsyncSession, ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, datetime | None], dict[uuid.UUID, str]]:
    """(newest sequence status, last sent at, newest conversation intent) per company id."""
    newest_status = dict(
        (
            await db.execute(
                select(OutreachSequence.company_id, OutreachSequence.status)
                .where(OutreachSequence.company_id.in_(ids))
                .ext(distinct_on(OutreachSequence.company_id))
                .order_by(
                    OutreachSequence.company_id,
                    OutreachSequence.created_at.desc(),
                    OutreachSequence.id.desc(),
                )
            )
        ).all()
    )
    last_sent = dict(
        (
            await db.execute(
                select(OutreachSequence.company_id, func.max(Message.sent_at))
                .join(Message, Message.sequence_id == OutreachSequence.id)
                .where(OutreachSequence.company_id.in_(ids), Message.sent_at.is_not(None))
                .group_by(OutreachSequence.company_id)
            )
        ).all()
    )
    intents = (
        await db.execute(
            select(OutreachSequence.company_id, Conversation.intent)
            .join(Conversation, Conversation.sequence_id == OutreachSequence.id)
            .where(OutreachSequence.company_id.in_(ids))
            .ext(distinct_on(OutreachSequence.company_id))
            .order_by(
                OutreachSequence.company_id,
                Conversation.created_at.desc(),
                Conversation.id.desc(),
            )
        )
    ).all()
    newest_intent = {company_id: intent or "" for company_id, intent in intents}
    return newest_status, last_sent, newest_intent


async def _fit_codes(db: AsyncSession, side: ExportSide) -> dict[str, list[tuple[str, str]]]:
    """side -> its confirmed signals as (code, label), in catalog order."""
    confirmed = await confirmed_codes(db, None if side == "all" else side)
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for signal in SIGNALS:
        if signal.code in confirmed:
            out[signal.side].append((signal.code, signal.label))
    return out


SNAPSHOT_NEEDS_FRESH_TRANSACTION = "Start the export with no transaction open on the session."


async def _begin_snapshot(db: AsyncSession) -> None:
    """Begin the session's transaction at REPEATABLE READ, so every query of the export (the
    company filter, then each batch's lookups) reads one snapshot. The caller's audit insert
    and commit then end it; SQLAlchemy resets the level when the connection is released.
    ``RuntimeError`` if a transaction is already open (its level can't change mid-way)."""
    if db.in_transaction():
        raise RuntimeError(SNAPSHOT_NEEDS_FRESH_TRANSACTION)
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _iso(at: datetime | None) -> str:
    return at.astimezone(UTC).isoformat(timespec="seconds") if at is not None else ""


async def iter_company_rows(
    db: AsyncSession, *, side: ExportSide, config: AppConfig, catalog: Catalog
) -> AsyncIterator[list[dict[str, str]]]:
    """``company_rows`` in batches of at most ``BATCH`` rows: the companies are read in one
    query (one snapshot of the filters), the rest in a fixed number of queries per batch."""
    if side not in SIDES:
        raise ValueError(f"Unknown export side: {side!r}")
    await _begin_snapshot(db)
    limit = config.contacts.max_targets_per_company
    fit_codes = await _fit_codes(db, side)
    all_codes = {code for codes in fit_codes.values() for code, _ in codes}
    companies = (await db.execute(_companies_stmt(side))).all()
    for start in range(0, len(companies), BATCH):
        chunk = companies[start : start + BATCH]
        ids = [c.id for c in chunk]
        contacts = await _contacts(db, ids, limit)
        status, last_sent, intent = await _outreach(db, ids)
        latest = await latest_values(db, all_codes, ids) if all_codes else {}
        batch: list[dict[str, str]] = []
        for c in chunk:
            values = latest.get(c.id, {})
            reasons = [
                f"{label}: {format_value(code, values[code], catalog)}"
                for code, label in fit_codes.get(c.side, [])
                if code in values
            ]
            row = {
                "side": c.side,
                "company": c.name,
                "domain": c.domain,
                "website": c.website_url,
                "country": c.country or "",
                "region": c.region or "",
                "score": "" if c.score is None else str(c.score),
                "fit_reasons": "; ".join(reasons),
                "profile_status": c.profile_status,
            }
            mine = contacts.get(c.id, [])
            for n in range(1, limit + 1):
                person, addr = mine[n - 1] if n <= len(mine) else (None, None)
                row[f"contact_{n}_name"] = person.full_name if person else ""
                row[f"contact_{n}_title"] = (person.title or "") if person else ""
                row[f"contact_{n}_email"] = addr.address if addr else ""
                row[f"contact_{n}_email_status"] = addr.status if addr else ""
            row["outreach_status"] = status.get(c.id, "")
            row["last_sent_at"] = _iso(last_sent.get(c.id))
            row["reply_intent"] = intent.get(c.id, "")
            batch.append(row)
        yield batch
