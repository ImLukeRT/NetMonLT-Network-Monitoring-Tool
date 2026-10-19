"""Backend tests for the latency-history query and endpoint behind the chart."""

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock, ScriptedProber, run_cycles
from netmonlt.api import create_app
from netmonlt.collector import stale_after_s
from netmonlt.domain import CheckResult, PingResult, Status
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber

GAP = 25.0
RECEIVED = {Status.UP: 3, Status.DEGRADED: 2, Status.DOWN: 0}


def add(db, ts, status=Status.UP, rtt=5.0, device="alpha", ping=None):
    """Store one check. Down checks have no latency, like real ones."""
    if ping is None:
        received = RECEIVED[status]
        ping = PingResult(3, received, *([rtt] * 3 if received else [None] * 3))
    db.record_check(CheckResult(device, ts, status, ping))


def series(db, start=0.0, end=100.0, max_points=300, device="alpha"):
    return db.latency_series(device, start, end, max_points, GAP)


# ------------------------------------------------------------------ the three cases to tell apart
def test_down_check_is_a_point_without_latency(db, devices):
    db.sync_devices(devices)
    add(db, 10, Status.UP, 4.0)
    add(db, 15, Status.DOWN)
    up, down = series(db)["points"]
    assert up["rtt_avg_ms"] == 4.0 and up["status"] == "up"
    assert down["status"] == "down" and down["down"] == 1 and down["received"] == 0
    assert down["rtt_min_ms"] is None and down["rtt_avg_ms"] is None and down["rtt_max_ms"] is None


def test_a_genuine_zero_latency_is_zero_not_missing(db, devices):
    db.sync_devices(devices)
    add(db, 10, ping=PingResult(3, 3, 0.0, 0.0, 0.0))
    (point,) = series(db)["points"]
    assert point["rtt_avg_ms"] == 0.0 and point["rtt_avg_ms"] is not None
    assert point["rtt_min_ms"] == 0.0 and point["rtt_max_ms"] == 0.0


def test_missing_measurements_are_gaps_not_points_or_zeros(db, devices):
    db.sync_devices(devices)
    for ts in (10, 15, 20, 70, 75):  # nothing between 20 and 70, e.g. probe errors or app stopped
        add(db, ts)
    result = series(db)
    assert [p["ts"] for p in result["points"]] == [10, 15, 20, 70, 75]  # no invented points
    assert {"start": 20, "end": 70} in result["gaps"]


def test_down_checks_are_not_gaps(db, devices):
    db.sync_devices(devices)
    for ts in range(10, 100, 5):
        add(db, ts, Status.DOWN)
    result = series(db)
    assert result["gaps"] == [{"start": 95, "end": 100}] or result["gaps"] == []
    assert all(p["status"] == "down" for p in result["points"])


# ------------------------------------------------------------------ empty and edge cases
def test_unknown_device_is_none(db, devices):
    db.sync_devices(devices)
    assert series(db, device="ghost") is None


def test_device_with_no_history(db, devices):
    db.sync_devices(devices)
    result = series(db)
    assert result["points"] == [] and result["gaps"] == [] and result["checks"] == 0
    assert result["first_check_ts"] is None and result["last_check_ts"] is None


def test_points_are_chronological_and_window_bounds_are_inclusive(db, devices):
    db.sync_devices(devices)
    for ts in (50, 10, 99.9, 100, 100.1, 9.9, 30):  # inserted out of order
        add(db, ts)
    result = series(db, start=10, end=100)
    assert [p["ts"] for p in result["points"]] == [10, 30, 50, 99.9, 100]
    assert result["checks"] == 5
    assert result["first_check_ts"] == 9.9  # all-time, not just this window


def test_checks_of_other_devices_are_excluded(db, devices):
    db.sync_devices(devices)
    add(db, 10, device="alpha")
    add(db, 10, device="beta", rtt=99.0)
    assert [p["rtt_avg_ms"] for p in series(db)["points"]] == [5.0]


# ------------------------------------------------------------------ gap rules
def test_gap_threshold_is_exclusive(db, devices):
    db.sync_devices(devices)
    for ts in (10, 10 + GAP, 10 + GAP + GAP + 0.5):
        add(db, ts)
    # 10 -> 35 is exactly the threshold (not a gap); 35 -> 60.5 is longer (a gap)
    assert series(db, end=70)["gaps"] == [{"start": 10 + GAP, "end": 10 + 2 * GAP + 0.5}]


def test_history_starting_inside_the_window_has_no_leading_gap(db, devices):
    db.sync_devices(devices)
    for ts in (80, 85, 90, 95, 100):  # device only started being monitored at t=80
        add(db, ts)
    assert series(db, start=0, end=100)["gaps"] == []  # "no history yet" is not "missing data"


