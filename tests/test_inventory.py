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


def test_name_with_trailing_newline_is_rejected():
    with pytest.raises(InventoryError, match="name"):
        parse_inventory({"devices": [{"name": "core\n", "address": "192.0.2.1"}]})


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -float("inf"), 10**400])
def test_non_finite_or_absurd_numbers_are_rejected(bad):
    with pytest.raises(InventoryError, match="poll_interval_s"):
        parse_inventory({"poll_interval_s": bad, "devices": [{"name": "a", "address": "x"}]})
    with pytest.raises(InventoryError, match="loss"):
        parse_inventory({"devices": [{"name": "a", "address": "x", "sim": {"loss": bad}}]})


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    f = tmp_path / "dup.yaml"
    f.write_text(
        "devices:\n  - {name: a, address: 192.0.2.1}\n"
        "devices:\n  - {name: b, address: 192.0.2.2}\n",
        encoding="utf-8",
    )
    with pytest.raises(InventoryError, match="duplicate key 'devices'"):
        load_inventory(f)


def test_duplicate_key_inside_a_device_is_rejected(tmp_path):
    f = tmp_path / "dup2.yaml"
    f.write_text(
        "devices:\n  - name: a\n    address: 192.0.2.1\n    address: 192.0.2.99\n",
        encoding="utf-8",
    )
    with pytest.raises(InventoryError, match="duplicate key 'address'"):
        load_inventory(f)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_inventory_saved_by_windows_editors_still_loads(tmp_path, encoding):
    # Notepad writes UTF-8 with a BOM; Windows PowerShell's ">" writes UTF-16.
    f = tmp_path / "win.yaml"
    f.write_bytes("devices:\n  - {name: a, address: 192.0.2.1}\n".encode(encoding))
    assert [d.name for d in load_inventory(f).devices] == ["a"]


def test_undecodable_inventory_gives_a_friendly_error(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_bytes(b"devices: \xff\xfe\x00 not text")
    with pytest.raises(InventoryError, match="invalid YAML"):
        load_inventory(f)
