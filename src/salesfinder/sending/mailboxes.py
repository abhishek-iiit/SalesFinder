"""Mailboxes: register Google Workspace mailboxes, check their domain's DNS, test the connection.

The app password is never stored: the row keeps a ``secret_name`` that keys into
``SALESFINDER_MAILBOX_PASSWORDS`` (settings). Passwords never reach the DB, logs, pages or the
audit trail, and server text from a connection test is never shown (fixed copy only).

Services flush and callers commit; ``refresh_readiness`` owns its own short sessions so DNS never
runs inside a transaction.
"""

import re
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.catalog import Facts
from salesfinder.config import AppConfig, ContactsConfig
from salesfinder.contacts.dns import MxChecker, Resolve
from salesfinder.db.models import Campaign, Mailbox, Message, User
from salesfinder.domains import normalize_domain
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.readiness import Report, check_domain
from salesfinder.mail.smtp import MailboxCredentials
from salesfinder.sending import policy
from salesfinder.settings import Settings

log = structlog.get_logger(__name__)

ELCHEMY_REFUSED = "Cold email is never sent from elchemy.com — use the separate sending domain."
DUPLICATE = "That mailbox already exists."
SECRET_TAKEN = "That password name is already used by another mailbox."  # noqa: S105
WEBSITE_UNSET = "Set website in facts.yaml first."
SETTINGS_CHANGED = (
    "Connection settings changed — test it, then enable it again. "
    "Replies and opt-outs aren't read until it passes."
)
NOT_TESTED = (
    "Not tested yet — test the connection, then enable it. "
    "Replies and opt-outs aren't read until it passes."
)
# Holds that mean "no login with its password until Test connection passes" (P6-V, P6-W).
UNTESTED_HOLDS = (SETTINGS_CHANGED, NOT_TESTED)
TEST_FIRST = "Test the connection first: its settings haven't passed a test yet."
CONNECTION_FIELDS = (
    "smtp_host", "smtp_port", "imap_host", "imap_port", "username", "secret_name",
)  # fmt: skip
GONE = "That mailbox no longer exists."
BAD_ADDRESS = "Enter a valid mailbox address."
BAD_SIDE = "Side must be buyer or supplier."
BAD_SECRET_NAME = "The password name must be 1 to 32 lowercase letters, digits or underscores."  # noqa: S105
MAX_DAILY_MAX = 50  # the table check
ADDRESS_FIXED = "A mailbox's address can't be changed. Add a new mailbox instead."
SIDE_IN_USE = "Campaigns already use this mailbox, so its side can't change."
SMTP_FAILED = "SMTP: {reason}"
IMAP_FAILED = "IMAP: couldn't sign in or open the mailbox. Check IMAP is on and the app password."
NO_SENT_FOLDER = "IMAP: couldn't find the Sent folder. Set its name on the mailbox."
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SECRET_NAME = re.compile(r"^[a-z0-9_]{1,32}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,255}$")


def no_password(secret_name: str) -> str:
    return f'Set SALESFINDER_MAILBOX_PASSWORDS for "{secret_name}" in .env and restart.'


