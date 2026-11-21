"""Admin pages: users and invites, activation, audit log, settings (sending, budget, HubSpot,
the companies CSV export), do-not-contact and suppressions."""

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit, export
from salesfinder.auth import users
from salesfinder.auth.tokens import hash_token
from salesfinder.db.models import AuditLog, HubSpotSync, Invite, User
from salesfinder.discovery.budget import MAX_BUDGET, get_budget, month_spend, set_budget
from salesfinder.exclusions import dnc, sweep
from salesfinder.exclusions.sync import ACTIVE, request_sync, sync_health
from salesfinder.ops import network
from salesfinder.sending import state as sending_state
from salesfinder.settings import Settings
from salesfinder.suppressions import service as suppressions
from salesfinder.web.deps import (
    AdminDep,
    AuthContext,
    CatalogDep,
    ConfigDep,
    CsrfDep,
    DbDep,
    SettingsDep,
    client_ip,
    require_admin,
    retry_on_conflict,
    rollback_keeping_auth,
    utcnow,
)
from salesfinder.web.sending_view import sending_view
from salesfinder.web.templating import render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

AUDIT_PAGE_SIZE = 200
SUPPRESSIONS_SHOWN = 200  # newest suppressions listed on the do-not-contact page
INVITE_EXISTS = "An account with this email already exists."
NO_SELF_DEACTIVATE = "You can't deactivate your own account. Ask another admin."
# Fixed flash copy for the users page, keyed by ``?msg=`` (anything else is ignored).
USERS_FLASH = {
    "invite_revoked": "Invite revoked.",
    "invite_not_pending": "That invite is no longer pending.",
}


async def _users_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTMLResponse:
    now = utcnow()
    all_users = list(await db.scalars(select(User).order_by(User.created_at, User.email)))
    pending = list(
        await db.scalars(
            select(Invite)
            .where(Invite.accepted_at.is_(None), Invite.expires_at > now)
            .order_by(Invite.created_at.desc())
        )
    )
    context: dict[str, Any] = {
        "auth": auth,
        "users": all_users,
        "pending": pending,
        "roles": users.ROLES,
        "form_email": "",
        "form_role": "member",
        "flash": None if "error" in extra else _flash(request.query_params.get("msg"), USERS_FLASH),
        **extra,
    }
    return render(request, "admin_users.html", context, status_code=status_code, headers=headers)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, auth: AdminDep, db: DbDep) -> HTMLResponse:
    return await _users_page(request, db, auth)


def invite_link(request: Request, settings: Settings, raw: str) -> str:
    """The configured public URL when set, whatever address the admin browsed from; else the
    request's own base URL (its Host already checked against allowed_hosts by TrustedHost;
    doctor and Settings warn when LAN mode has no public URL)."""
    if settings.public_url:
        return f"{settings.public_url}/invite/{raw}"
    return f"{request.base_url}invite/{raw}"


@router.post("/invites", response_class=HTMLResponse)
async def create_invite(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    settings: SettingsDep,
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "member",
) -> HTMLResponse:
    try:
        raw = await users.create_invite(
            db, email=email, role=role, invited_by=auth.user, now=utcnow()
        )
    except users.InviteError:
        # Raised before anything was written; no rollback (it would expire ``auth.user``).
        return await _users_page(
            request, db, auth, status_code=400, error=INVITE_EXISTS, form_email=email
        )
    except ValueError as exc:
        return await _users_page(
            request, db, auth, status_code=400, error=str(exc), form_email=email
        )
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    if invite is None:  # pragma: no cover - flushed moments ago in this transaction
        raise RuntimeError("invite row missing after create_invite")
    await audit.record(
        db,
        action=audit.INVITE_CREATED,
        actor_id=auth.user.id,
        target_type="invite",
        target_id=str(invite.id),
        details={"email": invite.email, "role": invite.role},
        ip=client_ip(request),
    )
    await db.commit()
    link = invite_link(request, settings, raw)
    return await _users_page(
        request,
        db,
        auth,
        headers={"Cache-Control": "no-store"},
        invite_link=link,
        invite_email=invite.email,
    )


