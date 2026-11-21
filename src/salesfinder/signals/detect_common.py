"""Detectors shared by buyer and supplier profiling. Pure functions over extracted page text."""

import re
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from salesfinder.catalog import Catalog, Facts
from salesfinder.config import Region
from salesfinder.industries import INDUSTRIES
from salesfinder.profiling.pages import PAGE_GROUPS
from salesfinder.signals.geo import STATE_CENTROIDS, nearest_warehouse
from salesfinder.signals.text import Evidence, PageText, snippet


@dataclass(frozen=True)
class Finding:
    code: str
    value: Any  # JSON-serializable
    evidence: Evidence | None = None


def _evidence(page: PageText, m: re.Match[str]) -> Evidence:
    return Evidence(url=page.url, snippet=snippet(page.text, m.start(), m.end()))


# A term must not be glued to other letters/digits (so SLES does not match inside SLESSOR).
_LB = r"(?<![A-Za-z0-9])"
_RB = r"(?![A-Za-z0-9])"
_SEP = r"[\s\-]+"


# --------------------------------------------------------------------------- chemicals


def _norm(term: str) -> str:
    """Canonical form: lowercase, hyphen/space runs collapsed, sulphate -> sulfate."""
    return re.sub(r"[\s\-]+", " ", term.lower()).strip().replace("sulph", "sulf")


_SHORT_TERM = 4
_DASHES = dict.fromkeys([*range(0x2010, 0x2016), 0x2212], "-")


def normalize_dashes(text: str) -> str:
    """Unicode hyphens/dashes -> "-" (1:1, so offsets are preserved)."""
    return text.translate(_DASHES)


def _term_pattern_raw(term: str) -> str:
    return _SEP.join(re.escape(tok) for tok in term.split(" "))


def _term_pattern(norm_term: str) -> str:
    parts = [re.escape(tok) for tok in norm_term.split(" ")]
    return _SEP.join(parts).replace("sulf", "sul(?:f|ph)")


def _product_terms(
    name: str, search_name: str, synonyms: Iterable[str], inci: str | None
) -> set[str]:
    terms = {name, search_name, *synonyms}
    if inci:
        terms.add(inci)
    # "Sodium Laureth Sulfate (SLES 70%)" -> "Sodium Laureth Sulfate"; parenthetical alias if
    # it carries no strength/number.
    for text in list(terms):
        outer = re.sub(r"\([^)]*\)", " ", text)
        terms.add(outer)
        for inner in re.findall(r"\(([^)]*)\)", text):
            if not re.search(r"\d", inner):
                terms.add(inner)
    cleaned = {re.sub(r"[\s\-]+", " ", re.sub(r"[^\w\s\-]", " ", t)).strip() for t in terms}
    return {t for t in cleaned if len(t) >= 3 and re.search(r"[A-Za-z]", t)}


@dataclass(frozen=True)
class _Matcher:
    regex: re.Pattern[str]
    terms: dict[str, tuple[str, ...]]  # normalized term -> product ids (case-insensitive)
    short: dict[str, tuple[str, ...]]  # short term as written -> product ids (case-sensitive)
    cas: dict[str, tuple[str, ...]]  # CAS number -> product ids


@lru_cache(maxsize=8)
def _build_matcher(
    fingerprint: tuple[tuple[str, str, str, tuple[str, ...], str | None, tuple[str, ...]], ...],
) -> _Matcher:
    terms: dict[str, set[str]] = {}
    short: dict[str, set[str]] = {}
    cas: dict[str, set[str]] = {}
    for pid, name, search_name, synonyms, inci, cas_numbers in fingerprint:
        for t in _product_terms(name, search_name, synonyms, inci):
            if len(t) <= _SHORT_TERM:
                short.setdefault(t, set()).add(pid)  # case-sensitive, as written in the catalog
            else:
                terms.setdefault(_norm(t), set()).add(pid)
        for c in cas_numbers:
            cas.setdefault(c, set()).add(pid)
    alts = []
    if short:
        body = "|".join(_term_pattern_raw(t) for t in sorted(short, key=lambda t: (-len(t), t)))
        alts.append(f"{_LB}(?P<short>(?-i:{body})){_RB}")
    if terms:
        body = "|".join(_term_pattern(t) for t in sorted(terms, key=lambda t: (-len(t), t)))
        alts.append(f"{_LB}(?P<term>{body}){_RB}")
    if cas:
        body = "|".join(re.escape(c) for c in sorted(cas, key=lambda c: (-len(c), c)))
        alts.append(rf"(?<![\d-])(?P<cas>{body})(?![\d-])")
    regex = re.compile("|".join(alts) if alts else r"(?!x)x", re.IGNORECASE)
    return _Matcher(
        regex=regex,
        terms={k: tuple(sorted(v)) for k, v in terms.items()},
        short={k: tuple(sorted(v)) for k, v in short.items()},
        cas={k: tuple(sorted(v)) for k, v in cas.items()},
    )


