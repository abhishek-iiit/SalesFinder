"""Supplier-specific detectors: manufacturer vs trader, exports, stated capacity. Pure."""

import re
from bisect import bisect_left
from collections.abc import Sequence

from salesfinder.signals.detect_buyer import _count, _first, phrase_regex
from salesfinder.signals.detect_common import Finding, _evidence
from salesfinder.signals.text import PageText

_MANUFACTURER = phrase_regex(
    (
        "manufacturing facility",
        "manufacturing plant",
        "our factory",
        "production capacity",
        "installed capacity",
        "reactor",
        "we manufacture",
    ),
    # "Manufacturer and Exporter", "Manufacturers & Exporters", "Manufacturer, Exporter and
    # Supplier"
    patterns=(r"manufacturers?(?:\s*[,&]\s*|\s+and\s+)exporters?",),
)
_TRADER = phrase_regex(
    (
        "trader",
        "traders",
        "trading company",
        "stockist",
        "dealer",
        "importer and distributor",
        "indenting agent",
    )
)


# The values MANUFACTURER_NOT_TRADER can take besides "unclear" (the cohort form offers these).
PRODUCER_KINDS = ("manufacturer", "trader")


def detect_manufacturer_or_trader(pages: Sequence[PageText]) -> Finding:
    made, traded = _count(pages, _MANUFACTURER), _count(pages, _TRADER)
    if made >= 2 and made > traded:
        return Finding("MANUFACTURER_NOT_TRADER", "manufacturer", _first(pages, _MANUFACTURER))
    if traded >= 2 and traded > made:
        return Finding("MANUFACTURER_NOT_TRADER", "trader", _first(pages, _TRADER))
    return Finding("MANUFACTURER_NOT_TRADER", "unclear", None)


_EXPORT = re.compile(
    r"(?<!\w)(?:export(?:s|ed|er|ers|ing)?|we export to|international markets"
    r"|overseas customers)(?!\w)",
    re.IGNORECASE,
)
_US = re.compile(
    r"(?<!\w)(?:(?-i:U\.S\.A\.?|USA|U\.S\.)|(?-i:US)\s+market|United States|North America)(?!\w)",
    re.IGNORECASE,
)
_US_WINDOW = 200


def detect_exports(pages: Sequence[PageText]) -> tuple[Finding, Finding]:
    exports = Finding("EXPORTS", False, None)
    for page in pages:
        export_matches = list(_EXPORT.finditer(page.text))
        if not export_matches:
            continue
        if not exports.value:
            exports = Finding("EXPORTS", True, _evidence(page, export_matches[0]))
        # Matches don't overlap, so starts and ends are both sorted: the first export ending
        # within the window of a US match has the smallest start of all candidates.
        ends = [m.end() for m in export_matches]
        for us in _US.finditer(page.text):
            i = bisect_left(ends, us.start() - _US_WINDOW)
            if i < len(ends) and export_matches[i].start() - us.end() <= _US_WINDOW:
                return exports, Finding("EXPORTS_TO_US", True, _evidence(page, export_matches[i]))
    return exports, Finding("EXPORTS_TO_US", False, None)


_NUMBER = r"(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MAX_DIGITS = 12  # a longer stated number is noise (and would overflow as a float)
# Below this, "MTPA" is million tonnes per annum ("0.5 MTPA"); from it on, metric tonnes per
# annum ("24,000 MTPA").
_MILLION_TONNES_BELOW = 100
_PERIOD = (
    r"(?:\s*(?:per\s+month|/\s*month|monthly)(?P<month>)"
    r"|\s*(?:per\s+annum|per\s+year|/\s*year|annually)(?P<year>))(?!\w)"
)
_CAPACITY = re.compile(
    _NUMBER + r"\s*(?:"
    r"(?P<mtpa>MTPA)(?!\w)"
    r"|(?P<tpa>TPA)(?!\w)"
    r"|(?P<tpm>TPM)(?!\w)"
    r"|(?:MT|metric\s+tons?|metric\s+tonnes?|tons?|tonnes?|KL)(?![A-Za-z])" + _PERIOD + r")",
    re.IGNORECASE,
)


def detect_capacity(pages: Sequence[PageText]) -> Finding:
    best: tuple[int, PageText, re.Match[str]] | None = None
    for page in pages:
        for m in _CAPACITY.finditer(page.text):
            digits = m.group("num").replace(",", "")
            if len(digits.split(".", 1)[0]) > _MAX_DIGITS:
                continue
            try:
                number = float(digits)
            except ValueError:  # pragma: no cover - the pattern only admits numbers
                continue
            if m.group("mtpa") is not None:
                per_year = number * 1_000_000 if number < _MILLION_TONNES_BELOW else number
            elif m.group("tpa") is not None or m.group("year") is not None:
                per_year = number
            else:  # TPM or an explicit month period
                per_year = number * 12
            value = round(per_year)
            if best is None or value > best[0]:
                best = (value, page, m)
    if best is None:
        return Finding("STATED_CAPACITY", {"found": False}, None)
    value, page, m = best
    return Finding(
        "STATED_CAPACITY", {"text": m.group(0), "mt_per_year": value}, _evidence(page, m)
    )