@router.post("/invites/{invite_id}/revoke", response_class=HTMLResponse)
async def revoke_invite(
    request: Request, invite_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> Response:
    """Revoke a pending invite. Idempotent: an invite already revoked (or expired) gets a
    neutral flash, unaudited (nothing changed); an accepted one shows the error."""
    try:
        invite = await users.revoke_invite(db, invite_id, now=utcnow())
    except LookupError:
        exists = await db.get(Invite, invite_id)
        await rollback_keeping_auth(db, auth)
        if exists is None:
            raise HTTPException(status_code=404, detail="No such invite.") from None
        return RedirectResponse("/admin/users?msg=invite_not_pending", status_code=303)
    except ValueError as exc:
        await rollback_keeping_auth(db, auth)
        return await _users_page(request, db, auth, status_code=400, error=str(exc))
    await audit.record(
        db,
        action=audit.INVITE_REVOKED,
        actor_id=auth.user.id,
        target_type="invite",
        target_id=str(invite.id),
        details={"email": invite.email, "role": invite.role},
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/admin/users?msg=invite_revoked", status_code=303)


async def _set_active(
    request: Request, auth: AuthContext, db: AsyncSession, user_id: uuid.UUID, active: bool
) -> RedirectResponse:
    if not active and user_id == auth.user.id:
        raise HTTPException(status_code=400, detail=NO_SELF_DEACTIVATE)
    try:
        target, invites_revoked = await users.set_active(db, user_id, active, now=utcnow())
    except LookupError:
        raise HTTPException(status_code=404, detail="No such user.") from None
    details: dict[str, Any] = {"email": target.email}
    if not active:
        details["invites_revoked"] = invites_revoked
    await audit.record(
        db,
        action=audit.USER_REACTIVATED if active else audit.USER_DEACTIVATED,
        actor_id=auth.user.id,
        target_type="user",
        target_id=str(target.id),
        details=details,
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/deactivate")
async def deactivate(
    request: Request, user_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> RedirectResponse:
    return await _set_active(request, auth, db, user_id, False)


@router.post("/users/{user_id}/reactivate")
async def reactivate(
    request: Request, user_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> RedirectResponse:
    return await _set_active(request, auth, db, user_id, True)


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, auth: AdminDep, db: DbDep) -> HTMLResponse:
    rows = (
        await db.execute(
            select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .order_by(AuditLog.at.desc(), AuditLog.id)
            .limit(AUDIT_PAGE_SIZE)
        )
    ).all()
    entries = [{"row": row, "actor": actor_email} for row, actor_email in rows]
    return render(
        request, "admin_audit.html", {"auth": auth, "entries": entries, "limit": AUDIT_PAGE_SIZE}
    )


# --- settings: budget and HubSpot ------------------------------------------------------------

BUDGET_RANGE = "Enter an amount between $0 and $10,000."
_BUDGET_RE = re.compile(r"\d{1,5}(\.\d{1,2})?")
HUBSPOT_NOT_SET = "SALESFINDER_HUBSPOT_TOKEN is not set."
MAX_FLASH_COUNT = 1_000_000

# Fixed flash copy keyed by ``?msg=``; anything else in the URL is ignored, never echoed.
SETTINGS_FLASH = {
    "budget_saved": "Budget saved.",
    "sync_queued": "Sync queued — the worker will run it within a minute.",
    "sync_pending": "A sync is already queued or running.",
    "resumed": "Sending is on.",
    "already_on": "Sending was already on.",
    "paused": "Sending is paused.",
    "already_paused": "Sending was already paused.",
}


def _flash(msg: str | None, table: dict[str, str]) -> str | None:
    return table.get(msg) if msg else None


async def _settings_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    config: Any,
    settings: Any,
    *,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    now = utcnow()
    budget = await get_budget(db, config.discovery)
    spent = await month_spend(db, now=now)
    configured = settings.hubspot_token is not None
    health = await sync_health(db, now=now, token_configured=configured, config=config.hubspot)
    syncs = list(
        await db.scalars(select(HubSpotSync).order_by(HubSpotSync.created_at.desc()).limit(5))
    )
    context: dict[str, Any] = {
        "auth": auth,
        "budget": budget,
        "spent": spent,
        "hubspot_configured": configured,
        "hubspot_not_set": HUBSPOT_NOT_SET,
        "health": health,
        "syncs": syncs,
        "form_budget": format(budget, ".2f"),
        "sending": await sending_view(db, now=now, config=config),
        "bounce_max_pct": f"{config.sending.bounce_max_rate * 100:g}",
        "export_max_contacts": config.contacts.max_targets_per_company,
        "network": {
            "mode": network.mode_line(settings),
            "allowed_hosts": list(settings.allowed_hosts),
            "public_url": settings.public_url,
            "warnings": network.lan_warnings(settings),
        },
        "flash": None
        if "error" in extra
        else _flash(request.query_params.get("msg"), SETTINGS_FLASH),
        **extra,
    }
    return render(request, "admin_settings.html", context, status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, auth: AdminDep, db: DbDep, config: ConfigDep, settings: SettingsDep
) -> HTMLResponse:
    return await _settings_page(request, db, auth, config, settings)


@router.post("/settings/budget", response_class=HTMLResponse)
async def update_budget(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    settings: SettingsDep,
    budget: Annotated[str, Form()] = "",
) -> Response:
    text = budget.strip()
    if not _BUDGET_RE.fullmatch(text) or Decimal(text) > MAX_BUDGET:
        return await _settings_page(
            request,
            db,
            auth,
            config,
            settings,
            status_code=400,
            error=BUDGET_RANGE,
            form_budget=text[:20],
        )
    new = Decimal(text)
    old = await get_budget(db, config.discovery)
    await set_budget(db, new, actor=auth.user)
    await audit.record(
        db,
        action=audit.SETTINGS_UPDATED,
        actor_id=auth.user.id,
        target_type="setting",
        target_id="discovery.monthly_budget_usd",
        details={"old": format(old, ".2f"), "new": format(new, ".2f")},
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/admin/settings?msg=budget_saved", status_code=303)


@router.post("/hubspot/sync")
async def hubspot_sync(
    request: Request, auth: CsrfDep, db: DbDep, settings: SettingsDep
) -> RedirectResponse:
    if settings.hubspot_token is None:
        raise HTTPException(status_code=400, detail=HUBSPOT_NOT_SET)
    active_before = await db.scalar(
        select(func.count()).select_from(HubSpotSync).where(HubSpotSync.status.in_(ACTIVE))
    )
    row = await request_sync(db, requested_by=auth.user)
    if active_before:
        await db.commit()
        return RedirectResponse("/admin/settings?msg=sync_pending", status_code=303)
    await audit.record(
        db,
        action=audit.HUBSPOT_SYNC_REQUESTED,
        actor_id=auth.user.id,
        target_type="hubspot_sync",
        target_id=str(row.id),
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/admin/settings?msg=sync_queued", status_code=303)


# --- companies CSV export -------------------------------------------------------------------

UNKNOWN_EXPORT_SIDE = "Choose buyer, supplier or all."


@router.post("/export/companies", response_model=None)
async def export_companies(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    config: ConfigDep,
    catalog: CatalogDep,
    side: Annotated[str, Form()] = "",
) -> Response:
    """Download the companies CSV (admins only: the router's gate; a POST with the CSRF token,
    so another site can't trigger it). Audited with the side and the row count, never row
    data; committed before the file is returned, so no transaction is open while it downloads.
    The filename is fixed apart from the validated side and the UTC date."""
    if side not in export.SIDES:
        raise HTTPException(status_code=400, detail=UNKNOWN_EXPORT_SIDE)
    chosen: export.ExportSide = side  # narrowed by the SIDES check above
    # End whatever the dependencies read, so the export opens its own REPEATABLE READ snapshot
    # (the audit row below is written in it and committed with it).
    await db.commit()
    body, rows = await export.build_csv(db, side=chosen, config=config, catalog=catalog)
    await audit.record(
        db,
        action=audit.COMPANIES_EXPORTED,
        actor_id=auth.user.id,
        target_type="export",
        target_id="companies",
        details={"side": chosen, "rows": rows},
        ip=client_ip(request),
    )
    await db.commit()
    filename = f"salesfinder-companies-{chosen}-{utcnow():%Y%m%d}.csv"
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# --- sending: resume (pausing is at /sending/pause, open to every teammate) -----------------


def _shown_changed_at(raw: object) -> datetime | None:
    """The ``changed_at`` the page showed ("" for a state never written); ``ValueError`` for
    anything that isn't one (it can't match, so the resume is refused)."""
    if not isinstance(raw, str):
        raise ValueError(sending_state.STATE_CHANGED)
    if not raw:
        return None
    try:
        shown = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(sending_state.STATE_CHANGED) from None
    if shown.tzinfo is None:
        raise ValueError(sending_state.STATE_CHANGED)
    return shown


@router.post("/sending/resume", response_model=None)
async def resume_sending(
    request: Request, auth: CsrfDep, db: DbDep, config: ConfigDep, settings: SettingsDep
) -> Response:
    """Turn sending on, only if the state is still the one the page showed (Ruling P6-H): an
    auto-pause or another teammate's change in between is shown, not overwritten."""
    form = await request.form()
    raw = form.get("changed_at")

    async def attempt() -> Response:
        try:
            _, changed = await sending_state.resume(
                db, actor=auth.user, now=utcnow(), expected_changed_at=_shown_changed_at(raw)
            )
        except ValueError as exc:
            await rollback_keeping_auth(db, auth)
            return await _settings_page(
                request, db, auth, config, settings, status_code=409, error=str(exc)
            )
        if changed:
            await audit.record(
                db,
                action=audit.SENDING_RESUMED,
                actor_id=auth.user.id,
                target_type="setting",
                target_id=sending_state.KEY,
                details={"source": "web"},
                ip=client_ip(request),
            )
        await db.commit()
        msg = "resumed" if changed else "already_on"
        return RedirectResponse(f"/admin/settings?msg={msg}", status_code=303)

    return await retry_on_conflict(db, auth, attempt)


# --- do-not-contact --------------------------------------------------------------------------


def _dnc_flash(request: Request) -> str | None:
    params = request.query_params
    msg = params.get("msg")
    if msg == "removed":
        return "Removed. Companies already excluded stay excluded."
    if msg == "suppression_removed":
        return "Removed from the suppression list."
    if msg == "added":
        try:
            count = min(max(int(params.get("n", "0")), 0), MAX_FLASH_COUNT)
        except ValueError:
            count = 0
        if count == 0:
            return "Added."
        noun = "company" if count == 1 else "companies"
        return f"Added. {count} {noun} now excluded."
    return None


async def _dnc_page(
    request: Request,
    db: AsyncSession,
    auth: AuthContext,
    *,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    entries = await dnc.list_entries(db)
    suppressed = await suppressions.list_suppressions(db, limit=SUPPRESSIONS_SHOWN)
    user_ids = {e.added_by_id for e in entries if e.added_by_id}
    user_ids |= {s.created_by_id for s in suppressed if s.created_by_id}
    adders = {u.id: u.email for u in await db.scalars(select(User).where(User.id.in_(user_ids)))}
    context: dict[str, Any] = {
        "auth": auth,
        "entries": entries,
        "adders": adders,
        "suppressions": suppressed,
        "suppressions_total": await suppressions.count_suppressions(db),
        "suppressions_shown": SUPPRESSIONS_SHOWN,
        "form_entry": "",
        "form_reason": "",
        "flash": _dnc_flash(request),
        **extra,
    }
    return render(request, "admin_dnc.html", context, status_code=status_code)


@router.get("/do-not-contact", response_class=HTMLResponse)
async def dnc_page(request: Request, auth: AdminDep, db: DbDep) -> HTMLResponse:
    return await _dnc_page(request, db, auth)


@router.post("/do-not-contact", response_class=HTMLResponse)
async def dnc_add(
    request: Request,
    auth: CsrfDep,
    db: DbDep,
    entry: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    try:
        row = await dnc.add_entry(db, raw=entry, reason=reason, added_by=auth.user)
    except ValueError as exc:
        return await _dnc_page(
            request,
            db,
            auth,
            status_code=400,
            error=str(exc),
            form_entry=entry[:320],
            form_reason=reason[:300],
        )
    excluded = 0
    if row.kind == "domain":
        # Every not-yet-excluded company on that domain (any status, not just "new").
        excluded = await sweep.exclude_do_not_contact(db, value=row.value)
    await audit.record(
        db,
        action=audit.DNC_ADDED,
        actor_id=auth.user.id,
        target_type="do_not_contact",
        target_id=str(row.id),
        details={
            "kind": row.kind,
            "value": row.value,
            "reason": row.reason,
            "companies_excluded": excluded,
        },
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse(f"/admin/do-not-contact?msg=added&n={excluded}", status_code=303)


@router.post("/do-not-contact/{entry_id}/remove")
async def dnc_remove(
    request: Request, entry_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> RedirectResponse:
    try:
        row = await dnc.remove_entry(db, entry_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such entry.") from None
    await audit.record(
        db,
        action=audit.DNC_REMOVED,
        actor_id=auth.user.id,
        target_type="do_not_contact",
        target_id=str(entry_id),
        details={"kind": row.kind, "value": row.value, "reason": row.reason},
        ip=client_ip(request),
    )
    await db.commit()
    return RedirectResponse("/admin/do-not-contact?msg=removed", status_code=303)


# --- suppressions (unsubscribed, bounced, manual) ---------------------------------------------


@router.post("/suppressions/{suppression_id}/remove", response_class=HTMLResponse)
async def suppression_remove(
    request: Request, suppression_id: uuid.UUID, auth: CsrfDep, db: DbDep
) -> Response:
    """Take a bounced or manual suppression off the list (audited by the service). An
    unsubscribe is refused: the page is shown again with the reason."""
    try:
        await suppressions.remove(db, suppression_id, actor=auth.user, ip=client_ip(request))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such suppression.") from None
    except ValueError as exc:
        return await _dnc_page(request, db, auth, status_code=400, error=str(exc))
    await db.commit()
    return RedirectResponse("/admin/do-not-contact?msg=suppression_removed", status_code=303)
