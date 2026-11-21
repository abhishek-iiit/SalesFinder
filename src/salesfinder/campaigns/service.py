"""Campaigns (spec §6.1): create, activate, pause, resume and archive; choose one recipient per
company; and draft — Claude writes a two-email sequence, the gate checks the first email, and
both wait for the Outbox. Nothing is sent here (Phase 6).

Lifecycle services flush; callers commit and audit. ``draft_batch`` owns its transactions and
never holds one open while Claude writes (or while DNS refreshes a stale mail-server check): it
reads what it needs, closes the session, calls Claude, then stores the sequence in one short
transaction under a row lock on the campaign.

A paused campaign doesn't draft, but its waiting drafts can still be approved; sending
(Phase 6) never sends for a paused campaign. Archiving stops the campaign's unfinished
sequences and cancels its unsent emails, which frees their addresses for other campaigns.
"""

import asyncio
import enum
import sys
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, case, delete, func, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.brain.budget import BudgetReached, remaining_calls
from salesfinder.brain.runtime import SIGNED_OUT, BrainError, BrainUnavailable, ClaudeRuntime
from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.cohorts.evaluate import list_matches, unconfirmed_conditions
from salesfinder.cohorts.model import CohortDefinition
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker, mx_domain, refresh_mx, stale_mx_domains
from salesfinder.contacts.service import HELD, unsendable_reason
from salesfinder.db.models import (
    Campaign,
    Cohort,
    Company,
    DomainMx,
    DraftFailure,
    EmailAddress,
    Mailbox,
    Message,
    Person,
    Sequence,
    User,
)
from salesfinder.db.models.outreach import SIDES
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email
from salesfinder.exclusions.check import exclusion_for
from salesfinder.gate.checks import gate_message
from salesfinder.outbox.service import waiting_for_review
from salesfinder.outreach.locks import lock_recipient
from salesfinder.signals.catalog import by_code
from salesfinder.signals.settings import confirmed_codes
from salesfinder.writer.compose import assemble, prompt_for, write_sequence
from salesfinder.writer.context import build_context

NAME_TAKEN = "A campaign with that name already exists."
GONE = "That campaign no longer exists."
COHORT_GONE = "That cohort no longer exists."
COHORT_OTHER_SIDE = "That cohort is for the other side."
COHORT_INVALID = "That cohort's definition is no longer valid; edit and save the cohort."
BAD_SIDE = "Side must be buyer or supplier."
MAX_DAILY_CAP = 200
CAP_RANGE = f"The daily draft cap must be 1 to {MAX_DAILY_CAP}."
POSTAL_UNCONFIRMED = "Confirm Elchemy's postal address in facts.yaml first."
NOT_ACTIVATABLE = "Only a draft or paused campaign can be activated."
NOT_PAUSABLE = "Only an active campaign can be paused."
NOT_RESUMABLE = "Only a paused campaign can be resumed."
ALREADY_ARCHIVED = "That campaign is already archived."
CHOOSE_MAILBOX = "Choose a sending mailbox for this campaign."
MAILBOX_OTHER_SIDE = "That mailbox is for the other side."
MAILBOX_GONE = "That mailbox no longer exists."
ARCHIVED_NO_MAILBOX = "An archived campaign's mailbox can't be changed."
# The unique constraints a concurrent create with the same name (any case) can hit.
_NAME_INDEXES = ("uq_campaigns_name", "uq_campaigns_lower_name")
ARCHIVE_STOP_REASON = "Campaign archived"
# Sequences that hold their address: no other draft may go to it.
IN_PROGRESS = ("awaiting_approval", "active", "replied")
# Sequence statuses that are final; archiving stops every other one.
FINISHED = ("completed", "stopped", "failed")
# Messages not sent yet (an approved one waits for Phase 6); archiving cancels them.
UNSENT = ("pending", "awaiting_approval", "gate_failed", "approved")
_LAST_ERROR_MAX = 300


def persona_unconfirmed(side: str) -> str:
    return f"Persona for {side} isn't confirmed in facts.yaml."


def stale_cohort(labels: Collection[str]) -> str:
    return f"This cohort uses signals that aren't being collected: {', '.join(labels)}."


