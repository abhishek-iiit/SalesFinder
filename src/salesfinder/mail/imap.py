"""Read-only IMAP (stdlib imaplib in worker threads). Messages are fetched with BODY.PEEK so the
``\\Seen`` flag is never set. Folder, server text and message contents are never logged."""

import asyncio
import imaplib
import re
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol, TypeVar

import structlog

from salesfinder.mail.smtp import MailboxCredentials

log = structlog.get_logger(__name__)

T = TypeVar("T")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)")


class ImapError(Exception):
    """The server refused a command (the server's text is deliberately not carried)."""


@dataclass(frozen=True)
class FetchedMail:
    uid: int
    raw: bytes
    truncated: bool


@dataclass(frozen=True)
class FolderBatch:
    name: str
    uidvalidity: int
    mails: list[FetchedMail]
    more: bool


class MailReader(Protocol):
    async def special_folder(self, use: Literal["sent"]) -> str | None: ...

    async def fetch_new(
        self,
        folder: str,
        *,
        uidvalidity: int | None,
        after_uid: int,
        since: date,
        limit: int,
        max_bytes: int,
    ) -> FolderBatch: ...

    async def has_message_id(self, folder: str, message_id: str) -> bool: ...

    async def aclose(self) -> None: ...


def _quote(value: str) -> str:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError("IMAP arguments must not contain line breaks.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _ok(result: tuple[str, Sequence[object]]) -> Sequence[object]:
    status, data = result
    if status != "OK":
        raise ImapError(f"IMAP command failed ({status})")
    return data


def _uids(data: Sequence[object]) -> list[int]:
    if not data or not isinstance(data[0], bytes):
        return []
    return sorted(int(tok) for tok in data[0].split() if tok.isdigit())


def _literal(data: Sequence[object]) -> bytes:
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return b""


class ImaplibReader:
    def __init__(self, conn: imaplib.IMAP4_SSL) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()  # one command at a time on the connection
        self._dead = False

    def _guard(self) -> None:
        if self._dead:
            raise ConnectionError("IMAP connection is closed after an earlier failure")

    async def _run(self, func: Callable[..., T], *args: object) -> T:
        """Run one blocking call under the lock; a timeout or socket error kills the reader."""
        self._guard()
        async with self._lock:
            self._guard()
            try:
                return await asyncio.to_thread(func, *args)
            except (TimeoutError, OSError):
                self._dead = True
                raise

    async def special_folder(self, use: Literal["sent"]) -> str | None:
        data = _ok(await self._run(self._conn.list))
        for item in data:
            if not isinstance(item, bytes):
                continue
            match = _LIST_RE.match(item.strip())
            if not match:
                continue
            flags = {f.lower() for f in match["flags"].split()}
            if rb"\sent" in flags:
                name = match["name"].strip()
                if name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2:
                    name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
                return name.decode("utf-8", "replace")
        return None

    def _select(self, folder: str) -> int:
        _ok(self._conn.select(_quote(folder), readonly=True))
        raw = self._conn.response("UIDVALIDITY")[1]
        first = raw[0] if raw else None
        return int(first) if isinstance(first, bytes) and first.isdigit() else 0

    def _fetch_new(
        self,
        folder: str,
        uidvalidity: int | None,
        after_uid: int,
        since: date,
        limit: int,
        max_bytes: int,
    ) -> FolderBatch:
        current = self._select(folder)
        if uidvalidity is None or uidvalidity != current:
            found = _uids(_ok(self._conn.uid("SEARCH", "SINCE", _imap_date(since))))
        else:
            found = [
                u
                for u in _uids(_ok(self._conn.uid("SEARCH", "UID", f"{after_uid + 1}:*")))
                if u > after_uid  # "n:*" always includes the last message, even if older
            ]
        more = len(found) > limit
        wanted = found[:limit]
        sizes: dict[int, int] = {}
        if wanted:
            listing = _ok(
                self._conn.uid("FETCH", ",".join(str(u) for u in wanted), "(RFC822.SIZE)")
            )
            for item in listing:
                blob = item[0] if isinstance(item, tuple) else item
                if isinstance(blob, bytes):
                    uid_m, size_m = _UID_RE.search(blob), _SIZE_RE.search(blob)
                    if uid_m and size_m:
                        sizes[int(uid_m[1])] = int(size_m[1])
        mails: list[FetchedMail] = []
        for uid in wanted:
            truncated = sizes.get(uid, max_bytes + 1) > max_bytes  # unknown size: headers only
            part = "(BODY.PEEK[HEADER])" if truncated else "(BODY.PEEK[])"
            raw = _literal(_ok(self._conn.uid("FETCH", str(uid), part)))
            mails.append(FetchedMail(uid=uid, raw=raw, truncated=truncated))
        log.info("imap_fetched", count=len(mails), more=more)
        return FolderBatch(name=folder, uidvalidity=current, mails=mails, more=more)

    async def fetch_new(
        self,
        folder: str,
        *,
        uidvalidity: int | None,
        after_uid: int,
        since: date,
        limit: int,
        max_bytes: int,
    ) -> FolderBatch:
        return await self._run(
            self._fetch_new, folder, uidvalidity, after_uid, since, limit, max_bytes
        )

    def _has_message_id(self, folder: str, message_id: str) -> bool:
        self._select(folder)
        data = _ok(self._conn.uid("SEARCH", "HEADER", "Message-ID", _quote(message_id)))
        return bool(_uids(data))

    async def has_message_id(self, folder: str, message_id: str) -> bool:
        return await self._run(self._has_message_id, folder, message_id)

    async def aclose(self) -> None:
        if self._dead:  # never wait on a lock held by a stuck call; just drop the socket
            try:
                self._conn.shutdown()
            except Exception:
                log.debug("imap_shutdown_failed")
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._conn.logout)
            except Exception:
                log.debug("imap_logout_failed")


async def open_reader(
    creds: MailboxCredentials, *, tls_context: ssl.SSLContext | None = None
) -> MailReader:
    """Log in over implicit TLS that verifies the server's certificate and hostname. imaplib's
    own default (``ssl._create_stdlib_context``) verifies neither, so the context is always
    passed: ``ssl.create_default_context()`` unless a test hands in one trusting its local
    self-signed certificate (``tls_context``, like ``AioSmtpSender(tls_context=...)``)."""
    context = tls_context if tls_context is not None else ssl.create_default_context()

    def connect() -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(
            creds.imap_host, creds.imap_port, ssl_context=context, timeout=creds.timeout
        )
        try:
            conn.login(creds.username, creds.password.get_secret_value())
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                log.debug("imap_shutdown_failed")
            raise
        return conn

    return ImaplibReader(await asyncio.to_thread(connect))
