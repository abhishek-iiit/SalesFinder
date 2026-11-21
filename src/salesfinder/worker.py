"""The background worker: reap stuck work, sweep exclusions, run a due HubSpot sync, read the
mailboxes (replies, bounces, unsubscribes, takeovers), classify replies and draft answers
(into the Outbox), send approved emails (at most one per mailbox per pass), queue due
follow-ups for review, run one approved job, profile a batch of
companies, check a batch of companies' contacts, and draft emails for active campaigns (into
the Outbox).

Run it next to the web server (``salesfinder worker``). It never starts discovery on its own:
only a job a teammate approved is ever claimed.

Every pass is recorded in the heartbeat log (``ops.runs``): the current ``runs`` row gets the
pass's log-safe lines and failures, and a background beat keeps its liveness and current step
fresh during long steps. Recording never stops the worker: a failed ``runs`` write is logged
(type only) and the work goes on. During quiet hours (``usage.quiet_hours``) the Claude steps
(Replies and Writing) wait; everything else keeps running.
"""

import asyncio
import contextlib
import os
import random
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from salesfinder.brain.runtime import ClaudeRuntime
from salesfinder.campaigns.service import DraftOutcome, draft_batch
from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, Resolve
from salesfinder.contacts.service import EXCLUDED, VerifyOutcome, verify_batch
from salesfinder.contacts.verifier import MillionVerifier
from salesfinder.db.models import Run
from salesfinder.discovery.jobs import claim_next_job, fail_interrupted_jobs, run_job
from salesfinder.discovery.serper import SerperClient
from salesfinder.exclusions.sweep import sweep_exclusions
from salesfinder.exclusions.sync import due_for_sync, fail_interrupted_syncs, run_sync
from salesfinder.followups.service import queue_due
from salesfinder.mail.factory import MailFactory
from salesfinder.ops import runs as runs_lines
from salesfinder.ops import version as ops_version
from salesfinder.ops.quiet import quiet_line, quiet_period
from salesfinder.ops.runs import (
    PassResult,
    RunNotOpen,
    beat,
    finish_run,
    record_pass,
    start_run,
)
from salesfinder.outreach.stop import stop_excluded
from salesfinder.profiling.fetch import Resolver, SafeFetcher
from salesfinder.profiling.service import NETWORK_DOWN, SKIPPED, ProfileOutcome, profile_batch
from salesfinder.replies.classify import RepliesOutcome, handle_replies
from salesfinder.replies.ingest import PollOutcome, poll_mailboxes
from salesfinder.sending.sender import SendOutcome, pause_line, send_pass
from salesfinder.settings import Settings
from salesfinder.suppressions.service import refresh_suppressed_blocks

log = structlog.get_logger(__name__)

NO_SERPER_KEY = "SALESFINDER_SERPER_API_KEY is not set."
NO_VERIFIER_KEY = "Email verifier configured but SALESFINDER_EMAIL_VERIFIER_KEY is not set."
VERIFIER_FAILED = "Email verifier failed — check the key and credits."
# pg_try_advisory_lock key held by `salesfinder worker` for its lifetime (one worker at a time).
WORKER_LOCK_KEY = 0x53_46_57_4B  # "SFWK"
NO_SEND_LINE = runs_lines.NO_SEND_LINE
ANOTHER_WORKER = "Another SalesFinder worker is already running."
LOCK_LOST = runs_lines.LOCK_LOST_LINE
# Longest a lock check may take before the lock counts as lost.
LOCK_CHECK_SECONDS = 30.0
# Longest the beat task gets to finish an in-flight beat when it is stopped.
BEAT_STOP_SECONDS = 5.0
BEAT_TASK = "salesfinder-beat"
# The steps that call Claude; they wait during quiet hours (nothing else does).
CLAUDE_STEPS = frozenset({"Replies", "Writing"})


class AnotherWorkerRunning(Exception):
    """Another worker's ``runs`` row is open (``uq_runs_one_open`` refused ours): this process
    no longer runs alone (its advisory lock was lost, e.g. Postgres restarted under it, and a
    second worker started). It stops rather than work alongside the one holding the lock."""


