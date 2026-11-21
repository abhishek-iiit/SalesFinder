"""Worker health: is the background worker running, and on the same code as this process?

Read from the latest ``runs`` row, the open one if any: one query of plain columns that picks
the row through the indexes first and only then tests its ``lines`` in SQL (no ``Run`` object
enters the session), so the banner on every page stays cheap. The states, in the order they
are decided:

1. ``never``: no row (a fresh install).
2. ``stopped``: the latest row is finished (a clean stop, a ``--once`` pass; a ``crashed`` row
   says so, and so does a row whose last line is ``LOCK_LOST_LINE``: the worker stopped after
   losing its lock, Ruling P7-C, and launchd restarts it).
3. ``stale``: open, but no beat for more than ``ops.worker_stale_seconds`` (the worker died
   without shutting down, or hangs); the copy names the step it was in.
4. ``mismatch``: open and beating, but the worker's ``code_version`` isn't this process's.
5. ``running``; ``no_send`` when the open row holds ``NO_SEND_LINE`` (a ``--no-send`` worker:
   nothing will be sent; Ruling P7-J).

All copy is our own text; pages still render it escaped."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from salesfinder.config import AppConfig
from salesfinder.db.models import Run
from salesfinder.ops.runs import LOCK_LOST_LINE, NO_SEND_LINE

__all__ = [
    "CRASHED",
    "LOCK_LOST",
    "MISMATCH",
    "NEVER",
    "NO_SEND",
    "REASON_LABELS",
    "STALE",
    "STOPPED",
    "HealthState",
    "WorkerHealth",
    "ago",
    "last_pass_phrase",
    "reason_label",
    "run_label",
    "running_line",
    "worker_health",
]

HealthState = Literal["never", "running", "stale", "stopped", "mismatch"]

_WAITING = "Approved email, replies and discovery jobs wait until it's back."
NEVER = (
    "The worker hasn't run yet — start it (see Operations). "
    "Approved email, replies and discovery jobs wait until it runs."
)
STOPPED = "The worker isn't running (stopped {ago}). " + _WAITING
# A finished row closed as ``crashed`` (normally the next start closes it and opens its own
# row at once, so the latest row is rarely this one).
CRASHED = "The worker isn't running (it stopped without shutting down {ago}). " + _WAITING
# A finished row whose last line is LOCK_LOST_LINE (Ruling P7-C).
LOCK_LOST = (
    "The worker isn't running (it lost its database lock {ago}; launchd restarts it). " + _WAITING
)
STALE = "The worker stopped responding {ago}{during}. " + _WAITING
# A running ``--no-send`` worker (Ruling P7-J).
NO_SEND = "Running with --no-send (nothing will be sent)"
MISMATCH = "The web app and the worker are running different versions — restart both together."

# ``runs.end_reason`` as people read it (None: the run is still open).
REASON_LABELS: dict[str | None, str] = {
    None: "Running",
    "window": "Closed",
    "shutdown": "Shut down",
    "crashed": "Crashed",
    "once": "Single pass",
}


@dataclass(frozen=True)
class WorkerHealth:
    state: HealthState
    # What's wrong, for the banner (None only while ``running``).
    problem: str | None
    last_pass_at: datetime | None
    last_beat_at: datetime | None
    # The step the worker was in at its last beat (None between steps).
    step: str | None
    # The latest run's id (no ``Run`` object: the read loads none into the session).
    run_id: uuid.UUID | None
    # The open run holds NO_SEND_LINE: a ``--no-send`` worker, nothing will be sent.
    no_send: bool = False


def ago(delta: timedelta) -> str:
    """ "less than a minute ago", "N min ago" (under 120 min), "N h ago" (under 48 h), else
    "N days ago". A negative delta (clock skew) is "less than a minute ago"."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "less than a minute ago"
    if seconds < 120 * 60:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def reason_label(end_reason: str | None) -> str:
    return REASON_LABELS.get(end_reason, end_reason or "Running")


