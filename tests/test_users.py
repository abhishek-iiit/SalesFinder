import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from salesfinder import audit
from salesfinder.auth import sessions, users
from salesfinder.auth.tokens import hash_token
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import AuditLog, Invite, User

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
PASSWORD = "long enough pass"  # noqa: S105
NEW_PASSWORD = "a brand new password"  # noqa: S105


async def _admin(db: AsyncSession) -> User:
    admin = await users.create_admin(db, email="Boss@Elchemy.com ", name="Boss", password=PASSWORD)
    await db.commit()
    return admin


async def _member(db: AsyncSession, email: str = "member@elchemy.com") -> User:
    admin = await _admin(db)
    raw = await users.create_invite(db, email=email, role="member", invited_by=admin, now=NOW)
    user = await users.accept_invite(db, raw, name="Member", password=PASSWORD, now=NOW)
    await db.commit()
    return user


async def _user_count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


async def test_invite_accept_login_with_messy_email(db: AsyncSession) -> None:
    admin = await _admin(db)
    assert admin.email == "boss@elchemy.com"
    raw = await users.create_invite(
        db, email=" New.User@Elchemy.com", role="member", invited_by=admin, now=NOW
    )
    user = await users.accept_invite(
        db, raw, name="New", password=PASSWORD, now=NOW + timedelta(hours=1)
    )
    await db.commit()
    assert user.email == "new.user@elchemy.com" and user.role == "member"
    assert await users.authenticate(db, "NEW.USER@elchemy.com ", PASSWORD, now=NOW) == user


async def test_invite_expired_used_invalid(db: AsyncSession) -> None:
    admin = await _admin(db)

    expired = await users.create_invite(
        db, email="late@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    await db.commit()
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(
            db, expired, name="Late", password=PASSWORD, now=NOW + timedelta(hours=73)
        )
    assert exc.value.reason == "expired"
    assert await _user_count(db) == 1

    raw = await users.create_invite(
        db, email="twice@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    await users.accept_invite(db, raw, name="Twice", password=PASSWORD, now=NOW)
    await db.commit()
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, raw, name="Twice", password=PASSWORD, now=NOW)
    assert exc.value.reason == "used"
    assert await _user_count(db) == 2

    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, "made-up-token", name="X", password=PASSWORD, now=NOW)
    assert exc.value.reason == "invalid"
    assert await _user_count(db) == 2


