"""``salesfinder`` command line."""

import asyncio
import contextlib
import os
import shlex
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder import audit
from salesfinder.auth.passwords import password_problems
from salesfinder.catalog import Catalog, Facts, load_catalog, load_facts
from salesfinder.config import AppConfig, ConfigError, load_config
from salesfinder.db.engine import make_engine, make_session_factory
from salesfinder.db.migrate import upgrade_to_head
from salesfinder.db.models import Company, DiscoveryJob, HubSpotSync
from salesfinder.doctor import run_checks
from salesfinder.logging import configure_logging
from salesfinder.sending.state import SendingState
from salesfinder.settings import Settings, get_settings

# Locals (e.g. a plaintext password) must never be dumped into a traceback.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
users_app = typer.Typer(no_args_is_help=True, help="User commands.")
app.add_typer(db_app, name="db")
hubspot_app = typer.Typer(no_args_is_help=True, help="HubSpot commands.")
discover_app = typer.Typer(no_args_is_help=True, help="Discovery commands.")
app.add_typer(users_app, name="users")
app.add_typer(hubspot_app, name="hubspot")
app.add_typer(discover_app, name="discover")
sending_app = typer.Typer(no_args_is_help=True, help="Sending controls.")
app.add_typer(sending_app, name="sending")
ops_app = typer.Typer(no_args_is_help=True, help="Operations on the office Mac.")
app.add_typer(ops_app, name="ops")


def _settings() -> Settings:
    try:
        settings = get_settings()
    except ValidationError as exc:
        for error in exc.errors():
            message = str(error["msg"]).removeprefix("Value error, ")
            typer.echo(f"Configuration error: {message}", err=True)
        raise typer.Exit(2) from exc
    try:
        configure_logging(settings.log_level, settings.log_json, settings.log_file)
    except OSError as exc:
        typer.echo(
            f"Configuration error: can't write the log file {settings.log_file} "
            f"({type(exc).__name__}).",
            err=True,
        )
        raise typer.Exit(2) from exc
    return settings


def _app_config_or_exit(settings: Settings) -> tuple[AppConfig, Catalog, Facts]:
    """Load salesfinder.yaml, catalog.yaml and facts.yaml; a problem is one line, exit 2."""
    try:
        return (
            load_config(settings.config_dir),
            load_catalog(settings.config_dir),
            load_facts(settings.config_dir),
        )
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _run_db[T](coro: Awaitable[T]) -> T:
    """Run a database coroutine; connection problems become one clean line, exit 1."""

    async def _await() -> T:
        return await coro

    try:
        return asyncio.run(_await())
    except (SQLAlchemyError, OSError) as exc:
        reason = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
        typer.echo(
            f"Database error: {type(exc).__name__}: {reason}. Run `salesfinder doctor`.", err=True
        )
        raise typer.Exit(1) from exc


async def _in_session(settings: Settings, work: Callable[[AsyncSession], Awaitable[str]]) -> str:
    engine = make_engine(settings.database_url)
    try:
        async with make_session_factory(engine)() as session:
            message = await work(session)
            await session.commit()
            return message
    finally:
        await engine.dispose()


def _prompt_password() -> str:
    password: str = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    problems = password_problems(password)
    if problems:
        typer.echo(" ".join(problems), err=True)
        raise typer.Exit(1)
    return password


