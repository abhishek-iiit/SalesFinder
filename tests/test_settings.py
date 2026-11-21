import os

import pytest
from pydantic import SecretStr, ValidationError

from salesfinder.settings import Settings

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [v for v in os.environ if v.startswith("SALESFINDER_")]:
        monkeypatch.delenv(var)


def test_loopback_without_tls_is_fine():
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert s.lan_mode is False and s.https is False


def test_lan_bind_without_tls_refuses():
    with pytest.raises(ValidationError) as e:
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://x/y",
            secret_key=KEY,
            bind_host="192.168.1.20",
        )
    assert "SALESFINDER_TLS_CERTFILE" in str(e.value) and "SALESFINDER_TLS_KEYFILE" in str(e.value)


def test_lan_bind_with_tls_ok(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    s = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x/y",
        secret_key=KEY,
        bind_host="0.0.0.0",  # noqa: S104
        tls_certfile=cert,
        tls_keyfile=key,
    )
    assert s.lan_mode and s.https


def test_short_secret_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key="short")  # noqa: S106


def test_empty_secret_has_clear_message():
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key="")
    assert "SALESFINDER_SECRET_KEY" in str(e.value) and "32" in str(e.value)


def test_defaults_and_env(monkeypatch):
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", KEY)
    monkeypatch.setenv("SALESFINDER_PORT", "6000")
    s = Settings(
        _env_file=None,
    )
    assert s.port == 6000 and s.bind_host == "127.0.0.1" and s.claude_bin == "claude"
    assert s.session_ttl_hours == 12 and s.allowed_hosts == ["127.0.0.1", "localhost"]


def test_hubspot_token_empty_is_none_and_secret_hidden():
    base = {"_env_file": None, "database_url": "postgresql+asyncpg://x/y", "secret_key": KEY}
    s = Settings(**base, hubspot_token="", serper_api_key="")
    assert s.hubspot_token is None and s.serper_api_key is None
    s = Settings(
        **base,
        hubspot_token="pat-na1-supersecret",  # noqa: S106
        serper_api_key="serper-secret",
    )
    assert isinstance(s.hubspot_token, SecretStr)
    assert "supersecret" not in repr(s) and "serper-secret" not in repr(s)
    assert s.hubspot_token.get_secret_value() == "pat-na1-supersecret"


def test_email_verifier_key_empty_is_none_and_hidden():
    base = {"_env_file": None, "database_url": "postgresql+asyncpg://x/y", "secret_key": KEY}
    assert Settings(**base).email_verifier_key is None
    assert Settings(**base, email_verifier_key="  ").email_verifier_key is None
    s = Settings(**base, email_verifier_key="mv-secret-key")
    assert isinstance(s.email_verifier_key, SecretStr)
    assert "mv-secret-key" not in repr(s)
    assert s.email_verifier_key.get_secret_value() == "mv-secret-key"


def test_mailbox_passwords_parse_and_stay_hidden(monkeypatch):
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", '{"sales":"abcd efgh","empty":"  "}')
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert list(s.mailbox_passwords) == ["sales"]
    assert s.mailbox_passwords["sales"].get_secret_value() == "abcd efgh"
    assert "abcd" not in repr(s) and "efgh" not in repr(s) and "abcd" not in str(s)


def test_mailbox_passwords_default_empty():
    s = Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert s.mailbox_passwords == {}


def test_mailbox_passwords_non_string_value_is_rejected_without_echo(monkeypatch):
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", '{"sales": 987654321}')
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert "987654321" not in str(e.value)


def test_mailbox_passwords_malformed_json_does_not_echo(monkeypatch):
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", '{"sales": "hunter2secret"')
    with pytest.raises(Exception) as e:
        Settings(_env_file=None, database_url="postgresql+asyncpg://x/y", secret_key=KEY)
    assert "hunter2secret" not in str(e.value)


# --------------------------------------------------------------- final fix wave (P6-U)

