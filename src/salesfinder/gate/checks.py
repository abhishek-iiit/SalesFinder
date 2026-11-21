"""The pre-send gate (spec §6.4): every draft passes it before teammates see it as ready, again at
approval, and again before sending.

``content_problems`` is pure: placeholders, length, banned phrases, prices, certifications we
haven't confirmed, links and email addresses, the signature and compliance footer, and echoes of
our own instructions. ``recipient_problems`` reads live state (never writes): exclusions and
disqualification, the chosen contact (the sequence's own person) and its sendable status, MX
freshness, and other sequences to the same address.
Each problem is a short sentence a teammate can act on; an empty list means the draft may go
ahead. The checks are tuned for recall: a false alarm costs a teammate an edit, a miss can put a
false claim or a stranger's link in front of a customer.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.auth.emails import normalize_email
from salesfinder.catalog import Facts, Side
from salesfinder.config import AppConfig, WritingConfig
from salesfinder.contacts.dns import mx_domain, mx_is_stale
from salesfinder.contacts.service import unsendable_reason
from salesfinder.db.models import Company, DomainMx, EmailAddress, Person, Sequence
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email, normalize_domain
from salesfinder.exclusions.check import exclusion_for
from salesfinder.signals.detect_common import CERT_NAMES, CERT_PATTERNS, normalize_dashes
from salesfinder.writer.compose import footer, signature

__all__ = [
    "ALREADY_CONTACTED",
    "DISQUALIFIED",
    "FOOTER_CHANGED",
    "INVALID_RECIPIENT",
    "MX_OUT_OF_DATE",
    "NON_ASCII_LOCAL",
    "NOT_CHOSEN",
    "SIGNATURE_CHANGED",
    "content_problems",
    "gate_message",
    "recipient_problems",
    "word_limit",
]

SUBJECT_EMPTY = "Subject is empty."
PLACEHOLDER = "Unfilled placeholder left in the email."
PRICE = "Mentions a price or currency amount."
MORE_THAN_ONE_LINK = "Has more than one link."
SIGNATURE_CHANGED = "Signature is missing or changed."
FOOTER_CHANGED = "Footer is missing or changed."
POSTAL_UNCONFIRMED = "Postal address isn't confirmed in facts.yaml."
ECHO = "Looks like it repeats our instructions."
INVALID_RECIPIENT = "Recipient address is invalid."
# We send without SMTPUTF8 (one address form on the wire), so the local part must be ASCII.
NON_ASCII_LOCAL = "Recipient address has non-ASCII characters before the @; we can't send to it."
NOT_CHOSEN = "Recipient isn't one of our chosen contacts."
# Shown when the recipient domain's mail-server answer is missing, unknown or older than
# ``contacts.mx_cache_days``. Approve, Re-check and Edit look it up again first, so this
# usually means DNS couldn't answer just now.
MX_OUT_OF_DATE = "Couldn't confirm the recipient's mail server just now — try Re-check later."
DISQUALIFIED = "Company is disqualified (it doesn't fit our customer profile)."
ALREADY_CONTACTED = "Already being contacted by another campaign."


# Ideographic full stops read as "." (NFKC keeps them); U+FF0E is folded by NFKC itself.
_DOTS = str.maketrans({"\u3002": ".", "\uff61": "."})


def _nfkc(text: str) -> str:
    """Compatibility forms folded (fullwidth dollar sign and full stop, ligatures), invisible
    format characters (zero-width space, soft hyphen, ...) dropped, ideographic full stops
    and dashes made "." and "-"."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return normalize_dashes(text.translate(_DOTS))


# --------------------------------------------------------------------------- placeholders

