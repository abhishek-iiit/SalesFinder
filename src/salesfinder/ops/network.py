"""LAN-mode configuration checks, shared by the doctor ``network`` check and the Settings page
(so both name the same problems in the same words)."""

from urllib.parse import urlsplit

from salesfinder.hosts import host_allowed, is_loopback_host, normalize_host
from salesfinder.settings import Settings

LOOPBACK_ONLY = (
    "LAN mode is on but SALESFINDER_ALLOWED_HOSTS lists only loopback names — teammates'"
    ' browsers will be refused. Add the names they use, e.g. ["salesfinder.local",'
    ' "192.168.1.20"].'
)
WILDCARD = (
    'SALESFINDER_ALLOWED_HOSTS contains "*" — any Host header is accepted, so DNS-rebinding'
    " protection is off."
)
NO_PUBLIC_URL = (
    "LAN mode is on but SALESFINDER_PUBLIC_URL isn't set — invite links will use whatever"
    " address the admin browsed from."
)
PUBLIC_URL_HOST = (
    "SALESFINDER_PUBLIC_URL's host {host} isn't in SALESFINDER_ALLOWED_HOSTS — invite links"
    " would be refused."
)


def _public_host(settings: Settings) -> str | None:
    """The public URL's host as a Host header carries it (IPv6 literals bracketed)."""
    if not settings.public_url:
        return None
    try:
        hostname = urlsplit(settings.public_url).hostname
    except ValueError:  # pragma: no cover - validated by Settings
        return None
    return normalize_host(hostname) if hostname else None


def lan_warnings(settings: Settings) -> list[str]:
    """Every LAN misconfiguration found, in a fixed order; ``[]`` when there is none."""
    warnings: list[str] = []
    hosts = settings.allowed_hosts
    if settings.lan_mode and all(is_loopback_host(h) for h in hosts):
        warnings.append(LOOPBACK_ONLY)
    wildcard = "*" in hosts
    if wildcard:
        warnings.append(WILDCARD)
    if settings.lan_mode and not settings.public_url:
        warnings.append(NO_PUBLIC_URL)
    host = _public_host(settings)
    if host is not None and not wildcard and not host_allowed(host, hosts):
        warnings.append(PUBLIC_URL_HOST.format(host=host))
    return warnings


def mode_line(settings: Settings) -> str:
    if settings.lan_mode:
        return f"LAN over HTTPS on {settings.bind_host}:{settings.port}"
    return f"Loopback only ({settings.bind_host})"
