"""Server-side login sessions; the cookie carries a raw token, the DB its SHA-256 hash."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.auth.tokens import hash_token, new_token
from salesfinder.db.models import User, UserSession

_IP_MAX = 64
_USER_AGENT_MAX = 512


def require_aware(now: datetime) -> datetime:
    """Reject naive datetimes so every comparison against timestamptz is in absolute time."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")
    return now


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    ttl: timedelta,
    now: datetime,
    ip: str | None,
    user_agent: str | None,
) -> tuple[UserSession, str]:
    require_aware(now)
    raw = new_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw),
        created_at=now,
        expires_at=now + ttl,
        last_seen_at=now,
        ip=ip[:_IP_MAX] if ip else None,
        user_agent=user_agent[:_USER_AGENT_MAX] if user_agent else None,
    )
    db.add(session)
    await db.flush()
    return session, raw


async def resolve_session(
    db: AsyncSession,
    raw: str | None,
    *,
    now: datetime,
    touch_after: timedelta = timedelta(0),
) -> tuple[UserSession, User] | None:
    """Return the live session and its user, or None if unknown/expired/revoked/inactive.

    ``last_seen_at`` moves to ``now`` only once it is at least ``touch_after`` old, so frequent
    requests (HTMX polling) don't write on every call.
    """
    require_aware(now)
    if not raw:
        return None
    row = (
        await db.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(raw))
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    session, user = row
    if session.revoked_at is not None or session.expires_at <= now:
        return None
    if not user.is_active:
        session.revoked_at = now
        await db.flush()
        return None
    if now - session.last_seen_at >= touch_after:
        session.last_seen_at = now
        await db.flush()
    return session, user


async def revoke_session(db: AsyncSession, raw: str, *, now: datetime) -> None:
    require_aware(now)
    await db.execute(
        update(UserSession)
        .where(UserSession.token_hash == hash_token(raw), UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> int:
    """Revoke every not-yet-revoked session of the user; returns how many were revoked."""
    require_aware(now)
    revoked = await db.scalars(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(UserSession.id)
    )
    return len(revoked.all())
