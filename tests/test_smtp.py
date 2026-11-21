"""The only test module that opens a socket, and only to 127.0.0.1 (an aiosmtpd controller on a
free port, stopped in teardown). Implicit TLS uses a throwaway self-signed certificate made by the
``openssl`` CLI; if it isn't installed the TLS tests are skipped."""

import asyncio
import shutil
import socket
import ssl
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult
from pydantic import SecretStr

from salesfinder.mail.mime import build_message
from salesfinder.mail.smtp import AioSmtpSender, MailboxCredentials, SendResult, clean_detail

pytestmark = pytest.mark.filterwarnings("ignore:Requiring AUTH:UserWarning")

_REAL_SEND = AioSmtpSender.send  # captured before the autouse guard patches it
_REAL_CHECK = AioSmtpSender.check

USER, PASSWORD = "sales@send-domain.com", "app-password"
BAD_SECRET = "not-the-password"  # noqa: S105


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(autouse=True)
def _real_smtp_client(_guard_real_mail: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client, for loopback servers only: anything else fails before connecting."""

    async def send(
        self: AioSmtpSender, creds: MailboxCredentials, *args: Any, **kwargs: Any
    ) -> SendResult:
        assert creds.smtp_host in LOOPBACK, "test_smtp only talks to 127.0.0.1"
        return await _REAL_SEND(self, creds, *args, **kwargs)

    async def check(self: AioSmtpSender, creds: MailboxCredentials) -> str | None:
        assert creds.smtp_host in LOOPBACK, "test_smtp only talks to 127.0.0.1"
        return await _REAL_CHECK(self, creds)

    monkeypatch.setattr(AioSmtpSender, "send", send)
    monkeypatch.setattr(AioSmtpSender, "check", check)


async def test_real_client_refuses_a_non_loopback_host(server: "Server") -> None:
    creds = replace(server.creds(), smtp_host="smtp.example.com")
    with pytest.raises(AssertionError, match=r"only talks to 127\.0\.0\.1"):
        await server.sender().check(creds)


class Handler:
    def __init__(self) -> None:
        self.rcpt_reply = "250 OK"
        self.data_reply: str | None = None  # None: accept
        self.drop_in_data = False
        self.stall_in_data = False
        self.received: list[bytes] = []
        self.auth_ok = True
        self.ehlo_names: list[str] = []

    async def handle_EHLO(
        self, server: Any, session: Any, envelope: Any, hostname: str, responses: list[str]
    ) -> list[str]:
        self.ehlo_names.append(hostname)
        session.host_name = hostname  # the new-style hook must do what aiosmtpd otherwise does
        return responses

    async def handle_RCPT(
        self, server: Any, session: Any, envelope: Any, address: str, opts: Any
    ) -> str:
        if self.rcpt_reply.startswith("250"):
            envelope.rcpt_tos.append(address)
        return self.rcpt_reply

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        if self.drop_in_data:
            server.transport.abort()
            return "250 never delivered"
        if self.stall_in_data:
            await asyncio.sleep(5)
        if self.data_reply:
            return self.data_reply
        self.received.append(envelope.content)
        return "250 2.0.0 OK queued"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not available")
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    openssl = shutil.which("openssl")
    assert openssl
    args = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
    args += ["-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1"]
    args += ["-addext", "subjectAltName=IP:127.0.0.1"]
    subprocess.run(args, check=True, capture_output=True)  # noqa: S603
    return cert, key


class Server:
    def __init__(self, handler: Handler, controller: Controller, port: int, cert: Path | None):
        self.handler, self.controller, self.port, self.cert = handler, controller, port, cert

    def creds(
        self, security: Literal["ssl", "starttls"] = "ssl", secret: str = PASSWORD
    ) -> MailboxCredentials:
        return MailboxCredentials(
            address=USER, username=USER, password=SecretStr(secret),
            smtp_host="127.0.0.1", smtp_port=self.port, smtp_security=security,
            imap_host="127.0.0.1", imap_port=993, sent_folder=None, timeout=2,
        )  # fmt: skip

    def sender(self) -> AioSmtpSender:
        assert self.cert is not None
        return AioSmtpSender(tls_context=ssl.create_default_context(cafile=str(self.cert)))


def authenticator(
    server: Any, session: Any, envelope: Any, mechanism: str, auth_data: Any
) -> AuthResult:
    ok = auth_data.login == USER.encode() and auth_data.password == PASSWORD.encode()
    return AuthResult(success=ok, handled=False)


def start(handler: Handler, tls: tuple[Path, Path], *, implicit: bool) -> Server:
    cert, key = tls
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    port = free_port()
    kwargs: dict[str, Any] = (
        {"ssl_context": server_ctx} if implicit else {"tls_context": server_ctx}
    )
    controller = Controller(
        handler, hostname="127.0.0.1", port=port, authenticator=authenticator,
        auth_required=True, auth_require_tls=False, ready_timeout=10, **kwargs,
    )  # fmt: skip
    controller.start()
    return Server(handler, controller, port, cert)


@pytest.fixture
def server(tls_files: tuple[Path, Path]) -> Iterator[Server]:
    srv = start(Handler(), tls_files, implicit=True)
    try:
        yield srv
    finally:
        srv.controller.stop()


@pytest.fixture
def starttls_server(tls_files: tuple[Path, Path]) -> Iterator[Server]:
    srv = start(Handler(), tls_files, implicit=False)
    try:
        yield srv
    finally:
        srv.controller.stop()


def message() -> Any:
    return build_message(
        from_name="Jordan Rivera", from_address=USER, to_address="jane.doe@acme-soap.com",
        subject="Sourcing glycerin", body="Hi Jane,\n\n.A line starting with a dot.\nBye\n",
        message_id="<t1@send-domain.com>", date=datetime(2026, 11, 10, tzinfo=UTC),
        in_reply_to=None, references=[], unsubscribe_mailto="unsub@send-domain.com",
    )  # fmt: skip


async def send(
    srv: Server, security: Literal["ssl", "starttls"] = "ssl", secret: str = PASSWORD
) -> SendResult:
    return await srv.sender().send(
        srv.creds(security, secret), message(),
        envelope_from=USER, envelope_to="jane.doe@acme-soap.com",
    )  # fmt: skip


async def test_accepted_message_is_sent_with_exact_bytes(server: Server) -> None:
    result = await send(server)
    assert result.outcome == "sent" and result.code == 250
    assert server.handler.received == [message().as_bytes()]


async def test_starttls_is_sent(starttls_server: Server) -> None:
    result = await send(starttls_server, "starttls")
    assert result.outcome == "sent"
    assert len(starttls_server.handler.received) == 1


async def test_check_ok_and_bad_login(server: Server) -> None:
    assert await server.sender().check(server.creds()) is None
    reason = await server.sender().check(server.creds(secret=BAD_SECRET))
    assert reason is not None and "535" in reason and BAD_SECRET not in reason


async def test_permanent_recipient_refusal_is_rejected(server: Server) -> None:
    server.handler.rcpt_reply = "550 5.1.1 No such user here"
    result = await send(server)
    assert (result.outcome, result.code) == ("rejected", 550)
    assert server.handler.received == []


async def test_temporary_recipient_refusal_is_retry(server: Server) -> None:
    server.handler.rcpt_reply = "451 4.7.1 Greylisted, try later"
    result = await send(server)
    assert (result.outcome, result.code) == ("retry", 451)


async def test_bad_login_is_mailbox_error(server: Server) -> None:
    result = await send(server, secret=BAD_SECRET)
    assert (result.outcome, result.code) == ("mailbox_error", 535)
    assert BAD_SECRET not in result.detail


async def test_data_rejection_is_failed(server: Server) -> None:
    server.handler.data_reply = "554 5.7.1 Message refused"
    result = await send(server)
    assert (result.outcome, result.code) == ("failed", 554)


async def test_temporary_data_error_is_retry(server: Server) -> None:
    server.handler.data_reply = "451 4.3.0 Try again"
    assert (await send(server)).outcome == "retry"


async def test_connection_dropped_during_data_is_ambiguous(server: Server) -> None:
    server.handler.drop_in_data = True
    assert (await send(server)).outcome == "ambiguous"


async def test_timeout_during_data_is_ambiguous(server: Server) -> None:
    server.handler.stall_in_data = True
    srv_creds = replace(server.creds(), timeout=0.5)
    result = await server.sender().send(
        srv_creds, message(), envelope_from=USER, envelope_to="jane.doe@acme-soap.com"
    )
    assert result.outcome == "ambiguous"


async def test_connection_refused_is_retry() -> None:
    creds = MailboxCredentials(
        address=USER, username=USER, password=SecretStr(PASSWORD), smtp_host="127.0.0.1",
        smtp_port=free_port(), smtp_security="ssl", imap_host="127.0.0.1", imap_port=993,
        sent_folder=None, timeout=2,
    )  # fmt: skip
    result = await AioSmtpSender().send(
        creds, message(), envelope_from=USER, envelope_to="jane.doe@acme-soap.com"
    )
    assert result.outcome == "retry"
    assert await AioSmtpSender().check(creds) == "Couldn't connect to the server."


def test_send_result_detail_is_clean_and_short() -> None:
    result = SendResult("failed", 554, "bad\x00thing\r\nBcc: x\x1b[31m " + "y" * 500)
    assert len(result.detail) == 200
    assert all(ch.isprintable() for ch in result.detail)
    assert clean_detail("a\n\tb") == "a b"


async def test_ehlo_names_the_mailbox_domain(server: Server) -> None:
    assert (await send(server)).outcome == "sent"
    assert server.handler.ehlo_names
    assert set(server.handler.ehlo_names) == {"send-domain.com"}
    assert socket.gethostname() not in server.handler.ehlo_names