_NAME_WORDS = r"(?:first|last|full|company|recipient|contact)[\s_]*name"
_PLACEHOLDER_RE = re.compile(
    r"\{\{|\}\}|\{\s*first|\{\s*name|\{\s*[A-Za-z_][\w ]{0,40}\}|<<|>>|(?<![\d.])%\s*first"
    rf"|<\w+_\w+>|<\s*{_NAME_WORDS}\s*>|\(\s*{_NAME_WORDS}\s*\)"
    r"|\[\s*(?:your[\s_]*)?(?:first[\s_]*)?name\s*\]|\[\s*company(?:[\s_]*name)?\s*\]"
    r"|\[\s*(?:insert|recipient|title|company|first|last|full|your|contact)\b[^\]\n]{0,30}\]"
    r"|(?<![A-Za-z0-9])x{3,}(?![A-Za-z0-9])"
    r"|(?-i:\bTODO\b|\[[A-Z][A-Za-z _]{1,30}\](?!\()|<[A-Z][a-z]+[A-Z]\w*>)",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- prices

_CODES = r"(?:USD|INR|EUR|GBP|CAD|AUD|SGD|AED|CNY|RMB|JPY|CHF|MXN|Rs\.?)"
_PRICE_RE = re.compile(
    r"[$₹€£¥]"
    rf"|(?<![A-Za-z]){_CODES}\s?\d"
    rf"|\dk?\s?{_CODES}(?![A-Za-z])"
    r"|\d\s*%\s*off\b"
    r"|\b(?:dollars?|(?<!per\s)cents?|bucks(?!\s+county)|rupees?|euros?|paise|pounds\s+sterling"
    r"|yuan|renminbi|dirhams?|pesos?|ringgit|baht)\b"
    # Also names and ordinary words ("Ayn Rand", "a yen for"): only right after a number.
    r"|\d\s*(?:rand|yen|dong)\b",
    re.IGNORECASE,
)
# A rate on weight or volume ("per kg", "a tonne") makes any nearby number a price; a rate on a
# container ("per drum") only a decimal or a number next to a price word ("packed 4 per drum"
# is a count).
_WEIGHT_UNIT_RE = re.compile(
    r"^(?:kgs?|kilos?|kilograms?|g|grams?|mts?|m\.t|t|tons?|tonnes?|lbs?|pounds?|litres?|liters?"
    r"|l|ml|gallons?|gal)$"
)
_CONTAINER_UNIT_RE = re.compile(
    r"^(?:drums?|bags?|pallets?|totes?|ibcs?|pails?|containers?|units?|box(?:es)?|cartons?)$"
)
# "4,500", "4.5k", and ranges "880-900" (dashes are normalized to "-").
_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)*k?(?:-\d+(?:[.,]\d+)*k?)?$")
# Lead times and counts aren't money: "a quote within 24 hours", "2 weeks per 20 MT lot".
_NOT_MONEY_RE = re.compile(
    r"^(?:hours?|hrs?|days?|weeks?|months?|years?|business|working|skus?|trucks?|truckloads?"
    r"|containers?|samples?|products?|items?|grades?|plants?|customers?|people|lots?"
    r"|batch(?:es)?|loads?|orders?|shipments?|percent)$"
)
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_SPELLED = frozenset(
    "one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty "
    "forty fifty sixty seventy eighty ninety hundred thousand".split()
)
_PRICE_WORDS = frozenset(
    {"price", "prices", "priced", "pricing", "cost", "costs", "rate", "rates", "quote", "quoted"}
    | {"charge", "charges", "pay", "at", "only", "just", "offer"}
)
# A price noun followed closely by a bare number: "our price is 4,500 for the container".
_PRICE_NOUNS = frozenset(
    {"price", "prices", "priced", "pricing", "cost", "costs", "quote", "rate", "rates"}
)
_PRICE_WINDOW = 6  # words either side of a "per kg"
_RATE_GAP = 2  # words allowed between "per" and its unit: "per metric ton", "per 1 kg"
# A rate and its number sit in one clause: "10/12 drums, version 2.5" isn't a price. A comma or
# a single line break right before "per"/"/" doesn't end the clause ("900,\nper MT").
_CLAUSE_RE = re.compile(r"[.;!?](?=\s|$)|\n\s*\n|,(?=\s|$)(?!\s*(?:per|/))|\n(?!\s*(?:per|/))")


def _tokens(text: str) -> list[str]:
    text = re.sub(r"\bper\s*cent\b", "percent", text, flags=re.I)
    text = re.sub(r"\bper-(?=[^\W\d_])", "per ", text, flags=re.I)  # "per-kg"
    return [t.strip(",;:!?()[]{}\"'*_").strip(".").lower() for t in re.findall(r"/|[^\s/]+", text)]


def _is_unit(token: str) -> bool:
    return bool(_WEIGHT_UNIT_RE.match(token) or _CONTAINER_UNIT_RE.match(token))


def _rate_unit(tokens: list[str], i: int) -> tuple[int, bool] | None:
    """For a rate word at ``i``: the index of its unit and whether it is a weight/volume
    unit. "per"/"/" may have up to two words before the unit ("per metric ton", "per 1 kg");
    "a"/"an" only directly ("4.50 a kg", not "a 200 kg drum", which is a quantity)."""
    word = tokens[i]
    if word in ("a", "an"):
        reach = 1
    elif word in ("per", "/"):
        reach = 1 + _RATE_GAP
    else:
        return None
    for j in range(i + 1, min(len(tokens), i + 1 + reach)):
        if _WEIGHT_UNIT_RE.match(tokens[j]):
            return j, True
        if _CONTAINER_UNIT_RE.match(tokens[j]):
            return j, False
    return None


