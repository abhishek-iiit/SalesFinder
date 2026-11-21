import dataclasses
import time
from pathlib import Path

from salesfinder.mail.parse import (
    bounce_of,
    is_auto_reply,
    is_unsubscribe,
    parse_mail,
    strip_quoted,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mail"
JANE = "jane.doe@acme-soap.com"


def load(name: str, max_chars: int = 20_000):  # type: ignore[no-untyped-def]
    return parse_mail((FIXTURES / f"{name}.eml").read_bytes(), max_body_chars=max_chars)


def test_gmail_dsn_550_is_hard() -> None:
    mail = load("gmail_dsn_550")
    bounce = bounce_of(mail, known_recipients=[JANE])
    assert bounce is not None
    assert (bounce.kind, bounce.recipient, bounce.status) == ("hard", JANE, "5.1.1")
    assert bounce.original_message_id == "<20261110.4242.ours1@send-domain.com>"
    assert mail.report is not None and mail.report.structured
    assert mail.report.recipients[0].diagnostic.startswith("smtp; 550 5.1.1")


def test_plain_bounce_needs_exactly_one_known_recipient() -> None:
    mail = load("plain_bounce_no_report")
    assert mail.report is None
    one = bounce_of(mail, known_recipients=[JANE, "other@example.com"])
    assert one is not None and (one.kind, one.recipient) == ("hard", JANE)
    assert one.status == "5.1.1"
    # The text mentions only one address; make a second one appear too.
    two_raw = (
        FIXTURES / "plain_bounce_no_report.eml"
    ).read_bytes() + b"\nAlso tried: pat@example.com\n"
    two = parse_mail(two_raw, max_body_chars=20_000)
    unclear = bounce_of(two, known_recipients=[JANE, "pat@example.com"])
    assert unclear is not None and unclear.kind == "unclear" and unclear.recipient is None
    none_known = bounce_of(mail, known_recipients=["unrelated@example.com"])
    assert none_known is not None and none_known.kind == "unclear"


def test_delayed_4xx_is_soft() -> None:
    bounce = bounce_of(load("dsn_delayed_4xx"), known_recipients=["buyer@example.com"])
    assert bounce is not None
    assert (bounce.kind, bounce.status) == ("soft", "4.4.1")
    assert bounce.original_message_id == "<20261111.4244.ours3@send-domain.com>"


def test_dsn_with_unknown_message_id_still_hard_for_our_recipient() -> None:
    bounce = bounce_of(load("dsn_unknown_msgid"), known_recipients=[JANE])
    assert bounce is not None and bounce.kind == "hard"
    assert bounce.original_message_id == "<not-ours-9999@other-domain.example>"


def test_dsn_for_address_not_ours_suppresses_nothing() -> None:
    bounce = bounce_of(load("gmail_dsn_550"), known_recipients=["someone.else@example.com"])
    assert bounce is not None and bounce.kind == "unclear" and bounce.recipient is None


def test_ordinary_mail_is_not_a_bounce() -> None:
    assert bounce_of(load("reply_gmail_quoted"), known_recipients=[JANE]) is None


def test_auto_replies() -> None:
    by_header = load("ooo_auto_submitted")
    assert by_header.headers_auto and is_auto_reply(by_header)
    by_subject = load("outlook_ooo_subject_only")
    assert not by_subject.headers_auto and is_auto_reply(by_subject)
    assert not is_auto_reply(load("reply_gmail_quoted"))


def test_gmail_reply_quoted_text_removed() -> None:
    mail = load("reply_gmail_quoted")
    assert mail.from_address == JANE and mail.from_local == "jane.doe"
    assert mail.in_reply_to == ["<20261110.4242.ours1@send-domain.com>"]
    assert mail.message_id == "<CAFake0007@mail.example.com>"
    assert mail.new_text == (
        "Hi Jordan,\n\nYes, we are interested. Can you send pricing for 20 tonnes?\n\nThanks,\nJane"
    )
    assert "unsubscribe" in mail.text  # the quote is still in the full text
    assert mail.date is not None and mail.date.utcoffset() is not None


def test_outlook_original_message_removed() -> None:
    mail = load("reply_outlook_original_message")
    assert mail.new_text == "Please call me tomorrow afternoon."
    assert mail.subject == "RE: Sourcing glycerin for your soap line"


def test_html_only_becomes_text() -> None:
    mail = load("reply_html_only")
    assert "Sounds good, please send a quote." in mail.text
    assert "color: red" not in mail.text and "<p>" not in mail.text
    assert mail.new_text.startswith("Sounds good")


def test_is_unsubscribe() -> None:
    button = load("gmail_unsubscribe_button")
    assert button.new_text == ""
    assert is_unsubscribe(button.subject, button.new_text)
    assert is_unsubscribe("Re: hello", "Please remove me from your list")
    assert is_unsubscribe("Re: hello", "please don't contact me again")
    footer_only = load("reply_gmail_quoted")
    assert not is_unsubscribe(footer_only.subject, footer_only.new_text)
    assert not is_unsubscribe("Re: hello", "Interested, send pricing.")
    assert not is_unsubscribe("Re: hi", "x" * 600 + " unsubscribe")


OPT_OUT = (
    "If you'd rather not hear from us, just reply \"unsubscribe\" and we won't email you again."
)


def test_our_own_footer_is_never_an_unsubscribe() -> None:
    """Final-wave M3: a reply whose quoted copy of our email isn't recognised as quoted still
    carries our footer; the opt-out line in it (even re-wrapped, any case) isn't theirs."""
    top_post = (
        "Thanks, sounds good. Send a quote for 20 tonnes.\n\n"
        "Elchemy · 1 Main St, Mumbai\n" + OPT_OUT
    )
    assert is_unsubscribe("Re: hello", top_post)  # what happened before the fix
    assert not is_unsubscribe("Re: hello", top_post, ours=[OPT_OUT])
    wrapped = "Sounds good.\n" + OPT_OUT.upper().replace(" just ", "\njust ")
    assert not is_unsubscribe("Re: hello", wrapped, ours=[OPT_OUT])
    # Far down the text, the footer cut at the 500-character window isn't half-matched.
    late = "y" * 470 + "\n" + OPT_OUT
    assert not is_unsubscribe("Re: hello", late, ours=[OPT_OUT])
    # Their own words still count, with our footer present or not.
    assert is_unsubscribe("Re: hello", "Please unsubscribe me.\n\n" + OPT_OUT, ours=[OPT_OUT])
    assert is_unsubscribe("unsubscribe", OPT_OUT, ours=[OPT_OUT])


def test_human_mail_has_no_threading_headers() -> None:
    mail = load("human_sent_no_inreplyto")
    assert mail.in_reply_to == [] and mail.references == []
    assert mail.from_address == "sales@send-domain.com"
    assert mail.to_addresses == [JANE]


def test_garbage_bytes_do_not_raise() -> None:
    for raw in (
        b"",
        b"\x00\xff\xfe garbage \r\n\r\n\x80",
        b"Content-Type: multipart/report\n\n--x",
    ):
        mail = parse_mail(raw, max_body_chars=100)
        assert mail.message_id is None and mail.from_address is None
        assert mail.in_reply_to == [] and mail.to_addresses == [] and mail.subject == ""
        assert mail.report is None or mail.report.recipients == []


def test_body_is_capped() -> None:
    raw = b"From: a@example.com\nSubject: x\n\n" + b"word " * 1000
    assert len(parse_mail(raw, max_body_chars=100).text) <= 100


def test_strip_quoted_variants() -> None:
    assert strip_quoted("Hi\n\nOn Mon, Jan 5, 2026 at 3:04 PM Bob <b@x.com> wrote:\n> old") == "Hi"
    assert strip_quoted("Hi\n________________________________\nFrom: a\nSent: b") == "Hi"
    assert strip_quoted("Hi\nFrom: Bob <b@x.com>\nSent: Monday\nTo: me\nold") == "Hi"
    assert strip_quoted("Line\n> quoted\nmore") == "Line\nmore"
    assert strip_quoted("From: me is how I sign\nbye") == "From: me is how I sign\nbye"


def _hostile_cases() -> dict[str, bytes]:
    html = b"From: a@example.com\nContent-Type: text/html\n\n" + b"<div>" * 20_000
    nested = b"".join(
        b'Content-Type: multipart/mixed; boundary="b%d"\n\n--b%d\n' % (i, i) for i in range(3000)
    )
    long_from = b"From: " + b"a" * 450_000 + b"@example.com\nSubject: x\n\nhi\n"
    on_line = b"From: a@example.com\n\nOn " + b"x" * 1_000_000 + b"\n"
    runs = b"From: mailer-daemon@example.com\nSubject: Undelivered\n\n" + b"a-" * 300_000
    many = (
        b'From: a@example.com\nContent-Type: multipart/mixed; boundary="b"\n\n'
        + b"--b\nContent-Type: text/plain\n\nx\n" * 200_000
    )
    long_to = b"From: a@example.com\nTo: " + b", ".join(
        b"u%d@example.com" % i for i in range(30_000)
    )
    return {
        "many_parts": many,
        "long_to": long_to + b"\n\nhi\n",
        "html": html,
        "multipart": nested,
        "from": long_from,
        "on": on_line,
        "emails": runs,
    }


def test_hostile_input_parses_quickly() -> None:
    for name, raw in _hostile_cases().items():
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            mail = parse_mail(raw, max_body_chars=20_000)
            bounce_of(mail, known_recipients=[JANE])
            strip_quoted("On " + "x" * 1_000_000)
            best = min(best, time.perf_counter() - start)
        assert best < 3.0, f"{name} took {best:.2f}s"


def test_long_references_keep_the_latest_ids() -> None:
    ids = [f"<m{i}@send-domain.com>" for i in range(600)]
    raw = (
        "From: a@example.com\nReferences: "
        + "\n ".join(ids)
        + "\nIn-Reply-To: "
        + ids[-1]
        + "\n\nhi\n"
    )
    mail = parse_mail(raw.encode(), max_body_chars=1000)
    assert mail.references == ids[-100:] and mail.references[-1] == ids[-1]
    assert mail.in_reply_to == [ids[-1]]


def test_long_to_header_still_finds_early_recipient() -> None:
    to = ", ".join([JANE] + [f"user{i}@example.com" for i in range(400)])
    mail = parse_mail(f"From: a@example.com\nTo: {to}\n\nhi\n".encode(), max_body_chars=1000)
    assert JANE in mail.to_addresses and len(mail.to_addresses) > 300


def test_control_characters_and_absurd_dates_are_dropped() -> None:
    """Ruling P6-K (a): a NUL in an RFC 2047 From, in a Message-ID or a threading id never
    comes out of the parser (Postgres refuses NUL), and a year-9999 Date counts as missing."""
    raw = (
        b"From: =?utf-8?b?eABAZXZpbC5leGFtcGxl?=\r\n"
        b"To: sales@send-domain.com, =?utf-8?b?YQBAYi5j?=\r\n"
        b"Subject: hi\r\n"
        b"Message-ID: <a\x00b@x>\r\n"
        b"In-Reply-To: <c\x01d@x>\r\n"
        b"References: <ok@x> <e\x7ff@x>\r\n"
        b"Date: Fri, 31 Dec 9999 23:59:00 -0500\r\n\r\nplease unsubscribe\x00 me\r\n"
    )
    parsed = parse_mail(raw, max_body_chars=1000)
    assert (parsed.from_address, parsed.from_local, parsed.message_id) == (None, "", None)
    assert (parsed.in_reply_to, parsed.references) == ([], ["<ok@x>"])
    assert parsed.to_addresses == ["sales@send-domain.com"]
    assert parsed.date is None
    assert "\x00" not in parsed.text and parsed.subject == "hi"


def test_raw_utf8_headers_decode_to_text_not_surrogates() -> None:
    """RFC 6532 raw UTF-8 in To/Cc and threading headers comes back as text (C1), normalized
    like any other IDN address; undecodable bytes become U+FFFD, never a lone surrogate.
    Message ids are printable ASCII only (final-wave M5, the same rule ``mime`` builds with),
    so a non-ASCII one is dropped rather than later refused as our answer's In-Reply-To."""
    raw = (
        "From: Jane <jane.doe@acme-soap.com>\r\n"
        "To: sales@send-domain.com\r\n"
        "Cc: béx@acme-soap.com, JANE@MÜNCHEN.DE\r\n"
        "Subject: café\r\n"
        "In-Reply-To: <ü@x>\r\n"
        "References: <ok@x> <é@y>\r\n\r\nhi\r\n"
    ).encode() + b"X-Junk: \xff\r\n"
    parsed = parse_mail(raw, max_body_chars=1000)
    assert parsed.cc_addresses == ["béx@acme-soap.com", "jane@münchen.de"]
    assert (parsed.in_reply_to, parsed.references) == ([], ["<ok@x>"])
    assert parsed.subject == "café"
    bad = parse_mail(b"From: J <j@a.com>\r\nCc: b\xffx@a.com\r\n\r\nx", max_body_chars=100)
    assert bad.cc_addresses == ["b�x@a.com"]
    for value in [*parsed.cc_addresses, *parsed.references, *bad.cc_addresses]:
        value.encode("utf-8")  # no lone surrogates


def test_message_ids_are_printable_ascii_like_the_ones_we_build() -> None:
    """Final-wave M5: whatever parse keeps as an id, ``build_message`` accepts as In-Reply-To
    and References (and the unstructured-bounce Message-ID search follows the same rule)."""
    from salesfinder.mail.mime import message_id_ok

    raw = (
        "From: J <j@a.com>\r\nMessage-ID: <ok.1@x>\r\n"
        "References: <a\x7fb@x> <ok@x> <\u00e9@y> <tab\there@x>\r\n\r\n"
        "Delivery failed for jane@acme.com 5.1.1\r\nMessage-ID: <qu\u00f6te@x>\r\n"
    ).encode()
    parsed = parse_mail(raw, max_body_chars=1000)
    assert parsed.message_id == "<ok.1@x>"
    assert parsed.references == ["<ok@x>"]
    for mid in [parsed.message_id, *parsed.references]:
        assert message_id_ok(mid)
    bounce = bounce_of(
        dataclasses.replace(parsed, from_local="mailer-daemon"), known_recipients={"jane@acme.com"}
    )
    assert bounce is not None and bounce.original_message_id is None