async def test_accept_invite_rejects_short_password_without_consuming(db: AsyncSession) -> None:
    admin = await _admin(db)
    raw = await users.create_invite(
        db, email="short@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    await db.commit()
    with pytest.raises(ValueError, match="at least 12 characters"):
        await users.accept_invite(db, raw, name="Short", password="short", now=NOW)  # noqa: S106
    user = await users.accept_invite(db, raw, name="Short", password=PASSWORD, now=NOW)
    assert user.email == "short@elchemy.com"


async def test_invite_for_existing_user_refused(db: AsyncSession) -> None:
    admin = await _admin(db)
    with pytest.raises(users.InviteError) as exc:
        await users.create_invite(
            db, email=" BOSS@elchemy.com", role="member", invited_by=admin, now=NOW
        )
    assert exc.value.reason == "exists"


async def test_authenticate_rejects_wrong_unknown_inactive(db: AsyncSession) -> None:
    user = await _member(db)
    assert await users.authenticate(db, user.email, PASSWORD, now=NOW) == user
    assert user.last_login_at == NOW
    assert await users.authenticate(db, user.email, "wrong password!!", now=NOW) is None
    assert await users.authenticate(db, "nobody@elchemy.com", PASSWORD, now=NOW) is None
    assert await users.authenticate(db, "not-an-email", PASSWORD, now=NOW) is None
    await users.set_active(db, user.id, False, now=NOW)
    await db.commit()
    assert await users.authenticate(db, user.email, PASSWORD, now=NOW) is None


async def test_session_lifecycle(db: AsyncSession) -> None:
    user = await _member(db)
    session, raw = await sessions.create_session(
        db, user, ttl=timedelta(hours=12), now=NOW, ip="127.0.0.1", user_agent="pytest"
    )
    await db.commit()
    assert session.token_hash != raw

    resolved = await sessions.resolve_session(db, raw, now=NOW + timedelta(hours=11))
    assert resolved == (session, user)
    assert session.last_seen_at == NOW + timedelta(hours=11)

    assert await sessions.resolve_session(db, raw, now=NOW + timedelta(hours=13)) is None
    assert await sessions.resolve_session(db, None, now=NOW) is None
    assert await sessions.resolve_session(db, "made-up-token", now=NOW) is None

    await sessions.revoke_session(db, raw, now=NOW + timedelta(hours=1))
    await db.commit()
    assert await sessions.resolve_session(db, raw, now=NOW + timedelta(hours=2)) is None


async def test_resolve_session_touch_after_throttles_last_seen(db: AsyncSession) -> None:
    user = await _member(db)
    session, raw = await sessions.create_session(
        db, user, ttl=timedelta(hours=12), now=NOW, ip=None, user_agent=None
    )
    minute = timedelta(seconds=60)
    await sessions.resolve_session(db, raw, now=NOW + timedelta(seconds=59), touch_after=minute)
    assert session.last_seen_at == NOW
    await sessions.resolve_session(db, raw, now=NOW + minute, touch_after=minute)
    assert session.last_seen_at == NOW + minute


async def test_deactivation_revokes_sessions(db: AsyncSession) -> None:
    user = await _member(db)
    session, raw = await sessions.create_session(
        db, user, ttl=timedelta(hours=12), now=NOW, ip=None, user_agent=None
    )
    await db.commit()
    await users.set_active(db, user.id, False, now=NOW)
    await db.commit()
    assert await sessions.resolve_session(db, raw, now=NOW) is None
    await db.refresh(session)
    assert session.revoked_at == NOW


async def test_resolve_revokes_session_of_inactive_user(db: AsyncSession) -> None:
    user = await _member(db)
    session, raw = await sessions.create_session(
        db, user, ttl=timedelta(hours=12), now=NOW, ip=None, user_agent=None
    )
    user.is_active = False  # deactivated without going through set_active
    await db.commit()
    assert await sessions.resolve_session(db, raw, now=NOW) is None
    await db.commit()
    await db.refresh(session)
    assert session.revoked_at == NOW


async def test_audit_record(db: AsyncSession) -> None:
    u = await _member(db)
    row = await audit.record(
        db, action="user.login", actor_id=u.id, ip="127.0.0.1", details={"a": 1}
    )
    await db.commit()
    stored = await db.scalar(select(AuditLog).where(AuditLog.id == row.id))
    assert stored is not None
    assert stored.details == {"a": 1}
    assert stored.action == "user.login" and stored.actor_id == u.id and stored.ip == "127.0.0.1"


async def test_accept_invite_sees_concurrent_acceptance_as_used(
    db: AsyncSession, engine: AsyncEngine
) -> None:
    admin = await _admin(db)
    raw = await users.create_invite(
        db, email="race@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    await db.commit()
    # Pre-load the invite (unaccepted) into this session's identity map.
    assert (await db.scalar(select(Invite))).accepted_at is None  # type: ignore[union-attr]
    async with make_session_factory(engine)() as other:
        await other.execute(update(Invite).values(accepted_at=NOW))
        await other.commit()
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, raw, name="Loser", password=PASSWORD, now=NOW)
    assert exc.value.reason == "used"


async def test_reset_password(db: AsyncSession) -> None:
    member = await _member(db)
    _, raw_session = await sessions.create_session(
        db, member, now=NOW, ttl=timedelta(hours=1), ip=None, user_agent=None
    )
    await db.commit()
    updated = await users.reset_password(
        db, email=" Member@Elchemy.com", password=NEW_PASSWORD, now=NOW
    )
    await db.commit()
    assert updated.id == member.id
    assert await users.authenticate(db, "member@elchemy.com", PASSWORD, now=NOW) is None
    assert await users.authenticate(db, "member@elchemy.com", NEW_PASSWORD, now=NOW)
    assert await sessions.resolve_session(db, raw_session, now=NOW) is None


async def test_reset_password_rejects_short_and_unknown(db: AsyncSession) -> None:
    await _member(db)
    with pytest.raises(ValueError, match="12 characters"):
        await users.reset_password(
            db,
            email="member@elchemy.com",
            password="short",  # noqa: S106
            now=NOW,
        )
    with pytest.raises(LookupError):
        await users.reset_password(db, email="nobody@elchemy.com", password=NEW_PASSWORD, now=NOW)


# --------------------------------------------------------------------------- invite revocation


async def _invite_row(db: AsyncSession, raw: str) -> Invite:
    invite = await db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    assert invite is not None
    return invite


async def test_revoke_invite_expires_it_and_accept_fails(db: AsyncSession) -> None:
    admin = await _admin(db)
    raw = await users.create_invite(
        db, email="new@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    invite = await _invite_row(db, raw)
    await db.commit()
    later = NOW + timedelta(minutes=5)
    revoked = await users.revoke_invite(db, invite.id, now=later)
    await db.commit()
    # The revoked marker: no later than the invite's creation (see the ordering test below).
    assert revoked.id == invite.id and revoked.expires_at == NOW == invite.created_at
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, raw, name="New", password=PASSWORD, now=later)
    assert exc.value.reason == "expired"
    assert await _user_count(db) == 1


async def test_revoke_invite_missing_or_already_expired(db: AsyncSession) -> None:
    admin = await _admin(db)
    with pytest.raises(LookupError):
        await users.revoke_invite(db, uuid.uuid4(), now=NOW)
    raw = await users.create_invite(
        db, email="new@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    invite = await _invite_row(db, raw)
    await users.revoke_invite(db, invite.id, now=NOW)
    await db.commit()
    # A second revoke (or one after natural expiry) finds nothing pending.
    with pytest.raises(LookupError):
        await users.revoke_invite(db, invite.id, now=NOW + timedelta(seconds=1))
    with pytest.raises(LookupError):
        await users.revoke_invite(db, invite.id, now=NOW)


async def test_revoke_accepted_invite_refused(db: AsyncSession) -> None:
    admin = await _admin(db)
    raw = await users.create_invite(
        db, email="new@elchemy.com", role="member", invited_by=admin, now=NOW
    )
    await users.accept_invite(db, raw, name="New", password=PASSWORD, now=NOW)
    await db.commit()
    invite = await _invite_row(db, raw)
    with pytest.raises(ValueError, match=r"That invite was already used\."):
        await users.revoke_invite(db, invite.id, now=NOW + timedelta(minutes=1))
    assert users.INVITE_USED == "That invite was already used."
    await db.refresh(invite)
    assert invite.expires_at == NOW + users.INVITE_TTL


async def test_expire_invites_from_only_touches_that_users_pending_invites(
    db: AsyncSession,
) -> None:
    boss = await _admin(db)
    other = await users.create_admin(db, email="other@elchemy.com", name="Other", password=PASSWORD)
    pending = [
        await users.create_invite(
            db, email=f"p{i}@elchemy.com", role="member", invited_by=other, now=NOW
        )
        for i in range(2)
    ]
    accepted = await users.create_invite(
        db, email="done@elchemy.com", role="member", invited_by=other, now=NOW
    )
    await users.accept_invite(db, accepted, name="Done", password=PASSWORD, now=NOW)
    old = await users.create_invite(
        db, email="old@elchemy.com", role="member", invited_by=other, now=NOW - timedelta(days=9)
    )
    boss_invite = await users.create_invite(
        db, email="boss-pick@elchemy.com", role="member", invited_by=boss, now=NOW
    )
    await db.commit()

    later = NOW + timedelta(hours=1)
    assert await users.expire_invites_from(db, other.id, now=later) == 2
    await db.commit()
    for raw in pending:
        assert (await _invite_row(db, raw)).expires_at == NOW  # their created_at
    assert (await _invite_row(db, accepted)).expires_at == NOW + users.INVITE_TTL
    assert (await _invite_row(db, old)).expires_at == NOW - timedelta(days=9) + users.INVITE_TTL
    assert (await _invite_row(db, boss_invite)).expires_at == NOW + users.INVITE_TTL
    assert await users.expire_invites_from(db, other.id, now=later) == 0


async def test_deactivation_expires_pending_invites(db: AsyncSession) -> None:
    await _admin(db)
    other = await users.create_admin(db, email="other@elchemy.com", name="Other", password=PASSWORD)
    raws = [
        await users.create_invite(
            db, email=f"p{i}@elchemy.com", role="member", invited_by=other, now=NOW
        )
        for i in range(2)
    ]
    await db.commit()
    user, revoked = await users.set_active(db, other.id, False, now=NOW)
    await db.commit()
    assert user.id == other.id and not user.is_active and revoked == 2
    for raw in raws:
        with pytest.raises(users.InviteError) as exc:
            await users.accept_invite(db, raw, name="P", password=PASSWORD, now=NOW)
        assert exc.value.reason == "expired"
    # Reactivating doesn't bring them back.
    user, revoked = await users.set_active(db, other.id, True, now=NOW)
    assert user.is_active and revoked == 0


async def test_revoke_beats_an_accept_whose_clock_was_read_earlier(db: AsyncSession) -> None:
    """I1: an accept reads ``now`` before it waits for the row lock; a revoke committed in that
    gap (with a later clock) must still win."""
    admin = await _admin(db)
    created = NOW
    raw = await users.create_invite(
        db, email="race@elchemy.com", role="member", invited_by=admin, now=created
    )
    invite = await _invite_row(db, raw)
    await db.commit()
    accept_clock = created + timedelta(milliseconds=500)  # read by the accept first
    await users.revoke_invite(db, invite.id, now=created + timedelta(seconds=1))
    await db.commit()
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, raw, name="Race", password=PASSWORD, now=accept_clock)
    assert exc.value.reason == "expired"
    with pytest.raises(users.InviteError):
        await users.accept_invite(db, raw, name="Race", password=PASSWORD, now=created)
    assert await _user_count(db) == 1


async def test_deactivation_beats_an_accept_whose_clock_was_read_earlier(
    db: AsyncSession,
) -> None:
    await _admin(db)
    other = await users.create_admin(db, email="other@elchemy.com", name="Other", password=PASSWORD)
    raw = await users.create_invite(
        db, email="race@elchemy.com", role="member", invited_by=other, now=NOW
    )
    await db.commit()
    _, revoked = await users.set_active(db, other.id, False, now=NOW + timedelta(seconds=1))
    await db.commit()
    assert revoked == 1
    with pytest.raises(users.InviteError) as exc:
        await users.accept_invite(db, raw, name="Race", password=PASSWORD, now=NOW)
    assert exc.value.reason == "expired"