def _rate_price(text: str) -> bool:
    """A number near a rate ("per kg", "/MT", "a tonne", "per metric ton") that isn't itself a
    quantity ("200 kg/drum"); see ``_rate_unit`` and the unit classes above."""
    return any(_clause_price(_tokens(clause)) for clause in _CLAUSE_RE.split(text))


def _clause_price(tokens: list[str]) -> bool:
    for i, tok in enumerate(tokens):
        if tok in _PRICE_NOUNS:
            for k in range(i + 1, min(len(tokens), i + 5)):
                if _NUMBER_RE.match(tokens[k]) and not _quantity(tokens, k):
                    if not _YEAR_RE.match(tokens[k]):
                        return True
        if tok == "each" and i > 0 and _NUMBER_RE.match(tokens[i - 1]):  # "4.50 each"
            before = tokens[max(0, i - 1 - _PRICE_WINDOW) : i - 1]
            if re.search(r"[.,]\d|k$", tokens[i - 1]) or _PRICE_WORDS.intersection(before):
                return True
        found = _rate_unit(tokens, i)
        if found is None:
            continue
        j, weight = found
        window = range(max(0, i - _PRICE_WINDOW), min(len(tokens), j + 1 + _PRICE_WINDOW))
        priced = any(tokens[k] in _PRICE_WORDS for k in window)
        for k in window:
            if i < k <= j or _quantity(tokens, k):
                continue
            number = bool(_NUMBER_RE.match(tokens[k]))
            if weight and (number or tokens[k] in _SPELLED):
                return True
            if number and (priced or re.search(r"[.,]\d|k$", tokens[k])):
                return True
    return False


def _quantity(tokens: list[str], k: int) -> bool:
    """A number followed by a unit or "%" ("200 kg", "40 bags", "70 %") is an amount; one
    followed by a time or count noun ("24 hours", "2 SKUs") isn't money either."""
    nxt = tokens[k + 1] if k + 1 < len(tokens) else ""
    return nxt == "%" or _is_unit(nxt) or bool(_NOT_MONEY_RE.match(nxt))


# Cyrillic letters that look Latin ("per kg" typed with Cyrillic e and k) read as Latin for the
# price words only (in a link a look-alike is a different domain, so links keep them).
_LOOKALIKES = str.maketrans(
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u043a\u043c\u0442\u0432\u043d"
    "\u0410\u0415\u041e\u0420\u0421\u0423\u0425\u041a\u041c\u0422\u0412\u041d",
    "aeopcyxkmtbhAEOPCYXKMTBH",
)


def _mentions_price(text: str) -> bool:
    text = text.translate(_LOOKALIKES)
    return bool(_PRICE_RE.search(text)) or _rate_price(text)


# --------------------------------------------------------------------------- banned phrases


def _banned_hit(text: str, phrases: Iterable[str]) -> str | None:
    """The banned phrase found earliest in ``text`` (whole phrase, any case, any spacing;
    "100 %" and "100 percent" read as "100%")."""
    text = re.sub(r"\s*%|\s+percent\b", "%", text.replace("\u2019", "'"), flags=re.IGNORECASE)
    best: tuple[int, str] | None = None
    for phrase in phrases:
        words = re.sub(r"\s*%", "%", phrase.replace("\u2019", "'")).split()
        if not words:
            continue
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), phrase)
    return best[1] if best else None


# --------------------------------------------------------------------------- certifications

_W = r"\W+(?:\w+\W+)"  # a few words between two terms: _W + "{0,3}"
_NOT_FSSC = r"(?<!fssc)(?<!fssc )(?<!fssc-)"
# High recall for drafts (CERT_PATTERNS, tuned for web pages, still decide what's confirmed).
_GATE_CERTS: dict[str, re.Pattern[str]] = {
    "ISO 9001": re.compile(r"(?<!\d)(?:iso\W{0,3}9\s?001|9001)(?!\d)", re.I),
    "ISO 14001": re.compile(r"(?<!\d)(?:iso\W{0,3}14\s?001|14001)(?!\d)", re.I),
    "ISO 45001": re.compile(r"(?<!\d)(?:iso\W{0,3}45\s?001|45001)(?!\d)", re.I),
    "ISO 22000": re.compile(rf"(?<!\d)(?:iso\W{{0,3}}22\s?000|{_NOT_FSSC}22000)(?!\d)", re.I),
    "FSSC 22000": re.compile(r"\bfssc", re.I),
    "GMP": re.compile(r"\bc?gmp\b|good\s+manufacturing\s+practices?", re.I),
    "NSF": re.compile(r"\bnsf\b", re.I),
    "REACH": re.compile(
        r"\breach[\s-]+(?:regist\w*|complian\w*|certified|ready|approved)"
        r"|(?-i:(?:regist\w*|complian\w*|certified)\W+(?:\w+\W+){0,2}REACH\b)",
        re.I,
    ),
    "Kosher": re.compile(r"\bkosher\b", re.I),
    "Halal": re.compile(r"\bhala+l\b", re.I),
    "RSPO": re.compile(r"\brspo\b", re.I),
    "ECOCERT": re.compile(r"\becocert\b", re.I),
    "COSMOS": re.compile(r"\bcosmos\b", re.I),
    "USDA Organic": re.compile(
        rf"\b(?:usda|certified){_W}{{0,3}}organic\b|\borganic{_W}{{0,3}}(?:usda|certified)\b",
        re.I,
    ),
    "FDA registered": re.compile(rf"\bfda{_W}{{0,4}}regist|\bregist\w*{_W}{{0,4}}fda\b", re.I),
    "BRC": re.compile(r"\bbrc(?:gs)?\b", re.I),
}
if tuple(_GATE_CERTS) != CERT_NAMES:  # pragma: no cover - a new detector cert needs a pattern
    raise RuntimeError("gate cert patterns must cover CERT_NAMES")
