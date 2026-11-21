"""Discovered companies: Buyers/Suppliers tabs with filters, and one company's page."""

import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.catalog import Catalog
from salesfinder.contacts.dns import mx_domain
from salesfinder.contacts.service import (
    can_allow,
    is_sendable,
    mx_state_of,
    set_override,
    unsendable_reason,
)
from salesfinder.db.models import (
    Company,
    DiscoveryJob,
    EmailAddress,
    EmailPattern,
    Observation,
    Person,
    User,
)
from salesfinder.profiling.scoring import ScoreResult, score_company
from salesfinder.profiling.service import request_reprofile
from salesfinder.signals.catalog import SIGNALS, SignalDef
from salesfinder.signals.latest import latest_values
from salesfinder.signals.settings import confirmed_codes
from salesfinder.web.contact_format import Badge, pattern_line, reason_text, status_badge
from salesfinder.web.deps import AuthDep, CatalogDep, ConfigDep, CsrfDep, DbDep, client_ip
from salesfinder.web.signal_format import format_value
from salesfinder.web.templating import render

router = APIRouter(prefix="/companies")

PAGE_SIZE = 50
MAX_PAGE = 10_000
MAX_QUERY = 200
STATUSES = ("new", "excluded")
EXCLUSION_LABELS = {
    "existing_relationship": "Existing relationship",
    "do_not_contact": "Do not contact",
}
BAD_FILTER = "That filter isn't valid."
SORTS = ("newest", "score")
PROFILE_LABELS = {
    "pending": "Waiting",
    "ok": "Profiled",
    "partial": "Partly profiled",
    "failed": "Failed",
    "blocked": "Blocked",
    "no_site": "No website",
}
# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
FLASH = {
    "queued": "Queued — the worker will profile it shortly.",
    "allowed": "Sending to this address is now allowed.",
    "blocked": "We won't send to this address.",
    "cleared": "Override cleared.",
}
NOT_PROFILED = "Excluded companies aren't profiled."
NOT_CONTACTED = "Excluded companies aren't contacted."
CANT_ALLOW = "Addresses that won't deliver can't be allowed."
ALREADY_SENDABLE = "This address is already sendable."
CANT_LIFT = "Allowing sending can't lift what holds this address back."
BAD_DECISION = "That choice isn't valid."
# decision -> (override value, flash key)
DECISIONS: dict[str, tuple[bool | None, str]] = {
    "allow": (True, "allowed"),
    "block": (False, "blocked"),
    "clear": (None, "cleared"),
}