@dataclass
class WorkerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    config: AppConfig
    catalog: Catalog
    facts: Facts
    now: Callable[[], datetime]
    hubspot_transport: httpx.AsyncBaseTransport | None = None
    serper_transport: httpx.AsyncBaseTransport | None = None
    # Tests inject a fake web; None means the real network and DNS.
    web_transport: httpx.AsyncBaseTransport | None = None
    web_resolver: Resolver | None = None
    # Tests inject fake MX answers and a fake verifier API; None means real DNS / the real API.
    dns_resolve: Resolve | None = None
    verifier_transport: httpx.AsyncBaseTransport | None = None
    # The isolated Claude CLI runtime; built from settings on first use and kept, so its
    # isolation-flag check runs once per process.
    brain: ClaudeRuntime | None = None
    # When this process last ran the writing step (it runs at most once every
    # ``writing.min_interval_minutes``).
    last_writing_at: datetime | None = None
    # SMTP/IMAP for sending and reading mail; None means the Mail and Sending steps do
    # nothing (tests inject fakes).
    mail: MailFactory | None = None
    # When this process last read the mailboxes (at most once every
    # ``mail.poll_interval_minutes``).
    last_mail_at: datetime | None = None
    # Random spacing between one mailbox's sends.
    rng: random.Random = field(default_factory=random.Random)
    # ``--no-send``: the Sending step never runs (said once per runs row: ``no_send_noted``
    # and ``no_send_noted_run``).
    send_enabled: bool = True
    # This worker's open ``runs`` row (None before it opens, or when opening it failed).
    run_id: uuid.UUID | None = None
    # The step running now and since when (for the beat; None between steps).
    current_step: str | None = None
    step_started_at: datetime | None = None
    # The quiet period (its local start date) the quiet-hours line was last said for.
    quiet_noted: date | None = None
    no_send_noted: bool = False
    no_send_noted_run: uuid.UUID | None = None
    # The global pause line (its runs row and reason) last said: said once per runs row while
    # the same pause holds approved mail, not once per pass.
    pause_noted: tuple[uuid.UUID | None, str] | None = None
    # Per mailbox, the "Held: <mailbox> — <reason>" line last said (its runs row and reason):
    # said once per runs row and reason while the hold lasts, like the pause line. A None row
    # (no row open yet) is the row ``record`` opens next.
    held_noted: dict[str, tuple[uuid.UUID | None, str]] = field(default_factory=dict)
    # The code this process runs (stored on its ``runs`` rows; the web compares it).
    code_version: str = field(default_factory=ops_version.code_version)
    # Whether this process still holds the single-worker advisory lock (the CLI passes a
    # check on its lock connection; None: no lock to check, e.g. tests). Checked every beat.
    lock_alive: Callable[[], Awaitable[bool]] | None = None
    # A beat found the lock gone: the worker stops after the current pass (exit 1).
    lock_lost: bool = False
    # When this process started (set from ``now`` when the deps are made): an open runs row
    # with this host and pid that started earlier is another process's (pid reuse after a
    # reboot) and is never adopted.
    process_started_at: datetime | None = None
    # Set when the worker is asked to stop (SIGTERM/SIGINT, or the lock lost): Replies,
    # Writing and Discovery start no new item, so the pass ends soon and launchd never has to
    # SIGKILL it mid-pass (Ruling P7-G). A Claude call already running finishes.
    stopping: asyncio.Event | None = None

    def __post_init__(self) -> None:
        if self.process_started_at is None:
            self.process_started_at = self.now()


async def _reap(deps: WorkerDeps) -> list[str]:
    async with deps.session_factory() as db:
        jobs = await fail_interrupted_jobs(db, now=deps.now())
        syncs = await fail_interrupted_syncs(db, now=deps.now())
        await db.commit()
    lines = []
    if jobs:
        lines.append(f"Failed {jobs} interrupted discovery job(s).")
    if syncs:
        lines.append(f"Failed {syncs} interrupted HubSpot sync(s).")
    return lines


async def _sweep(deps: WorkerDeps) -> list[str]:
    # Catch do-not-contact entries added (e.g. while a job was running) and anything the
    # exclusion lists learned since a company was stored. Its own transaction: a failure here
    # never rolls back the reaper, and vice versa.
    async with deps.session_factory() as db:
        excluded = await sweep_exclusions(db)
        await db.commit()
    lines: list[str] = []
    if excluded:
        lines.append(f"Excluded {excluded} stored company(ies) now on an exclusion list.")
    # Block stored addresses suppressed while their caller held outreach locks (suppress with
    # refresh=False). Its own transaction, holding no outreach locks (it locks companies).
    async with deps.session_factory() as db:
        await refresh_suppressed_blocks(db)
        await db.commit()
    # Then stop sequences already drafted to anyone now excluded (one short transaction each).
    stopped = await stop_excluded(deps.session_factory, now=deps.now())
    if stopped:
        lines.append(f"Stopped {stopped} sequence(s) now excluded.")
    return lines


async def _hubspot(deps: WorkerDeps) -> list[str]:
    token = deps.settings.hubspot_token
    if token is None or _stopping(deps):
        return []  # stopping: a due sync runs after the next start
    async with deps.session_factory() as db:
        due = await due_for_sync(db, now=deps.now(), config=deps.config.hubspot)
    if not due:
        return []
    row = await run_sync(
        deps.session_factory,
        token=token.get_secret_value(),
        now=deps.now,
        transport=deps.hubspot_transport,
    )
    if row.status == "succeeded":
        return [
            f"HubSpot sync succeeded: {row.companies_count} companies, "
            f"{row.contacts_count} contacts."
        ]
    return [f"HubSpot sync failed: {row.error}"]