BASE = {"database_url": "postgresql+asyncpg://x/y", "secret_key": KEY}


def _names(s: Settings) -> dict[str, str]:
    return {k: v.get_secret_value() for k, v in s.mailbox_passwords.items()}


def test_mailbox_passwords_come_from_one_source_never_merged(monkeypatch, tmp_path):
    """Ruling P6-U: the environment replaces .env's value as a whole (no deep merge), so the
    passwords in use are exactly one source's: a key in .env only is not added in."""
    env_file = tmp_path / ".env"
    env_file.write_text('SALESFINDER_MAILBOX_PASSWORDS={"old": "x", "sales": "y"}\n', "utf-8")
    assert _names(Settings(_env_file=env_file, **BASE)) == {"old": "x", "sales": "y"}

    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", '{"sales": "z"}')
    assert _names(Settings(_env_file=env_file, **BASE)) == {"sales": "z"}


def test_mailbox_passwords_given_explicitly_replace_the_environment(monkeypatch):
    """What tests (and any caller) pass wins outright: an exported value isn't merged in."""
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", '{"leaked": "from-shell"}')
    assert Settings(_env_file=None, **BASE, mailbox_passwords={}).mailbox_passwords == {}
    given = Settings(_env_file=None, **BASE, mailbox_passwords={"sales": "pw"})
    assert _names(given) == {"sales": "pw"}
    # Even junk in the environment can't break explicit settings.
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", "not json at all")
    assert Settings(_env_file=None, **BASE, mailbox_passwords={}).mailbox_passwords == {}


@pytest.mark.parametrize("raw", ['["hunter2secret"]', '"hunter2secret"', "hunter2secret"])
def test_mailbox_passwords_must_be_a_json_object_and_are_never_echoed(monkeypatch, raw):
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", raw)
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, **BASE)
    assert "hunter2secret" not in str(e.value)
    assert "SALESFINDER_MAILBOX_PASSWORDS" in str(e.value)


# --------------------------------------------------------------------------- public URL


def _tls_files(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    return {"tls_certfile": cert, "tls_keyfile": key}


def test_public_url_defaults_to_none():
    assert Settings(_env_file=None, **BASE).public_url is None


def test_public_url_empty_is_none(monkeypatch):
    monkeypatch.setenv("SALESFINDER_PUBLIC_URL", "  ")
    assert Settings(_env_file=None, **BASE).public_url is None


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("https://salesfinder.local:5555/", "https://salesfinder.local:5555"),
        ("https://salesfinder.local:5555", "https://salesfinder.local:5555"),
        ("http://127.0.0.1:5555", "http://127.0.0.1:5555"),
        (" HTTPS://192.168.1.20 ", "https://192.168.1.20"),
    ],
)
def test_public_url_stored_without_trailing_slash(raw, stored):
    assert Settings(_env_file=None, **BASE, public_url=raw).public_url == stored


@pytest.mark.parametrize(
    "raw",
    [
        "https://salesfinder.local/app",
        "https://salesfinder.local/?next=x",
        "https://salesfinder.local#frag",
        "https://user:pw@salesfinder.local",
        "https://user@salesfinder.local",
        "ftp://salesfinder.local",
        "salesfinder.local:5555",
        "https://",
        "https://:5555",
        "https://salesfinder.local:99999",
        "https://sales finder.local",
        "https://salesfinder.local:abc",
        "javascript:alert(1)",
    ],
)
def test_public_url_rejects_anything_but_scheme_host_port(raw):
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, **BASE, public_url=raw)
    message = str(e.value)
    assert "SALESFINDER_PUBLIC_URL must look like https://host[:port]" in message
    assert KEY not in message


def test_public_url_http_refused_in_lan_mode(tmp_path):
    with pytest.raises(ValidationError) as e:
        Settings(
            _env_file=None,
            **BASE,
            bind_host="0.0.0.0",  # noqa: S104
            public_url="http://salesfinder.local:5555",
            **_tls_files(tmp_path),
        )
    message = str(e.value)
    assert "SALESFINDER_PUBLIC_URL must use https in LAN mode." in message
    assert KEY not in message


