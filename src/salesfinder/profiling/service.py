"""Profile discovered companies: read their own website politely, store what each confirmed
signal finds as observations, and score them.

``profile_company`` and ``profile_batch`` are worker jobs and own their transactions (the
documented exception, like discovery jobs): no transaction is open while a site is being read;
the claim and the final write are each one short transaction. ``rescore_all``,
``change_signal_states``, ``mark_side_due``, ``request_reprofile`` and ``observed_counts`` are
ordinary services: they flush, callers commit.
"""

import asyncio
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import structlog
from sqlalchemy import (
    ColumnElement,
    Table,
    and_,
    bindparam,
    case,
    exists,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder import audit
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.contacts.extract import (
    EmailFound,
    PersonFound,
    contact_form_url,
    extract_emails,
    extract_people,
    is_people_page,
)
from salesfinder.contacts.service import store_extraction
from salesfinder.db.models import Company, Observation, User
from salesfinder.profiling.fetch import FetchBlocked, SafeFetcher
from salesfinder.profiling.pages import pick_pages
from salesfinder.profiling.scoring import is_present, score_company
from salesfinder.signals.catalog import SIGNALS, by_code
from salesfinder.signals.detect_common import Finding
from salesfinder.signals.latest import latest_values as _latest_values
from salesfinder.signals.run import run_detectors
from salesfinder.signals.settings import confirmed_codes, set_state, states
from salesfinder.signals.text import PageText, extract

log = structlog.get_logger(__name__)

MAX_ERROR = 300  # companies.profile_error is String(300)
MAX_URL = 2048  # observations.evidence_url
MAX_SNIPPET = 300  # observations.evidence_snippet
# A pending company whose claim is younger than this is being profiled (or its run died):
# leave it alone until then.
CLAIM_RETRY = timedelta(hours=1)
# One company's whole fetch phase may take this many page timeouts beyond its page budget.
DEADLINE_EXTRA_PAGES = 2
RESCORE_CHUNK = 1000
# Transaction-scoped: writers of a profile hold it shared, rescore_all exclusive. So a rescore
# never computes from observations a profile write is about to commit (and then overwrites its
# score), and a profile write never scores with a signal state a rescore is changing.
SCORING_LOCK_KEY = 0x53_46_53_43  # "SFSC"
_SCORING_LOCK_SHARED = text(f"SELECT pg_advisory_xact_lock_shared({SCORING_LOCK_KEY})")
_SCORING_LOCK = text(f"SELECT pg_advisory_xact_lock({SCORING_LOCK_KEY})")
TIMED_OUT = "Timed out while reading the website."
NOT_A_WEB_PAGE = "The homepage is not a web page."
NO_CONNECT = "Could not connect"  # SafeFetcher's error when nothing answered (or no DNS)
TIMED_OUT_PREFIX = "Timed out"  # TIMED_OUT, and SafeFetcher's "Timed out after 10s"
# A batch where at least this many companies whose sites weren't already failing all failed to
# connect or timed out (and none was read) says more about our network than about their sites:
# nothing is recorded, and they are tried again after NETWORK_RETRY (circuit breaker).
NETWORK_DOWN_MIN_BATCH = 3
NETWORK_DOWN = "Network looks down — will retry."
NETWORK_RETRY = timedelta(minutes=15)
# A company held this many times in a row is no longer held: its unreachable outcome is
# recorded (so a genuinely dead site can't sit behind the breaker forever).
MAX_BREAKER_HOLDS = 3
# Statuses retried sooner than profiling.refresh_after_days: the site may be back tomorrow.
TRANSIENT_STATUSES = ("failed", "no_site")
# Wait after the 1st, 2nd consecutive transient failure; from the 3rd on, refresh_after_days
# (and never longer than it).
FAILURE_BACKOFF = (timedelta(days=1), timedelta(days=7))
# Detectors see at most this much of each page's visible text (from the start), so a huge
# page can't make one company's analysis take seconds.
MAX_DETECT_CHARS = 200_000
MAX_CRASH_FRAMES = 12

SKIPPED = "skipped"  # outcome only: nothing was written (excluded, or nothing to look for)
FetchStatus = Literal["ok", "partial", "failed", "blocked", "no_site"]


@dataclass(frozen=True)
class ProfileOutcome:
    company_id: uuid.UUID
    profile_status: str  # a companies.profile_status value, or "skipped"
    pages_fetched: int
    findings: int  # positive findings (observations that found something)
    score: int | None
    error: str | None
    capped: bool = False  # recorded as unreachable because it reached MAX_BREAKER_HOLDS


@dataclass
class _Fetched:
    status: FetchStatus
    error: str | None
    pages: list[tuple[str, str]] = field(default_factory=list)  # (final URL, HTML)

    @property
    def unreachable(self) -> bool:
        """The homepage couldn't be connected to, or reading the site timed out."""
        return self.status in TRANSIENT_STATUSES and (
            self.error == NO_CONNECT or (self.error or "").startswith(TIMED_OUT_PREFIX)
        )


@dataclass(frozen=True)
class _Claim:
    """What a claimed company looked like before the claim."""

    profiled_at: datetime | None
    profile_status: str
    holds: int = 0


@dataclass(frozen=True)
class _Held:
    """An unreachable homepage not recorded yet: the batch decides (see ``profile_batch``)."""

    company_id: uuid.UUID
    fetched: _Fetched


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_nul(value: Any) -> Any:
    """Postgres text and JSONB reject U+0000; page text is untrusted and might carry one."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    return value


# --- fetching (no database) ----------------------------------------------------------------------


async def _read_site(
    fetcher: SafeFetcher, website_url: str, domain: str, *, max_pages: int
) -> _Fetched:
    """The homepage, then up to ``max_pages - 1`` picked pages, one at a time (politely).

    A refusal or error on the homepage decides the company's status; on a picked page it only
    makes the profile partial (Ruling S)."""
    try:
        home = await fetcher.get(website_url, site_domain=domain)
    except FetchBlocked as exc:
        return _Fetched("blocked", _clip(exc.message, MAX_ERROR))
    if home.error is not None:
        status: FetchStatus = "no_site" if home.error == NO_CONNECT else "failed"
        return _Fetched(status, _clip(home.error, MAX_ERROR))
    if home.html is None:
        return _Fetched("failed", NOT_A_WEB_PAGE)

    pages = [(home.url, home.html)]
    # Parsing a large page is CPU work: keep it off the event loop.
    picked = await asyncio.to_thread(pick_pages, home.html, home.url, limit=max_pages)
    failed = 0
    for url in picked:
        try:
            result = await fetcher.get(url, site_domain=domain)
        except FetchBlocked:
            failed += 1
            continue
        if result.error is not None:
            failed += 1
        elif result.html is not None:  # a non-HTML link (a PDF without .pdf) is just skipped
            pages.append((result.url, result.html))
    if failed:
        return _Fetched("partial", f"{failed} of {len(picked) + 1} pages could not be read.", pages)
    return _Fetched("ok", None, pages)


@dataclass(frozen=True)
class _Contacts:
    """People, published addresses and the contact form read from the pages."""

    people: list[PersonFound]
    emails: list[EmailFound]
    form_url: str | None


@dataclass(frozen=True)
class _Analysis:
    findings: list[Finding]
    contacts: _Contacts | None  # None: extraction crashed, so nothing is stored (or dropped)
    people_pages: int = 0  # about/contact/team/leadership pages among those read


def _extract_contacts(
    pages: Sequence[PageText],
    raw_pages: Sequence[tuple[str, str]],
    *,
    domain: str,
    company_name: str,
) -> _Contacts | None:
    try:
        people = extract_people(raw_pages, company_name=company_name, company_domain=domain)
        emails = extract_emails(pages, company_domain=domain, people=people, pages_html=raw_pages)
        form_url = contact_form_url(raw_pages)
    except Exception as exc:
        # Only the type and frames: the message could carry page text.
        log.warning(
            "contact_extraction_failed", error_type=type(exc).__name__, frames=crash_frames(exc)
        )
        return None
    return _Contacts(people, emails, form_url)


def _analyse(
    raw_pages: Sequence[tuple[str, str]],
    *,
    side: str,
    domain: str,
    company_name: str,
    codes: frozenset[str],
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    region_fallback: str | None,
) -> _Analysis:
    """Extract text, run the detectors and read people / addresses / the contact form.
    CPU-bound: runs in a worker thread. Each page's text is cut to its first
    ``MAX_DETECT_CHARS`` characters (a prefix, so evidence offsets into it stay valid)."""
    pages: list[PageText] = []
    for url, html in raw_pages:
        page = extract(html, url)
        if len(page.text) > MAX_DETECT_CHARS:
            page = replace(page, text=page.text[:MAX_DETECT_CHARS])
        pages.append(page)
    findings = run_detectors(
        pages,
        side=side,
        codes=codes,
        catalog=catalog,
        facts=facts,
        config=config,
        region_fallback=region_fallback,
    )
    contacts = _extract_contacts(pages, raw_pages, domain=domain, company_name=company_name)
    people_pages = sum(1 for page in pages if is_people_page(page.url, page.title))
    return _Analysis(findings, contacts, people_pages)


# --- the database side ---------------------------------------------------------------------------


async def _locked_company(db: AsyncSession, company_id: uuid.UUID) -> Company | None:
    return await db.scalar(
        select(Company)
        .where(Company.id == company_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _skipped(company_id: uuid.UUID, error: str | None = None) -> ProfileOutcome:
    return ProfileOutcome(company_id, SKIPPED, 0, 0, None, error)


async def _write_failure(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    *,
    status: str,
    error: str | None,
    now: datetime,
) -> ProfileOutcome:
    """Record a profile that produced no pages. Earlier observations and score stay as they
    were: a site being down today says nothing about what it said before."""
    async with db_factory() as db:
        company = await _locked_company(db, company_id)
        if company is None or company.status == "excluded":
            return _skipped(company_id)
        company.profile_status = status
        company.profile_error = error
        company.profiled_at = now
        company.profile_retry_at = None
        company.profile_holds = 0
        company.pages_fetched = 0
        if status in TRANSIENT_STATUSES:
            company.profile_failures += 1
        score = company.score
        await db.commit()
    return ProfileOutcome(company_id, status, 0, 0, score, error)


async def _write_results(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    fetched: _Fetched,
    analysis: _Analysis,
    *,
    config: AppConfig,
    now: datetime,
) -> ProfileOutcome:
    """Observations, people / published addresses and the company update in one transaction,
    with the company row locked so the exclusion sweep can't slip in between the check and the
    write.

    A ``partial`` run never overwrites a positive finding with a negative one: the page that
    said it may be the one that couldn't be read, so the earlier observation stays latest. Nor
    does it drop people or addresses found earlier."""
    findings = analysis.findings
    async with db_factory() as db:
        await db.execute(_SCORING_LOCK_SHARED)
        company = await _locked_company(db, company_id)
        if company is None or company.status == "excluded":
            return _skipped(company_id)  # excluded while its site was being read
        # A signal un-confirmed while the site was being read writes nothing.
        confirmed = await confirmed_codes(db, company.side)
        kept = [f for f in findings if f.code in confirmed]
        latest = (await _latest_values(db, confirmed, [company_id])).get(company_id, {})
        for finding in kept:
            value = _strip_nul(finding.value)
            if (
                fetched.status == "partial"
                and not is_present(finding.code, value)
                and is_present(finding.code, latest.get(finding.code))
            ):
                continue
            evidence = finding.evidence
            await db.execute(
                update(Observation)
                .where(
                    Observation.company_id == company_id,
                    Observation.code == finding.code,
                    Observation.is_latest,
                )
                .values(is_latest=False)
            )
            db.add(
                Observation(
                    company_id=company_id,
                    code=finding.code,
                    value=value,
                    evidence_url=_clip(evidence.url, MAX_URL) if evidence else None,
                    evidence_snippet=(
                        _clip(_strip_nul(evidence.snippet), MAX_SNIPPET) if evidence else None
                    ),
                    observed_at=now,
                    is_latest=True,
                )
            )
            latest[finding.code] = value
        result = score_company(
            latest, side=company.side, confirmed=confirmed, config=config.scoring
        )
        company.profile_status = fetched.status
        company.profile_error = fetched.error
        company.profiled_at = now
        company.pages_fetched = len(fetched.pages)
        company.profile_failures = 0
        company.profile_retry_at = None
        company.profile_holds = 0
        company.score = result.score
        company.disqualified = result.disqualified
        company.scored_at = now
        company.people_pages_read = analysis.people_pages
        company.contacts_extract_failed = analysis.contacts is None
        contacts = analysis.contacts
        if contacts is not None:
            await store_extraction(
                db,
                company,
                contacts.people,
                contacts.emails,
                contacts.form_url,
                side=company.side,
                config=config,
                now=now,
                partial=fetched.status == "partial",
            )
        await db.commit()
    positive = sum(1 for f in kept if is_present(f.code, f.value))
    return ProfileOutcome(
        company_id, fetched.status, len(fetched.pages), positive, result.score, fetched.error
    )


async def profile_company(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    *,
    fetcher: SafeFetcher,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    now: Callable[[], datetime],
) -> ProfileOutcome:
    """Read one company's website and record what its confirmed signals find.

    Excluded companies are never fetched. Fetching happens outside any transaction and is
    bounded by a per-company deadline; the results are written in one transaction at the end
    (and not at all if the company was excluded meanwhile)."""
    outcome = await _profile(
        db_factory,
        company_id,
        fetcher=fetcher,
        catalog=catalog,
        facts=facts,
        config=config,
        now=now,
        hold_unreachable=False,
    )
    return cast(ProfileOutcome, outcome)  # never _Held without hold_unreachable


async def _profile(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    *,
    fetcher: SafeFetcher,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    now: Callable[[], datetime],
    hold_unreachable: bool,
) -> ProfileOutcome | _Held:
    """``profile_company``; with ``hold_unreachable`` an unreachable homepage is returned as
    ``_Held`` instead of being recorded."""
    async with db_factory() as db:
        company = await db.get(Company, company_id)
        if company is None:
            raise LookupError("Company not found.")
        if company.status == "excluded":
            return _skipped(company_id)
        side, domain, website_url = company.side, company.domain, company.website_url
        company_name = company.name
        region = company.region
        codes = await confirmed_codes(db, side)
    if not codes:
        return _skipped(company_id, f"No {side} signals are confirmed.")

    profiling = config.profiling
    deadline = profiling.page_timeout_seconds * (
        profiling.max_pages_per_company + DEADLINE_EXTRA_PAGES
    )
    try:
        async with asyncio.timeout(deadline):
            fetched = await _read_site(
                fetcher, website_url, domain, max_pages=profiling.max_pages_per_company
            )
    except TimeoutError:
        fetched = _Fetched("failed", TIMED_OUT)

    if fetched.status not in ("ok", "partial"):
        if hold_unreachable and fetched.unreachable:
            return _Held(company_id, fetched)
        return await _write_failure(
            db_factory, company_id, status=fetched.status, error=fetched.error, now=now()
        )
    analysis = await asyncio.to_thread(
        _analyse,
        fetched.pages,
        side=side,
        domain=domain,
        company_name=company_name,
        codes=codes,
        catalog=catalog,
        facts=facts,
        config=config,
        region_fallback=region,
    )
    return await _write_results(db_factory, company_id, fetched, analysis, config=config, now=now())


def crash_frames(exc: BaseException) -> list[str]:
    """Where an exception came from as "file.py:line:function" (innermost last), without its
    message or any local values: those could carry page text."""
    frames = traceback.extract_tb(exc.__traceback__)[-MAX_CRASH_FRAMES:]
    return [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames]


async def _record_crash(
    db_factory: async_sessionmaker[AsyncSession],
    company_id: uuid.UUID,
    exc: Exception,
    *,
    now: datetime,
) -> ProfileOutcome:
    # Only the exception type: its message could carry page text.
    error = f"Profiling error ({type(exc).__name__})."
    log.warning(
        "profile_company_failed",
        company_id=str(company_id),
        error_type=type(exc).__name__,
        frames=crash_frames(exc),
    )
    try:
        async with db_factory() as db:
            await db.execute(
                update(Company)
                .where(Company.id == company_id, Company.status != "excluded")
                .values(
                    profile_status="failed",
                    profile_error=error,
                    profiled_at=now,
                    profile_failures=Company.profile_failures + 1,
                    profile_retry_at=None,
                    profile_holds=0,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as write_exc:
        log.warning(
            "profile_failure_not_recorded",
            company_id=str(company_id),
            error_type=type(write_exc).__name__,
        )
    return ProfileOutcome(company_id, "failed", 0, 0, None, error)


def _due(config: AppConfig, now: datetime) -> ColumnElement[bool]:
    """Pending companies not claimed in the last hour (Ruling N); any company profiled more than
    ``refresh_after_days`` ago; and a failed / no_site one sooner, after a backoff of 1 day
    after its first consecutive failure, then 7 days (capped at ``refresh_after_days``). None
    of them before its ``profile_retry_at``."""
    refresh = timedelta(days=config.profiling.refresh_after_days)
    first, second = (min(wait, refresh) for wait in FAILURE_BACKOFF)
    not_held = or_(Company.profile_retry_at.is_(None), Company.profile_retry_at <= now)
    due = or_(
        and_(
            Company.profile_status == "pending",
            or_(Company.profiled_at.is_(None), Company.profiled_at < now - CLAIM_RETRY),
        ),
        Company.profiled_at < now - refresh,
        and_(
            Company.profile_status.in_(TRANSIENT_STATUSES),
            or_(
                and_(Company.profile_failures <= 1, Company.profiled_at < now - first),
                and_(Company.profile_failures == 2, Company.profiled_at < now - second),
            ),
        ),
    )
    return and_(not_held, due)


async def _claim(
    db_factory: async_sessionmaker[AsyncSession], *, config: AppConfig, now: datetime, limit: int
) -> dict[uuid.UUID, _Claim]:
    """Due companies of a side with confirmed signals, pending first then oldest profile;
    marked ``profiled_at=now`` in one short transaction so neither a concurrent batch nor a
    restart right after a crash picks them again (Ruling N). Returns id -> what it looked like
    before (to release the claim), in claim order."""
    async with db_factory() as db:
        codes = await confirmed_codes(db)
        sides = sorted({by_code(c).side for c in codes})
        if not sides:
            return {}
        rows = (
            await db.execute(
                select(
                    Company.id, Company.profiled_at, Company.profile_status, Company.profile_holds
                )
                .where(
                    Company.status != "excluded",
                    Company.side.in_(sides),
                    _due(config, now),
                )
                .order_by(
                    case((Company.profile_status == "pending", 0), else_=1),
                    Company.profiled_at.asc().nulls_first(),
                    Company.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed = {cid: _Claim(at, status, holds) for cid, at, status, holds in rows}
        ids = list(claimed)
        if ids:
            await db.execute(
                update(Company)
                .where(Company.id.in_(ids))
                .values(profiled_at=now)
                .execution_options(synchronize_session=False)
            )
        await db.commit()
    return claimed


async def _release(
    db_factory: async_sessionmaker[AsyncSession],
    previous: Mapping[uuid.UUID, datetime | None],
    *,
    claimed_at: datetime,
) -> None:
    """Undo claims without recording anything: each company gets back the ``profiled_at`` it
    had (status untouched, ``profile_holds`` + 1), and isn't claimed again for ``NETWORK_RETRY``
    so the same unreachable companies can't take every pass. A company
    changed meanwhile (re-queued, excluded) is left alone."""
    async with db_factory() as db:
        for company_id, profiled_at in previous.items():
            await db.execute(
                update(Company)
                .where(
                    Company.id == company_id,
                    Company.profiled_at == claimed_at,
                    Company.status != "excluded",
                )
                .values(
                    profiled_at=profiled_at,
                    profile_retry_at=claimed_at + NETWORK_RETRY,
                    profile_holds=Company.profile_holds + 1,
                )
                .execution_options(synchronize_session=False)
            )
        await db.commit()


async def profile_batch(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    fetcher: SafeFetcher,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    now: Callable[[], datetime],
    limit: int,
) -> list[ProfileOutcome]:
    """Claim up to ``limit`` due companies and profile them, ``profiling.concurrency`` at a
    time. One company's crash becomes a ``failed`` outcome and never stops the batch. Returns
    ``[]`` (and fetches nothing) when no signal is confirmed. Outcomes are in claim order.

    Circuit breaker: when no site in the batch could be read and at least
    ``NETWORK_DOWN_MIN_BATCH`` of the unreachable ones (could not connect / timed out) weren't
    already failing, our network is the likelier culprit: nothing is recorded, the claims are
    released (tried again after ``NETWORK_RETRY``), and each comes back ``skipped`` with
    ``NETWORK_DOWN``. Companies already ``failed`` / ``no_site`` failing again are expected and
    are no evidence of an outage."""
    claimed_at = now()
    previous = await _claim(db_factory, config=config, now=claimed_at, limit=limit)
    if not previous:
        return []
    semaphore = asyncio.Semaphore(config.profiling.concurrency)

    async def one(company_id: uuid.UUID) -> ProfileOutcome | _Held:
        async with semaphore:
            try:
                return await _profile(
                    db_factory,
                    company_id,
                    fetcher=fetcher,
                    catalog=catalog,
                    facts=facts,
                    config=config,
                    now=now,
                    hold_unreachable=True,
                )
            except Exception as exc:
                return await _record_crash(db_factory, company_id, exc, now=now())

    results = list(await asyncio.gather(*(one(i) for i in previous)))
    held = [r for r in results if isinstance(r, _Held)]
    read = [r for r in results if isinstance(r, ProfileOutcome) and r.profile_status != SKIPPED]
    # Companies already held often enough are recorded as unreachable, not held again.
    capped = {h.company_id for h in held if previous[h.company_id].holds + 1 >= MAX_BREAKER_HOLDS}
    evidence = [
        h
        for h in held
        if h.company_id not in capped
        and previous[h.company_id].profile_status not in TRANSIENT_STATUSES
    ]
    if len(evidence) >= NETWORK_DOWN_MIN_BATCH and not read:
        to_hold = [h for h in held if h.company_id not in capped]
        await _release(
            db_factory,
            {h.company_id: previous[h.company_id].profiled_at for h in to_hold},
            claimed_at=claimed_at,
        )
        log.warning("profiling_network_down", companies=len(to_hold), recorded=len(capped))
        recorded: dict[uuid.UUID, ProfileOutcome] = {}
        for h in held:
            if h.company_id in capped:
                recorded[h.company_id] = await _record_held(db_factory, h, now=now, capped=True)
        final: list[ProfileOutcome] = []
        for r in results:
            if not isinstance(r, _Held):
                final.append(r)
            else:
                final.append(recorded.get(r.company_id) or _skipped(r.company_id, NETWORK_DOWN))
        return final
    outcomes: list[ProfileOutcome] = []
    for r in results:
        outcomes.append(await _record_held(db_factory, r, now=now) if isinstance(r, _Held) else r)
    return outcomes


async def _record_held(
    db_factory: async_sessionmaker[AsyncSession],
    held: _Held,
    *,
    now: Callable[[], datetime],
    capped: bool = False,
) -> ProfileOutcome:
    """Record a held unreachable homepage as the failure it is."""
    try:
        outcome = await _write_failure(
            db_factory,
            held.company_id,
            status=held.fetched.status,
            error=held.fetched.error,
            now=now(),
        )
    except Exception as exc:
        outcome = await _record_crash(db_factory, held.company_id, exc, now=now())
    return replace(outcome, capped=capped) if capped else outcome


# --- services: flush, callers commit -------------------------------------------------------------


async def rescore_all(db: AsyncSession, *, config: AppConfig, now: datetime) -> int:
    """Recompute score/disqualified/scored_at of every non-excluded company that has been
    scored or observed, from the latest observations of the currently confirmed codes (after a
    signal changed state). Set-based: one query for the companies, one for the latest values,
    then executemany UPDATEs in chunks; a company excluded meanwhile is never written. Returns
    how many companies were rescored. Flushes; the caller commits (which releases the scoring
    lock taken here)."""
    await db.execute(_SCORING_LOCK)
    confirmed = await confirmed_codes(db)
    observed = exists().where(Observation.company_id == Company.id)
    companies = (
        await db.execute(
            select(Company.id, Company.side).where(
                Company.status != "excluded",
                or_(Company.scored_at.is_not(None), observed),
            )
        )
    ).all()
    latest = await _latest_values(db, confirmed)
    params: list[dict[str, Any]] = []
    for company_id, side in companies:
        result = score_company(
            latest.get(company_id, {}), side=side, confirmed=confirmed, config=config.scoring
        )
        params.append(
            {
                "b_id": company_id,
                "score": result.score,
                "disqualified": result.disqualified,
                "scored_at": now,
            }
        )
    # Core UPDATE (not ORM bulk-by-PK) so the extra WHERE applies to every row.
    table = cast(Table, Company.__table__)
    stmt = (
        update(table)
        .where(table.c.id == bindparam("b_id"), table.c.status != "excluded")
        .values(
            score=bindparam("score"),
            disqualified=bindparam("disqualified"),
            scored_at=bindparam("scored_at"),
        )
    )
    for start in range(0, len(params), RESCORE_CHUNK):
        await db.execute(stmt, params[start : start + RESCORE_CHUNK])
    await db.flush()
    return len(params)


async def mark_side_due(db: AsyncSession, side: str) -> int:
    """Queue every non-excluded ``side`` company profiled before (any status but ``pending``)
    for re-profiling, so a newly confirmed signal gets filled in as the worker re-reads their
    sites. Companies still pending (never profiled, or being profiled right now) are left
    alone. Returns how many were queued. Flushes."""
    result = await db.execute(
        update(Company)
        .where(
            Company.side == side,
            Company.status != "excluded",
            Company.profile_status != "pending",
        )
        .values(profile_status="pending", profiled_at=None, profile_retry_at=None)
        .returning(Company.id)
        .execution_options(synchronize_session=False)
    )
    count = len(result.scalars().all())
    await db.flush()
    return count


@dataclass(frozen=True)
class SignalChanges:
    changed: dict[str, list[str]]  # new state -> codes that moved to it
    queued: int  # companies queued for re-profiling (newly confirmed signals)
    rescored: int


async def change_signal_states(
    db: AsyncSession,
    wanted: Mapping[str, str],
    *,
    actor: User | None,
    config: AppConfig,
    now: datetime,
    ip: str | None = None,
) -> SignalChanges:
    """Set signal states (code -> state), audit each real change (``signal.state_changed``,
    old/new), queue the side's profiled companies again for every side that gained a confirmed
    signal (Ruling W), and rescore everyone. Unchanged codes are no-ops; nothing at all changed
    means no rescore. Raises ValueError for an unknown code or state. Flushes; caller commits."""
    current = await states(db)
    changed: dict[str, list[str]] = {}
    sides_gained: set[str] = set()
    for code, state in wanted.items():
        if code not in current:
            raise ValueError(f"unknown signal code {code!r}")
        old = current[code]
        if old == state:
            continue
        await set_state(db, code, state, actor=actor)
        await audit.record(
            db,
            action=audit.SIGNAL_STATE_CHANGED,
            actor_id=actor.id if actor else None,
            target_type="signal",
            target_id=code,
            details={"code": code, "old": old, "new": state},
            ip=ip,
        )
        changed.setdefault(state, []).append(code)
        if state == "confirmed":
            sides_gained.add(by_code(code).side)
    if not changed:
        return SignalChanges({}, 0, 0)
    queued = 0
    for side in sorted(sides_gained):
        queued += await mark_side_due(db, side)
    rescored = await rescore_all(db, config=config, now=now)
    return SignalChanges(changed, queued, rescored)


async def request_reprofile(db: AsyncSession, company_id: uuid.UUID, *, actor: User | None) -> None:
    """Queue a company for the worker's next pass (``pending`` with no claim, so it is picked
    at once). Refuses excluded companies. The caller audits and commits; flushes."""
    company = await _locked_company(db, company_id)
    if company is None:
        raise LookupError("Company not found.")
    if company.status == "excluded":
        raise ValueError("This company is excluded and is never profiled.")
    company.profile_status = "pending"
    company.profiled_at = None
    company.profile_retry_at = None
    await db.flush()
    log.info(
        "reprofile_requested",
        company_id=str(company_id),
        actor_id=str(actor.id) if actor else None,
    )


async def observed_counts(db: AsyncSession) -> dict[str, int]:
    """code -> how many non-excluded companies' latest observation of it found something."""
    latest = await _latest_values(db, [s.code for s in SIGNALS])
    counts = dict.fromkeys((s.code for s in SIGNALS), 0)
    for values in latest.values():
        for code, value in values.items():
            if is_present(code, value):
                counts[code] += 1
    return counts
