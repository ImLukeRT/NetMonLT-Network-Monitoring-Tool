"""Storage query and endpoints for network quality, plus the simulated demo's grades."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from helpers import REPO_ROOT, FakeClock
from netmonlt.api import create_app
from netmonlt.collector import poll_once
from netmonlt.domain import CheckResult, PingResult, QualityConfig, QualityThresholds, Status
from netmonlt.inventory import Inventory, load_inventory
from netmonlt.probes import SimulatedProber
from netmonlt.quality import CheckSample

INTERVAL = 5.0
NOW = 10_000.0


def add(db, ts, rtt=10.0, received=3, device="alpha"):
    """Store one check: three pings, `received` answered at `rtt` ms (none if received == 0)."""
    if received:
        ping = PingResult(3, received, rtt, rtt, rtt)
        status = Status.UP if received == 3 else Status.DEGRADED
    else:
        ping, status = PingResult(3, 0, None, None, None), Status.DOWN
    db.record_check(CheckResult(device, ts, status, ping))


def fill(db, n, end=NOW, device="alpha", jitter=1.0):
    """n checks ending at `end`, one every 5 s, with RTTs alternating by `jitter` ms."""
    for i in range(n):
        add(db, end - (n - 1 - i) * INTERVAL, 10.0 + (jitter if i % 2 else 0.0), device=device)


@pytest.fixture
def inventory(devices):
    return Inventory(mode="simulated", poll_interval_s=INTERVAL, devices=tuple(devices))


def client_for(db, inventory, now=NOW, cfg=None):
    if cfg is not None:
        inventory = Inventory(mode="simulated", poll_interval_s=INTERVAL,
                              devices=inventory.devices, quality=cfg)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False,
                     clock=FakeClock(now))
    return TestClient(app)


# ------------------------------------------------------------------ storage
def test_quality_samples_are_the_stored_checks_in_time_order(db, devices):
    db.sync_devices(devices)
    add(db, 30, 7.0)
    add(db, 10, 5.0)
    add(db, 20, received=0)
    add(db, 20, 9.0, device="beta")
    samples, first, last = db.quality_samples("alpha", 0, 100)
    assert samples == [CheckSample(10, 3, 3, 5.0), CheckSample(20, 3, 0, None),
                       CheckSample(30, 3, 3, 7.0)]
    assert (first, last) == (10, 30)


def test_quality_samples_respect_the_window_but_report_the_whole_history(db, devices):
    db.sync_devices(devices)
    for ts in (10, 50, 90):
        add(db, ts)
    samples, first, last = db.quality_samples("alpha", 40, 60)
    assert [s.ts for s in samples] == [50] and (first, last) == (10, 90)


def test_quality_samples_for_an_unknown_device_is_none_and_an_empty_one_has_no_history(db, devices):
    db.sync_devices(devices)
    assert db.quality_samples("ghost", 0, 100) is None
    assert db.quality_samples("alpha", 0, 100) == ([], None, None)


def test_asking_for_quality_stores_nothing(db, devices):
    db.sync_devices(devices)
    fill(db, 12)
    before = db._conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.quality_samples("alpha", 0, NOW)
    assert db._conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == before
    assert not {t for t in tables if "quality" in t or "jitter" in t}  # no duplicated telemetry


# ------------------------------------------------------------------ device endpoint
def test_device_endpoint_reports_figures_grade_and_the_definitions(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 20)
    with client_for(db, inventory) as client:
        body = client.get("/api/devices/alpha/quality").json()
    s = body["summary"]
    assert s["level"] == "excellent" and s["jitter_ms"] == 1.0 and s["loss_pct"] == 0.0
    assert s["latency_ms"] == 10.5 and s["checks"] == 20 and s["jitter_pairs"] == 19
    assert body["window_s"] == 900 and body["start"] == NOW - 900 and body["end"] == NOW
    assert body["thresholds"]["jitter_ms"] == {"excellent": 5.0, "good": 15.0, "fair": 30.0}
    assert body["definitions"]["jitter"].startswith("Mean absolute difference")
    assert "RFC 3550" in body["definitions"]["jitter"]
    assert "does not verify or enforce QoS" in body["scope"] and "heuristics" in body["scope"]
    assert body["points"] and body["bucket_s"] == 30.0 and body["gaps"] == []


def test_the_response_never_contains_a_made_up_number_for_missing_data(db, devices, inventory):
    db.sync_devices(devices)
    for i in range(12):
        add(db, NOW - 60 + i * INTERVAL, received=0)  # a minute of nothing but down checks
    with client_for(db, inventory) as client:
        s = client.get("/api/devices/alpha/quality").json()["summary"]
    assert s["latency_ms"] is None and s["jitter_ms"] is None  # not 0
    assert s["loss_pct"] == 100.0 and s["level"] == "poor"
    assert "no replies" in s["reason"]


def test_unknown_device_is_404(db, devices, inventory):
    db.sync_devices(devices)
    with client_for(db, inventory) as client:
        assert client.get("/api/devices/ghost/quality").status_code == 404


def test_a_device_with_no_checks_yet_is_unknown_not_an_error(db, devices, inventory):
    db.sync_devices(devices)
    with client_for(db, inventory) as client:
        body = client.get("/api/devices/alpha/quality").json()
    assert body["summary"]["level"] == "unknown"
    assert body["summary"]["reason"] == "no measurements yet"
    assert body["points"] == [] and body["gaps"] == [] and body["first_check_ts"] is None


def test_too_few_checks_are_unknown_with_the_reason_but_figures_still_shown(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 4)
    with client_for(db, inventory) as client:
        s = client.get("/api/devices/alpha/quality").json()["summary"]
    assert s["level"] == "unknown" and "insufficient samples" in s["reason"]
    assert s["latency_ms"] == 10.5 and s["checks"] == 4


def test_a_stale_device_is_unknown_and_not_graded_from_old_data(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 30, end=NOW - 600)  # the monitor stopped ten minutes ago
    with client_for(db, inventory) as client:
        s = client.get("/api/devices/alpha/quality").json()["summary"]
    assert s["stale"] is True and s["level"] == "unknown" and "stale" in s["reason"]
    assert s["last_check_age_s"] == pytest.approx(600)


def test_downtime_inside_the_window_is_loss_with_a_gap_free_chart(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 20, end=NOW - 30)
    for i in range(6):
        add(db, NOW - 25 + i * INTERVAL, received=0)  # down for the last 25 s
    with client_for(db, inventory) as client:
        body = client.get("/api/devices/alpha/quality").json()
    s = body["summary"]
    assert s["loss_pct"] == pytest.approx(100 * 6 / 26, abs=0.01)
    assert s["latency_ms"] == 10.5  # the outage did not drag latency towards zero
    assert body["gaps"] == []  # down checks are measurements, not gaps
    assert max(p["loss_pct"] for p in body["points"]) == 100.0


def test_a_monitoring_gap_is_a_gap_and_is_not_counted_as_loss(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 12, end=NOW - 400)  # then nothing for 400 s, then healthy again
    fill(db, 40, end=NOW)
    with client_for(db, inventory) as client:
        body = client.get("/api/devices/alpha/quality?window_s=900").json()
    s = body["summary"]
    assert s["loss_pct"] == 0.0 and s["gap_count"] == 1 and len(body["gaps"]) == 1
    gap = body["gaps"][0]
    assert gap["end"] - gap["start"] > 100
    # no bucket is invented inside the hole
    assert all(not (gap["start"] + 35 < p["start"] and p["end"] < gap["end"] - 35)
               for p in body["points"])


def test_sparse_history_is_unknown(db, devices, inventory):
    db.sync_devices(devices)
    for burst_end in (NOW - 800, NOW):
        fill(db, 6, end=burst_end)  # two bursts, most of the window unmeasured
    with client_for(db, inventory) as client:
        s = client.get("/api/devices/alpha/quality").json()["summary"]
    assert s["level"] == "unknown" and "sparse history" in s["reason"]


def test_history_shorter_than_the_window_is_graded_normally(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 15)  # only 70 s of a 15-minute window exist
    with client_for(db, inventory) as client:
        body = client.get("/api/devices/alpha/quality?window_s=900").json()
    assert body["summary"]["level"] == "excellent" and body["gaps"] == []


def test_thresholds_from_the_inventory_change_the_grade(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 20, jitter=6.0)  # 6 ms jitter: Good with the defaults
    strict = QualityConfig(jitter_ms=QualityThresholds(1, 2, 3))
    with client_for(db, inventory) as client:
        assert client.get("/api/devices/alpha/quality").json()["summary"]["level"] == "good"
    with client_for(db, inventory, cfg=strict) as client:
        body = client.get("/api/devices/alpha/quality").json()
    assert body["summary"]["level"] == "poor" and body["thresholds"]["jitter_ms"]["fair"] == 3.0


def test_window_and_point_limits_are_validated(db, devices, inventory):
    db.sync_devices(devices)
    with client_for(db, inventory) as client:
        for bad in ("window_s=10", "window_s=99999999", "max_points=1", "max_points=9999"):
            assert client.get(f"/api/devices/alpha/quality?{bad}").status_code == 422


def test_the_default_window_comes_from_the_inventory(db, devices, inventory):
    db.sync_devices(devices)
    with client_for(db, inventory, cfg=QualityConfig(window_s=3600)) as client:
        assert client.get("/api/devices/alpha/quality").json()["window_s"] == 3600
        assert client.get("/api/quality").json()["window_s"] == 3600
        assert client.get("/api/quality?window_s=600").json()["window_s"] == 600


def test_the_quality_endpoints_are_read_only(db, devices, inventory):
    db.sync_devices(devices)
    with client_for(db, inventory) as client:
        for path in ("/api/quality", "/api/devices/alpha/quality"):
            assert client.post(path).status_code == 405
            assert client.delete(path).status_code == 405


# ------------------------------------------------------------------ overview endpoint
def test_overview_lists_every_device_in_inventory_order(db, devices, inventory):
    db.sync_devices(devices)
    fill(db, 20, device="alpha")
    fill(db, 4, device="beta")  # too few checks
    with client_for(db, inventory) as client:
        body = client.get("/api/quality").json()
    assert [d["device"] for d in body["devices"]] == ["alpha", "beta"]
    alpha, beta = body["devices"]
    assert alpha["level"] == "excellent" and beta["level"] == "unknown"
    assert body["simulated"] is True and body["mode"] == "simulated"
    assert "points" not in alpha  # the overview is only summaries
    assert "latency" in body["definitions"] and "does not verify" in body["scope"]


# ------------------------------------------------------------------ the simulated demo
def demo_levels():
    """Run the shipped demo inventory for 15 simulated minutes and grade each device."""
    inv = load_inventory(REPO_ROOT / "config" / "devices.sim.yaml")
    from netmonlt.storage import Database

    db = Database(":memory:")
    db.sync_devices(inv.devices)
    sim_time = [0.0]
    prober = SimulatedProber(seed=0, clock=lambda: sim_time[0])
    clock = FakeClock(1000.0)

    async def go():
        for i in range(180):
            sim_time[0] = i * 5.0
            clock.now = 1000.0 + i * 5.0
            await poll_once(inv.devices, prober, db, clock=clock)

    asyncio.run(go())
    app = create_app(inv, db, prober, start_collector=False, clock=FakeClock(1000.0 + 179 * 5 + 2))
    with TestClient(app) as client:
        result = {d["device"]: d for d in client.get("/api/quality").json()["devices"]}
    db.close()
    return result


def test_the_simulated_demo_shows_every_kind_of_grade_deterministically():
    first, second = demo_levels(), demo_levels()
    assert first == second  # seeded simulation: identical on every run
    levels = {name: d["level"] for name, d in first.items()}
    assert levels == {
        "core-router": "excellent", "edge-firewall": "excellent", "file-server": "excellent",
        "access-switch": "fair",  # its scripted 30 s outage is about 3% loss
        "wifi-ap": "poor",  # 40% simulated loss
        "branch-vpn": "poor",  # 170 ms latency
        "voice-gateway": "fair",  # low latency and no loss, but unsteady: graded on jitter
    }
    assert first["voice-gateway"]["limiting"] == ["jitter"]
    assert first["branch-vpn"]["limiting"] == ["latency"]
    assert first["wifi-ap"]["limiting"] == ["loss"]
    assert first["access-switch"]["limiting"] == ["loss"]
    assert first["core-router"]["jitter_ms"] < 1.0 and first["voice-gateway"]["jitter_ms"] > 15.0
