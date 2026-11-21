"""Fetch pages from untrusted company websites without SSRF, hangs or unbounded memory.

Every connection (each redirect hop, each robots.txt fetch) goes through the same path:

1. only ``http``/``https``;
2. the hostname is resolved once with the injected resolver and *every* address must be
   globally routable (an IP-literal host is checked directly, never resolved);
3. the connection is pinned to the validated address: the request URL carries the IP, the
   ``Host`` header carries the original host, and TLS uses the original host for SNI and
   certificate verification (httpcore's ``sni_hostname`` extension). Nothing re-resolves the
   name between validation and connect, so DNS rebinding can't swap the address;
4. redirects are followed by hand (≤ 3), each hop re-validated and kept on the same
   registrable domain;
5. DNS + connect + headers + body of one request share one ``page_timeout_seconds`` deadline,
   and the body (after decompression) is read only up to ``max_page_bytes``.
"""

import asyncio
import codecs
import ipaddress
import re
import socket
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Literal, Self
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
import structlog

from salesfinder.config import ProfilingConfig
from salesfinder.domains import host_of, normalize_domain

log = structlog.get_logger(__name__)

BlockReason = Literal[
    "private_address", "robots", "scheme", "off_site_redirect", "too_many_redirects"
]
Resolver = Callable[[str], Awaitable[list[str]]]

MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})  # beyond the scheme's default port
# One get() — robots.txt fetches and every redirect hop included — never takes longer than this
# many page timeouts.
OVERALL_TIMEOUT_FACTOR = 3
_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([^\s;"']+)""", re.IGNORECASE)


class FetchBlocked(Exception):
    """The fetch was refused before (or instead of) connecting."""

    def __init__(self, reason: BlockReason, message: str) -> None:
        super().__init__(message)
        self.reason: BlockReason = reason
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL, after redirects
    status: int  # 0 when no response was received
    content_type: str  # bare lowercased media type, "" if absent
    html: str | None  # decoded text for HTML responses, else None
    truncated: bool
    error: str | None


async def default_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(raw: str) -> bool:
    """True only for a globally routable unicast address (IPv4-mapped IPv6 judged as its v4)."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and not is_public_address(str(ip.ipv4_mapped)):
            return False
        if ip.is_site_local:
            return False
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def robots_agent(user_agent: str) -> str:
    """The product token robots.txt groups are matched against ("SalesFinderBot/0.1 (...)" ->
    "SalesFinderBot")."""
    return re.split(r"[/\s]", user_agent.strip(), maxsplit=1)[0] or user_agent


def _site_of(url: str) -> str | None:
    return normalize_domain(url) or host_of(url)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _decode(body: bytes, charset: str | None) -> str:
    """Decode with the declared charset if it is a real text encoding, else UTF-8 with
    replacement. Server-chosen codecs can fail in odd ways (``undefined`` and ``idna`` raise
    ``UnicodeError`` even with ``errors="replace"``; ``rot13``/``base64`` aren't text)."""
    if charset:
        try:
            if codecs.lookup(charset)._is_text_encoding:
                return body.decode(charset, errors="replace")
        except (LookupError, UnicodeError, ValueError):
            pass
    return body.decode("utf-8", errors="replace")


class _UnsupportedEncoding(Exception):
    pass


class _BoundedDecoder:
    """Content-Encoding decoder that never produces more than it is asked for (gzip bombs)."""

    def __init__(self, encoding: str) -> None:
        encoding = encoding.strip().lower()
        if encoding in ("", "identity"):
            self._zlib = None
        elif encoding in ("gzip", "x-gzip", "deflate"):
            self._zlib = zlib.decompressobj(32 + zlib.MAX_WBITS)  # gzip or zlib header
        else:
            raise _UnsupportedEncoding(encoding)

    def feed(self, data: bytes, max_length: int) -> bytes:
        if self._zlib is None:
            return data[:max_length]
        return self._zlib.decompress(data, max_length)

    def flush(self, max_length: int) -> bytes:
        return b"" if self._zlib is None else self._zlib.flush()[:max_length]


@dataclass(frozen=True)
class _Raw:
    url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes | None
    truncated: bool
    error: str | None


class SafeFetcher:
    def __init__(
        self,
        config: ProfilingConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._config = config
        self._resolver = resolver or default_resolver
        self._timeout = config.page_timeout_seconds
        self._agent = robots_agent(config.user_agent)
        if transport is None:
            # No keep-alive: the pool is keyed by IP, so a kept connection (with one host's SNI)
            # could otherwise be reused for a different hostname that shares the address.
            transport = httpx.AsyncHTTPTransport(
                limits=httpx.Limits(max_keepalive_connections=0), retries=0
            )
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # no proxies/.netrc from the environment
            timeout=httpx.Timeout(self._timeout),
            # Cookies would be stored against the pinned IP and leak across sites sharing it.
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            headers={
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def timeout_error(self) -> str:
        return f"Timed out after {self._timeout:g}s"

    async def get(self, url: str, *, site_domain: str) -> FetchResult:
        """Fetch ``url`` (an HTML page of ``site_domain``). Refusals raise ``FetchBlocked``;
        network failures and HTTP errors come back as a result with ``error`` set. The whole
        call (robots.txt fetches and redirect hops included) is bounded by
        ``OVERALL_TIMEOUT_FACTOR`` page timeouts."""
        site = normalize_domain(site_domain) or site_domain
        overall = self._timeout * OVERALL_TIMEOUT_FACTOR
        try:
            async with asyncio.timeout(overall):
                raw = await self._fetch(url, site_domain=site, check_robots=True, read_any=False)
        except TimeoutError:
            raw = self._failed(url, f"Timed out after {overall:g}s")
        html = None
        if raw.body is not None and raw.content_type in HTML_TYPES:
            html = _decode(raw.body, raw.charset)
        return FetchResult(
            url=raw.url,
            status=raw.status,
            content_type=raw.content_type,
            html=html,
            truncated=raw.truncated,
            error=raw.error,
        )

    async def allowed_by_robots(self, url: str) -> bool:
        """Whether robots.txt of ``url``'s origin lets our agent fetch it. Fetched once per
        origin; a missing, erroring, unreachable or refused robots.txt allows everything."""
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL:
            return True  # the fetch itself will fail on this URL
        origin = f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots:
                self._robots[origin] = await self._load_robots(f"{origin}/robots.txt")
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self._agent, url)

    async def _load_robots(self, robots_url: str) -> RobotFileParser | None:
        try:
            raw = await self._fetch(
                robots_url, site_domain=_site_of(robots_url), check_robots=False, read_any=True
            )
        except FetchBlocked as exc:
            log.warning("robots_unavailable", url=robots_url, reason=exc.reason)
            return None
        if raw.error is not None or raw.body is None:
            if raw.status == 0 or raw.status >= 500:
                log.warning("robots_unavailable", url=robots_url, error=raw.error)
            return None  # 4xx: no robots.txt, everything allowed
        parser = RobotFileParser(robots_url)
        parser.parse(_decode(raw.body, raw.charset).splitlines())
        return parser

    async def _fetch(
        self, url: str, *, site_domain: str | None, check_robots: bool, read_any: bool
    ) -> _Raw:
        current = url
        redirects = 0
        while True:
            target = self._check_url(current)
            if target.userinfo:  # never send (or report) credentials embedded in a URL
                target = target.copy_with(userinfo=b"")
                current = str(target)
            if redirects and _site_of(current) != site_domain:
                raise FetchBlocked(
                    "off_site_redirect", f"Redirected off-site to {target.host} (from {url})"
                )
            if check_robots and not await self.allowed_by_robots(current):
                raise FetchBlocked("robots", f"robots.txt disallows {current}")
            outcome = await self._request(current, target, read_any=read_any)
            if isinstance(outcome, _Raw):
                return outcome
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise FetchBlocked(
                    "too_many_redirects", f"More than {MAX_REDIRECTS} redirects from {url}"
                )
            current = urljoin(current, outcome)

    @staticmethod
    def _check_url(url: str) -> httpx.URL:
        try:
            target = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise FetchBlocked("scheme", f"Not a fetchable URL: {url!r}") from exc
        if target.scheme not in ("http", "https") or not target.host:
            raise FetchBlocked("scheme", f"Only http(s) URLs are fetched: {url!r}")
        if target.port is not None and target.port not in ALLOWED_PORTS:
            raise FetchBlocked("scheme", "Only standard web ports are allowed.")
        if _is_ip_literal(target.host) and not is_public_address(target.host):
            raise FetchBlocked("private_address", f"{target.host} is not a public address")
        return target

    async def _resolve(self, host: str) -> list[str]:
        """Validated addresses to connect to (empty if the name doesn't resolve). Any non-public
        address refuses the whole host, so no answer order can sneak one in."""
        if _is_ip_literal(host):
            return [host]  # already checked in _check_url
        try:
            host.encode("idna")  # rejects empty or over-long (> 63 chars) labels up front
            addresses = await self._resolver(host)
        except (OSError, UnicodeError, ValueError):
            return []
        bad = [a for a in addresses if not is_public_address(a)]
        if bad:
            raise FetchBlocked("private_address", f"{host} resolves to non-public {bad[0]}")
        return addresses

    def _failed(self, url: str, error: str, status: int = 0, content_type: str = "") -> _Raw:
        return _Raw(url, status, content_type, None, None, False, error)

    async def _request(self, url: str, target: httpx.URL, *, read_any: bool) -> _Raw | str:
        """One pinned GET: a finished ``_Raw``, or the ``Location`` of a redirect."""
        try:
            async with asyncio.timeout(self._timeout):
                for address in await self._resolve(target.raw_host.decode("ascii")):
                    try:
                        return await self._exchange(url, target, address, read_any=read_any)
                    except httpx.ConnectError:
                        continue  # e.g. an IPv6 answer on a v4-only path: try the next one
                return self._failed(url, "Could not connect")
        except (TimeoutError, httpx.TimeoutException):
            return self._failed(url, self.timeout_error)
        except httpx.TransportError as exc:
            return self._failed(url, f"Connection failed ({type(exc).__name__})")

    async def _exchange(
        self, url: str, target: httpx.URL, address: str, *, read_any: bool
    ) -> _Raw | str:
        pinned = target.copy_with(host=address)
        extensions: dict[str, str] = {}
        if target.scheme == "https" and not _is_ip_literal(target.host):
            # TLS SNI and certificate verification against the name, not the pinned IP.
            extensions["sni_hostname"] = target.raw_host.decode("ascii")
        headers = {"Host": target.netloc.decode("ascii")}
        async with self._client.stream(
            "GET", pinned, headers=headers, extensions=extensions
        ) as response:
            status = response.status_code
            content_type_header = response.headers.get("content-type", "")
            content_type = content_type_header.split(";", 1)[0].strip().lower()
            location: str | None = response.headers.get("location")
            if status in REDIRECT_STATUSES and location:
                return location
            if not 200 <= status < 300:
                return self._failed(url, f"HTTP {status}", status, content_type)
            if not read_any and content_type not in HTML_TYPES:
                return _Raw(url, status, content_type, None, None, False, None)
            match = _CHARSET_RE.search(content_type_header)
            charset = match.group(1) if match else None
            try:
                body, truncated = await self._read_bounded(response)
            except _UnsupportedEncoding as exc:
                return self._failed(
                    url, f"Unsupported content encoding {exc}", status, content_type
                )
            except zlib.error:
                return self._failed(url, "Could not decode response", status, content_type)
            return _Raw(url, status, content_type, charset, body, truncated, None)

    async def _read_bounded(self, response: httpx.Response) -> tuple[bytes, bool]:
        limit = self._config.max_page_bytes
        decoder = _BoundedDecoder(response.headers.get("content-encoding", ""))
        body = bytearray()
        async for chunk in response.aiter_raw():
            # Ask for one byte past the limit so "exactly full" and "overflowing" differ.
            body += decoder.feed(chunk, limit - len(body) + 1)
            if len(body) > limit:
                return bytes(body[:limit]), True
        body += decoder.flush(limit - len(body) + 1)
        if len(body) > limit:
            return bytes(body[:limit]), True
        return bytes(body), False