AUTO_PAUSED_PREFIX = "Paused automatically: "


def mail_lines(outcomes: Mapping[str, PollOutcome]) -> list[str]:
    """E.g. "Read mail: 2 repl(ies), 1 bounce(s), 0 unsubscribe(s), 0 takeover(s)." (counts
    only; omitted when none of these happened), then "Sending paused automatically: <why>"
    when a bounce read here tripped the kill switch, then "Mail read error for <mailbox>:
    <error>" for each mailbox with an error and "Not reading <mailbox>: test its connection
    first." for each one held untested (all our own text and the mailbox address)."""
    replies = sum(o.replies for o in outcomes.values())
    bounces = sum(o.bounces for o in outcomes.values())
    unsubscribes = sum(o.unsubscribes for o in outcomes.values())
    takeovers = sum(o.takeovers for o in outcomes.values())
    lines: list[str] = []
    if replies or bounces or unsubscribes or takeovers:
        lines.append(
            f"Read mail: {replies} repl(ies), {bounces} bounce(s), "
            f"{unsubscribes} unsubscribe(s), {takeovers} takeover(s)."
        )
    paused = next((o.paused for o in outcomes.values() if o.paused), None)
    if paused is not None:
        lines.append(f"Sending paused automatically: {paused.removeprefix(AUTO_PAUSED_PREFIX)}")
    lines += [
        f"Mail read error for {address}: {o.error}"
        for address, o in outcomes.items()
        if o.error is not None
    ]
    lines += [
        f"Not reading {address}: test its connection first."
        for address, o in outcomes.items()
        if o.untested
    ]
    return lines


async def _mail(deps: WorkerDeps) -> list[str]:
    if deps.mail is None:
        return []
    now = deps.now()
    interval = timedelta(minutes=deps.config.mail.poll_interval_minutes)
    if deps.last_mail_at is not None and now - deps.last_mail_at < interval:
        return []
    deps.last_mail_at = now
    outcomes = await poll_mailboxes(
        deps.session_factory,
        mail=deps.mail,
        settings=deps.settings,
        config=deps.config,
        now=deps.now,
    )
    return mail_lines(outcomes)


def replies_lines(outcome: RepliesOutcome) -> list[str]:
    """E.g. "Classified 3 repl(ies); drafted 2 answer(s) for review (1 held by the gate); 1
    need you." (omitted when nothing happened), then "Replies stopped: <reason>" when the pass
    stopped early (the budget, Claude unusable). Counts and our own reasons only."""
    lines: list[str] = []
    if outcome.classified or outcome.drafted or outcome.needs_you:
        lines.append(
            f"Classified {outcome.classified} repl(ies); drafted {outcome.drafted} answer(s) "
            f"for review ({outcome.held} held by the gate); {outcome.needs_you} need you."
        )
    if outcome.stopped_reason is not None:
        lines.append(f"Replies stopped: {outcome.stopped_reason}")
    return lines


def _stopping(deps: WorkerDeps) -> bool:
    return deps.stopping is not None and deps.stopping.is_set()


async def _replies(deps: WorkerDeps) -> list[str]:
    # Claude classifies and drafts with no transaction open; answers wait in the Outbox.
    if _stopping(deps):
        return []
    if deps.brain is None:
        deps.brain = ClaudeRuntime(deps.settings, deps.config.brain)
    now = deps.now()
    outcome = await handle_replies(
        deps.session_factory,
        runtime=deps.brain,
        catalog=deps.catalog,
        facts=deps.facts,
        config=deps.config,
        now=deps.now,
        # Time-boxed so sending (the next step) is never long in coming.
        deadline=now + timedelta(seconds=deps.config.replies.max_pass_seconds),
        mx_checker=MxChecker(deps.config.contacts, resolve=deps.dns_resolve),
        stop=deps.stopping,
    )
    return replies_lines(outcome)


def _held_line(mailbox: str, reason: str) -> str:
    return f"Held: {mailbox} — {reason}"


def sending_lines(outcome: SendOutcome) -> list[str]:
    """E.g. "Sent 2 email(s).", the global pause (once, only when approved mail waits), each
    mailbox's hold, and sends that couldn't be confirmed. Counts and our own reasons only."""
    lines: list[str] = []
    if outcome.sent:
        lines.append(f"Sent {outcome.sent} email(s).")
    if outcome.paused is not None:
        lines.append(pause_line(outcome.paused))
    lines += [_held_line(mailbox, reason) for mailbox, reason in outcome.held.items()]
    if outcome.unconfirmed:
        lines.append(f"{outcome.unconfirmed} send(s) couldn't be confirmed — check the Outbox.")
    return lines