def test_leading_gap_when_history_existed_before_the_window(db, devices):
    db.sync_devices(devices)
    add(db, 5)  # before the window: proves the device was monitored earlier
    for ts in (60, 65, 70):
        add(db, ts)
    assert {"start": 20, "end": 60} in series(db, start=20, end=100)["gaps"]


def test_trailing_gap_when_measurements_stop(db, devices):
    db.sync_devices(devices)
    for ts in (10, 15, 20):
        add(db, ts)
    assert series(db, start=0, end=100)["gaps"] == [{"start": 20, "end": 100}]


def test_a_window_entirely_after_the_history_is_one_big_gap(db, devices):
    db.sync_devices(devices)
    for ts in (10, 15, 20):
        add(db, ts)
    result = series(db, start=500, end=600)
    assert result["points"] == [] and result["checks"] == 0
    assert result["gaps"] == [{"start": 500, "end": 600}]
    assert result["first_check_ts"] == 10 and result["last_check_ts"] == 20


# ------------------------------------------------------------------ downsampling
def test_small_windows_return_every_check(db, devices):
    db.sync_devices(devices)
    for ts in range(0, 100, 5):
        add(db, ts)
    result = series(db, max_points=20)
    assert result["bucket_s"] is None and len(result["points"]) == 20


def test_large_windows_are_downsampled_to_roughly_max_points(db, devices):
    db.sync_devices(devices)
    for ts in range(0, 1000):  # 1000 checks, one per second
        add(db, ts, rtt=10.0)
    result = series(db, start=0, end=1000, max_points=50)
    assert result["bucket_s"] == 20.0
    assert 45 <= len(result["points"]) <= 51
    assert sum(p["checks"] for p in result["points"]) == 1000  # nothing lost
    assert all(p["rtt_avg_ms"] == 10.0 for p in result["points"])


def test_bucket_latency_is_weighted_by_replies_and_ignores_down_checks(db, devices):
    db.sync_devices(devices)
    add(db, 1, ping=PingResult(3, 3, 10.0, 10.0, 10.0))
    add(db, 1.5, ping=PingResult(3, 1, 50.0, 50.0, 50.0))
    add(db, 2, Status.DOWN)  # must not drag the average towards 0
    add(db, 2.5, ping=PingResult(3, 3, 0.0, 0.0, 0.0))  # a real 0 ms does count
    (point,) = series(db, start=0, end=12, max_points=3)["points"]  # one 4s bucket holds all
    assert point["checks"] == 4 and point["received"] == 7 and point["down"] == 1
    assert point["rtt_avg_ms"] == pytest.approx((3 * 10 + 1 * 50 + 3 * 0) / 7, abs=0.01)
    assert (point["rtt_min_ms"], point["rtt_max_ms"]) == (0.0, 50.0)


def test_bucket_status_rules(db, devices):
    db.sync_devices(devices)
    for ts in (1, 2, 3):
        add(db, ts, Status.DOWN)
    for ts in (11, 12, 13):
        add(db, ts, Status.UP)
    for ts in (21, 22, 23):
        add(db, ts, Status.UP if ts != 22 else Status.DOWN)  # one down among ups
    for ts in (31, 32, 33):
        add(db, ts, Status.UP if ts != 32 else Status.DEGRADED)
    points = series(db, start=0, end=40, max_points=4)["points"]
    assert [p["status"] for p in points] == ["down", "up", "degraded", "degraded"]
    assert points[0]["rtt_avg_ms"] is None  # all-down bucket has no latency, not 0
    assert points[2]["down"] == 1


def test_empty_buckets_are_omitted_not_zero_filled(db, devices):
    db.sync_devices(devices)
    for ts in (1, 2, 3, 91, 92, 93):
        add(db, ts)
    result = series(db, start=0, end=100, max_points=5)  # 20s buckets; the middle is empty
    assert len(result["points"]) == 2
    assert all(p["rtt_avg_ms"] == 5.0 for p in result["points"])
    assert {"start": 3, "end": 91} in result["gaps"]


# ------------------------------------------------------------------ the API endpoint
@pytest.fixture
def inventory(devices):
    return Inventory(mode="simulated", poll_interval_s=5.0, devices=tuple(devices))


def api(db, inventory, now):
    clock = FakeClock(now)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=clock)
    return TestClient(app), clock


