"""In-memory mail transport for tests: no sockets, no real mail."""

from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from typing import Literal

from salesfinder.mail.factory import MailFactory
from salesfinder.mail.imap import FetchedMail, FolderBatch, MailReader
from salesfinder.mail.smtp import MailboxCredentials, SendResult


class FakeSender:
    """``outcomes[envelope_to]`` is a result or a list used in order (the last one repeats)."""

    def __init__(
        self,
        outcomes: dict[str, SendResult | list[SendResult]] | None = None,
        *,
        check_error: str | None = None,
    ) -> None:
        self.outcomes: dict[str, SendResult | list[SendResult]] = dict(outcomes or {})
        self.check_error = check_error
        self.sent: list[tuple[str, str, EmailMessage]] = []
        self.checked: list[str] = []

    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult:
        self.sent.append((creds.address, envelope_to, message))
        outcome = self.outcomes.get(envelope_to, SendResult("sent", 250, "OK"))
        if isinstance(outcome, list):
            return outcome.pop(0) if len(outcome) > 1 else outcome[0]
        return outcome

    async def check(self, creds: MailboxCredentials) -> str | None:
        self.checked.append(creds.address)
        return self.check_error


@dataclass
class FakeFolder:
    uidvalidity: int
    mails: list[tuple[int, bytes]] = field(default_factory=list)

    def add(self, raw: bytes) -> int:
        uid = max((u for u, _ in self.mails), default=0) + 1
        self.mails.append((uid, raw))
        return uid


def _headers_only(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        head, found, _ = raw.partition(sep)
        if found:
            return head + sep
    return raw


class FakeReader:
    def __init__(
        self,
        folders: dict[str, FakeFolder],
        special: dict[str, str] | None = None,
        *,
        fail: Exception | None = None,
    ) -> None:
        self.folders = folders
        self.special = {"sent": "[Gmail]/Sent Mail"} if special is None else special
        self.fail = fail
        self.closed = False
        self.fetches: list[str] = []

    def _folder(self, name: str) -> FakeFolder:
        if self.fail is not None:
            raise self.fail
        try:
            return self.folders[name]
        except KeyError:
            raise LookupError("no such folder") from None

    async def special_folder(self, use: Literal["sent"]) -> str | None:
        if self.fail is not None:
            raise self.fail
        return self.special.get(use)

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
        fo = self._folder(folder)
        self.fetches.append(folder)
        everything = uidvalidity is None or uidvalidity != fo.uidvalidity
        chosen = [(u, r) for u, r in sorted(fo.mails) if everything or u > after_uid]
        mails = [
            FetchedMail(
                uid=u,
                raw=_headers_only(r) if len(r) > max_bytes else r,
                truncated=len(r) > max_bytes,
            )
            for u, r in chosen[:limit]
        ]
        return FolderBatch(
            name=folder, uidvalidity=fo.uidvalidity, mails=mails, more=len(chosen) > limit
        )

    async def has_message_id(self, folder: str, message_id: str) -> bool:
        fo = self._folder(folder)
        needle = message_id.encode()
        return any(needle in _headers_only(r) for _, r in fo.mails)

    async def aclose(self) -> None:
        self.closed = True


def fake_mail(sender: FakeSender, reader: MailReader | None) -> MailFactory:
    async def open_reader(_creds: MailboxCredentials) -> MailReader:
        if reader is None:
            raise RuntimeError("no reader configured")
        return reader

    return MailFactory(sender=sender, open_reader=open_reader)
