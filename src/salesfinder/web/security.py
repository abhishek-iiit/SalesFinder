"""Security middleware: response headers and the cross-origin check for unsafe methods."""

from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from salesfinder.hosts import host_allowed

__all__ = [
    "CSP",
    "HSTS",
    "OriginCheckMiddleware",
    "SecurityHeadersMiddleware",
    "host_allowed",
    "safe_next",
]

CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
HSTS = "max-age=31536000"
SAFE_METHODS = frozenset({"GET", "HEAD"})


class SecurityHeadersMiddleware:
    """Adds the security headers to every HTTP response (pure ASGI, so it wraps errors too)."""

    def __init__(self, app: ASGIApp, *, https: bool) -> None:
        self.app = app
        self.headers: dict[str, str] = {
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
        }
        if https:
            self.headers["Strict-Transport-Security"] = HSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _source_host(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        return parts.hostname if parts.scheme in ("http", "https") else None
    except ValueError:
        return None


class OriginCheckMiddleware:
    """Rejects unsafe requests whose Origin (else Referer) names a host outside allowed_hosts.

    Requests carrying neither header pass through; authenticated forms are additionally
    protected by the per-session CSRF token. ``Origin: null`` (sandboxed/opaque origins) and
    unparseable values are rejected.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        source = headers.get("origin") or headers.get("referer")
        if source is not None and not host_allowed(_source_host(source), self.allowed_hosts):
            response = PlainTextResponse("Cross-origin request rejected.", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def safe_next(value: str | None) -> str:
    """Return ``value`` if it is a same-site absolute path, else ``"/"`` (no open redirects)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return value
