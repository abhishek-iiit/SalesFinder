"""The heartbeat log: the worker's ``runs`` rows. Services flush; callers commit.

One row covers one heartbeat window (``usage.heartbeat_interval_minutes``) of worker passes.
``record_pass`` adds each pass's log-safe lines, failures and counts and rolls over to a new
row when the window is up; ``beat`` keeps ``last_beat_at`` (liveness) and the current step
fresh during long steps; ``finish_run`` closes the row on a clean stop. A worker that died
without shutting down leaves its row open: the next ``start_run`` closes it as ``crashed``.

Callers write ``runs`` rows only in their own short transactions, never while holding an
outreach lock (messages, sequences, recipients, mailboxes, conversations), and never across
network or subprocess I/O.

``lines`` and ``failures`` take only the worker's own log-safe lines (counts, our own copy,
mailbox addresses, exception type names), never untrusted text."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import DateTime, and_, bindparam, cast, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.models import Run, UsageEvent

__all__ = [
    "CRASHED_LINE",
    "LATEST_FIRST",
    "LOCK_LOST_LINE",
    "MAX_FAILURES",
    "MAX_LINE_CHARS",
    "MAX_LIST",
    "NO_SEND_LINE",
    "PassResult",
    "RunNotOpen",
    "beat",
    "count_recent_failures",
    "finish_run",
    "latest_run",
    "list_runs",
    "prune_runs",
    "recent_failures",
    "record_pass",
    "run_usage",
    "start_run",
]

CRASHED_LINE = "The worker stopped without shutting down."
# Said once per row by a ``salesfinder worker --no-send`` (worker health reads it).
NO_SEND_LINE = "Sending skipped: this worker runs with --no-send."
# The last line of a row closed because the worker lost its database lock (Ruling P7-C).
LOCK_LOST_LINE = "Lost the worker lock (database restarted?) — stopping so launchd restarts me."
# Most failure items one row keeps, the newest (``failed_steps`` still counts every one).
MAX_FAILURES = 50
# Longest text one line or failure item keeps.
MAX_LINE_CHARS = 500
# Most rows one ``list_runs`` / items one ``recent_failures`` call returns.
MAX_LIST = 500
# Column sizes (db/models/ops.py).
_HOST_MAX = 255
_STEP_MAX = 32
_VERSION_MAX = 16


class RunNotOpen(LookupError):
    """``record_pass`` was given a run that is missing or already finished."""


@dataclass(frozen=True)
class PassResult:
    """What one worker pass did: every log line, the "<Step> step failed: <Type>" lines among
    them, and whether it ran in quiet hours."""

    lines: list[str]
    failures: list[str]
    quiet: bool


def _item(at: datetime, text: str) -> dict[str, str]:
    return {"at": at.astimezone(UTC).isoformat(), "text": text[:MAX_LINE_CHARS]}


def _clamp(value: int, high: int) -> int:
    return max(0, min(value, high))


async def _close_crashed(db: AsyncSession) -> None:
    """Close the unfinished run (at most one: ``uq_runs_one_open``) as ``crashed`` at its last
    beat: its worker is gone (only one worker runs at a time, and it closes its own row before
    it stops)."""
    open_runs = (
        await db.execute(
            select(Run)
            .where(Run.finished_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    for run in open_runs:
        run.finished_at = run.last_beat_at
        run.end_reason = "crashed"
        run.lines = [*run.lines, _item(run.last_beat_at, CRASHED_LINE)]
    await db.flush()


async def _open(
    db: AsyncSession,
    *,
    now: datetime,
    host: str,
    pid: int,
    code_version: str,
    last_pass_at: datetime | None = None,
    lines: Sequence[str] = (),
) -> Run:
    run = Run(
        started_at=now,
        last_beat_at=now,
        last_pass_at=last_pass_at,
        host=host[:_HOST_MAX],
        pid=pid,
        code_version=code_version[:_VERSION_MAX],
        lines=[_item(now, line) for line in lines],
    )
    db.add(run)
    await db.flush()
    return run


async def start_run(
    db: AsyncSession,
    *,
    now: datetime,
    host: str,
    pid: int,
    code_version: str,
    close_crashed: bool = True,
) -> Run:
    """A worker starting: close any run left open by a worker that died, then open a new one.

    ``close_crashed=False`` only opens the new row: for a live worker whose own run was closed
    under it (``record_pass`` raised LookupError). If another run is still open, the flush
    raises IntegrityError (``uq_runs_one_open``): some other worker is running."""
    if close_crashed:
        await _close_crashed(db)
    return await _open(db, now=now, host=host, pid=pid, code_version=code_version)


async def record_pass(
    db: AsyncSession,
    run_id: uuid.UUID,
    result: PassResult,
    *,
    now: datetime,
    config: AppConfig,
    carry: Sequence[str] = (),
) -> uuid.UUID:
    """Add one pass to the open run ``run_id`` (locked ``FOR UPDATE``). When the run's window
    is up (``now - started_at >= usage.heartbeat_interval_minutes``) old finished runs are
    pruned, it is finished as ``window``, and a new run opens for the same worker (its
    ``last_pass_at`` is this pass, its first lines ``carry``: lines about the worker itself,
    such as ``--no-send``): the new run's id is returned (otherwise ``run_id``).
    RunNotOpen (a LookupError) if the run is missing or already finished."""
    run = (
        await db.execute(
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.finished_at is not None:
        raise RunNotOpen(f"Run {run_id} is not open.")
    run.passes += 1
    run.last_pass_at = now
    run.last_beat_at = now
    room = max(0, config.ops.max_run_lines - len(run.lines))
    kept = result.lines[:room]
    if kept:
        run.lines = [*run.lines, *(_item(now, line) for line in kept)]
    run.lines_dropped += len(result.lines) - len(kept)
    if result.failures:
        # The newest MAX_FAILURES of the window.
        run.failures = [*run.failures, *(_item(now, line) for line in result.failures)][
            -MAX_FAILURES:
        ]
    run.failed_steps += len(result.failures)
    run.quiet = run.quiet or result.quiet
    await db.flush()

    if now - run.started_at < timedelta(minutes=config.usage.heartbeat_interval_minutes):
        return run.id
    host, pid, version = run.host, run.pid, run.code_version
    await prune_runs(db, now=now, keep_days=config.ops.runs_keep_days)
    await finish_run(db, run.id, now=now, reason="window")
    # No crash-close here: this worker's run was just finished, and it is the only worker.
    new = await _open(
        db, now=now, host=host, pid=pid, code_version=version, last_pass_at=now, lines=carry
    )
    return new.id


async def beat(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    now: datetime,
    step: str | None,
    step_started_at: datetime | None,
) -> None:
    """The worker is alive (and in ``step``, or between steps). A finished run is left alone."""
    await db.execute(
        update(Run)
        .where(Run.id == run_id, Run.finished_at.is_(None))
        .values(
            last_beat_at=now,
            step=step[:_STEP_MAX] if step is not None else None,
            step_started_at=step_started_at,
        )
    )
    await db.flush()


async def finish_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    now: datetime,
    reason: Literal["window", "shutdown", "once"],
    line: str | None = None,
) -> None:
    """Close an open run (a finished one keeps its end and its lines). ``line``, if given, is
    added as the run's last line in the same UPDATE (past ``ops.max_run_lines``: it says why
    the worker stopped)."""
    values: dict[str, object] = {
        "finished_at": now,
        "end_reason": reason,
        "last_beat_at": now,
        "step": None,
        "step_started_at": None,
    }
    if line is not None:
        values["lines"] = Run.lines.op("||")(cast([_item(now, line)], JSONB))
    await db.execute(
        update(Run).where(Run.id == run_id, Run.finished_at.is_(None)).values(**values)
    )
    await db.flush()


async def prune_runs(db: AsyncSession, *, now: datetime, keep_days: int) -> int:
    """Delete runs that finished more than ``keep_days`` ago (``coalesce(finished_at,
    started_at)``; an open run is never pruned). A run that started long ago but closed just
    now (a Mac asleep mid-window) is kept a full ``keep_days``."""
    cutoff = now - timedelta(days=keep_days)
    result = await db.execute(
        delete(Run).where(
            Run.finished_at.is_not(None),
            func.coalesce(Run.finished_at, Run.started_at) < cutoff,
        )
    )
    await db.flush()
    return int(getattr(result, "rowcount", 0) or 0)


# The latest run: the open one if any (even if a finished row carries a later start, e.g. the
# clock was stepped back), else the newest.
LATEST_FIRST = (Run.finished_at.is_(None).desc(), Run.started_at.desc(), Run.id.desc())


async def latest_run(db: AsyncSession) -> Run | None:
    return (await db.execute(select(Run).order_by(*LATEST_FIRST).limit(1))).scalar_one_or_none()


async def list_runs(db: AsyncSession, *, limit: int, offset: int) -> list[Run]:
    """Newest first. ``limit`` is clamped to 0..MAX_LIST, ``offset`` to at least 0."""
    rows = await db.execute(
        select(Run)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(_clamp(limit, MAX_LIST))
        .offset(max(0, offset))
    )
    return list(rows.scalars())


_RECENT_FAILURES = text(
    "SELECT (f.value->>'at')::timestamptz AS at, f.value->>'text' AS text"
    " FROM runs CROSS JOIN LATERAL jsonb_array_elements(runs.failures) WITH ORDINALITY"
    " AS f(value, n)"
    " WHERE runs.last_beat_at >= :cutoff AND (f.value->>'at')::timestamptz >= :cutoff"
    " ORDER BY at DESC, runs.started_at DESC, f.n DESC"
    " LIMIT :limit"
).bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))


async def recent_failures(
    db: AsyncSession, *, now: datetime, hours: int = 24, limit: int = 5
) -> list[tuple[datetime, str]]:
    """The newest failure lines of the last ``hours`` (from runs beating since then), newest
    first. ``hours`` and ``limit`` are clamped to 0..a year and 0..MAX_LIST."""
    cutoff = now - timedelta(hours=_clamp(hours, 24 * 366))
    rows = await db.execute(_RECENT_FAILURES, {"cutoff": cutoff, "limit": _clamp(limit, MAX_LIST)})
    return [(at, text_) for at, text_ in rows]


_COUNT_RECENT_FAILURES = text(
    "SELECT coalesce(sum("
    " (SELECT count(*) FROM jsonb_array_elements(runs.failures) AS f(value)"
    "  WHERE (f.value->>'at')::timestamptz >= :cutoff)"
    " + CASE WHEN runs.started_at >= :cutoff"
    "   THEN greatest(0, runs.failed_steps - jsonb_array_length(runs.failures)) ELSE 0 END"
    "), 0) FROM runs WHERE runs.last_beat_at >= :cutoff"
).bindparams(bindparam("cutoff", type_=DateTime(timezone=True)))


async def count_recent_failures(db: AsyncSession, *, now: datetime, hours: int = 24) -> int:
    """Step failures in the last ``hours``, by the list's rule (``recent_failures``): the kept
    items stamped since the cutoff, plus, for a run that started since the cutoff, the
    failures past its kept ``MAX_FAILURES`` (all of them fell inside the window). ``hours`` is
    clamped to 0..a year."""
    cutoff = now - timedelta(hours=_clamp(hours, 24 * 366))
    total = await db.scalar(_COUNT_RECENT_FAILURES, {"cutoff": cutoff})
    return int(total or 0)


async def run_usage(
    db: AsyncSession, runs: Sequence[Run], *, now: datetime
) -> dict[uuid.UUID, tuple[int, Decimal]]:
    """Per run: the Claude calls (``usage_events``, success or failure) and their summed
    ``cost_usd`` inside ``[started_at, finished_at or now)``. One query for the whole page."""
    if not runs:
        return {}
    usage: dict[uuid.UUID, tuple[int, Decimal]] = {run.id: (0, Decimal(0)) for run in runs}
    end = func.coalesce(Run.finished_at, bindparam("now", now, type_=DateTime(timezone=True)))
    rows = await db.execute(
        select(Run.id, func.count(UsageEvent.id), func.sum(UsageEvent.cost_usd))
        .select_from(Run)
        .join(UsageEvent, and_(UsageEvent.at >= Run.started_at, UsageEvent.at < end))
        .where(Run.id.in_(list(usage)))
        .group_by(Run.id)
    )
    for run_id, calls, cost in rows:
        usage[run_id] = (int(calls), Decimal(cost) if cost is not None else Decimal(0))
    return usage
