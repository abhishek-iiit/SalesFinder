"""Campaigns: the list, the new-campaign form, and one campaign's page with its lifecycle
buttons (activate, pause, resume, archive). Every change is audited; refusals are shown as 400
with the service's message."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.campaigns import service
from salesfinder.campaigns.service import (
    CAP_RANGE,
    GONE,
    MAX_DAILY_CAP,
    activation_problems,
    create_campaign,
    drafted_today,
    list_campaigns,
)
from salesfinder.catalog import Catalog, Facts
from salesfinder.cohorts.model import CohortDefinition
from salesfinder.db.models import Campaign, Cohort, Mailbox, User
from salesfinder.industries import SIDES
from salesfinder.outbox.service import outbox_counts
from salesfinder.web.cohort_form import describe
from salesfinder.web.deps import (
    AuthContext,
    AuthDep,
    CatalogDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    FactsDep,
    client_ip,
    retry_on_conflict,
    rollback_keeping_auth,
    utcnow,
)
from salesfinder.web.templating import render

router = APIRouter(prefix="/campaigns")

SIDE_LABELS = {"buyer": "Buyer", "supplier": "Supplier"}
STATUS_LABELS = {"draft": "Draft", "active": "Active", "paused": "Paused", "archived": "Archived"}
NO_CAMPAIGN = "No such campaign."
BAD_SIDE = "That side isn't valid."
CHOOSE_COHORT = "Choose a cohort."
UNREADABLE = "This cohort's saved definition can't be read; edit the cohort to fix it."
DEFAULT_CAP = 40
# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {
    "created": "Campaign created. Activate it to start drafting.",
    "activated": "Campaign activated.",
    "paused": "Campaign paused.",
    "resumed": "Campaign resumed.",
    "archived": "Campaign archived.",
    "mailbox": "Sending mailbox saved.",
}
Action = Literal["activate", "pause", "resume", "archive"]
_AUDIT = {
    "activate": audit.CAMPAIGN_ACTIVATED,
    "pause": audit.CAMPAIGN_PAUSED,
    "resume": audit.CAMPAIGN_RESUMED,
    "archive": audit.CAMPAIGN_ARCHIVED,
}
_DONE = {"activate": "activated", "pause": "paused", "resume": "resumed", "archive": "archived"}
# The buttons each status offers (Activate also takes a paused campaign; Resume reads better).
_BUTTONS: dict[str, list[Action]] = {
    "draft": ["activate", "archive"],
    "active": ["pause", "archive"],
    "paused": ["resume", "archive"],
    "archived": [],
}


def _flash(request: Request) -> str | None:
    msg = request.query_params.get("msg")
    return FLASH.get(msg) if msg else None


async def _cohorts(db: AsyncSession, side: str) -> list[Cohort]:
    return list(
        await db.scalars(
            select(Cohort).where(Cohort.side == side).order_by(func.lower(Cohort.name), Cohort.id)
        )
    )


@router.get("", response_class=HTMLResponse)
async def campaigns_page(request: Request, auth: AuthDep, db: DbDep) -> HTMLResponse:
    context = {
        "auth": auth,
        "summaries": await list_campaigns(db, now=utcnow()),
        "side_labels": SIDE_LABELS,
        "status_labels": STATUS_LABELS,
        "flash": _flash(request),
    }
    return render(request, "campaigns.html", context)


async def _render_form(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    *,
    side: str,
    name: str = "",
    cohort_id: str = "",
    cap: str = str(DEFAULT_CAP),
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context = {
        "auth": auth,
        "side": side,
        "side_labels": SIDE_LABELS,
        "cohorts": await _cohorts(db, side),
        "name": name,
        "cohort_id": cohort_id,
        "cap": cap,
        "max_cap": MAX_DAILY_CAP,
        "error": error,
    }
    return render(request, "campaign_form.html", context, status_code=status_code)


@router.get("/new", response_class=HTMLResponse)
async def new_campaign(request: Request, auth: AuthDep, db: DbDep, side: str = "buyer") -> Response:
    if side not in SIDES:
        raise HTTPException(status_code=400, detail=BAD_SIDE)
    return await _render_form(request, auth, db, side=side)


def _field(value: object) -> str:
    return value if isinstance(value, str) else ""


@router.post("/new", response_model=None)
async def create(request: Request, auth: CsrfDep, db: DbDep) -> Response:
    form = await request.form()
    side = _field(form.get("side"))
    if side not in SIDES:
        raise HTTPException(status_code=400, detail=BAD_SIDE)
    name, raw_cohort, raw_cap = (
        _field(form.get("name")),
        _field(form.get("cohort_id")),
        _field(form.get("daily_draft_cap")),
    )

    async def refuse(message: str) -> HTMLResponse:
        await rollback_keeping_auth(db, auth)
        return await _render_form(
            request,
            auth,
            db,
            side=side,
            name=name,
            cohort_id=raw_cohort,
            cap=raw_cap,
            error=message,
            status_code=400,
        )

    async def attempt() -> Response:
        try:
            cohort_id = uuid.UUID(raw_cohort)
        except ValueError:
            return await refuse(CHOOSE_COHORT)
        try:
            cap = int(raw_cap.strip())
        except ValueError:
            return await refuse(CAP_RANGE)
        try:
            campaign = await create_campaign(
                db, name=name, side=side, cohort_id=cohort_id, daily_draft_cap=cap, actor=auth.user
            )
        except ValueError as exc:
            return await refuse(str(exc))
        await audit.record(
            db,
            action=audit.CAMPAIGN_CREATED,
            actor_id=auth.user.id,
            target_type="campaign",
            target_id=str(campaign.id),
            details={
                "name": campaign.name,
                "side": campaign.side,
                "cohort_id": str(campaign.cohort_id),
                "daily_draft_cap": campaign.daily_draft_cap,
            },
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse(f"/campaigns/{campaign.id}?msg=created", status_code=303)

    return await retry_on_conflict(db, auth, attempt)


def _cohort_summary(cohort: Cohort | None, catalog: Catalog) -> str:
    if cohort is None:
        return "The cohort no longer exists."
    try:
        defn = CohortDefinition.model_validate(cohort.definition)
    except ValidationError:
        return UNREADABLE
    return describe(defn, catalog)


async def _get(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.get(Campaign, campaign_id, populate_existing=True)
    if campaign is None:
        raise HTTPException(status_code=404, detail=NO_CAMPAIGN)
    return campaign


async def _render_campaign(
    request: Request,
    auth: AuthContext,
    db: AsyncSession,
    campaign: Campaign,
    *,
    catalog: Catalog,
    facts: Facts,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    cohort = await db.get(Cohort, campaign.cohort_id)
    creator = await db.get(User, campaign.created_by_id)
    counts = await outbox_counts(db, campaign_id=campaign.id)
    problems = (
        await activation_problems(db, campaign, facts=facts)
        if campaign.status != "archived"
        else []
    )
    mailboxes = list(
        await db.scalars(
            select(Mailbox).where(Mailbox.side == campaign.side).order_by(Mailbox.address)
        )
    )
    context = {
        "auth": auth,
        "campaign": campaign,
        "mailboxes": mailboxes,
        "cohort": cohort,
        "cohort_summary": _cohort_summary(cohort, catalog),
        "creator": creator.name if creator else None,
        "side_label": SIDE_LABELS.get(campaign.side, campaign.side),
        "status_label": STATUS_LABELS.get(campaign.status, campaign.status),
        "drafted_today": await drafted_today(db, campaign.id, now=utcnow()),
        "ready": counts["awaiting_approval"],
        "held": counts["gate_failed"],
        "problems": problems,
        "buttons": _BUTTONS.get(campaign.status, []),
        "error": error,
        "flash": None if error else _flash(request),
    }
    return render(request, "campaign.html", context, status_code=status_code)


@router.get("/{campaign_id}", response_class=HTMLResponse)
async def campaign_page(
    request: Request,
    campaign_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    catalog: CatalogDep,
    facts: FactsDep,
) -> HTMLResponse:
    campaign = await _get(db, campaign_id)
    return await _render_campaign(request, auth, db, campaign, catalog=catalog, facts=facts)


@router.post("/{campaign_id}/mailbox", response_model=None)
async def choose_mailbox(
    request: Request,
    campaign_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    catalog: CatalogDep,
    facts: FactsDep,
    mailbox_id: Annotated[str, Form()] = "",
) -> Response:
    async def attempt() -> Response:
        campaign = await _get(db, campaign_id)
        try:
            chosen = uuid.UUID(mailbox_id)
        except ValueError:
            return await _render_campaign(
                request, auth, db, campaign, catalog=catalog, facts=facts,
                error=service.CHOOSE_MAILBOX, status_code=400,
            )  # fmt: skip
        before = campaign.mailbox_id
        try:
            await service.set_mailbox(db, campaign_id, chosen, actor=auth.user)
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            if str(exc) == GONE:
                raise HTTPException(status_code=404, detail=NO_CAMPAIGN) from None
            campaign = await _get(db, campaign_id)
            return await _render_campaign(
                request, auth, db, campaign, catalog=catalog, facts=facts,
                error=str(exc), status_code=400,
            )  # fmt: skip
        mailbox = await db.get(Mailbox, chosen)
        await audit.record(
            db,
            action=audit.CAMPAIGN_MAILBOX_SET,
            actor_id=auth.user.id,
            target_type="campaign",
            target_id=str(campaign_id),
            details={
                "name": campaign.name,
                "from": str(before) if before else None,
                "to": str(chosen),
                "address": mailbox.address if mailbox else None,
            },
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse(f"/campaigns/{campaign_id}?msg=mailbox", status_code=303)

    return await retry_on_conflict(db, auth, attempt)


@router.post("/{campaign_id}/{action}", response_model=None)
async def lifecycle(
    request: Request,
    campaign_id: uuid.UUID,
    action: Action,
    auth: CsrfDep,
    db: DbDep,
    catalog: CatalogDep,
    facts: FactsDep,
    config: ConfigDep,
) -> Response:
    async def attempt() -> Response:
        campaign = await _get(db, campaign_id)
        before = campaign.status
        extra: dict[str, int] = {}
        try:
            if action == "activate":
                await service.activate(
                    db, campaign_id, actor=auth.user, facts=facts, config=config, now=utcnow()
                )
            elif action == "resume":
                await service.resume(
                    db, campaign_id, actor=auth.user, facts=facts, config=config, now=utcnow()
                )
            elif action == "pause":
                await service.pause(db, campaign_id, actor=auth.user)
            else:
                archived = await service.archive(db, campaign_id, actor=auth.user)
                extra = {
                    "sequences_stopped": archived.sequences_stopped,
                    "messages_cancelled": archived.messages_cancelled,
                }
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            if str(exc) == GONE:
                raise HTTPException(status_code=404, detail=NO_CAMPAIGN) from None
            campaign = await _get(db, campaign_id)
            return await _render_campaign(
                request,
                auth,
                db,
                campaign,
                catalog=catalog,
                facts=facts,
                error=str(exc),
                status_code=400,
            )
        await audit.record(
            db,
            action=_AUDIT[action],
            actor_id=auth.user.id,
            target_type="campaign",
            target_id=str(campaign_id),
            details={"name": campaign.name, "from": before, "to": campaign.status, **extra},
            ip=client_ip(request),
        )
        await db.commit()
        return RedirectResponse(f"/campaigns/{campaign_id}?msg={_DONE[action]}", status_code=303)

    return await retry_on_conflict(db, auth, attempt)
