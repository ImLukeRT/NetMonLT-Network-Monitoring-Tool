"""The Wi-Fi endpoints: disabled, pending, current state, failures, warnings, history."""

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock
from netmonlt.api import create_app
from netmonlt.domain import WifiConfig
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber
from netmonlt.wifi import STATE_CONNECTED, STATE_DISCONNECTED, WifiReading

NOW = 10_000.0
INTERVAL = 5.0


def link(signal=75, ts=NOW, source="wlanapi", **kw):
    fields = dict(state=STATE_CONNECTED, ssid="H*******", interface="Wi-Fi", signal_pct=signal,
                  rx_mbps=300.0, tx_mbps=200.0, radio_type="802.11ac", channel=36,
                  source=source)
    fields.update(kw)
    return ts, WifiReading(**fields)


def drop(ts):
    return ts, WifiReading(STATE_DISCONNECTED, interface="Wi-Fi", source="wlanapi")


def client_for(db, devices, *, now=NOW, enabled=True, mode="simulated", **cfg):
    wifi = WifiConfig(enabled=enabled, poll_interval_s=INTERVAL, **cfg)
    inventory = Inventory(mode=mode, poll_interval_s=INTERVAL, devices=tuple(devices), wifi=wifi)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=FakeClock(now))
    return TestClient(app)


def store(db, *items):
    for ts, reading in items:
        db.save_wifi_observation(ts, reading)


# ------------------------------------------------------------------ disabled and pending
def test_when_disabled_the_api_says_so_and_exposes_nothing(db, devices):
    db.sync_devices(devices)
    store(db, link())  # even if rows exist (for example from an earlier run)
    with client_for(db, devices, enabled=False) as client:
        assert client.get("/api/wifi").json() == {"enabled": False}
        assert client.get("/api/wifi/history").status_code == 404


def test_before_the_first_poll_it_is_pending_with_no_figures(db, devices):
    db.sync_devices(devices)
    with client_for(db, devices) as client:
        body = client.get("/api/wifi").json()
    assert body["enabled"] is True and body["current"] is None
    assert body["status"]["status"] == "pending" and body["warning"]["low_signal"] is False


# ------------------------------------------------------------------ the current state
def test_current_state_carries_every_figure_with_its_provenance(db, devices):
    store(db, link(72, NOW - 3, rssi_dbm=-60, rssi_source="Windows WLAN API (driver-reported)"))
    with client_for(db, devices, mode="real") as client:
        body = client.get("/api/wifi").json()
    cur = body["current"]
    assert (cur["state"], cur["ssid"], cur["signal_pct"], cur["rssi_dbm"]) == (
        "connected", "H*******", 72, -60)
    assert cur["age_s"] == pytest.approx(3) and cur["rssi_source"].startswith("Windows WLAN API")
    assert set(cur["fields"].values()) == {"reported"}
    assert body["simulated"] is False and body["source"] == "wlanapi"
    assert body["source_label"] == "Windows WLAN API"


def test_a_missing_rssi_is_unavailable_and_is_not_derived_from_the_percentage(db, devices):
    store(db, link(80, NOW - 1))
    with client_for(db, devices, mode="real") as client:
        cur = client.get("/api/wifi").json()["current"]
    assert cur["signal_pct"] == 80
    assert cur["rssi_dbm"] is None and cur["rssi_source"] is None
    assert cur["fields"]["rssi_dbm"] == "unavailable"
    assert cur["fields"]["signal_pct"] == "reported"


def test_simulated_data_is_labelled_simulated_field_by_field(db, devices):
    store(db, link(70, NOW - 1, source="simulated", rssi_dbm=-65, rssi_source="simulated"))
    with client_for(db, devices) as client:
        body = client.get("/api/wifi").json()
    assert body["simulated"] is True and body["source"] == "simulated"
    assert set(body["current"]["fields"].values()) == {"simulated"}


