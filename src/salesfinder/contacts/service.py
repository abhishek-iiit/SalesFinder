"""People and email addresses: store what profiling found, verify (MX, learned pattern, optional
paid verifier) and decide which addresses may be sent to (spec §6.2.1).

``store_extraction``, ``set_override`` and ``refresh_blocks`` are ordinary services: they flush,
callers commit.
``verify_company`` and ``verify_batch`` are worker jobs and own their transactions: DNS and
verifier calls happen outside any transaction, and the final write is one transaction with the
company row locked and its exclusion re-checked. No SMTP, ever: DNS MX lookups only.
"""

import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, case, delete, not_, null, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.config import AppConfig
from salesfinder.contacts.dns import (
    INVALID_DOMAIN,
    UNKNOWN_RETRY_AFTER,
    MxChecker,
    mx_domain,
    mx_for,
)
from salesfinder.contacts.extract import NEVER_SEND_LOCALS, EmailFound, PersonFound
from salesfinder.contacts.names import name_key, split_name
from salesfinder.contacts.patterns import Candidate, LearnedPattern, candidates_for, learn_pattern
from salesfinder.contacts.titles import rank_people
from salesfinder.contacts.verifier import Verifier, VerifyResult, daily_count
from salesfinder.db.models import (
    Company,
    DomainMx,
    EmailAddress,
    EmailPattern,
    Person,
    Suppression,
    User,
)
from salesfinder.db.models.contacts import TITLE_CATEGORIES
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email
from salesfinder.exclusions.check import exclusion_for, exclusions_for_emails

__all__ = [
    "DNS_UNAVAILABLE",
    "EXCLUDED",
    "Verifier",
    "VerifyOutcome",
    "can_allow",
    "drop_pattern",
    "is_sendable",
    "lock_bounce_companies",
    "mark_invalid",
    "mx_state_of",
    "refresh_blocks",
    "set_override",
    "store_extraction",
    "unsendable_reason",
    "verify_batch",
    "verify_company",
]

log = structlog.get_logger(__name__)

DNS_UNAVAILABLE = "DNS unavailable"
EXCLUDED = "Company is excluded."
MAX_CRASH_FRAMES = 12
# A check that crashed is due again after this (not after recheck_after_days).
CRASH_RETRY = timedelta(hours=1)

# Column limits (db/models/contacts.py, companies.contact_form_url).
_MAX_NAME = 200
_MAX_FIRST_LAST = 100
_MAX_TITLE = 200
_MAX_URL = 2048
_MAX_SNIPPET = 300
_MAX_ADDRESS = 320
_MAX_REASON = 300

# Sendable by default (given MX and no exclusion); held unless overridden; never.
DEFAULT_SENDABLE = frozenset({"published", "pattern_confirmed", "verified"})
HELD = {"pattern_guess": "Held: guess", "risky": "Held: risky (catch-all)"}
WONT_DELIVER = "Won't deliver"
OVERRIDDEN_OFF = "Overridden: don't send"
NO_MAIL_SERVER = "No mail server"
MX_NOT_CHECKED = "Mail server not checked yet"
NOT_A_MAILBOX = "Not a person's mailbox"
NO_LONGER_ON_SITE = "No longer on their site"
# What an "allow sending" override lifts: a guess, a catch-all, an address the site dropped.
LIFTABLE = frozenset({*HELD.values(), NO_LONGER_ON_SITE})
# Published addresses stored per company, per profile (the first ones, in page order).
MAX_PUBLISHED = 50
# The paid verifier is not called again in a batch after this many failed calls in a row.
MAX_VERIFIER_FAILURES = 3

# What a stored address is before MX / verifier results, by where it came from.
_BASE_STATUS = {"page": "published", "pattern": "pattern_confirmed", "guess": "pattern_guess"}
# Verifier answers that settle an address (never asked again); "unknown" is retried.
_DEFINITE = frozenset({"verified", "risky", "invalid"})
_TO_VERIFY = frozenset({"pattern_confirmed", "pattern_guess"})


@dataclass(frozen=True)
class VerifyOutcome:
    company_id: uuid.UUID
    targets: int  # people marked as targets
    addresses: int  # addresses stored for the company
    sendable: int  # targets with at least one sendable address
    mx_state: str
    error: str | None


