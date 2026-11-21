"""MX lookups (DNS only, never SMTP) with a bounded, cached checker.

A domain can receive mail if it has an MX record (RFC 5321: or, failing that, an A/AAAA record
as the implicit MX). A null MX (a single ``.``) and NXDOMAIN mean it cannot. Timeouts and server
failures are ``unknown`` and retried soon. No connection to any mail server is ever made.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import dns.asyncresolver
import dns.exception
import dns.resolver
import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from salesfinder.config import ContactsConfig
from salesfinder.db.models import DomainMx
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email

log = structlog.get_logger(__name__)

MxState = Literal["has_mx", "no_mail", "unknown"]
Resolve = Callable[[str, str], Awaitable[list[tuple[int, str]]]]

UNKNOWN_RETRY_AFTER = timedelta(hours=1)
INVALID_DOMAIN = "Invalid domain"
DNS_TIMED_OUT = "DNS timed out"
DNS_SERVER_FAILURE = "DNS server failure"
MAX_DOMAIN = 255  # domain_mx.domain


@dataclass(frozen=True)
class MxResult:
    state: MxState
    hosts: tuple[str, ...] = ()
    error: str | None = None


def _normalize(domain: str) -> str | None:
    name = domain.strip().lower().rstrip(".")
    if not name:
        return None
    try:
        return name.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def mx_domain(domain: str) -> str | None:
    """The normalized name (lowercase, IDNA) an MX result is cached under; None if invalid."""
    return _normalize(domain)


def default_resolve(config: ContactsConfig) -> Resolve:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = config.dns_timeout_seconds
    resolver.timeout = config.dns_timeout_seconds

    async def resolve(qname: str, rdtype: str) -> list[tuple[int, str]]:
        answer = await resolver.resolve(qname, rdtype)
        if rdtype == "MX":
            return [(int(r.preference), str(r.exchange)) for r in answer]
        return [(0, str(r)) for r in answer]

    return resolve


class MxChecker:
    def __init__(self, config: ContactsConfig, *, resolve: Resolve | None = None) -> None:
        self._resolve = resolve if resolve is not None else default_resolve(config)
        self._sem = asyncio.Semaphore(config.dns_concurrency)

    async def check(self, domain: str) -> MxResult:
        name = _normalize(domain)
        if name is None:
            return MxResult("unknown", (), INVALID_DOMAIN)
        async with self._sem:
            return await self._check(name)

    async def _check(self, name: str) -> MxResult:
        try:
            try:
                answers = await self._resolve(name, "MX")
            except dns.resolver.NoAnswer:
                return await self._implicit_mx(name)
        except dns.resolver.NXDOMAIN:
            return MxResult("no_mail")
        except (dns.exception.Timeout, dns.resolver.LifetimeTimeout):
            return MxResult("unknown", (), DNS_TIMED_OUT)
        except dns.exception.DNSException:
            return MxResult("unknown", (), DNS_SERVER_FAILURE)
        hosts = tuple(h.rstrip(".").lower() for _, h in sorted(answers, key=lambda a: a[0]))
        if len(hosts) == 1 and hosts[0] == "":
            return MxResult("no_mail")  # null MX (RFC 7505)
        if not hosts:
            return await self._safe_implicit(name)
        return MxResult("has_mx", hosts)

    async def _safe_implicit(self, name: str) -> MxResult:
        try:
            return await self._implicit_mx(name)
        except dns.resolver.NXDOMAIN:
            return MxResult("no_mail")
        except (dns.exception.Timeout, dns.resolver.LifetimeTimeout):
            return MxResult("unknown", (), DNS_TIMED_OUT)
        except dns.exception.DNSException:
            return MxResult("unknown", (), DNS_SERVER_FAILURE)

    async def _implicit_mx(self, name: str) -> MxResult:
        """No MX records: an A or AAAA record makes the domain itself the mail host."""
        for rdtype in ("A", "AAAA"):
            try:
                if await self._resolve(name, rdtype):
                    return MxResult("has_mx", (name,))
            except dns.resolver.NoAnswer:
                continue
        return MxResult("no_mail")


async def mx_for(
    db_factory: async_sessionmaker[AsyncSession],
    checker: MxChecker,
    domain: str,
    *,
    now: datetime,
    config: ContactsConfig,
) -> MxResult:
    """The cached MX result for ``domain``, querying DNS (outside any transaction) when stale.

    A lookup that can't get an answer (``unknown``) never replaces a known answer (Ruling AH):
    the last known state is returned and kept, with only the row's ``error`` updated, so a DNS
    blip doesn't flip a domain's addresses to unsendable. Its stale ``checked_at`` makes the next
    call try DNS again."""
    name = _normalize(domain)
    if name is None:
        result = MxResult("unknown", (), INVALID_DOMAIN)
        key = domain.strip().lower()
        if 0 < len(key) <= MAX_DOMAIN:
            # Recorded under the name as given (what the contacts claim joins on), so the
            # claim holds the company back like any other domain DNS couldn't answer for.
            await _store(db_factory, key, result, now=now)
        return result
    previous: MxResult | None = None
    async with db_factory() as db:
        row = await db.get(DomainMx, name)
        if row is not None:
            if row.state != "unknown":
                previous = MxResult(row.state, tuple(row.mx_hosts))  # type: ignore[arg-type]
            age = now - row.checked_at
            ttl = (
                UNKNOWN_RETRY_AFTER
                if row.state == "unknown"
                else timedelta(days=config.mx_cache_days)
            )
            if age < ttl:
                return MxResult(row.state, tuple(row.mx_hosts), row.error)  # type: ignore[arg-type]
    result = await checker.check(name)
    if result.state == "unknown" and previous is not None:
        async with db_factory() as db:
            await db.execute(
                update(DomainMx)
                .where(DomainMx.domain == name, DomainMx.state != "unknown")
                .values(error=result.error)
            )
            await db.commit()
        return MxResult(previous.state, previous.hosts, result.error)
    await _store(db_factory, name, result, now=now)
    return result


async def _store(
    db_factory: async_sessionmaker[AsyncSession], name: str, result: MxResult, *, now: datetime
) -> None:
    values = {
        "domain": name,
        "state": result.state,
        "mx_hosts": list(result.hosts),
        "checked_at": now,
        "error": result.error,
    }
    stmt = insert(DomainMx).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DomainMx.domain],
        set_={k: v for k, v in values.items() if k != "domain"},
    )
    async with db_factory() as db:
        await db.execute(stmt)
        await db.commit()


# --------------------------------------------------------------------------- stale refresh


def mx_is_stale(
    state: str | None, checked_at: datetime | None, *, now: datetime, config: ContactsConfig
) -> bool:
    """Whether a cached MX answer is too old (or missing, or ``unknown``) for drafting and the
    gate to rely on: they want a known answer younger than ``contacts.mx_cache_days``."""
    if state is None or checked_at is None or state == "unknown":
        return True
    return now - checked_at >= timedelta(days=config.mx_cache_days)


async def stale_mx_domains(
    db: AsyncSession, addresses: Iterable[str], *, now: datetime, config: ContactsConfig
) -> list[str]:
    """The distinct MX names (sorted) of ``addresses``' domains whose cached answer is stale
    (``mx_is_stale``); free-mail domains never need one. Read only."""
    names: set[str] = set()
    for address in addresses:
        domain = domain_of_email(address) or address.rpartition("@")[2]
        if domain in FREE_MAIL_DOMAINS:
            continue
        name = mx_domain(domain)
        if name is not None:
            names.add(name)
    if not names:
        return []
    rows = {
        row.domain: (row.state, row.checked_at)
        for row in (
            await db.execute(
                select(DomainMx.domain, DomainMx.state, DomainMx.checked_at).where(
                    DomainMx.domain.in_(sorted(names))
                )
            )
        ).all()
    }
    return sorted(
        name for name in names if mx_is_stale(*rows.get(name, (None, None)), now=now, config=config)
    )


async def refresh_mx(
    db_factory: async_sessionmaker[AsyncSession],
    checker: MxChecker,
    domains: Iterable[str],
    *,
    now: datetime,
    config: ContactsConfig,
) -> int:
    """``mx_for`` each domain (its own short sessions: never call this inside a transaction).
    A lookup that fails leaves the cached answer as it was, so the gate keeps holding with its
    usual reason; an unexpected error is logged (its type only) and the rest still run.
    Returns how many domains were looked up."""
    done = 0
    for domain in domains:
        done += 1
        try:
            await mx_for(db_factory, checker, domain, now=now, config=config)
        except Exception as exc:
            log.warning("mx_refresh_failed", error_type=type(exc).__name__)
    return done
