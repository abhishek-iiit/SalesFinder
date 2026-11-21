"""Environment-driven settings (pydantic-settings) with the LAN-requires-TLS rule."""

import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_SECRET_LENGTH = 32
PUBLIC_URL_SHAPE = "SALESFINDER_PUBLIC_URL must look like https://host[:port]"
PUBLIC_URL_NOT_HTTPS = "SALESFINDER_PUBLIC_URL must use https in LAN mode."
# ``host[:port]``: the host is bracketed (IPv6) or has no colon; the port is digits.
_NETLOC_RE = re.compile(r"(?P<host>\[[^\[\]]+\]|[^:\[\]]+)(?::(?P<port>[0-9]{1,5}))?")
# One host label: 1-63 letters and digits, with hyphens and underscores (local names such as
# "mac_mini.local") inside.
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?")
# A last label browsers read as part of an IPv4 address (all digits, or hexadecimal).
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _clean_host(host: str) -> str | None:
    """The host in canonical form (lower-case; IP addresses as ``ipaddress`` prints them,
    IPv6 bracketed), or None if it isn't a host name or IP address a browser uses as
    written: a numeric or ``0x…`` last label (browsers parse those as IPv4, so only a whole
    dotted-decimal IPv4 address may end that way), an IPv6 zone ID, or a label over 63
    characters."""
    if host.startswith("["):
        if "%" in host:
            return None  # a zone ID (fe80::1%en0): browsers don't take one in a URL
        try:
            return f"[{ipaddress.IPv6Address(host[1:-1])}]"
        except ValueError:
            return None
    host = host.lower()
    labels = host.split(".")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if _NUMERIC_LABEL_RE.fullmatch(labels[-1]):
        # Looks like an IPv4 address: it must be one.
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            return None
    return host


def _clean_public_url(raw: str) -> str:
    """``scheme://host[:port]`` (lower-case scheme and host, the port as a number, no trailing
    slash), else ValueError with fixed copy (the value is never echoed)."""
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ValueError(PUBLIC_URL_SHAPE) from None
    scheme = parts.scheme.lower()
    netloc = _NETLOC_RE.fullmatch(parts.netloc)
    if (
        scheme not in ("http", "https")
        or netloc is None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "?" in raw
        or "#" in raw
    ):
        raise ValueError(PUBLIC_URL_SHAPE)
    host = _clean_host(netloc["host"])
    port = int(netloc["port"]) if netloc["port"] is not None else None
    if host is None or (port is not None and not 1 <= port <= 65535):
        raise ValueError(PUBLIC_URL_SHAPE)
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SALESFINDER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must never echo env values (they can be secrets).
        hide_input_in_errors=True,
    )

    database_url: str
    secret_key: SecretStr
    bind_host: str = "127.0.0.1"
    port: int = 5555
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    config_dir: Path = Path("config")
    log_level: str = "INFO"
    log_json: bool = False
    session_ttl_hours: int = 12
    claude_bin: str = "claude"
    hubspot_token: SecretStr | None = None
    serper_api_key: SecretStr | None = None
    email_verifier_key: SecretStr | None = None
    # {"<secret_name>": "<app password>"}; the DB stores only the secret name. Read as a JSON
    # string and parsed below (``NoDecode``), so one source replaces another as a whole
    # (Ruling P6-U): pydantic-settings would otherwise deep-merge a dict from the shell
    # environment into .env's, and a password revoked in one would live on through the other.
    mailbox_passwords: Annotated[dict[str, SecretStr], NoDecode] = {}
    # The address teammates use (``https://host[:port]``), for invite links; https in LAN mode.
    public_url: str | None = None
    # Write logs to this file (reopened after newsyslog rotates it) instead of stderr; the
    # launchd plists set it (Ruling P7-F). Not a secret.
    log_file: Path | None = None

    @field_validator("allowed_hosts", mode="after")
    @classmethod
    def _hosts_lower_case(cls, v: list[str]) -> list[str]:
        # TrustedHost lower-cases both the Host header and its patterns (Starlette 1.10); we
        # strip and lower-case them here too, so the LAN warnings and the Origin check (which
        # read these values) match what TrustedHost does.
        return [host.strip().lower() for host in v]

    @field_validator("public_url", mode="before")
    @classmethod
    def _public_url_shape(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            return _clean_public_url(stripped) if stripped else None
        return v

    @field_validator("log_file", mode="before")
    @classmethod
    def _empty_log_file_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("hubspot_token", "serper_api_key", "email_verifier_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("mailbox_passwords", mode="before")
    @classmethod
    def _passwords_are_strings(cls, v: object) -> object:
        # Never echo a value: a rejected one may be a secret.
        if isinstance(v, str):
            try:
                v = json.loads(v) if v.strip() else {}
            except ValueError:
                raise ValueError(
                    "SALESFINDER_MAILBOX_PASSWORDS must be a JSON object "
                    '{"<name>": "<app password>"}'
                ) from None
            if not isinstance(v, dict):
                raise ValueError(
                    "SALESFINDER_MAILBOX_PASSWORDS must be a JSON object "
                    '{"<name>": "<app password>"}'
                )
        if isinstance(v, dict) and not all(isinstance(x, str) for x in v.values()):
            raise ValueError("mailbox passwords must all be strings")
        return v

    @field_validator("mailbox_passwords", mode="after")
    @classmethod
    def _drop_empty_passwords(cls, v: dict[str, SecretStr]) -> dict[str, SecretStr]:
        return {name: pw for name, pw in v.items() if pw.get_secret_value().strip()}

    @field_validator("secret_key")
    @classmethod
    def _secret_long_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"SALESFINDER_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters "
                '(empty or too short). Generate one with: python -c "import secrets;'
                'print(secrets.token_urlsafe(48))"'
            )
        return v

    @property
    def lan_mode(self) -> bool:
        return self.bind_host not in LOOPBACK_HOSTS

    @property
    def https(self) -> bool:
        return self.tls_certfile is not None and self.tls_keyfile is not None

    @model_validator(mode="after")
    def _lan_requires_https(self) -> Self:
        if self.lan_mode and not self.https:
            raise ValueError(
                "LAN mode requires HTTPS: set SALESFINDER_TLS_CERTFILE and SALESFINDER_TLS_KEYFILE"
            )
        if self.lan_mode and self.public_url and not self.public_url.startswith("https://"):
            raise ValueError(PUBLIC_URL_NOT_HTTPS)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
