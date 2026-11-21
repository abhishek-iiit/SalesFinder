"""Runs: the worker's heartbeat log for any signed-in teammate. A summary (the worker's health,
Claude calls today against the budget, discovery spend this month against the budget, step
failures in the last 24 h) and the ``runs`` rows, newest first, 25 a page, each with its passes,
failed steps, the Claude calls and cost in its window, and its lines.

Read only. Lines and failure texts are the worker's own log-safe text; they are still rendered
escaped (Jinja autoescape)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from salesfinder.brain.budget import calls_today
from salesfinder.db.models import Run
from salesfinder.discovery.budget import get_budget, month_spend
from salesfinder.ops import runs as runs_service
from salesfinder.ops.health import run_label, running_line
from salesfinder.web.deps import AuthDep, ConfigDep, DbDep, page_worker_health, utcnow
from salesfinder.web.templating import render

router = APIRouter()

PAGE_SIZE = 25
MAX_PAGE = 10_000
FAILURES_SHOWN = 10


@dataclass(frozen=True)
class RunLine:
    at: datetime | None
    text: str


@dataclass(frozen=True)
class RunRow:
    run: Run
    label: str
    # The label's badge class (None: plain text).
    badge: str | None
    calls: int
    cost: Decimal
    lines: list[RunLine]


def run_lines(items: list[Any]) -> list[RunLine]:
    """A run's stored ``{"at", "text"}`` items (our own writes; anything malformed is shown as
    best it can be, never raised over)."""
    out: list[RunLine] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        at: datetime | None
        try:
            at = datetime.fromisoformat(str(item.get("at")))
        except ValueError:
            at = None
        out.append(RunLine(at=at, text=str(item.get("text", ""))))
    return out


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    config: ConfigDep,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> HTMLResponse:
    now = utcnow()
    # One extra row tells whether an older page exists.
    found = await runs_service.list_runs(db, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    shown = found[:PAGE_SIZE]
    usage: dict[uuid.UUID, tuple[int, Decimal]] = await runs_service.run_usage(db, shown, now=now)
    rows = []
    for run in shown:
        label, badge = run_label(run, now=now, config=config)
        rows.append(
            RunRow(
                run=run,
                label=label,
                badge=badge,
                calls=usage[run.id][0],
                cost=usage[run.id][1],
                lines=run_lines(run.lines),
            )
        )
    worker = await page_worker_health(request, db, auth)
    context = {
        "auth": auth,
        "worker": worker,
        "worker_line": running_line(worker, now=now) if worker is not None else None,
        "calls": await calls_today(db, now=now),
        "call_budget": config.usage.max_daily_claude_calls,
        "spent": await month_spend(db, now=now),
        "budget": await get_budget(db, config.discovery),
        "failures_24h": await runs_service.count_recent_failures(db, now=now),
        "recent_failures": await runs_service.recent_failures(db, now=now, limit=FAILURES_SHOWN),
        "rows": rows,
        "page": page,
        "prev_url": f"/runs?page={page - 1}" if page > 1 else None,
        "next_url": f"/runs?page={page + 1}" if len(found) > PAGE_SIZE else None,
    }
    return render(request, "runs.html", context)
