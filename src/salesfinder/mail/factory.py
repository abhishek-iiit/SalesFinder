"""The injectable pair (sender, reader opener) that the send/ingest jobs use."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from email.message import EmailMessage

from salesfinder.mail.imap import MailReader, open_reader
from salesfinder.mail.smtp import AioSmtpSender, MailboxCredentials, MailSender, SendResult


@dataclass(frozen=True)
class MailFactory:
    sender: MailSender
    open_reader: Callable[[MailboxCredentials], Awaitable[MailReader]]


def default_mail() -> MailFactory:
    return MailFactory(sender=AioSmtpSender(), open_reader=open_reader)


class SendingDisabledError(RuntimeError):
    """A send was attempted by a worker started with ``--no-send``."""


NO_SEND_DETAIL = "This worker runs with --no-send."


class RefusingSender:
    """The sender of a ``--no-send`` worker: never connects anywhere. Belt and braces: with
    ``send_enabled=False`` the Sending step never reaches it. A send is answered with a
    ``retry`` (never an exception), so a claimed message goes back to approved and is never
    left ``sending``; a connection check refuses."""

    async def send(
        self,
        creds: MailboxCredentials,
        message: EmailMessage,
        *,
        envelope_from: str,
        envelope_to: str,
    ) -> SendResult:
        return SendResult("retry", None, NO_SEND_DETAIL)

    async def check(self, creds: MailboxCredentials) -> str | None:
        raise SendingDisabledError("this worker runs with --no-send")


def without_sending(mail: MailFactory) -> MailFactory:
    """``mail`` with a sender that refuses to send (mail reading unchanged)."""
    return replace(mail, sender=RefusingSender())