def _clip(value: str | None, limit: int) -> str | None:
    """None stays None; NUL characters (rejected by Postgres) dropped; cut to ``limit``."""
    if value is None:
        return None
    text = value.replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_free(address: str) -> bool:
    return domain_of_email(address) in FREE_MAIL_DOMAINS


# --- the sendable rule (single source of truth) ---------------------------------------------------


def _never_send(address: str) -> bool:
    local = address.rpartition("@")[0].split("+", 1)[0]
    return local in NEVER_SEND_LOCALS


def _reason(addr: EmailAddress, mx_state: str, override: bool | None) -> str | None:
    if addr.status == "invalid":
        return WONT_DELIVER
    if _never_send(addr.address):
        return NOT_A_MAILBOX
    if override is False:
        return OVERRIDDEN_OFF
    if addr.blocked_reason is not None:
        return addr.blocked_reason
    if addr.stale and override is not True:
        return NO_LONGER_ON_SITE
    if not _is_free(addr.address) and mx_state != "has_mx":
        return NO_MAIL_SERVER if mx_state == "no_mail" else MX_NOT_CHECKED
    if addr.status in DEFAULT_SENDABLE:
        return None
    if addr.status in HELD:
        return None if override is True else HELD[addr.status]
    return f"Unknown status {addr.status}"


def unsendable_reason(addr: EmailAddress, *, mx_state: str) -> str | None:
    """Why ``addr`` may not be sent to, or None if it may (spec §6.2.1), as of the last refresh
    of its status and ``blocked_reason`` (sending re-checks the exclusion lists live).

    ``invalid`` never; a no-reply/postmaster-style mailbox never; a "don't send" override
    never; an exclusion never; a published address the site no longer shows only with an
    override; the company domain must have MX (a free-mail address always has); then
    ``published`` / ``pattern_confirmed`` / ``verified`` are sendable, and ``pattern_guess`` /
    ``risky`` only with an override."""
    return _reason(addr, mx_state, addr.override_sendable)


def is_sendable(addr: EmailAddress, *, mx_state: str) -> bool:
    return unsendable_reason(addr, mx_state=mx_state) is None


def can_allow(addr: EmailAddress) -> bool:
    """Whether an "allow sending" override would lift what holds ``addr`` back: it is a guess,
    a catch-all or no longer on the site, and nothing an override can't lift applies. The
    domain's MX state is left out: an allowed address is sent once the domain has MX."""
    if addr.override_sendable is True:
        return False
    return _reason(addr, "has_mx", None) in LIFTABLE


async def mx_state_of(db: AsyncSession, domain: str) -> str:
    """The cached MX state of ``domain`` (``unknown`` if never looked up)."""
    name = mx_domain(domain)
    if name is None:
        return "unknown"
    state = await db.scalar(select(DomainMx.state).where(DomainMx.domain == name))
    return state or "unknown"


async def _recount(db: AsyncSession, company: Company, *, mx_state: str) -> tuple[int, int, int]:
    """Set ``contacts_found`` (targets) and ``sendable_contacts`` (targets with a sendable
    address). Returns (targets, addresses, sendable)."""
    targets = set(
        (
            await db.scalars(
                select(Person.id).where(Person.company_id == company.id, Person.is_target)
            )
        ).all()
    )
    rows = (
        await db.scalars(select(EmailAddress).where(EmailAddress.company_id == company.id))
    ).all()
    sendable = {
        r.person_id for r in rows if r.person_id in targets and is_sendable(r, mx_state=mx_state)
    }
    company.contacts_found = len(targets)
    company.sendable_contacts = len(sendable)
    return len(targets), len(rows), len(sendable)


# --- storing what profiling found -----------------------------------------------------------------


def _found(person: Person) -> PersonFound:
    return PersonFound(
        full_name=person.full_name,
        title=person.title,
        category=person.title_category,
        url=person.source_url,
        snippet=person.source_snippet or "",
    )


@dataclass(frozen=True)
class _Rankable:
    key: str
    category: str


