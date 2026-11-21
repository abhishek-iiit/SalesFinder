"""Buyer-specific detectors: company type and procurement hiring. Pure functions."""

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from salesfinder.profiling.pages import PAGE_GROUPS
from salesfinder.signals.detect_common import Finding, _evidence
from salesfinder.signals.text import Evidence, PageText

# Phrases ending in a stem (``manufactur``) also match its inflections.
_STEMS = ("manufactur",)


def phrase_regex(phrases: Sequence[str], *, patterns: Sequence[str] = ()) -> re.Pattern[str]:
    """Case-insensitive, whole-word match of any of the phrases (longest first), or of any of
    the regular-expression ``patterns``. A phrase that ends in a known stem may continue with
    letters."""
    ordered = sorted(set(phrases), key=lambda p: (-len(p), p))
    parts = []
    for p in ordered:
        body = r"\s+".join(re.escape(w) for w in p.split())
        parts.append(body + (r"\w*" if p.endswith(_STEMS) else ""))
    parts += patterns
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


_COMPANY_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "manufacturer",
        (
            "we manufacture",
            "manufacturer of",
            "manufacturing facility",
            "our plant",
            "production facility",
            "formulate and manufacture",
        ),
    ),
    ("contract_manufacturer", ("contract manufactur", "toll manufactur", "co-manufactur", "CMO")),
    ("private_label", ("private label", "white label")),
    (
        "distributor",
        (
            "distributor of",
            "we distribute",
            "distribution partner",
            "authorized distributor",
            "wholesale",
        ),
    ),
    ("retailer", ("add to cart", "shop now", "checkout", "free shipping")),
    ("service", ("consulting services", "testing services", "laboratory services")),
)
# Phrases that need more than a whole-word match.
_SPECIAL_PHRASES: dict[str, re.Pattern[str]] = {
    # Not "our plant-based shampoo".
    "our plant": re.compile(r"(?<!\w)our\s+plant(?![-\w])", re.IGNORECASE),
    # Only the upper-case abbreviation, and not the executive ("our CMO", "the CMO").
    "CMO": re.compile(r"(?<![Oo]ur )(?<![Tt]he )(?<!\w)CMO(?!\w)"),
}
_COMPANY_REGEXES = [
    (name, [_SPECIAL_PHRASES.get(p) or phrase_regex([p]) for p in phrases])
    for name, phrases in _COMPANY_TYPES
]
# The values COMPANY_TYPE can take besides "unknown" (the cohort form offers these).
COMPANY_TYPES = tuple(name for name, _ in _COMPANY_TYPES)
_PRODUCERS = frozenset({"manufacturer", "contract_manufacturer", "private_label"})


def _first(pages: Sequence[PageText], regex: re.Pattern[str]) -> Evidence | None:
    for page in pages:
        m = regex.search(page.text)
        if m:
            return _evidence(page, m)
    return None


def _count(pages: Sequence[PageText], regex: re.Pattern[str]) -> int:
    return sum(len(regex.findall(page.text)) for page in pages)


def _pages_with(pages: Sequence[PageText], regexes: Sequence[re.Pattern[str]]) -> int:
    """Each phrase counts once per page that has it, so site-wide navigation or a footer
    repeated on every page can't outweigh what the about page says."""
    return sum(1 for page in pages for regex in regexes if regex.search(page.text))


def _first_of(pages: Sequence[PageText], regexes: Sequence[re.Pattern[str]]) -> Evidence | None:
    for page in pages:
        found = [m for regex in regexes if (m := regex.search(page.text))]
        if found:
            return _evidence(page, min(found, key=lambda m: m.start()))
    return None


def detect_company_type(pages: Sequence[PageText]) -> Finding:
    """The type with the most phrase hits (each phrase once per page; at least 2; ties keep the
    earlier-listed type). "retailer" only when nothing says the company makes things: a brand
    that makes its own products often sells them online too."""
    scores = {name: _pages_with(pages, regexes) for name, regexes in _COMPANY_REGEXES}
    producer_hits = sum(scores[name] for name in _PRODUCERS)
    best_name, best_score = "unknown", 1
    best_regexes: list[re.Pattern[str]] | None = None
    for name, regexes in _COMPANY_REGEXES:
        if name == "retailer" and producer_hits:
            continue
        if scores[name] > best_score:  # strict: ties keep the earlier-listed type
            best_name, best_score, best_regexes = name, scores[name], regexes
    if best_regexes is None:
        return Finding("COMPANY_TYPE", "unknown", None)
    return Finding("COMPANY_TYPE", best_name, _first_of(pages, best_regexes))


_CAREERS_KEYWORDS = tuple(k for group, kws in PAGE_GROUPS if group == "careers" for k in kws)
_ROLES = (
    "purchasing",
    "procurement",
    "buyer",
    "supply chain",
    "sourcing",
    "formulation chemist",
    "R&D chemist",
    "formulator",
)
_ROLE_REGEX = phrase_regex(_ROLES)
_ROLE_NAMES = {" ".join(r.lower().split()): r.lower() for r in _ROLES}


def _is_careers_page(page: PageText) -> bool:
    haystack = f"{urlsplit(page.url).path} {page.title}".lower()
    return any(k in haystack for k in _CAREERS_KEYWORDS)


def detect_hiring(pages: Sequence[PageText]) -> Finding:
    careers = [p for p in pages if _is_careers_page(p)]
    if not careers:
        return Finding(
            "HIRING_PROCUREMENT", {"hiring": False, "roles": [], "careers_page": False}, None
        )
    roles: set[str] = set()
    evidence: Evidence | None = None
    for page in careers:
        for m in _ROLE_REGEX.finditer(page.text):
            roles.add(_ROLE_NAMES[" ".join(m.group(0).lower().split())])
            if evidence is None:
                evidence = _evidence(page, m)
    return Finding("HIRING_PROCUREMENT", {"hiring": bool(roles), "roles": sorted(roles)}, evidence)