class MailboxForm(BaseModel):
    """What an admin enters. Validate with ``context={"max_daily": config.sending.max_daily_cap}``
    to apply the configured ceiling (the database allows up to 50)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    address: str
    side: Literal["buyer", "supplier"]
    username: str = Field(min_length=1, max_length=320)
    secret_name: str
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    imap_host: str = "imap.gmail.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    sent_folder: str | None = Field(default=None, max_length=200)
    dkim_selector: str = Field(default="google", min_length=1, max_length=63)
    daily_max: int = Field(default=30, ge=1)
    warmup_started_on: date | None = None

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).lower()
        if not _ADDRESS.match(value) or len(value) > 320:
            raise ValueError(BAD_ADDRESS)
        return value

    @field_validator("secret_name")
    @classmethod
    def _secret(cls, value: str) -> str:
        if not _SECRET_NAME.match(value):
            raise ValueError(BAD_SECRET_NAME)
        return value

    @field_validator("smtp_host", "imap_host")
    @classmethod
    def _host(cls, value: str) -> str:
        if not _HOST.match(value):
            raise ValueError("Enter a valid server name.")
        return value.lower()

    @field_validator("sent_folder")
    @classmethod
    def _folder(cls, value: str | None) -> str | None:
        if not value:
            return None
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("The Sent folder name has invalid characters.")
        return value

    @field_validator("daily_max")
    @classmethod
    def _cap(cls, value: int, info: ValidationInfo) -> int:
        ceiling = (info.context or {}).get("max_daily", MAX_DAILY_MAX)
        if isinstance(ceiling, int) and value > ceiling:
            raise ValueError(f"The daily maximum can be at most {ceiling}.")
        return value


def _apply(mailbox: Mailbox, form: MailboxForm) -> None:
    mailbox.username = form.username
    mailbox.secret_name = form.secret_name
    mailbox.smtp_host = form.smtp_host
    mailbox.smtp_port = form.smtp_port
    mailbox.smtp_security = form.smtp_security
    mailbox.imap_host = form.imap_host
    mailbox.imap_port = form.imap_port
    mailbox.sent_folder = form.sent_folder
    mailbox.dkim_selector = form.dkim_selector
    mailbox.daily_max = form.daily_max
    mailbox.warmup_started_on = form.warmup_started_on


def _check_not_elchemy(address: str, facts: Facts) -> None:
    """Fail closed: refuse when our own website's domain can't be read, when the address's
    domain can't be normalised, or when they are the same registrable domain (compared after
    NFKC, so a fullwidth look-alike of elchemy.com is refused too)."""
    ours = normalize_domain(unicodedata.normalize("NFKC", facts.website))
    if ours is None:
        raise ValueError(WEBSITE_UNSET)
    theirs = normalize_domain(unicodedata.normalize("NFKC", address.rpartition("@")[2]))
    if theirs is None:
        raise ValueError(BAD_ADDRESS)
    if ours == theirs:
        raise ValueError(ELCHEMY_REFUSED)


def connection_changes(mailbox: Mailbox, form: MailboxForm) -> list[str]:
    """Names (never values) of the connection fields ``form`` would change."""
    return [f for f in CONNECTION_FIELDS if getattr(mailbox, f) != getattr(form, f)]


async def _secret_taken(
    db: AsyncSession, secret_name: str, *, except_id: uuid.UUID | None = None
) -> bool:
    stmt = select(Mailbox.id).where(Mailbox.secret_name == secret_name)
    if except_id is not None:
        stmt = stmt.where(Mailbox.id != except_id)
    return await db.scalar(stmt) is not None


async def get_mailbox(db: AsyncSession, mailbox_id: uuid.UUID, *, lock: bool = False) -> Mailbox:
    stmt = select(Mailbox).where(Mailbox.id == mailbox_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    mailbox = await db.scalar(stmt)
    if mailbox is None:
        raise ValueError(GONE)
    return mailbox


async def create_mailbox(
    db: AsyncSession, form: MailboxForm, *, actor: User, facts: Facts
) -> Mailbox:
    """A new mailbox, disabled. Refuses an address on Elchemy's own domain and duplicates. A side
    may have several mailboxes (and several enabled ones)."""
    _check_not_elchemy(form.address, facts)
    if await db.scalar(select(Mailbox.id).where(func.lower(Mailbox.address) == form.address)):
        raise ValueError(DUPLICATE)
    if await _secret_taken(db, form.secret_name):
        raise ValueError(SECRET_TAKEN)
    # Ruling P6-W: no login of any kind (not even to read mail) until its first passing test.
    mailbox = Mailbox(
        address=form.address,
        side=form.side,
        enabled=False,
        hold_reason=NOT_TESTED,
        created_by_id=actor.id,
    )
    _apply(mailbox, form)
    try:
        async with db.begin_nested():
            db.add(mailbox)
            await db.flush()
    except IntegrityError:
        raise ValueError(DUPLICATE) from None
    return mailbox


async def update_mailbox(
    db: AsyncSession, mailbox_id: uuid.UUID, form: MailboxForm, *, actor: User, facts: Facts
) -> Mailbox:
    mailbox = await get_mailbox(db, mailbox_id, lock=True)
    if form.address != mailbox.address:
        raise ValueError(ADDRESS_FIXED)
    if form.side != mailbox.side:
        in_use = await db.scalar(select(Campaign.id).where(Campaign.mailbox_id == mailbox.id))
        if in_use is not None:
            raise ValueError(SIDE_IN_USE)
        mailbox.side = form.side
    if await _secret_taken(db, form.secret_name, except_id=mailbox.id):
        raise ValueError(SECRET_TAKEN)
    if connection_changes(mailbox, form):
        # Ruling P6-V: never log in to (send the password to) the new settings, not even to
        # read mail (P6-J), until a successful Test connection (``clear_settings_hold``).
        mailbox.enabled = False
        mailbox.hold_reason = SETTINGS_CHANGED
    _apply(mailbox, form)
    await db.flush()
    return mailbox


def untested(mailbox: Mailbox) -> bool:
    """Its connection settings haven't passed a Test connection: never tested since it was
    created (Ruling P6-W) or since they changed (Ruling P6-V). No login of any kind (polling,
    reconcile) and no enabling until they do."""
    return mailbox.hold_reason in UNTESTED_HOLDS


def connection_snapshot(mailbox: Mailbox) -> tuple[object, ...]:
    """The connection settings a Test connection used (``clear_settings_hold`` compares)."""
    return tuple(getattr(mailbox, f) for f in CONNECTION_FIELDS)


async def clear_settings_hold(
    db: AsyncSession, mailbox_id: uuid.UUID, tested: tuple[object, ...]
) -> bool:
    """After a successful Test connection of the settings ``tested``
    (``connection_snapshot``): clear the untested hold (``NOT_TESTED`` or ``SETTINGS_CHANGED``),
    unless the settings changed again meanwhile. True if it was cleared (the caller audits it
    with the test). Flushes."""
    mailbox = await get_mailbox(db, mailbox_id, lock=True)
    if not untested(mailbox) or connection_snapshot(mailbox) != tested:
        return False
    mailbox.hold_reason = None
    await db.flush()
    return True


async def set_enabled(
    db: AsyncSession,
    mailbox_id: uuid.UUID,
    enabled: bool,
    *,
    actor: User,
    settings: Settings,
    facts: Facts,
) -> Mailbox:
    mailbox = await get_mailbox(db, mailbox_id, lock=True)
    if enabled:
        _check_not_elchemy(mailbox.address, facts)
    if enabled and mailbox.secret_name not in settings.mailbox_passwords:
        raise ValueError(no_password(mailbox.secret_name))
    if enabled and untested(mailbox):
        raise ValueError(TEST_FIRST)
    mailbox.enabled = enabled
    await db.flush()
    return mailbox


def credentials(
    mailbox: Mailbox, settings: Settings, config: AppConfig
) -> MailboxCredentials | None:
    """The login for ``mailbox``, or None while its password isn't set in the environment."""
    password = settings.mailbox_passwords.get(mailbox.secret_name)
    if password is None:
        return None
    return MailboxCredentials(
        address=mailbox.address,
        username=mailbox.username,
        password=password,
        smtp_host=mailbox.smtp_host,
        smtp_port=mailbox.smtp_port,
        smtp_security="starttls" if mailbox.smtp_security == "starttls" else "ssl",
        imap_host=mailbox.imap_host,
        imap_port=mailbox.imap_port,
        sent_folder=mailbox.sent_folder,
        timeout=config.sending.smtp_timeout_seconds,
    )


