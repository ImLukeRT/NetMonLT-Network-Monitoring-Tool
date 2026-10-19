import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock, HangingProber
from netmonlt.api import create_app
from netmonlt.collector import poll_once
from netmonlt.domain import CheckResult, PingResult, Status
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber


@pytest.fixture
def inventory(devices):
    return Inventory(mode="simulated", poll_interval_s=0.05, devices=tuple(devices))


def test_devices_endpoint_before_and_after_polling(db, devices, inventory):
    db.sync_devices(devices)
    prober = SimulatedProber()
    with TestClient(create_app(inventory, db, prober, start_collector=False)) as client:
        body = client.get("/api/devices").json()
        assert body["mode"] == "simulated"
        assert [d["status"] for d in body["devices"]] == [None, None]

        asyncio.run(poll_once(devices, prober, db))
        body = client.get("/api/devices").json()
        assert [d["status"] for d in body["devices"]] == ["up", "up"]
        assert body["devices"][0]["name"] == "alpha"


def test_history_endpoint_and_404(db, devices, inventory):
    db.sync_devices(devices)
    prober = SimulatedProber()
    for _ in range(3):
        asyncio.run(poll_once(devices, prober, db))
    with TestClient(create_app(inventory, db, prober, start_collector=False)) as client:
        assert len(client.get("/api/devices/alpha/history?limit=2").json()["checks"]) == 2
        assert client.get("/api/devices/nope/history").status_code == 404
        assert client.get("/api/devices/alpha/history?limit=0").status_code == 422


def test_dashboard_page_and_health(db, devices, inventory):
    with TestClient(create_app(inventory, db, SimulatedProber(), start_collector=False)) as client:
        page = client.get("/")
        assert page.status_code == 200 and "SIMULATED DATA" in page.text
        assert client.get("/healthz").json() == {"ok": True}


def test_background_collector_populates_database(db, devices, inventory):
    db.sync_devices(devices)
    app = create_app(inventory, db, SimulatedProber(), start_collector=True)
    with TestClient(app) as client:
        deadline = time.time() + 5
        while time.time() < deadline:
            rows = client.get("/api/devices").json()["devices"]
            if all(r["status"] for r in rows):
                break
            time.sleep(0.05)
        assert all(r["status"] == "up" for r in rows)


def test_stale_devices_are_flagged_using_the_server_clock(db, devices, inventory):
    db.sync_devices(devices)
    db.record_check(CheckResult("alpha", 1000.0, Status.UP, PingResult(3, 3, 5.0, 5.0, 5.0)))
    clock = FakeClock(1000.0)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=clock)
    with TestClient(app) as client:

        def snapshot():
            body = client.get("/api/devices").json()
            return body, {d["name"]: d for d in body["devices"]}

        body, devs = snapshot()
        stale_after = body["stale_after_s"]
        assert stale_after >= 3 * inventory.poll_interval_s
        assert body["server_time"] == 1000.0
        assert devs["alpha"]["stale"] is False and devs["alpha"]["age_s"] == 0

        clock.now = 1000.0 + stale_after - 1
        assert snapshot()[1]["alpha"]["stale"] is False

        clock.now = 1000.0 + stale_after + 1
        _, devs = snapshot()
        assert devs["alpha"]["stale"] is True
        assert devs["alpha"]["status"] == "up"  # raw status is kept; the flag says don't trust it
        assert devs["alpha"]["age_s"] == pytest.approx(stale_after + 1)

        # A device that was never checked is "unknown", not "stale".
        assert devs["beta"]["stale"] is False and devs["beta"]["age_s"] is None


def test_negative_age_from_a_clock_step_is_clamped(db, devices, inventory):
    db.sync_devices(devices)
    db.record_check(CheckResult("alpha", 5000.0, Status.UP, PingResult(3, 3, 5.0, 5.0, 5.0)))
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=FakeClock(10.0))
    with TestClient(app) as client:
        alpha = client.get("/api/devices").json()["devices"][0]
        assert alpha["age_s"] == 0 and alpha["stale"] is False