def _matcher_for(catalog: Catalog) -> _Matcher:
    fingerprint = tuple(
        (p.id, p.name, p.search_name, tuple(p.synonyms), p.inci, tuple(p.cas))
        for p in catalog.products
    )
    return _build_matcher(fingerprint)


def detect_chemicals(pages: Sequence[PageText], catalog: Catalog, *, code: str) -> Finding:
    """Catalog products mentioned on the pages (one combined regex per catalog, cached)."""
    matcher = _matcher_for(catalog)
    found: set[str] = set()
    evidence: Evidence | None = None
    for page in pages:
        for m in matcher.regex.finditer(normalize_dashes(page.text)):
            if m.group("short") is not None:
                ids = matcher.short.get(re.sub(r"[\s\-]+", " ", m.group("short")), ())
            elif m.group("term") is not None:
                ids = matcher.terms.get(_norm(m.group("term")), ())
            else:
                ids = matcher.cas.get(m.group("cas"), ())
            if ids:
                found.update(ids)
                if evidence is None:
                    evidence = _evidence(page, m)
    return Finding(code, sorted(found), evidence)


# --------------------------------------------------------------------------- industries


def _label_phrases(label: str) -> set[str]:
    phrases = {label}
    for inner in re.findall(r"\(([^)]*)\)", label):
        phrases.update(p.strip() for p in inner.split(","))
    outer = re.sub(r"\([^)]*\)", "", label).strip()
    phrases.add(outer)
    for p in list(phrases):
        if "&" in p:
            phrases.add(p.replace("&", "and"))
    return {p.lower() for p in phrases if p}


def _phrase_regex(phrases: Iterable[str]) -> re.Pattern[str]:
    ordered = sorted(set(phrases), key=lambda p: (-len(p), p))
    body = "|".join(_SEP.join(re.escape(w) for w in p.split()) for p in ordered)
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)


def detect_industries(pages: Sequence[PageText], keywords: dict[str, list[str]]) -> Finding:
    found: set[str] = set()
    evidence: Evidence | None = None
    for key, label in INDUSTRIES.items():
        phrases = _label_phrases(label) | {
            k.lower().strip() for k in keywords.get(key, []) if k.strip()
        }
        regex = _phrase_regex(phrases)
        for page in pages:
            m = regex.search(page.text)
            if m:
                found.add(key)
                if evidence is None:
                    evidence = _evidence(page, m)
                break
    return Finding("INDUSTRY_MATCH", sorted(found), evidence)


# --------------------------------------------------------------------------- certifications

_DIGIT_GUARD = r"(?<![\w])"
_CERTS: dict[str, re.Pattern[str]] = {
    "ISO 9001": re.compile(rf"{_DIGIT_GUARD}ISO[\s-]?9001(?::\d{{4}})?(?!\d)", re.I),
    "ISO 14001": re.compile(rf"{_DIGIT_GUARD}ISO[\s-]?14001(?::\d{{4}})?(?!\d)", re.I),
    "ISO 45001": re.compile(rf"{_DIGIT_GUARD}ISO[\s-]?45001(?::\d{{4}})?(?!\d)", re.I),
    "ISO 22000": re.compile(rf"{_DIGIT_GUARD}ISO[\s-]?22000(?::\d{{4}})?(?!\d)", re.I),
    "FSSC 22000": re.compile(r"(?<![\w])FSSC[\s-]?22000(?!\d)", re.I),
    "GMP": re.compile(r"\bc?GMP\b|good manufacturing practices?", re.I),
    "NSF": re.compile(r"\bNSF\b"),
    "REACH": re.compile(
        r"\bREACH\b(?=[^.]{0,60}?(?:regist|complian|certified))"
        r"|(?:regist|complian|certified)[^.]{0,60}?\bREACH\b"
    ),
    "Kosher": re.compile(r"\bkosher\b", re.I),
    "Halal": re.compile(r"\bhalal\b", re.I),
    "RSPO": re.compile(r"\bRSPO\b"),
    "ECOCERT": re.compile(r"\bECOCERT\b", re.I),
    "COSMOS": re.compile(r"\bCOSMOS\b"),
    "USDA Organic": re.compile(r"\bUSDA[\s-]+organic\b", re.I),
    "FDA registered": re.compile(r"\bFDA[\s-]registered\b", re.I),
    "BRC": re.compile(r"\bBRC(?:GS)?\b"),
}
# The certification names the detectors report (the cohort form offers these).
CERT_NAMES = tuple(_CERTS)
# Each name's matcher, read-only (the outreach gate checks drafts with these).
CERT_PATTERNS: Mapping[str, re.Pattern[str]] = MappingProxyType(_CERTS)