def _like_pattern(text: str) -> str:
    """``%text%`` with LIKE wildcards in ``text`` matched literally (escape character ``\\``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _parse_job(value: str) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=BAD_FILTER) from None


@router.get("", response_class=HTMLResponse)
async def companies_page(
    request: Request,
    auth: AuthDep,
    db: DbDep,
    side: Literal["buyer", "supplier"] = "buyer",
    # Blank values come from the filter form's "All" options, so these are checked by hand.
    status: str = "",
    job: str = "",
    sort: str = "newest",
    q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> HTMLResponse:
    if status and status not in STATUSES:
        raise HTTPException(status_code=400, detail=BAD_FILTER)
    if sort not in SORTS:
        raise HTTPException(status_code=400, detail=BAD_FILTER)
    job_id = _parse_job(job)
    if "\x00" in q:  # PostgreSQL text can't hold NUL; the query would fail with a 500
        raise HTTPException(status_code=400, detail=BAD_FILTER)
    q = q.strip()

    conditions: list[ColumnElement[bool]] = [Company.side == side]
    if status:
        conditions.append(Company.status == status)
    if job_id is not None:
        conditions.append(Company.discovered_by_job_id == job_id)
    if q:
        pattern = _like_pattern(q)
        conditions.append(
            or_(
                Company.name.ilike(pattern, escape="\\"), Company.domain.ilike(pattern, escape="\\")
            )
        )

    order = (
        [
            # Excluded companies are never contacted: their (stale) scores rank them last.
            case((Company.status == "excluded", 1), else_=0),
            Company.score.desc().nulls_last(),
            Company.created_at.desc(),
            Company.id,
        ]
        if sort == "score"
        else [Company.created_at.desc(), Company.id]
    )
    total = await db.scalar(select(func.count()).select_from(Company).where(*conditions)) or 0
    rows = list(
        await db.scalars(
            select(Company)
            .where(*conditions)
            .order_by(*order)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    )
    counts = dict(
        (await db.execute(select(Company.side, func.count()).group_by(Company.side))).all()
    )
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)

    filters = {
        "side": side,
        "status": status,
        "job": str(job_id) if job_id else "",
        "sort": sort if sort != "newest" else "",
        "q": q,
    }

    def page_url(number: int) -> str:
        params = {k: v for k, v in filters.items() if v}
        params["page"] = str(number)
        return "/companies?" + urlencode(params)

    def tab_url(tab: str) -> str:
        return "/companies?" + urlencode({"side": tab})

    context: dict[str, Any] = {
        "auth": auth,
        "side": side,
        "status": status,
        "job_id": job_id,
        "q": q,
        "sort": sort,
        "profile_labels": PROFILE_LABELS,
        "page": page,
        "pages": pages,
        "total": total,
        "rows": rows,
        "counts": {"buyer": counts.get("buyer", 0), "supplier": counts.get("supplier", 0)},
        "prev_url": page_url(min(page - 1, pages)) if page > 1 else None,
        "next_url": page_url(page + 1) if page < pages else None,
        "tab_url": tab_url,
        "exclusion_labels": EXCLUSION_LABELS,
    }
    return render(request, "companies.html", context)


@dataclass(frozen=True)
class SignalRow:
    signal: SignalDef
    text: str
    snippet: str | None
    url: str | None


async def _signal_rows(
    db: AsyncSession, company: Company, confirmed: frozenset[str], catalog: Catalog
) -> list[SignalRow]:
    if not confirmed:
        return []
    stmt = select(Observation).where(
        Observation.company_id == company.id,
        Observation.code.in_(confirmed),
        Observation.is_latest,
    )
    latest = {o.code: o for o in await db.scalars(stmt)}
    return [
        SignalRow(
            s,
            format_value(s.code, latest[s.code].value, catalog),
            latest[s.code].evidence_snippet,
            latest[s.code].evidence_url,
        )
        for s in SIGNALS
        if s.code in latest
    ]


@dataclass(frozen=True)
class AddressRow:
    id: uuid.UUID
    address: str
    kind: str
    badge: Badge
    sendable: bool
    reason: str
    can_allow: bool
    override: bool | None
    evidence_url: str | None
    evidence_snippet: str | None


@dataclass(frozen=True)
class ContactsView:
    people: list[Person]
    addresses: list[AddressRow]
    mx_state: str
    pattern: str
    people_note: str  # why there are no people (or that reading them failed); "" if neither


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def people_note(company: Company, *, has_people: bool) -> str:
    """What the last profile's people extraction says, for the contacts section."""
    if company.contacts_extract_failed:
        return "Couldn't read people from the pages (error)."
    if has_people:
        return ""
    if company.profiled_at is None or company.profile_status not in ("ok", "partial"):
        return "No people found on the company's own pages yet."
    if company.people_pages_read == 0:
        return "No team, about or contact page found."
    pages = _plural(company.people_pages_read, "people page", "people pages")
    return f"Read {pages} — no named people with titles."


async def _contacts_view(db: AsyncSession, company: Company) -> ContactsView:
    people = list(
        await db.scalars(
            select(Person)
            .where(Person.company_id == company.id)
            .order_by(Person.is_target.desc(), Person.rank.asc().nulls_last(), Person.full_name)
        )
    )
    rows = list(
        await db.scalars(
            select(EmailAddress)
            .where(EmailAddress.company_id == company.id)
            .order_by(EmailAddress.address)
        )
    )
    mx_state = await mx_state_of(db, company.domain)
    names = {
        u.id: u.name
        for u in await db.scalars(
            select(User).where(User.id.in_({r.override_by_id for r in rows if r.override_by_id}))
        )
    }
    addresses = [
        AddressRow(
            id=r.id,
            address=r.address,
            kind=r.kind,
            badge=status_badge(r.status),
            sendable=is_sendable(r, mx_state=mx_state),
            reason=reason_text(
                unsendable_reason(r, mx_state=mx_state),
                overridden_by=names.get(r.override_by_id) if r.override_by_id else None,
            ),
            can_allow=can_allow(r),
            override=r.override_sendable,
            evidence_url=r.evidence_url,
            evidence_snippet=r.evidence_snippet,
        )
        for r in rows
    ]
    key = mx_domain(company.domain)
    learned = await db.get(EmailPattern, key) if key else None
    return ContactsView(
        people=people,
        addresses=addresses,
        people_note=people_note(company, has_people=bool(people)),
        mx_state=mx_state,
        pattern=pattern_line(
            learned.pattern if learned else None,
            learned.support if learned else None,
            company.domain,
        ),
    )


