"""The inventory's optional `wifi:` block: opt-in by default, strict checks, shipped examples."""

import pytest

from helpers import REPO_ROOT
from netmonlt.domain import WifiConfig, WifiSimProfile
from netmonlt.inventory import InventoryError, load_inventory, parse_inventory

DEVICE = {"name": "a", "address": "192.0.2.1"}


def parse(wifi, mode="simulated"):
    devices = [{**DEVICE, "authorized": True}] if mode == "real" else [DEVICE]
    raw = {"mode": mode, "wifi": wifi, "devices": devices}
    if mode == "real":
        raw["poll_interval_s"] = 10
    return parse_inventory(raw).wifi


def test_wifi_monitoring_is_off_unless_the_inventory_enables_it():
    assert parse_inventory({"devices": [DEVICE]}).wifi == WifiConfig()
    assert WifiConfig().enabled is False
    assert parse(None).enabled is False and parse({}).enabled is False


def test_defaults_when_only_enabled():
    cfg = parse({"enabled": True})
    assert cfg.enabled is True and cfg.poll_interval_s == 10.0 and cfg.backend == "auto"
    assert cfg.ssid_privacy == "masked"  # the privacy-preserving default
    assert (cfg.low_signal_pct, cfg.warn_after_polls, cfg.retention_days) == (40, 3, 7)
    assert cfg.sim == WifiSimProfile()


def test_every_setting_can_be_overridden():
    cfg = parse({
        "enabled": True, "poll_interval_s": 15, "backend": "netsh", "ssid_privacy": "hidden",
        "low_signal_pct": 55, "warn_after_polls": 5, "retention_days": 30,
        "sim": {"ssid": "Lab", "base_signal_pct": 90, "dip_at_s": 10, "dip_duration_s": 20,
                "dip_signal_pct": 15, "disconnect_at_s": None, "disconnect_duration_s": 5},
    })
    assert (cfg.poll_interval_s, cfg.backend, cfg.ssid_privacy) == (15.0, "netsh", "hidden")
    assert (cfg.low_signal_pct, cfg.warn_after_polls, cfg.retention_days) == (55, 5, 30)
    assert cfg.sim == WifiSimProfile("Lab", 90, 10.0, 20.0, 15, None, 5.0)


@pytest.mark.parametrize("wifi, message", [
    ("yes", "must be a mapping"),
    ({"enable": True}, "unknown key"),
    ({"enabled": "true"}, "true or false"),
    ({"enabled": 1}, "true or false"),
    ({"backend": "scan"}, "wifi.backend must be one of"),
    ({"ssid_privacy": "visible"}, "wifi.ssid_privacy must be one of"),
    ({"poll_interval_s": 0.5}, "wifi.poll_interval_s"),
    ({"poll_interval_s": "fast"}, "must be a number"),
    ({"low_signal_pct": 101}, "wifi.low_signal_pct"),
    ({"low_signal_pct": -1}, "wifi.low_signal_pct"),
    ({"low_signal_pct": 40.5}, "whole number"),
    ({"warn_after_polls": 0}, "wifi.warn_after_polls"),
    ({"retention_days": 0}, "wifi.retention_days"),
    ({"retention_days": 1000}, "wifi.retention_days"),
    ({"sim": "x"}, "wifi.sim must be a mapping"),
    ({"sim": {"colour": "red"}}, "unknown key"),
    ({"sim": {"ssid": ""}}, "wifi.sim.ssid"),
    ({"sim": {"ssid": "x" * 33}}, "wifi.sim.ssid"),
    ({"sim": {"ssid": 5}}, "wifi.sim.ssid"),
    ({"sim": {"base_signal_pct": 0}}, "base_signal_pct"),
    ({"sim": {"dip_signal_pct": 101}}, "dip_signal_pct"),
    ({"sim": {"dip_duration_s": 0}}, "dip_duration_s"),
    ({"sim": {"dip_at_s": -5}}, "dip_at_s"),
])
def test_bad_wifi_settings_are_rejected_with_a_clear_message(wifi, message):
    with pytest.raises(InventoryError, match=message):
        parse(wifi)


def test_real_mode_has_a_gentler_minimum_poll_interval_than_the_simulation():
    assert parse({"enabled": True, "poll_interval_s": 1}).poll_interval_s == 1.0
    with pytest.raises(InventoryError, match="wifi.poll_interval_s"):
        parse({"enabled": True, "poll_interval_s": 2}, mode="real")
    assert parse({"enabled": True, "poll_interval_s": 5}, mode="real").poll_interval_s == 5.0


def test_the_block_is_also_valid_in_real_mode():
    cfg = parse({"enabled": True, "backend": "wlanapi"}, mode="real")
    assert cfg.enabled and cfg.backend == "wlanapi"


def test_the_wifi_block_cannot_name_a_network_or_an_adapter_to_monitor():
    # it is local-only by construction: there is no key for a target, SSID or scan
    for key in ("ssid", "networks", "scan", "target", "bssid", "interface", "address"):
        with pytest.raises(InventoryError, match="unknown key"):
            parse({key: "x"})


# ------------------------------------------------------------------ shipped examples
def test_the_shipped_demo_inventory_enables_simulated_wifi_with_safe_settings():
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    assert inv.mode == "simulated" and inv.wifi.enabled is True
    assert inv.wifi.ssid_privacy == "masked" and inv.wifi.poll_interval_s == 5
    assert inv.wifi.sim.dip_signal_pct < inv.wifi.low_signal_pct < inv.wifi.sim.base_signal_pct


def test_the_real_example_keeps_wifi_off_and_documents_the_opt_in():
    path = REPO_ROOT / "config" / "devices.real.example.yaml"
    assert load_inventory(path).wifi.enabled is False
    text = path.read_text(encoding="utf-8")
    assert "# wifi:" in text and "never scans" in text and "masked" in text