def test_a_disconnected_adapter_has_a_state_and_every_figure_unavailable(db, devices):
    store(db, link(70, NOW - 20), drop(NOW - 1))
    with client_for(db, devices) as client:
        cur = client.get("/api/wifi").json()["current"]
    assert cur["state"] == "disconnected" and cur["interface"] == "Wi-Fi"
    assert cur["signal_pct"] is None and cur["rx_mbps"] is None and cur["ssid"] is None
    assert cur["fields"]["signal_pct"] == "unavailable"


def test_the_ssid_privacy_setting_is_applied_again_when_the_response_is_made(db, devices):
    store(db, link(70, NOW - 1, ssid="HomeWiFi"))  # stored in full by an earlier configuration
    with client_for(db, devices, ssid_privacy="masked") as client:
        assert client.get("/api/wifi").json()["current"]["ssid"] == "H*******"
    with client_for(db, devices, ssid_privacy="hidden") as client:
        assert client.get("/api/wifi").json()["current"]["ssid"] is None
    with client_for(db, devices, ssid_privacy="full") as client:
        assert client.get("/api/wifi").json()["current"]["ssid"] == "HomeWiFi"


def test_nothing_identifying_beyond_the_chosen_ssid_is_in_the_response(db, devices):
    store(db, link(70, NOW - 1))
    with client_for(db, devices) as client:
        text = client.get("/api/wifi").text.lower()
        history = client.get("/api/wifi/history").text.lower()
    for word in ("bssid", "mac", "guid", "profile", "password", "key"):
        assert word not in text and word not in history


# ------------------------------------------------------------------ failures and staleness
@pytest.mark.parametrize("category", ["no_adapter", "unsupported_os", "unavailable", "timeout",
                                      "parse_error", "error"])
def test_every_failure_category_is_reported_with_a_safe_message(db, devices, category):
    db.save_wifi_failure(NOW - 2, category, "fixed message")
    with client_for(db, devices) as client:
        body = client.get("/api/wifi").json()
    assert body["status"]["status"] == category and body["status"]["message"] == "fixed message"
    assert body["current"] is None  # an Ethernet-only machine has nothing to show
    assert body["status"]["last_ok_age_s"] is None and body["status"]["stale"] is False


def test_after_a_failure_the_last_good_observation_is_still_shown_and_marked_stale(db, devices):
    store(db, link(70, NOW - 200))
    db.save_wifi_failure(NOW - 1, "no_adapter", "gone")
    with client_for(db, devices) as client:
        body = client.get("/api/wifi").json()
    assert body["current"]["signal_pct"] == 70 and body["current"]["age_s"] == pytest.approx(200)
    assert body["status"]["status"] == "no_adapter" and body["status"]["stale"] is True
    assert body["status"]["last_ok_age_s"] == pytest.approx(200)


def test_a_fresh_ok_status_is_not_stale(db, devices):
    store(db, link(70, NOW - 4))
    with client_for(db, devices) as client:
        body = client.get("/api/wifi").json()
    assert body["status"]["status"] == "ok" and body["status"]["stale"] is False
    assert body["stale_after_s"] == 3 * INTERVAL + 20


# ------------------------------------------------------------------ the low-signal warning
def test_the_warning_appears_after_enough_low_readings_and_is_configurable(db, devices):
    store(db, link(80, NOW - 40), link(30, NOW - 15), link(25, NOW - 10), link(20, NOW - 5))
    with client_for(db, devices, low_signal_pct=40, warn_after_polls=3) as client:
        w = client.get("/api/wifi").json()["warning"]
    assert w["low_signal"] is True and w["since"] == NOW - 15 and w["consecutive_low"] == 3
    with client_for(db, devices, low_signal_pct=40, warn_after_polls=4) as client:
        assert client.get("/api/wifi").json()["warning"]["low_signal"] is False
    with client_for(db, devices, low_signal_pct=10, warn_after_polls=1) as client:
        assert client.get("/api/wifi").json()["warning"]["low_signal"] is False


