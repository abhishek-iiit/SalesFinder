"""Cohorts: saved queries over companies' latest signals, with live counts."""

import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from salesfinder import audit
from salesfinder.catalog import Catalog
from salesfinder.cohorts.evaluate import (
    count_matches,
    create_cohort,
    delete_cohort,
    list_matches,
    update_cohort,
)
from salesfinder.cohorts.model import CohortDefinition
from salesfinder.db.models import Cohort, User
from salesfinder.industries import SIDES
from salesfinder.signals.catalog import Side, for_side
from salesfinder.signals.settings import confirmed_codes
from salesfinder.web.cohort_form import (
    MODE_LABELS,
    MODES,
    NUMBER_INPUTS,
    UNPROFILED_LABEL,
    FormState,
    choices_for,
    cohort_from_form,
    describe,
    stale_warnings,
    state_from_definition,
    state_from_form,
)
from salesfinder.web.deps import (
    CSRF_HEADER,
    AuthContext,
    AuthDep,
    CatalogDep,
    CsrfDep,
    DbDep,
    client_ip,
)
from salesfinder.web.routes.companies import PROFILE_LABELS
from salesfinder.web.templating import render

router = APIRouter(prefix="/cohorts")

PAGE_SIZE = 50
MAX_PAGE = 10_000
SIDE_LABELS = {"buyer": "Buyer", "supplier": "Supplier"}
BAD_SIDE = "That side isn't valid."
NO_COHORT = "No such cohort."
UNREADABLE = "This cohort's saved definition can't be read; edit it to fix it."
# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {"saved": "Cohort saved.", "deleted": "Cohort deleted."}


def count_text(n: int) -> str:
    return "1 company matches" if n == 1 else f"{n:,} companies match"


def _definition(cohort: Cohort) -> CohortDefinition | None:
    try:
        return CohortDefinition.model_validate(cohort.definition)
    except ValidationError:
        return None


def _side(form: FormData) -> Side:
    side = form.get("side")
    if not isinstance(side, str) or side not in SIDES:
        raise HTTPException(status_code=400, detail=BAD_SIDE)
    return cast(Side, side)


def _name(form: FormData) -> str:
    name = form.get("name")
    return name if isinstance(name, str) else ""


async def _get(db: AsyncSession, cohort_id: uuid.UUID) -> Cohort:
    cohort = await db.get(Cohort, cohort_id)
    if cohort is None:
        raise HTTPException(status_code=404, detail=NO_COHORT)
    return cohort


def _audit_details(cohort: Cohort) -> dict[str, Any]:
    return {"name": cohort.name, "side": cohort.side, "definition": cohort.definition}


@dataclass(frozen=True)
class FormRow:
    code: str
    label: str
    description: str
    choices: list[tuple[str, str]] | None
    number_label: str | None


async def _render_form(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    catalog: Catalog,
    *,
    side: Side,
    state: FormState,
    definition: CohortDefinition | None,
    cohort_id: uuid.UUID | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    confirmed = await confirmed_codes(db, side)
    signals = for_side(side)
    rows = [
        FormRow(
            s.code,
            s.label,
            s.description,
            choices_for(s.code, catalog),
            NUMBER_INPUTS[s.code].label if s.code in NUMBER_INPUTS else None,
        )
        for s in signals
        if s.code in confirmed
    ]
    context: dict[str, Any] = {
        "auth": auth,
        "side": side,
        "side_label": SIDE_LABELS[side],
        "state": state,
        "rows": rows,
        "unconfirmed": [s for s in signals if s.code not in confirmed],
        "modes": [(m, MODE_LABELS[m]) for m in MODES],
        "cohort_id": cohort_id,
        "action": f"/cohorts/{cohort_id}" if cohort_id else "/cohorts",
        "error": error,
        # JSON for HTMX's hx-headers attribute (autoescaped into the attribute; no script).
        "hx_headers": json.dumps({CSRF_HEADER: auth.csrf_token}),
        "count_text": count_text(await count_matches(db, definition)) if definition else None,
        "count_error": None if definition else error,
        "unprofiled_label": UNPROFILED_LABEL,
    }
    return render(request, "cohort_form.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def cohorts_page(
    request: Request, auth: AuthDep, db: DbDep, catalog: CatalogDep
) -> HTMLResponse:
    found = (
        await db.execute(
            select(Cohort, User.name)
            .join(User, User.id == Cohort.created_by_id)
            .order_by(Cohort.side, func.lower(Cohort.name), Cohort.id)
        )
    ).all()
    confirmed = {side: await confirmed_codes(db, side) for side in SIDES}
    items = []
    for cohort, creator in found:
        defn = _definition(cohort)
        items.append(
            {
                "cohort": cohort,
                "creator": creator,
                "summary": describe(defn, catalog) if defn else UNREADABLE,
                "warnings": (
                    stale_warnings(defn, confirmed.get(cohort.side, frozenset()), catalog)
                    if defn
                    else []
                ),
                # Each count re-evaluates the side's companies (fine at hundreds of cohorts x
                # thousands of companies; precompute if that ever grows).
                "count": await count_matches(db, defn) if defn else None,
            }
        )
    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "items": items,
        "side_labels": SIDE_LABELS,
        "flash": FLASH.get(msg) if msg else None,
    }
    return render(request, "cohorts.html", context)


@router.get("/new", response_class=HTMLResponse)
async def new_cohort(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    catalog: CatalogDep,
    side: Literal["buyer", "supplier"] = "buyer",
) -> HTMLResponse:
    return await _render_form(
        request,
        auth,
        db,
        catalog,
        side=side,
        state=FormState(),
        definition=CohortDefinition(side=side),
    )


