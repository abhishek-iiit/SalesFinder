"""Inbound mail for ingest tests: Task 2's fixtures rewritten to our Message-IDs and
recipients, and small builders for replies and Sent-folder copies (raw RFC 5322 bytes, as
``FakeReader`` serves them)."""

from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from sending_helpers import BOX, RECIPIENT

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
# The Message-IDs of "our" emails quoted in the Task 2 fixtures.
FIXTURE_OURS = (
    "<20261110.4242.ours1@send-domain.com>",
    "<20261110.4243.ours2@send-domain.com>",
    "<20261111.4244.ours3@send-domain.com>",
)
FIXTURE_RECIPIENTS = ("jane.doe@acme-soap.com", "buyer@example.com")


def fixture(name: str, *, ours: str | None = None, recipient: str | None = None) -> bytes:
    """A Task 2 fixture with its quoted Message-IDs of ours replaced by ``ours`` and its
    bounced/addressed recipient replaced by ``recipient``."""
    raw = (FIXTURES / name).read_bytes()
    if ours is not None:
        for old in FIXTURE_OURS:
            raw = raw.replace(old.encode(), ours.encode())
    if recipient is not None:
        for old_recipient in FIXTURE_RECIPIENTS:
            raw = raw.replace(old_recipient.encode(), recipient.encode())
    return raw


def dsn_for(name: str = "gmail_dsn_550.eml", *, original: str, recipient: str = RECIPIENT) -> bytes:
    """A bounce fixture about our email ``original`` (its Message-ID) to ``recipient``."""
    return fixture(name, ours=original, recipient=recipient)


def _build(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    date: datetime,
    in_reply_to: str | None = None,
    references: str | None = None,
    cc: str | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(date)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    return msg.as_bytes()


def reply_to(
    message_id: str | None,
    *,
    body: str = "Thanks, we are interested. Can you send a quote?",
    subject: str = "Re: Sourcing glycerin for your soap line",
    sender: str = f"Jane Doe <{RECIPIENT}>",
    to: str = BOX,
    mid: str = "<reply-0001@mail.acme-soap.com>",
    date: datetime,
    cc: str | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Their answer to our email ``message_id`` (None: a fresh email, no threading)."""
    return _build(
        sender=sender,
        to=to,
        cc=cc,
        subject=subject,
        body=body,
        message_id=mid,
        date=date,
        in_reply_to=message_id,
        references=message_id,
        headers=headers,
    )


def sent_copy(
    *,
    message_id: str,
    to: str = RECIPIENT,
    subject: str,
    body: str = "Hi Jane, writing directly. Free for a call this week?",
    date: datetime,
    in_reply_to: str | None = None,
) -> bytes:
    """A mail in the mailbox's Sent folder (ours, or a teammate's from Gmail)."""
    return _build(
        sender=f"Jordan Rivera <{BOX}>",
        to=to,
        subject=subject,
        body=body,
        message_id=message_id,
        date=date,
        in_reply_to=in_reply_to,
        references=in_reply_to,
    )
