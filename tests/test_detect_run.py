import pytest

from salesfinder.catalog import load_catalog, load_facts
from salesfinder.config import load_config
from salesfinder.signals.catalog import SIGNALS
from salesfinder.signals.run import run_detectors
from salesfinder.signals.text import extract


@pytest.fixture(scope="module")
def env(shipped_config_dir):
    return (
        load_catalog(shipped_config_dir),
        load_facts(shipped_config_dir),
        load_config(shipped_config_dir),
    )


def _run(env, pages, side, codes, fallback=None):
    catalog, facts, config = env
    return run_detectors(
        pages,
        side=side,
        codes=frozenset(codes),
        catalog=catalog,
        facts=facts,
        config=config,
        region_fallback=fallback,
    )


def test_only_confirmed_codes_and_order(env):
    pages = [
        extract(
            "<p>Visit us at 100 Main St, Newark, NJ 07102. ISO 9001 certified.</p>",
            "https://a.com/contact",
        )
    ]
    out = _run(env, pages, "buyer", {"QUALITY_CERTS", "NEAR_WAREHOUSE"})
    assert [f.code for f in out] == ["NEAR_WAREHOUSE", "QUALITY_CERTS"]
    assert "warehouse" in out[0].value
    assert out[1].value == ["ISO 9001"]


def test_order_follows_signals(env):
    codes = {s.code for s in SIGNALS if s.side == "buyer"}
    out = _run(env, [extract("<p>hello</p>", "https://a.com/")], "buyer", codes)
    assert [f.code for f in out] == [s.code for s in SIGNALS if s.side == "buyer"]


def test_supplier_side_ignores_buyer_codes(env):
    pages = [extract("<p>We export to USA. 10 TPM capacity.</p>", "https://a.com/")]
    out = _run(env, pages, "supplier", {"QUALITY_CERTS", "EXPORTS_TO_US", "STATED_CAPACITY"})
    assert [f.code for f in out] == ["EXPORTS_TO_US", "STATED_CAPACITY"]
    all_supplier = {s.code for s in SIGNALS if s.side == "supplier"}
    out = _run(env, pages, "supplier", all_supplier, fallback="India")
    assert [f.code for f in out] == [s.code for s in SIGNALS if s.side == "supplier"]
    assert next(f for f in out if f.code == "REGION").value == "India"


def test_no_codes_and_unknown_side(env):
    assert _run(env, [], "buyer", set()) == []
    assert _run(env, [], "buyer", {"EXPORTS"}) == []