@router.post("/count", response_class=HTMLResponse)
async def live_count(
    request: Request, auth: CsrfDep, db: DbDep, catalog: CatalogDep
) -> HTMLResponse:
    """The HTMX live count for the form's current selections (a fragment)."""
    form = await request.form()
    side = _side(form)
    context: dict[str, Any]
    try:
        defn = cohort_from_form(form, side, catalog=catalog)
    except ValueError as exc:
        # 200 so HTMX swaps the message in; nothing is saved.
        context = {"count_text": None, "count_error": str(exc)}
    else:
        context = {"count_text": count_text(await count_matches(db, defn)), "count_error": None}
    return render(request, "_cohort_count.html", context)


@router.post("", response_model=None)
async def create(request: Request, auth: CsrfDep, db: DbDep, catalog: CatalogDep) -> Response:
    form = await request.form()
    side = _side(form)
    state = state_from_form(form, side, catalog)
    try:
        defn = cohort_from_form(form, side, catalog=catalog)
    except ValueError as exc:
        return await _render_form(
            request,
            auth,
            db,
            catalog,
            side=side,
            state=state,
            definition=None,
            error=str(exc),
            status_code=400,
        )
    try:
        cohort = await create_cohort(db, name=_name(form), definition=defn, actor=auth.user)
    except ValueError as exc:
        return await _render_form(
            request,
            auth,
            db,
            catalog,
            side=side,
            state=state,
            definition=defn,
            error=str(exc),
            status_code=400,
        )
    await audit.record(
        db,
        action=audit.COHORT_CREATED,
        actor_id=auth.user.id,
        target_type="cohort",
        target_id=str(cohort.id),
        details=_audit_details(cohort),
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/cohorts/{cohort.id}?msg=saved", status_code=303)


@router.get("/{cohort_id}", response_class=HTMLResponse)
async def cohort_page(
    request: Request,
    cohort_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    catalog: CatalogDep,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> HTMLResponse:
    cohort = await _get(db, cohort_id)
    creator = await db.get(User, cohort.created_by_id)
    defn = _definition(cohort)
    rows, total = (
        await list_matches(db, defn, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        if defn
        else ([], 0)
    )
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)

    def page_url(number: int) -> str:
        return f"/cohorts/{cohort_id}?" + urlencode({"page": number})

    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "cohort": cohort,
        "creator": creator.name if creator else None,
        "side_label": SIDE_LABELS.get(cohort.side, cohort.side),
        "summary": describe(defn, catalog) if defn else UNREADABLE,
        "warnings": (
            stale_warnings(defn, await confirmed_codes(db, cohort.side), catalog) if defn else []
        ),
        "include_unprofiled": defn.include_unprofiled if defn else False,
        "profile_labels": PROFILE_LABELS,
        "readable": defn is not None,
        "count_text": count_text(total),
        "rows": rows,
        "page": page,
        "pages": pages,
        "prev_url": page_url(min(page - 1, pages)) if page > 1 else None,
        "next_url": page_url(page + 1) if page < pages else None,
        "flash": FLASH.get(msg) if msg else None,
    }
    return render(request, "cohort.html", context)


@router.get("/{cohort_id}/edit", response_class=HTMLResponse)
async def edit_cohort(
    request: Request, cohort_id: uuid.UUID, auth: AuthDep, db: DbDep, catalog: CatalogDep
) -> HTMLResponse:
    cohort = await _get(db, cohort_id)
    side = cast(Side, cohort.side)
    defn = _definition(cohort)
    state = state_from_definition(cohort.name, defn) if defn else FormState(name=cohort.name)
    return await _render_form(
        request,
        auth,
        db,
        catalog,
        side=side,
        state=state,
        definition=defn or CohortDefinition(side=side),
        cohort_id=cohort_id,
        error=None if defn else UNREADABLE,
    )


@router.post("/{cohort_id}", response_model=None)
async def update(
    request: Request, cohort_id: uuid.UUID, auth: CsrfDep, db: DbDep, catalog: CatalogDep
) -> Response:
    cohort = await _get(db, cohort_id)
    side = cast(Side, cohort.side)  # a cohort never changes side; the form's side is ignored
    form = await request.form()
    state = state_from_form(form, side, catalog)
    try:
        defn = cohort_from_form(form, side, catalog=catalog)
    except ValueError as exc:
        return await _render_form(
            request,
            auth,
            db,
            catalog,
            side=side,
            state=state,
            definition=None,
            cohort_id=cohort_id,
            error=str(exc),
            status_code=400,
        )
    try:
        cohort = await update_cohort(
            db, cohort_id, name=_name(form), definition=defn, actor=auth.user
        )
    except ValueError as exc:
        return await _render_form(
            request,
            auth,
            db,
            catalog,
            side=side,
            state=state,
            definition=defn,
            cohort_id=cohort_id,
            error=str(exc),
            status_code=400,
        )
    await audit.record(
        db,
        action=audit.COHORT_UPDATED,
        actor_id=auth.user.id,
        target_type="cohort",
        target_id=str(cohort_id),
        details=_audit_details(cohort),
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/cohorts/{cohort_id}?msg=saved", status_code=303)


@router.post("/{cohort_id}/delete")
async def delete(
    request: Request, cohort_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> RedirectResponse:
    cohort = await _get(db, cohort_id)
    details = _audit_details(cohort)
    await delete_cohort(db, cohort_id)
    await audit.record(
        db,
        action=audit.COHORT_DELETED,
        actor_id=auth.user.id,
        target_type="cohort",
        target_id=str(cohort_id),
        details=details,
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/cohorts?msg=deleted", status_code=303)
