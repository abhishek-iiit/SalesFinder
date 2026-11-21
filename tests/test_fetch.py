"""SafeFetcher: SSRF-safe, robots-obeying, bounded fetches (FakeWeb; no network)."""

import asyncio
import gzip
import itertools
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest

from fakes.web import FakeWeb, Site, Slow, redirect
from salesfinder.config import ProfilingConfig
from salesfinder.profiling.fetch import FetchBlocked, FetchResult, SafeFetcher

PUBLIC = "93.184.216.34"
PUBLIC_WWW = "93.184.216.35"
DNS = {"acme.com": [PUBLIC], "www.acme.com": [PUBLIC_WWW], "evil.com": ["198.51.100.7"]}
HOME = "<html><body><h1>Acme Chemicals</h1></body></html>"


def make(
    sites: dict[str, Site], dns: dict[str, list[str]] | None = None, **config: Any
) -> tuple[FakeWeb, SafeFetcher]:
    web = FakeWeb(sites, DNS if dns is None else dns)
    fetcher = SafeFetcher(ProfilingConfig(**config), transport=web.transport, resolver=web.resolver)
    return web, fetcher


def page_calls(web: FakeWeb) -> list[str]:
    return [u for u in web.logical_urls() if not u.endswith("/robots.txt")]


async def blocked(fetcher: SafeFetcher, url: str, site_domain: str = "acme.com") -> FetchBlocked:
    with pytest.raises(FetchBlocked) as info:
        async with fetcher:
            await fetcher.get(url, site_domain=site_domain)
    return info.value


async def test_fetches_html_with_user_agent() -> None:
    web, fetcher = make({"https://acme.com/": HOME})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")

    assert result == FetchResult(
        url="https://acme.com/",
        status=200,
        content_type="text/html",
        html=HOME,
        truncated=False,
        error=None,
    )
    assert web.calls, "no requests made"
    ua = ProfilingConfig().user_agent
    assert all(c.headers["user-agent"] == ua for c in web.calls)
    assert web.violations == []


async def test_connection_is_pinned_to_the_validated_ip() -> None:
    """DNS rebinding: the IP that was validated is the IP connected to, with the real Host."""
    web, fetcher = make({"https://acme.com:8443/about": HOME, "http://acme.com/": HOME})
    async with fetcher:
        await fetcher.get("https://acme.com:8443/about", site_domain="acme.com")
        await fetcher.get("http://acme.com/", site_domain="acme.com")

    assert web.calls
    for call in web.calls:
        assert call.url.host == PUBLIC  # connected to the literal validated address
        assert call.headers["host"] in {"acme.com:8443", "acme.com"}
    https = [c for c in web.calls if c.url.scheme == "https"]
    assert https and all(c.url.port == 8443 for c in https)
    # TLS SNI + certificate verification use the original hostname, not the IP.
    assert all(c.extensions["sni_hostname"] == "acme.com" for c in https)
    assert web.violations == []


async def test_rebinding_resolver_cannot_swap_the_address() -> None:
    """A resolver whose answers change between lookups: each connection uses exactly the address
    its own lookup validated (httpx never re-resolves), and a private answer is refused, never
    connected to. Lookup order: robots.txt first (gets the private answer), then the page."""
    answers = iter([["127.0.0.1"], [PUBLIC]])
    web = FakeWeb({"https://acme.com/": HOME}, {"acme.com": [PUBLIC, "127.0.0.1"]})

    async def rebinding(host: str) -> list[str]:
        web.lookups.append(host)
        return next(answers)

    fetcher = SafeFetcher(ProfilingConfig(), transport=web.transport, resolver=rebinding)
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.html == HOME
    assert [(c.url.host, c.headers["host"]) for c in web.calls] == [(PUBLIC, "acme.com")]
    assert len(web.lookups) == 2  # one per connection attempt, never re-resolved by httpx


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
    ],
)
async def test_blocks_private_ip_literal(url: str) -> None:
    web, fetcher = make({})
    err = await blocked(fetcher, url)
    assert err.reason == "private_address"
    assert err.message
    assert web.calls == []
    assert web.lookups == []  # an IP literal is never sent to DNS


