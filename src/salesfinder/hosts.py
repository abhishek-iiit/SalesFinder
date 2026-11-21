"""Host-name matching shared by the web app's Origin check and the LAN-config warnings, kept
here (not in ``web``) so ``ops`` and ``doctor`` don't depend on the web layer.

Starlette's TrustedHostMiddleware compares the lower-cased Host (IPv6 literals keep their
brackets, e.g. ``[fe80::1]``) with each ``allowed_hosts`` pattern, which it lower-cases too
(Starlette 1.10); Settings stores the patterns lower-cased and ``host_allowed`` lower-cases
both sides, so the rules below agree with it.
"""

import ipaddress
from collections.abc import Iterable


def _bare(host: str) -> str:
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def normalize_host(host: str) -> str:
    """Lower-case; a bare IPv6 literal (as ``urlsplit().hostname`` gives it) gets brackets,
    the form a Host header carries."""
    host = host.lower()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def is_loopback_host(host: str) -> bool:
    """``localhost``, any 127.x address, or ``::1`` (bracketed or not)."""
    bare = _bare(host.strip().lower())
    if bare == "localhost":
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def host_allowed(host: str | None, allowed_hosts: Iterable[str]) -> bool:
    """Match a host name against allowed patterns (same rules as TrustedHostMiddleware)."""
    if not host:
        return False
    host = normalize_host(host)
    for pattern in allowed_hosts:
        pattern = pattern.lower()
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False
