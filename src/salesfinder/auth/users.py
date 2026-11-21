"""User lifecycle: invites, admin bootstrap, password login, activation."""

import uuid
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.auth.emails import normalize_email
from salesfinder.auth.passwords import (
    dummy_hash,
    hash_password,
    needs_rehash,
    password_problems,
    verify_password,
)
from salesfinder.auth.sessions import require_aware, revoke_all_for_user
from salesfinder.auth.tokens import hash_token, new_token
from salesfinder.db.models import Invite, User

InviteFailure = Literal["invalid", "expired", "used", "exists"]
ROLES = ("admin", "member")
INVITE_TTL = timedelta(hours=72)
INVITE_USED = "That invite was already used."


def _revoked_expiry(invite: Invite, now: datetime) -> datetime:
    """The revoked marker: ``expires_at`` no later than the invite's creation. An accept
    compares against a clock it read *before* waiting for the row lock, so ``expires_at = now``
    could still be later than that clock and the accept would win; no accept's clock is earlier
    than the invite's ``created_at``, so this loses to every accept, whatever it read."""
    return min(now, invite.created_at)


_NAME_MAX = 200


class InviteError(Exception):
    def __init__(self, reason: InviteFailure) -> None:
        super().__init__(reason)
        self.reason: InviteFailure = reason


def _check_password(password: str) -> None:
    problems = password_problems(password)
    if problems:
        raise ValueError(" ".join(problems))


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Name is required.")
    if len(cleaned) > _NAME_MAX:
        raise ValueError(f"Name must be at most {_NAME_MAX} characters.")
    return cleaned


async def _user_by_email(db: AsyncSession, email: str) -> User | None:
    user: User | None = await db.scalar(select(User).where(User.email == email))
    return user


async def create_invite(
    db: AsyncSession,
    *,
    email: str,
    role: str,
    invited_by: User,
    now: datetime,
    ttl: timedelta = INVITE_TTL,
) -> str:
    """Create an invite and return its raw token (only its hash is stored)."""
    require_aware(now)
    normalized = normalize_email(email)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    if await _user_by_email(db, normalized) is not None:
        raise InviteError("exists")
    raw = new_token()
    db.add(
        Invite(
            email=normalized,
            role=role,
            token_hash=hash_token(raw),
            invited_by_id=invited_by.id,
            created_at=now,
            expires_at=now + ttl,
        )
    )
    await db.flush()
    return raw


async def accept_invite(
    db: AsyncSession, raw_token: str, *, name: str, password: str, now: datetime
) -> User:
    require_aware(now)
    invite = await db.scalar(
        select(Invite)
        .where(Invite.token_hash == hash_token(raw_token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invite is None:
        raise InviteError("invalid")
    if invite.accepted_at is not None:
        raise InviteError("used")
    if invite.expires_at <= now:
        raise InviteError("expired")
    clean_name = _clean_name(name)
    _check_password(password)
    if await _user_by_email(db, invite.email) is not None:
        raise InviteError("exists")
    user = User(
        email=invite.email,
        name=clean_name,
        role=invite.role,
        password_hash=hash_password(password),
    )
    invite.accepted_at = now
    db.add(user)
    await db.flush()
    return user


async def create_admin(db: AsyncSession, *, email: str, name: str, password: str) -> User:
    normalized = normalize_email(email)
    clean_name = _clean_name(name)
    _check_password(password)
    if await _user_by_email(db, normalized) is not None:
        raise ValueError("A user with that email already exists.")
    user = User(
        email=normalized, name=clean_name, role="admin", password_hash=hash_password(password)
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(
    db: AsyncSession, email: str, password: str, *, now: datetime
) -> User | None:
    """Return the user for valid credentials, else None.

    Unknown emails still pay for one Argon2 verification, so response time does not reveal
    whether an account exists.
    """
    require_aware(now)
    try:
        normalized: str | None = normalize_email(email)
    except ValueError:
        normalized = None
    user = await _user_by_email(db, normalized) if normalized else None
    if user is None or user.password_hash is None:
        verify_password(dummy_hash(), password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if not user.is_active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = now
    await db.flush()
    return user


async def revoke_invite(db: AsyncSession, invite_id: uuid.UUID, *, now: datetime) -> Invite:
    """Revoke a pending invite (caller audits and commits): ``expires_at`` becomes the revoked
    marker (see ``_revoked_expiry``). ``LookupError`` if there is no such invite or it already
    expired (or was revoked); ``ValueError(INVITE_USED)`` if it was accepted. Locks the row, as
    ``accept_invite`` does, so exactly one of the two wins."""
    require_aware(now)
    invite = await db.scalar(
        select(Invite)
        .where(Invite.id == invite_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invite is None:
        raise LookupError("No such invite.")
    if invite.accepted_at is not None:
        raise ValueError(INVITE_USED)
    if invite.expires_at <= now:
        raise LookupError("That invite already expired.")
    invite.expires_at = _revoked_expiry(invite, now)
    await db.flush()
    return invite


async def expire_invites_from(db: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> int:
    """Revoke every pending (unaccepted, unexpired) invite ``user_id`` sent, with the same
    marker as ``revoke_invite``; return how many."""
    require_aware(now)
    pending = list(
        await db.scalars(
            select(Invite)
            .where(
                Invite.invited_by_id == user_id,
                Invite.accepted_at.is_(None),
                Invite.expires_at > now,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for invite in pending:
        invite.expires_at = _revoked_expiry(invite, now)
    await db.flush()
    return len(pending)


async def set_active(
    db: AsyncSession, user_id: uuid.UUID, active: bool, *, now: datetime
) -> tuple[User, int]:
    """Activate or deactivate a user. Deactivating revokes their sessions and expires the
    pending invites they sent; returns the user and how many invites were expired (0 when
    activating: expired invites stay expired)."""
    require_aware(now)
    user = await db.get(User, user_id)
    if user is None:
        raise LookupError("No such user.")
    invites_revoked = 0
    if not active:
        await revoke_all_for_user(db, user_id, now=now)
        invites_revoked = await expire_invites_from(db, user_id, now=now)
    user.is_active = active
    await db.flush()
    return user, invites_revoked


async def reset_password(db: AsyncSession, *, email: str, password: str, now: datetime) -> User:
    """Set a new password and revoke every session of the user (caller commits)."""
    require_aware(now)
    _check_password(password)
    user = await _user_by_email(db, normalize_email(email))
    if user is None:
        raise LookupError("No such user.")
    user.password_hash = hash_password(password)
    await revoke_all_for_user(db, user.id, now=now)
    await db.flush()
    return user


async def find_by_email(db: AsyncSession, email: str) -> User | None:
    """Look a user up by any spelling of their email (ValueError if it is not an email)."""
    return await _user_by_email(db, normalize_email(email))