def _published_links(
    people: Sequence[PersonFound],
    ids: Sequence[uuid.UUID],
    emails: Sequence[EmailFound],
    domain: str,
) -> dict[str, uuid.UUID]:
    """address -> person for published personal addresses that one stored person's name renders
    under some pattern, unambiguously among all of them (the patterns module's rule)."""
    personal = [e for e in emails if e.kind == "personal"]
    domains = [domain]
    for e in personal:
        host = domain_of_email(e.address)
        if host in FREE_MAIL_DOMAINS and host not in domains:
            domains.append(host)
    links: dict[str, uuid.UUID] = {}
    for person, person_id in zip(people, ids, strict=True):
        for host in domains:
            for c in candidates_for(
                person, domain=host, emails=personal, learned=None, people=people
            ):
                if c.status == "published":
                    links.setdefault(c.address, person_id)
    return links


async def store_extraction(
    db: AsyncSession,
    company: Company,
    people: Sequence[PersonFound],
    emails: Sequence[EmailFound],
    form_url: str | None,
    *,
    side: str,
    config: AppConfig,
    now: datetime,
    partial: bool = False,
) -> None:
    """Store a profile's people and published addresses for ``company`` (locked by the caller).

    People are upserted by name key and ranked (at most ``max_targets_per_company`` targets).
    A complete profile drops people no longer on the site, except one with an overridden
    address; a ``partial`` one drops nothing. Published addresses (the first ``MAX_PUBLISHED``)
    are upserted and stamped ``last_seen_at = now``. They are never deleted (a teammate may
    rely on one), but a complete profile marks those it no longer saw ``stale``: unlinked from
    their person, no longer pattern evidence, and unsendable unless a teammate allows them. A
    ``partial`` profile marks nothing stale. Pattern/guess rows wait for the re-check, which is
    made due (``contacts_checked_at = NULL``). Flushes."""
    existing = {
        p.name_key: p
        for p in (await db.scalars(select(Person).where(Person.company_id == company.id))).all()
    }
    seen: dict[str, PersonFound] = {}
    for p in people:
        key = _clip(name_key(p.full_name), _MAX_NAME) or ""
        if key and key not in seen:
            seen[key] = p

    if not partial:
        stale = [row for key, row in existing.items() if key not in seen]
        protected: set[uuid.UUID | None] = set()
        if stale:
            protected = set(
                (
                    await db.scalars(
                        select(EmailAddress.person_id).where(
                            EmailAddress.person_id.in_([r.id for r in stale]),
                            EmailAddress.override_sendable.is_not(None),
                        )
                    )
                ).all()
            )
        for row in stale:
            if row.id not in protected:
                await db.delete(row)
                del existing[row.name_key]

    for key, p in seen.items():
        person = existing.get(key)
        if person is None:
            person = Person(company_id=company.id, name_key=key)
            db.add(person)
            existing[key] = person
        first, last = split_name(p.full_name)
        person.full_name = _clip(p.full_name, _MAX_NAME) or key
        person.first_name = _clip(first, _MAX_FIRST_LAST)
        person.last_name = _clip(last, _MAX_FIRST_LAST)
        person.title = _clip(p.title, _MAX_TITLE)
        person.title_category = p.category if p.category in TITLE_CATEGORIES else "other"
        person.source_url = _clip(p.url, _MAX_URL) or ""
        person.source_snippet = _clip(p.snippet, _MAX_SNIPPET)

    # Targets: among the people seen now, plus (partial) those kept from earlier reads.
    rankable = [_Rankable(k, existing[k].title_category) for k in seen]
    if partial:
        rankable += [_Rankable(k, r.title_category) for k, r in existing.items() if k not in seen]
    ranked = rank_people(rankable, side=side, limit=config.contacts.max_targets_per_company)
    rank = {r.key: i for i, r in enumerate(ranked, start=1)}
    for key, row in existing.items():
        row.rank = rank.get(key)
        row.is_target = key in rank
    await db.flush()  # person ids

    kept: dict[str, EmailFound] = {}
    for e in emails:
        address = e.address.strip().lower()
        if len(address) <= _MAX_ADDRESS and address not in kept:
            kept[address] = e
            if len(kept) >= MAX_PUBLISHED:
                break
    stored = list(existing.values())
    links = _published_links(
        [_found(p) for p in stored], [p.id for p in stored], list(kept.values()), company.domain
    )
    addresses = {
        e.address: e
        for e in (
            await db.scalars(select(EmailAddress).where(EmailAddress.company_id == company.id))
        ).all()
    }
    for address, e in kept.items():
        row_e = addresses.get(address)
        if row_e is None:
            row_e = EmailAddress(company_id=company.id, address=address)
            db.add(row_e)
            addresses[address] = row_e
        row_e.kind = e.kind
        row_e.source = "page"
        row_e.status = "published"  # MX is applied again by the re-check
        row_e.pattern = None
        row_e.person_id = links.get(address)
        row_e.evidence_url = _clip(e.url, _MAX_URL)
        row_e.evidence_snippet = _clip(e.snippet, _MAX_SNIPPET)
        row_e.last_seen_at = now
        row_e.stale = False
    if not partial:
        for address, row_e in addresses.items():
            if row_e.source == "page" and address not in kept:
                row_e.stale = True
                row_e.person_id = None

    # Excluded addresses are blocked at once, not only after the re-check.
    await _refresh_blocks(db, list(addresses.values()))
    if form_url is not None or not partial:
        company.contact_form_url = _clip(form_url, _MAX_URL)
    company.contacts_checked_at = None
    await db.flush()
    await _recount(db, company, mx_state=await mx_state_of(db, company.domain))
    await db.flush()