@app.command()
def web(
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload (dev).")] = False,
) -> None:
    """Run the web server."""
    settings = _settings()
    options: dict[str, object] = {
        "host": settings.bind_host,
        "port": settings.port,
        # The access log would contain raw /invite/<token> URLs.
        "access_log": False,
        "log_config": None,
    }
    if settings.https:
        options["ssl_certfile"] = str(settings.tls_certfile)
        options["ssl_keyfile"] = str(settings.tls_keyfile)
    # Validate the YAML config here, before uvicorn starts: a broken file is one clean line, and
    # with --reload the app is only built later, inside uvicorn's child process.
    _app_config_or_exit(settings)
    if reload:
        uvicorn.run("salesfinder.web.app:create_app_from_env", factory=True, reload=True, **options)  # type: ignore[arg-type]
    else:
        from salesfinder.web.app import create_app

        try:
            web_app = create_app(settings)
        except ConfigError as exc:  # pragma: no cover - the files changed since the check above
            typer.echo(f"Configuration error: {exc}", err=True)
            raise typer.Exit(2) from exc
        uvicorn.run(web_app, **options)  # type: ignore[arg-type]


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply all database migrations."""
    settings = _settings()
    _run_db(upgrade_to_head(settings.database_url))
    typer.echo("Database is up to date.")


@users_app.command("create-admin")
def create_admin(
    email: Annotated[str, typer.Option("--email")],
    name: Annotated[str, typer.Option("--name")],
) -> None:
    """Create an admin user (password is prompted, hidden)."""
    settings = _settings()
    password = _prompt_password()

    async def _work(session: AsyncSession) -> str:
        from salesfinder.auth import users

        user = await users.create_admin(session, email=email, name=name, password=password)
        await audit.record(
            session,
            action=audit.USER_ADMIN_CREATED,
            actor_id=None,
            target_type="user",
            target_id=str(user.id),
            details={"email": user.email},
        )
        return f"Created admin {user.email}."

    try:
        typer.echo(_run_db(_in_session(settings, _work)))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@users_app.command("reset-password")
def reset_password(email: Annotated[str, typer.Option("--email")]) -> None:
    """Set a new password for a user and sign them out everywhere (password is prompted)."""
    settings = _settings()
    password = _prompt_password()

    async def _work(session: AsyncSession) -> str:
        from salesfinder.auth import users

        user = await users.reset_password(
            session, email=email, password=password, now=datetime.now(UTC)
        )
        await audit.record(
            session,
            action=audit.USER_PASSWORD_RESET,
            actor_id=None,
            target_type="user",
            target_id=str(user.id),
            details={"email": user.email},
        )
        return f"Password reset for {user.email}; all sessions revoked."

    try:
        typer.echo(_run_db(_in_session(settings, _work)))
    except (ValueError, LookupError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@users_app.command("set-active")
def set_active(
    email: Annotated[str, typer.Option("--email")],
    active: Annotated[bool, typer.Option("--active/--inactive")],
) -> None:
    """Activate or deactivate a user (deactivating also revokes their sessions)."""
    settings = _settings()

    async def _work(session: AsyncSession) -> str:
        from salesfinder.auth import users

        found = await users.find_by_email(session, email)
        if found is None:
            raise LookupError("No such user.")
        user, invites_revoked = await users.set_active(
            session, found.id, active, now=datetime.now(UTC)
        )
        details: dict[str, object] = {"email": user.email}
        if not active:
            details["invites_revoked"] = invites_revoked
        await audit.record(
            session,
            action=audit.USER_REACTIVATED if active else audit.USER_DEACTIVATED,
            actor_id=None,
            target_type="user",
            target_id=str(user.id),
            details=details,
        )
        return f"{user.email} is now {'active' if active else 'inactive'}."

    try:
        typer.echo(_run_db(_in_session(settings, _work)))
    except (ValueError, LookupError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@app.command()
def doctor() -> None:
    """Check the environment; exit 1 if any check fails."""
    settings = _settings()
    results = asyncio.run(run_checks(settings))
    for r in results:
        typer.echo(f"{'✓' if r.ok else '✗'} {r.name} — {r.detail}")
    if not all(r.ok for r in results):
        raise typer.Exit(1)


@app.command()
def worker(
    once: Annotated[
        bool, typer.Option("--once", help="One recorded pass, then exit (1 if a step failed).")
    ] = False,
    no_send: Annotated[
        bool, typer.Option("--no-send", help="Skip the Sending step: no email is sent.")
    ] = False,
) -> None:
    """Run the background worker (HubSpot sync, sending approved emails, approved discovery
    jobs, profiling, contacts, drafting for active campaigns). Each pass is recorded in the
    runs log; quiet hours pause only the Claude steps."""
    from salesfinder import worker as worker_module
    from salesfinder.mail.factory import default_mail, without_sending

    settings = _settings()
    config, catalog, facts = _app_config_or_exit(settings)

    async def _main() -> tuple[str | None, list[str]]:
        """(why it couldn't run or stopped early, or None; the --once pass's failures)."""
        engine = make_engine(settings.database_url)
        try:
            # One worker at a time (--once included): a session-level advisory lock, held on
            # a dedicated connection for the worker's lifetime (released when it closes) and
            # checked on every beat (Ruling P7-C: never run unlocked).
            async with engine.connect() as lock_conn:
                locked = await lock_conn.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": worker_module.WORKER_LOCK_KEY},
                )
                await lock_conn.commit()  # don't sit idle-in-transaction; the lock survives
                if not locked:
                    return worker_module.ANOTHER_WORKER, []
                failures: list[str] = []
                try:
                    mail = default_mail()
                    deps = worker_module.WorkerDeps(
                        session_factory=make_session_factory(engine),
                        settings=settings,
                        config=config,
                        catalog=catalog,
                        facts=facts,
                        now=lambda: datetime.now(UTC),
                        # --no-send: the Sending step never runs; its sender refuses anyway.
                        mail=without_sending(mail) if no_send else mail,
                        send_enabled=not no_send,
                        lock_alive=worker_module.lock_check(lock_conn),
                    )
                    if once:
                        result = await worker_module.run_single(deps)
                        for line in result.lines:
                            typer.echo(line)
                        failures = result.failures
                    else:
                        await worker_module.run_forever(deps, stop=asyncio.Event())
                except worker_module.AnotherWorkerRunning:
                    return worker_module.ANOTHER_WORKER, []
                finally:
                    # Belt and braces (closing the connection releases it too); a broken
                    # connection here (the lock lost with it) must not mask anything.
                    with contextlib.suppress(SQLAlchemyError, OSError):
                        await lock_conn.execute(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": worker_module.WORKER_LOCK_KEY},
                        )
                        await lock_conn.commit()
            if deps.lock_lost:
                return worker_module.LOCK_LOST, failures
            return None, failures
        finally:
            await engine.dispose()

    stopped, failures = _run_db(_main())
    if stopped is not None:
        typer.echo(stopped, err=True)
        raise typer.Exit(1)
    if failures:
        raise typer.Exit(1)


@app.command()
def runs(
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=500, help="How many runs to list, newest first.")
    ] = 10,
) -> None:
    """The worker's health, then its latest runs (one line each)."""
    from salesfinder.ops import health
    from salesfinder.ops import runs as runs_service
    from salesfinder.ops.version import code_version

    settings = _settings()
    config, _, _ = _app_config_or_exit(settings)
    stamp = "%Y-%m-%d %H:%M UTC"

    async def _work(db: AsyncSession) -> str:
        now = datetime.now(UTC)
        result = await health.worker_health(db, now=now, config=config, code_version=code_version())
        running = "Worker running"
        if result.no_send:
            running += " with --no-send (nothing will be sent)"
        lines = [
            result.problem
            if result.problem is not None
            else f"{running}, {health.last_pass_phrase(result, now=now)}."
        ]
        for run in await runs_service.list_runs(db, limit=limit, offset=0):
            start = run.started_at.astimezone(UTC).strftime(stamp)
            end = run.finished_at.astimezone(UTC).strftime(stamp) if run.finished_at else "running"
            label, _ = health.run_label(run, now=now, config=config)
            lines.append(
                f"{start} → {end}  {label}  passes={run.passes}  failed={run.failed_steps}"
            )
        return "\n".join(lines)

    typer.echo(_run_db(_in_session(settings, _work)))


