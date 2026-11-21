"""What the Today and Settings pages show about sending: the global state, the bounce rate and
each mailbox's day (sent today against today's cap, and why it may be holding). Reads only."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.config import AppConfig
from salesfinder.db.models import Mailbox, User
from salesfinder.sending import mailboxes, policy
from salesfinder.sending import state as sending_state
from salesfinder.sending.state import SendingState

DISABLED = "Disabled."


@dataclass(frozen=True)
class MailboxLine:
    mailbox: Mailbox
    sent_today: int
    cap_today: int
    hold: str | None


@dataclass(frozen=True)
class SendingView:
    state: SendingState
    changed_by: str | None
    bounces: int
    window: int
    mailboxes: list[MailboxLine]

    @property
    def bounce_pct(self) -> int:
        return round(100 * self.bounces / self.window) if self.window else 0

    @property
    def changed_at_value(self) -> str:
        """The state's ``changed_at`` as shown (a resume compares it): "" if never written."""
        return self.state.changed_at.isoformat() if self.state.changed_at else ""


async def sending_view(db: AsyncSession, *, now: datetime, config: AppConfig) -> SendingView:
    state = await sending_state.get_state(db)
    changed_by = (
        await db.scalar(select(User.name).where(User.id == state.changed_by_id))
        if state.changed_by_id
        else None
    )
    bounces, _ = await sending_state.bounce_rate(db, now=now, config=config)
    sent = await mailboxes.sent_today_counts(db, now=now)
    lines: list[MailboxLine] = []
    for box in await db.scalars(select(Mailbox).order_by(Mailbox.side, Mailbox.address)):
        count = sent.get(box.id, 0)
        hold = (
            DISABLED
            if not box.enabled
            else box.hold_reason
            or policy.ramp_hold(box, today=now.date(), sent_today=count, config=config)
        )
        lines.append(
            MailboxLine(box, count, policy.ramp_cap(box, today=now.date(), config=config), hold)
        )
    return SendingView(state, changed_by, bounces, config.sending.bounce_window, lines)