def last_pass_phrase(health: "WorkerHealth", *, now: datetime) -> str:
    """ "last pass 2 min ago", or "first pass under way" before the run's first pass."""
    if health.last_pass_at is None:
        return "first pass under way"
    return f"last pass {ago(now - health.last_pass_at)}"


def running_line(health: "WorkerHealth", *, now: datetime) -> str:
    """Today's and the Runs page's line for a healthy worker ("Running with --no-send (nothing
    will be sent) — ..." for a ``--no-send`` one)."""
    return f"{NO_SEND if health.no_send else 'Running'} — {last_pass_phrase(health, now=now)}"


def _stale(last_beat_at: datetime, *, now: datetime, config: AppConfig) -> bool:
    return now - last_beat_at > timedelta(seconds=config.ops.worker_stale_seconds)


def _says(lines: Any, line: str) -> bool:
    return isinstance(lines, list) and any(
        isinstance(item, dict) and item.get("text") == line for item in lines
    )


def run_label(run: Run, *, now: datetime, config: AppConfig) -> tuple[str, str | None]:
    """A ``runs`` row's end as the Runs page and ``salesfinder runs`` show it, with its badge
    class (None: plain text). An open row with no beat for ``ops.worker_stale_seconds`` is
    "No beat" (never a green "Running"); an open ``--no-send`` row says so. Needs ``lines``
    loaded."""
    if run.finished_at is not None:
        label = reason_label(run.end_reason)
        return label, "badge-off" if run.end_reason == "crashed" else None
    if _stale(run.last_beat_at, now=now, config=config):
        return "No beat", "badge-off"
    if _says(run.lines, NO_SEND_LINE):
        return "Running with --no-send", "badge-warn"
    return reason_label(None), "badge-ok"


# The latest row's id, picked through the indexes (``ix_runs_open``, ``ix_runs_started_at``)
# before any JSON is read: the open row if there is one, else the newest. Ordering every row by
# ``finished_at IS NULL`` would scan and sort the whole table on every page.
_open = aliased(Run)
_any = aliased(Run)
_LATEST_ID = func.coalesce(
    select(_open.id)
    .where(_open.finished_at.is_(None))
    .order_by(_open.started_at.desc())
    .limit(1)
    .scalar_subquery(),
    select(_any.id).order_by(_any.started_at.desc(), _any.id.desc()).limit(1).scalar_subquery(),
)

_LATEST = select(
    Run.id,
    Run.finished_at,
    Run.end_reason,
    Run.last_beat_at,
    Run.last_pass_at,
    Run.step,
    Run.code_version,
    Run.lines.contains([{"text": NO_SEND_LINE}]).label("no_send"),
    (Run.lines[-1]["text"].astext == LOCK_LOST_LINE).label("lock_lost"),
).where(Run.id == _LATEST_ID)


async def worker_health(
    db: AsyncSession, *, now: datetime, config: AppConfig, code_version: str
) -> WorkerHealth:
    """The worker's state from the latest ``runs`` row, compared with ``code_version`` (this
    process's ``ops.version.code_version()``). Read only; one query."""
    run = (await db.execute(_LATEST)).one_or_none()
    if run is None:
        return WorkerHealth("never", NEVER, None, None, None, None)

    def health(state: HealthState, problem: str | None, no_send: bool = False) -> WorkerHealth:
        return WorkerHealth(
            state, problem, run.last_pass_at, run.last_beat_at, run.step, run.id, no_send
        )

    if run.finished_at is not None:
        if run.end_reason == "crashed":
            copy = CRASHED
        elif run.lock_lost:
            copy = LOCK_LOST
        else:
            copy = STOPPED
        return health("stopped", copy.format(ago=ago(now - run.finished_at)))
    if _stale(run.last_beat_at, now=now, config=config):
        during = f" (during {run.step})" if run.step else ""
        return health("stale", STALE.format(ago=ago(now - run.last_beat_at), during=during))
    if run.code_version != code_version:
        return health("mismatch", MISMATCH)
    return health("running", None, bool(run.no_send))