@hubspot_app.command("sync")
def hubspot_sync() -> None:
    """Sync HubSpot companies and contacts now (read-only) and print the result."""
    from salesfinder.exclusions.sync import request_sync, run_sync

    settings = _settings()
    token = settings.hubspot_token
    if token is None:
        typer.echo("SALESFINDER_HUBSPOT_TOKEN is not set.", err=True)
        raise typer.Exit(1)

    async def _work() -> tuple[str, bool]:
        engine = make_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                running = await db.scalar(
                    select(HubSpotSync.id).where(HubSpotSync.status == "running").limit(1)
                )
                if running is not None:
                    return "A HubSpot sync is already running.", False
                await request_sync(db, requested_by=None)
                await db.commit()
            row = await run_sync(
                factory, token=token.get_secret_value(), now=lambda: datetime.now(UTC)
            )
            if row.status == "succeeded":
                return (
                    f"HubSpot sync succeeded: {row.companies_count} companies, "
                    f"{row.contacts_count} contacts.",
                    True,
                )
            return f"HubSpot sync failed: {row.error}", False
        finally:
            await engine.dispose()

    message, ok = _run_db(_work())
    typer.echo(message, err=not ok)
    if not ok:
        raise typer.Exit(1)


@discover_app.command("jobs")
def discover_jobs() -> None:
    """List the 20 latest discovery jobs."""
    settings = _settings()

    async def _work() -> list[DiscoveryJob]:
        engine = make_engine(settings.database_url)
        try:
            async with make_session_factory(engine)() as db:
                result = await db.scalars(
                    select(DiscoveryJob).order_by(DiscoveryJob.created_at.desc()).limit(20)
                )
                return list(result.all())
        finally:
            await engine.dispose()

    rows = _run_db(_work())
    if not rows:
        typer.echo("No discovery jobs yet.")
        return
    typer.echo(
        f"{'created':<17} {'side':<8} {'status':<10} {'searches':>9} {'cost':>9} "
        f"{'new':>5} {'excluded':>8} {'dropped':>7}"
    )
    for j in rows:
        typer.echo(
            f"{j.created_at:%Y-%m-%d %H:%M}   {j.side:<8} {j.status:<10} "
            f"{f'{j.searches_done}/{j.estimated_searches}':>9} "
            f"{f'${j.actual_cost_usd:.4f}':>9} {j.companies_new:>5} "
            f"{j.companies_excluded:>8} {j.results_dropped:>7}"
        )


