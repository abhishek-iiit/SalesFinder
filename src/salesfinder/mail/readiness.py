"""Is the sending domain ready for cold mail? SPF, DKIM, DMARC and MX, by DNS only.

Cold sending stays blocked until every check passes. Nothing here connects to a mail server;
a DNS error is reported as "Couldn't check" and counts as not ok (never as a pass).
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import dns.exception
import dns.resolver

from salesfinder.contacts.dns import DNS_SERVER_FAILURE, DNS_TIMED_OUT, MxChecker, Resolve

CheckName = Literal["spf", "dkim", "dmarc", "mx"]

NO_SPF = "No SPF record."
MANY_SPF = "More than one SPF record."
SPF_NO_GOOGLE = "SPF doesn't include Google (include:_spf.google.com)."
SPF_NO_END = "SPF doesn't end with ~all or -all."
MANY_DMARC = "More than one DMARC record."
DMARC_POLICIES = ("none", "quarantine", "reject")
SPF_PLUS_ALL = "SPF allows everyone (+all)."
NO_DMARC = "No DMARC record."
DMARC_NO_POLICY = "DMARC has no policy (p=)."
NO_MX = "Domain can't receive mail (no MX)."
OK = "OK."
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class Check:
    name: CheckName
    ok: bool
    detail: str


@dataclass(frozen=True)
class Report:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {"checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}


def report_from_json(data: dict[str, Any]) -> Report:
    """A stored report; anything malformed reads as no checks (so not ok)."""
    checks: list[Check] = []
    raw = data.get("checks") if isinstance(data, dict) else None
    for item in raw if isinstance(raw, list) else []:
        if (
            isinstance(item, dict)
            and item.get("name") in ("spf", "dkim", "dmarc", "mx")
            and isinstance(item.get("ok"), bool)
            and isinstance(item.get("detail"), str)
        ):
            checks.append(Check(item["name"], item["ok"], item["detail"]))
    return Report(checks)


def _unquote(raw: str) -> str:
    """One TXT answer as the record text: its quoted chunks joined (RFC 1035 splits long ones)."""
    chunks = _QUOTED.findall(raw)
    if not chunks:
        return raw.strip()
    return "".join(c.replace('\\"', '"').replace("\\\\", "\\") for c in chunks)


async def _txt(resolve: Resolve, name: str) -> list[str]:
    """The TXT records at ``name`` (none if the name has none). DNS errors propagate."""
    try:
        answers = await resolve(name, "TXT")
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return []
    return [_unquote(text) for _, text in answers]


def _failure(exc: dns.exception.DNSException) -> str:
    timed_out = isinstance(exc, dns.exception.Timeout | dns.resolver.LifetimeTimeout)
    return f"Couldn't check: {DNS_TIMED_OUT if timed_out else DNS_SERVER_FAILURE}."


async def _spf(domain: str, resolve: Resolve, *, google: bool) -> Check:
    records = [t for t in await _txt(resolve, domain) if t.lower().startswith("v=spf1")]
    if not records:
        return Check("spf", False, NO_SPF)
    if len(records) > 1:
        return Check("spf", False, MANY_SPF)
    terms = records[0].lower().split()
    if google and "include:_spf.google.com" not in terms:
        return Check("spf", False, SPF_NO_GOOGLE)
    if any(t in ("+all", "all") for t in terms):
        return Check("spf", False, SPF_PLUS_ALL)
    if terms[-1] not in ("~all", "-all"):
        return Check("spf", False, SPF_NO_END)
    return Check("spf", True, OK)


async def _dkim(domain: str, selector: str, resolve: Resolve) -> Check:
    name = f"{selector}._domainkey.{domain}"
    for text in await _txt(resolve, name):
        tags = _tags(text)
        if tags.get("p", ""):
            return Check("dkim", True, OK)
    return Check("dkim", False, f"No DKIM key at {name}.")


def _tags(record: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in record.split(";"):
        key, eq, value = part.partition("=")
        if eq:
            tags.setdefault(key.strip().lower(), value.strip())
    return tags


async def _dmarc(domain: str, resolve: Resolve) -> Check:
    records = [
        t
        for t in await _txt(resolve, f"_dmarc.{domain}")
        if t.strip().upper().startswith("V=DMARC1")
    ]
    if not records:
        return Check("dmarc", False, NO_DMARC)
    if len(records) > 1:
        return Check("dmarc", False, MANY_DMARC)
    if _tags(records[0]).get("p", "").lower() not in DMARC_POLICIES:
        return Check("dmarc", False, DMARC_NO_POLICY)
    return Check("dmarc", True, OK)


async def _mx(domain: str, mx: MxChecker) -> Check:
    try:
        result = await mx.check(domain)
    except Exception:
        return Check("mx", False, f"Couldn't check: {DNS_SERVER_FAILURE}.")
    if result.state == "has_mx":
        return Check("mx", True, OK)
    if result.state == "no_mail":
        return Check("mx", False, NO_MX)
    return Check("mx", False, f"Couldn't check: {result.error or DNS_SERVER_FAILURE}.")


async def check_domain(
    domain: str,
    *,
    dkim_selector: str,
    resolve: Resolve,
    mx: MxChecker,
    google: bool = True,
) -> Report:
    """SPF, DKIM, DMARC and MX of ``domain``. ``google`` says the mailbox sends through
    Gmail/Workspace, so the SPF record must include Google's."""
    domain = domain.strip().lower().rstrip(".")
    checks: list[Check] = []
    runs: list[tuple[CheckName, Callable[[], Awaitable[Check]]]] = [
        ("spf", lambda: _spf(domain, resolve, google=google)),
        ("dkim", lambda: _dkim(domain, dkim_selector, resolve)),
        ("dmarc", lambda: _dmarc(domain, resolve)),
    ]
    for name, run in runs:
        try:
            checks.append(await run())
        except dns.exception.DNSException as exc:
            checks.append(Check(name, False, _failure(exc)))
        except Exception:
            checks.append(Check(name, False, f"Couldn't check: {DNS_SERVER_FAILURE}."))
    checks.append(await _mx(domain, mx))
    return Report(checks)