async def test_blocks_hostname_resolving_private() -> None:
    web, fetcher = make({"https://acme.com/": HOME}, {"acme.com": ["192.168.1.5"]})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "private_address"
    assert web.calls == []


async def test_blocks_mixed_resolution() -> None:
    web, fetcher = make({"https://acme.com/": HOME}, {"acme.com": [PUBLIC, "10.0.0.1"]})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "private_address"
    assert web.calls == []


@pytest.mark.parametrize(
    "ips",
    [
        ["::ffff:10.0.0.1"],  # IPv4-mapped IPv6 of a private v4
        ["::ffff:127.0.0.1"],
        ["::ffff:169.254.169.254"],
        ["::10.0.0.1"],  # deprecated IPv4-compatible form
        ["fe80::1"],
        ["fc00::1"],
        ["2002:a00:1::"],  # 6to4 of 10.0.0.1
        ["224.0.0.1"],  # multicast (ipaddress calls it "global")
        ["100.64.0.1"],  # carrier-grade NAT
        ["not-an-ip"],
    ],
)
async def test_blocks_non_global_resolved_addresses(ips: list[str]) -> None:
    web, fetcher = make({"https://acme.com/": HOME}, {"acme.com": ips})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "private_address"
    assert web.calls == []


async def test_allows_global_ipv6() -> None:
    web, fetcher = make({"https://acme.com/": HOME}, {"acme.com": ["2606:4700::1111"]})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.html == HOME
    assert {c.url.host for c in web.calls} == {"2606:4700::1111"}


async def test_redirect_offsite_and_private() -> None:
    web, fetcher = make({"https://acme.com/": redirect("https://evil.com/")})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "off_site_redirect"
    assert "evil.com" not in str(web.logical_urls())

    web, fetcher = make({"https://acme.com/": redirect("http://10.0.0.1/")})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "private_address"
    assert all(c.url.host != "10.0.0.1" for c in web.calls)

    # Same registrable domain but resolving to a private address: re-validated on the hop.
    dns = {"acme.com": [PUBLIC], "intranet.acme.com": ["10.9.9.9"]}
    web, fetcher = make({"https://acme.com/": redirect("https://intranet.acme.com/")}, dns)
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "private_address"
    assert all(c.url.host != "10.9.9.9" for c in web.calls)


async def test_redirect_within_site_is_followed() -> None:
    web, fetcher = make(
        {"https://acme.com/": redirect("https://www.acme.com/"), "https://www.acme.com/": HOME}
    )
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.url == "https://www.acme.com/"
    assert result.html == HOME
    assert "https://www.acme.com/robots.txt" in web.logical_urls()  # robots per origin


async def test_redirect_limit() -> None:
    chain: dict[str, Site] = {
        "https://acme.com/": redirect("/1"),
        "https://acme.com/1": redirect("/2", 302),
        "https://acme.com/2": redirect("https://acme.com/3", 307),
        "https://acme.com/3": redirect("/4", 308),
        "https://acme.com/4": HOME,
    }
    web, fetcher = make(chain)
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "too_many_redirects"
    assert "https://acme.com/4" not in web.logical_urls()

    web, fetcher = make(chain)
    async with fetcher:
        result = await fetcher.get("https://acme.com/1", site_domain="acme.com")
    assert result.url == "https://acme.com/4"
    assert result.html == HOME


async def test_scheme() -> None:
    _, fetcher = make({})
    assert (await blocked(fetcher, "ftp://acme.com/")).reason == "scheme"
    _, fetcher = make({})
    assert (await blocked(fetcher, "https://acme.com:99999/")).reason == "scheme"
    _, fetcher = make({})
    assert (await blocked(fetcher, "file:///etc/passwd")).reason == "scheme"
    web, fetcher = make({"https://acme.com/": redirect("file:///etc/passwd")})
    assert (await blocked(fetcher, "https://acme.com/")).reason == "scheme"
    assert len(page_calls(web)) == 1


