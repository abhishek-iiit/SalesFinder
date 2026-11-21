"""SMTP submission to a mailbox's own server (never probing; spec §6.2.1).

The send is done in steps so the outcome tells us whether the message may have been accepted:
anything that fails before DATA is safe to retry; anything unclear during DATA is ``ambiguous``
and is never resent automatically. Server text is cleaned and capped, and never logged.
"""

import ssl
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal, Protocol

import aiosmtplib
import structlog
from pydantic import SecretStr

log = structlog.get_logger(__name__)

MAX_DETAIL = 200
Outcome = Literal["sent", "retry", "rejected", "mailbox_error", "failed", "ambiguous"]


def clean_detail(text: object, limit: int = MAX_DETAIL) -> str:
    """One line, no control characters, at most ``limit`` characters."""
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in str(text)
        if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )
    return " ".join(kept.split())[:limit]


@dataclass(frozen=True)
class MailboxCredentials:
    address: str
    username: str
    password: SecretStr
    smtp_host: str
    smtp_port: int
    smtp_security: Literal["ssl", "starttls"]
    imap_host: str
    imap_port: int
    sent_folder: str | None
    timeout: float


@dataclass(frozen=True)
class SendResult:
    outcome: Outcome
    code: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", clean_detail(self.detail))


class MailSender(Protocol):
    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult: ...

    async def check(self, creds: MailboxCredentials) -> str | None:
        """Login + NOOP + QUIT. None means OK, otherwise a short reason for the admin."""
        ...


def _code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _detail(exc: BaseException) -> str:
    return clean_detail(getattr(exc, "message", None) or type(exc).__name__)


class AioSmtpSender:
    def __init__(self, *, tls_context: ssl.SSLContext | None = None) -> None:
        self._tls_context = tls_context  # tests only: trust a local self-signed certificate

    def _client(self, creds: MailboxCredentials) -> aiosmtplib.SMTP:
        kwargs: dict[str, object] = {}
        # EHLO names the mailbox's domain, never this machine's host name.
        domain = creds.address.rpartition("@")[2].strip().lower()
        if domain:
            kwargs["local_hostname"] = domain
        if self._tls_context is not None:
            kwargs["tls_context"] = self._tls_context
        return aiosmtplib.SMTP(
            hostname=creds.smtp_host,
            port=creds.smtp_port,
            use_tls=creds.smtp_security == "ssl",
            start_tls=False,
            timeout=creds.timeout,
            **kwargs,  # type: ignore[arg-type]
        )

    async def _open(self, client: aiosmtplib.SMTP, creds: MailboxCredentials) -> None:
        await client.connect()
        if creds.smtp_security == "starttls":
            await client.starttls()
        await client.login(creds.username, creds.password.get_secret_value())

    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult:
        try:
            payload = message.as_bytes()
        except Exception as exc:
            return SendResult(
                "failed", None, f"Could not encode the message ({type(exc).__name__})"
            )
        client = self._client(creds)
        try:
            try:
                await self._open(client, creds)
                await client.mail(envelope_from)
                await client.rcpt(envelope_to)
            except aiosmtplib.SMTPAuthenticationError as exc:
                return SendResult("mailbox_error", _code(exc), _detail(exc))
            except aiosmtplib.SMTPSenderRefused as exc:
                code = _code(exc) or 0
                return SendResult("mailbox_error" if code >= 500 else "retry", code, _detail(exc))
            except aiosmtplib.SMTPRecipientRefused as exc:
                code = _code(exc) or 0
                return SendResult("rejected" if code >= 500 else "retry", code, _detail(exc))
            except Exception as exc:  # nothing was sent yet: safe to retry
                return SendResult("retry", _code(exc), _detail(exc))

            try:
                await client.data(payload)
            except aiosmtplib.SMTPDataError as exc:
                code = _code(exc) or 0
                if code >= 500:
                    return SendResult("failed", code, _detail(exc))
                if 400 <= code < 500:
                    return SendResult("retry", code, _detail(exc))
                return SendResult("ambiguous", code or None, _detail(exc))
            except Exception as exc:  # timeout, dropped connection, anything else mid-DATA
                return SendResult("ambiguous", _code(exc), _detail(exc))
            try:
                await client.quit()
            except Exception:
                log.debug("smtp_quit_failed")
            return SendResult("sent", 250, "OK")
        finally:
            try:
                client.close()
            except Exception:
                log.debug("smtp_close_failed")

    async def check(self, creds: MailboxCredentials) -> str | None:
        client = self._client(creds)
        try:
            await self._open(client, creds)
            await client.noop()
            await client.quit()
        except aiosmtplib.SMTPAuthenticationError as exc:
            return f"The server rejected the login (code {_code(exc)})."
        except (TimeoutError, aiosmtplib.SMTPTimeoutError):
            return "The server timed out."
        except aiosmtplib.SMTPResponseException as exc:
            return f"The server answered with an error (code {_code(exc)})."
        except ssl.SSLError:
            return "The secure connection could not be set up."
        except (OSError, aiosmtplib.SMTPException):
            return "Couldn't connect to the server."
        finally:
            try:
                client.close()
            except Exception:
                log.debug("smtp_close_failed")
        return None
