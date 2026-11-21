import json

import pytest

from salesfinder.signals.detect_supplier import (
    detect_capacity,
    detect_exports,
    detect_manufacturer_or_trader,
)
from salesfinder.signals.text import PageText, extract


def page(text: str, url: str = "https://a.com/") -> PageText:
    return extract(f"<p>{text}</p>", url)


def test_manufacturer():
    f = detect_manufacturer_or_trader(
        [page("A manufacturer and exporter with installed capacity. Visit our factory.")]
    )
    assert (f.code, f.value) == ("MANUFACTURER_NOT_TRADER", "manufacturer")
    assert f.evidence is not None


def test_trader():
    f = detect_manufacturer_or_trader([page("Leading traders and stockist. A trading company.")])
    assert f.value == "trader"


def test_unclear():
    assert detect_manufacturer_or_trader([page("Chemicals for all.")]).value == "unclear"
    assert detect_manufacturer_or_trader([page("We manufacture. Dealer.")]).value == "unclear"
    f = detect_manufacturer_or_trader(
        [page("our factory, reactor, trader, dealer, stockist, traders")]
    )
    assert f.value == "trader"


def test_exports_to_us():
    e, us = detect_exports([page("We export to USA, UAE and Europe.")])
    assert (e.code, e.value, us.code, us.value) == ("EXPORTS", True, "EXPORTS_TO_US", True)
    assert e.evidence is not None and us.evidence is not None
    e, us = detect_exports([page("We export to Europe.")])
    assert (e.value, us.value) == (True, False)
    e, us = detect_exports([page("Local sales only. USA office.")])
    assert (e.value, us.value) == (False, False)
    assert e.evidence is None


def test_exports_us_must_be_near():
    filler = "x " * 200
    e, us = detect_exports([page(f"We export worldwide. {filler} Our USA office.")])
    assert (e.value, us.value) == (True, False)


@pytest.mark.parametrize(
    "phrase,mt",
    [
        ("capacity of 5,000 MT per month", 60000),
        ("1200 TPA", 1200),
        ("0.5 MTPA", 500000),
        ("installed capacity 300 tonnes/month", 3600),
        ("2,500 KL annually", 2500),
        ("100 Metric Tons per year", 100),
        ("10 TPM", 120),
    ],
)
def test_capacity(phrase, mt):
    f = detect_capacity([page(phrase)])
    assert f.code == "STATED_CAPACITY"
    assert f.value["mt_per_year"] == mt
    assert isinstance(f.value["mt_per_year"], int)
    assert f.value["text"] in phrase
    assert f.evidence is not None
    json.dumps(f.value)


def test_capacity_largest_wins_and_not_found():
    f = detect_capacity([page("100 TPA unit and a 20 TPM unit and 5 MT per year")])
    assert f.value["mt_per_year"] == 240
    g = detect_capacity([page("We ship 500 MT of goods. Founded 2005 tons of love.")])
    assert g.value == {"found": False}
    assert g.evidence is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We export to U.S.A. clients", (True, True)),
        ("We export to U.S.A clients", (True, True)),
        ("Exports to the US Market", (True, True)),
        ("export to us soon", (True, False)),
        ("We export to North America", (True, True)),
    ],
)
def test_exports_us_variants(text, expected):
    e, us = detect_exports([page(text)])
    assert (e.value, us.value) == expected


def test_exports_linear_time():
    import time

    filler = " " + "x " * 150
    big = page(("export" + filler) * 20000 + ("USA" + filler) * 20000)
    start = time.perf_counter()
    e, us = detect_exports([big])
    # Guards against quadratic matching: a quadratic scan of this ~12 MB page takes minutes,
    # so 5 s keeps the guarantee without failing on a busy machine (1 s flaked under load).
    assert time.perf_counter() - start < 5
    assert (e.value, us.value) == (True, False)
    near = page("export " + "x " * 10 + "USA")
    assert tuple(f.value for f in detect_exports([near])) == (True, True)


# --- final review fixes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Leading Manufacturers &amp; Exporters of SLES. Visit our factory.",
        "Manufacturer, Exporter and Supplier of SLES. Visit our factory.",
        "Manufacturers and exporters of surfactants. Our factory is in Gujarat.",
        "Manufacturer &amp; Exporter of SLES. Visit our factory.",
    ],
)
def test_manufacturer_exporter_variants(text):
    assert detect_manufacturer_or_trader([page(text)]).value == "manufacturer"


def test_manufacturer_exporter_needs_both_words():
    # "Manufacturers" alone is not one of the phrases; one hit isn't enough either way.
    assert detect_manufacturer_or_trader([page("Manufacturers welcome.")]).value == "unclear"
    assert detect_manufacturer_or_trader([page("Manufacturersand exporters")]).value == "unclear"


@pytest.mark.parametrize(
    "phrase,mt",
    [
        ("0.5 MTPA", 500_000),
        ("2 MTPA", 2_000_000),
        ("99.5 MTPA", 99_500_000),
        ("100 MTPA", 100),
        ("24,000 MTPA", 24_000),
        ("60000 MTPA", 60_000),
    ],
)
def test_mtpa_million_tonnes_only_below_100(phrase, mt):
    assert detect_capacity([page(phrase)]).value["mt_per_year"] == mt


def test_capacity_ignores_numbers_over_12_digits():
    f = detect_capacity([page("9" * 400 + " TPA and 1,234 TPA")])
    assert f.value["mt_per_year"] == 1234
    g = detect_capacity([page("1234567890123 MTPA")])  # 13 digits
    assert g.value == {"found": False}
    h = detect_capacity([page("123,456,789,012 TPA")])  # 12 digits: kept
    assert h.value["mt_per_year"] == 123_456_789_012
