import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from netmonlt.api import create_app
from netmonlt.collector import poll_once
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
