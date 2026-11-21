"""Pick the few same-site pages worth fetching from a company's homepage links."""

import re
from collections.abc import Collection, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from salesfinder.domains import normalize_domain

# Priority order; at most one URL per group. Keywords match whole words of the lowercased path
# (split on "/", "-", "_", "." and any other non-alphanumeric) or of the anchor text; see
# ``keyword_in``. A hyphenated keyword ("who-we-are") matches those words in a row, or run
# together as one word ("whoweare").
PAGE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("about", ("about", "company", "who-we-are", "profile")),
    ("products", ("product", "catalog", "catalogue", "portfolio", "chemicals", "range")),
    ("contact", ("contact", "reach-us", "locations")),
    (
        "team",
        (
            "team",
            "our-team",
            "meet-the-team",
            "our-people",
            "people",
            "staff",
            "directors",
            "board",
        ),
    ),
    ("leadership", ("leadership", "management", "management-team", "founder", "promoter")),
    ("careers", ("career", "jobs", "join-us", "vacanc", "opportunit")),
    ("quality", ("quality", "certif", "compliance", "sustainab")),
    ("export", ("export", "global", "international", "markets")),
)

# Keywords that are stems: they also match as the start of a word ("product" -> "products",
# "contact" -> "contactus", "vacanc" -> "vacancies"). Every other keyword must equal a whole word,
# so "range" does not match "orange" and "people" does not match "peoples".
_STEMS = frozenset(
    {
        "about",
        "company",
        "product",
        "catalog",
        "contact",
        "founder",
        "promoter",
        "career",
        "vacanc",
        "opportunit",
        "certif",
        "quality",
        "sustainab",
        "export",
    }
)

# A query string is dropped unless the path is a script that routes by query
# ("/index.php?page=about", "/index.php?option=com_content&view=article&id=5"); then the whole
# query is kept, minus tracking/session parameters, sorted so equivalent links dedupe.
_SCRIPT_RE = re.compile(r"\.(php\d?|aspx?|jsp|cfm|cgi|pl)$", re.IGNORECASE)
_TRACKING_KEYS = frozenset({"gclid", "fbclid", "ref", "sid", "sessionid", "phpsessid"})

_FILE_RE = re.compile(r"\.(pdf|jpe?g|png|zip|doc\w*|xls\w*)$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-z]+")


def words(text: str) -> list[str]:
    """Lowercased alphanumeric words of ``text`` (a path, title or anchor text)."""
    return [w for w in _NON_WORD.split(text.lower()) if w]


def keyword_in(keyword: str, text_words: Sequence[str]) -> bool:
    """Whether ``keyword`` (a PAGE_GROUPS keyword) occurs in ``text_words`` on word boundaries."""
    parts = keyword.split("-")
    if len(parts) > 1:
        n = len(parts)
        joined = "".join(parts)
        return joined in text_words or any(
            list(text_words[i : i + n]) == parts for i in range(len(text_words) - n + 1)
        )
    if keyword in _STEMS:
        return any(w.startswith(keyword) for w in text_words)
    return keyword in text_words


def matches_groups(text: str, groups: Collection[str], *, skip: Collection[str] = ()) -> bool:
    """Whether ``text`` contains a keyword (other than ``skip``) of one of the named groups."""
    text_words = words(text)
    return any(
        keyword_in(k, text_words)
        for name, keywords in PAGE_GROUPS
        if name in groups
        for k in keywords
        if k not in skip
    )


def _kept_query(query: str) -> list[tuple[str, str]]:
    """Query parameters minus tracking/session ones, sorted."""
    return sorted(
        (k, v)
        for k, v in parse_qsl(query)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS
    )


def route_text(url: str) -> str:
    """The routing part of a URL: its path plus the keys and values of its kept query."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return " ".join([parts.path, *(f"{k} {v}" for k, v in _kept_query(parts.query))])


def _clean(href: str, base_url: str) -> str | None:
    """Absolute http(s) URL without credentials or fragment, host lowercased; None if unusable.

    The query is dropped, except on a script path (see ``_SCRIPT_RE``)."""
    try:
        parts = urlsplit(urljoin(base_url, href.strip()))
        port = parts.port  # raises ValueError on an out-of-range port
    except ValueError:
        return None
    host = parts.hostname  # lowercased, without userinfo
    if parts.scheme not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = ""
    if parts.query and _SCRIPT_RE.search(parts.path):
        query = urlencode(_kept_query(parts.query))
    return urlunsplit((parts.scheme, netloc, parts.path or "/", query, ""))


def _links(home_html: str, base_url: str) -> list[tuple[str, str]]:
    """(clean URL, anchor text) for every usable link, in document order."""
    links: list[tuple[str, str]] = []
    for node in LexborHTMLParser(home_html).css("a[href]"):
        href = node.attributes.get("href")
        if not href:
            continue
        url = _clean(href, base_url)
        if url is not None:
            links.append((url, node.text(separator=" ")))
    return links


def pick_pages(home_html: str, base_url: str, *, limit: int) -> list[str]:
    """Up to ``limit - 1`` same-site page URLs (the homepage itself counts toward ``limit``)."""
    site = normalize_domain(base_url)
    home = _clean(base_url, base_url)
    if site is None or limit <= 1:
        return []

    candidates: list[tuple[str, list[str], list[str]]] = []
    for url, text in _links(home_html, base_url):
        path = urlsplit(url).path
        if url == home or path == "/" or _FILE_RE.search(path):
            continue
        if normalize_domain(url) != site:
            continue
        candidates.append((url, words(route_text(url)), words(text)))

    picked: list[str] = []
    for _group, keywords in PAGE_GROUPS:
        if len(picked) >= limit - 1:
            break
        for url, route_words, text_words in candidates:
            if url not in picked and any(
                keyword_in(k, route_words) or keyword_in(k, text_words) for k in keywords
            ):
                picked.append(url)
                break
    return picked