# --- verification ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    people: list[Person]
    emails: list[EmailAddress]
    learned: LearnedPattern | None
    candidates: dict[uuid.UUID, Candidate]  # target person id -> its candidate, in rank order


async def _plan(db: AsyncSession, company: Company) -> _Plan:
    """Learn the pattern from stored people + published personal addresses and pick each
    target's candidate. Pure apart from reading the rows."""
    people = list(
        (
            await db.scalars(
                select(Person)
                .where(Person.company_id == company.id)
                .order_by(Person.rank.asc().nulls_last(), Person.created_at, Person.id)
            )
        ).all()
    )
    emails = list(
        (
            await db.scalars(
                select(EmailAddress)
                .where(EmailAddress.company_id == company.id)
                .order_by(EmailAddress.created_at, EmailAddress.id)
            )
        ).all()
    )
    found = {p.id: _found(p) for p in people}
    everyone = list(found.values())
    # A published address the site no longer shows is no evidence of anything.
    published = [
        EmailFound(e.address, "personal", e.evidence_url or "", e.evidence_snippet or "")
        for e in emails
        if e.source == "page" and e.kind == "personal" and not e.stale
    ]
    learned = learn_pattern(company.domain, everyone, published)
    # Mail to an address made from this very pattern bounced (``drop_pattern``): what it gives
    # is only a guess from now on, whatever the site still suggests.
    dropped = learned is not None and await _pattern_bounced(db, company.domain, learned.pattern)
    has_published = {
        e.person_id for e in emails if e.source == "page" and not e.stale and e.person_id
    }
    candidates: dict[uuid.UUID, Candidate] = {}
    # Nobody's candidate is an address the site dropped (it stays held, unlinked).
    taken: set[str] = {e.address for e in emails if e.source == "page" and e.stale}
    for person in people:
        if not person.is_target:
            continue
        options = candidates_for(
            found[person.id],
            domain=company.domain,
            emails=published,
            learned=learned,
            people=everyone,
        )
        if not options:
            continue
        candidate = options[0]
        if dropped and candidate.source == "pattern":
            candidate = Candidate(candidate.address, "pattern_guess", "guess", candidate.pattern)
        # A published address (e.g. free mail) beats a pattern or a guess.
        if candidate.status != "published" and person.id in has_published:
            continue
        if candidate.address in taken or len(candidate.address) > _MAX_ADDRESS:
            continue
        taken.add(candidate.address)
        candidates[person.id] = candidate
    return _Plan(people, emails, learned, candidates)


def _derived_status(row: EmailAddress, mx_state: str) -> str:
    if mx_state == "no_mail" and not _is_free(row.address):
        return "invalid"
    if row.source != "page" and row.verifier_result in _DEFINITE:
        return row.verifier_result
    return _BASE_STATUS.get(row.source, row.status)


def _excluded(company_id: uuid.UUID) -> VerifyOutcome:
    return VerifyOutcome(company_id, 0, 0, 0, "unknown", EXCLUDED)


