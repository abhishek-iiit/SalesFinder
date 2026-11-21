import asyncio
import imaplib
import re
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from salesfinder.mail import imap
from salesfinder.mail.smtp import MailboxCredentials

CREDS = MailboxCredentials(
    address="sales@send-domain.com",
    username="sales@send-domain.com",
    password=SecretStr("app-password"),
    smtp_host="smtp.example.com",
    smtp_port=465,
    smtp_security="ssl",
    imap_host="imap.example.com",
    imap_port=993,
    sent_folder=None,
    timeout=5,
)
SINCE = date(2026, 11, 1)
_REAL_IMAP4_SSL = imaplib.IMAP4_SSL  # captured before the autouse guard patches it


class StandInIMAP:
    """A scripted imaplib.IMAP4_SSL: records commands, opens no sockets."""

    instances: ClassVar[list["StandInIMAP"]] = []
    uidvalidity = b"77"
    messages: ClassVar[dict[int, bytes]] = {}
    list_lines: ClassVar[list[bytes]] = []
    error: Exception | None = None
    hide_sizes = False

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float | None = None,
    ) -> None:
        self.args = (host, port, timeout)
        self.ssl_context = ssl_context
        self.commands: list[tuple[Any, ...]] = []
        self.logged_out = False
        StandInIMAP.instances.append(self)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGIN", user))
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        if StandInIMAP.error:
            raise StandInIMAP.error
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"3"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [self.uidvalidity]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append((command, *args))
        msgs = StandInIMAP.messages
        if command == "SEARCH":
            if args[0] == "UID":
                low = int(args[1].split(":")[0])
                hits = [u for u in msgs if u >= low] or [max(msgs)]  # n:* includes the last
            elif args[0] == "SINCE":
                hits = list(msgs)
            else:
                hits = [u for u, raw in msgs.items() if args[2].strip('"').encode() in raw]
            return "OK", [" ".join(str(u) for u in hits).encode()]
        assert command == "FETCH"
        if args[1] == "(RFC822.SIZE)":
            uids = [int(u) for u in args[0].split(",")]
            if StandInIMAP.hide_sizes:
                return "OK", [f"{i} (UID {u})".encode() for i, u in enumerate(uids, 1)]
            return "OK", [
                f"{i} (UID {u} RFC822.SIZE {len(msgs[u])})".encode() for i, u in enumerate(uids, 1)
            ]
        uid = int(args[0])
        raw = msgs[uid]
        if args[1] == "(BODY.PEEK[HEADER])":
            raw = raw.split(b"\n\n")[0] + b"\n\n"
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b""]

    def shutdown(self) -> None:
        pass

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", self.list_lines


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> type[StandInIMAP]:
    StandInIMAP.instances = []
    StandInIMAP.uidvalidity = b"77"
    StandInIMAP.error = None
    StandInIMAP.hide_sizes = False
    StandInIMAP.messages = {
        7: b"Subject: seven\n\nbody seven\n",
        8: b"Message-ID: <m8@send-domain.com>\nSubject: eight\n\nbody eight\n",
        9: b"Subject: nine\n\n" + b"x" * 500,
    }
    StandInIMAP.list_lines = []
    monkeypatch.setattr("imaplib.IMAP4_SSL", StandInIMAP)
    return StandInIMAP


def fetch_cmds(conn: StandInIMAP) -> list[str]:
    return [" ".join(c[1:]) for c in conn.commands if c[0] == "FETCH"]