async def test_robots_disallow_and_missing() -> None:
    web, fetcher = make(
        {
            "https://acme.com/robots.txt": (
                200,
                {"content-type": "text/plain"},
                b"User-agent: *\nDisallow: /\n",
            ),
            "https://acme.com/": HOME,
        }
    )
    async with fetcher:
        assert await fetcher.allowed_by_robots("https://acme.com/") is False
        with pytest.raises(FetchBlocked) as info:
            await fetcher.get("https://acme.com/", site_domain="acme.com")
        with pytest.raises(FetchBlocked):
            await fetcher.get("https://acme.com/about", site_domain="acme.com")
    assert info.value.reason == "robots"
    assert page_calls(web) == []
    assert web.logical_urls().count("https://acme.com/robots.txt") == 1  # cached per site

    web, fetcher = make({"https://acme.com/": HOME})  # robots.txt is a 404
    async with fetcher:
        assert await fetcher.allowed_by_robots("https://acme.com/")
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.html == HOME


async def test_robots_uses_our_agent_token() -> None:
    robots = b"User-agent: *\nAllow: /\n\nUser-agent: SalesFinderBot\nDisallow: /private\n"
    _, fetcher = make({"https://acme.com/robots.txt": (200, {}, robots)})
    async with fetcher:
        assert await fetcher.allowed_by_robots("https://acme.com/about")
        assert not await fetcher.allowed_by_robots("https://acme.com/private/x")


@pytest.mark.parametrize(
    "robots",
    [
        (503, {}, b"down"),
        (403, {}, b"forbidden"),
        Slow(5),
        redirect("http://10.0.0.1/robots.txt"),
        redirect("https://evil.com/robots.txt"),
    ],
)
async def test_unavailable_robots_allows(robots: Site) -> None:
    web, fetcher = make({"https://acme.com/robots.txt": robots}, page_timeout_seconds=0.05)
    async with fetcher:
        assert await fetcher.allowed_by_robots("https://acme.com/")
        assert await fetcher.allowed_by_robots("https://acme.com/x")
    assert web.logical_urls().count("https://acme.com/robots.txt") == 1
    assert all(c.url.host == PUBLIC for c in web.calls)  # the robots redirect isn't followed out


async def test_truncates_large_body() -> None:
    body = b"<html><body>" + b"a" * 3_000_000
    web, fetcher = make({"https://acme.com/": (200, {"content-type": "text/html"}, body)})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.truncated is True
    assert result.html is not None
    assert len(result.html) <= 2_000_000
    assert result.html.startswith("<html><body>aaa")
    assert web.bytes_sent < 2_200_000  # stopped reading, didn't drain the body


async def test_compressed_body_is_bounded_after_decompression() -> None:
    headers = {"content-type": "text/html", "content-encoding": "gzip"}
    small = (200, headers, gzip.compress(HOME.encode()))
    bomb = (200, headers, gzip.compress(b"<html>" + b"a" * 20_000_000))  # ~20 KB on the wire
    _, fetcher = make(
        {"https://acme.com/": small, "https://acme.com/bomb": bomb}, max_page_bytes=10_000
    )
    async with fetcher:
        # Also warms lazy one-off allocations (robots cache, the public-suffix list).
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
        assert (result.html, result.truncated, result.error) == (HOME, False, None)

        tracemalloc.start()
        try:
            result = await fetcher.get("https://acme.com/bomb", site_domain="acme.com")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 2_000_000  # the 20 MB was never inflated in memory
    assert result.truncated is True
    assert result.html is not None
    assert len(result.html) == 10_000
    assert result.html.startswith("<html>aaa")


async def test_non_html_not_parsed() -> None:
    pdf = (200, {"content-type": "application/pdf"}, b"%PDF-1.4 ...")
    _, fetcher = make({"https://acme.com/brochure": pdf})
    async with fetcher:
        result = await fetcher.get("https://acme.com/brochure", site_domain="acme.com")
    assert result.status == 200
    assert result.content_type == "application/pdf"
    assert result.html is None
    assert result.error is None


async def test_xhtml_is_html() -> None:
    xhtml = (200, {"content-type": "application/xhtml+xml"}, HOME.encode())
    _, fetcher = make({"https://acme.com/": xhtml})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.html == HOME