def test_endpoint_response_shape_and_defaults(db, devices, inventory):
    db.sync_devices(devices)
    for ts in (9100.0, 9105.0, 9110.0):
        add(db, ts, rtt=3.0)
    client, _ = api(db, inventory, 9112.0)
    with client:
        body = client.get("/api/devices/alpha/latency").json()
    assert body["window_s"] == 900 and body["start"] == 9112.0 - 900 and body["end"] == 9112.0
    assert body["server_time"] == 9112.0 and body["poll_interval_s"] == 5.0
    assert body["resolution_s"] == 5.0 and body["aggregated"] is False
    assert body["gap_threshold_s"] == stale_after_s(5.0)
    assert (body["first_check_ts"], body["last_check_ts"]) == (9100.0, 9110.0)
    assert body["last_check_age_s"] == 2.0 and body["stale"] is False and body["checks"] == 3
    assert [p["ts"] for p in body["points"]] == [9100.0, 9105.0, 9110.0]
    assert body["gaps"] == []


def test_endpoint_flags_stale_devices_and_shows_the_trailing_gap(db, devices, inventory):
    db.sync_devices(devices)
    for ts in (1000.0, 1005.0, 1010.0):
        add(db, ts)
    client, clock = api(db, inventory, 1012.0)
    with client:
        assert client.get("/api/devices/alpha/latency").json()["stale"] is False
        clock.now = 1010.0 + stale_after_s(5.0) + 1
        body = client.get("/api/devices/alpha/latency").json()
    assert body["stale"] is True and body["last_check_age_s"] > stale_after_s(5.0)
    assert body["gaps"] == [{"start": 1010.0, "end": clock.now}]


def test_endpoint_for_a_device_with_no_history(db, devices, inventory):
    db.sync_devices(devices)
    client, _ = api(db, inventory, 500.0)
    with client:
        body = client.get("/api/devices/beta/latency").json()
    assert body["points"] == [] and body["gaps"] == [] and body["checks"] == 0
    assert body["first_check_ts"] is None and body["last_check_age_s"] is None
    assert body["stale"] is False  # never measured is "no history", not "stale"


def test_endpoint_downsamples_and_reports_the_bucket_size(db, devices, inventory):
    db.sync_devices(devices)
    for ts in range(0, 3000, 5):  # 600 checks
        add(db, 1000.0 + ts)
    client, _ = api(db, inventory, 4000.0)
    with client:
        body = client.get("/api/devices/alpha/latency?window_s=3600&max_points=100").json()
    assert body["aggregated"] is True and body["resolution_s"] == 36.0
    assert 80 <= len(body["points"]) <= 101


def test_endpoint_validation(db, devices, inventory):
    db.sync_devices(devices)
    client, _ = api(db, inventory, 1000.0)
    with client:
        assert client.get("/api/devices/nope/latency").status_code == 404
        for query in ("window_s=59", "window_s=604801", "window_s=abc", "max_points=9",
                      "max_points=1001"):
            assert client.get(f"/api/devices/alpha/latency?{query}").status_code == 422, query
        assert client.get("/api/devices/alpha/latency?window_s=60&max_points=10").status_code == 200
        assert client.get("/api/devices/alpha/latency?window_s=604800").status_code == 200


def test_endpoint_is_read_only(db, devices, inventory):
    client, _ = api(db, inventory, 1000.0)
    with client:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/devices/alpha/latency").status_code == 405


def test_the_existing_history_endpoint_is_unchanged(db, devices, inventory):
    db.sync_devices(devices)
    for ts in (1.0, 2.0, 3.0):
        add(db, ts)
    client, _ = api(db, inventory, 10.0)
    with client:
        body = client.get("/api/devices/alpha/history?limit=2").json()
    assert body["device"] == "alpha" and [c["ts"] for c in body["checks"]] == [3.0, 2.0]


def test_end_to_end_outage_probe_errors_and_recovery(db, devices, inventory):
    """Real cycles: up, down, probe errors (no measurement), then up again."""
    db.sync_devices(devices)
    up, down = Status.UP, Status.DOWN
    script = {"alpha": [up] * 4 + [down] * 3 + [None] * 7 + [up] * 3, "beta": [up]}
    run_cycles(devices, ScriptedProber(script), db, 17, start=1000.0)
    client, _ = api(db, inventory, 1000.0 + 16 * 5 + 1)
    with client:
        body = client.get("/api/devices/alpha/latency").json()
    points = body["points"]
    assert [p["status"] for p in points] == ["up"] * 4 + ["down"] * 3 + ["up"] * 3
    assert [p["rtt_avg_ms"] is None for p in points] == [False] * 4 + [True] * 3 + [False] * 3
    # 7 probe errors (1035..1065): the last measurement was 1030, the next 1070. No data, not down.
    assert body["gaps"] == [{"start": 1030.0, "end": 1070.0}]


def test_dashboard_page_contains_the_chart_controls(db, devices, inventory):
    client, _ = api(db, inventory, 1000.0)
    with client:
        page = client.get("/").text
    for needle in ("Latency history", 'id="chart"', 'id="chart-device"', "data-window", "/latency"):
        assert needle in page, needle