def test_the_warning_clears_on_recovery_and_on_disconnection(db, devices):
    store(db, link(20, NOW - 20), link(20, NOW - 15), link(20, NOW - 10), link(70, NOW - 5))
    with client_for(db, devices) as client:
        assert client.get("/api/wifi").json()["warning"]["low_signal"] is False
    store(db, link(20, NOW - 4), link(20, NOW - 3), link(20, NOW - 2), drop(NOW - 1))
    with client_for(db, devices) as client:
        assert client.get("/api/wifi").json()["warning"]["low_signal"] is False


def test_no_warning_from_stale_observations(db, devices):
    store(db, link(10, NOW - 300), link(10, NOW - 295), link(10, NOW - 290))
    with client_for(db, devices) as client:
        assert client.get("/api/wifi").json()["warning"]["low_signal"] is False


# ------------------------------------------------------------------ history
def fill(db, signals, end=NOW, step=INTERVAL):
    for i, signal in enumerate(signals):
        ts = end - (len(signals) - 1 - i) * step
        store(db, drop(ts) if signal is None else link(signal, ts))


def test_history_reports_buckets_with_signal_and_the_limit(db, devices):
    fill(db, [80, 70, 60, 50, 40, 30], end=NOW)
    with client_for(db, devices, low_signal_pct=45) as client:
        body = client.get("/api/wifi/history?window_s=900").json()
    assert body["observations"] == 6 and body["low_signal_pct"] == 45
    assert body["window_s"] == 900 and body["end"] == NOW and body["bucket_s"] == 3 * INTERVAL
    means = [p["signal_mean"] for p in body["points"]]
    assert means == [70.0, 40.0]  # buckets of three polls each
    assert body["gaps"] == []


def test_a_disconnection_is_counted_and_never_averaged_in_as_zero(db, devices):
    fill(db, [80, 80, 80, None, None, None, 60, 60, 60])  # the middle bucket is all disconnected
    with client_for(db, devices) as client:
        points = client.get("/api/wifi/history").json()["points"]
    assert sum(p["not_connected"] for p in points) == 3
    assert all(p["signal_mean"] != 0 for p in points)
    off_only = [p for p in points if p["connected"] == 0]
    assert off_only and all(p["signal_mean"] is None for p in off_only)


def test_monitoring_gaps_are_gaps_not_disconnections(db, devices):
    fill(db, [80] * 8, end=NOW - 500)
    fill(db, [75] * 8, end=NOW)
    with client_for(db, devices) as client:
        body = client.get("/api/wifi/history").json()
    assert len(body["gaps"]) == 1 and body["gaps"][0]["end"] - body["gaps"][0]["start"] > 400
    assert all(p["not_connected"] == 0 for p in body["points"])  # nothing invented in the hole


def test_history_with_no_observations_is_empty_not_an_error(db, devices):
    with client_for(db, devices) as client:
        body = client.get("/api/wifi/history").json()
    assert body["points"] == [] and body["first_ts"] is None and body["observations"] == 0


def test_history_window_and_point_limits_are_validated(db, devices):
    with client_for(db, devices) as client:
        for bad in ("window_s=10", "window_s=99999999", "max_points=1", "max_points=9999"):
            assert client.get(f"/api/wifi/history?{bad}").status_code == 422


def test_history_only_covers_the_requested_window(db, devices):
    fill(db, [60] * 4, end=NOW - 3000)
    fill(db, [90] * 4, end=NOW)
    with client_for(db, devices) as client:
        short = client.get("/api/wifi/history?window_s=900").json()
        long = client.get("/api/wifi/history?window_s=21600").json()
    assert short["observations"] == 4 and long["observations"] == 8


def test_the_wifi_endpoints_are_read_only(db, devices):
    with client_for(db, devices) as client:
        for path in ("/api/wifi", "/api/wifi/history"):
            assert client.post(path).status_code == 405
            assert client.delete(path).status_code == 405


def test_the_dashboard_page_still_serves_and_names_the_panel(db, devices):
    with client_for(db, devices) as client:
        page = client.get("/")
    assert page.status_code == 200 and "Wi-Fi signal (this computer)" in page.text