async def test_fetch_after_uid_uses_uid_range_and_peek(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    batch = await reader.fetch_new(
        "INBOX", uidvalidity=77, after_uid=7, since=SINCE, limit=10, max_bytes=10_000
    )
    conn = stand_in.instances[0]
    assert conn.args == ("imap.example.com", 993, 5)
    assert ("SEARCH", "UID", "8:*") in conn.commands
    assert ("SELECT", '"INBOX"', True) in conn.commands  # read-only
    assert [m.uid for m in batch.mails] == [8, 9]
    assert batch.uidvalidity == 77 and not batch.more
    assert batch.mails[0].raw.startswith(b"Message-ID: <m8@")
    cmds = fetch_cmds(conn)
    assert cmds and all("BODY.PEEK[" in c or c.endswith("(RFC822.SIZE)") for c in cmds)
    assert not any(re.search(r"\(RFC822\)|BODY\[", c) for c in cmds)
    await reader.aclose()
    assert conn.logged_out


async def test_uid_star_old_message_is_not_returned(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    batch = await reader.fetch_new(
        "INBOX", uidvalidity=77, after_uid=9, since=SINCE, limit=10, max_bytes=10_000
    )
    assert batch.mails == []


async def test_uidvalidity_change_searches_since(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    for stored in (12, None):
        batch = await reader.fetch_new(
            "INBOX", uidvalidity=stored, after_uid=8, since=SINCE, limit=10, max_bytes=10_000
        )
        assert batch.uidvalidity == 77 and len(batch.mails) == 3
    searches = [c for c in stand_in.instances[0].commands if c[0] == "SEARCH"]
    assert searches == [("SEARCH", "SINCE", "01-Nov-2026")] * 2


async def test_limit_sets_more(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    batch = await reader.fetch_new(
        "INBOX", uidvalidity=None, after_uid=0, since=SINCE, limit=2, max_bytes=10_000
    )
    assert [m.uid for m in batch.mails] == [7, 8] and batch.more


async def test_oversize_message_is_headers_only(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    batch = await reader.fetch_new(
        "INBOX", uidvalidity=77, after_uid=8, since=SINCE, limit=10, max_bytes=100
    )
    (mail,) = batch.mails
    assert mail.truncated and mail.raw == b"Subject: nine\n\n"
    assert "(BODY.PEEK[HEADER])" in fetch_cmds(stand_in.instances[0])[-1]


async def test_special_folder_sent(stand_in: type[StandInIMAP]) -> None:
    stand_in.list_lines = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    ]
    reader = await imap.open_reader(CREDS)
    assert await reader.special_folder("sent") == "[Gmail]/Sent Mail"
    stand_in.list_lines = [b'(\\HasNoChildren) "/" "INBOX"']
    assert await reader.special_folder("sent") is None
    stand_in.list_lines = [b'(\\HasNoChildren \\Sent) "." Sent']
    assert await reader.special_folder("sent") == "Sent"


async def test_has_message_id(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    assert await reader.has_message_id("[Gmail]/Sent Mail", "<m8@send-domain.com>")
    assert not await reader.has_message_id("INBOX", "<nope@send-domain.com>")
    cmd = next(
        c for c in stand_in.instances[0].commands if c[:3] == ("SEARCH", "HEADER", "Message-ID")
    )
    assert cmd[3] == '"<m8@send-domain.com>"'
    with pytest.raises(ValueError):
        await reader.has_message_id("INBOX", "<a@b>\r\nX: y")


async def test_timeout_surfaces(stand_in: type[StandInIMAP]) -> None:
    stand_in.error = TimeoutError("timed out")
    reader = await imap.open_reader(CREDS)
    with pytest.raises(TimeoutError):
        await reader.fetch_new(
            "INBOX", uidvalidity=77, after_uid=0, since=SINCE, limit=5, max_bytes=1000
        )


async def test_missing_size_counts_as_oversize(stand_in: type[StandInIMAP]) -> None:
    stand_in.hide_sizes = True
    reader = await imap.open_reader(CREDS)
    batch = await reader.fetch_new(
        "INBOX", uidvalidity=77, after_uid=7, since=SINCE, limit=10, max_bytes=10_000
    )
    assert batch.mails and all(m.truncated for m in batch.mails)
    assert all("BODY.PEEK[HEADER]" in c for c in fetch_cmds(stand_in.instances[0])[1:])


async def test_reader_is_dead_after_timeout(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    stand_in.error = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        await reader.has_message_id("INBOX", "<a@b>")
    stand_in.error = None
    for _ in range(2):  # fails straight away, never touches the connection again
        with pytest.raises(ConnectionError):
            await reader.special_folder("sent")
    conn = stand_in.instances[0]
    before = len(conn.commands)
    with pytest.raises(ConnectionError):
        await reader.fetch_new(
            "INBOX", uidvalidity=77, after_uid=0, since=SINCE, limit=5, max_bytes=1000
        )
    assert len(conn.commands) == before
    await asyncio.wait_for(reader.aclose(), 1)
    assert not conn.logged_out  # dropped, not a graceful LOGOUT


async def test_oserror_also_kills_reader(stand_in: type[StandInIMAP]) -> None:
    reader = await imap.open_reader(CREDS)
    stand_in.error = OSError("reset")
    with pytest.raises(OSError):
        await reader.has_message_id("INBOX", "<a@b>")
    with pytest.raises(ConnectionError):
        await reader.has_message_id("INBOX", "<a@b>")


# --- TLS verification (C1): a real imaplib client against a TLS server on 127.0.0.1 ----------


async def test_open_reader_always_passes_a_verifying_tls_context(
    stand_in: type[StandInIMAP],
) -> None:
    """imaplib's own default context verifies nothing; open_reader must never fall back to it."""
    await imap.open_reader(CREDS)
    ctx = stand_in.instances[0].ssl_context
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def _make_cert(directory: Path, name: str, san: str) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI not available")
    cert, key = directory / f"{name}.pem", directory / f"{name}.key"
    args = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
    args += ["-keyout", str(key), "-out", str(cert), "-subj", f"/CN={name}"]
    args += ["-addext", f"subjectAltName={san}"]
    subprocess.run(args, check=True, capture_output=True)  # noqa: S603
    return cert, key


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    d = tmp_path_factory.mktemp("imap-tls")
    return {
        "right": _make_cert(d, "localhost-imap", "IP:127.0.0.1"),
        "wrong": _make_cert(d, "other.example", "DNS:other.example"),
    }


class TlsImapServer:
    """A minimal IMAP4rev1 server on 127.0.0.1 (implicit TLS) in a thread: greeting,
    CAPABILITY, LOGIN and LOGOUT only. Records the logins it accepted."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.05)
        self.port = int(self.sock.getsockname()[1])
        self.logins: list[str] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                raw, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            raw.settimeout(5)
            try:
                with self.ctx.wrap_socket(raw, server_side=True) as conn:
                    self._session(conn.makefile("rwb"))
            except (OSError, ssl.SSLError):
                continue  # the client refused our certificate: nothing was sent

    def _session(self, f: Any) -> None:
        f.write(b"* OK IMAP4rev1 ready\r\n")
        f.flush()
        for line in f:
            tag, _, rest = line.decode().strip().partition(" ")
            command = rest.split(" ", 1)[0].upper()
            if command == "CAPABILITY":
                f.write(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n")
            elif command == "LOGIN":
                self.logins.append(rest.split(" ")[1])
            elif command == "LOGOUT":
                f.write(b"* BYE bye\r\n" + f"{tag} OK done\r\n".encode())
                f.flush()
                return
            f.write(f"{tag} OK done\r\n".encode())
            f.flush()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)
        self.sock.close()


def _with_imap(port: int) -> MailboxCredentials:
    return replace(CREDS, imap_host="127.0.0.1", imap_port=port, timeout=5)


@pytest.fixture
def real_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("imaplib.IMAP4_SSL", _REAL_IMAP4_SSL)


@pytest.fixture
def tls_server(
    certs: dict[str, tuple[Path, Path]], request: pytest.FixtureRequest, real_imap: None
) -> Iterator[TlsImapServer]:
    server = TlsImapServer(*certs[request.param])
    try:
        yield server
    finally:
        server.close()


@pytest.mark.parametrize("tls_server", ["right", "wrong"], indirect=True)
async def test_untrusted_certificate_is_refused_by_default(tls_server: TlsImapServer) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        await imap.open_reader(_with_imap(tls_server.port))
    assert tls_server.logins == []  # the password never went out


@pytest.mark.parametrize("tls_server", ["wrong"], indirect=True)
async def test_wrong_host_certificate_is_refused_even_when_trusted(
    tls_server: TlsImapServer, certs: dict[str, tuple[Path, Path]]
) -> None:
    trusted = ssl.create_default_context(cafile=str(certs["wrong"][0]))
    with pytest.raises(ssl.SSLCertVerificationError):
        await imap.open_reader(_with_imap(tls_server.port), tls_context=trusted)
    assert tls_server.logins == []


@pytest.mark.parametrize("tls_server", ["right"], indirect=True)
async def test_trusted_certificate_is_accepted_through_the_test_hook(
    tls_server: TlsImapServer, certs: dict[str, tuple[Path, Path]]
) -> None:
    trusted = ssl.create_default_context(cafile=str(certs["right"][0]))
    reader = await imap.open_reader(_with_imap(tls_server.port), tls_context=trusted)
    await reader.aclose()
    assert tls_server.logins == [CREDS.username]
