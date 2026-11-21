"""Per-signal settings: which signals the team has confirmed for collection.

Services flush; the caller commits."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import SignalSetting, User
from salesfinder.signals.catalog import SIGNALS, by_code

STATES = ("proposed", "confirmed", "skipped", "rejected")


async def states(db: AsyncSession) -> dict[str, str]:
    stored = dict((await db.execute(select(SignalSetting.code, SignalSetting.state))).all())
    return {s.code: stored.get(s.code, "proposed") for s in SIGNALS}


async def set_state(db: AsyncSession, code: str, state: str, *, actor: User | None) -> None:
    try:
        by_code(code)
    except KeyError:
        raise ValueError(f"unknown signal code {code!r}") from None
    if state not in STATES:
        raise ValueError(f"unknown signal state {state!r}")
    actor_id = actor.id if actor else None
    stmt = insert(SignalSetting).values(code=code, state=state, updated_by_id=actor_id)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SignalSetting.code],
        set_={"state": state, "updated_by_id": actor_id, "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.flush()


async def confirmed_codes(db: AsyncSession, side: str | None = None) -> frozenset[str]:
    current = await states(db)
    return frozenset(
        s.code
        for s in SIGNALS
        if current[s.code] == "confirmed" and (side is None or s.side == side)
    )


async def confirm_free(db: AsyncSession, *, actor: User | None) -> int:
    current = await states(db)
    changed = 0
    for s in SIGNALS:
        if s.cost == "free" and current[s.code] != "confirmed":
            await set_state(db, s.code, "confirmed", actor=actor)
            changed += 1
    return changed
