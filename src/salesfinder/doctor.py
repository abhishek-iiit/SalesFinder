"""Environment self-checks behind ``salesfinder doctor``."""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from salesfinder.brain.runtime import BrainError, cli_missing_flags
from salesfinder.catalog import load_catalog, load_facts
from salesfinder.config import ConfigError, load_config
from salesfinder.contacts import dns as dns_module
from salesfinder.contacts.dns import MxChecker, Resolve
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.db.migrate import current_and_head
from salesfinder.db.models import Mailbox, User
from salesfinder.exclusions.sync import sync_health
from salesfinder.mail import factory as mail_factory
from salesfinder.mail.factory import MailFactory
from salesfinder.mail.readiness import check_domain
from salesfinder.ops import health, network
from salesfinder.ops.version import code_version
from salesfinder.sending import mailboxes as mailbox_service
from salesfinder.sending import state as sending_state
from salesfinder.settings import Settings


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _check_config(settings: Settings) -> CheckResult:
    try:
        load_config(settings.config_dir)
        load_catalog(settings.config_dir)
        load_facts(settings.config_dir)
    except ConfigError as exc:
        return CheckResult("config", False, str(exc))
    return CheckResult("config", True, f"loaded from {settings.config_dir}")


async def _check_database(settings: Settings) -> CheckResult:
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return CheckResult("database", False, f"{type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()
    return CheckResult("database", True, "connected")


async def _check_migrations(settings: Settings) -> CheckResult:
    try:
        current, head = await current_and_head(settings.database_url)
    except Exception as exc:
        return CheckResult("migrations", False, f"{type(exc).__name__}: {exc}")
    if current == head:
        return CheckResult("migrations", True, f"at head ({head})")
    return CheckResult(
        "migrations",
        False,
        f"current {current or 'none'}, head {head}; run: salesfinder db upgrade",
    )


async def _check_admin(settings: Settings) -> CheckResult:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == "admin", User.is_active.is_(True))
            )
    except Exception as exc:
        return CheckResult("admin_user", False, f"{type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()
    if count:
        return CheckResult("admin_user", True, f"{count} active admin(s)")
    return CheckResult("admin_user", False, "no active admin; run: salesfinder users create-admin")


async def _check_claude(settings: Settings) -> CheckResult:
    # The runtime's own preflight (same isolated --help run), so doctor and the brain agree.
    try:
        missing = await cli_missing_flags(settings.claude_bin)
    except BrainError as exc:
        return CheckResult("claude_cli", False, exc.reason)
    if missing:
        return CheckResult("claude_cli", False, "missing flags: " + ", ".join(missing))
    return CheckResult("claude_cli", True, "all required flags present")


def _check_https(settings: Settings) -> CheckResult:
    if not settings.lan_mode and settings.tls_certfile is None and settings.tls_keyfile is None:
        return CheckResult("https", True, "loopback only")
    if (settings.tls_certfile is None) != (settings.tls_keyfile is None):
        return CheckResult(
            "https",
            False,
            "set both SALESFINDER_TLS_CERTFILE and SALESFINDER_TLS_KEYFILE, or neither",
        )
    problems = []
    for label, path in (("cert", settings.tls_certfile), ("key", settings.tls_keyfile)):
        if path is None or not path.is_file() or not os.access(path, os.R_OK):
            problems.append(f"TLS {label} file missing or unreadable: {path}")
    if problems:
        return CheckResult("https", False, "; ".join(problems))
    return CheckResult("https", True, "TLS cert and key readable")


def _check_network(settings: Settings) -> CheckResult:
    """The same LAN warnings the Settings page shows; the mode when there are none."""
    warnings = network.lan_warnings(settings)
    if warnings:
        return CheckResult("network", False, " ".join(warnings))
    return CheckResult("network", True, network.mode_line(settings))


async def _check_hubspot(settings: Settings) -> CheckResult:
    if settings.hubspot_token is None:
        return CheckResult(
            "hubspot",
            False,
            "SALESFINDER_HUBSPOT_TOKEN is not set — cold sending will stay paused.",
        )
    config = load_config(settings.config_dir)
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            health = await sync_health(
                session, now=datetime.now(UTC), token_configured=True, config=config.hubspot
            )
    finally:
        await engine.dispose()
    return CheckResult("hubspot", health.state == "fresh", health.message)


def _check_serper(settings: Settings) -> CheckResult:
    if settings.serper_api_key is None:
        return CheckResult(
            "serper", False, "SALESFINDER_SERPER_API_KEY is not set — discovery jobs can't run."
        )
    return CheckResult("serper", True, "key configured")


async def _check_mailboxes(
    settings: Settings, mail: MailFactory | None, dns_resolve: Resolve | None
) -> CheckResult:
    """Each enabled mailbox: its password is set, its domain's DNS is ready (a live lookup,
    nothing stored) and a connection test passes. Every mailbox (enabled or not) whose password
    is set but which is held untested is a problem too: its mail isn't read (P6-V/W), so
    opt-outs may be waiting; it is never logged in to here. The real mail transport and
    resolver are built only when there is a mailbox to check."""
    config = load_config(settings.config_dir)
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            boxes = list(
                await session.scalars(
                    select(Mailbox).where(Mailbox.enabled.is_(True)).order_by(Mailbox.address)
                )
            )
            held = await mailbox_service.not_read(session, settings, config)
    finally:
        await engine.dispose()
    held_problems = [
        f"{address}: not read until Test connection passes — opt-outs may be waiting."
        for address in held
    ]
    # An enabled mailbox is never untested (enabling needs a passing test, and changed settings
    # disable it); never send its password to an untested host all the same.
    boxes = [box for box in boxes if not mailbox_service.untested(box)]
    if not boxes:
        return CheckResult(
            "mailboxes",
            False,
            " ".join(["No mailboxes configured — nothing can send.", *held_problems]),
        )
    transport = mail if mail is not None else mail_factory.default_mail()
    resolve = (
        dns_resolve if dns_resolve is not None else dns_module.default_resolve(config.contacts)
    )
    mx = MxChecker(config.contacts, resolve=resolve)
    problems: list[str] = list(held_problems)
    for box in boxes:
        creds = mailbox_service.credentials(box, settings, config)
        if creds is None:
            problems.append(f"{box.address}: {mailbox_service.no_password(box.secret_name)}")
            continue
        report = await check_domain(
            box.address.rpartition("@")[2],
            dkim_selector=box.dkim_selector,
            resolve=resolve,
            mx=mx,
            google=mailbox_service.is_google_host(box),
        )
        problems += [f"{box.address}: {c.detail}" for c in report.checks if not c.ok]
        problems += [
            f"{box.address}: {p}"
            for p in await mailbox_service.test_connection(box, creds, transport)
        ]
    if problems:
        return CheckResult("mailboxes", False, " ".join(problems))
    return CheckResult("mailboxes", True, f"{len(boxes)} mailbox(es) ready")


async def _check_sending(settings: Settings) -> CheckResult:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            state = await sending_state.get_state(session)
    finally:
        await engine.dispose()
    if state.paused:
        return CheckResult("sending", True, f"Sending is paused: {state.reason}")
    return CheckResult("sending", True, "Sending is on.")


async def _check_worker(settings: Settings) -> CheckResult:
    """Ok before the worker's first run (setup runs doctor before starting it) and while it
    runs on this code; a problem when it stopped, stopped responding, runs other code or runs
    with ``--no-send``."""
    config = load_config(settings.config_dir)
    engine = make_engine(settings.database_url)
    now = datetime.now(UTC)
    try:
        async with make_session_factory(engine)() as session:
            result = await health.worker_health(
                session, now=now, config=config, code_version=code_version()
            )
    finally:
        await engine.dispose()
    if result.state == "never":
        return CheckResult("worker", True, "hasn't run yet")
    if result.state == "running" and result.no_send:
        # A warning: a leftover dry-run worker sends nothing (Ruling P7-J).
        return CheckResult(
            "worker",
            False,
            f"{health.NO_SEND}, {health.last_pass_phrase(result, now=now)}. Stop it and start "
            "the worker without --no-send (see Operations).",
        )
    if result.state == "running":
        return CheckResult("worker", True, f"running, {health.last_pass_phrase(result, now=now)}")
    return CheckResult("worker", False, result.problem or result.state)


async def _isolated(name: str, check: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
    """One failing check must never prevent the others from running."""
    try:
        return await check()
    except Exception as exc:
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


async def _async(result: CheckResult) -> CheckResult:
    return result


async def run_checks(
    settings: Settings,
    *,
    mail: MailFactory | None = None,
    dns_resolve: Resolve | None = None,
) -> list[CheckResult]:
    return [
        await _isolated("config", lambda: _async(_check_config(settings))),
        await _isolated("database", lambda: _check_database(settings)),
        await _isolated("migrations", lambda: _check_migrations(settings)),
        await _isolated("admin_user", lambda: _check_admin(settings)),
        await _isolated("claude_cli", lambda: _check_claude(settings)),
        await _isolated("https", lambda: _async(_check_https(settings))),
        await _isolated("network", lambda: _async(_check_network(settings))),
        await _isolated("hubspot", lambda: _check_hubspot(settings)),
        await _isolated("serper", lambda: _async(_check_serper(settings))),
        await _isolated("mailboxes", lambda: _check_mailboxes(settings, mail, dns_resolve)),
        await _isolated("sending", lambda: _check_sending(settings)),
        await _isolated("worker", lambda: _check_worker(settings)),
    ]