def test_collector_stops_when_the_app_shuts_down(db, devices, inventory):
    db.sync_devices(devices)
    with TestClient(create_app(inventory, db, SimulatedProber())) as client:
        deadline = time.time() + 5
        while not db.history("alpha") and time.time() < deadline:
            time.sleep(0.02)
        assert client.get("/healthz").status_code == 200
    count = len(db.history("alpha", 1000))
    time.sleep(0.3)  # several poll intervals
    assert len(db.history("alpha", 1000)) == count


def test_shutdown_is_prompt_even_if_a_probe_is_hung(db, devices, inventory):
    app = create_app(inventory, db, HangingProber())

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run(), 5))


def test_dashboard_labels_real_and_simulated_data_differently(db, devices):
    def page(mode):
        inv = Inventory(mode=mode, poll_interval_s=5, devices=tuple(devices))
        with TestClient(create_app(inv, db, SimulatedProber(), start_collector=False)) as client:
            return client.get("/").text

    assert "SIMULATED DATA" in page("simulated") and "LIVE" not in page("simulated")
    assert "LIVE · ICMP" in page("real") and "SIMULATED DATA" not in page("real")


# ------------------------------------------------------------------ alerts endpoint
def make_alert(ts, severity="critical", old=Status.UP, new=Status.DOWN, device="alpha"):
    from netmonlt.alerts import Alert, Severity

    return Alert(device, ts, Severity(severity), old, new, f"{device} went {new.value} at {ts}")


def test_alerts_endpoint_is_empty_before_anything_happens(db, devices, inventory):
    db.sync_devices(devices)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=FakeClock(5.0))
    with TestClient(app) as client:
        assert client.get("/api/alerts").json() == {"server_time": 5.0, "alerts": []}


def test_alerts_endpoint_returns_newest_first_with_all_fields(db, devices, inventory):
    from netmonlt.alerts import AlertState

    db.sync_devices(devices)
    db.save_alert_state("alpha", AlertState(updated=100.0), make_alert(100.0))
    db.save_alert_state(
        "beta", AlertState(updated=200.0), make_alert(200.0, "info", Status.DOWN, Status.UP, "beta")
    )
    clock = FakeClock(260.0)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False, clock=clock)
    with TestClient(app) as client:
        body = client.get("/api/alerts").json()
    assert body["server_time"] == 260.0
    first, second = body["alerts"]
    assert first == {
        "id": 2, "device": "beta", "ts": 200.0, "severity": "info", "from_status": "down",
        "to_status": "up", "transition": "down -> up", "message": "beta went up at 200.0",
        "age_s": 60.0,
    }
    assert (second["device"], second["severity"], second["transition"]) == (
        "alpha", "critical", "up -> down"
    )
    assert second["age_s"] == 160.0


def test_alerts_limit_and_validation(db, devices, inventory):
    from netmonlt.alerts import AlertState

    db.sync_devices(devices)
    for ts in (1.0, 2.0, 3.0):
        db.save_alert_state("alpha", AlertState(updated=ts), make_alert(ts))
    app = create_app(inventory, db, SimulatedProber(), start_collector=False)
    with TestClient(app) as client:
        assert [a["ts"] for a in client.get("/api/alerts?limit=2").json()["alerts"]] == [3.0, 2.0]
        assert client.get("/api/alerts?limit=0").status_code == 422
        assert client.get("/api/alerts?limit=501").status_code == 422


def test_alerts_endpoint_is_read_only(db, devices, inventory):
    app = create_app(inventory, db, SimulatedProber(), start_collector=False)
    with TestClient(app) as client:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/alerts").status_code == 405


def test_alerts_appear_through_the_api_after_polling(db, devices, inventory):
    from helpers import ScriptedProber, run_cycles

    db.sync_devices(devices)
    script = {"alpha": [Status.UP] * 3 + [Status.DOWN], "beta": [Status.UP]}
    run_cycles(devices, ScriptedProber(script), db, 12)
    with TestClient(create_app(inventory, db, SimulatedProber(), start_collector=False)) as client:
        alerts = client.get("/api/alerts").json()["alerts"]
    assert [(a["device"], a["transition"]) for a in alerts] == [("alpha", "up -> down")]


def test_dashboard_page_has_an_alerts_section(db, devices, inventory):
    with TestClient(create_app(inventory, db, SimulatedProber(), start_collector=False)) as client:
        page = client.get("/").text
    assert "Recent alerts" in page and "/api/alerts" in page
