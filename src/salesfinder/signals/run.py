"""Run the confirmed detectors for one side over a company's pages. Pure."""

from collections.abc import Callable, Sequence

from salesfinder.catalog import Catalog, Facts
from salesfinder.config import AppConfig
from salesfinder.signals.catalog import SIGNALS
from salesfinder.signals.detect_buyer import detect_company_type, detect_hiring
from salesfinder.signals.detect_common import (
    Finding,
    detect_certs,
    detect_chemicals,
    detect_industries,
    detect_near_warehouse,
    detect_region,
    detect_us_location,
)
from salesfinder.signals.detect_supplier import (
    detect_capacity,
    detect_exports,
    detect_manufacturer_or_trader,
)
from salesfinder.signals.text import PageText


def run_detectors(
    pages: Sequence[PageText],
    *,
    side: str,
    codes: frozenset[str],
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
    region_fallback: str | None,
) -> list[Finding]:
    """Findings for the confirmed ``codes`` of ``side``, in ``SIGNALS`` order."""
    wanted = [s.code for s in SIGNALS if s.side == side and s.code in codes]
    if not wanted:
        return []

    def exports(index: int) -> Callable[[], Finding]:
        return lambda: detect_exports(pages)[index]

    discovery = config.discovery
    builders: dict[str, Callable[[], Finding]] = {
        "INDUSTRY_MATCH": lambda: detect_industries(pages, discovery.industry_keywords),
        "USES_CATALOG_CHEMICAL": lambda: detect_chemicals(
            pages, catalog, code="USES_CATALOG_CHEMICAL"
        ),
        "COMPANY_TYPE": lambda: detect_company_type(pages),
        "US_LOCATION": lambda: detect_us_location(pages),
        "NEAR_WAREHOUSE": lambda: detect_near_warehouse(detect_us_location(pages), facts),
        "HIRING_PROCUREMENT": lambda: detect_hiring(pages),
        "QUALITY_CERTS": lambda: detect_certs(pages, code="QUALITY_CERTS"),
        "MAKES_CATALOG_CHEMICAL": lambda: detect_chemicals(
            pages, catalog, code="MAKES_CATALOG_CHEMICAL"
        ),
        "MANUFACTURER_NOT_TRADER": lambda: detect_manufacturer_or_trader(pages),
        "EXPORTS": exports(0),
        "EXPORTS_TO_US": exports(1),
        "SUPPLIER_CERTS": lambda: detect_certs(pages, code="SUPPLIER_CERTS"),
        "STATED_CAPACITY": lambda: detect_capacity(pages),
        "REGION": lambda: detect_region(pages, discovery.supplier_regions, region_fallback),
    }
    return [builders[code]() for code in wanted]
