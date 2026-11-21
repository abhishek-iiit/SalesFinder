"""A fake web behind ``httpx.MockTransport`` plus a fake DNS resolver: strict, with a call log.

``sites`` maps a full URL (``"https://acme.com/"``, ``"https://acme.com/robots.txt"``) to:

- a ``str``: a 200 ``text/html; charset=utf-8`` page;
- a ``(status, headers, body)`` tuple: served as given (3xx + ``location`` for redirects);
- a ``Slow``: sleeps before answering, or (``drip=True``) answers at once and then drips the
  body one small chunk per ``seconds``.

A URL on a known host that isn't in ``sites`` is a 404. ``dns`` maps a hostname to the IPs the
resolver returns; an unknown hostname fails to resolve and a request to an IP that isn't one of
the Host's addresses is a connection error. Bodies are streamed in 64 KiB chunks and
``bytes_sent`` counts what was actually read, so tests can tell whether a reader stopped early.

The fetcher must pin connections: every request must be addressed to an IP literal, with the
real hostname in the ``Host`` header. Anything else is recorded in ``violations`` and raises.
"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

CHUNK = 64 * 1024

Canned = tuple[int, dict[str, str], bytes]


@dataclass(frozen=True)
class Slow:
    seconds: float
    drip: bool = False


Site = str | Canned | Slow


def html(body: str) -> Canned:
    return (200, {"content-type": "text/html; charset=utf-8"}, body.encode())


def redirect(location: str, status: int = 301) -> Canned:
    return (status, {"location": location}, b"")


class _Stream(httpx.AsyncByteStream):
    def __init__(self, web: "FakeWeb", body: bytes, delay: float = 0.0, *, endless: bool = False):
        self.web = web
        self.body = body
        self.delay = delay
        self.endless = endless

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.endless:
            while True:
                self.web.bytes_sent += 1
                yield b"x"
                await asyncio.sleep(self.delay)
        for start in range(0, len(self.body), CHUNK):
            chunk = self.body[start : start + CHUNK]
            self.web.bytes_sent += len(chunk)
            yield chunk


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class FakeWeb:
    def __init__(self, sites: dict[str, Site], dns: dict[str, list[str]]) -> None:
        self.sites = dict(sites)
        self.dns = {host: list(ips) for host, ips in dns.items()}
        self.calls: list[httpx.Request] = []
        self.lookups: list[str] = []
        self.violations: list[str] = []
        self.bytes_sent = 0
        self.transport = httpx.MockTransport(self._handle)

    async def resolver(self, host: str) -> list[str]:
        self.lookups.append(host)
        if host not in self.dns:
            raise socket.gaierror(socket.EAI_NONAME, f"unknown host {host}")
        return list(self.dns[host])

    def logical_urls(self) -> list[str]:
        """The URLs as the site sees them (Host header + path), in request order."""
        return [self._logical_url(c) for c in self.calls]

    def _violation(self, message: str) -> None:
        self.violations.append(message)
        raise AssertionError(message)

    @staticmethod
    def _logical_url(request: httpx.Request) -> str:
        path = request.url.raw_path.decode("ascii") or "/"
        return f"{request.url.scheme}://{request.headers['host']}{path}"

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method != "GET":
            self._violation(f"non-GET request: {request.method} {request.url}")
        if not _is_ip(request.url.host):
            self._violation(f"unpinned request (URL host is not an IP): {request.url}")
        host = httpx.URL(f"//{request.headers['host']}").raw_host.decode("ascii")
        addresses = [host] if _is_ip(host) else self.dns.get(host, [])
        if request.url.host not in addresses:
            raise httpx.ConnectError(f"no route to {request.url.host} for {host}", request=request)

        site = self.sites.get(self._logical_url(request))
        if site is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"nope")
        if isinstance(site, Slow):
            if site.drip:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    stream=_Stream(self, b"", site.seconds, endless=True),
                )
            await asyncio.sleep(site.seconds)
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"late")
        status, headers, body = html(site) if isinstance(site, str) else site
        return httpx.Response(status, headers=headers, stream=_Stream(self, body))
