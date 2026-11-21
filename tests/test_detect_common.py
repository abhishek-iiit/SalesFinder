import json

import pytest

from salesfinder.catalog import load_catalog, load_facts
from salesfinder.config import load_config
from salesfinder.signals.detect_common import (
    Finding,
    detect_certs,
    detect_chemicals,
    detect_industries,
    detect_near_warehouse,
    detect_region,
    detect_us_location,
)
from salesfinder.signals.text import PageText, extract


def page(text: str, url: str = "https://a.com/") -> PageText:
    return extract(f"<p>{text}</p>", url)


@pytest.fixture(scope="module")
def catalog(shipped_config_dir):
    return load_catalog(shipped_config_dir)


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("We buy Sodium Laureth Sulphate in bulk", ["sles"]),
        ("SLES-70 and CAPB for shampoos", ["capb", "sles"]),
        ("CAS No. 68585-34-2", ["sles"]),
        ("caustic soda flakes and soda ash", ["caustic_soda", "soda_ash"]),
        ("SLESSOR Industries", []),
        ("glycerol USP", ["glycerine"]),
        ("CAS# 1310-73-2 and cas 1310-73-23", ["caustic_soda"]),
        ("Sodium-Laureth sulfate", ["sles"]),
        ("Potassium hydroxide, hydrochloric acid", ["hcl", "koh"]),
        ("tankoh", []),
        ("KOH flakes 90%", ["koh"]),
        ("Muriatic acid 33%", ["hcl"]),
        ("HCl 33%", ["hcl"]),
        ("HCL Technologies", []),
        ("sles lowercase", []),
        ("SLES lowercase", ["sles"]),
        ("CAS 1310\u201373\u20132", ["caustic_soda"]),
    ],
)
def test_chemicals(catalog, phrase, expected):
    finding = detect_chemicals([page(phrase)], catalog, code="CHEMICALS")
    assert finding.code == "CHEMICALS"
    assert finding.value == expected
    assert (finding.evidence is None) == (expected == [])


def test_chemicals_evidence_is_first_match_with_url(catalog):
    pages = [
        page("nothing here", "https://a.com/x"),
        page("We sell citric acid", "https://a.com/y"),
    ]
    finding = detect_chemicals(pages, catalog, code="C")
    assert finding.evidence is not None
    assert finding.evidence.url == "https://a.com/y"
    assert "citric acid" in finding.evidence.snippet


def test_chemicals_large_page_is_fast(catalog):
    big = page("lorem ipsum dolor " * 50000 + " SLES")
    assert detect_chemicals([big], catalog, code="C").value == ["sles"]


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("ISO 9001:2015 certified", ["ISO 9001"]),
        ("cGMP facility, Halal-certified", ["GMP", "Halal"]),
        ("Call +1 800 900 1234", []),
        ("REACH registered substances", ["REACH"]),
        ("We reach customers worldwide", []),
        ("ISO9001 and ISO 14001, FSSC 22000", ["FSSC 22000", "ISO 14001", "ISO 9001"]),
        ("tel 5550 ISO 90012 or 1900199001", []),
        ("Kosher, RSPO, ECOCERT, COSMOS, USDA Organic, FDA-registered, BRC, NSF", None),
    ],
)
def test_certs(phrase, expected):
    finding = detect_certs([page(phrase)], code="CERTS")
    if expected is None:
        expected = sorted(
            ["Kosher", "RSPO", "ECOCERT", "COSMOS", "USDA Organic", "FDA registered", "BRC", "NSF"]
        )
    assert finding.value == expected


def test_industries():
    kw = {"personal_care": ["shampoo", "skin care"], "home_care": ["detergent"]}
    finding = detect_industries([page("shampoo and skin care manufacturer")], kw)
    assert finding.code == "INDUSTRY_MATCH"
    assert finding.value == ["personal_care"]
    assert finding.evidence is not None
    none = detect_industries([page("shampooing detergentsX")], kw)
    assert none.value == [] and none.evidence is None


def test_industries_label_phrase():
    finding = detect_industries([page("we serve Oil and Gas operators")], {})
    assert finding.value == ["oil_gas"]


