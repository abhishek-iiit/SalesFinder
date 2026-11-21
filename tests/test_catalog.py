from pathlib import Path

import pytest

from salesfinder.catalog import load_catalog, load_facts
from salesfinder.config import ConfigError


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "catalog.yaml").write_text(body)
    return tmp_path


def _product(**over: str) -> str:
    fields = {
        "id": "x",
        "name": "X",
        "search_name": "x",
        "cas": "[]",
        "industries": "[personal_care]",
        "sides": '["buyer", "supplier"]',
    }
    fields.update(over)
    return "  - " + "\n    ".join(f"{k}: {v}" for k, v in fields.items()) + "\n"


def test_shipped_catalog_and_facts_load():
    cat = load_catalog(Path("config"))
    facts = load_facts(Path("config"))
    assert cat.by_id("sles").cas == ["68585-34-2", "9004-82-4"]
    assert len(cat.products) == 20 and all(not p.confirmed for p in cat.products)
    assert cat.by_id("monk_fruit_extract").cas == []
    assert {w.state for w in facts.warehouses} == {"NJ", "TX", "IL", "CA"}
    coords = {w.label: (w.lat, w.lon) for w in facts.warehouses}
    assert coords == {
        "Bridgewater, NJ": (40.59, -74.62),
        "Houston, TX": (29.76, -95.37),
        "Illinois": (41.88, -87.63),
        "Santa Fe Springs, CA": (33.94, -118.08),
    }
    assert all(not c.confirmed for c in facts.claims)
    claim_ids = [c.id for c in facts.claims]
    assert len(claim_ids) == len(set(claim_ids))
    assert "us_distributor" in claim_ids
    assert set(facts.personas) == {"buyer", "supplier"}


def test_bad_cas_names_file_and_key(tmp_path):
    _write(tmp_path, "products:\n" + _product(cas='["12-3"]'))
    with pytest.raises(ConfigError) as e:
        load_catalog(tmp_path)
    assert "catalog.yaml" in str(e.value) and "cas" in str(e.value)


def test_unknown_industry_rejected(tmp_path):
    _write(tmp_path, "products:\n" + _product(industries="[plastics]"))
    with pytest.raises(ConfigError, match="plastics"):
        load_catalog(tmp_path)


def test_duplicate_product_ids_rejected(tmp_path):
    _write(tmp_path, "products:\n" + _product() + _product())
    with pytest.raises(ConfigError, match="duplicate"):
        load_catalog(tmp_path)


def test_for_side(tmp_path):
    _write(tmp_path, "products:\n" + _product(id="a", sides='["buyer"]') + _product(id="b"))
    cat = load_catalog(tmp_path)
    assert [p.id for p in cat.for_side("supplier")] == ["b"]
    assert [p.id for p in cat.for_side("buyer")] == ["a", "b"]
    with pytest.raises(KeyError):
        cat.by_id("nope")


def test_missing_file_names_file(tmp_path):
    with pytest.raises(ConfigError, match=r"facts\.yaml"):
        load_facts(tmp_path)
