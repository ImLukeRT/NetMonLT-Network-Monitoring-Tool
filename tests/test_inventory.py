import pytest

from helpers import REPO_ROOT
from netmonlt.inventory import InventoryError, load_inventory, parse_inventory


def test_shipped_demo_inventory_is_valid():
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    assert inv.mode == "simulated"
    assert len(inv.devices) >= 5


def test_defaults_applied():
    inv = parse_inventory({"devices": [{"name": "a", "address": "192.0.2.1"}]})
    assert inv.poll_interval_s == 5
    assert inv.devices[0].type == "host"
    assert inv.devices[0].sim.loss == 0


def test_sim_profile_parsed():
    inv = parse_inventory({"devices": [{
        "name": "a", "address": "192.0.2.1",
        "sim": {"rtt_ms": 20, "loss": 0.5, "outage_start_s": 10, "outage_duration_s": 5},
    }]})
    sim = inv.devices[0].sim
    assert (sim.rtt_ms, sim.loss, sim.outage_start_s, sim.outage_duration_s) == (20, 0.5, 10, 5)


@pytest.mark.parametrize("raw, message", [
    (None, "mapping"),
    ({}, "non-empty list"),
    ({"devices": []}, "non-empty list"),
    ({"devices": [{"name": "a", "address": "x"}], "typo": 1}, "unknown key"),
    ({"mode": "real", "devices": [{"name": "a", "address": "x"}]}, "not supported"),
    ({"devices": [{"address": "x"}]}, "name"),
    ({"devices": [{"name": "bad name", "address": "x"}]}, "name"),
    ({"devices": [{"name": "a/b", "address": "x"}]}, "name"),
    ({"devices": [{"name": "a"}]}, "address"),
    ({"devices": [{"name": "a", "address": "has space"}]}, "address"),
    ({"devices": [{"name": "a", "address": "x"}, {"name": "a", "address": "y"}]}, "duplicate"),
    ({"devices": [{"name": "a", "address": "x", "sim": {"loss": 2}}]}, "between"),
    ({"devices": [{"name": "a", "address": "x", "sim": {"rtt": 5}}]}, "unknown key"),
    ({"devices": [{"name": "a", "address": "x", "sim": {"loss": True}}]}, "number"),
    ({"devices": [{"name": "a", "address": "x", "sim": {"outage_duration_s": 5}}]},
     "outage_start_s"),
    ({"poll_interval_s": 0, "devices": [{"name": "a", "address": "x"}]}, "poll_interval_s"),
])
def test_invalid_inventories_are_rejected(raw, message):
    with pytest.raises(InventoryError, match=message):
        parse_inventory(raw)


def test_missing_file(tmp_path):
    with pytest.raises(InventoryError, match="not found"):
        load_inventory(tmp_path / "nope.yaml")


def test_malformed_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("devices: [unclosed", encoding="utf-8")
    with pytest.raises(InventoryError, match="invalid YAML"):
        load_inventory(bad)