def test_us_location():
    f = detect_us_location([page("1200 Main St, Houston, TX 77002")])
    assert f.code == "US_LOCATION"
    assert f.value == {"state": "TX", "city": "Houston"}
    assert detect_us_location([page("Houston, Texas 77002")]).value == {
        "state": "TX",
        "city": "Houston",
    }
    assert detect_us_location([page("New Brunswick, New Jersey 08901-1234")]).value == {
        "state": "NJ",
        "city": "New Brunswick",
    }


@pytest.mark.parametrize(
    "text,city",
    [
        ("12 Main St Houston, TX 77002", "Houston"),
        ("Suite 200 Bridgewater, NJ 08807", "Bridgewater"),
        ("San Antonio, TX 78205", "San Antonio"),
        ("Acme Corp, 500 Oak Avenue Floor 3 Newark, NJ 07102", "Newark"),
        ("1200 Main St, Houston, TX 77002", "Houston"),
    ],
)
def test_us_location_city_drops_street_words(text: str, city: str) -> None:
    assert detect_us_location([page(text)]).value == {
        "state": text.split(", ")[-1].split()[0],
        "city": city,
    }


def test_us_location_prefers_contact_and_not_found():
    pages = [
        page("Dallas, TX 75001", "https://a.com/"),
        page("Newark, NJ 07102", "https://a.com/contact-us"),
    ]
    assert detect_us_location(pages).value["state"] == "NJ"
    nope = detect_us_location([page("Houston, in 77002 and Paris, France 75001")])
    assert nope.value == {"found": False} and nope.evidence is None


def test_near_warehouse(shipped_config_dir):
    facts = load_facts(shipped_config_dir)

    def near(text: str) -> Finding:
        return detect_near_warehouse(detect_us_location([page(text)]), facts)

    nj = near("Newark, NJ 07102")
    assert nj.code == "NEAR_WAREHOUSE"
    assert nj.value["warehouse"] == "Bridgewater, NJ" and nj.value["miles"] < 60
    assert near("Miami, FL 33101").value["warehouse"] == "Houston, TX"
    assert near("Los Angeles, CA 90001").value["warehouse"] == "Santa Fe Springs, CA"
    assert near("nothing").value == {"found": False}


def test_region(shipped_config_dir):
    regions = load_config(shipped_config_dir).discovery.supplier_regions
    contact = page("Plot 12, GIDC Vapi, Gujarat 396195", "https://a.com/contact")
    f = detect_region([contact], regions, "Maharashtra")
    assert f.code == "REGION" and f.value == "Gujarat" and f.evidence is not None
    # not a contact/about page, or no address-like context -> fallback
    other = page("Plot 12, GIDC Vapi, Gujarat 396195", "https://a.com/blog")
    assert detect_region([other], regions, "Tamil Nadu").value == "Tamil Nadu"
    far = page("We ship to gujarat. " + "x " * 100 + "Plot 5", "https://a.com/about")
    assert detect_region([far], regions, None).value is None
    ci = page("Unit 4, Industrial Area, TELANGANA", "https://a.com/about-us")
    assert detect_region([ci], regions, None).value == "Telangana"
    nothing = detect_region([page("hello", "https://a.com/contact")], regions, "Gujarat")
    assert nothing.value == "Gujarat" and nothing.evidence is None


def test_not_found_has_no_evidence(catalog):
    assert detect_chemicals([], catalog, code="C").evidence is None
    assert detect_certs([], code="C").evidence is None
    assert detect_us_location([]).evidence is None


def test_findings_are_json_serializable(catalog):
    p = page("SLES, ISO 9001, Houston, TX 77002")
    for f in (
        detect_chemicals([p], catalog, code="C"),
        detect_certs([p], code="C"),
        detect_us_location([p]),
    ):
        json.dumps(f.value)
        assert f.evidence is None or len(f.evidence.snippet) <= 300


def test_state_names_cover_centroids():
    from salesfinder.signals.detect_common import _STATE_NAMES
    from salesfinder.signals.geo import STATE_CENTROIDS

    assert set(_STATE_NAMES.values()) == set(STATE_CENTROIDS)