# --------------------------------------------------------------------------- lifecycle


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("A campaign name must be 1 to 120 characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("A campaign name can't contain control characters.")
    return name


async def create_campaign(
    db: AsyncSession,
    *,
    name: str,
    side: str,
    cohort_id: uuid.UUID,
    daily_draft_cap: int,
    actor: User,
) -> Campaign:
    """A new ``draft`` campaign; its playbook is its side, and the cohort must be of that
    side. Names are unique regardless of case."""
    name = _clean_name(name)
    if side not in SIDES:
        raise ValueError(BAD_SIDE)
    if not 1 <= daily_draft_cap <= MAX_DAILY_CAP:
        raise ValueError(CAP_RANGE)
    cohort = await db.get(Cohort, cohort_id)
    if cohort is None:
        raise ValueError(COHORT_GONE)
    if cohort.side != side:
        raise ValueError(COHORT_OTHER_SIDE)
    taken = await db.scalar(
        select(func.count()).select_from(Campaign).where(func.lower(Campaign.name) == name.lower())
    )
    if taken:
        raise ValueError(NAME_TAKEN)
    campaign = Campaign(
        name=name,
        side=side,
        playbook=side,
        cohort_id=cohort_id,
        status="draft",
        daily_draft_cap=daily_draft_cap,
        created_by_id=actor.id,
    )
    try:
        async with db.begin_nested():  # a concurrent create can take the name first
            db.add(campaign)
            await db.flush()
    except IntegrityError as exc:
        if any(index in str(exc.orig) for index in _NAME_INDEXES):
            raise ValueError(NAME_TAKEN) from None
        raise
    return campaign


# Campaign rows are locked FOR NO KEY UPDATE everywhere: every locker changes only non-key
# columns (status, activated_at, last_error), and the weaker lock doesn't block the FOR KEY
# SHARE that inserting a row referencing the campaign takes (outbox feedback on reject, usage
# events, sequences). FOR UPDATE would, and a reject inserting feedback while archive held
# the campaign deadlocked. Nothing here deletes a campaign or changes its id or name, the only
# changes that would need FOR UPDATE.
_CAMPAIGN_LOCK = {"key_share": True}


async def _locked(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if campaign is None:
        raise ValueError(GONE)
    return campaign


async def activation_problems(db: AsyncSession, campaign: Campaign, *, facts: Facts) -> list[str]:
    """What stops ``campaign`` from drafting now, in order: its cohort (gone, other side,
    invalid, or using signals nobody collects), the side's persona, the postal address. The
    same checks run before every drafting pass, so nothing is spent on drafts the gate would
    hold."""
    problems: list[str] = []
    cohort = await db.get(Cohort, campaign.cohort_id)
    if cohort is None:
        problems.append(COHORT_GONE)
    elif cohort.side != campaign.side:
        problems.append(COHORT_OTHER_SIDE)
    else:
        try:
            defn = CohortDefinition.model_validate(cohort.definition)
        except ValidationError:
            problems.append(COHORT_INVALID)
        else:
            stale = unconfirmed_conditions(defn, await confirmed_codes(db, defn.side))
            if stale:
                labels = list(dict.fromkeys(by_code(c.code).label for c in stale))
                problems.append(stale_cohort(labels))
    persona = next((p for side, p in facts.personas.items() if side == campaign.side), None)
    if (
        persona is None
        or not persona.confirmed
        or not persona.name.strip()
        or not persona.title.strip()
    ):
        problems.append(persona_unconfirmed(campaign.side))
    if not facts.postal_address_confirmed or not facts.postal_address.strip():
        problems.append(POSTAL_UNCONFIRMED)
    # An active campaign without a mailbox keeps drafting: its mail is simply held at send time.
    if campaign.mailbox_id is None and campaign.status != "active":
        problems.append(CHOOSE_MAILBOX)
    return problems


async def _start(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    allowed: tuple[str, ...],
    refusal: str,
    facts: Facts,
    now: datetime | None,
) -> Campaign:
    campaign = await _locked(db, campaign_id)
    if campaign.status not in allowed:
        raise ValueError(refusal)
    problems = await activation_problems(db, campaign, facts=facts)
    if problems:
        raise ValueError(problems[0])
    campaign.status = "active"
    if campaign.activated_at is None:  # "oldest activated first" keeps the first activation
        campaign.activated_at = now or datetime.now(UTC)
    campaign.last_error = None
    await db.flush()
    return campaign


async def activate(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    actor: User | None,
    facts: Facts,
    config: AppConfig,
    now: datetime | None = None,
) -> Campaign:
    """A draft or paused campaign becomes ``active`` (raises ``ValueError`` with the first
    activation problem otherwise)."""
    return await _start(
        db,
        campaign_id,
        allowed=("draft", "paused"),
        refusal=NOT_ACTIVATABLE,
        facts=facts,
        now=now,
    )


async def resume(
    db: AsyncSession,
    campaign_id: uuid.UUID,
    *,
    actor: User | None,
    facts: Facts,
    config: AppConfig,
    now: datetime | None = None,
) -> Campaign:
    """A paused campaign becomes ``active`` again, after the same checks as activation."""
    return await _start(
        db, campaign_id, allowed=("paused",), refusal=NOT_RESUMABLE, facts=facts, now=now
    )


async def pause(db: AsyncSession, campaign_id: uuid.UUID, *, actor: User | None) -> Campaign:
    campaign = await _locked(db, campaign_id)
    if campaign.status != "active":
        raise ValueError(NOT_PAUSABLE)
    campaign.status = "paused"
    await db.flush()
    return campaign


async def set_mailbox(
    db: AsyncSession, campaign_id: uuid.UUID, mailbox_id: uuid.UUID, *, actor: User | None
) -> Campaign:
    """Choose the mailbox a campaign sends from (it must be for the campaign's side). Flushes;
    the caller commits and audits."""
    campaign = await _locked(db, campaign_id)
    if campaign.status == "archived":
        raise ValueError(ARCHIVED_NO_MAILBOX)
    mailbox = await db.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise ValueError(MAILBOX_GONE)
    if mailbox.side != campaign.side:
        raise ValueError(MAILBOX_OTHER_SIDE)
    campaign.mailbox_id = mailbox.id
    await db.flush()
    return campaign


@dataclass(frozen=True)
class ArchiveOutcome:
    campaign: Campaign
    sequences_stopped: int
    messages_cancelled: int


async def archive(
    db: AsyncSession, campaign_id: uuid.UUID, *, actor: User | None
) -> ArchiveOutcome:
    """Archived for good: never drafts again. In the same transaction every unfinished
    sequence of the campaign is ``stopped`` ("Campaign archived") and every unsent email
    (pending, waiting, held or approved) is ``cancelled``, so the Outbox drops them and their
    addresses are free for other campaigns. Flushes; the caller commits and audits the
    counts."""
    campaign = await _locked(db, campaign_id)
    if campaign.status == "archived":
        raise ValueError(ALREADY_ARCHIVED)
    campaign.status = "archived"
    in_campaign = select(Sequence.id).where(Sequence.campaign_id == campaign_id)
    # Lock messages, then sequences, each in id order. The Outbox (``outbox.service._lock``)
    # locks all of one sequence's messages in id order, then that sequence, so the two orders
    # agree and can't cross.
    messages = list(
        (
            await db.scalars(
                select(Message.id)
                .where(Message.sequence_id.in_(in_campaign), Message.status.in_(UNSENT))
                .order_by(Message.id)
                .with_for_update()
            )
        ).all()
    )
    sequences = list(
        (
            await db.scalars(
                select(Sequence.id)
                .where(Sequence.campaign_id == campaign_id, Sequence.status.not_in(FINISHED))
                .order_by(Sequence.id)
                .with_for_update()
            )
        ).all()
    )
    if messages:
        await db.execute(
            update(Message)
            .where(Message.id.in_(messages))
            .values(status="cancelled")
            .execution_options(synchronize_session="fetch")
        )
    if sequences:
        await db.execute(
            update(Sequence)
            .where(Sequence.id.in_(sequences))
            .values(status="stopped", stop_reason=ARCHIVE_STOP_REASON)
            .execution_options(synchronize_session="fetch")
        )
    await db.flush()
    return ArchiveOutcome(campaign, len(sequences), len(messages))


# --------------------------------------------------------------------------- counts


def _utc_day(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def drafted_today(db: AsyncSession, campaign_id: uuid.UUID, *, now: datetime) -> int:
    """Sequences the campaign created in ``now``'s UTC day, whatever the gate said (the daily
    draft cap counts these)."""
    start, end = _utc_day(now)
    count = await db.scalar(
        select(func.count())
        .select_from(Sequence)
        .where(
            Sequence.campaign_id == campaign_id,
            Sequence.created_at >= start,
            Sequence.created_at < end,
        )
    )
    return int(count or 0)


@dataclass(frozen=True)
class CampaignSummary:
    campaign: Campaign
    cohort_name: str
    drafted_today: int
    awaiting_approval: int  # first emails ready for a teammate in the Outbox (same filter)


async def list_campaigns(db: AsyncSession, *, now: datetime) -> list[CampaignSummary]:
    """Every campaign, newest first, with today's drafts and Outbox count."""
    rows = (
        await db.execute(
            select(Campaign, Cohort.name)
            .join(Cohort, Cohort.id == Campaign.cohort_id)
            .order_by(Campaign.created_at.desc(), Campaign.name)
        )
    ).all()
    start, end = _utc_day(now)
    drafted = dict(
        (
            await db.execute(
                select(Sequence.campaign_id, func.count())
                .where(Sequence.created_at >= start, Sequence.created_at < end)
                .group_by(Sequence.campaign_id)
            )
        ).all()
    )
    awaiting = dict(
        (
            await db.execute(
                select(Sequence.campaign_id, func.count())
                .join(Message, Message.sequence_id == Sequence.id)
                .where(*waiting_for_review(), Message.status == "awaiting_approval")
                .group_by(Sequence.campaign_id)
            )
        ).all()
    )
    return [
        CampaignSummary(c, name, drafted.get(c.id, 0), awaiting.get(c.id, 0)) for c, name in rows
    ]


# --------------------------------------------------------------------------- recipient

# Best first: published on their site, verified, built from their confirmed pattern, then an
# address a teammate allowed (a held guess / catch-all, or one no longer on their site).
_STATUS_ORDER = {"published": 0, "verified": 1, "pattern_confirmed": 2}
_OVERRIDDEN = 3


def address_preference(addr: EmailAddress) -> int:
    """Sort key for a person's addresses, best first: published, verified, pattern_confirmed,
    then one a teammate allowed (held or stale), then anything else."""
    if addr.status in HELD or addr.stale:
        return _OVERRIDDEN
    return _STATUS_ORDER.get(addr.status, _OVERRIDDEN + 1)


async def _mx(db: AsyncSession, address: str) -> tuple[bool, str, datetime | None]:
    """(free mail, MX state, checked at) for the address's domain."""
    domain = domain_of_email(address) or address.rpartition("@")[2]
    if domain in FREE_MAIL_DOMAINS:
        return True, "has_mx", None
    name = mx_domain(domain)
    if name is None:
        return False, "unknown", None
    row = (
        await db.execute(select(DomainMx.state, DomainMx.checked_at).where(DomainMx.domain == name))
    ).one_or_none()
    return (False, row.state, row.checked_at) if row is not None else (False, "unknown", None)


async def select_recipient(
    db: AsyncSession, company: Company, *, now: datetime, config: AppConfig
) -> tuple[Person, EmailAddress] | None:
    """The one person and address a campaign writes to at ``company``, or None.

    Targets in ``rank`` order; for each, its linked addresses best first (published, verified,
    pattern_confirmed, then overridden). An address is used only if it is sendable now
    (recomputed live: the sendable rule, the exclusion lists, a fresh MX answer for its
    domain) and isn't in an awaiting-approval or active sequence of any campaign. First hit
    wins. An excluded or disqualified company gets nobody. Read only."""
    live = (
        await db.execute(
            select(Company.status, Company.domain, Company.disqualified).where(
                Company.id == company.id
            )
        )
    ).one_or_none()
    if live is None or live.status == "excluded" or live.disqualified:
        return None
    if await exclusion_for(db, domain=live.domain) is not None:
        return None
    people = (
        await db.scalars(
            select(Person)
            .where(Person.company_id == company.id, Person.is_target)
            .order_by(Person.rank.asc().nulls_last(), Person.full_name, Person.id)
        )
    ).all()
    if not people:
        return None
    addresses = (
        await db.scalars(
            select(EmailAddress).where(
                EmailAddress.company_id == company.id,
                EmailAddress.person_id.in_([p.id for p in people]),
            )
        )
    ).all()
    busy = set(
        (
            await db.scalars(
                select(Sequence.recipient).where(
                    Sequence.recipient.in_([a.address for a in addresses]),
                    Sequence.status.in_(IN_PROGRESS),
                )
            )
        ).all()
    )
    fresh = timedelta(days=config.contacts.mx_cache_days)
    for person in people:
        mine = sorted(
            (a for a in addresses if a.person_id == person.id),
            key=lambda a: (address_preference(a), a.address),
        )
        for addr in mine:
            if addr.address in busy:
                continue
            free, state, checked_at = await _mx(db, addr.address)
            if unsendable_reason(addr, mx_state=state) is not None:
                continue
            if not free and (checked_at is None or now - checked_at >= fresh):
                # The gate would hold it (MX_OUT_OF_DATE). Drafting refreshes stale checks
                # before choosing (``_refresh_stale_mx``); one still stale couldn't be
                # refreshed this pass.
                continue
            if await exclusion_for(db, domain=live.domain, email=addr.address) is not None:
                continue
            return person, addr
    return None


# --------------------------------------------------------------------------- drafting

KEPT_FAILING = "Claude kept failing — stopped for this pass."
# Claude-side failures in a row (timeouts, errors, unusable replies) before the pass stops.
MAX_CLAUDE_FAILURES_IN_A_ROW = 3
# A company drafting failed for is left alone this long, and for the rest of the UTC day once
# it has failed this many times that day.
FAILURE_BACKOFF = timedelta(hours=6)
MAX_FAILURES_PER_DAY = 3
_FAILURE_ERROR_MAX = 200


@dataclass(frozen=True)
class DraftOutcome:
    drafted: int = 0  # sequences created (held ones included)
    gate_failed: int = 0  # of those, first emails the gate held
    skipped: int = 0  # companies passed over: no recipient, or company/address taken meanwhile
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    # Campaigns whose walk ended early because, while Claude wrote, the campaign stopped being
    # active or another drafter filled its daily cap (that draft is discarded).
    closed: int = 0


@dataclass
class _Tally:
    drafted: int = 0
    gate_failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    closed: int = 0
    attempts: int = 0  # companies Claude was asked about (the pass's ``limit`` counts these)
    claude_failures: int = 0  # Claude-side failures in a row
    mx_refreshes: int = 0  # stale mail-server checks looked up again this pass
    out_of_time: bool = False  # the pass reached its deadline (or the worker is stopping)

    def freeze(self) -> DraftOutcome:
        return DraftOutcome(
            self.drafted,
            self.gate_failed,
            self.skipped,
            list(self.errors),
            self.stopped_reason,
            self.closed,
        )


class _Result(enum.Enum):
    DRAFTED = "drafted"
    HELD = "held"
    SKIPPED = "skipped"
    ERROR = "error"
    STOP = "stop"  # the whole pass
    CLOSED = "closed"  # this campaign: no longer active, or its daily cap is reached


@dataclass(frozen=True)
class _Plan:
    campaign_id: uuid.UUID
    side: Side
    room: int  # drafts left today under the cap
    candidates: list[uuid.UUID]


@dataclass(frozen=True)
class _Written:
    company_id: uuid.UUID
    person_id: uuid.UUID
    address_id: uuid.UUID
    recipient: str
    prompt: str
    first: tuple[str, str]
    second: tuple[str, str]


@dataclass(frozen=True)
class _Drafting:
    db_factory: async_sessionmaker[AsyncSession]
    runtime: ClaudeRuntime
    catalog: Catalog
    facts: Facts
    config: AppConfig
    now: Callable[[], datetime]
    # Refreshes stale mail-server checks before choosing a recipient; None: never (a stale
    # address is passed over, as the gate would hold it).
    mx_checker: MxChecker | None = None
    max_mx_refreshes: int = 0  # per pass, so skipped companies can't cause unbounded DNS work
    # No new company is started at or after this (so writing never starves sending).
    deadline: datetime | None = None
    # Set when the worker is asked to stop: no new company is started (Ruling P7-G).
    stop: asyncio.Event | None = None


async def _set_last_error(ctx: _Drafting, campaign_id: uuid.UUID, error: str) -> None:
    async with ctx.db_factory() as db, db.begin():
        campaign = await db.get(Campaign, campaign_id, with_for_update=_CAMPAIGN_LOCK)
        if campaign is not None:
            campaign.last_error = error[:_LAST_ERROR_MAX]


async def _tell_active_campaigns(ctx: _Drafting, error: str) -> None:
    """A stop that isn't about one campaign (the budget, Claude unusable): every active
    campaign's last error says why it isn't drafting."""
    async with ctx.db_factory() as db, db.begin():
        await db.execute(
            update(Campaign)
            .where(Campaign.status == "active")
            .values(last_error=error[:_LAST_ERROR_MAX])
            .execution_options(synchronize_session=False)
        )


async def _record_failure(
    ctx: _Drafting, campaign_id: uuid.UUID, company_id: uuid.UUID, error: str, now: datetime
) -> None:
    """Count a failed draft for the company (the count restarts on a new UTC day)."""
    day_start, _ = _utc_day(now)
    insert = pg_insert(DraftFailure).values(
        campaign_id=campaign_id,
        company_id=company_id,
        failures=1,
        last_failed_at=now,
        last_error=error[:_FAILURE_ERROR_MAX],
    )
    insert = insert.on_conflict_do_update(
        index_elements=[DraftFailure.campaign_id, DraftFailure.company_id],
        set_={
            "failures": case(
                (DraftFailure.last_failed_at >= day_start, DraftFailure.failures + 1), else_=1
            ),
            "last_failed_at": insert.excluded.last_failed_at,
            "last_error": insert.excluded.last_error,
        },
    )
    try:
        async with ctx.db_factory() as db, db.begin():
            await db.execute(insert)
    except IntegrityError:
        pass  # the campaign or company was deleted meanwhile: nothing to back off from


async def _backing_off(db: AsyncSession, campaign_id: uuid.UUID, now: datetime) -> set[uuid.UUID]:
    day_start, _ = _utc_day(now)
    rows = await db.scalars(
        select(DraftFailure.company_id).where(
            DraftFailure.campaign_id == campaign_id,
            or_(
                DraftFailure.last_failed_at > now - FAILURE_BACKOFF,
                and_(
                    DraftFailure.failures >= MAX_FAILURES_PER_DAY,
                    DraftFailure.last_failed_at >= day_start,
                ),
            ),
        )
    )
    return set(rows.all())


async def _plan(ctx: _Drafting, campaign_id: uuid.UUID, tally: _Tally) -> _Plan | None:
    """What this campaign may draft now: None if it isn't active, can't draft (the problem is
    recorded as its ``last_error``) or has reached today's cap. Candidates are the cohort's
    matches with a sendable contact, not yet in the campaign and not backed off."""
    problem: str | None = None
    now = ctx.now()
    async with ctx.db_factory() as db:
        campaign = await db.get(Campaign, campaign_id)
        if campaign is None or campaign.status != "active":
            return None
        name, side, last_error = campaign.name, campaign.side, campaign.last_error
        problems = await activation_problems(db, campaign, facts=ctx.facts)
        if problems:
            problem = problems[0]
        else:
            room = campaign.daily_draft_cap - await drafted_today(db, campaign_id, now=now)
            if room <= 0:
                return None
            cohort = await db.get(Cohort, campaign.cohort_id)
            if cohort is None:  # deleted meanwhile (activation_problems saw it)
                return None
            defn = CohortDefinition.model_validate(cohort.definition)
            matches, _ = await list_matches(db, defn, limit=sys.maxsize)
            done = set(
                (
                    await db.scalars(
                        select(Sequence.company_id).where(Sequence.campaign_id == campaign_id)
                    )
                ).all()
            )
            done |= await _backing_off(db, campaign_id, now)
    if problem is not None:
        tally.errors.append(f"Campaign {name}: {problem}")
        if last_error != problem[:_LAST_ERROR_MAX]:
            await _set_last_error(ctx, campaign_id, problem)
        return None
    return _Plan(
        campaign_id=campaign_id,
        side="supplier" if side == "supplier" else "buyer",
        room=room,
        candidates=[
            c.id
            for c in matches
            if c.sendable_contacts > 0 and not c.disqualified and c.id not in done
        ],
    )


async def _stop(
    ctx: _Drafting, plan: _Plan, tally: _Tally, reason: str, *, everywhere: bool
) -> _Result:
    tally.stopped_reason = reason
    if everywhere:
        await _tell_active_campaigns(ctx, reason)
    else:
        await _set_last_error(ctx, plan.campaign_id, reason)
    return _Result.STOP


async def _company_failed(
    ctx: _Drafting,
    plan: _Plan,
    tally: _Tally,
    company_id: uuid.UUID,
    label: str,
    error: str,
    now: datetime,
) -> _Result:
    tally.errors.append(f"{label}: {error}")
    await _record_failure(ctx, plan.campaign_id, company_id, error, now)
    return _Result.ERROR


async def _refresh_stale_mx(
    ctx: _Drafting, company_id: uuid.UUID, tally: _Tally, now: datetime
) -> None:
    """Look up again the stale (or missing) mail-server checks of the company's target
    addresses, so ``select_recipient`` sees fresh answers instead of passing them over. The
    read session is closed before DNS runs; at most ``max_mx_refreshes`` lookups per pass."""
    room = ctx.max_mx_refreshes - tally.mx_refreshes
    if ctx.mx_checker is None or room <= 0:
        return
    async with ctx.db_factory() as db:
        addresses = (
            await db.scalars(
                select(EmailAddress.address)
                .join(Person, Person.id == EmailAddress.person_id)
                .join(Company, Company.id == EmailAddress.company_id)
                .where(
                    EmailAddress.company_id == company_id,
                    Person.company_id == company_id,
                    Person.is_target,
                    Company.status != "excluded",
                    ~Company.disqualified,
                )
            )
        ).all()
        stale = await stale_mx_domains(db, addresses, now=now, config=ctx.config.contacts)
    if stale:
        tally.mx_refreshes += await refresh_mx(
            ctx.db_factory, ctx.mx_checker, stale[:room], now=now, config=ctx.config.contacts
        )


async def _draft_company(
    ctx: _Drafting, plan: _Plan, company_id: uuid.UUID, tally: _Tally
) -> _Result:
    now = ctx.now()  # fresh per company: the budget's UTC day
    context_error: str | None = None
    # 0. Stale mail-server checks are looked up again first (no transaction open meanwhile).
    await _refresh_stale_mx(ctx, company_id, tally, now)
    # 1. Read what the writer needs, then close the session: no transaction while Claude runs.
    async with ctx.db_factory() as db:
        campaign = await db.get(Campaign, plan.campaign_id)
        company = await db.get(Company, company_id)
        if campaign is None or campaign.status != "active":
            return _Result.CLOSED
        if company is None or company.side != plan.side:
            return _Result.SKIPPED
        picked = await select_recipient(db, company, now=now, config=ctx.config)
        if picked is None:
            return _Result.SKIPPED
        person, address = picked
        label = company.domain
        try:
            inp = await build_context(
                db,
                campaign=campaign,
                company=company,
                person=person,
                catalog=ctx.catalog,
                facts=ctx.facts,
                config=ctx.config,
            )
        except ValueError as exc:
            context_error = str(exc)
        person_id, address_id, recipient = person.id, address.id, address.address
    if context_error is not None:
        return await _company_failed(ctx, plan, tally, company_id, label, context_error, now)

    # 2. Claude writes (its own short sessions record usage and check the budget).
    tally.attempts += 1
    try:
        written = await write_sequence(
            ctx.runtime,
            ctx.db_factory,
            inp,
            now=now,
            config=ctx.config,
            campaign_id=plan.campaign_id,
        )
    except (BrainUnavailable, BudgetReached) as exc:
        return await _stop(ctx, plan, tally, exc.reason, everywhere=True)
    except BrainError as exc:
        if exc.reason == SIGNED_OUT:  # not signed in / over quota: every call would fail
            return await _stop(ctx, plan, tally, exc.reason, everywhere=True)
        await _company_failed(ctx, plan, tally, company_id, label, exc.reason, now)
        tally.claude_failures += 1
        if tally.claude_failures >= MAX_CLAUDE_FAILURES_IN_A_ROW:
            return await _stop(ctx, plan, tally, KEPT_FAILING, everywhere=False)
        return _Result.ERROR
    tally.claude_failures = 0
    try:
        first = assemble(
            written.first_subject,
            written.first_body,
            facts=ctx.facts,
            writing=ctx.config.writing,
            side=plan.side,
        )
        second = assemble(
            written.followup_subject,
            written.followup_body,
            facts=ctx.facts,
            writing=ctx.config.writing,
            side=plan.side,
        )
    except ValueError as exc:
        return await _company_failed(ctx, plan, tally, company_id, label, str(exc), now)

    # 3. Gate and store in one short transaction.
    return await _store(
        ctx,
        plan,
        _Written(company_id, person_id, address_id, recipient, prompt_for(inp), first, second),
    )


async def _store(ctx: _Drafting, plan: _Plan, w: _Written) -> _Result:
    now = ctx.now()
    async with ctx.db_factory() as db, db.begin():
        # The campaign row lock serializes drafters of one campaign: status, the "one sequence
        # per company" rule and the daily cap are re-checked under it.
        campaign = await db.get(
            Campaign, plan.campaign_id, with_for_update=_CAMPAIGN_LOCK, populate_existing=True
        )
        if campaign is None or campaign.status != "active":
            return _Result.CLOSED
        taken = await db.scalar(
            select(Sequence.id).where(
                Sequence.campaign_id == plan.campaign_id, Sequence.company_id == w.company_id
            )
        )
        if taken is not None:
            return _Result.SKIPPED
        if await drafted_today(db, plan.campaign_id, now=now) >= campaign.daily_draft_cap:
            return _Result.CLOSED
        # One lock per address across campaigns: whoever stores a draft to it first wins, and
        # the other draft is dropped (not stored as held).
        await lock_recipient(db, w.recipient)
        busy = await db.scalar(
            select(Sequence.id)
            .where(Sequence.recipient == w.recipient, Sequence.status.in_(IN_PROGRESS))
            .limit(1)
        )
        if busy is not None:
            return _Result.SKIPPED
        company = await db.get(Company, w.company_id)
        if company is None:
            return _Result.SKIPPED
        address = await db.get(EmailAddress, w.address_id)
        person = await db.get(Person, w.person_id)
        # Before the sequence exists, so the check counts every other in-progress sequence.
        problems = await gate_message(
            db,
            subject=w.first[0],
            body=w.first[1],
            step=1,
            side=plan.side,
            facts=ctx.facts,
            prompt_text=w.prompt,
            sequence_or_none=None,
            recipient=w.recipient,
            company=company,
            address=address,
            now=now,
            config=ctx.config,
        )
        sequence = Sequence(
            campaign_id=plan.campaign_id,
            company_id=w.company_id,
            person_id=person.id if person is not None else None,
            email_address_id=address.id if address is not None else None,
            recipient=w.recipient,
            status="awaiting_approval",
            created_at=now,
            updated_at=now,
        )
        try:
            async with db.begin_nested():
                db.add(sequence)
                await db.flush()
                db.add_all(
                    [
                        Message(
                            sequence_id=sequence.id,
                            step=1,
                            subject=w.first[0],
                            body=w.first[1],
                            status="gate_failed" if problems else "awaiting_approval",
                            gate_errors=problems,
                        ),
                        # Gated when it falls due (Phase 6); no due date until step 1 is sent.
                        Message(
                            sequence_id=sequence.id,
                            step=2,
                            subject=w.second[0],
                            body=w.second[1],
                            status="pending",
                            gate_errors=null(),
                        ),
                    ]
                )
                await db.flush()
        except IntegrityError:
            return _Result.SKIPPED  # another drafter stored this company first
        await db.execute(
            delete(DraftFailure).where(
                DraftFailure.campaign_id == plan.campaign_id,
                DraftFailure.company_id == w.company_id,
            )
        )
        campaign.last_error = None  # the campaign is drafting again
        return _Result.HELD if problems else _Result.DRAFTED


async def _draft_campaign(
    ctx: _Drafting, campaign_id: uuid.UUID, *, limit: int, tally: _Tally
) -> None:
    plan = await _plan(ctx, campaign_id, tally)
    if plan is None:
        return
    drafted_here = 0
    for company_id in plan.candidates:
        if drafted_here >= plan.room or tally.attempts >= limit:
            return
        if (ctx.deadline is not None and ctx.now() >= ctx.deadline) or (
            ctx.stop is not None and ctx.stop.is_set()
        ):
            tally.out_of_time = True
            return
        result = await _draft_company(ctx, plan, company_id, tally)
        match result:
            case _Result.DRAFTED | _Result.HELD:
                drafted_here += 1
                tally.drafted += 1
                if result is _Result.HELD:
                    tally.gate_failed += 1
            case _Result.SKIPPED:
                tally.skipped += 1
            case _Result.ERROR:
                pass
            case _Result.STOP:
                return
            case _Result.CLOSED:
                tally.closed += 1
                return


async def draft_batch(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    runtime: ClaudeRuntime,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    now: Callable[[], datetime],
    limit: int,
    campaign_id: uuid.UUID | None = None,
    mx_checker: MxChecker | None = None,
    deadline: datetime | None = None,
    stop: asyncio.Event | None = None,
) -> DraftOutcome:
    """One drafting pass over the active campaigns (oldest activated first; only
    ``campaign_id`` if given). ``now`` is read afresh for every company.

    With ``mx_checker``, a candidate's stale mail-server check is looked up again (outside any
    transaction) before its recipient is chosen, at most ``2 * limit`` lookups per pass;
    without one, or when the lookup fails, that address is passed over as the gate would hold
    it.

    At most ``limit`` companies are sent to Claude in all (drafted, held by the gate or
    failed), and each campaign stores at most its ``daily_draft_cap`` sequences per UTC day.
    With today's budget already spent the pass does nothing at all. Today's budget running
    out, the Claude CLI being unusable or signed out stops the pass and every active
    campaign's ``last_error`` says why; three Claude failures in a row stop it too
    (``KEPT_FAILING``, on that campaign). Any other failure for one company is listed in
    ``errors`` and the company is backed off (``FAILURE_BACKOFF``; the rest of the UTC day
    after ``MAX_FAILURES_PER_DAY``).

    With ``deadline``, no company is started once ``now() >= deadline`` (a company already
    being written finishes): the worker's other steps, sending above all, never wait long.
    Likewise once ``stop`` is set (the worker is shutting down; Ruling P7-G): the company being
    written finishes, no new one starts, so a planned stop never waits on more than that."""
    tally = _Tally()
    ctx = _Drafting(
        db_factory,
        runtime,
        catalog,
        facts,
        config,
        now,
        mx_checker=mx_checker,
        max_mx_refreshes=2 * limit,
        deadline=deadline,
        stop=stop,
    )
    async with db_factory() as db:
        if await remaining_calls(db, now=now(), config=config) <= 0:
            return tally.freeze()
        query = (
            select(Campaign.id)
            .where(Campaign.status == "active")
            .order_by(Campaign.activated_at.asc().nulls_last(), Campaign.created_at, Campaign.id)
        )
        if campaign_id is not None:
            query = query.where(Campaign.id == campaign_id)
        ids = list((await db.scalars(query)).all())
    for cid in ids:
        if tally.attempts >= limit or tally.stopped_reason is not None or tally.out_of_time:
            break
        await _draft_campaign(ctx, cid, limit=limit, tally=tally)
    return tally.freeze()
