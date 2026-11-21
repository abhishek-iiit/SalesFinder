"""Discovery jobs: estimate -> approve (budget-checked) -> claim -> run.

``create_estimate``, ``approve_job``, ``cancel_job``, ``claim_next_job`` and
``fail_interrupted_jobs`` flush; their callers commit. ``run_job`` is a worker job and commits in
its own sessions (the documented exception, like the HubSpot sync): the spend of every search is
committed as soon as Serper answers, and that search's companies in the next transaction, so
progress is visible while it runs and money spent survives a crash.
"""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.catalog import Catalog
from salesfinder.config import DiscoveryConfig
from salesfinder.db.models import Company, DiscoveryDrop, DiscoveryJob, User
from salesfinder.discovery.budget import get_budget, month_spend
from salesfinder.discovery.junk import junk_reason
from salesfinder.discovery.queries import Query, build_queries, estimate_cost, search_cost
from salesfinder.discovery.serper import SearchResult, SerperClient, SerperError
from salesfinder.domains import host_of, normalize_domain
from salesfinder.exclusions.check import exclusion_for

log = structlog.get_logger(__name__)

APPROVAL_LOCK = text("SELECT pg_advisory_xact_lock(4242)")
INTERRUPTED = "Interrupted (worker stopped mid-job)."
CANCELLED_MID_RUN = "The discovery job was cancelled while running."
# The worker was asked to stop mid-job (Ruling P7-G): the searches already paid for are kept.
STOPPED_FOR_SHUTDOWN = (
    "Stopped after {done} of {total} searches: the worker was shutting down. "
    "Start a new search for the rest."
)
MAX_ERROR = 500
TITLE_SEPARATORS = (" - ", " | ", " \u2013 ")  # hyphen, pipe, en dash
ZERO = Decimal(0)
CENTS = Decimal("0.01")


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _locked_job(db: AsyncSession, job_id: uuid.UUID) -> DiscoveryJob:
    """The job row, locked ``FOR UPDATE`` and re-read from the database."""
    job = await db.scalar(
        select(DiscoveryJob)
        .where(DiscoveryJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise LookupError("Discovery job not found.")
    return job


async def _flush(db: AsyncSession, job: DiscoveryJob) -> DiscoveryJob:
    await db.flush()
    await db.refresh(job, ["created_at", "updated_at"])  # server-side timestamps
    return job


# --- the teammate's side: estimate, approve, cancel ---------------------------------------------


async def create_estimate(
    db: AsyncSession,
    *,
    side: str,
    product_ids: Sequence[str],
    industries: Sequence[str],
    region_names: Sequence[str],
    created_by: User,
    catalog: Catalog,
    config: DiscoveryConfig,
) -> DiscoveryJob:
    """Build the queries and price them. Nothing is searched until someone approves."""
    queries = build_queries(
        side=side,
        product_ids=product_ids,
        industries=industries,
        region_names=region_names,
        catalog=catalog,
        config=config,
    )
    searches, cost = estimate_cost(len(queries), config)
    job = DiscoveryJob(
        side=side,
        params={
            "products": list(dict.fromkeys(product_ids)),
            "industries": list(dict.fromkeys(industries)) if side == "buyer" else [],
            "regions": list(dict.fromkeys(region_names)),
        },
        queries=[asdict(q) for q in queries],
        pages_per_query=config.pages_per_query,
        estimated_searches=searches,
        estimated_cost_usd=cost,
        status="estimated",
        created_by_id=created_by.id,
    )
    db.add(job)
    return await _flush(db, job)


async def approve_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    approved_by: User,
    now: datetime,
    config: DiscoveryConfig,
) -> DiscoveryJob:
    """Approve an estimate if it fits in what's left of this month's budget.

    The transaction-scoped advisory lock serializes approvals until the caller commits, so two
    approvals can't both see the same remaining budget.
    """
    await db.execute(APPROVAL_LOCK)
    job = await _locked_job(db, job_id)
    if job.status != "estimated":
        raise ValueError(f"Only an estimated job can be approved (this one is {job.status}).")
    budget = await get_budget(db, config)
    remaining = max(budget - await month_spend(db, now=now), ZERO)
    if job.estimated_cost_usd > remaining:
        # Round the amount left down, so "$0.05 left" never hides that only $0.0499 is.
        shown = remaining.quantize(CENTS, rounding=ROUND_DOWN)
        raise ValueError(
            f"This job needs ${job.estimated_cost_usd:.4f}; "
            f"${shown:.2f} of this month's ${budget:.2f} budget is left."
        )
    job.status = "approved"
    job.approved_by_id = approved_by.id
    job.approved_at = now
    return await _flush(db, job)


async def cancel_job(
    db: AsyncSession, job_id: uuid.UUID, *, actor: User, now: datetime | None = None
) -> DiscoveryJob:
    """Cancel a job that hasn't started. ``actor`` is for the caller's audit row."""
    job = await _locked_job(db, job_id)
    if job.status not in ("estimated", "approved"):
        raise ValueError(
            f"Only a job that hasn't started can be cancelled (this one is {job.status})."
        )
    job.status = "cancelled"
    job.finished_at = now or _utcnow()
    return await _flush(db, job)


# --- the worker's side: claim, reap, run ---------------------------------------------------------


async def claim_next_job(db: AsyncSession, *, now: datetime | None = None) -> DiscoveryJob | None:
    """Mark the oldest approved job running. Rows another worker holds are skipped."""
    job = await db.scalar(
        select(DiscoveryJob)
        .where(DiscoveryJob.status == "approved")
        .order_by(DiscoveryJob.approved_at, DiscoveryJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = now or _utcnow()
    return await _flush(db, job)


async def fail_interrupted_jobs(
    db: AsyncSession, *, now: datetime, older_than: timedelta = timedelta(hours=1)
) -> int:
    """Fail jobs left ``running`` by a worker that stopped. Returns how many."""
    result = await db.execute(
        update(DiscoveryJob)
        .where(DiscoveryJob.status == "running", DiscoveryJob.started_at < now - older_than)
        .values(status="failed", error=INTERRUPTED, finished_at=now, updated_at=func.now())
        .returning(DiscoveryJob.id)
    )
    return len(result.scalars().all())


def company_name(title: str, domain: str) -> str:
    """The title up to the first separator (hyphen, pipe or en dash between spaces), trimmed;
    the domain if that leaves nothing."""
    cut = len(title)
    for sep in TITLE_SEPARATORS:
        at = title.find(sep)
        if at != -1:
            cut = min(cut, at)
    name = title[:cut].strip()
    return (name or domain)[:300]


@dataclass
class _Run:
    """In-memory state of one run."""

    job_id: uuid.UUID
    side: str
    available: Decimal  # what this job may spend in total: budget - everyone else's spend
    plan: list[tuple[Query, int]]
    searches_done: int
    handled: set[str] = field(default_factory=set)  # domains already counted in this job


async def _start(
    db_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    config: DiscoveryConfig,
    now: Callable[[], datetime],
) -> _Run:
    async with db_factory() as db, db.begin():
        job = await _locked_job(db, job_id)
        if job.status == "approved":  # not claimed first: claim it here
            job.status = "running"
            job.started_at = now()
        elif job.status != "running":
            raise ValueError(f"Only an approved job can run (this one is {job.status}).")
        # Leave this job out of the month's spend entirely: while approved it is counted at its
        # estimate, which must not be held against itself; once running, at its own spend.
        others = await month_spend(db, now=now(), exclude_job_id=job.id)
        available = await get_budget(db, config) - others
        plan = [
            (Query(**q), page) for q in job.queries for page in range(1, job.pages_per_query + 1)
        ]
        return _Run(job.id, job.side, available, plan, job.searches_done)


async def _record_search(
    db_factory: async_sessionmaker[AsyncSession], run: _Run, config: DiscoveryConfig
) -> DiscoveryJob:
    """Commit the spend of one completed search, whatever state the job is in now."""
    async with db_factory() as db, db.begin():
        job = await _locked_job(db, run.job_id)
        job.searches_done += 1
        job.actual_cost_usd = search_cost(job.searches_done, config)
        run.searches_done = job.searches_done
        return await _flush(db, job)


async def _record_result(
    db: AsyncSession,
    job: DiscoveryJob,
    run: _Run,
    query: Query,
    result: SearchResult,
    config: DiscoveryConfig,
) -> None:
    job.results_seen += 1
    domain = normalize_domain(result.url)
    reason = junk_reason(result.url, config.junk_domains)
    if reason is not None or domain is None:
        db.add(
            DiscoveryDrop(
                job_id=job.id,
                url=result.url[:2048],
                domain=domain[:255] if domain else None,
                reason=reason or "no_domain",
            )
        )
        job.results_dropped += 1
        return
    if domain in run.handled:
        job.companies_existing += 1
        return
    run.handled.add(domain)
    existing = await db.scalar(
        select(Company.id).where(Company.side == run.side, Company.domain == domain)
    )
    if existing is not None:
        job.companies_existing += 1
        return
    exclusion = await exclusion_for(db, domain=domain)
    inserted = await db.scalar(
        insert(Company)
        .values(
            id=uuid.uuid4(),
            side=run.side,
            name=company_name(result.title, domain),
            domain=domain[:255],
            website_url=f"https://{host_of(result.url) or domain}"[:2048],
            country=query.country[:2] or None,
            region=query.region[:100] or None,
            source="serper",
            discovered_by_job_id=job.id,
            discovery_query=query.q[:500],
            snippet=result.snippet[:1000] or None,
            status="excluded" if exclusion else "new",
            exclusion_reason=exclusion.reason if exclusion else None,
            exclusion_detail=exclusion.detail[:300] if exclusion else None,
        )
        .on_conflict_do_nothing(constraint="uq_companies_side_domain")
        .returning(Company.id)
    )
    if inserted is None:  # another job inserted it after our check
        job.companies_existing += 1
    elif exclusion is not None:
        job.companies_excluded += 1
    else:
        job.companies_new += 1


async def _record_results(
    db_factory: async_sessionmaker[AsyncSession],
    run: _Run,
    query: Query,
    results: list[SearchResult],
    config: DiscoveryConfig,
) -> DiscoveryJob:
    async with db_factory() as db, db.begin():
        job = await _locked_job(db, run.job_id)
        if job.status == "running":
            for result in results:
                await _record_result(db, job, run, query, result, config)
            await _flush(db, job)
        return job


async def _finish(
    db_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    status: str,
    error: str | None,
    now: Callable[[], datetime],
) -> DiscoveryJob:
    """End a running job. A job something else already ended is left as it is."""
    async with db_factory() as db, db.begin():
        job = await _locked_job(db, job_id)
        if job.status == "running":
            job.status = status
            job.finished_at = now()
            job.error = error[:MAX_ERROR] if error else None
            await _flush(db, job)
    log.info(
        "discovery_job_finished",
        job_id=str(job_id),
        status=job.status,
        searches=job.searches_done,
        cost_usd=str(job.actual_cost_usd),
        new=job.companies_new,
        existing=job.companies_existing,
        excluded=job.companies_excluded,
        dropped=job.results_dropped,
        error=job.error,
    )
    return job


async def _search_all(
    db_factory: async_sessionmaker[AsyncSession],
    run: _Run,
    *,
    client: SerperClient,
    config: DiscoveryConfig,
    now: Callable[[], datetime],
    stop: asyncio.Event | None = None,
) -> DiscoveryJob:
    total = len(run.plan)
    for query, page in run.plan[run.searches_done :]:
        if stop is not None and stop.is_set():
            return await _finish(
                db_factory,
                run.job_id,
                "failed",
                STOPPED_FOR_SHUTDOWN.format(done=run.searches_done, total=total),
                now,
            )
        if search_cost(run.searches_done + 1, config) > run.available:
            return await _finish(
                db_factory,
                run.job_id,
                "stopped_budget",
                f"Stopped after {run.searches_done} of {total} searches: the next one would "
                f"exceed this month's budget (${max(run.available, ZERO):.4f} was available "
                "for this job).",
                now,
            )
        try:
            results = await client.search(query.q, gl=query.gl, page=page)
        except SerperError as exc:
            return await _finish(db_factory, run.job_id, "failed", exc.reason, now)
        job = await _record_search(db_factory, run, config)
        if job.status != "running":  # ended elsewhere (reaped); the spend is recorded
            return job
        job = await _record_results(db_factory, run, query, results, config)
        if job.status != "running":
            return job
    return await _finish(db_factory, run.job_id, "succeeded", None, now)


async def run_job(
    db_factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    client: SerperClient,
    catalog: Catalog,
    config: DiscoveryConfig,
    now: Callable[[], datetime],
    stop: asyncio.Event | None = None,
) -> DiscoveryJob:
    """Run an approved (or claimed) job to a terminal state and return it.

    Once ``stop`` is set (the worker is shutting down; Ruling P7-G) no new search starts: the
    job ends ``failed`` with ``STOPPED_FOR_SHUTDOWN``, keeping the searches already paid for.

    Raises ``LookupError``/``ValueError`` only when the job can't start (missing, or not
    approved/running). After that, Serper and database failures don't raise: the job ends
    ``failed`` with a reason, keeping every search already paid for. ``catalog`` is unused:
    the queries were fixed when the job was estimated.
    """
    run = await _start(db_factory, job_id, config, now)
    log.info("discovery_job_started", job_id=str(job_id), searches=len(run.plan))
    try:
        return await _search_all(db_factory, run, client=client, config=config, now=now, stop=stop)
    except asyncio.CancelledError:
        # Don't leave the job "running" until the reaper finds it; then keep cancelling.
        await asyncio.shield(_finish(db_factory, job_id, "failed", CANCELLED_MID_RUN, now))
        raise
    except Exception as exc:
        # Only the type: a database error's text can carry SQL parameters (customer data).
        reason = f"Discovery job failed unexpectedly ({type(exc).__name__})."
        log.warning("discovery_job_error", job_id=str(job_id), error_type=type(exc).__name__)
        return await _finish(db_factory, job_id, "failed", reason, now)
