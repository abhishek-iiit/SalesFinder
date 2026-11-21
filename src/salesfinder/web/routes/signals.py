"""The Signals page: which signals profiling collects, and their states."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from salesfinder.profiling.service import change_signal_states, observed_counts
from salesfinder.signals.catalog import SIGNALS, for_side
from salesfinder.signals.settings import STATES, states
from salesfinder.web.deps import AuthDep, ConfigDep, CsrfDep, DbDep, client_ip, utcnow
from salesfinder.web.templating import render

router = APIRouter(prefix="/signals")

BAD_STATE = "That state isn't valid."
# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {"saved": "Saved. Scores updated."}
STATE_LABELS = {
    "proposed": "Proposed",
    "confirmed": "Confirmed",
    "skipped": "Skipped",
    "rejected": "Rejected",
}
KNOWN_CODES = frozenset(s.code for s in SIGNALS)


@router.get("", response_class=HTMLResponse)
async def signals_page(request: Request, auth: AuthDep, db: DbDep) -> HTMLResponse:
    current = await states(db)
    counts = await observed_counts(db)
    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "groups": [
            ("Buyer signals", for_side("buyer")),
            ("Supplier signals", for_side("supplier")),
        ],
        "states": current,
        "counts": counts,
        "state_choices": [(s, STATE_LABELS[s]) for s in STATES],
        "state_labels": STATE_LABELS,
        "flash": FLASH.get(msg) if msg else None,
    }
    return render(request, "signals.html", context)


async def _apply(
    request: Request, auth: CsrfDep, db: DbDep, config: ConfigDep, wanted: dict[str, str]
) -> RedirectResponse:
    await change_signal_states(
        db, wanted, actor=auth.user, config=config, now=utcnow(), ip=client_ip(request)
    )
    await db.commit()
    return RedirectResponse("/signals?msg=saved", status_code=303)


@router.post("/confirm-free")
async def confirm_free(
    request: Request, auth: CsrfDep, db: DbDep, config: ConfigDep
) -> RedirectResponse:
    wanted = {s.code: "confirmed" for s in SIGNALS if s.cost == "free"}
    return await _apply(request, auth, db, config, wanted)


@router.post("/{code}/state")
async def set_signal_state(
    code: str,
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    state: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if code not in KNOWN_CODES:
        raise HTTPException(status_code=404, detail="No such signal.")
    if state not in STATES:
        raise HTTPException(status_code=400, detail=BAD_STATE)
    return await _apply(request, auth, db, config, {code: state})