_STATE_VERBS = {"confirmed": "Confirmed", "rejected": "Rejected", "skipped": "Skipped"}


def _wanted_states(confirm: list[str], reject: list[str], skip: list[str]) -> dict[str, str]:
    """code -> new state from the options; ``--confirm free`` means every free signal (codes
    named explicitly win over it). Unknown or conflicting codes exit 1."""
    from salesfinder.signals.catalog import SIGNALS, by_code

    wanted: dict[str, str] = {}
    free = False
    for state, given in (("confirmed", confirm), ("rejected", reject), ("skipped", skip)):
        for raw in given:
            if state == "confirmed" and raw.strip().lower() == "free":
                free = True
                continue
            code = raw.strip().upper()
            try:
                by_code(code)
            except KeyError:
                typer.echo(f"Unknown signal {raw}", err=True)
                raise typer.Exit(1) from None
            if wanted.get(code, state) != state:
                typer.echo(f"Signal {code} is given more than one state.", err=True)
                raise typer.Exit(1)
            wanted[code] = state
    if free:
        for s in SIGNALS:
            if s.cost == "free":
                wanted.setdefault(s.code, "confirmed")
    return wanted


@app.command()
def signals(
    confirm: Annotated[
        list[str] | None,
        typer.Option("--confirm", help="Confirm a signal (CODE, or `free` for all free ones)."),
    ] = None,
    reject: Annotated[list[str] | None, typer.Option("--reject", help="Reject a signal.")] = None,
    skip: Annotated[list[str] | None, typer.Option("--skip", help="Skip a signal.")] = None,
    rescore: Annotated[
        bool,
        typer.Option(
            "--rescore", help="Recompute every company's score (after changing scoring weights)."
        ),
    ] = False,
) -> None:
    """List the signals profiling can collect, or change their state (then rescore)."""
    from salesfinder.profiling.service import change_signal_states, observed_counts, rescore_all
    from salesfinder.signals import settings as sig
    from salesfinder.signals.catalog import SIGNALS

    settings = _settings()
    wanted = _wanted_states(confirm or [], reject or [], skip or [])
    if not (confirm or reject or skip or rescore):

        async def _list(db: AsyncSession) -> str:
            states = await sig.states(db)
            counts = await observed_counts(db)
            rows = [f"{'code':<24} {'side':<9} {'state':<10} {'observed':>8}  label"]
            rows += [
                f"{s.code:<24} {s.side:<9} {states[s.code]:<10} {counts[s.code]:>8}  {s.label}"
                for s in SIGNALS
            ]
            return "\n".join(rows)

        typer.echo(_run_db(_in_session(settings, _list)))
        return

    config, _, _ = _app_config_or_exit(settings)

    async def _change(db: AsyncSession) -> str:
        result = await change_signal_states(
            db, wanted, actor=None, config=config, now=datetime.now(UTC)
        )
        if not result.changed:
            if rescore:
                rescored = await rescore_all(db, config=config, now=datetime.now(UTC))
                done = f"Rescored {rescored} company(ies)."
                return f"No changes.\n{done}" if wanted else done
            return "No changes."
        lines = [
            f"{_STATE_VERBS[state]} {len(codes)} signal(s)."
            for state, codes in result.changed.items()
        ]
        if result.queued:
            lines.append(f"Queued {result.queued} company(ies) to be profiled again.")
        lines.append(f"Rescored {result.rescored} company(ies).")
        return "\n".join(lines)

    typer.echo(_run_db(_in_session(settings, _change)))