async def test_decodes_declared_charset_else_utf8_with_replacement() -> None:
    latin = (200, {"content-type": "text/html; charset=ISO-8859-1"}, "Café".encode("latin-1"))
    broken = (200, {"content-type": "text/html"}, b"caf\xe9 ok")
    bogus = (200, {"content-type": "text/html; charset=no-such-codec"}, "Café".encode())
    _, fetcher = make(
        {"https://acme.com/a": latin, "https://acme.com/b": broken, "https://acme.com/c": bogus}
    )
    async with fetcher:
        a = await fetcher.get("https://acme.com/a", site_domain="acme.com")
        b = await fetcher.get("https://acme.com/b", site_domain="acme.com")
        c = await fetcher.get("https://acme.com/c", site_domain="acme.com")
    assert a.html == "Café"
    assert b.html == "caf� ok"
    assert c.html == "Café"


@pytest.mark.parametrize(
    ("sites", "url", "check"),
    [
        (
            {"https://acme.com/": (500, {"content-type": "text/html"}, b"boom")},
            "https://acme.com/",
            lambda r: r.error == "HTTP 500" and r.status == 500 and r.html is None,
        ),
        (
            {"https://acme.com/": (403, {}, b"")},
            "https://acme.com/",
            lambda r: r.error == "HTTP 403",
        ),
        ({}, "https://acme.com/missing", lambda r: r.error == "HTTP 404"),
        (
            {"https://acme.com/": Slow(5)},
            "https://acme.com/",
            lambda r: r.error == "Timed out after 0.2s" and r.html is None,
        ),
        (
            {"https://acme.com/": Slow(0.01, drip=True)},  # slow-drip body
            "https://acme.com/",
            lambda r: r.error == "Timed out after 0.2s",
        ),
        ({}, "https://nowhere.example/", lambda r: r.error == "Could not connect"),
    ],
)
async def test_errors_are_results(
    sites: dict[str, Site], url: str, check: Callable[[FetchResult], bool]
) -> None:
    _, fetcher = make(sites, page_timeout_seconds=0.2)
    async with fetcher:
        result = await fetcher.get(url, site_domain="acme.com")
    assert check(result), result


async def test_connection_refused_is_a_result() -> None:
    web = FakeWeb({}, {"acme.com": [PUBLIC]})

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.99"]  # public, but the fake web has no route to it

    fetcher = SafeFetcher(ProfilingConfig(), transport=web.transport, resolver=resolver)
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.error == "Could not connect"
    assert result.status == 0
    assert result.html is None


async def test_tries_each_validated_address() -> None:
    dns = {"acme.com": ["2606:4700::1111", PUBLIC]}
    web = FakeWeb({"https://acme.com/": HOME}, {"acme.com": [PUBLIC]})  # no route over v6

    async def resolver(host: str) -> list[str]:
        return dns[host]

    fetcher = SafeFetcher(ProfilingConfig(), transport=web.transport, resolver=resolver)
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.html == HOME
    assert {c.url.host for c in web.calls} <= {"2606:4700::1111", PUBLIC}


async def test_idn_host_uses_ascii_for_dns_sni_and_host() -> None:
    web, fetcher = make({"https://xn--mnchen-3ya.de/": HOME}, {"xn--mnchen-3ya.de": [PUBLIC]})
    async with fetcher:
        result = await fetcher.get("https://münchen.de/", site_domain="münchen.de")
    assert result.html == HOME
    assert set(web.lookups) == {"xn--mnchen-3ya.de"}
    page = web.calls[-1]
    assert page.headers["host"] == "xn--mnchen-3ya.de"
    assert page.extensions["sni_hostname"] == "xn--mnchen-3ya.de"


BAD_CHARSETS = ["undefined", "idna", "rot13", "base64"]


@pytest.mark.parametrize("charset", BAD_CHARSETS)
async def test_hostile_charset_on_a_page_falls_back_to_utf8(charset: str) -> None:
    page = (200, {"content-type": f"text/html; charset={charset}"}, "Café".encode())
    _, fetcher = make({"https://acme.com/": page})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert (result.html, result.error) == ("Café", None)


@pytest.mark.parametrize("charset", BAD_CHARSETS)
async def test_hostile_charset_on_robots_is_still_parsed(charset: str) -> None:
    robots = (
        200,
        {"content-type": f"text/plain; charset={charset}"},
        b"User-agent: *\nDisallow: /",
    )
    _, fetcher = make({"https://acme.com/robots.txt": robots, "https://acme.com/": HOME})
    err = await blocked(fetcher, "https://acme.com/")
    assert err.reason == "robots"