_ANY_ISO_RE = re.compile(r"\biso\W{0,3}(\d{4,5})(?!\d)", re.I)
_NAMED_ISO = frozenset({"9001", "14001", "45001", "22000"})
# Claims outside the detector vocabulary; allowed only if the same pattern matches a confirmed
# claim.
_EXTRA_CERTS: dict[str, re.Pattern[str]] = {
    "FDA approved": re.compile(
        rf"\bfda{_W}{{0,4}}(?:approv|clear)|\b(?:approv|clear)\w*{_W}{{0,4}}fda\b", re.I
    ),
    "HACCP": re.compile(r"\bhaccp\b", re.I),
    "GFSI": re.compile(r"\bgfsi\b", re.I),
    "SQF": re.compile(r"\bsqf\b", re.I),
    "Sedex/SMETA": re.compile(r"\bsedex\b|\bsmeta\b", re.I),
    "EcoVadis": re.compile(r"\becovadis\b", re.I),
}
# "ISO 14OO1" (letter O for zero) reads as "ISO 14001".
_O_DIGITS_RE = re.compile(r"\b(?=[\dOo]*\d)[\dOo]{4,6}\b")


def _fix_o_digits(text: str) -> str:
    return _O_DIGITS_RE.sub(lambda m: m.group().replace("o", "0").replace("O", "0"), text)


def _unconfirmed_cert(text: str, facts: Facts) -> str | None:
    confirmed = _nfkc("\n".join(c.text for c in facts.claims if c.confirmed))
    text = _fix_o_digits(text)
    for name, rx in _GATE_CERTS.items():
        if rx.search(text) and not CERT_PATTERNS[name].search(confirmed):
            return name
    for name, rx in _EXTRA_CERTS.items():
        if rx.search(text) and not rx.search(confirmed):
            return name
    confirmed_iso = set(_ANY_ISO_RE.findall(confirmed))
    for number in _ANY_ISO_RE.findall(text):
        if number not in _NAMED_ISO and number not in confirmed_iso:
            return f"ISO {number}"
    return None


# --------------------------------------------------------------------------- links and emails

