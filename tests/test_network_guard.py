"""The autouse network backstop in conftest: only loopback may be resolved or connected to,
and a swallowed refusal still fails the test (checked here, then cleared)."""

import asyncio
import socket

import httpx
import pytest

from conftest import RealNetworkInTests
from salesfinder.profiling import fetch


def test_getaddrinfo_refuses_a_public_name(_guard_real_network: list[str]) -> None:
    with pytest.raises(RealNetworkInTests):
        socket.getaddrinfo("example.com", 443)
    assert _guard_real_network == ["getaddrinfo 'example.com'"]
    _guard_real_network.clear()


def test_gethostbyname_refuses_a_public_name(_guard_real_network: list[str]) -> None:
    with pytest.raises(RealNetworkInTests):
        socket.gethostbyname("example.com")
    with pytest.raises(RealNetworkInTests):
        socket.gethostbyname_ex("example.com")
    assert _guard_real_network == ["gethostbyname 'example.com'", "gethostbyname_ex 'example.com'"]
    _guard_real_network.clear()
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert _guard_real_network == []


def test_connect_refuses_a_public_address(_guard_real_network: list[str]) -> None:
    with socket.socket() as s, pytest.raises(RealNetworkInTests):
        s.connect(("93.184.215.14", 80))
    with socket.socket() as s, pytest.raises(RealNetworkInTests):
        s.connect_ex(("8.8.8.8", 53))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(RealNetworkInTests):
        s.sendto(b"x", ("8.8.8.8", 53))
    assert len(_guard_real_network) == 3
    _guard_real_network.clear()


async def test_asyncio_and_httpx_are_refused(_guard_real_network: list[str]) -> None:
    with pytest.raises(OSError):
        await asyncio.get_running_loop().getaddrinfo("example.com", 443)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://example.com/")
    assert _guard_real_network
    _guard_real_network.clear()


async def test_profiling_default_resolver_raises(_guard_real_network: list[str]) -> None:
    with pytest.raises(RealNetworkInTests):
        await fetch.default_resolver("example.com")
    _guard_real_network.clear()


def test_loopback_is_allowed(_guard_real_network: list[str]) -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        try:
            socket.getaddrinfo(host, 5432)
        except socket.gaierror:
            pass  # e.g. no IPv6 on this machine: not the guard
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())
    assert _guard_real_network == []
