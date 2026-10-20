"""The inventory's optional `quality:` block (thresholds and minimum data), and shipped examples."""

import pytest

from helpers import REPO_ROOT
from netmonlt.domain import QualityConfig, QualityThresholds
from netmonlt.inventory import InventoryError, load_inventory, parse_inventory

DEVICE = {"name": "a", "address": "192.0.2.1"}


def parse(quality):
    return parse_inventory({"quality": quality, "devices": [DEVICE]}).quality


def test_no_quality_block_uses_the_documented_defaults():
    inv = parse_inventory({"devices": [DEVICE]})
    assert inv.quality == QualityConfig()
    assert inv.quality.latency_ms == QualityThresholds(30, 80, 150)
    assert inv.quality.jitter_ms == QualityThresholds(5, 15, 30)
    assert inv.quality.loss_pct == QualityThresholds(0.5, 2, 5)
    assert (inv.quality.window_s, inv.quality.min_checks) == (900, 10)
    assert (inv.quality.min_jitter_pairs, inv.quality.max_gap_fraction) == (3, 0.5)


def test_an_empty_block_is_the_defaults_too():
    assert parse({}) == QualityConfig()


def test_thresholds_and_limits_can_be_overridden_individually():
    cfg = parse({
        "latency_ms": {"excellent": 10, "good": 20, "fair": 40},
        "window_s": 3600, "min_checks": 20, "min_jitter_pairs": 5, "max_gap_fraction": 0.25,
    })
    assert cfg.latency_ms == QualityThresholds(10, 20, 40)
    assert cfg.jitter_ms == QualityConfig().jitter_ms  # untouched figures keep their defaults
    assert (cfg.window_s, cfg.min_checks, cfg.min_jitter_pairs) == (3600, 20, 5)
    assert cfg.max_gap_fraction == 0.25


@pytest.mark.parametrize("quality, message", [
    ("excellent", "must be a mapping"),
    ({"latencyms": {}}, "unknown key"),
    ({"latency_ms": 50}, "mapping with excellent, good and fair"),
    ({"latency_ms": {"excellent": 1, "good": 2}}, "missing fair"),
    ({"latency_ms": {"excellent": 1, "good": 2, "fair": 3, "poor": 4}}, "unknown key"),
    ({"latency_ms": {"excellent": 20, "good": 10, "fair": 30}}, "excellent < good < fair"),
    ({"jitter_ms": {"excellent": 5, "good": 5, "fair": 9}}, "excellent < good < fair"),
    ({"latency_ms": {"excellent": -1, "good": 5, "fair": 9}}, "latency_ms.excellent"),
    ({"loss_pct": {"excellent": 1, "good": 50, "fair": 150}}, "loss_pct.fair"),
    ({"jitter_ms": {"excellent": "a", "good": 5, "fair": 9}}, "must be a number"),
    ({"latency_ms": {"excellent": True, "good": 5, "fair": 9}}, "must be a number"),
    ({"latency_ms": {"excellent": float("nan"), "good": 5, "fair": 9}}, "finite"),
    ({"window_s": 10}, "window_s"),
    ({"window_s": 10**9}, "window_s"),
    ({"window_s": 900.5}, "whole number"),
    ({"min_checks": 1}, "min_checks"),
    ({"min_checks": "many"}, "must be a number"),
    ({"min_jitter_pairs": 0}, "min_jitter_pairs"),
    ({"max_gap_fraction": 0}, "max_gap_fraction"),
    ({"max_gap_fraction": 1.5}, "max_gap_fraction"),
])
def test_bad_quality_settings_are_rejected_with_a_clear_message(quality, message):
    with pytest.raises(InventoryError, match=message):
        parse(quality)


def test_the_quality_block_is_also_valid_in_real_mode():
    raw = {"mode": "real", "poll_interval_s": 10, "quality": {"min_checks": 5},
           "devices": [{"name": "lo", "address": "127.0.0.1", "authorized": True}]}
    assert parse_inventory(raw).quality.min_checks == 5


# ------------------------------------------------------------------ shipped examples
def test_the_shipped_demo_inventory_spells_out_the_defaults():
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    assert inv.quality == QualityConfig()  # the example documents the built-in defaults


def test_the_demo_inventory_has_a_device_that_is_graded_by_jitter():
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    voice = next(d for d in inv.devices if d.name == "voice-gateway")
    assert voice.sim.loss == 0 and voice.sim.rtt_ms < inv.quality.latency_ms.excellent
    assert voice.sim.jitter_ms > inv.quality.jitter_ms.good


def test_the_real_example_still_loads_with_its_commented_quality_block():
    inv = load_inventory(REPO_ROOT / "config" / "devices.real.example.yaml")
    assert inv.mode == "real" and inv.quality == QualityConfig()