async def not_read(db: AsyncSession, settings: Settings, config: AppConfig) -> list[str]:
    """Addresses (sorted) of the mailboxes whose password is set but which are held untested
    (``untested``): the worker's mail step skips exactly these (Rulings P6-V/W), so replies and
    opt-outs sent to them wait unread. A mailbox without a password isn't listed: it's never
    read, and its missing password is reported on its own. Read only."""
    boxes = await db.scalars(
        select(Mailbox).where(Mailbox.hold_reason.in_(UNTESTED_HOLDS)).order_by(Mailbox.address)
    )
    return [
        box.address
        for box in boxes
        if untested(box) and credentials(box, settings, config) is not None
    ]


def is_google_host(mailbox: Mailbox) -> bool:
    host = mailbox.smtp_host.lower()
    return any(
        host == d or host.endswith("." + d) for d in ("gmail.com", "googlemail.com", "google.com")
    )


async def refresh_readiness(
    db_factory: async_sessionmaker[AsyncSession],
    mailbox_id: uuid.UUID,
    *,
    resolve: Resolve,
    now: datetime,
    contacts: ContactsConfig | None = None,
) -> Report:
    """Check the mailbox's domain and store the outcome (``dns_ok``, ``dns_checked_at``,
    ``dns_report``). DNS runs between two short sessions, never inside a transaction."""
    async with db_factory() as db:
        mailbox = await get_mailbox(db, mailbox_id)
        domain = mailbox.address.rpartition("@")[2]
        selector = mailbox.dkim_selector
        google = is_google_host(mailbox)
        await db.rollback()
    report = await check_domain(
        domain,
        dkim_selector=selector,
        resolve=resolve,
        mx=MxChecker(contacts or ContactsConfig(), resolve=resolve),
        google=google,
    )
    async with db_factory() as db:
        row = await get_mailbox(db, mailbox_id, lock=True)
        row.dns_ok = report.ok
        row.dns_checked_at = now
        row.dns_report = report.to_json()
        await db.commit()
    return report


