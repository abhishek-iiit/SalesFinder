"""Parse inbound mail: headers, text, bounces (DSN), auto-replies, quoted text, unsubscribes.

Never raises on malformed input (missing fields come back empty). Nothing here logs: subjects,
bodies and server text are untrusted.
"""

import email
import email.policy
import re
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Literal

from selectolax.lexbor import LexborHTMLParser

from salesfinder.auth.emails import normalize_email
from salesfinder.mail.mime import ID_CHARS

MAX_SUBJECT = 300
MAX_DIAGNOSTIC = 200
MAX_HEADER_CHARS = 4096
MAX_RAW_BYTES = 1_000_000
MAX_RAW_ID_HEADER = 65_536
MAX_RAW_ADDR_HEADER = 32_768
MAX_IDS = 100
MAX_DEPTH = 10
MAX_PARTS = 50
MAX_MATCH_LINE = 1000
MAX_SCAN_CHARS = 20_000
# Printable ASCII only (``mime.ID_CHARS``: no whitespace, control characters, non-ASCII or lone
# surrogates): any other id is dropped, so every id we store can be sent back in our
# In-Reply-To/References, and none reaches the database with a NUL.
_ID_RE = re.compile(rf"<{ID_CHARS}+>")
_QUOTED_ID_RE = re.compile(rf"^message-id:[ \t]*(<{ID_CHARS}+>)(?!\S)", re.I | re.M)
_ANY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
# Dates outside these years are treated as missing (arithmetic near datetime.max overflows).
MIN_YEAR, MAX_YEAR = 1900, 9000
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+'\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,8}"
)
_STATUS_RE = re.compile(r"^([245])\.\d{1,3}\.\d{1,3}$")
_FIVE_CODE_RE = re.compile(r"\b5\.\d{1,3}\.\d{1,3}\b")
_BOUNCE_PHRASES = (
    "address not found",
    "user unknown",
    "no such user",
    "does not exist",
    "mailbox unavailable",
    "recipient address rejected",
)
_BOUNCE_LOCALS = ("mailer-daemon", "postmaster")
_AUTO_SUBJECTS = ("automatic reply", "auto:", "autoreply", "out of office", "out of the office")
_AUTO_PRECEDENCE = {"auto_reply", "bulk", "junk", "list"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


@dataclass(frozen=True)
class DsnRecipient:
    address: str
    action: str
    status: str
    diagnostic: str


@dataclass(frozen=True)
class DsnReport:
    recipients: list[DsnRecipient]
    original_message_id: str | None
    structured: bool


@dataclass(frozen=True)
class ParsedMail:
    message_id: str | None
    in_reply_to: list[str]
    references: list[str]
    from_address: str | None
    from_local: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    date: datetime | None
    headers_auto: bool
    text: str
    new_text: str
    report: DsnReport | None


@dataclass(frozen=True)
class Bounce:
    kind: Literal["hard", "soft", "unclear"]
    recipient: str | None
    status: str | None
    original_message_id: str | None


_PART_MARK_RE = re.compile(rb"\n--[^\n]{1,200}\n", re.M)


def _cap_raw(raw: bytes) -> bytes:
    """Bound what the stdlib parser sees: at most MAX_RAW_BYTES, and stop at the ~2*MAX_PARTS-th
    MIME boundary line (parts beyond MAX_PARTS are ignored anyway; parsing them costs seconds)."""
    raw = raw[:MAX_RAW_BYTES]
    for count, match in enumerate(_PART_MARK_RE.finditer(raw), 1):
        if count > MAX_PARTS * 2:
            return raw[: match.start()]
    return raw


def _raw_header(msg: Message, name: str) -> str | None:
    """The header's raw (unparsed) text, or None if absent. Cheap whatever its size."""
    lowered = name.lower()
    for key, raw in msg.raw_items():
        if key.lower() == lowered:
            return _unescape(str(raw))
    return None


def _unescape(value: str) -> str:
    """Raw 8-bit header bytes (UTF-8 under RFC 6532, e.g. ``Cc: béx@acme-soap.com``) come
    back from the parser as lone surrogates; turn them back into text (undecodable bytes
    become U+FFFD) so nothing downstream sees a surrogate."""
    try:
        return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeError:
        return value.encode("utf-8", "replace").decode("utf-8", "replace")


def _long_ids(msg: Message, name: str) -> list[str]:
    """Threading ids come from the raw header (never dropped): the last MAX_IDS of them."""
    try:
        raw = _raw_header(msg, name) or ""
        return _ids(raw[-MAX_RAW_ID_HEADER:])[-MAX_IDS:]
    except Exception:
        return []


def _long_addresses(msg: Message, name: str) -> list[str]:
    """To/Cc: parse as much of the raw header as fits the bound instead of dropping it."""
    try:
        raw = " ".join((_raw_header(msg, name) or "").split())
        if len(raw) > MAX_RAW_ADDR_HEADER:
            raw = raw[:MAX_RAW_ADDR_HEADER].rpartition(",")[0]
        return _addresses(raw)
    except Exception:
        return []


def _header(msg: Message, name: str) -> str:
    try:
        raw_value = _raw_header(msg, name)
        if raw_value is None or len(raw_value) > MAX_HEADER_CHARS:  # hostile: slow to parse
            return ""
        value = msg.get(name)
        return "" if value is None else str(value)
    except Exception:
        return ""


def _one_line(value: str, limit: int) -> str:
    return " ".join(_CONTROL_RE.sub(" ", value).split())[:limit]


def _normalize_address(raw: str) -> str | None:
    candidate = raw.strip().strip("<>").lower()
    if "@" not in candidate or _ANY_CONTROL_RE.search(candidate):
        return None
    try:
        return normalize_email(candidate)
    except ValueError:
        return candidate if " " not in candidate else None


def _addresses(value: str) -> list[str]:
    try:
        pairs = getaddresses([value])
    except Exception:
        return []
    found: list[str] = []
    for _name, addr in pairs:
        norm = _normalize_address(addr)
        if norm and norm not in found:
            found.append(norm)
    return found


def _ids(value: str) -> list[str]:
    return _ID_RE.findall(value)


def _decode(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _walk(msg: Message) -> Iterator[Message]:
    """Like ``msg.walk()`` but at most MAX_DEPTH deep and MAX_PARTS parts."""
    stack: list[tuple[Message, int]] = [(msg, 0)]
    count = 0
    while stack and count < MAX_PARTS:
        part, depth = stack.pop()
        count += 1
        yield part
        if part.is_multipart() and depth < MAX_DEPTH:
            payload = part.get_payload()
            if isinstance(payload, list):
                stack.extend((p, depth + 1) for p in reversed(payload) if isinstance(p, Message))


def _html_to_text(html: str, limit: int) -> str:
    try:
        tree = LexborHTMLParser(html[: limit * 10])
        tree.strip_tags(["script", "style", "head"])
        body = tree.body
        return str(body.text(separator="\n", strip=True)) if body is not None else ""
    except Exception:
        return ""


def _body_text(msg: Message, limit: int) -> str:
    plain = html = None
    for part in _walk(msg):
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode(part)
        elif ctype == "text/html" and html is None:
            html = _decode(part)
    if plain is not None and plain.strip():
        text = plain
    elif html is not None:
        text = _html_to_text(html, limit)
    else:
        text = plain or ""
    return _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _headers_auto(msg: Message) -> bool:
    auto = _header(msg, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if _header(msg, "X-Autoreply") or _header(msg, "X-Autorespond"):
        return True
    return _header(msg, "Precedence").strip().lower() in _AUTO_PRECEDENCE


def _original_message_id(msg: Message) -> str | None:
    for part in _walk(msg):
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload()
            if isinstance(inner, list) and inner and isinstance(inner[0], Message):
                ids = _ids(_header(inner[0], "Message-ID"))
                if ids:
                    return ids[0]
        elif ctype in ("text/rfc822-headers", "text/rfc822-header"):
            ids = _ids(_header(email.message_from_string(_decode(part)), "Message-ID"))
            if ids:
                return ids[0]
    return None


def _dsn_blocks(part: Message) -> list[Message]:
    payload = part.get_payload()
    if isinstance(payload, list):
        return [b for b in payload if isinstance(b, Message)]
    if isinstance(payload, str):
        return [
            email.message_from_string(chunk)
            for chunk in re.split(r"\n\s*\n", payload)
            if chunk.strip()
        ]
    return []


def _build_report(msg: Message) -> DsnReport | None:
    is_report = msg.get_content_type() == "multipart/report" and (
        (msg.get_param("report-type") or "").lower() == "delivery-status"  # type: ignore[union-attr]
    )
    status_parts = [p for p in _walk(msg) if p.get_content_type() == "message/delivery-status"]
    if not is_report and not status_parts:
        return None
    recipients: list[DsnRecipient] = []
    for part in status_parts:
        for block in _dsn_blocks(part)[1:]:  # first block describes the message, not a recipient
            raw = _header(block, "Final-Recipient") or _header(block, "Original-Recipient")
            addr = _normalize_address(raw.rpartition(";")[2]) if raw else None
            if not addr:
                continue
            recipients.append(
                DsnRecipient(
                    address=addr,
                    action=_header(block, "Action").strip().lower(),
                    status=_header(block, "Status").strip(),
                    diagnostic=_one_line(_header(block, "Diagnostic-Code"), MAX_DIAGNOSTIC),
                )
            )
    return DsnReport(
        recipients=recipients,
        original_message_id=_original_message_id(msg),
        structured=any(r.action or r.status for r in recipients),
    )


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def parse_mail(raw: bytes, *, max_body_chars: int) -> ParsedMail:
    msg: Message
    try:
        msg = email.message_from_bytes(_cap_raw(raw), policy=email.policy.default)
    except Exception:
        msg = Message()
    try:
        ids = _ids(_header(msg, "Message-ID"))
        from_list = _addresses(_header(msg, "From"))
        from_address = from_list[0] if from_list else None
        text = _body_text(msg, max_body_chars)[:max_body_chars]
        try:
            report = _build_report(msg)
        except Exception:
            report = None
        return ParsedMail(
            message_id=ids[0] if ids else None,
            in_reply_to=_long_ids(msg, "In-Reply-To"),
            references=_long_ids(msg, "References"),
            from_address=from_address,
            from_local=from_address.rpartition("@")[0] if from_address else "",
            to_addresses=_long_addresses(msg, "To"),
            cc_addresses=_long_addresses(msg, "Cc"),
            subject=_one_line(_header(msg, "Subject"), MAX_SUBJECT),
            date=_parse_date(_header(msg, "Date")),
            headers_auto=_headers_auto(msg),
            text=text,
            new_text=strip_quoted(text),
            report=report,
        )
    except Exception:
        return ParsedMail(None, [], [], None, "", [], [], "", None, False, "", "", None)


def bounce_of(parsed: ParsedMail, *, known_recipients: Collection[str]) -> Bounce | None:
    report = parsed.report
    if report is None and parsed.from_local not in _BOUNCE_LOCALS:
        return None
    original = report.original_message_id if report else None
    known = {a.lower() for a in known_recipients}

    if report is not None and report.structured:
        ours = {r.address for r in report.recipients} & known
        if len(ours) != 1:  # none of ours (e.g. a forwarded chain) or several: don't guess
            return Bounce("unclear", None, None, original)
        rec = next(r for r in report.recipients if r.address in ours)
        match = _STATUS_RE.match(rec.status)
        if rec.action == "failed" and match and match[1] == "5":
            return Bounce("hard", rec.address, rec.status, original)
        if rec.action == "delayed" or (match and match[1] == "4"):
            return Bounce("soft", rec.address, rec.status or None, original)
        return Bounce("unclear", rec.address, rec.status or None, original)

    # Unstructured: a status code or a well-known phrase, and exactly one of our recipients.
    haystack = f"{parsed.subject}\n{parsed.text}"[:MAX_SCAN_CHARS]
    lowered = haystack.lower()
    code = _FIVE_CODE_RE.search(haystack)
    hinted = code is not None or any(p in lowered for p in _BOUNCE_PHRASES)
    mentioned = {a.lower().rstrip(".") for a in _EMAIL_RE.findall(haystack)} & known
    if original is None:
        found = _QUOTED_ID_RE.search(parsed.text)
        original = found[1] if found else None
    if hinted and len(mentioned) == 1:
        return Bounce("hard", next(iter(mentioned)), code[0] if code else None, original)
    return Bounce("unclear", None, code[0] if code else None, original)


def is_auto_reply(parsed: ParsedMail) -> bool:
    if parsed.headers_auto:
        return True
    subject = parsed.subject.lower().lstrip()
    return subject.startswith(_AUTO_SUBJECTS)


_ON_WROTE_RE = re.compile(r"^On .{1,200}wrote:\s*$")
_ORIGINAL_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I)
_UNDERSCORES_RE = re.compile(r"^_{10,}$")
_FROM_RE = re.compile(r"^From:\s")
_SENT_RE = re.compile(r"^(Sent|Date):")


def _quote_starts(lines: list[str], i: int) -> bool:
    line = lines[i].strip()
    if len(line) > MAX_MATCH_LINE:  # a quote marker is never this long
        return False
    if _ON_WROTE_RE.match(line) or _ORIGINAL_RE.match(line) or _UNDERSCORES_RE.match(line):
        return True
    if line.startswith("On ") and i + 1 < len(lines):
        if _ON_WROTE_RE.match(f"{line} {lines[i + 1].strip()[:MAX_MATCH_LINE]}"):
            return True
    if _FROM_RE.match(line):
        return any(_SENT_RE.match(nxt.strip()[:MAX_MATCH_LINE]) for nxt in lines[i + 1 : i + 5])
    return False


def strip_quoted(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    cut = next((i for i in range(len(lines)) if _quote_starts(lines, i)), len(lines))
    kept = [ln for ln in lines[:cut] if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


_UNSUB_RE = re.compile(
    r"\b(unsubscribe|remove me|opt[\s-]?out|take me off|stop (emailing|sending|contacting)"
    r"|do not (email|contact)|don'?t (email|contact))\b",
    re.I,
)


UNSUB_WINDOW = 500  # characters of their new text searched for an opt-out


def _without_ours(text: str, ours: Collection[str]) -> str:
    """``text`` with its whitespace collapsed and every occurrence of our own lines (the
    footer's opt-out line, which quotes "unsubscribe"; any case, however re-wrapped) cut."""
    flat = " ".join(text.split())
    for line in ours:
        needle = " ".join(line.split())
        if needle:
            flat = re.sub(re.escape(needle), " ", flat, flags=re.I)
    return flat


def is_unsubscribe(subject: str, new_text: str, *, ours: Collection[str] = ()) -> bool:
    """An opt-out in the subject or in the first ``UNSUB_WINDOW`` characters of their new
    text. ``ours`` are lines of our own email (``writing.opt_out_line``) removed from the
    text first: a quoted copy of our email that ``strip_quoted`` didn't recognise must not
    count as their unsubscribe."""
    if _UNSUB_RE.search(subject):
        return True
    return bool(_UNSUB_RE.search(_without_ours(new_text, ours)[:UNSUB_WINDOW]))