CATEGORY_LABELS = {
    "procurement": "Procurement",
    "supply_chain": "Supply chain",
    "rnd": "R&D",
    "owner_exec": "Owner / executive",
    "export": "Export",
    "business_dev": "Business development",
    "marketing": "Marketing",
    "other": "Other",
}
MX_LABELS = {
    "has_mx": "Mail server: found",
    "no_mail": "Mail server: none — this domain can't receive email",
    "unknown": "Mail server: not checked yet",
}


@router.get("/{company_id}", response_class=HTMLResponse)
async def company_page(
    request: Request,
    company_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    catalog: CatalogDep,
    config: ConfigDep,
) -> HTMLResponse:
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="No such company.")
    job = (
        await db.get(DiscoveryJob, company.discovered_by_job_id)
        if company.discovered_by_job_id
        else None
    )
    confirmed = await confirmed_codes(db, company.side)
    score_result: ScoreResult | None = None
    if company.status != "excluded":
        latest = (await latest_values(db, confirmed, [company.id])).get(company.id, {})
        score_result = score_company(
            latest, side=company.side, confirmed=confirmed, config=config.scoring
        )
    msg = request.query_params.get("msg")
    context = {
        "auth": auth,
        "company": company,
        "job": job,
        "exclusion_labels": EXCLUSION_LABELS,
        "profile_labels": PROFILE_LABELS,
        "score_result": score_result,
        "signal_rows": await _signal_rows(db, company, confirmed, catalog),
        "flash": FLASH.get(msg) if msg else None,
        "contacts": await _contacts_view(db, company) if company.status != "excluded" else None,
        "mx_labels": MX_LABELS,
        "category_labels": CATEGORY_LABELS,
    }
    return render(request, "company.html", context)


@router.post("/{company_id}/reprofile")
async def reprofile(
    request: Request, company_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> RedirectResponse:
    try:
        await request_reprofile(db, company_id, actor=auth.user)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such company.") from None
    except ValueError:
        raise HTTPException(status_code=400, detail=NOT_PROFILED) from None
    await audit.record(
        db,
        action=audit.COMPANY_REPROFILE_REQUESTED,
        actor_id=auth.user.id,
        target_type="company",
        target_id=str(company_id),
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/companies/{company_id}?msg=queued", status_code=303)


@router.post("/{company_id}/emails/{email_id}/override")
async def override_email(
    request: Request,
    company_id: uuid.UUID,
    email_id: uuid.UUID,
    auth: CsrfDep,
    db: DbDep,
    decision: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail=BAD_DECISION)
    company = await db.get(Company, company_id)
    row = await db.get(EmailAddress, email_id)
    if company is None or row is None or row.company_id != company_id:
        raise HTTPException(status_code=404, detail="No such address.")
    if company.status == "excluded":
        raise HTTPException(status_code=400, detail=NOT_CONTACTED)
    value, flash_key = DECISIONS[decision]
    if value is True and row.status == "invalid":
        raise HTTPException(status_code=400, detail=CANT_ALLOW)
    if value is True and is_sendable(row, mx_state=await mx_state_of(db, company.domain)):
        raise HTTPException(status_code=400, detail=ALREADY_SENDABLE)
    if value is True and not can_allow(row):
        raise HTTPException(status_code=400, detail=CANT_LIFT)
    address, old = row.address, row.override_sendable
    try:
        await set_override(db, email_id, sendable=value, actor=auth.user)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such address.") from None
    except ValueError:
        raise HTTPException(status_code=400, detail=NOT_CONTACTED) from None
    await audit.record(
        db,
        action=audit.EMAIL_OVERRIDE_CLEARED if value is None else audit.EMAIL_OVERRIDE_SET,
        actor_id=auth.user.id,
        target_type="email_address",
        target_id=str(email_id),
        details={"address": address, "old": old, "new": value},
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/companies/{company_id}?msg={flash_key}", status_code=303)