def readiness_is_fresh(mailbox: Mailbox, *, now: datetime, config: AppConfig) -> bool:
    return (
        mailbox.dns_ok
        and mailbox.dns_checked_at is not None
        and now - mailbox.dns_checked_at <= timedelta(hours=config.sending.dns_max_age_hours)
    )


async def test_connection(
    mailbox: Mailbox, creds: MailboxCredentials, mail: MailFactory
) -> list[str]:
    """SMTP login + NOOP + QUIT, then IMAP login and the Sent folder. Returns the problems
    (empty when all is well), in fixed wording: server text is never shown or logged."""
    problems: list[str] = []
    try:
        reason = await mail.sender.check(creds)
    except Exception as exc:
        log.warning("mailbox_smtp_test_failed", error_type=type(exc).__name__)
        reason = "couldn't connect."
    if reason is not None:
        problems.append(SMTP_FAILED.format(reason=reason))
    try:
        reader = await mail.open_reader(creds)
    except Exception as exc:
        log.warning("mailbox_imap_test_failed", error_type=type(exc).__name__)
        problems.append(IMAP_FAILED)
        return problems
    try:
        if creds.sent_folder is None and await reader.special_folder("sent") is None:
            problems.append(NO_SENT_FOLDER)
    except Exception as exc:
        log.warning("mailbox_imap_test_failed", error_type=type(exc).__name__)
        problems.append(IMAP_FAILED)
    finally:
        try:
            await reader.aclose()
        except Exception as exc:
            log.warning("mailbox_imap_close_failed", error_type=type(exc).__name__)
    return problems


# --------------------------------------------------------------------------- page figures


def ramp_cap_today(mailbox: Mailbox, *, today: date, config: AppConfig) -> int:
    """Today's cold-send cap for the Mailboxes page: ``policy.ramp_cap``, the one rule."""
    return policy.ramp_cap(mailbox, today=today, config=config)


async def sent_today_counts(db: AsyncSession, *, now: datetime) -> dict[uuid.UUID, int]:
    """``policy.sent_today`` for every mailbox at once (the same filter), per mailbox."""
    rows = await db.execute(
        select(Message.mailbox_id, func.count())
        .where(Message.mailbox_id.is_not(None), *policy.cold_sent_today_filter(now))
        .group_by(Message.mailbox_id)
    )
    return {mailbox_id: count for mailbox_id, count in rows.all() if mailbox_id is not None}


def audit_details(mailbox: Mailbox, **extra: Any) -> dict[str, Any]:
    """What the audit trail records about a mailbox: names and settings, never a password."""
    return {
        "address": mailbox.address,
        "side": mailbox.side,
        "secret_name": mailbox.secret_name,
        "enabled": mailbox.enabled,
        **extra,
    }
