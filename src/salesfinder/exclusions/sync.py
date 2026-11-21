"""HubSpot -> known_relationships sync: request, schedule, run, and report health.

``run_sync`` is a worker job and commits in its own sessions (the documented exception to
"services flush, callers commit"). Everything is fetched into memory first; the upsert of every
row, the delete of rows HubSpot no longer has, the exclusion sweep of stored companies, the
refresh of stored addresses' blocks, and the ``succeeded`` status then commit in one
transaction, so a failure at any point leaves the previous rows exactly as they were.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
import structlog
from sqlalchemy import ARRAY, Uuid, bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.auth.emails import normalize_email
from salesfinder.config import HubSpotConfig
from salesfinder.contacts.service import refresh_blocks
from salesfinder.db.models import HubSpotSync, KnownRelationship, User
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email, normalize_domain
from salesfinder.exclusions.hubspot import HubSpotClient, HubSpotError
from salesfinder.exclusions.sweep import sweep_exclusions

log = structlog.get_logger(__name__)

SOURCE = "hubspot"
UPSERT_BATCH = 1000  # 10 bound columns per row -> well under asyncpg's 32767-parameter limit
MAX_ERROR = 500
ACTIVE = ("queued", "running")
INTERRUPTED = "Interrupted (worker stopped mid-sync)."
CANCELLED = "Sync was cancelled."
FAILURE_BACKOFF_BASE = timedelta(minutes=5)
EMPTY_RESULT = (
    "HubSpot returned no usable companies or contacts; kept the existing {existing} rows. "
    "Check that the token belongs to Elchemy's portal."
)

Key = tuple[str, str]  # (kind, value)


# --- requesting and scheduling ------------------------------------------------------------


async def request_sync(db: AsyncSession, *, requested_by: User | None) -> HubSpotSync:
    """Queue a sync unless one is already queued or running (then return that one). Flushes."""
    existing = await db.scalar(
        select(HubSpotSync)
        .where(HubSpotSync.status.in_(ACTIVE))
        .order_by(HubSpotSync.created_at)
        .limit(1)
    )
    if existing is not None:
        return existing
    row = HubSpotSync(status="queued", requested_by_id=requested_by.id if requested_by else None)
    db.add(row)
    await db.flush()
    await db.refresh(row, ["created_at"])
    return row


async def _has_status(db: AsyncSession, status: str) -> bool:
    found = await db.scalar(select(HubSpotSync.id).where(HubSpotSync.status == status).limit(1))
    return found is not None


async def _last_success_at(db: AsyncSession) -> datetime | None:
    return await db.scalar(
        select(func.max(HubSpotSync.finished_at)).where(HubSpotSync.status == "succeeded")
    )


def failure_backoff(consecutive_failures: int, config: HubSpotConfig) -> timedelta:
    """5 min, 10, 20, 40, ... after the 1st, 2nd, 3rd, 4th ... failure in a row, capped at
    the sync interval. (The exponent is capped too, so a long outage can't overflow.)"""
    exponent = min(max(consecutive_failures - 1, 0), 20)
    return min(FAILURE_BACKOFF_BASE * (1 << exponent), timedelta(hours=config.sync_interval_hours))


async def due_for_sync(db: AsyncSession, *, now: datetime, config: HubSpotConfig) -> bool:
    """A queued request (Sync now) is due at once. Otherwise a sync is due once the last
    success is older than the interval, but not while backing off from recent failures: a
    broken token or a HubSpot outage must not be retried on every worker poll."""
    if await _has_status(db, "running"):
        return False
    if await _has_status(db, "queued"):
        return True
    last = await _last_success_at(db)
    if last is not None and last >= now - timedelta(hours=config.sync_interval_hours):
        return False
    # A sync cut short by a stopping worker says nothing about HubSpot: retry it at once.
    failed_since_success = select(func.count(), func.max(HubSpotSync.finished_at)).where(
        HubSpotSync.status == "failed",
        HubSpotSync.finished_at.is_not(None),
        func.coalesce(HubSpotSync.error, "").not_in((INTERRUPTED, CANCELLED)),
    )
    if last is not None:
        failed_since_success = failed_since_success.where(HubSpotSync.finished_at > last)
    failures, last_failure = (await db.execute(failed_since_success)).one()
    if not failures or last_failure is None:
        return True
    return bool(now >= last_failure + failure_backoff(failures, config))


async def fail_interrupted_syncs(
    db: AsyncSession, *, now: datetime, older_than: timedelta = timedelta(hours=1)
) -> int:
    """Fail syncs left ``running`` by a worker that stopped (else none would ever be due again).

    Returns how many. Flushes; the caller commits.
    """
    result = await db.execute(
        update(HubSpotSync)
        .where(HubSpotSync.status == "running", HubSpotSync.started_at < now - older_than)
        .values(status="failed", error=INTERRUPTED, finished_at=now)
        .returning(HubSpotSync.id)
    )
    return len(result.scalars().all())


# --- health ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncHealth:
    state: Literal["not_configured", "never_synced", "fresh", "stale", "failing"]
    last_success_at: datetime | None
    message: str


def _fmt(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


async def _latest_finished(db: AsyncSession) -> HubSpotSync | None:
    return await db.scalar(
        select(HubSpotSync)
        .where(HubSpotSync.status.in_(("succeeded", "failed")))
        .where(HubSpotSync.finished_at.is_not(None))
        .order_by(HubSpotSync.finished_at.desc())
        .limit(1)
    )


def _failure_text(row: HubSpotSync) -> tuple[str, str]:
    when = _fmt(row.finished_at) if row.finished_at else "recently"
    return when, row.error or "no reason recorded"


async def sync_health(
    db: AsyncSession, *, now: datetime, token_configured: bool, config: HubSpotConfig
) -> SyncHealth:
    last_success = await _last_success_at(db)
    if not token_configured:
        return SyncHealth(
            "not_configured",
            last_success,
            "HubSpot is not configured: set SALESFINDER_HUBSPOT_TOKEN.",
        )
    latest = await _latest_finished(db)
    latest_failed = latest is not None and latest.status == "failed"
    if last_success is not None and last_success >= now - timedelta(hours=config.stale_after_hours):
        message = f"HubSpot last synced {_fmt(last_success)}"
        if latest is not None and latest_failed:
            # Still fresh (the data is recent enough), but don't hide that syncing is broken.
            when, error = _failure_text(latest)
            message += f"; the latest attempt failed {when}: {error}"
        else:
            message += "."
        return SyncHealth("fresh", last_success, message)
    if latest is not None and latest_failed:
        when, error = _failure_text(latest)
        return SyncHealth(
            "failing", last_success, f"The last HubSpot sync failed ({when}): {error}"
        )
    if last_success is None:
        return SyncHealth("never_synced", None, "HubSpot has never been synced.")
    return SyncHealth(
        "stale",
        last_success,
        f"HubSpot data is stale: the last successful sync was {_fmt(last_success)}, "
        f"more than {config.stale_after_hours} hours ago.",
    )


# --- turning HubSpot records into rows -----------------------------------------------------


@dataclass
class _Collected:
    rows: dict[Key, dict[str, Any]]
    companies: int = 0
    contacts: int = 0
    skipped_values: int = 0
    skipped_records: int = 0


def _prop(item: dict[str, Any], name: str) -> str | None:
    props = item.get("properties")
    if not isinstance(props, dict):
        return None
    value = props.get(name)
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _clip(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else None


def _company_domains(item: dict[str, Any]) -> tuple[list[str], int]:
    """Normalized domains from ``domain``, ``website`` and ``hs_additional_domains``."""
    raws = [_prop(item, "domain"), _prop(item, "website")]
    raws += (_prop(item, "hs_additional_domains") or "").split(";")
    domains: list[str] = []
    skipped = 0
    for raw in raws:
        if raw is None or not raw.strip():
            continue
        domain = normalize_domain(raw)
        if domain is None:
            skipped += 1
        elif domain not in domains:
            domains.append(domain)
    return domains, skipped


def _collect(
    companies: Iterable[dict[str, Any]],
    contacts: Iterable[dict[str, Any]],
    owners: dict[str, str],
    *,
    synced_at: datetime,
) -> _Collected:
    """Dedupe in memory by (kind, value); a later record wins over an earlier one of the same
    object type, but a company-sourced row always wins over a contact-derived one.

    Each contact also yields a ``domain`` row for its email's domain (unless it is a free-mail
    provider): a customer that exists in HubSpot only as contacts must still exclude its domain.
    """
    out = _Collected(rows={})

    def add(kind: str, value: str, obj: str, item: dict[str, Any], name: str | None) -> None:
        current = out.rows.get((kind, value))
        if obj == "contact" and current is not None and current["hubspot_object"] == "company":
            return  # never overwrite a company's row with a contact-derived one
        owner_id = _prop(item, "hubspot_owner_id")
        out.rows[(kind, value)] = {
            "source": SOURCE,
            "kind": kind,
            "value": value,
            "hubspot_object": obj,
            "hubspot_id": str(item.get("id", ""))[:32],
            "name": _clip(name, 200),
            "lifecycle_stage": _clip(_prop(item, "lifecyclestage"), 64),
            "owner_name": _clip(owners.get(owner_id) if owner_id else None, 200),
            "synced_at": synced_at,
        }

    for item in companies:
        out.companies += 1
        domains, skipped = _company_domains(item)
        out.skipped_values += skipped
        if not domains:
            out.skipped_records += 1
        for domain in domains:
            add("domain", domain, "company", item, _prop(item, "name"))

    for item in contacts:
        out.contacts += 1
        raw = _prop(item, "email")
        try:
            email = normalize_email(raw) if raw else None
        except ValueError:
            email = None
        if email is None:
            out.skipped_records += 1
            if raw:
                out.skipped_values += 1
            continue
        first, last = _prop(item, "firstname"), _prop(item, "lastname")
        name = " ".join(p for p in (first, last) if p) or None
        add("email", email, "contact", item, name)
        email_domain = domain_of_email(email)
        if email_domain is not None and email_domain not in FREE_MAIL_DOMAINS:
            add("domain", email_domain, "contact", item, _prop(item, "company") or email)
    return out


# --- the replace transaction ----------------------------------------------------------------


async def _upsert(db: AsyncSession, rows: list[dict[str, Any]]) -> list[uuid.UUID]:
    """INSERT ... ON CONFLICT DO UPDATE in batches; returns the id of every row touched."""
    ids: list[uuid.UUID] = []
    for start in range(0, len(rows), UPSERT_BATCH):
        batch = [{"id": uuid.uuid4(), **row} for row in rows[start : start + UPSERT_BATCH]]
        base = insert(KnownRelationship).values(batch)
        stmt = base.on_conflict_do_update(
            constraint="uq_known_relationships_source_kind_value",
            set_={
                "hubspot_object": base.excluded.hubspot_object,
                "hubspot_id": base.excluded.hubspot_id,
                "name": base.excluded.name,
                "lifecycle_stage": base.excluded.lifecycle_stage,
                "owner_name": base.excluded.owner_name,
                "synced_at": base.excluded.synced_at,
                "updated_at": func.now(),
            },
        ).returning(KnownRelationship.id)
        ids.extend(await db.scalars(stmt))
    return ids


# NOT EXISTS over unnest() plans as a hash anti-join: fine for tens of thousands of ids.
_DELETE_UNSEEN = text(
    "DELETE FROM known_relationships AS k "
    "WHERE k.source = :source "
    "AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:ids AS uuid[])) AS seen(id) WHERE seen.id = k.id)"
).bindparams(bindparam("ids", type_=ARRAY(Uuid())))


async def _delete_unseen(db: AsyncSession, keep_ids: list[uuid.UUID]) -> int:
    """Delete HubSpot rows this sync did not upsert. Returns how many were removed."""
    result = await db.execute(_DELETE_UNSEEN, {"source": SOURCE, "ids": keep_ids})
    return int(getattr(result, "rowcount", 0) or 0)


# --- the job --------------------------------------------------------------------------------


async def _claim(
    db_factory: async_sessionmaker[AsyncSession], now: Callable[[], datetime]
) -> uuid.UUID:
    """Mark the oldest queued sync (or a new one) running, and commit."""
    async with db_factory() as db, db.begin():
        row = await db.scalar(
            select(HubSpotSync)
            .where(HubSpotSync.status == "queued")
            .order_by(HubSpotSync.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            row = HubSpotSync(status="queued")
            db.add(row)
        row.status = "running"
        row.started_at = now()
        await db.flush()
        return row.id


async def _fail(
    db_factory: async_sessionmaker[AsyncSession],
    sync_id: uuid.UUID,
    error: str,
    now: Callable[[], datetime],
) -> HubSpotSync:
    async with db_factory() as db, db.begin():
        row = await db.get(HubSpotSync, sync_id, with_for_update=True)
        if row is None:  # pragma: no cover - the row was committed by _claim
            raise RuntimeError(f"HubSpot sync {sync_id} vanished")
        row.status = "failed"
        row.finished_at = now()
        row.error = error[:MAX_ERROR]
    return row


def _error_text(exc: BaseException, token: str) -> str:
    if isinstance(exc, HubSpotError):
        message = exc.reason
    else:
        # Only the type: a database error's text carries the SQL and its parameters, i.e.
        # HubSpot contacts' names and emails, which must not land in hubspot_syncs.error.
        message = f"Sync failed unexpectedly ({type(exc).__name__})."
    if token:
        message = message.replace(token, "[redacted]")  # belt and braces: never store it
    return message[:MAX_ERROR]


async def _fetch(
    token: str,
    transport: httpx.AsyncBaseTransport | None,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    async with HubSpotClient(token, transport=transport, sleep=sleep) as client:
        owners = await client.owners()
        companies = [item async for item in client.iter_companies()]
        contacts = [item async for item in client.iter_contacts()]
    return companies, contacts, owners


async def _replace(
    db_factory: async_sessionmaker[AsyncSession],
    sync_id: uuid.UUID,
    collected: _Collected,
    now: Callable[[], datetime],
) -> tuple[HubSpotSync, int, int, int]:
    """One transaction: upsert every row, delete unseen rows, re-exclude stored companies the
    new rows cover, mark the sync succeeded. Returns (sync, rows kept, rows removed,
    companies newly excluded)."""
    async with db_factory() as db, db.begin():
        row = await db.get(HubSpotSync, sync_id, with_for_update=True)
        if row is None:  # pragma: no cover - the row was committed by _claim
            raise RuntimeError(f"HubSpot sync {sync_id} vanished")
        if not collected.rows:
            existing = await db.scalar(
                select(func.count())
                .select_from(KnownRelationship)
                .where(KnownRelationship.source == SOURCE)
            )
            if existing:
                raise HubSpotError(EMPTY_RESULT.format(existing=existing))
        keep = await _upsert(db, list(collected.rows.values()))
        removed = await _delete_unseen(db, keep)
        excluded = await sweep_exclusions(db)
        # Stored addresses HubSpot now knows (or no longer knows) are blocked (or unblocked)
        # with the new rows, not only at their next contact check.
        await refresh_blocks(db)
        row.companies_count = collected.companies
        row.contacts_count = collected.contacts
        row.status = "succeeded"
        row.finished_at = now()
        row.error = None
    return row, len(keep), removed, excluded


async def run_sync(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    token: str,
    now: Callable[[], datetime],
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> HubSpotSync:
    """Run one sync end to end. HubSpot and DB failures don't raise: the failed row is returned.

    Only the fetch and the replace transaction sit inside ``try``: once the replace has
    committed, nothing (e.g. a logging error) can flip a succeeded sync to failed.
    """
    sync_id = await _claim(db_factory, now)
    try:
        log.info("hubspot_sync_started", sync_id=str(sync_id))
        companies, contacts, owners = await _fetch(token, transport, sleep)
        collected = _collect(companies, contacts, owners, synced_at=now())
        row, kept, removed, excluded = await _replace(db_factory, sync_id, collected, now)
    except asyncio.CancelledError:
        # Don't leave the row "running" forever; record why, then keep cancelling.
        await asyncio.shield(_fail(db_factory, sync_id, CANCELLED, now))
        raise
    except Exception as exc:
        failed = await _fail(db_factory, sync_id, _error_text(exc, token), now)
        log.warning("hubspot_sync_failed", sync_id=str(sync_id), error=failed.error)
        return failed
    log.info(
        "hubspot_sync_succeeded",
        sync_id=str(sync_id),
        companies=collected.companies,
        contacts=collected.contacts,
        rows=kept,
        removed=removed,
        companies_excluded=excluded,
        skipped_values=collected.skipped_values,
        skipped_records=collected.skipped_records,
    )
    return row
