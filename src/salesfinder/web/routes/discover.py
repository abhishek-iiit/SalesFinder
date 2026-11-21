"""Discover: pick side/products/industries/regions -> estimate -> approve (or cancel) -> watch.

Nothing is searched from here: approving only marks the job for the worker (spec: discovery never
runs without a teammate confirming the estimate, and never past the monthly budget).
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.catalog import Catalog, Product
from salesfinder.config import AppConfig
from salesfinder.db.models import DiscoveryJob, User
from salesfinder.discovery import jobs
from salesfinder.discovery.budget import get_budget, month_spend
from salesfinder.industries import INDUSTRIES
from salesfinder.web.deps import (
    AuthContext,
    AuthDep,
    CatalogDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    client_ip,
    utcnow,
)
from salesfinder.web.templating import render

router = APIRouter(prefix="/discover")

RECENT_JOBS = 20
QUERY_PREVIEW = 20
POLLING_STATUSES = frozenset({"approved", "running"})
STATUS_LABELS = {
    "estimated": "estimated — waiting for your go-ahead",
    "approved": "approved — waiting for the worker",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "stopped_budget": "stopped (budget reached)",
    "cancelled": "cancelled",
}
NO_JOB = "No such discovery job."
ZERO = Decimal(0)

FormList = Annotated[list[str] | None, Form()]


@dataclass
class Selection:
    """What the form had ticked, so a refused estimate re-renders with it kept."""

    side: str = "buyer"
    products: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)


def _product_groups(catalog: Catalog, side: Literal["buyer", "supplier"]) -> list[dict[str, Any]]:
    """The side's products grouped under their first industry (each product listed once)."""
    by_industry: dict[str, list[Product]] = {key: [] for key in INDUSTRIES}
    for product in catalog.for_side(side):
        by_industry[product.industries[0]].append(product)
    return [
        {"label": INDUSTRIES[key], "products": products}
        for key, products in by_industry.items()
        if products
    ]


async def _discover_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    config: AppConfig,
    catalog: Catalog,
    selection: Selection,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    side: Literal["buyer", "supplier"] = "supplier" if selection.side == "supplier" else "buyer"
    discovery = config.discovery
    budget = await get_budget(db, discovery)
    spent = await month_spend(db, now=utcnow())
    recent = list(
        await db.scalars(
            select(DiscoveryJob)
            .order_by(DiscoveryJob.created_at.desc(), DiscoveryJob.id)
            .limit(RECENT_JOBS)
        )
    )
    context: dict[str, Any] = {
        "auth": auth,
        "side": side,
        "selection": selection,
        "groups": _product_groups(catalog, side),
        "industries": INDUSTRIES,
        "regions": discovery.buyer_regions if side == "buyer" else discovery.supplier_regions,
        "budget": budget,
        "spent": spent,
        "remaining": max(budget - spent, ZERO),
        "recent": recent,
        "status_labels": STATUS_LABELS,
        "error": error,
    }
    return render(request, "discover.html", context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
    side: Literal["buyer", "supplier"] = "buyer",
) -> HTMLResponse:
    return await _discover_page(request, db, auth, config, catalog, Selection(side=side))


