"""Published email addresses, named decision makers and the contact form, read from a company's
own pages. Pure functions: no I/O, never raise on odd input, snippets at most 300 characters.

Precision over recall: a person needs a strict person-name gate plus a recognisable job title
next to it, and only pages that are about the company, its people or how to contact it count."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote

from email_validator import EmailNotValidError, validate_email
from selectolax.lexbor import LexborHTMLParser, LexborNode

from salesfinder.contacts.names import (
    is_stop_word,
    looks_like_person_name,
    name_key,
    normalize_name,
    split_name,
)
from salesfinder.contacts.titles import categorize_title, has_title_word
from salesfinder.domains import FREE_MAIL_DOMAINS, domain_of_email, normalize_domain
from salesfinder.profiling.pages import matches_groups, route_text
from salesfinder.signals.text import PageText, snippet

# Page groups (from profiling.pages.PAGE_GROUPS) whose pages may name people / publish free-mail
# addresses. "company" is too generic to trust in a <title> ("Acme Chemical Company | Products").
_PEOPLE_GROUPS = ("about", "contact", "team", "leadership")
_TITLE_SKIP = ("company",)
MAX_PEOPLE = 20

_WS = re.compile(r"\s+")
_DASHES = dict.fromkeys([*range(0x2010, 0x2016), 0x2212], "-")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip().translate(_DASHES)


def is_people_page(url: str, title: str) -> bool:
    """An about/contact/team/leadership page, by its URL or its <title>."""
    return matches_groups(route_text(url), _PEOPLE_GROUPS) or matches_groups(
        title, _PEOPLE_GROUPS, skip=_TITLE_SKIP
    )


# --- emails ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailFound:
    address: str
    kind: Literal["personal", "role"]
    url: str
    snippet: str


@dataclass(frozen=True)
class PersonFound:
    full_name: str
    title: str | None
    category: str
    url: str
    snippet: str


# "@" or an obfuscated "at"/"dot": bracketed in any case ("[at]", "(dot)", "{at}"), or bare
# uppercase between spaces ("name AT acme DOT com"). A lowercase bare "at" is ordinary prose.
_AT = r"(?:@|\s?[\[\(\{]\s?(?i:at)\s?[\]\)\}]\s?|\s+AT\s+)"
_DOT = r"(?:\.|\s?[\[\(\{]\s?(?i:dot)\s?[\]\)\}]\s?|\s+DOT\s+)"
_DOT_RE = re.compile(_DOT)
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(
    rf"(?<![A-Za-z0-9._%+])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+'-]{{0,63}})"
    rf"{_AT}(?P<domain>{_LABEL}(?:{_DOT}{_LABEL})*{_DOT}[A-Za-z]{{2,24}})(?![A-Za-z0-9-])"
)
_ASSET_RE = re.compile(r"\.(?:png|jpe?g|webp|svg|gif|bmp|ico|tiff?|avif|css|js)$")
_RETINA_RE = re.compile(r"^\d+(?:\.\d+)?x$")  # "logo@2x.png" -> domain label "2x"
_PLACEHOLDER_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com"}
)
_TRACKER_HOSTS = ("sentry", "wixpress")
_EDGE_PUNCT = " \t.,;:)(<>[]'\""

ROLE_LOCALS = frozenset(
    {
        "info",
        "sales",
        "contact",
        "enquiry",
        "enquiries",
        "inquiry",
        "purchase",
        "purchasing",
        "procurement",
        "export",
        "exports",
        "marketing",
        "admin",
        "office",
        "support",
        "hello",
        "mail",
        "customercare",
        "care",
        "hr",
        "careers",
        "jobs",
        "accounts",
        "orders",
        "md",
        "gm",
        "ceo",
        "director",
        "webmaster",
        "query",
        "queries",
        "service",
        "team",
        "corporate",
        "trade",
        "postmaster",
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "abuse",
        "mailer-daemon",
        "bounce",
        "bounces",
        "newsletter",
    }
)
# Mailboxes no person reads: never sendable, whatever a teammate says.
NEVER_SEND_LOCALS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "postmaster",
        "abuse",
        "mailer-daemon",
        "bounce",
        "bounces",
    }
)


def _canonical(raw: str) -> str | None:
    """A validated, lowercased address, or None for junk (assets, placeholders, trackers)."""
    address = raw.strip(_EDGE_PUNCT).lower()
    local, at, host = address.rpartition("@")
    if not at or not local or not host:
        return None
    first_label = host.split(".", 1)[0]
    if (
        _ASSET_RE.search(host)
        or _ASSET_RE.search(local)
        or _RETINA_RE.match(first_label)
        or host in _PLACEHOLDER_DOMAINS
        or any(t in label for label in host.split(".") for t in _TRACKER_HOSTS)
    ):
        return None
    try:
        validated = validate_email(address, check_deliverability=False)
    except (EmailNotValidError, ValueError):
        return None
    return validated.normalized.lower()


def _is_role_local(local: str) -> bool:
    head = re.split(r"[._+-]", local, maxsplit=1)[0]
    return any(re.sub(r"\d+", "", part) in ROLE_LOCALS for part in (local, head))


def _matches_person(local: str, people: Sequence[PersonFound]) -> bool:
    """Whether a local part names one of ``people``: both first and last name as tokens
    ("anil.mehta", "mehta_anil"), or run together, possibly with one initial ("anilmehta",
    "amehta", "anilm", "mehtaa"). Digits separate tokens ("rajeshsharma75")."""
    tokens = [t for t in re.split(r"[^a-z]+", local) if t]
    for person in people:
        first, last = (
            name_key(part).replace(" ", "") if part else "" for part in split_name(person.full_name)
        )
        if len(first) < 2 or len(last) < 2:
            continue
        if first in tokens and last in tokens:
            return True
        joined = {first + last, last + first, first[0] + last, first + last[0], last + first[0]}
        if any(t in joined for t in tokens):
            return True
    return False


_CF_HREF = re.compile(r"/cdn-cgi/l/email-protection#([0-9A-Fa-f]*)")
_CF_PLACEHOLDER = "[email protected]"


def _cf_decode(encoded: str) -> str | None:
    """Cloudflare email protection: hex bytes, XORed with the first byte. None if malformed."""
    if len(encoded) < 4 or len(encoded) % 2:
        return None
    try:
        data = bytes.fromhex(encoded)
        return bytes(b ^ data[0] for b in data[1:]).decode("utf-8")
    except ValueError:  # bad hex, or bytes that are not UTF-8
        return None


def _context_evidence(context: str, address: str) -> str:
    """A snippet of ``context`` (link text, a parent element's text) around ``address``."""
    text = _collapse(context).replace(_CF_PLACEHOLDER, address)
    if address.lower() not in text.lower():
        text = _collapse(f"{text} {address}")
    idx = text.lower().find(address.lower())
    return snippet(text, idx, idx + len(address))


def _candidates(page: PageText) -> list[tuple[str, str]]:
    """(raw address, snippet) for every address-looking string on the page: text, then mailto
    and Cloudflare-protected links."""
    found = [
        (f"{m.group('local')}@{_DOT_RE.sub('.', m.group('domain'))}", snippet(page.text, *m.span()))
        for m in _EMAIL_RE.finditer(page.text)
    ]
    lowered = page.text.lower()
    for href, anchor in page.links:
        protected = _CF_HREF.search(href)
        if protected is not None:
            decoded = _cf_decode(protected.group(1))
            if decoded:
                found.append((decoded, _context_evidence(anchor, decoded.strip())))
            continue
        if href[:7].lower() != "mailto:":
            continue
        # unquote() is lenient: it never raises on bad percent-escapes.
        targets = unquote(href[7:].split("?", 1)[0])
        for raw in re.split(r"[,;]", targets):
            address = raw.strip(_EDGE_PUNCT).lower()
            if not address:
                continue
            idx = lowered.find(address)
            if idx >= 0:
                found.append((raw, snippet(page.text, idx, idx + len(address))))
            else:
                found.append((raw, _context_evidence(anchor, address)))
    return found


def _html_candidates(html: str) -> tuple[str, list[tuple[str, str]]]:
    """(page title, [(address, snippet)]) for Cloudflare ``data-cfemail`` elements in raw HTML."""
    if not html.strip():
        return "", []
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    title = _collapse(title_node.text()) if title_node is not None else ""
    found: list[tuple[str, str]] = []
    for node in tree.css("[data-cfemail]"):
        decoded = _cf_decode((node.attributes.get("data-cfemail") or "").strip())
        if not decoded:
            continue
        holder = node.parent if node.parent is not None else node
        found.append((decoded, _context_evidence(holder.text(separator=" "), decoded.strip())))
    return title, found


def extract_emails(
    pages: Sequence[PageText],
    *,
    company_domain: str,
    people: Sequence[PersonFound] = (),
    pages_html: Sequence[tuple[str, str]] = (),
) -> list[EmailFound]:
    """Published addresses at the company's own domain, plus free-mail addresses published on its
    about/contact/team/leadership pages. ``pages_html`` (raw (url, html) pairs) adds addresses
    only visible in markup (Cloudflare ``data-cfemail``). Deduped by address; first evidence
    wins, ``pages`` before ``pages_html``."""
    company = normalize_domain(company_domain) if company_domain else None
    results: dict[str, EmailFound] = {}

    def collect(url: str, people_page: bool, candidates: list[tuple[str, str]]) -> None:
        for raw, evidence in candidates:
            address = _canonical(raw)
            if address is None or address in results:
                continue
            local, _, host = address.rpartition("@")
            free_mail = host in FREE_MAIL_DOMAINS
            if not (
                (company is not None and domain_of_email(address) == company)
                or (free_mail and people_page)
            ):
                continue
            role = _is_role_local(local) or (free_mail and not _matches_person(local, people))
            results[address] = EmailFound(
                address=address, kind="role" if role else "personal", url=url, snippet=evidence
            )

    for page in pages:
        collect(page.url, is_people_page(page.url, page.title), _candidates(page))
    for url, html in pages_html:
        title, candidates = _html_candidates(html)
        collect(url, is_people_page(url, title), candidates)
    return list(results.values())


# --- page structure -------------------------------------------------------------------------------

_SKIP_TAGS = frozenset(
    {
        "head",
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "nav",
        "form",
        "select",
        "option",
        "button",
        "textarea",
        "iframe",
        "object",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "html",
        "body",
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


@dataclass
class _Element:
    parent: int
    start: int  # first fragment index inside
    block: bool
    end: int = -1  # one past the last fragment index inside
    text_len: int = 0


@dataclass(frozen=True)
class _Fragment:
    text: str  # one text node, collapsed
    element: int  # innermost element index
    line: int


@dataclass
class _Layout:
    fragments: list[_Fragment] = field(default_factory=list)
    elements: list[_Element] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # text between blocks / <br>, collapsed
    _pending: list[str] = field(default_factory=list)

    def flush(self) -> None:
        text = _collapse("".join(self._pending))
        if text:
            self.lines.append(text)
        self._pending.clear()

    def add_text(self, raw: str, element: int) -> None:
        self._pending.append(raw)
        text = _collapse(raw)
        if text and element >= 0:
            self.fragments.append(_Fragment(text, element, len(self.lines)))
            self.elements[element].text_len += len(text)

    def open(self, parent: int, block: bool) -> int:
        if block:
            self.flush()
        self.elements.append(_Element(parent=parent, start=len(self.fragments), block=block))
        return len(self.elements) - 1

    def close(self, index: int) -> None:
        element = self.elements[index]
        element.end = len(self.fragments)
        if element.parent >= 0:
            self.elements[element.parent].text_len += element.text_len
        if element.block:
            self.flush()


# --- the company's own name -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Own:
    """The company's own name, so its people's "…, Acme Chemicals" isn't another company."""

    tokens: frozenset[str] = frozenset()  # distinctive lowercase words of its name
    label: str = ""  # its domain's first label ("acme" for acme.co.in)
    name: str = ""  # its name, lowercased

    def owns(self, word: str) -> bool:
        w = "".join(c for c in word.lower() if c.isalpha())
        if len(w) < 2:
            return False
        if w in self.tokens or w == self.label:
            return True
        label = self.label
        return len(label) >= 4 and len(w) >= 4 and (label.startswith(w) or w.startswith(label))


def _own(company_name: str | None, company_domain: str | None) -> _Own:
    name = _collapse(company_name or "")
    tokens = frozenset(
        w
        for raw in name.split()
        if len(w := "".join(c for c in raw.lower() if c.isalpha())) >= 3 and not is_stop_word(w)
    )
    domain = normalize_domain(company_domain) if company_domain else None
    label = domain.split(".", 1)[0] if domain else ""
    return _Own(tokens, label, name.lower())


_WORD_EDGE = " \t.,;:'\"()[]&/\u2019"


def _capitalized_words(text: str) -> list[str]:
    words = [w.strip(_WORD_EDGE) for w in text.split()]
    return [w for w in words if w[:1].isalpha() and w[0].isupper() and sum(map(str.isalpha, w)) > 1]


def _is_own_segment(text: str, own: _Own) -> bool:
    """A segment naming the company itself ("Acme Chemicals Pvt Ltd" on acme.com)."""
    return any(own.owns(w) for w in _capitalized_words(text))


def _is_org_segment(text: str, own: _Own) -> bool:
    """A segment naming some other organisation: a capitalized word that is no stop word
    (places, departments, company forms), in a segment that is not a job title and does not
    name the company itself ("Hindustan Unilever", "Brenntag USA", "XYZ Pharma Ltd")."""
    t = _clean_title(text)
    if not t or "@" in t or any(c.isdigit() for c in t):
        return False
    words = _capitalized_words(t)
    if not words or any(own.owns(w) for w in words):
        return False
    if has_title_word(t):
        return False  # another title ("Director"), not an organisation
    return any(not is_stop_word(w) for w in words)


# --- page structure: quotes and other companies' sections -----------------------------------------

# Containers of other companies' people (testimonials, reviews, client quotes) by class/id token.
# Generic sliders/carousels are skipped too, unless the same element says it holds the team.
_QUOTE_TAGS = frozenset({"blockquote", "q", "cite"})
_QUOTE_TOKENS = frozenset(
    {
        "client",
        "clients",
        "customer",
        "customers",
        "say",
        "says",
        "speak",
        "speaks",
        "words",
        "review",
        "reviews",
        "testimonial",
        "testimonials",
        "feedback",
        "quote",
        "quotes",
    }
)
_WIDGET_TOKENS = frozenset({"slider", "carousel"})
_TEAM_TOKENS = frozenset(
    {"team", "member", "members", "leader", "leaders", "leadership", "people", "staff"}
    | {"director", "directors", "management"}
)
_TOKEN_SPLIT = re.compile(r"[\s_-]+")

# A section under a heading like these is about other companies' people: its names are skipped
# until a heading of the same or a higher level, or the end of the enclosing section.
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
_QUOTE_HEADING = re.compile(
    r"(?<![a-z])(?:clients|customers|testimonials?|partners|associates|bankers|trusted\s+by"
    r"|our\s+(?:client|customer|partner)|what\b.{0,40}?\bsays?)(?![a-z])",
    re.IGNORECASE,
)
_TEAM_HEADING = re.compile(
    r"(?<![a-z])(?:team|leadership|management|directors|founders|people|staff|board)(?![a-z])",
    re.IGNORECASE,
)
_HEADING_MAX = 80
# Elements a heading's section ends with (or any element whose class/id says "section").
_SECTION_TAGS = frozenset(
    {"html", "body", "main", "section", "article", "aside", "header", "footer"}
)


def _is_quote_block(node: LexborNode) -> bool:
    if node.tag in _QUOTE_TAGS:
        return True
    tokens = _marker_tokens(node)
    if tokens & _QUOTE_TOKENS:
        return True
    return bool(tokens & _WIDGET_TOKENS) and not tokens & _TEAM_TOKENS


def _marker_tokens(node: LexborNode) -> set[str]:
    attrs = node.attributes
    marker = f"{attrs.get('class') or ''} {attrs.get('id') or ''}".lower()
    return set(_TOKEN_SPLIT.split(marker))


def _is_quote_heading(text: str, own: _Own) -> bool:
    """A heading over other companies' people ("What our clients say", "Our Partners"), unless
    it is about the team or the matched words are the company's own name ("Mehta & Associates")."""
    if len(text) > _HEADING_MAX or _TEAM_HEADING.search(text):
        return False
    return any(m.group(0).lower() not in own.name for m in _QUOTE_HEADING.finditer(text))


@dataclass(frozen=True)
class _Heading:
    level: int
    quote: bool
    section: int  # element index the heading's section ends with (-1: the whole page)


def _section_of(node: LexborNode, index: int, elements: list[_Element]) -> int:
    """The element index of ``node``'s nearest enclosing section (``index`` is its parent's)."""
    ancestor = node.parent
    while index >= 0 and ancestor is not None:
        tag = ancestor.tag or ""
        if tag in _SECTION_TAGS or any("section" in t for t in _marker_tokens(ancestor)):
            return index
        ancestor, index = ancestor.parent, elements[index].parent
    return -1


def _layout(root: LexborNode, own: _Own) -> _Layout:
    """Visible text as fragments (text nodes) and lines (runs between block boundaries and
    <br>), in document order, without quote blocks and sections headed as other companies'
    (clients, partners, testimonials). Iterative, so deeply nested markup cannot hit the
    recursion limit."""
    out = _Layout()
    headings: list[_Heading] = []
    # (node, parent element index) to enter, or (None, element index) to close.
    stack: list[tuple[LexborNode | None, int]] = [(root, -1)]
    while stack:
        node, index = stack.pop()
        if node is None:
            out.close(index)
            headings = [h for h in headings if h.section != index]
            continue
        tag = node.tag or ""
        if tag == "-text":
            if not any(h.quote for h in headings):
                out.add_text(node.text_content or "", index)
            continue
        if tag.startswith("-") or tag in _SKIP_TAGS or _is_quote_block(node):
            continue
        if tag == "br":
            out.flush()
            continue
        level = _HEADING_LEVELS.get(tag)
        if level is not None:
            heading = _Heading(
                level,
                _is_quote_heading(_collapse(node.text(separator=" ")), own),
                _section_of(node, index, out.elements),
            )
            headings = [h for h in headings if h.level < level] + [heading]
        element = out.open(index, tag in _BLOCK_TAGS)
        stack.append((None, element))
        children: list[LexborNode] = []
        child = node.child
        while child is not None:
            children.append(child)
            child = child.next
        stack.extend((c, element) for c in reversed(children))
    out.flush()
    return out


# --- people ---------------------------------------------------------------------------------------

_LINE_MAX = 150  # longer lines are prose, not "Name - Title" lines
_TITLE_MAX = 80
_CARD_MAX = 400  # a container with more text than this is a section, not one person's card
_LOOKAHEAD = 3  # fragments after a name, inside its card, that may hold the title
_SEP_RE = re.compile(r"(\s+-\s+|\s*\|\s*|,\s+|:\s+)")
_PAREN_RE = re.compile(r"\s*\(([^()]{2,60})\)")
_ATTRIBUTION_START = "-~\"'\u201c\u201d\u2018\u2019\u00ab\u00bb"
_TITLE_EDGE = " \t-|,:;\u2022\u00b7/()*"


def _clean_title(text: str) -> str:
    return _collapse(text).strip(_TITLE_EDGE)


def _name_part(text: str) -> str | None:
    """The normalized name if ``text`` is (only) a person's name."""
    return normalize_name(text) if looks_like_person_name(text) else None


def _line_people(line: str, own: _Own) -> list[tuple[str, str]]:
    """(name, title) pairs from a "Name <sep> Title" / "Title <sep> Name" line.

    Someone whose title is followed by another organisation ("Rakesh Gupta - Purchase Manager,
    Hindustan Unilever") works there, not here: neither they nor that organisation are taken.
    Nor is "Title, Name" read backwards: "Purchase Manager, Hindustan Unilever" names a
    company far more often than a person."""
    if len(line) > _LINE_MAX:
        return []
    pieces = _SEP_RE.split(_PAREN_RE.sub(r" | \1", line))
    texts, seps = pieces[0::2], pieces[1::2]
    names = [_name_part(t) for t in texts]
    titled = [
        names[i] is None and len(t) <= _TITLE_MAX and has_title_word(_clean_title(t))
        for i, t in enumerate(texts)
    ]
    used: set[int] = set()

    def title_of(lo: int, hi: int) -> str:  # texts[lo..hi] with their separators
        parts = [texts[lo]]
        for k in range(lo + 1, hi + 1):
            parts += [seps[k - 1], texts[k]]
        return _clean_title("".join(parts))

    def elsewhere(start: int) -> bool:
        """Whether another organisation is named after a title run ending before ``start``
        (before the next person on the line, if any)."""
        for m in range(start, len(texts)):
            if m in used or (names[m] is not None and m + 1 < len(texts) and titled[m + 1]):
                return False  # the next person starts
            if _is_org_segment(texts[m], own):
                return True
        return False

    found: list[tuple[str, str]] = []
    for i, name in enumerate(names):
        if name is None or i in used:
            continue
        j = i + 1
        while j < len(texts) and titled[j] and j not in used:
            j += 1
        if j > i + 1:
            if elsewhere(j):
                used.update(range(i, len(texts)))
                continue
            found.append((name, title_of(i + 1, j - 1)))
            used.update(range(i, j))
            continue
        k = i - 1
        while k >= 0 and titled[k] and k not in used:
            k -= 1
        if k < i - 1:
            if seps[i - 1].strip() != ",":
                found.append((name, title_of(k + 1, i - 1)))
            used.update(range(k + 1, i + 1))
    return found


def _card(layout: _Layout, fragment: int) -> _Element | None:
    """The innermost container of ``fragment`` holding more text than the fragment itself."""
    frag = layout.fragments[fragment]
    index = frag.element
    while index >= 0:
        element = layout.elements[index]
        if element.text_len > len(frag.text):
            return element
        index = element.parent
    return None


_ELSEWHERE = object()  # a job title at another organisation
_NEXT_PERSON = object()  # another person's "Name - Title" line


def _card_title(text: str, own: _Own) -> str | object | None:
    """The categorized job title ``text`` is (any trailing segments naming the company itself
    dropped), ``_ELSEWHERE`` for a title followed by another organisation ("Purchase Manager,
    Hindustan Unilever"), ``_NEXT_PERSON`` for a line starting with someone else's name, or
    None if it is no job title."""
    parts = _SEP_RE.split(text)
    if len(parts) > 1 and _name_part(parts[0]) is not None:
        return _NEXT_PERSON
    end = len(parts)
    while end > 1 and _is_own_segment(parts[end - 1], own):
        end -= 2  # the segment and the separator before it
    kept = "".join(parts[:end])
    segments = _SEP_RE.split(_PAREN_RE.sub(r" | \1", kept))[0::2]
    if any(_is_org_segment(s, own) for s in segments[1:]) and has_title_word(
        _clean_title(segments[0])
    ):
        return _ELSEWHERE
    title = _clean_title(kept)
    if len(title) <= _TITLE_MAX and has_title_word(title) and categorize_title(title) != "other":
        return title
    return None


def _card_person(layout: _Layout, i: int, own: _Own) -> tuple[int, tuple[str, str, str] | None]:
    """(title fragment, (name, title, evidence)) when fragment ``i`` is a name followed, inside
    its card (or as the very next text), by a categorized job title; (title fragment, None) when
    that title is at another organisation; (-1, None) when there is no title."""
    name = _name_part(layout.fragments[i].text)
    if name is None:
        return -1, None
    card = _card(layout, i)
    if card is None:
        return -1, None
    small = card.text_len <= _CARD_MAX
    last = min(card.end, i + 1 + (_LOOKAHEAD if small else 1))
    for j in range(i + 1, last):
        candidate = layout.fragments[j].text
        if _name_part(candidate) is not None:
            return -1, None  # the next person starts before any title
        title = _card_title(candidate, own)
        if title is _NEXT_PERSON:
            return -1, None
        if title is _ELSEWHERE:
            return j, None
        if isinstance(title, str):
            span = layout.fragments[card.start : card.end] if small else layout.fragments[i : j + 1]
            return j, (name, title, _collapse(" ".join(f.text for f in span)))
    return -1, None


def _page_people(url: str, html: str, own: _Own) -> list[PersonFound]:
    if not html.strip():
        return []
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    title = _collapse(title_node.text()) if title_node is not None else ""
    if not is_people_page(url, title) or tree.root is None:
        return []
    layout = _layout(tree.root, own)
    # Cards first: a line that is the title under someone's name ("Purchase Manager, Hindustan
    # Unilever" below "Rakesh Gupta") is not read again as "Title, Name".
    cards: list[tuple[int, PersonFound]] = []
    title_lines: set[int] = set()
    for i in range(len(layout.fragments)):
        j, hit = _card_person(layout, i, own)
        if j >= 0 and layout.fragments[j].line != layout.fragments[i].line:
            title_lines.add(layout.fragments[j].line)
        if hit is not None:
            name, job, context = hit
            start = max(context.find(layout.fragments[i].text), 0)
            evidence = snippet(context, start, start + len(layout.fragments[i].text))
            cards.append((i, PersonFound(name, job, categorize_title(job), url, evidence)))
    # (document position, person); a line's position is its first fragment.
    found: list[tuple[int, PersonFound]] = []
    line_pos: dict[int, int] = {}
    for index, frag in enumerate(layout.fragments):
        line_pos.setdefault(frag.line, index)
    for line_no, line in enumerate(layout.lines):
        if line[:1] in _ATTRIBUTION_START or line_no in title_lines:
            continue  # "- Sarah Lee, Purchasing Manager": a quote's attribution
        for name, job in _line_people(line, own):
            evidence = snippet(line, 0, len(line))
            person = PersonFound(name, job, categorize_title(job), url, evidence)
            found.append((line_pos.get(line_no, 0), person))
    found += cards
    found.sort(key=lambda pair: pair[0])
    return [person for _, person in found]


def extract_people(
    pages_html: Sequence[tuple[str, str]],
    *,
    company_name: str | None = None,
    company_domain: str | None = None,
) -> list[PersonFound]:
    """Named people with a job title, from about/contact/team/leadership pages (by URL or
    <title>), in page then document order; deduped by name key, at most ``MAX_PEOPLE``.

    ``company_name`` / ``company_domain`` tell the company's own name apart from other
    organisations named next to a title ("Priya Nair, Head of Procurement, Acme Chemicals" is
    one of ours; "..., Hindustan Unilever" is a customer)."""
    own = _own(company_name, company_domain)
    people: list[PersonFound] = []
    seen: set[str] = set()
    for url, html in pages_html:
        for person in _page_people(url, html, own):
            key = name_key(person.full_name)
            if key and key not in seen:
                seen.add(key)
                people.append(person)
                if len(people) >= MAX_PEOPLE:
                    return people
    return people


# --- contact form ---------------------------------------------------------------------------------


def _has_contact_form(tree: LexborHTMLParser) -> bool:
    for form in tree.css("form"):
        if form.css_first("textarea") is not None:
            return True
        for field_node in form.css("input"):
            if (field_node.attributes.get("type") or "").strip().lower() == "email":
                return True
    return False


def contact_form_url(pages_html: Sequence[tuple[str, str]]) -> str | None:
    """The first contact page, on the same site as the first page, with a form that takes a
    message (a textarea) or an email address."""
    site: str | None = None
    for position, (url, html) in enumerate(pages_html):
        domain = normalize_domain(url)
        if position == 0:
            site = domain
        if domain is None or domain != site or not html.strip():
            continue
        tree = LexborHTMLParser(html)
        title_node = tree.css_first("title")
        title = _collapse(title_node.text()) if title_node is not None else ""
        on_contact_page = matches_groups(route_text(url), ("contact",)) or matches_groups(
            title, ("contact",)
        )
        if on_contact_page and _has_contact_form(tree):
            return url
    return None
