"""LAN-mode configuration warnings and the mode line (doctor ``network`` check and Settings)."""

import os
from pathlib import Path

import pytest

from salesfinder import hosts
from salesfinder.ops import network
from salesfinder.settings import Settings
from salesfinder.web import security

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [v for v in os.environ if v.startswith("SALESFINDER_")]:
        monkeypatch.delenv(var)


def _tls(tmp_path: Path) -> dict[str, Path]:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    return {"tls_certfile": cert, "tls_keyfile": key}


def _loopback(**kw: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://x/y",
        secret_key=KEY,
        **kw,  # type: ignore[arg-type]
    )


def _lan(tmp_path: Path, **kw: object) -> Settings:
    return _loopback(bind_host="0.0.0.0", **_tls(tmp_path), **kw)  # noqa: S104


GOOD_HOSTS = ["salesfinder.local", "192.168.1.20", "127.0.0.1", "localhost"]
GOOD_URL = "https://salesfinder.local:5555"


def test_clean_lan_config_has_no_warnings(tmp_path: Path) -> None:
    settings = _lan(tmp_path, allowed_hosts=GOOD_HOSTS, public_url=GOOD_URL)
    assert network.lan_warnings(settings) == []


def test_default_loopback_config_has_no_warnings() -> None:
    assert network.lan_warnings(_loopback()) == []


def test_lan_with_only_loopback_hosts(tmp_path: Path) -> None:
    settings = _lan(tmp_path, public_url="https://127.0.0.1:5555")
    assert network.lan_warnings(settings) == [network.LOOPBACK_ONLY]


def test_loopback_only_hosts_are_fine_on_loopback() -> None:
    assert network.LOOPBACK_ONLY not in network.lan_warnings(_loopback())


def test_wildcard_host(tmp_path: Path) -> None:
    settings = _lan(tmp_path, allowed_hosts=["*"], public_url=GOOD_URL)
    assert network.lan_warnings(settings) == [network.WILDCARD]
    # Flagged on loopback too: the Host check is off either way.
    assert network.WILDCARD in network.lan_warnings(_loopback(allowed_hosts=["*"]))


def test_lan_without_public_url(tmp_path: Path) -> None:
    settings = _lan(tmp_path, allowed_hosts=GOOD_HOSTS)
    assert network.lan_warnings(settings) == [network.NO_PUBLIC_URL]
    # On loopback the request's own address is fine.
    assert network.lan_warnings(_loopback()) == []


def test_public_url_host_not_allowed(tmp_path: Path) -> None:
    settings = _lan(tmp_path, allowed_hosts=["192.168.1.20", "localhost"], public_url=GOOD_URL)
    assert network.lan_warnings(settings) == [
        network.PUBLIC_URL_HOST.format(host="salesfinder.local")
    ]
    assert network.lan_warnings(settings)[0] == (
        "SALESFINDER_PUBLIC_URL's host salesfinder.local isn't in SALESFINDER_ALLOWED_HOSTS"
        " — invite links would be refused."
    )


def test_public_url_host_matches_a_suffix_pattern(tmp_path: Path) -> None:
    settings = _lan(
        tmp_path, allowed_hosts=["*.elchemy.lan", "127.0.0.1"], public_url="https://sf.elchemy.lan"
    )
    assert network.lan_warnings(settings) == []


def test_public_url_host_is_case_insensitive(tmp_path: Path) -> None:
    settings = _lan(
        tmp_path, allowed_hosts=["SalesFinder.Local"], public_url="https://salesfinder.local"
    )
    assert network.lan_warnings(settings) == []


def test_public_url_host_checked_on_loopback_too() -> None:
    settings = _loopback(public_url="http://salesfinder.local:5555")
    assert network.lan_warnings(settings) == [
        network.PUBLIC_URL_HOST.format(host="salesfinder.local")
    ]


def test_wildcard_skips_the_public_url_host_rule(tmp_path: Path) -> None:
    settings = _lan(tmp_path, allowed_hosts=["*"], public_url="https://elsewhere.local")
    assert network.lan_warnings(settings) == [network.WILDCARD]


def test_several_problems_are_all_listed(tmp_path: Path) -> None:
    settings = _lan(tmp_path)
    assert network.lan_warnings(settings) == [network.LOOPBACK_ONLY, network.NO_PUBLIC_URL]


def test_warning_copy() -> None:
    assert network.LOOPBACK_ONLY == (
        "LAN mode is on but SALESFINDER_ALLOWED_HOSTS lists only loopback names — teammates'"
        ' browsers will be refused. Add the names they use, e.g. ["salesfinder.local",'
        ' "192.168.1.20"].'
    )
    assert network.WILDCARD == (
        'SALESFINDER_ALLOWED_HOSTS contains "*" — any Host header is accepted, so DNS-rebinding'
        " protection is off."
    )
    assert network.NO_PUBLIC_URL == (
        "LAN mode is on but SALESFINDER_PUBLIC_URL isn't set — invite links will use whatever"
        " address the admin browsed from."
    )


def test_mode_line(tmp_path: Path) -> None:
    assert network.mode_line(_loopback()) == "Loopback only (127.0.0.1)"
    lan = _lan(tmp_path, port=5555)
    assert network.mode_line(lan) == "LAN over HTTPS on 0.0.0.0:5555"


def test_ipv6_public_url_host_matches_the_bracketed_form(tmp_path: Path) -> None:
    """TrustedHost compares the Host header's host with its brackets (``[fe80::1]``)."""
    url = "https://[fe80::1]:5555"
    ok = _lan(tmp_path, allowed_hosts=["[fe80::1]", "127.0.0.1"], public_url=url)
    assert network.lan_warnings(ok) == []
    bare = _lan(tmp_path, allowed_hosts=["fe80::1", "127.0.0.1"], public_url=url)
    assert network.lan_warnings(bare) == [network.PUBLIC_URL_HOST.format(host="[fe80::1]")]


@pytest.mark.parametrize(
    "hosts",
    [
        ["[::1]"],
        ["::1", "127.0.0.1"],
        ["127.0.0.2", "LOCALHOST"],
        ["127.1.2.3"],
        [],
    ],
)
def test_loopback_only_covers_every_loopback_spelling(tmp_path: Path, hosts: list[str]) -> None:
    settings = _lan(tmp_path, allowed_hosts=hosts, public_url=GOOD_URL)
    assert network.LOOPBACK_ONLY in network.lan_warnings(settings)


def test_lan_hosts_that_only_look_like_loopback_are_not(tmp_path: Path) -> None:
    for host in ("localhost.evil", "128.0.0.1", "[fe80::1]"):
        settings = _lan(tmp_path, allowed_hosts=[host, "127.0.0.1"], public_url=GOOD_URL)
        assert network.LOOPBACK_ONLY not in network.lan_warnings(settings), host


def test_host_allowed_is_shared_and_brackets_ipv6() -> None:
    assert security.host_allowed is hosts.host_allowed
    assert hosts.host_allowed("fe80::1", ["[fe80::1]"])
    assert hosts.host_allowed("[FE80::1]", ["[fe80::1]"])
    assert not hosts.host_allowed("fe80::1", ["fe80::1"])
    assert hosts.host_allowed("Sales.Elchemy.lan", ["*.elchemy.lan"])
    assert not hosts.host_allowed(None, ["*"])