@router.post("/estimate", response_class=HTMLResponse, response_model=None)
async def estimate(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
    side: Annotated[str, Form()] = "",
    products: FormList = None,
    industries: FormList = None,
    regions: FormList = None,
) -> HTMLResponse | RedirectResponse:
    selection = Selection(
        side=side, products=products or [], industries=industries or [], regions=regions or []
    )
    try:
        job = await jobs.create_estimate(
            db,
            side=side,
            product_ids=selection.products,
            industries=selection.industries,
            region_names=selection.regions,
            created_by=auth.user,
            catalog=catalog,
            config=config.discovery,
        )
    except ValueError as exc:
        # Raised while building the queries, before anything was written.
        return await _discover_page(
            request, db, auth, config, catalog, selection, error=str(exc), status_code=400
        )
    await audit.record(
        db,
        action=audit.DISCOVERY_ESTIMATED,
        actor_id=auth.user.id,
        target_type="discovery_job",
        target_id=str(job.id),
        details={
            "side": job.side,
            "searches": job.estimated_searches,
            "cost": str(job.estimated_cost_usd),
        },
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/discover/jobs/{job.id}", status_code=303)


async def _get_job(db: AsyncSession, job_id: uuid.UUID) -> DiscoveryJob:
    job = await db.get(DiscoveryJob, job_id, populate_existing=True)
    if job is None:
        raise HTTPException(status_code=404, detail=NO_JOB)
    return job


def _status_context(auth: AuthContext, job: DiscoveryJob) -> dict[str, Any]:
    return {
        "auth": auth,
        "job": job,
        "status_label": STATUS_LABELS.get(job.status, job.status),
        "polling": job.status in POLLING_STATUSES,
    }


async def _job_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    config: AppConfig,
    catalog: Catalog,
    job: DiscoveryJob,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    budget = await get_budget(db, config.discovery)
    remaining = max(budget - await month_spend(db, now=utcnow()), ZERO)
    created_by = await db.get(User, job.created_by_id)
    approved_by = await db.get(User, job.approved_by_id) if job.approved_by_id else None

    def product_name(pid: str) -> str:
        try:
            return catalog.by_id(pid).name
        except KeyError:  # removed from catalog.yaml since the estimate
            return pid

    params = job.params or {}
    context = {
        **_status_context(auth, job),
        "products": [product_name(p) for p in params.get("products", [])],
        "industries": [INDUSTRIES.get(i, i) for i in params.get("industries", [])],
        "regions": params.get("regions", []),
        "queries": job.queries[:QUERY_PREVIEW],
        "more_queries": max(len(job.queries) - QUERY_PREVIEW, 0),
        "remaining": remaining,
        "budget": budget,
        "created_by": created_by,
        "approved_by": approved_by,
        "error": error,
    }
    return render(request, "discover_job.html", context, status_code=status_code)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(
    request: Request,
    job_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> HTMLResponse:
    job = await _get_job(db, job_id)
    return await _job_page(request, db, auth, config, catalog, job)


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: uuid.UUID, auth: AuthDep, db: DbDep) -> HTMLResponse:
    """The HTMX-polled fragment: polls itself while the job is approved or running."""
    job = await _get_job(db, job_id)
    return render(
        request,
        "_job_status.html",
        _status_context(auth, job),
        headers={"Cache-Control": "no-store"},
    )


async def _refused(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    config: AppConfig,
    catalog: Catalog,
    job_id: uuid.UUID,
    message: str,
) -> HTMLResponse:
    """The job page with the service's refusal. Nothing was written; the rollback after
    rendering releases the row (and approval) locks the service took."""
    page = await _job_page(
        request,
        db,
        auth,
        config,
        catalog,
        await _get_job(db, job_id),
        error=message,
        status_code=400,
    )
    await db.rollback()
    return page


@router.post("/jobs/{job_id}/approve", response_class=HTMLResponse, response_model=None)
async def approve(
    request: Request,
    job_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> HTMLResponse | RedirectResponse:
    try:
        job = await jobs.approve_job(
            db, job_id, approved_by=auth.user, now=utcnow(), config=config.discovery
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=NO_JOB) from None
    except ValueError as exc:
        return await _refused(request, db, auth, config, catalog, job_id, str(exc))
    await audit.record(
        db,
        action=audit.DISCOVERY_APPROVED,
        actor_id=auth.user.id,
        target_type="discovery_job",
        target_id=str(job.id),
        details={
            "side": job.side,
            "searches": job.estimated_searches,
            "cost": str(job.estimated_cost_usd),
        },
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/discover/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse, response_model=None)
async def cancel(
    request: Request,
    job_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
) -> HTMLResponse | RedirectResponse:
    try:
        job = await jobs.cancel_job(db, job_id, actor=auth.user, now=utcnow())
    except LookupError:
        raise HTTPException(status_code=404, detail=NO_JOB) from None
    except ValueError as exc:
        return await _refused(request, db, auth, config, catalog, job_id, str(exc))
    await audit.record(
        db,
        action=audit.DISCOVERY_CANCELLED,
        actor_id=auth.user.id,
        target_type="discovery_job",
        target_id=str(job.id),
        details={"side": job.side},
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/discover/jobs/{job.id}", status_code=303)
