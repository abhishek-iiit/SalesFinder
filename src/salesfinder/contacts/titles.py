"""Job titles: keyword categories, and which people to target first for each side."""

import re
from collections.abc import Iterable
from typing import Protocol

from salesfinder.db.models.contacts import TITLE_CATEGORIES

CATEGORIES = TITLE_CATEGORIES


def _rx(*alternatives: str) -> re.Pattern[str]:
    """Case-insensitive whole-word alternation (a word edge is any non-alphanumeric)."""
    return re.compile(rf"(?<![a-z0-9])(?:{'|'.join(alternatives)})(?![a-z0-9])", re.IGNORECASE)


# First match wins, in this order.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("procurement", _rx(r"purchas[a-z]*", "procurement", "buyer", "sourcing")),
    ("supply_chain", _rx(r"supply\s+chain", "logistics", r"materials?\s+manager")),
    (
        "rnd",
        _rx(r"r\s*&\s*d", "research", r"formulat[a-z]*", r"technical\s+director", "chemist"),
    ),
    # Specific sales/business phrases outrank the generic "director"/"president" below.
    (
        "business_dev",
        _rx(
            r"sales\s+director",
            r"director\s*(?:of|[-\u2013\u2014,:|])?\s*sales",
            r"vp\s*[-\u2013\u2014,:|]?\s*sales",
            r"vice\s+president\s*(?:of|[-\u2013\u2014,:|])?\s*sales",
            r"head\s+of\s+sales",
            r"sales\s+head",
            r"business\s+development",
        ),
    ),
    (
        "owner_exec",
        _rx(
            "owner",
            "founder",
            "co-founder",
            "proprietor",
            "proprietress",
            "partner",
            "president",
            "ceo",
            r"chief\s+executive",
            r"managing\s+director",
            "md",
            "director",
            "chairman",
            "chairperson",
            "promoter",
            r"general\s+manager",
        ),
    ),
    ("export", _rx(r"export[a-z]*", r"international\s+sales")),
    ("business_dev", _rx("bd")),
    ("marketing", _rx("marketing")),
)

# Generic job-title words: a "Name - Title" line needs at least one of these (or a categorized
# keyword) on the title side.
_TITLE_WORDS = _rx(
    "manager",
    "head",
    "director",
    "officer",
    "executive",
    "engineer",
    "chemist",
    "scientist",
    "technologist",
    "accountant",
    "auditor",
    "treasurer",
    "secretary",
    "consultant",
    "advis[eo]r",
    "lead",
    "specialist",
    "analyst",
    "co-?ordinator",
    "supervisor",
    "superintendent",
    "controller",
    "administrator",
    "representative",
    "associate",
    "assistant",
    "trustee",
    "principal",
    "in-?charge",
    "trainee",
    "ceo",
    "cfo",
    "coo",
    "cto",
    "cmo",
    "md",
    "gm",
    "dgm",
    "agm",
    "vp",
    "avp",
    "svp",
    "evp",
)

# Words that mark a label rather than a job title ("Head Office", "Email", "Address").
_NOT_TITLE = _rx(
    "office",
    "address",
    "phone",
    "telephone",
    "tel",
    "mobile",
    "e-?mail",
    "fax",
    "website",
    "road",
    "street",
    "floor",
    "building",
    "nagar",
    "timings?",
    "hours",
    "headquarters",
    "quarters",
    "map",
    # another company's name, not a role here ("Purchase Manager, XYZ Pharma Ltd")
    "ltd",
    "limited",
    "pvt",
    "inc",
    "llc",
    "llp",
    "plc",
    "gmbh",
    "corp",
    "corporation",
    "industries",
    "pharma",
    "pharmaceuticals",
    "chemicals",
    "enterprises",
    # a business relationship, not a job ("Our Partner", "Supplier Partner")
    r"(?:our|supplier|suppliers|supply|channel|key|technology|knowledge|trusted|preferred"
    r"|official|business|strategic|distribution|logistics|trade|trading|export|global)"
    r"\s+partners?",
)


def categorize_title(title: str) -> str:
    """The first category whose keywords occur in ``title`` (whole words), else ``other``."""
    for category, regex in _RULES:
        if regex.search(title):
            return category
    return "other"


# A role at some other company: "Purchasing Manager at OtherCo", "... from OtherCo", "... @ X".
_ELSEWHERE = re.compile(r"\s(?i:at|from)\s+[A-Z]|@")


def has_title_word(title: str) -> bool:
    """Whether ``title`` reads like a job title here: a title word, and no digits, label word
    or other company ("... at OtherCo")."""
    if (
        not title
        or any(c.isdigit() for c in title)
        or _NOT_TITLE.search(title)
        or _ELSEWHERE.search(title)
    ):
        return False
    return _TITLE_WORDS.search(title) is not None or categorize_title(title) != "other"


_PRIORITY: dict[str, tuple[str, ...]] = {
    "buyer": (
        "procurement",
        "supply_chain",
        "rnd",
        "owner_exec",
        "business_dev",
        "marketing",
        "export",
    ),
    "supplier": (
        "export",
        "owner_exec",
        "business_dev",
        "marketing",
        "procurement",
        "supply_chain",
        "rnd",
    ),
}


class _Categorized(Protocol):
    @property
    def category(self) -> str: ...


def rank_people[P: _Categorized](people: Iterable[P], *, side: str, limit: int) -> list[P]:
    """The top ``limit`` people by the side's category priority (stable within a category);
    ``other`` is never targeted. An unknown side ranks like a buyer."""
    order = _PRIORITY.get(side, _PRIORITY["buyer"])
    rank = {category: i for i, category in enumerate(order)}
    eligible = [p for p in people if p.category in rank]
    eligible.sort(key=lambda p: rank[p.category])
    return eligible[: max(limit, 0)]
