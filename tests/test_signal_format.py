"""format_value: observed signal values as readable text (pure)."""

from pathlib import Path

import pytest

from salesfinder.catalog import Catalog, load_catalog
from salesfinder.web.signal_format import format_value

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_catalog(CONFIG)


def test_product_ids_become_names(catalog: Catalog) -> None:
    assert format_value("USES_CATALOG_CHEMICAL", ["sles", "capb"], catalog) == (
        "Sodium Laureth Sulfate (SLES 70%), Cocamidopropyl Betaine (CAPB)"
    )
    assert format_value("MAKES_CATALOG_CHEMICAL", ["gone_id"], catalog) == "gone_id"


def test_industries_and_certs(catalog: Catalog) -> None:
    assert format_value("INDUSTRY_MATCH", ["oil_gas", "mystery"], catalog) == "Oil & Gas, mystery"
    assert format_value("QUALITY_CERTS", ["ISO 9001", "GMP"], catalog) == "ISO 9001, GMP"


def test_objects(catalog: Catalog) -> None:
    assert format_value("US_LOCATION", {"state": "TX", "city": "Houston"}, catalog) == (
        "Houston, TX"
    )
    assert format_value("US_LOCATION", {"state": "TX"}, catalog) == "TX"
    near = {"warehouse": "Houston, TX", "miles": 320}
    assert format_value("NEAR_WAREHOUSE", near, catalog) == "320 miles from Houston, TX"
    cap = {"text": "5,000 MT per month", "mt_per_year": 60000}
    assert format_value("STATED_CAPACITY", cap, catalog) == ("60,000 MT/year (5,000 MT per month)")
    assert format_value("STATED_CAPACITY", {"text": "lots"}, catalog) == "lots"


def test_hiring(catalog: Catalog) -> None:
    assert format_value(
        "HIRING_PROCUREMENT", {"hiring": True, "roles": ["procurement", "sourcing"]}, catalog
    ) == ("Yes — procurement, sourcing")
    assert format_value("HIRING_PROCUREMENT", {"hiring": True, "roles": []}, catalog) == "Yes"
    assert format_value(
        "HIRING_PROCUREMENT", {"hiring": False, "roles": [], "careers_page": False}, catalog
    ) == ("No careers page found")
    assert format_value("HIRING_PROCUREMENT", {"hiring": False, "roles": []}, catalog) == "No"


def test_booleans_enums_and_negatives(catalog: Catalog) -> None:
    assert format_value("EXPORTS", True, catalog) == "Yes"
    assert format_value("EXPORTS", False, catalog) == "No"
    assert format_value("COMPANY_TYPE", "contract_manufacturer", catalog) == (
        "contract manufacturer"
    )
    assert format_value("COMPANY_TYPE", "unknown", catalog) == "Not found"
    assert format_value("INDUSTRY_MATCH", [], catalog) == "Not found"
    assert format_value("US_LOCATION", {"found": False}, catalog) == "Not found"
    assert format_value("STATED_CAPACITY", {"found": False}, catalog) == "Not found"
    assert format_value("REGION", "", catalog) == "Not found"
    assert format_value("REGION", "India", catalog) == "India"
    assert format_value("QUALITY_CERTS", None, catalog) == "Not found"
    assert format_value("US_LOCATION", 42, catalog) == "Not found"


def test_capacity_numbers_too_big_for_a_float(catalog: Catalog) -> None:
    huge = {"text": "lots", "mt_per_year": 10**400}
    assert format_value("STATED_CAPACITY", huge, catalog) == "lots"
    assert format_value("NEAR_WAREHOUSE", {"miles": 10**400}, catalog) == "Not found"
