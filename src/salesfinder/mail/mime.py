"""Build the outgoing message. Only MIME encoding happens here: the stored subject and body are
sent as given, headers are the only additions."""

import re
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from salesfinder.contacts.dns import mx_domain

NON_ASCII_LOCAL = (
    "Recipient address has non-ASCII characters before the @; SMTPUTF8 isn't supported."
)
# A Message-ID (and In-Reply-To/References entry): printable ASCII other than "<" and ">",
# in angle brackets. ``parse`` keeps only ids of this form, so any it stores can be built.
ID_CHARS = r"[\x21-\x3b\x3d\x3f-\x7e]"
_ID_RE = re.compile(rf"<{ID_CHARS}+>")
_BODY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BAD_ADDRESS_CHARS = re.compile(r"[\s\x00-\x1f\x7f<>]")


def message_id_ok(value: str) -> bool:
    """``value`` is exactly one id: ``<`` printable ASCII ``>`` (``ID_CHARS``)."""
    return _ID_RE.fullmatch(value) is not None


def _check_line(name: str, value: str) -> None:
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{name} must not contain line breaks.")


def envelope_address(address: str) -> str:
    """The one wire form: the local part as stored (ASCII) plus the IDNA domain."""
    local, sep, domain = address.strip().rpartition("@")
    if not sep or not local:
        raise ValueError("Address must look like name@domain.")
    if not local.isascii():
        raise ValueError(NON_ASCII_LOCAL)
    if _BAD_ADDRESS_CHARS.search(local):
        raise ValueError("Address has invalid characters.")
    ascii_domain = mx_domain(domain)
    if ascii_domain is None or _BAD_ADDRESS_CHARS.search(ascii_domain):
        raise ValueError("Address has an invalid domain.")
    return f"{local}@{ascii_domain}"


def new_message_id(mailbox_address: str) -> str:
    """``<unique@mailbox-domain>``: the domain is the mailbox's, never this host's name."""
    domain = mx_domain(mailbox_address.rpartition("@")[2])
    if domain is None:
        raise ValueError("Mailbox address has an invalid domain.")
    return make_msgid(domain=domain)


def build_message(
    *,
    from_name: str,
    from_address: str,
    to_address: str,
    subject: str,
    body: str,
    message_id: str,
    date: datetime,
    in_reply_to: str | None,
    references: list[str],
    unsubscribe_mailto: str,
) -> EmailMessage:
    """Build the outgoing message; the stored body is sent as given, UTF-8, quoted-printable.

    The only body changes: line endings become CRLF on the wire (a lone CR is a line break) and
    the email API always ends the body with a line break, so a body without a final newline gains
    one. Ids must be ``<...>`` tokens; the body may not hold control characters other than
    \\n, \\r and \\t.
    """
    for name, value in (
        ("from_name", from_name),
        ("from_address", from_address),
        ("to_address", to_address),
        ("subject", subject),
        ("message_id", message_id),
        ("in_reply_to", in_reply_to or ""),
        ("unsubscribe_mailto", unsubscribe_mailto),
    ):
        _check_line(name, value)
    for ref in references:
        _check_line("references", ref)
    if not message_id_ok(message_id):
        raise ValueError("message_id must look like <id@domain>.")
    if in_reply_to is not None and not message_id_ok(in_reply_to):
        raise ValueError("in_reply_to must look like <id@domain>.")
    if not all(message_id_ok(ref) for ref in references):
        raise ValueError("references must each look like <id@domain>.")
    if _BODY_CONTROL_RE.search(body):
        raise ValueError("body must not contain control characters.")
    unsubscribe = envelope_address(unsubscribe_mailto)

    sender = envelope_address(from_address)
    recipient = envelope_address(to_address)
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = Address(display_name=from_name, addr_spec=sender)
    msg["To"] = Address(addr_spec=recipient)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg["List-Unsubscribe"] = f"<mailto:{unsubscribe}?subject=unsubscribe>"
    # quoted-printable keeps the wire pure ASCII (no 8BITMIME needed from the relay)
    msg.set_content(body, charset="utf-8", cte="quoted-printable")
    return msg