_URL_CHARS = r"[^\s<>\"'()\[\]{}]"
_URL_RE = re.compile(rf"(?<![\w:/])//{_URL_CHARS}+")
# Any scheme ("https:", "ftp:"), then a protocol-relative "//host".
_SCHEME_URL_RE = re.compile(
    rf"\b[a-z][a-z0-9+.-]{{0,15}}://(?:\[[0-9a-f:.]+\]{_URL_CHARS}*|{_URL_CHARS}+)", re.I
)
# Script and inline-document links have no host: the scheme stands for it.
_SCRIPT_URL_RE = re.compile(
    r"\b(?:javascript|vbscript):\S+|\bdata:[a-z]+/[\w.+-]+[;,]\S*", re.IGNORECASE
)
_WWW_RE = re.compile(rf"(?<![\w.@-])www\.{_URL_CHARS}+", re.I)
# Anchored at the start of a run (and capped) so a long run without "@" is scanned once.
_EMAIL_RE = re.compile(r"(?<![\w.+'-])[\w.+'-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_IPV4_RE = re.compile(rf"(?<![\w.])(?:\d{{1,3}}\.){{3}}\d{{1,3}}(?![\w.]\w)(?:/{_URL_CHARS}*)?")
_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
# "_evil.com_" is a link: an underscore (markdown emphasis) doesn't glue a label to a word.
_BARE_RE = re.compile(
    rf"(?<![^\W_])(?<![.@-])(?:{_LABEL}\.)+[^\W\d_]{{2,63}}(?![^\W_])(?![@-])"
    rf"(?:[/?#]{_URL_CHARS}*)?"
)
_TRAILING = ".,;:!?*_'\"\u201d\u2019"
_DEOBFUSCATE = (
    (re.compile(r"\s*[\[(]\s*(?:\.|dot)\s*[\])]\s*", re.I), "."),
    (re.compile(r"(?<=\w)\s+dot\s+(?=(?:com|net|org|io|co|in|us|uk|info|biz)\b)", re.I), "."),
)


@dataclass(frozen=True)
class _Link:
    host: str
    path: str  # everything after the host: "/x?y"
    domain: str | None  # registrable domain; None for an IP or unknown suffix


def _parse_link(raw: str) -> _Link:
    """Host is what follows the last "@" of the authority; "\\" ends it like "/" (browsers)."""
    rest = re.sub(r"^(?:[a-z][a-z0-9+.-]*:)?//", "", raw, flags=re.I)
    cut = re.search(r"[/?#\\]", rest)
    authority, path = (rest[: cut.start()], rest[cut.start() :]) if cut else (rest, "")
    host = authority.rpartition("@")[2]
    host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
    host = host.lower().rstrip(".")
    return _Link(host or raw, path.replace("\\", "/"), normalize_domain(host) if host else None)


def _blank(text: str, m: re.Match[str]) -> str:
    return text[: m.start()] + " " * (m.end() - m.start()) + text[m.end() :]


def _scan(text: str) -> tuple[list[_Link], list[str]]:
    """Every link and every email address in ``text``, each in order of appearance. URLs are
    found before email addresses, so ``https://elchemy.com@evil.com`` is a link to evil.com,
    and an "address" followed by a path (``elchemy.com@evil.com/x``) is a link too."""
    for rx, repl in _DEOBFUSCATE:
        text = rx.sub(repl, text)
    links: list[tuple[int, _Link]] = []
    emails: list[tuple[int, str]] = []
    for m in list(_SCRIPT_URL_RE.finditer(text)):
        scheme = m.group().split(":", 1)[0].lower() + ":"
        links.append((m.start(), _Link(scheme, "", None)))
        text = _blank(text, m)
    for rx in (_SCHEME_URL_RE, _URL_RE, _WWW_RE):
        for m in list(rx.finditer(text)):
            links.append((m.start(), _parse_link(m.group().rstrip(_TRAILING))))
            text = _blank(text, m)
    for m in list(_EMAIL_RE.finditer(text)):
        after = text[m.end() : m.end() + 1]
        port = after == ":" and text[m.end() + 1 : m.end() + 2].isdigit()
        if after and (after in "/?#\\" or port):  # a path or port: a URL with userinfo
            tail = re.match(rf"{_URL_CHARS}*", text[m.end() :])
            raw = m.group() + (tail.group() if tail else "")
            links.append((m.start(), _parse_link(raw.rstrip(_TRAILING))))
        else:
            emails.append((m.start(), m.group().rstrip(_TRAILING).lower()))
        text = _blank(text, m)
    for m in list(_IPV4_RE.finditer(text)):
        links.append((m.start(), _parse_link(m.group().rstrip(_TRAILING))))
        text = _blank(text, m)
    for m in _BARE_RE.finditer(text):
        raw = m.group().rstrip(_TRAILING)
        link = _parse_link(raw)
        if link.domain is None:
            continue  # "co.uk", "node.js": not a domain anyone can own
        # Any registrable name counts, whatever its case or suffix ("Evil.Now", "Attacker.Zip"),
        # at the cost of flagging a missing space after a full stop ("Texas.now we ship").
        links.append((m.start(), link))
    return (
        [link for _, link in sorted(links, key=lambda f: f[0])],
        [e for _, e in sorted(emails)],
    )


def _booking_match(link: _Link, booking: _Link) -> bool:
    """The booking link itself or a page under it: same host, path equal or a sub-path. A path
    with a "." or ".." segment (also percent-encoded) never matches: it could climb out."""
    if link.host != booking.host:
        return False
    path = link.path.split("?", 1)[0].split("#", 1)[0]
    for _ in range(3):  # "%252e" decodes to "%2e", then to "."
        path = unquote(path)
    segments = [seg.split(";", 1)[0] for seg in path.replace("\\", "/").split("/")]
    if any(seg in (".", "..") for seg in segments):
        return False
    base = booking.path.rstrip("/")
    return (
        not base
        or link.path == base
        or (link.path.startswith(base) and link.path[len(base)] in "/?#")
    )


def _foreign_link(links: Iterable[_Link], facts: Facts, writing: WritingConfig) -> _Link | None:
    """The first link that isn't to our website (any page of its registrable domain) or to the
    configured booking page."""
    website = normalize_domain(facts.website)
    booking = _parse_link(writing.booking_url) if writing.booking_url else None
    for link in links:
        if link.domain is not None and link.domain == website:
            continue
        if booking is not None and _booking_match(link, booking):
            continue
        return link
    return None


# --------------------------------------------------------------------------- instruction echo

_ORDERS = r"(?:instructions?|prompts?|rules|directions?|guidelines|messages?\W+above)\b"
# Words that may sit between "ignore"/"disregard"/"forget" and "previous"/"above" (never a
# possessive: "please ignore my previous email" is an ordinary follow-up).
_DETERMINERS = r"(?:the|all|any|of|everything|anything|that|this|these|those)"
_INJECTION_RE = re.compile(
    rf"\b(?:ignore|disregard|forget)\W+(?:{_DETERMINERS}\W+){{0,3}}(?:previous|prior|above|before)\b"
    # A few words may sit in between only when instructions are named, so "please ignore my
    # previous email" in a follow-up is fine.
    rf"|\bignore\W+(?:\w+\W+){{0,3}}?(?:previous|prior|above|earlier)\W+{_ORDERS}"
    rf"|\bdisregard\W+(?:\w+\W+){{0,3}}?{_ORDERS}"
    rf"|\bforget\W+(?:(?:all|your|the|any|previous|prior|earlier)\W+){{0,3}}{_ORDERS}"
    r"|\bignore\W+everything\W+(?:before|above)"
    r"|\bsystem\s+prompt|(?<!\w)as\s+an\s+a\.?\s?i\b\.?"
    r"|\blanguage\s+model|</?\s*untrusted|&lt;/?\s*untrusted",
    re.IGNORECASE,
)
_UNTRUSTED_BLOCK_RE = re.compile(r"<untrusted\b[^>]*>.*?</untrusted\s*>", re.IGNORECASE | re.DOTALL)
_ECHO_WORDS = 8
# Facts the writer may repeat word for word are only masked out of the prompt when this long.
_MIN_ALLOWED_WORDS = 3


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _grams(words: list[str]) -> set[tuple[str, ...]]:
    n = _ECHO_WORDS
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _allowed_texts(facts: Facts, writing: WritingConfig) -> list[str]:
    """What the writer may legitimately repeat word for word from the prompt."""
    texts = [c.text for c in facts.claims if c.confirmed]
    texts += [facts.company_name, facts.website, facts.postal_address, writing.opt_out_line]
    texts += [w.label for w in facts.warehouses]
    for persona in facts.personas.values():
        texts += [persona.name, persona.title, persona.signature]
    if writing.booking_url:
        texts.append(writing.booking_url)
    return texts


def _instruction_grams(prompt_text: str, allowed: Iterable[str]) -> set[tuple[str, ...]]:
    """8-word runs of the prompt, leaving out the untrusted recipient data and our allowed
    facts (no run crosses either)."""
    allowed_runs = [w for t in allowed if len(w := _words(t)) >= _MIN_ALLOWED_WORDS]
    grams: set[tuple[str, ...]] = set()
    for segment in _UNTRUSTED_BLOCK_RE.split(_nfkc(prompt_text)):
        words = _words(segment)
        blocked = [False] * len(words)
        for run in allowed_runs:
            n = len(run)
            for i in range(len(words) - n + 1):
                if words[i] == run[0] and words[i : i + n] == run:
                    blocked[i : i + n] = [True] * n
        current: list[str] = []
        for word, is_blocked in zip(words, blocked, strict=True):
            if is_blocked:
                grams |= _grams(current)
                current = []
            else:
                current.append(word)
        grams |= _grams(current)
    return grams


def _echoes(
    subject: str, body: str, prompt_text: str, facts: Facts, writing: WritingConfig
) -> bool:
    if _INJECTION_RE.search(subject) or _INJECTION_RE.search(body):
        return True
    allowed = [_nfkc(t) for t in _allowed_texts(facts, writing)]
    instructions = _instruction_grams(prompt_text, allowed)
    if not instructions:
        return False
    return any(not instructions.isdisjoint(_grams(_words(t))) for t in (subject, body))


# --------------------------------------------------------------------------- content_problems


def _word_count(text: str) -> int:
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


def word_limit(step: int, writing: WritingConfig) -> int:
    """Most words for a step: the first email, the follow-up, or a reply (step 3 and up)."""
    if step == 1:
        return writing.first_email_max_words
    if step == 2:
        return writing.followup_max_words
    return writing.reply_max_words


def content_problems(
    subject: str,
    body: str,
    *,
    step: int,
    side: Side,
    recipient: str,
    facts: Facts,
    writing: WritingConfig,
    prompt_text: str,
) -> list[str]:
    """What's wrong with a draft's text, in a fixed order, each at most once.

    ``body`` is the full assembled body: the written part, then exactly ``signature(facts,
    side) + footer(facts, writing)``. When it doesn't end that way, every check runs on the
    whole body (nothing is trusted as "ours"). ``recipient`` is the address the draft goes to:
    the only email address besides our own domain's that the written part may mention."""
    problems: list[str] = []
    subject_text = subject.strip()
    if not subject_text:
        problems.append(SUBJECT_EMPTY)
    elif len(subject_text) > writing.subject_max_chars:
        problems.append(f"Subject is longer than {writing.subject_max_chars} characters.")

    tail = signature(facts, side) + footer(facts, writing)
    trimmed = body.rstrip()
    tail_ok = trimmed.endswith(tail.rstrip())
    if tail_ok:
        # A second copy inside the written part would let the draft pose as code-added text.
        head = trimmed[: len(trimmed) - len(tail.rstrip())]
        copied = (signature(facts, side).strip(), footer(facts, writing).strip())
        tail_ok = not any(part and part in head for part in copied)
    written = _nfkc(trimmed[: len(trimmed) - len(tail.rstrip())] if tail_ok else body)
    subject_n, body_n = _nfkc(subject), _nfkc(body)
    own = f"{subject_n}\n{written}"  # what the writer (Claude or a teammate) wrote

    if _PLACEHOLDER_RE.search(own):
        problems.append(PLACEHOLDER)
    limit = word_limit(step, writing)
    if _word_count(written) > limit:
        problems.append(f"Email is longer than {limit} words.")
    if (phrase := _banned_hit(own, writing.banned_phrases)) is not None:
        problems.append(f"Contains a banned phrase: “{phrase}”.")
    if _mentions_price(own):
        problems.append(PRICE)
    if (cert := _unconfirmed_cert(own, facts)) is not None:
        problems.append(f"Mentions “{cert}”, which isn't in our confirmed facts.")

    # The signature always carries our website, so only the written part counts toward "one".
    own_links, own_emails = _scan(own)
    if len(own_links) > 1:
        problems.append(MORE_THAN_ONE_LINK)
    all_links, _ = _scan(f"{subject_n}\n{body_n}")
    if (foreign := _foreign_link(all_links, facts, writing)) is not None:
        problems.append(f"Links to {foreign.host}, which isn't ours.")
    website = normalize_domain(facts.website)
    to = recipient.strip().lower()
    for address in own_emails:
        if address != to and domain_of_email(address) != website:
            problems.append(f"Mentions an email address that isn't ours: {address}.")
            break

    if not tail_ok:
        footer_ok = trimmed.endswith(footer(facts, writing).rstrip())
        problems.append(SIGNATURE_CHANGED if footer_ok else FOOTER_CHANGED)
    if not facts.postal_address_confirmed or not facts.postal_address.strip():
        problems.append(POSTAL_UNCONFIRMED)
    if _echoes(subject_n, body_n, prompt_text, facts, writing):
        problems.append(ECHO)
    return problems


# --------------------------------------------------------------------------- recipient checks

_EXCLUSION_LABELS = {
    "existing_relationship": "existing relationship",
    "do_not_contact": "do-not-contact",
}
_IN_PROGRESS = ("awaiting_approval", "active", "replied")


def _clause(text: str) -> str:
    """``text`` ready to end a sentence with our own full stop."""
    return text.strip().rstrip(".")


async def _live_address(db: AsyncSession, address: EmailAddress | None) -> EmailAddress | None:
    """A detached copy of the address row as it is in the database now (never the caller's
    possibly stale or modified object, and never added to the session); None unless it is
    linked to a person of its company who is still a target (one of our chosen contacts)."""
    if address is None:
        return None
    row = (
        await db.execute(
            select(
                EmailAddress.company_id,
                EmailAddress.person_id,
                EmailAddress.address,
                EmailAddress.status,
                EmailAddress.blocked_reason,
                EmailAddress.override_sendable,
                EmailAddress.stale,
                Person.is_target,
                Person.company_id.label("person_company_id"),
            )
            .outerjoin(Person, Person.id == EmailAddress.person_id)
            .where(EmailAddress.id == address.id)
        )
    ).one_or_none()
    if row is None or not row.is_target or row.person_company_id != row.company_id:
        return None
    return EmailAddress(
        id=address.id,
        company_id=row.company_id,
        person_id=row.person_id,
        address=row.address,
        status=row.status,
        blocked_reason=row.blocked_reason,
        override_sendable=row.override_sendable,
        stale=row.stale,
    )


async def _company_exclusion(db: AsyncSession, company: Company) -> str | None:
    live = (
        await db.execute(
            select(Company.status, Company.exclusion_reason, Company.exclusion_detail).where(
                Company.id == company.id
            )
        )
    ).one_or_none()
    status, reason, detail = (
        (live.status, live.exclusion_reason, live.exclusion_detail)
        if live is not None
        else (company.status, company.exclusion_reason, company.exclusion_detail)
    )
    if status != "excluded":
        return None
    why = detail or _EXCLUSION_LABELS.get(reason or "", reason or "excluded")
    return f"Company is excluded: {_clause(why)}."


async def _disqualified(db: AsyncSession, company: Company) -> bool:
    live = await db.scalar(select(Company.disqualified).where(Company.id == company.id))
    return bool(company.disqualified if live is None else live)


async def _in_other_sequence(
    db: AsyncSession, recipient: str, raw: str, sequence: Sequence | None
) -> bool:
    query = select(Sequence.id).where(
        Sequence.recipient.in_(sorted({recipient, raw.strip().lower()})),
        Sequence.status.in_(_IN_PROGRESS),
    )
    if sequence is not None:
        query = query.where(
            Sequence.id != sequence.id,
            ~(
                (Sequence.campaign_id == sequence.campaign_id)
                & (Sequence.company_id == sequence.company_id)
            ),
        )
    return (await db.scalar(query.limit(1))) is not None


async def recipient_problems(
    db: AsyncSession,
    *,
    sequence_or_none: Sequence | None,
    recipient: str,
    company: Company,
    address: EmailAddress | None,
    now: datetime,
    config: AppConfig,
) -> list[str]:
    """What stops this recipient from being emailed now, from live state (read only). A
    malformed recipient, or one with a non-ASCII local part (we never send SMTPUTF8), fails
    closed: nothing else is checked for it. A suppressed address (unsubscribed, bounced) is
    reported through the exclusion check: "Recipient is excluded: Unsubscribed on ...".

    Pass the message's sequence once it exists (approval, Task 7; sending): with
    ``sequence_or_none=None`` every in-progress sequence to this address counts as another
    campaign's, including this campaign's own; with it, the address must still belong to the
    sequence's own person."""
    problems: list[str] = []
    with db.no_autoflush:
        if (excluded := await _company_exclusion(db, company)) is not None:
            problems.append(excluded)
        if await _disqualified(db, company):
            problems.append(DISQUALIFIED)
        try:
            email = normalize_email(recipient)
        except ValueError:
            problems.append(INVALID_RECIPIENT)
            return problems
        if not email.rpartition("@")[0].isascii():
            problems.append(NON_ASCII_LOCAL)
            return problems

        hit = await exclusion_for(db, domain=company.domain, email=email)
        if hit is not None:
            problems.append(f"Recipient is excluded: {_clause(hit.detail)}.")

        domain = domain_of_email(email) or email.rpartition("@")[2]
        free = domain in FREE_MAIL_DOMAINS
        mx: tuple[str, datetime] | None = None
        if not free and (name := mx_domain(domain)) is not None:
            found = (
                await db.execute(
                    select(DomainMx.state, DomainMx.checked_at).where(DomainMx.domain == name)
                )
            ).one_or_none()
            mx = (found.state, found.checked_at) if found is not None else None

        row = await _live_address(db, address)
        if (
            row is None
            or row.company_id != company.id
            or row.person_id is None
            or row.address != email
            or (sequence_or_none is not None and sequence_or_none.person_id != row.person_id)
        ):
            problems.append(NOT_CHOSEN)
        elif (why := unsendable_reason(row, mx_state=mx[0] if mx else "unknown")) is not None:
            problems.append(f"Not sendable: {_clause(why)}.")

        # A fresh "no mail server" answer is reported by the sendable rule above; this is for
        # an answer that is missing, unknown or too old to rely on.
        if not free and (mx is None or mx_is_stale(mx[0], mx[1], now=now, config=config.contacts)):
            problems.append(MX_OUT_OF_DATE)

        if await _in_other_sequence(db, email, recipient, sequence_or_none):
            problems.append(ALREADY_CONTACTED)
    return problems


async def gate_message(
    db: AsyncSession,
    *,
    subject: str,
    body: str,
    step: int,
    side: Side,
    facts: Facts,
    prompt_text: str,
    sequence_or_none: Sequence | None,
    recipient: str,
    company: Company,
    address: EmailAddress | None,
    now: datetime,
    config: AppConfig,
) -> list[str]:
    """The whole gate: content problems, then recipient problems."""
    content = content_problems(
        subject,
        body,
        step=step,
        side=side,
        recipient=recipient,
        facts=facts,
        writing=config.writing,
        prompt_text=prompt_text,
    )
    return content + await recipient_problems(
        db,
        sequence_or_none=sequence_or_none,
        recipient=recipient,
        company=company,
        address=address,
        now=now,
        config=config,
    )