@app.command()
def profile(
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Companies to profile (default profiling.batch_size)."),
    ] = None,
) -> None:
    """Profile due companies now (one batch, like a worker pass) and print the outcomes."""
    from salesfinder.profiling.fetch import SafeFetcher
    from salesfinder.profiling.service import ProfileOutcome, profile_batch
    from salesfinder.signals.settings import confirmed_codes
    from salesfinder.worker import outcomes_line

    settings = _settings()
    config, catalog, facts = _app_config_or_exit(settings)

    async def _work() -> tuple[bool, list[tuple[ProfileOutcome, str]]]:
        engine = make_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                if not await confirmed_codes(db):
                    return False, []
            async with SafeFetcher(config.profiling) as fetcher:
                outcomes = await profile_batch(
                    factory,
                    fetcher=fetcher,
                    catalog=catalog,
                    facts=facts,
                    config=config,
                    now=lambda: datetime.now(UTC),
                    limit=limit or config.profiling.batch_size,
                )
            ids = [o.company_id for o in outcomes]
            async with factory() as db:
                rows = await db.execute(
                    select(Company.id, Company.domain).where(Company.id.in_(ids))
                )
                domains = {company_id: domain for company_id, domain in rows.all()}
            return True, [(o, domains.get(o.company_id, str(o.company_id))) for o in outcomes]
        finally:
            await engine.dispose()

    any_confirmed, results = _run_db(_work())
    if not any_confirmed:
        typer.echo(
            "No signals are confirmed, so there is nothing to look for. "
            "Run `salesfinder signals --confirm free` first."
        )
        return
    if not results:
        typer.echo("Nothing to profile right now.")
        return
    for outcome, domain in results:
        score = "-" if outcome.score is None else str(outcome.score)
        typer.echo(
            f"{domain:<40} {outcome.profile_status:<8} {outcome.pages_fetched:>2} pages "
            f"{outcome.findings:>2} found  score {score:>3}  {outcome.error or ''}".rstrip()
        )
    typer.echo(outcomes_line([o for o, _ in results]))


def _plain(value: str | None) -> str:
    """Page text for a terminal: control characters dropped."""
    return "".join(c for c in (value or "") if c.isprintable())


@app.command()
def contacts(
    company: Annotated[
        str | None,
        typer.Option("--company", help="Show the people and addresses found for this domain."),
    ] = None,
    verify: Annotated[
        bool, typer.Option("--verify", help="Check due companies' contacts now (one batch).")
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Companies to check (default contacts.batch_size)."),
    ] = None,
) -> None:
    """Show a company's people and email addresses, or check due companies' contacts now."""
    if company is None and not verify:
        typer.echo("Give --company <domain> or --verify.", err=True)
        raise typer.Exit(2)
    settings = _settings()
    if company is not None:
        _show_contacts(settings, company)
    if verify:
        _verify_contacts(settings, limit)