async def _locked_company(db: AsyncSession, company_id: uuid.UUID) -> Company | None:
    return await db.scalar(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _refresh_blocks(db: AsyncSession, rows: Sequence[EmailAddress]) -> None:
    hits = await exclusions_for_emails(db, [row.address for row in rows])
    for row in rows:
        hit = hits.get(row.address)
        row.blocked_reason = _clip(hit.detail, _MAX_REASON) if hit else None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def refresh_blocks(
    db: AsyncSession, *, domain: str | None = None, email: str | None = None
) -> int:
    """Bring stored addresses' ``blocked_reason`` in line with the exclusion lists now, and
    recount ``sendable_contacts`` for the companies whose addresses changed. Only addresses at
    ``domain`` (or a subdomain of it) and/or equal to ``email`` when given; every address
    otherwise. Call it in the transaction that changed a list (do-not-contact add/remove, a
    HubSpot sync). Returns how many addresses changed. Flushes; the caller commits.

    Affected companies are locked (in id order) before their addresses are written, like the
    contact check does, so the two can't deadlock or overwrite each other's counts."""
    match: list[Any] = []
    if domain:
        host = domain.strip().lower()
        match.append(EmailAddress.address.like(f"%@{_like_escape(host)}", escape="\\"))
        match.append(EmailAddress.address.like(f"%.{_like_escape(host)}", escape="\\"))
    if email:
        match.append(EmailAddress.address == email.strip().lower())
    where = or_(*match) if match else true()
    company_ids = list(
        (await db.scalars(select(EmailAddress.company_id).where(where).distinct())).all()
    )
    if not company_ids:
        return 0
    companies = (
        await db.scalars(
            select(Company)
            .where(Company.id.in_(company_ids))
            .order_by(Company.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    rows = (
        await db.scalars(
            select(EmailAddress)
            .where(where, EmailAddress.company_id.in_(company_ids))
            .execution_options(populate_existing=True)
        )
    ).all()
    hits = await exclusions_for_emails(db, [row.address for row in rows])
    changed: set[uuid.UUID] = set()
    count = 0
    for row in rows:
        hit = hits.get(row.address)
        reason = _clip(hit.detail, _MAX_REASON) if hit else None
        if reason != row.blocked_reason:
            row.blocked_reason = reason
            changed.add(row.company_id)
            count += 1
    await db.flush()
    for company in companies:
        if company.id in changed:
            await _recount(db, company, mx_state=await mx_state_of(db, company.domain))
    await db.flush()
    return count


async def _pattern_bounced(db: AsyncSession, domain: str, pattern: str) -> bool:
    """Whether ``pattern`` is the domain's stored pattern and mail made from it bounced."""
    bounced_at = await db.scalar(
        select(EmailPattern.bounced_at).where(
            EmailPattern.domain == domain, EmailPattern.pattern == pattern
        )
    )
    return bounced_at is not None


async def _save_pattern(
    db: AsyncSession, domain: str, learned: LearnedPattern | None, *, now: datetime
) -> None:
    """Store the learned pattern. A pattern whose mail bounced (``bounced_at``) is never
    re-confirmed: relearning the same pattern keeps ``bounced_at``, and the row isn't deleted
    when nothing is learned (so the bounce is remembered). A different pattern starts clean."""
    if learned is None:
        await db.execute(
            delete(EmailPattern).where(
                EmailPattern.domain == domain, EmailPattern.bounced_at.is_(None)
            )
        )
        return
    values = {
        "domain": domain,
        "pattern": learned.pattern,
        "support": learned.support,
        "examples": list(learned.examples),
        "learned_at": now,
    }
    stmt = insert(EmailPattern).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[EmailPattern.domain],
            set_={
                **{k: v for k, v in values.items() if k != "domain"},
                "bounced_at": case(
                    (EmailPattern.pattern == stmt.excluded.pattern, EmailPattern.bounced_at),
                    else_=null(),
                ),
            },
        )
    )


async def _lock_companies_at(db: AsyncSession, where: Any) -> list[Company]:
    """Lock (``FOR UPDATE``, id order, fresh) the companies holding addresses that match
    ``where``, as ``refresh_blocks`` and the contact check do."""
    ids = list((await db.scalars(select(EmailAddress.company_id).where(where).distinct())).all())
    if not ids:
        return []
    return list(
        (
            await db.scalars(
                select(Company)
                .where(Company.id.in_(ids))
                .order_by(Company.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def lock_bounce_companies(db: AsyncSession, address: str) -> None:
    """Lock, once and in id order, every company that a bounce of ``address`` may write: the
    holders of the address and the holders of its domain's pattern addresses (what
    ``mark_invalid``, ``drop_pattern`` and ``refresh_blocks`` then touch; their own locks are
    then already held). Holding no ``outreach.locks`` lock."""
    email = address.strip().lower()
    host = email.rpartition("@")[2]
    await _lock_companies_at(
        db,
        or_(
            EmailAddress.address == email,
            and_(
                EmailAddress.address.like(f"%@{_like_escape(host)}", escape="\\"),
                EmailAddress.source == "pattern",
            ),
        ),
    )


async def mark_invalid(db: AsyncSession, address: str) -> list[tuple[str, str]]:
    """Mark every stored copy of ``address`` (lowercase) ``invalid`` (mail to it hard-bounced)
    and recount its companies. Returns each copy's ``(source, status)`` as it was just before,
    so the caller can tell whether one came from a confirmed pattern.

    Takes company locks (like ``refresh_blocks``): call it holding no ``outreach.locks`` lock.
    Flushes; the caller commits."""
    where = EmailAddress.address == address.strip().lower()
    companies = await _lock_companies_at(db, where)
    if not companies:
        return []
    rows = list(
        (
            await db.scalars(
                select(EmailAddress).where(where).execution_options(populate_existing=True)
            )
        ).all()
    )
    before = [(row.source, row.status) for row in rows]
    for row in rows:
        row.status = "invalid"
    await db.flush()
    for company in companies:
        await _recount(db, company, mx_state=await mx_state_of(db, company.domain))
    await db.flush()
    return before


async def drop_pattern(db: AsyncSession, domain: str, *, now: datetime) -> int:
    """Mail to an address made from ``domain``'s learned pattern hard-bounced: the pattern is
    no longer trusted. Sets ``email_patterns.bounced_at`` (kept; ``_save_pattern`` never
    re-confirms it) and makes the domain's other pattern addresses guesses (``source='guess'``;
    a ``pattern_confirmed`` one becomes ``pattern_guess``, held unless a teammate allows it).
    Invalid addresses are left alone. Recounts the companies touched and returns how many
    addresses changed.

    Takes company locks (like ``refresh_blocks``): call it holding no ``outreach.locks`` lock.
    Flushes; the caller commits."""
    host = domain.strip().lower()
    await db.execute(
        update(EmailPattern)
        .where(EmailPattern.domain == host, EmailPattern.bounced_at.is_(None))
        .values(bounced_at=now)
    )
    where = and_(
        EmailAddress.address.like(f"%@{_like_escape(host)}", escape="\\"),
        EmailAddress.source == "pattern",
        EmailAddress.status != "invalid",
    )
    companies = await _lock_companies_at(db, where)
    if not companies:
        return 0
    rows = list(
        (
            await db.scalars(
                select(EmailAddress).where(where).execution_options(populate_existing=True)
            )
        ).all()
    )
    for row in rows:
        row.source = "guess"
        if row.status == "pattern_confirmed":
            row.status = "pattern_guess"
    await db.flush()
    for company in companies:
        await _recount(db, company, mx_state=await mx_state_of(db, company.domain))
    await db.flush()
    return len(rows)


async def verify_company(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    *,
    checker: MxChecker,
    verifier: Verifier | None,
    config: AppConfig,
    now: datetime,
) -> VerifyOutcome:
    """Check one company's addresses: MX for its domain, the learned pattern, one candidate per
    target, the optional verifier (within its daily cap; never for published addresses),
    exclusions, and the counts. When DNS can't answer nothing changes but the counts, and the
    company stays due (``contacts_checked_at = NULL``)."""
    async with db_factory() as db:
        company = await db.get(Company, company_id)
        if company is None:
            raise LookupError("Company not found.")
        if company.status == "excluded":
            return _excluded(company_id)
        domain = company.domain
        plan = await _plan(db, company)
        stored = {e.address: e for e in plan.emails}
        to_verify: list[str] = []
        for candidate in plan.candidates.values():
            row = stored.get(candidate.address)
            if candidate.status not in _TO_VERIFY or (
                row is not None
                and (
                    row.source == "page"
                    or row.verifier_result in _DEFINITE
                    or row.override_sendable is False
                )
            ):
                # Published on their site (never sent to the verifier), settled already, or a
                # teammate said never to send it.
                continue
            if await exclusion_for(db, email=candidate.address) is None:
                to_verify.append(candidate.address)
        used = await daily_count(db, now=now) if verifier is not None and to_verify else 0

    # Network: outside any transaction.
    mx = await mx_for(db_factory, checker, domain, now=now, config=config.contacts)
    results: dict[str, VerifyResult] = {}
    if verifier is not None and mx.state == "has_mx":
        budget = max(config.contacts.verifier_max_per_day - used, 0)
        for address in to_verify[:budget]:
            # The client spans the batch: after a run of failures (a bad key, no credits) it
            # isn't called again until the next batch.
            if verifier.consecutive_failures >= MAX_VERIFIER_FAILURES:
                break
            result = await verifier.verify(address)
            if result is not None:  # a failed call records nothing and uses no daily cap
                results[address] = result

    async with db_factory() as db:
        company = await _locked_company(db, company_id)
        if company is None or company.status == "excluded":
            return _excluded(company_id)  # excluded while we were looking
        if mx.state == "unknown":
            rows = (
                await db.scalars(select(EmailAddress).where(EmailAddress.company_id == company_id))
            ).all()
            await _refresh_blocks(db, rows)
            # DNS may answer later; a domain that isn't a valid name never will, so it waits
            # for the ordinary recheck instead of being claimed again on every pass.
            company.contacts_checked_at = now if mx.error == INVALID_DOMAIN else None
            targets, addresses, sendable = await _recount(db, company, mx_state=mx.state)
            await db.commit()
            return VerifyOutcome(
                company_id, targets, addresses, sendable, mx.state, DNS_UNAVAILABLE
            )

        # Recomputed from the rows as they are now: a profile may have landed meanwhile.
        plan = await _plan(db, company)
        await _save_pattern(db, domain, plan.learned, now=now)
        by_address = {e.address: e for e in plan.emails}
        wanted: set[str] = set()
        for person_id, candidate in plan.candidates.items():
            wanted.add(candidate.address)
            row = by_address.get(candidate.address)
            if row is None:
                row = EmailAddress(
                    company_id=company_id,
                    address=candidate.address,
                    kind="personal",
                    status=candidate.status,
                    source=candidate.source,
                    pattern=candidate.pattern,
                    person_id=person_id,
                )
                db.add(row)
                by_address[candidate.address] = row
            elif row.source != "page":
                row.source = candidate.source
                row.pattern = candidate.pattern
                row.person_id = person_id
            elif candidate.status == "published":
                # A page address is linked only when it unambiguously renders this person's
                # name; one that merely equals a pattern or a guess stays as it is.
                row.person_id = person_id
        for row in plan.emails:
            # A pattern/guess address no target needs any more goes, unless a teammate
            # overrode it.
            if row.source != "page" and row.address not in wanted and row.override_sendable is None:
                await db.delete(row)
                del by_address[row.address]
        for address, result in results.items():
            row = by_address.get(address)
            if row is not None and row.source != "page":
                row.verifier_result = result
                row.verified_at = now
        rows = list(by_address.values())
        # A hard bounce is final while its suppression stands (an admin may lift it).
        bounced = set(
            (
                await db.scalars(
                    select(Suppression.address).where(
                        Suppression.reason == "bounced",
                        Suppression.address.in_([row.address for row in rows]),
                    )
                )
            ).all()
        )
        for row in rows:
            row.status = "invalid" if row.address in bounced else _derived_status(row, mx.state)
        await _refresh_blocks(db, rows)
        await db.flush()
        company.contacts_checked_at = now
        targets, addresses, sendable = await _recount(db, company, mx_state=mx.state)
        await db.commit()
    return VerifyOutcome(company_id, targets, addresses, sendable, mx.state, None)


async def _claim(
    db_factory: async_sessionmaker[AsyncSession], *, config: AppConfig, now: datetime, limit: int
) -> list[uuid.UUID]:
    """Due companies (read ok/partial, never checked or checked more than
    ``recheck_after_days`` ago), never-checked first then oldest; stamped
    ``contacts_checked_at = now`` in one short transaction so a concurrent batch skips them.

    A company whose domain's DNS couldn't answer within the last ``UNKNOWN_RETRY_AFTER`` is left
    out until then: it would only get the cached "unknown" again, and a handful of such domains
    would otherwise take every batch."""
    recheck = now - timedelta(days=config.contacts.recheck_after_days)
    dns_pending = and_(DomainMx.state == "unknown", DomainMx.checked_at > now - UNKNOWN_RETRY_AFTER)
    async with db_factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Company.id)
                    .outerjoin(DomainMx, DomainMx.domain == Company.domain)
                    .where(
                        Company.status != "excluded",
                        Company.profile_status.in_(("ok", "partial")),
                        or_(
                            Company.contacts_checked_at.is_(None),
                            Company.contacts_checked_at < recheck,
                        ),
                        or_(DomainMx.domain.is_(None), not_(dns_pending)),
                    )
                    .order_by(Company.contacts_checked_at.asc().nulls_first(), Company.id)
                    .limit(limit)
                    .with_for_update(of=Company, skip_locked=True)
                )
            ).all()
        )
        if ids:
            await db.execute(
                update(Company)
                .where(Company.id.in_(ids))
                .values(contacts_checked_at=now)
                .execution_options(synchronize_session=False)
            )
        await db.commit()
    return ids


def _frames(exc: BaseException) -> list[str]:
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_CRASH_FRAMES:]
    return [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames]


async def verify_batch(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    checker: MxChecker,
    verifier: Verifier | None,
    config: AppConfig,
    now: datetime,
    limit: int,
) -> list[VerifyOutcome]:
    """Claim up to ``limit`` due companies and check them one after another (so the verifier's
    daily cap is counted exactly). One company's crash becomes an error outcome (it stays
    claimed until the next recheck) and never stops the batch."""
    outcomes: list[VerifyOutcome] = []
    for company_id in await _claim(db_factory, config=config, now=now, limit=limit):
        try:
            outcome = await verify_company(
                db_factory,
                company_id,
                checker=checker,
                verifier=verifier,
                config=config,
                now=now,
            )
        except Exception as exc:
            # Only the exception type: its message could carry page text.
            log.warning(
                "contact_check_failed",
                company_id=str(company_id),
                error_type=type(exc).__name__,
                frames=_frames(exc),
            )
            outcome = VerifyOutcome(
                company_id, 0, 0, 0, "unknown", f"Contact check error ({type(exc).__name__})."
            )
            await _retry_soon(db_factory, company_id, config=config, now=now)
        outcomes.append(outcome)
    return outcomes


async def _retry_soon(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    *,
    config: AppConfig,
    now: datetime,
) -> None:
    """Make a crashed check due again after ``CRASH_RETRY`` (Ruling AI): back-date its claim so
    the ordinary selection picks it up then. Left alone if anything else touched it meanwhile."""
    due_at = now - timedelta(days=config.contacts.recheck_after_days) + CRASH_RETRY
    try:
        async with db_factory() as db:
            await db.execute(
                update(Company)
                .where(Company.id == company_id, Company.contacts_checked_at == now)
                .values(contacts_checked_at=due_at)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as exc:
        log.warning(
            "contact_check_retry_not_recorded",
            company_id=str(company_id),
            error_type=type(exc).__name__,
        )


# --- overrides ------------------------------------------------------------------------------------


async def set_override(
    db: AsyncSession, address_id: uuid.UUID, *, sendable: bool | None, actor: User
) -> EmailAddress:
    """Allow (True) or stop (False) sending to one address, or clear the override (None), and
    recount the company's sendable contacts. An ``invalid`` address stays unsendable whatever
    the override. Refuses excluded companies. The caller audits and commits; flushes."""
    company_id = await db.scalar(
        select(EmailAddress.company_id).where(EmailAddress.id == address_id)
    )
    if company_id is None:
        raise LookupError("Email address not found.")
    company = await _locked_company(db, company_id)
    if company is None:
        raise LookupError("Company not found.")
    if company.status == "excluded":
        raise ValueError("This company is excluded.")
    row = await db.scalar(
        select(EmailAddress)
        .where(EmailAddress.id == address_id)
        .execution_options(populate_existing=True)
    )
    if row is None:  # pragma: no cover - deleted between the two reads, under the lock
        raise LookupError("Email address not found.")
    row.override_sendable = sendable
    row.override_by_id = actor.id if sendable is not None else None
    row.override_at = datetime.now(UTC) if sendable is not None else None
    await db.flush()
    await _recount(db, company, mx_state=await mx_state_of(db, company.domain))
    await db.flush()
    return row