def test_public_url_https_accepted_in_lan_mode(tmp_path):
    s = Settings(
        _env_file=None,
        **BASE,
        bind_host="0.0.0.0",  # noqa: S104
        public_url="https://salesfinder.local:5555/",
        **_tls_files(tmp_path),
    )
    assert s.public_url == "https://salesfinder.local:5555"


def test_public_url_from_env_errors_never_echo_the_secret_key(monkeypatch):
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", KEY)
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql+asyncpg://x/y")
    monkeypatch.setenv("SALESFINDER_PUBLIC_URL", f"https://salesfinder.local/{KEY}")
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None)
    assert KEY not in str(e.value)
    assert "SALESFINDER_PUBLIC_URL" in str(e.value)


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("https://SalesFinder.Local:05555/", "https://salesfinder.local:5555"),
        ("https://[FE80::0001]:5555", "https://[fe80::1]:5555"),
        ("https://sales-finder.elchemy.lan", "https://sales-finder.elchemy.lan"),
        ("https://10.0.0.5:443", "https://10.0.0.5:443"),
        # Final wave T4: underscores inside labels; 63-character labels; a digit-led last label
        # that isn't all digits.
        ("https://sales_finder.local", "https://sales_finder.local"),
        ("https://Mac_Mini.local", "https://mac_mini.local"),
        ("https://" + "a" * 63 + ".local", "https://" + "a" * 63 + ".local"),
        ("https://host.1a", "https://host.1a"),
    ],
)
def test_public_url_is_normalised(raw, stored):
    assert Settings(_env_file=None, **BASE, public_url=raw).public_url == stored


@pytest.mark.parametrize(
    "raw",
    [
        "https://-sales.local",
        "https://sales-.local",
        "https://sales..local",
        "https://.sales.local",
        "https://sales.local.",
        "https://_sales.local",
        "https://sales_.local",
        "https://999.1.1.1",
        # Final wave T4: hosts a browser wouldn't use as written.
        "https://salesfinder.123",  # a numeric last label: browsers read it as an IPv4 address
        "https://a.b.0x1f",
        "https://sales.0X1F",
        "https://0x7f.0.0.1",
        "https://10.0.0.010",  # leading zeros
        "https://[fe80::1%25en0]",  # an IPv6 zone ID
        "https://[fe80::1%en0]",
        "https://" + "a" * 64 + ".local",  # a label over 63 characters
        "https://10.0.0",
        "https://[zz::1]",
        "https://[fe80::1",
        "https://fe80::1",
        "https://salesfinder.local:",
        "https://salesfinder.local:0",
        "https://salesfinder.local:65536",
        "https://salesfinder.local:123456",
    ],
)
def test_public_url_host_and_port_must_be_valid(raw):
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, **BASE, public_url=raw)
    message = str(e.value)
    assert "SALESFINDER_PUBLIC_URL must look like https://host[:port]" in message
    assert raw not in message
    assert KEY not in message


def test_allowed_hosts_are_lower_cased(monkeypatch):
    monkeypatch.setenv("SALESFINDER_ALLOWED_HOSTS", '[" SalesFinder.Local ", "127.0.0.1"]')
    s = Settings(_env_file=None, **BASE)
    assert s.allowed_hosts == ["salesfinder.local", "127.0.0.1"]


def test_log_file_defaults_to_stderr_and_reads_the_env(monkeypatch, tmp_path):
    base = {"_env_file": None, "database_url": "postgresql+asyncpg://x/y", "secret_key": KEY}
    assert Settings(**base).log_file is None
    assert Settings(**base, log_file="  ").log_file is None
    monkeypatch.setenv("SALESFINDER_LOG_FILE", str(tmp_path / "worker.log"))
    assert Settings(**base).log_file == tmp_path / "worker.log"
