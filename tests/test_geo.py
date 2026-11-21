import pytest

from salesfinder.catalog import load_facts
from salesfinder.signals.geo import STATE_CENTROIDS, haversine_miles, nearest_warehouse


def test_all_states_and_dc_present():
    assert len(STATE_CENTROIDS) == 51
    assert "DC" in STATE_CENTROIDS and "AK" in STATE_CENTROIDS and "HI" in STATE_CENTROIDS


def test_haversine():
    assert haversine_miles((40.0, -74.0), (40.0, -74.0)) == 0
    assert 2400 < haversine_miles((40.71, -74.0), (34.05, -118.24)) < 2500


@pytest.mark.parametrize(
    "state,label",
    [
        ("NJ", "Bridgewater, NJ"),
        ("FL", "Houston, TX"),
        ("CA", "Santa Fe Springs, CA"),
        ("IL", "Illinois"),
    ],
)
def test_nearest_warehouse(shipped_config_dir, state, label):
    facts = load_facts(shipped_config_dir)
    warehouse, miles = nearest_warehouse(state, facts.warehouses)
    assert warehouse.label == label
    assert isinstance(miles, int)
    if state == "NJ":
        assert miles < 60


def test_nearest_warehouse_unknown_state(shipped_config_dir):
    facts = load_facts(shipped_config_dir)
    with pytest.raises(KeyError):
        nearest_warehouse("ZZ", facts.warehouses)