async def _sending(deps: WorkerDeps) -> list[str]:
    if not deps.send_enabled:
        # --no-send: never reaches send_pass (no SMTP at all, and no reconcile either),
        # whatever the paused state says. Said once per runs row.
        if deps.no_send_noted and deps.no_send_noted_run == deps.run_id:
            return []
        deps.no_send_noted, deps.no_send_noted_run = True, deps.run_id
        return [NO_SEND_LINE]
    if deps.mail is None:
        return []
    outcome = await send_pass(
        deps.session_factory,
        mail=deps.mail,
        settings=deps.settings,
        config=deps.config,
        facts=deps.facts,
        catalog=deps.catalog,
        now=deps.now,
        mx_checker=MxChecker(deps.config.contacts, resolve=deps.dns_resolve),
        rng=deps.rng,
    )
    return _once_per_row(deps, outcome, sending_lines(outcome))


def _once_per_row(deps: WorkerDeps, outcome: SendOutcome, lines: list[str]) -> list[str]:
    """Drop the pause line and each "Held: …" line already said in this runs row for the same
    reason: every pass would otherwise repeat them (~90 lines per 15-minute row), crowding out
    the row's other lines. A pause or hold that lifts is forgotten, so it is said again when
    it comes back."""
    if outcome.paused is None:
        deps.pause_noted = None  # a later pause is said again
    else:
        noted = (deps.run_id, outcome.paused)
        if deps.pause_noted == noted:
            lines.remove(pause_line(outcome.paused))
        deps.pause_noted = noted
    held: dict[str, tuple[uuid.UUID | None, str]] = {}
    for mailbox, reason in outcome.held.items():
        held[mailbox] = (deps.run_id, reason)
        if deps.held_noted.get(mailbox) == held[mailbox]:
            lines.remove(_held_line(mailbox, reason))
    deps.held_noted = held  # mailboxes no longer held are forgotten
    return lines


def followups_line(queued: int, held: int) -> str:
    """The Follow-ups step's line, e.g. "Queued 2 follow-up(s) for review (1 held by the
    gate)." (counts only)."""
    return f"Queued {queued} follow-up(s) for review ({held} held by the gate)."


async def _followups(deps: WorkerDeps) -> list[str]:
    # Gated into the Outbox for a teammate; never sent from here.
    queued, held = await queue_due(
        deps.session_factory,
        now=deps.now,
        config=deps.config,
        facts=deps.facts,
        catalog=deps.catalog,
        mx_checker=MxChecker(deps.config.contacts, resolve=deps.dns_resolve),
    )
    return [followups_line(queued, held)] if queued else []


async def _discovery(deps: WorkerDeps) -> list[str]:
    if _stopping(deps):
        return []  # no new job while stopping; an approved one waits for the next start
    key = deps.settings.serper_api_key
    async with deps.session_factory() as db:
        job = await claim_next_job(db, now=deps.now())
        if job is None:
            return []
        job_id = job.id
        if key is None:
            job.status = "failed"
            job.error = NO_SERPER_KEY
            job.finished_at = deps.now()
            await db.flush()
        await db.commit()
    if key is None:
        return [f"Discovery job {job_id} failed: {NO_SERPER_KEY}"]
    async with SerperClient(key.get_secret_value(), transport=deps.serper_transport) as client:
        done = await run_job(
            deps.session_factory,
            job_id,
            client=client,
            catalog=deps.catalog,
            config=deps.config.discovery,
            now=deps.now,
            stop=deps.stopping,
        )
    detail = f": {done.error}" if done.error else ""
    return [
        f"Discovery job {job_id} {done.status}: {done.searches_done} searches, "
        f"{done.companies_new} new, {done.companies_excluded} excluded{detail}"
    ]


OK_STATUSES = frozenset({"ok", "partial"})


def profiled_line(statuses: list[str]) -> str:
    """E.g. "Profiled 7 companies (6 ok, 1 failed)."; ok counts ok + partial, failed the rest.
    Skipped outcomes (excluded while being read) are left out of the totals and noted after."""
    done = [s for s in statuses if s != SKIPPED]
    ok = sum(1 for s in done if s in OK_STATUSES)
    noun = "company" if len(done) == 1 else "companies"
    line = f"Profiled {len(done)} {noun} ({ok} ok, {len(done) - ok} failed)."
    skipped = len(statuses) - len(done)
    if skipped:
        line += f" {skipped} skipped (excluded meanwhile)."
    return line


def outcomes_line(outcomes: Sequence[ProfileOutcome]) -> str:
    """The worker line for a profiling batch: ``NETWORK_DOWN`` when the circuit breaker held
    the batch back, else ``profiled_line``."""
    if any(o.error == NETWORK_DOWN for o in outcomes):
        capped = sum(1 for o in outcomes if o.capped)
        if capped:
            return f"{NETWORK_DOWN} {capped} recorded as unreachable after repeated checks."
        return NETWORK_DOWN
    return profiled_line([o.profile_status for o in outcomes])


