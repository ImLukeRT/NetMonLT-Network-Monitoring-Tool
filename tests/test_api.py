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
