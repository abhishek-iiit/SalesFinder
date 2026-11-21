import email
import socket
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from typing import Any

import pytest

from salesfinder.mail.mime import build_message, envelope_address, new_message_id

NOW = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


def make(**over: Any) -> EmailMessage:
    kwargs: dict[str, Any] = {
        "from_name": "Jordan Rivera",
        "from_address": "sales@send-domain.com",
        "to_address": "jane.doe@acme-soap.com",
        "subject": "Sourcing glycerin",
        "body": "Hi Jane,\n\nWe supply glycerin.\n",
        "message_id": "<abc@send-domain.com>",
        "date": NOW,
        "in_reply_to": None,
        "references": [],
        "unsubscribe_mailto": "unsub@send-domain.com",
    }
    kwargs.update(over)
    return build_message(**kwargs)


def reparse(msg: EmailMessage) -> EmailMessage:
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def test_body_round_trips_exactly() -> None:
    body = "Hi Jane,\n\nWe supply glycerin.\nLine three.\n"
    msg = reparse(make(body=body))
    assert msg.get_content().replace("\r\n", "\n") == body
    assert b"\r\n" in make(body=body).as_bytes()


def test_non_ascii_body_is_utf8() -> None:
    body = "Grüße, Jürgen — 100 €\n"
    msg = reparse(make(body=body, subject="Grüße"))
    assert msg.get_content_charset() == "utf-8"
    assert msg.get_content().replace("\r\n", "\n") == body
    assert msg["Subject"] == "Grüße"


@pytest.mark.parametrize("field", ["subject", "from_name", "message_id", "in_reply_to"])
def test_header_injection_refused(field: str) -> None:
    with pytest.raises(ValueError):
        make(**{field: "x\r\nBcc: x@evil.example"})
    with pytest.raises(ValueError):
        make(references=["<a@b>\nBcc: x@evil.example"])


def test_envelope_address_forms() -> None:
    with pytest.raises(ValueError, match="SMTPUTF8"):
        envelope_address("jürgen@bücher.de")
    assert envelope_address("jane@bücher.de") == "jane@xn--bcher-kva.de"
    assert envelope_address("Jane.Doe@Acme-Soap.COM") == "Jane.Doe@acme-soap.com"
    with pytest.raises(ValueError):
        envelope_address("no-at-sign")


def test_message_id_uses_mailbox_domain() -> None:
    mid = new_message_id("sales@send-domain.com")
    assert mid.startswith("<") and mid.endswith("@send-domain.com>")
    assert socket.gethostname() not in mid
    assert new_message_id("sales@send-domain.com") != mid


def test_threading_and_unsubscribe_headers() -> None:
    msg = reparse(
        make(
            in_reply_to="<prev@send-domain.com>",
            references=["<first@send-domain.com>", "<prev@send-domain.com>"],
        )
    )
    assert msg["In-Reply-To"] == "<prev@send-domain.com>"
    assert msg["References"] == "<first@send-domain.com> <prev@send-domain.com>"
    assert msg["List-Unsubscribe"] == "<mailto:unsub@send-domain.com?subject=unsubscribe>"
    assert msg["Message-ID"] == "<abc@send-domain.com>"
    assert msg["To"] == "jane.doe@acme-soap.com"
    assert "Jordan Rivera" in msg["From"]
    assert make()["In-Reply-To"] is None


def test_non_ascii_body_and_headers_are_pure_ascii_on_the_wire() -> None:
    body = "Prices — per tonne · café é\nSecond line\n"
    msg = make(body=body, subject="Prices — café", from_name="Jörg · Rivera")
    raw = msg.as_bytes()
    assert raw.isascii()
    assert b"quoted-printable" in raw and b"8bit" not in raw
    assert reparse(msg).get_content().replace("\r\n", "\n") == body


def test_body_without_final_newline_gains_exactly_one() -> None:
    # The email API always terminates the body; pinned: only line endings and this newline change.
    got = reparse(make(body="a\nb")).get_content()
    assert got == "a\r\nb\r\n"
    assert reparse(make(body="a\rb\n")).get_content() == "a\r\nb\r\n"
    assert reparse(make(body="a\r\nb\n")).get_content() == "a\r\nb\r\n"


def test_formats_are_validated() -> None:
    for bad in ("u@x.co>, <mailto:y@evil", "not an address", "a b@x.co"):
        with pytest.raises(ValueError):
            make(unsubscribe_mailto=bad)
    for bad_id in (
        "abc@send-domain.com",
        "<a@b> <c@d>",
        "<a b@c>",
        "<>",
        # Final-wave M5: printable ASCII only (no controls, DEL, non-ASCII, < or > inside).
        "<a\x01b@c>",
        "<a\x7fb@c>",
        "<ü@x>",
        "<a\u00a0b@c>",
        "<a<b@c>",
        "<a@b>\n",
    ):
        with pytest.raises(ValueError):
            make(message_id=bad_id)
        with pytest.raises(ValueError):
            make(in_reply_to=bad_id)
        with pytest.raises(ValueError):
            make(references=[bad_id])


def test_body_control_characters_refused() -> None:
    for bad in ("a\x00b", "a\x1b[31mb", "a\x0bb", "a\x7fb"):
        with pytest.raises(ValueError):
            make(body=bad)
    assert reparse(make(body="tab\there\n")).get_content() == "tab\there\r\n"