LONG_LABEL = "a" * 64


@pytest.mark.parametrize(
    "url", [f"https://{LONG_LABEL}.acme.com/", "https://a..acme.com/", f"http://{LONG_LABEL}.com/"]
)
async def test_unencodable_host_is_could_not_connect(url: str) -> None:
    web, fetcher = make({})
    async with fetcher:
        result = await fetcher.get(url, site_domain="acme.com")
    assert result.error == "Could not connect"
    assert web.lookups == []  # rejected before the resolver
    assert web.calls == []


async def test_redirect_to_unencodable_host_is_could_not_connect() -> None:
    _, fetcher = make({"https://acme.com/": redirect(f"https://{LONG_LABEL}.acme.com/")})
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.error == "Could not connect"
    assert result.url == f"https://{LONG_LABEL}.acme.com/"


async def test_resolver_unicode_error_is_could_not_connect() -> None:
    web = FakeWeb({}, {})

    async def resolver(host: str) -> list[str]:
        raise UnicodeError("label too long")

    fetcher = SafeFetcher(ProfilingConfig(), transport=web.transport, resolver=resolver)
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.error == "Could not connect"
    assert web.calls == []


async def test_overall_deadline_covers_robots_and_redirects() -> None:
    """Every hop is within its own page timeout, but four slow robots.txt fetches plus a redirect
    chain would exceed it many times over: get() as a whole stops at 3 x page timeout."""
    hosts = ["acme.com", "www.acme.com", "m.acme.com", "x.acme.com"]
    sites: dict[str, Site] = {f"https://{h}/robots.txt": Slow(5) for h in hosts}
    for here, there in itertools.pairwise(hosts):
        sites[f"https://{here}/"] = redirect(f"https://{there}/")
    sites["https://x.acme.com/"] = HOME
    dns = {h: [PUBLIC] for h in hosts}
    _, fetcher = make(sites, dns, page_timeout_seconds=0.1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with fetcher:
        result = await fetcher.get("https://acme.com/", site_domain="acme.com")
    assert result.error == "Timed out after 0.3s"
    assert result.html is None
    assert loop.time() - started < 1.0


@pytest.mark.parametrize(
    "url", ["https://acme.com:22/", "http://acme.com:6379/", "https://acme.com:1/"]
)
async def test_non_web_ports_are_refused(url: str) -> None:
    web, fetcher = make({})
    err = await blocked(fetcher, url)
    assert (err.reason, err.message) == ("scheme", "Only standard web ports are allowed.")
    assert web.calls == []


async def test_redirect_to_non_web_port_is_refused() -> None:
    _, fetcher = make({"https://acme.com/": redirect("http://acme.com:25/")})
    assert (await blocked(fetcher, "https://acme.com/")).reason == "scheme"


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.com:80/",
        "https://acme.com:443/",
        "http://acme.com:8080/",
        "http://acme.com:443/",
    ],
)
async def test_standard_web_ports_are_allowed(url: str) -> None:
    web, fetcher = make({})
    async with fetcher:
        result = await fetcher.get(url, site_domain="acme.com")
    assert result.error == "HTTP 404"  # reached the (fake) server
    assert web.calls


async def test_userinfo_is_never_sent() -> None:
    web, fetcher = make({"http://acme.com/": HOME, "http://acme.com/next": HOME})
    async with fetcher:
        result = await fetcher.get("http://user:pw@acme.com/", site_domain="acme.com")
    assert result.html == HOME
    assert result.url == "http://acme.com/"
    assert web.calls
    assert all("authorization" not in c.headers for c in web.calls)
    assert all(not c.url.userinfo for c in web.calls)

    web, fetcher = make({"http://acme.com/": redirect("http://admin:secret@acme.com/next")})
    web.sites["http://acme.com/next"] = HOME
    async with fetcher:
        result = await fetcher.get("http://acme.com/", site_domain="acme.com")
    assert result.url == "http://acme.com/next"
    assert all("authorization" not in c.headers for c in web.calls)