async def _profiling(deps: WorkerDeps) -> list[str]:
    if _stopping(deps):
        return []  # due-based: the batch runs after the next start (Ruling P7-G)
    async with SafeFetcher(
        deps.config.profiling, transport=deps.web_transport, resolver=deps.web_resolver
    ) as fetcher:
        outcomes = await profile_batch(
            deps.session_factory,
            fetcher=fetcher,
            catalog=deps.catalog,
            facts=deps.facts,
            config=deps.config,
            now=deps.now,
            limit=deps.config.profiling.batch_size,
        )
    if not outcomes:
        return []
    return [outcomes_line(outcomes)]


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def contacts_line(outcomes: Sequence[VerifyOutcome], *, verifier_failed: bool = False) -> str:
    """E.g. "Checked contacts for 3 companies (4 sendable addresses).", plus
    " (1 couldn't be checked)" when DNS couldn't answer or a check crashed (Ruling AJ), and
    ``VERIFIER_FAILED`` when a paid-verifier call failed. Companies excluded meanwhile aren't
    counted."""
    done = [o for o in outcomes if o.error != EXCLUDED]
    companies = _plural(len(done), "company", "companies")
    sendable = _plural(sum(o.sendable for o in done), "sendable address", "sendable addresses")
    line = f"Checked contacts for {companies} ({sendable})."
    unchecked = sum(1 for o in done if o.error is not None)
    if unchecked:
        line += f" ({unchecked} couldn't be checked)"
    if verifier_failed:
        line += f" {VERIFIER_FAILED}"
    return line


