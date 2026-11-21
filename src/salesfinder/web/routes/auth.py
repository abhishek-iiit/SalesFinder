"""Sign-in, sign-out and invite acceptance."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.auth import users
from salesfinder.auth.emails import normalize_email
from salesfinder.auth.sessions import create_session, revoke_session
from salesfinder.auth.tokens import hash_token
from salesfinder.db.models import Invite, User
from salesfinder.settings import Settings
from salesfinder.web.deps import (
    SESSION_COOKIE,
    CsrfDep,
    DbDep,
    OptionalAuthDep,
    SettingsDep,
    clear_session_cookie,
    client_ip,
    get_rate_limiter,
    set_session_cookie,
    utcnow,
)
from salesfinder.web.security import safe_next
from salesfinder.web.templating import render

router = APIRouter()

LOGIN_FAILED = "Email or password is incorrect."
RATE_LIMITED = "Too many attempts. Try again in 15 minutes."
PASSWORD_MISMATCH = "The two passwords don't match."  # noqa: S105 - UI copy, not a secret
INVITE_ERRORS: dict[users.InviteFailure, str] = {
    "invalid": "This invite link isn't valid.",
    "expired": "This invite link has expired. Ask an admin for a new one.",
    "used": "This invite link has already been used.",
    "exists": "An account with this email already exists. Sign in instead.",
}
_AUDIT_EMAIL_MAX = 320


def _limiter_key(email: str, ip: str | None) -> str:
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = email.strip().lower()
    return f"{normalized}|{ip}"


def _audit_email(email: str) -> str:
    """Only a valid, normalised email is audited: a mistyped password must never be stored."""
    try:
        return normalize_email(email)[:_AUDIT_EMAIL_MAX]
    except ValueError:
        return "<invalid>"


async def _start_session(
    request: Request, db: AsyncSession, user: User, settings: Settings, *, now: datetime
) -> str:
    """Create a session (revoking any session cookie the browser already held)."""
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        await revoke_session(db, previous, now=now)
    _, raw = await create_session(
        db,
        user,
        ttl=timedelta(hours=settings.session_ttl_hours),
        now=now,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return raw


def _login_page(
    request: Request, *, email: str = "", next_path: str = "/", error: str | None = None
) -> dict[str, object]:
    return {"email": email, "next": safe_next(next_path), "error": error}


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request, auth: OptionalAuthDep, next: str = "/"
) -> HTMLResponse | RedirectResponse:
    if auth is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", _login_page(request, next_path=next))


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> HTMLResponse | RedirectResponse:
    now = utcnow()
    ip = client_ip(request)
    limiter = get_rate_limiter(request)
    key = _limiter_key(email, ip)
    attempted = _audit_email(email)

    if limiter.is_blocked(key, now):
        await audit.record(
            db,
            action=audit.USER_LOGIN_FAILED,
            actor_id=None,
            details={"email": attempted, "reason": "rate_limited"},
            ip=ip,
        )
        await db.commit()
        context = _login_page(request, email=email, next_path=next, error=RATE_LIMITED)
        return render(request, "login.html", context, status_code=429)

    user = await users.authenticate(db, email, password, now=now)
    if user is None:
        limiter.record_failure(key, now)
        await audit.record(
            db,
            action=audit.USER_LOGIN_FAILED,
            actor_id=None,
            details={"email": attempted, "reason": "bad_credentials"},
            ip=ip,
        )
        await db.commit()
        context = _login_page(request, email=email, next_path=next, error=LOGIN_FAILED)
        return render(request, "login.html", context, status_code=401)

    limiter.reset(key)
    raw = await _start_session(request, db, user, settings, now=now)
    await audit.record(
        db,
        action=audit.USER_LOGIN,
        actor_id=user.id,
        target_type="user",
        target_id=str(user.id),
        ip=ip,
    )
    await db.commit()
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(response, raw, settings)
    return response


@router.post("/logout")
async def logout(
    request: Request, auth: CsrfDep, db: DbDep, settings: SettingsDep
) -> RedirectResponse:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        await revoke_session(db, raw, now=utcnow())
    await audit.record(
        db,
        action=audit.USER_LOGOUT,
        actor_id=auth.user.id,
        target_type="user",
        target_id=str(auth.user.id),
        ip=client_ip(request),
    )
    await db.commit()
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


async def _check_invite(
    db: AsyncSession, raw: str, *, now: datetime
) -> tuple[Invite | None, users.InviteFailure | None]:
    """Read-only pre-check for rendering; ``accept_invite`` re-checks under a row lock."""
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    if invite is None:
        return None, "invalid"
    if invite.accepted_at is not None:
        return invite, "used"
    if invite.expires_at <= now:
        return invite, "expired"
    if await db.scalar(select(User.id).where(User.email == invite.email)) is not None:
        return invite, "exists"
    return invite, None


def _invite_error(request: Request, reason: users.InviteFailure) -> HTMLResponse:
    context = {"title": "Invite", "message": INVITE_ERRORS[reason], "show_login": True}
    return render(request, "error.html", context, status_code=400)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str, db: DbDep) -> HTMLResponse:
    invite, problem = await _check_invite(db, token, now=utcnow())
    if problem is not None or invite is None:
        return _invite_error(request, problem or "invalid")
    return render(request, "invite.html", {"invite": invite, "token": token, "name": ""})


@router.post("/invite/{token}", response_class=HTMLResponse, response_model=None)
async def invite_submit(
    request: Request,
    token: str,
    db: DbDep,
    settings: SettingsDep,
    name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str | None, Form()] = None,
) -> HTMLResponse | RedirectResponse:
    now = utcnow()
    invite, problem = await _check_invite(db, token, now=now)
    if problem is not None or invite is None:
        return _invite_error(request, problem or "invalid")

    def form_error(message: str) -> HTMLResponse:
        context = {"invite": invite, "token": token, "name": name, "error": message}
        return render(request, "invite.html", context, status_code=400)

    if password_confirm is not None and password_confirm != password:
        return form_error(PASSWORD_MISMATCH)
    try:
        user = await users.accept_invite(db, token, name=name, password=password, now=now)
    except users.InviteError as exc:
        # accept_invite raises before writing anything; the session closes (rolls back) after.
        return _invite_error(request, exc.reason)
    except ValueError as exc:
        return form_error(str(exc))

    ip = client_ip(request)
    await audit.record(
        db,
        action=audit.INVITE_ACCEPTED,
        actor_id=user.id,
        target_type="invite",
        target_id=str(invite.id),
        details={"email": user.email, "role": user.role},
        ip=ip,
    )
    raw = await _start_session(request, db, user, settings, now=now)
    await audit.record(
        db,
        action=audit.USER_LOGIN,
        actor_id=user.id,
        target_type="user",
        target_id=str(user.id),
        details={"via": "invite"},
        ip=ip,
    )
    await db.commit()
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, raw, settings)
    return response