def _show_contacts(settings: Settings, raw_domain: str) -> None:
    from salesfinder.contacts.service import mx_state_of, unsendable_reason
    from salesfinder.db.models import EmailAddress, Person
    from salesfinder.domains import normalize_domain

    domain = normalize_domain(raw_domain.strip()) or raw_domain.strip().lower()

    async def _work(db: AsyncSession) -> str:
        companies = (
            await db.scalars(select(Company).where(Company.domain == domain).order_by(Company.side))
        ).all()
        blocks: list[str] = []
        for c in companies:
            mx_state = await mx_state_of(db, c.domain)
            people = (
                await db.scalars(
                    select(Person)
                    .where(Person.company_id == c.id)
                    .order_by(Person.rank.asc().nulls_last(), Person.full_name)
                )
            ).all()
            addresses = (
                await db.scalars(
                    select(EmailAddress)
                    .where(EmailAddress.company_id == c.id)
                    .order_by(EmailAddress.address)
                )
            ).all()
            checked = (
                f"contacts checked {c.contacts_checked_at:%Y-%m-%d %H:%M} UTC"
                if c.contacts_checked_at
                else "contacts not checked yet"
            )
            lines = [
                f"{_plain(c.name)} ({c.domain}, {c.side})",
                f"Mail server: {mx_state} · {checked} · "
                f"{c.contacts_found} targets, {c.sendable_contacts} sendable",
            ]
            if c.status == "excluded":
                lines.append(f"Excluded: {_plain(c.exclusion_detail) or c.exclusion_reason}")
            if c.contact_form_url:
                lines.append(f"Contact form: {_plain(c.contact_form_url)}")
            lines.append("People:" if people else "People: none found")
            for p in people:
                star = "★" if p.is_target else " "
                title = f" — {_plain(p.title)}" if p.title else ""
                lines.append(f"  {star} {_plain(p.full_name)}{title} ({p.title_category})")
            lines.append("Addresses:" if addresses else "Addresses: none found")
            width = max((len(a.address) for a in addresses), default=0)
            for a in addresses:
                reason = unsendable_reason(a, mx_state=mx_state)
                verdict = "sendable: yes" if reason is None else f"sendable: no ({_plain(reason)})"
                lines.append(f"  {a.address:<{width}}  {a.status:<17}  {verdict}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    output = _run_db(_in_session(settings, _work))
    if not output:
        typer.echo(f"No company with domain {domain}.", err=True)
        raise typer.Exit(1)
    typer.echo(output)


def _verify_contacts(settings: Settings, limit: int | None) -> None:
    from salesfinder.worker import WorkerDeps, run_contacts_batch

    config, catalog, facts = _app_config_or_exit(settings)

    async def _work() -> tuple[list[str], str | None]:
        engine = make_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            deps = WorkerDeps(
                session_factory=factory,
                settings=settings,
                config=config,
                catalog=catalog,
                facts=facts,
                now=lambda: datetime.now(UTC),
            )
            run = await run_contacts_batch(deps, limit=limit or config.contacts.batch_size)
            outcomes, note = run.outcomes, run.note
            ids = [o.company_id for o in outcomes]
            async with factory() as db:
                rows = await db.execute(
                    select(Company.id, Company.domain).where(Company.id.in_(ids))
                )
                domains = {company_id: d for company_id, d in rows.all()}
            lines = [
                f"{domains.get(o.company_id, str(o.company_id)):<40} {o.mx_state:<8} "
                f"{o.targets:>2} targets {o.sendable:>2} sendable  {o.error or ''}".rstrip()
                for o in outcomes
            ]
            if outcomes:
                lines.append(run.line)
            return lines, note
        finally:
            await engine.dispose()

    lines, note = _run_db(_work())
    if note:
        typer.echo(note, err=True)
    if not lines:
        typer.echo("No companies due for a contact check.")
        return
    typer.echo("\n".join(lines))


@app.command()
def campaigns() -> None:
    """List campaigns: side, status, cohort, drafts today / daily cap, first emails awaiting
    approval in the Outbox."""
    from salesfinder.campaigns.service import list_campaigns

    settings = _settings()

    async def _work(db: AsyncSession) -> str:
        summaries = await list_campaigns(db, now=datetime.now(UTC))
        if not summaries:
            return "No campaigns yet."
        header = ("Name", "Side", "Status", "Cohort", "Today/cap", "Awaiting")
        rows = [
            (
                _plain(s.campaign.name),
                s.campaign.side,
                s.campaign.status,
                _plain(s.cohort_name),
                f"{s.drafted_today}/{s.campaign.daily_draft_cap}",
                str(s.awaiting_approval),
            )
            for s in summaries
        ]
        widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
        lines = ["  ".join(v.ljust(w) for v, w in zip(header, widths, strict=True)).rstrip()]
        for summary, row in zip(summaries, rows, strict=True):
            lines.append("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())
            if summary.campaign.last_error:
                lines.append(f"  Last error: {_plain(summary.campaign.last_error)}")
        return "\n".join(lines)

    typer.echo(_run_db(_in_session(settings, _work)))


@app.command()
def draft(
    campaign: Annotated[str, typer.Option("--campaign", help="The campaign's name.")],
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Most companies to ask Claude about (default writing.batch_size).",
        ),
    ] = None,
) -> None:
    """Draft emails for one active campaign now (one pass, like the worker's) and print the
    outcome. Drafts wait in the Outbox; nothing is sent."""
    from salesfinder.brain.runtime import ClaudeRuntime
    from salesfinder.campaigns.service import DraftOutcome, draft_batch
    from salesfinder.contacts.dns import MxChecker
    from salesfinder.db.models import Campaign
    from salesfinder.worker import writing_lines

    settings = _settings()
    config, catalog, facts = _app_config_or_exit(settings)
    name = campaign.strip()

    async def _work() -> tuple[str | None, DraftOutcome | None]:
        engine = make_engine(settings.database_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                row = await db.scalar(
                    select(Campaign).where(func.lower(Campaign.name) == name.lower())
                )
            if row is None:
                return f"No campaign named “{_plain(name)}”.", None
            if row.status != "active":
                return f"Campaign “{_plain(row.name)}” isn't active (it's {row.status}).", None
            outcome = await draft_batch(
                factory,
                runtime=ClaudeRuntime(settings, config.brain),
                catalog=catalog,
                facts=facts,
                config=config,
                now=lambda: datetime.now(UTC),
                limit=limit or config.writing.batch_size,
                campaign_id=row.id,
                mx_checker=MxChecker(config.contacts),
            )
            return None, outcome
        finally:
            await engine.dispose()

    refusal, outcome = _run_db(_work())
    if refusal is not None or outcome is None:
        typer.echo(refusal, err=True)
        raise typer.Exit(1)
    for error in outcome.errors:
        typer.echo(_plain(error))
    lines = writing_lines(outcome) or ["Drafted 0 emails for review (0 held by the gate)."]
    typer.echo("\n".join(lines))
    if outcome.stopped_reason is not None:
        raise typer.Exit(1)


def _sending_line(state: SendingState) -> str:
    return f"Sending is paused: {state.reason}" if state.paused else "Sending is on."


@sending_app.command("pause")
def sending_pause(
    reason: Annotated[str, typer.Option("--reason", help="Why (one line).")] = (
        "Paused from the command line."
    ),
) -> None:
    """Stop all sending at once."""
    from salesfinder.sending import state as sending_state

    settings = _settings()

    async def _work(db: AsyncSession) -> str:
        state, changed = await sending_state.pause(
            db, reason=reason, actor=None, now=datetime.now(UTC)
        )
        if not changed:
            return f"Already paused. {_sending_line(state)}"
        await audit.record(
            db,
            action=audit.SENDING_PAUSED,
            actor_id=None,
            details={"source": "cli", "reason": state.reason},
        )
        return _sending_line(state)

    try:
        typer.echo(_run_db(_in_session(settings, _work)))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@sending_app.command("resume")
def sending_resume(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm turning sending on.")] = False,
) -> None:
    """Turn sending on (the bounce window starts now)."""
    from salesfinder.sending import state as sending_state

    if not yes:
        typer.echo("Resuming turns real email sending on. Re-run with --yes to confirm.", err=True)
        raise typer.Exit(1)
    settings = _settings()

    async def _read(db: AsyncSession) -> str:
        state = await sending_state.get_state(db)
        return state.changed_at.isoformat() if state.changed_at else ""

    # Show the state being replaced, then resume only if it is still that state: an
    # auto-pause landing in between is refused, not overwritten.
    shown = _run_db(_in_session(settings, _read))
    expected = datetime.fromisoformat(shown) if shown else None

    async def _work(db: AsyncSession) -> str:
        state, changed = await sending_state.resume(
            db, actor=None, now=datetime.now(UTC), expected_changed_at=expected
        )
        if not changed:
            return f"Already on. {_sending_line(state)}"
        await audit.record(
            db, action=audit.SENDING_RESUMED, actor_id=None, details={"source": "cli"}
        )
        return _sending_line(state)

    try:
        typer.echo(_run_db(_in_session(settings, _work)))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@sending_app.command("status")
def sending_status() -> None:
    """Sending state, bounce rate and each mailbox's ramp."""
    from salesfinder.db.models import Mailbox
    from salesfinder.sending import policy
    from salesfinder.sending import state as sending_state

    settings = _settings()
    config, _, _ = _app_config_or_exit(settings)

    async def _work(db: AsyncSession) -> str:
        now = datetime.now(UTC)
        state = await sending_state.get_state(db)
        bounces, window = await sending_state.bounce_rate(db, now=now, config=config)
        lines = [_sending_line(state), f"Bounce rate: {bounces} of the last {window} sends."]
        boxes = (await db.scalars(select(Mailbox).order_by(Mailbox.address))).all()
        if not boxes:
            lines.append("No mailboxes configured.")
        for box in boxes:
            sent = await policy.sent_today(db, box.id, now=now)
            cap = policy.ramp_cap(box, today=now.date(), config=config)
            hold = box.hold_reason or policy.ramp_hold(
                box, today=now.date(), sent_today=sent, config=config
            )
            lines.append(
                f"{_plain(box.address)}: {'enabled' if box.enabled else 'disabled'}, "
                f"cap today {cap}, sent today {sent}, "
                f"DNS {'ok' if box.dns_ok else 'not ok'}"
                + (f", hold: {_plain(hold)}" if hold else "")
            )
        return "\n".join(lines)

    typer.echo(_run_db(_in_session(settings, _work)))


NOT_A_REPO = "Run this from the SalesFinder folder after `uv sync`."
NO_CLAUDE_DIR = "Claude CLI not found: set SALESFINDER_CLAUDE_BIN to its absolute path."


@ops_app.command("plists")
def ops_plists(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write <label>.plist files here (else print both)."),
    ] = None,
    log_dir: Annotated[
        Path | None,
        typer.Option("--log-dir", help="Where the logs go [default: ~/Library/Logs/SalesFinder]."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing plist files.")] = False,
) -> None:
    """Render the launchd LaunchAgents for the web app and the worker (never loads them)."""
    from salesfinder.ops import launchd

    repo = Path.cwd()
    if not (repo / "pyproject.toml").is_file() or not os.access(
        repo / ".venv" / "bin" / "salesfinder", os.X_OK
    ):
        typer.echo(NOT_A_REPO, err=True)
        raise typer.Exit(1)
    settings = _settings()
    claude = launchd.claude_dir(settings.claude_bin)
    if claude is None:
        typer.echo(NO_CLAUDE_DIR, err=True)
        raise typer.Exit(1)
    logs = (log_dir or launchd.default_log_dir()).expanduser().absolute()
    rendered = {
        label: launchd.render(kind, repo=repo, log_dir=logs, claude_dir=claude)
        for kind, label in launchd.LABELS.items()
    }
    if out is None:
        for label, data in rendered.items():
            typer.echo(f"# {label}.plist")
            typer.echo(data.decode("utf-8"))
        return
    target = out.expanduser().absolute()
    paths = {label: target / f"{label}.plist" for label in rendered}
    if not force:
        for path in paths.values():
            if path.exists():
                typer.echo(f"{path} exists; re-run with --force.", err=True)
                raise typer.Exit(1)
    target.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    for label, path in paths.items():
        path.write_bytes(rendered[label])
        typer.echo(f"Wrote {path}")
    typer.echo(f"Logs go to {logs} (web.log, worker.log; crash output in *.crash.log).")
    typer.echo("To load them (what `make start` does):")
    for path in paths.values():
        typer.echo(f"  launchctl bootstrap gui/{os.getuid()} {shlex.quote(str(path))}")