def build_verifier(
    config: AppConfig, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[MillionVerifier | None, str | None]:
    """The paid verifier when one is configured and its key is set, else None; plus a note
    when it is configured without a key."""
    if config.contacts.verifier != "millionverifier":
        return None, None
    key = settings.email_verifier_key
    if key is None:
        return None, NO_VERIFIER_KEY
    return MillionVerifier(key.get_secret_value(), transport=transport), None


@dataclass(frozen=True)
class ContactsRun:
    outcomes: list[VerifyOutcome]
    note: str | None  # the verifier is configured without a key
    verifier_failed: bool  # at least one paid-verifier call failed

    @property
    def line(self) -> str:
        return contacts_line(self.outcomes, verifier_failed=self.verifier_failed)


async def run_contacts_batch(deps: WorkerDeps, *, limit: int) -> ContactsRun:
    """One ``verify_batch`` with the configured checker / verifier."""
    verifier, note = build_verifier(deps.config, deps.settings, transport=deps.verifier_transport)
    checker = MxChecker(deps.config.contacts, resolve=deps.dns_resolve)
    try:
        outcomes = await verify_batch(
            deps.session_factory,
            checker=checker,
            verifier=verifier,
            config=deps.config,
            now=deps.now(),
            limit=limit,
        )
    finally:
        if verifier is not None:
            await verifier.aclose()
    failed = verifier is not None and verifier.failures > 0
    return ContactsRun(outcomes, note, failed)


async def _contacts(deps: WorkerDeps) -> list[str]:
    if _stopping(deps):
        return []  # due-based: the batch runs after the next start (Ruling P7-G)
    run = await run_contacts_batch(deps, limit=deps.config.contacts.batch_size)
    if not run.outcomes:
        return []  # nothing due: stay quiet (the note too, or it would repeat every pass)
    return [run.line] + ([run.note] if run.note else [])


def writing_lines(outcome: DraftOutcome) -> list[str]:
    """E.g. "Drafted 3 emails for review (1 held by the gate)." (omitted when nothing was
    drafted and nothing stopped), then "Writing stopped: <reason>" when the pass stopped."""
    lines: list[str] = []
    if outcome.drafted or outcome.stopped_reason is not None:
        emails = _plural(outcome.drafted, "email", "emails")
        lines.append(f"Drafted {emails} for review ({outcome.gate_failed} held by the gate).")
    if outcome.stopped_reason is not None:
        lines.append(f"Writing stopped: {outcome.stopped_reason}")
    return lines


async def _writing(deps: WorkerDeps) -> list[str]:
    if _stopping(deps):
        return []
    now = deps.now()
    interval = timedelta(minutes=deps.config.writing.min_interval_minutes)
    if deps.last_writing_at is not None and now - deps.last_writing_at < interval:
        return []
    deps.last_writing_at = now
    if deps.brain is None:
        deps.brain = ClaudeRuntime(deps.settings, deps.config.brain)
    outcome = await draft_batch(
        deps.session_factory,
        runtime=deps.brain,
        catalog=deps.catalog,
        facts=deps.facts,
        config=deps.config,
        now=deps.now,
        limit=deps.config.writing.batch_size,
        mx_checker=MxChecker(deps.config.contacts, resolve=deps.dns_resolve),
        # Time-boxed so the next pass (and its sends) is never long in coming.
        deadline=now + timedelta(seconds=deps.config.writing.max_pass_seconds),
        stop=deps.stopping,
    )
    if outcome.errors:
        # Counts only: an error names a company's domain.
        log.warning("writing_errors", count=len(outcome.errors))
    return writing_lines(outcome)


async def _step(
    name: str,
    step: Callable[[WorkerDeps], Awaitable[list[str]]],
    deps: WorkerDeps,
    failures: list[str] | None = None,
) -> list[str]:
    deps.current_step, deps.step_started_at = name, deps.now()
    try:
        return await step(deps)
    except Exception as exc:
        log.warning("worker_step_failed", step=name, error_type=type(exc).__name__)
        # The type only: an exception's message can carry page text or a provider's reply.
        line = f"{name} step failed: {type(exc).__name__}"
        if failures is not None:
            failures.append(line)
        return [line]
    finally:
        deps.current_step, deps.step_started_at = None, None


async def run_pass(deps: WorkerDeps) -> PassResult:
    """One pass; each step is isolated so a failure in one never blocks the next. In quiet
    hours the Claude steps (Replies, Writing) are skipped and the quiet line is said once per
    quiet period; mail reading, sending and everything else run as always."""
    period = quiet_period(deps.now(), deps.config.usage.quiet_hours)
    quiet = period is not None
    lines: list[str] = []
    failures: list[str] = []
    if period is not None and deps.quiet_noted != period:
        deps.quiet_noted = period
        lines.append(quiet_line(deps.config.usage.quiet_hours))
    # Looked up at call time (tests replace single steps).
    steps: list[tuple[str, Callable[[WorkerDeps], Awaitable[list[str]]]]] = [
        ("Reaper", _reap),
        ("Sweep", _sweep),
        ("HubSpot", _hubspot),
        ("Mail", _mail),
        ("Replies", _replies),
        ("Sending", _sending),
        ("Follow-ups", _followups),
        ("Discovery", _discovery),
        ("Profiling", _profiling),
        ("Contacts", _contacts),
        ("Writing", _writing),
    ]
    for name, step in steps:
        if quiet and name in CLAUDE_STEPS:
            continue
        lines += await _step(name, step, deps, failures)
    return PassResult(lines=lines, failures=failures, quiet=quiet)


async def run_once(deps: WorkerDeps) -> list[str]:
    """One pass's lines (``run_pass``, unrecorded)."""
    return (await run_pass(deps)).lines


# --------------------------------------------------------------------------- the runs log
# Each write is its own short transaction on its own session: never inside a step's
# transaction, never while an outreach lock is held, never across network I/O.


def _identity() -> tuple[str, int]:
    """This process as its ``runs`` rows record it (host, pid)."""
    return socket.gethostname()[:255], os.getpid()


async def _own_open_run(db: AsyncSession, *, since: datetime | None) -> uuid.UUID | None:
    """The open run, if this process opened it: same host and pid, and started no earlier
    than this process (``since``; after a reboot the same pid can be another process's)."""
    run = (await db.execute(select(Run).where(Run.finished_at.is_(None)))).scalar_one_or_none()
    if run is None or (run.host, run.pid) != _identity():
        return None
    if since is None or run.started_at < since:
        return None
    return run.id


async def _open_run(deps: WorkerDeps, *, close_crashed: bool) -> uuid.UUID:
    """Open this worker's ``runs`` row (``close_crashed``: first close a dead worker's) and
    keep its id. If another open row refuses ours (IntegrityError, rolled back) and this
    process opened it (e.g. a rollover whose COMMIT went through but whose acknowledgement was
    lost), adopt it; otherwise AnotherWorkerRunning."""
    host, pid = _identity()
    async with deps.session_factory() as db:
        try:
            run = await start_run(
                db,
                now=deps.now(),
                host=host,
                pid=pid,
                code_version=deps.code_version,
                close_crashed=close_crashed,
            )
            await db.commit()
        except IntegrityError as exc:
            # The only constraint a new runs row can break is uq_runs_one_open.
            await db.rollback()
            own = await _own_open_run(db, since=deps.process_started_at)
            if own is None:
                log.warning("run_refused_another_worker_running")
                raise AnotherWorkerRunning from exc
            log.warning("run_adopted")
            deps.run_id = own
            return own
    deps.run_id = run.id
    return run.id


async def _holds_lock(deps: WorkerDeps) -> bool:
    """Whether the lock check passes (False without one, or when it fails)."""
    if deps.lock_alive is None:
        return False
    try:
        return await deps.lock_alive()
    except Exception as exc:
        log.warning("worker_lock_check_failed", error_type=type(exc).__name__)
        return False


async def start(deps: WorkerDeps) -> None:
    """Open the worker's run (right after taking the lock), closing a dead worker's as
    ``crashed``. A failure (other than AnotherWorkerRunning) is logged by type and left for
    ``record`` to retry."""
    try:
        await _open_run(deps, close_crashed=True)
    except AnotherWorkerRunning:
        raise
    except Exception as exc:
        log.warning("run_start_failed", error_type=type(exc).__name__)


async def _record_pass(deps: WorkerDeps, run_id: uuid.UUID, result: PassResult) -> uuid.UUID:
    # A --no-send worker's next row starts with its line (health reads the open row).
    carry = () if deps.send_enabled else (NO_SEND_LINE,)
    async with deps.session_factory() as db:
        new_id = await record_pass(
            db, run_id, result, now=deps.now(), config=deps.config, carry=carry
        )
        await db.commit()
    if new_id != run_id and carry:
        deps.no_send_noted, deps.no_send_noted_run = True, new_id
    return new_id


def _claim_unkeyed_notes(deps: WorkerDeps, run_id: uuid.UUID) -> None:
    """Lines said while no row was open (the start's runs write failed) are recorded in the
    row ``record`` just opened: note them under it, so the next pass doesn't say them again
    there."""
    if deps.no_send_noted and deps.no_send_noted_run is None:
        deps.no_send_noted_run = run_id
    if deps.pause_noted is not None and deps.pause_noted[0] is None:
        deps.pause_noted = (run_id, deps.pause_noted[1])
    deps.held_noted = {
        mailbox: (run_id if noted_run is None else noted_run, reason)
        for mailbox, (noted_run, reason) in deps.held_noted.items()
    }


async def record(deps: WorkerDeps, result: PassResult) -> None:
    """Record one pass in the worker's run (rolling over per window); never raises, except
    AnotherWorkerRunning.

    - No run yet (opening it failed at start): open it now. A dead worker's run is
      crash-closed only while the lock check passes (Ruling P7-C); otherwise no crash-close.
    - Our run was closed under us (RunNotOpen: e.g. Postgres restarted, the advisory lock
      dropped and another process crash-closed it): open a new run *without* crash-closing
      anything, and record the pass there.
    - Either way, an open row refusing ours is adopted if this process opened it, else
      another worker is running: AnotherWorkerRunning.
    - Anything else: logged as ``run_record_failed`` with the exception type only."""
    try:
        run_id = deps.run_id
        opened = run_id is None
        if run_id is None:
            run_id = await _open_run(deps, close_crashed=await _holds_lock(deps))
        try:
            new_id = await _record_pass(deps, run_id, result)
            if opened:
                _claim_unkeyed_notes(deps, run_id)
            deps.run_id = new_id
        except RunNotOpen:
            # deps.run_id keeps the lost run's id until a new one opens: if this reopen
            # fails, the next record tries it again, still without crash-closing.
            log.warning("run_reopened")
            run_id = await _open_run(deps, close_crashed=False)
            deps.run_id = await _record_pass(deps, run_id, result)
    except AnotherWorkerRunning:
        deps.run_id = None
        raise
    except Exception as exc:
        log.warning("run_record_failed", error_type=type(exc).__name__)
        # This pass's lines weren't recorded: the once-per-row lines among them are said
        # again next pass.
        _forget_notes(deps)


def _forget_notes(deps: WorkerDeps) -> None:
    deps.no_send_noted, deps.no_send_noted_run = False, None
    deps.pause_noted = None
    deps.held_noted = {}


async def beat_once(deps: WorkerDeps) -> None:
    """Refresh the run's liveness and current step (its own transaction)."""
    if deps.run_id is None:
        return
    async with deps.session_factory() as db:
        await beat(
            db,
            deps.run_id,
            now=deps.now(),
            step=deps.current_step,
            step_started_at=deps.step_started_at,
        )
        await db.commit()


async def _beat_loop(
    deps: WorkerDeps, stop: asyncio.Event, *, shutdown: asyncio.Event | None = None
) -> None:
    """Every ``ops.beat_seconds`` until ``stop`` is set: check the worker lock, then beat. A
    failed beat is logged by type and the next one tries again. When the lock is gone (Ruling
    P7-C: never run unlocked) ``deps.lock_lost`` is set and ``shutdown`` (the worker's stop)
    is set; beats go on while the current pass drains."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=deps.config.ops.beat_seconds)
        if stop.is_set():
            return
        if deps.lock_alive is not None and not deps.lock_lost and not await _holds_lock(deps):
            log.warning("worker_lock_lost")
            deps.lock_lost = True
            if shutdown is not None:
                shutdown.set()
        try:
            await beat_once(deps)
        except Exception as exc:
            log.warning("run_beat_failed", error_type=type(exc).__name__)


async def finish(deps: WorkerDeps, reason: Literal["shutdown", "once"]) -> None:
    """Close the worker's run (errors logged by type); after a lost lock its last line says so
    (worker health reads it)."""
    run_id, deps.run_id = deps.run_id, None
    if run_id is None:
        return
    line = LOCK_LOST if deps.lock_lost else None
    try:
        async with deps.session_factory() as db:
            await finish_run(db, run_id, now=deps.now(), reason=reason, line=line)
            await db.commit()
    except Exception as exc:
        log.warning("run_finish_failed", error_type=type(exc).__name__)


@dataclass(frozen=True)
class _Beating:
    stop: asyncio.Event
    task: asyncio.Task[None]


def _start_beating(deps: WorkerDeps, *, shutdown: asyncio.Event | None) -> _Beating:
    """Start the beat task with its own stop event, so a shutdown request doesn't stop it
    while the current pass drains."""
    stop = asyncio.Event()
    task = asyncio.create_task(_beat_loop(deps, stop, shutdown=shutdown), name=BEAT_TASK)
    return _Beating(stop, task)


async def _stop_beating(beating: _Beating) -> None:
    """Stop the beat: let an in-flight beat finish (up to BEAT_STOP_SECONDS), then cancel."""
    beating.stop.set()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(beating.task, timeout=BEAT_STOP_SECONDS)


async def run_single(deps: WorkerDeps) -> PassResult:
    """``salesfinder worker --once``: open a run, one pass (beating meanwhile), record it and
    close the run as ``once``. SIGINT/SIGTERM don't cut the pass short (it finishes, as the
    loop's does), but from then on Replies, Writing and Discovery start no new item
    (``deps.stopping``). AnotherWorkerRunning if another worker's run is open. If the pass raises,
    the run is left open for the next start to close as ``crashed``."""
    stop = asyncio.Event()
    deps.stopping = stop
    remove = _install_signal_handlers(stop)
    try:
        await start(deps)
        beating = _start_beating(deps, shutdown=None)
        try:
            result = await run_pass(deps)
        finally:
            await _stop_beating(beating)
        await record(deps, result)
        await finish(deps, "once")
        return result
    finally:
        remove()


def _install_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            continue  # not the main thread / unsupported platform
        installed.append(sig)

    def remove() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return remove


async def run_forever(deps: WorkerDeps, *, stop: asyncio.Event) -> None:
    """Open a run, beat in the background, and pass → log → record → sleep until ``stop``
    (SIGINT/SIGTERM, or a beat finding the worker lock gone); the current pass always
    finishes, beating, and the run is closed as ``shutdown``. ``stop`` is also
    ``deps.stopping``: once it is set, Replies, Writing and Discovery start no new item (a
    Claude call already running finishes), so the pass ends well within launchd's
    ExitTimeOut (Ruling P7-G). AnotherWorkerRunning (at start
    or mid-loop) ends the loop: another worker's run is open, so this one stops. An
    exception escaping a pass leaves the run open: the next start closes it as ``crashed``."""
    deps.stopping = stop
    remove = _install_signal_handlers(stop)
    beating: _Beating | None = None
    clean = False
    try:
        await start(deps)
        beating = _start_beating(deps, shutdown=stop)
        while not stop.is_set():
            result = await run_pass(deps)
            for line in result.lines:
                log.info("worker", message=line)
            await record(deps, result)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=deps.config.worker.poll_seconds)
        clean = True
    finally:
        if beating is not None:
            await _stop_beating(beating)
        if clean:
            await finish(deps, "shutdown")
        remove()


# --------------------------------------------------------------------------- the worker lock

_HOLDS_LOCK = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND pid = pg_backend_pid() AND objsubid = 1"
    " AND ((classid::bigint << 32) | objid::bigint) = :key)"
)
_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")


def lock_check(
    conn: AsyncConnection, *, timeout: float = LOCK_CHECK_SECONDS
) -> Callable[[], Awaitable[bool]]:
    """A ``WorkerDeps.lock_alive`` for the CLI's lock connection: True while that session
    holds ``WORKER_LOCK_KEY`` (re-taken there if it's somehow free). False when the
    connection is gone (Postgres restarted: the session lock went with it), another session
    took the lock, or the check takes longer than ``timeout``. Checks run one at a time (two
    queries at once on one connection would fail and read as a lost lock); the wait for an
    earlier check counts towards ``timeout``."""
    serial = asyncio.Lock()

    async def alive() -> bool:
        try:
            async with asyncio.timeout(timeout), serial:
                held = bool(await conn.scalar(_HOLDS_LOCK, {"key": WORKER_LOCK_KEY}))
                if not held:
                    held = bool(await conn.scalar(_TRY_LOCK, {"key": WORKER_LOCK_KEY}))
                await conn.commit()  # never idle in a transaction; the lock survives
            return held
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            log.warning("worker_lock_check_failed", error_type=type(exc).__name__)
            return False

    return alive