def detect_certs(pages: Sequence[PageText], *, code: str) -> Finding:
    found: set[str] = set()
    evidence: Evidence | None = None
    for page in pages:
        matches = {name: m for name, rx in _CERTS.items() if (m := rx.search(page.text))}
        found.update(matches)
        if matches and evidence is None:
            evidence = _evidence(page, min(matches.values(), key=lambda m: m.start()))
    return Finding(code, sorted(found), evidence)


# --------------------------------------------------------------------------- US location

_STATE_NAMES: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}  # fmt: skip

_STATE_ALT = "|".join(
    re.escape(s) for s in sorted([*_STATE_NAMES, *STATE_CENTROIDS], key=lambda s: (-len(s), s))
)
_CITY_WORD = r"[A-Z][A-Za-z.'\-]*"
_US_ADDRESS = re.compile(
    rf"(?<![A-Za-z])(?P<city>(?:{_CITY_WORD}\s+){{0,3}}{_CITY_WORD}),\s*"
    rf"(?P<state>{_STATE_ALT})\.?\s+\d{{5}}(?:-\d{{4}})?(?!\d)"
)


_STREET_WORDS = frozenset(
    "st street ave avenue rd road blvd suite ste floor fl dr drive ln lane way pkwy hwy ct pl "
    "plaza building bldg".split()
)


def _clean_city(raw: str) -> str:
    """The city words of a captured run: anything up to and including a street-like word is
    dropped, at most three words kept."""
    words = raw.split()
    for i in range(len(words) - 1, -1, -1):
        if words[i].strip(".").lower() in _STREET_WORDS and i < len(words) - 1:
            words = words[i + 1 :]
            break
    return " ".join(words[-3:])


def _is_contact_or_about(url: str) -> bool:
    path = urlsplit(url).path.lower()
    keywords = [k for group, kws in PAGE_GROUPS if group in ("contact", "about") for k in kws]
    return any(k in path for k in keywords)


def detect_us_location(pages: Sequence[PageText]) -> Finding:
    ordered = sorted(pages, key=lambda p: not _is_contact_or_about(p.url))  # stable
    for page in ordered:
        m = _US_ADDRESS.search(page.text)
        if m:
            raw = m.group("state")
            state = _STATE_NAMES.get(raw, raw)
            return Finding(
                "US_LOCATION",
                {"state": state, "city": _clean_city(m.group("city"))},
                _evidence(page, m),
            )
    return Finding("US_LOCATION", {"found": False}, None)


def detect_near_warehouse(location: Finding, facts: Facts) -> Finding:
    value = location.value
    state = value.get("state") if isinstance(value, dict) else None
    if state not in STATE_CENTROIDS or not facts.warehouses:
        return Finding("NEAR_WAREHOUSE", {"found": False}, None)
    warehouse, miles = nearest_warehouse(state, facts.warehouses)
    return Finding(
        "NEAR_WAREHOUSE", {"warehouse": warehouse.label, "miles": miles}, location.evidence
    )


# --------------------------------------------------------------------------- region

_ADDRESS_ANCHOR = re.compile(
    r"(?<!\d)\d{6}(?!\d)|\b(?:Plot|GIDC|MIDC|Industrial Area|Estate|Road)\b"
)
_REGION_CONTEXT = 120


def detect_region(
    pages: Sequence[PageText], regions: Sequence[Region], fallback: str | None
) -> Finding:
    """First supplier region named in an address-like context (within 120 chars of a PIN code or
    address word) on a contact/about page, else the fallback with no evidence."""
    if regions:
        by_lower = {r.name.lower(): r.name for r in regions}
        names = sorted(by_lower, key=lambda n: (-len(n), n))
        regex = re.compile(
            r"(?<!\w)(?:"
            + "|".join(_SEP.join(map(re.escape, n.split())) for n in names)
            + r")(?!\w)",
            re.IGNORECASE,
        )
        for page in pages:
            if not _is_contact_or_about(page.url):
                continue
            anchors = [(a.start(), a.end()) for a in _ADDRESS_ANCHOR.finditer(page.text)]
            if not anchors:
                continue
            starts = [a[0] for a in anchors]
            for m in regex.finditer(page.text):
                i = bisect_left(starts, m.start())
                near = [anchors[j] for j in (i - 1, i) if 0 <= j < len(anchors)]
                # anchors may also overlap the preceding region; check both neighbours
                if any(
                    a_start - m.end() <= _REGION_CONTEXT and m.start() - a_end <= _REGION_CONTEXT
                    for a_start, a_end in near
                ):
                    name = by_lower[_norm_space(m.group(0))]
                    return Finding("REGION", name, _evidence(page, m))
    return Finding("REGION", fallback, None)


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())
