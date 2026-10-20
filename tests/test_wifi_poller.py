"""The Wi-Fi poller: storing results, privacy, failure handling, and isolation from ICMP/SNMP."""

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock, ScriptedProber
from netmonlt.api import create_app
from netmonlt.domain import Device, SimInterface, SimProfile, Status, WifiConfig
from netmonlt.inventory import Inventory
from netmonlt.snmp import SimulatedSnmpClient
from netmonlt.wifi import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    WIFI_MESSAGES,
    SimulatedWifiSource,
    WifiError,
    WifiReading,
)
from netmonlt.wifi_poller import (
    MAX_OBSERVATIONS,
    poll_wifi,
    run_wifi_forever,
    wifi_stale_after_s,
)

SECRET = "SECRET-sentinel-C:/Users/someone/HomeWiFi"
GOOD = WifiReading(STATE_CONNECTED, ssid="HomeWiFi", interface="Wi-Fi", signal_pct=70,
                   source="wlanapi")


class Scripted:
    """A source that returns or raises the next scripted item; "hang" never returns."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    async def read(self):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return item


def poll(db, source, cfg=None, clock=None, **kw):
    cfg = cfg or WifiConfig(enabled=True)
    return asyncio.run(poll_wifi(source, db, cfg, clock=clock or FakeClock(100.0), **kw))


# ------------------------------------------------------------------ storing results
def test_a_successful_poll_is_stored(db):
    assert poll(db, Scripted(GOOD), WifiConfig(ssid_privacy="full")) == "ok"
    ((r, ts),) = db.latest_wifi()
    assert ts == 100.0 and r.signal_pct == 70 and r.ssid == "HomeWiFi"
    assert db.get_wifi_status().status == "ok"


@pytest.mark.parametrize("mode, stored", [("masked", "H*******"), ("full", "HomeWiFi"),
                                          ("hidden", None)])
def test_the_ssid_is_reduced_before_it_reaches_the_database(db, mode, stored):
    poll(db, Scripted(GOOD), WifiConfig(ssid_privacy=mode))
    assert db.latest_wifi()[0][0].ssid == stored
    dump = " ".join(str(row) for row in db._conn.iterdump())
    if mode != "full":
        assert "HomeWiFi" not in dump


def test_a_disconnected_adapter_is_a_real_observation(db):
    poll(db, Scripted(WifiReading(STATE_DISCONNECTED, interface="Wi-Fi", source="wlanapi")))
    assert db.latest_wifi()[0][0].state == "disconnected"
    assert db.get_wifi_status().status == "ok"


# ------------------------------------------------------------------ failures
@pytest.mark.parametrize("category", ["no_adapter", "unsupported_os", "unavailable", "timeout",
                                      "parse_error", "error"])
def test_a_known_failure_stores_its_category_and_a_fixed_message(db, category):
    assert poll(db, Scripted(WifiError(category))) == category
    status = db.get_wifi_status()
    assert status.status == category and status.message == WIFI_MESSAGES[category]
    assert db.latest_wifi() == []  # no observation is invented


def test_an_unexpected_exception_is_an_error_and_its_text_is_never_stored_or_logged(db, caplog):
    caplog.set_level(logging.DEBUG)
    assert poll(db, Scripted(RuntimeError(f"boom {SECRET}"))) == "error"
    assert SECRET not in caplog.text and "boom" not in caplog.text
    assert SECRET not in " ".join(str(r) for r in db._conn.iterdump())
    assert "RuntimeError" in caplog.text  # only the type


def test_a_hung_query_times_out_instead_of_blocking(db):
    started = time.monotonic()
    assert poll(db, Scripted("hang"), timeout_s=0.1) == "timeout"
    assert time.monotonic() - started < 5


def test_a_failure_keeps_the_previous_history(db):
    poll(db, Scripted(GOOD), clock=FakeClock(100.0))
    poll(db, Scripted(WifiError("no_adapter")), clock=FakeClock(130.0))
    assert len(db.latest_wifi(10)) == 1
    status = db.get_wifi_status()
    assert (status.status, status.last_ok_ts) == ("no_adapter", 100.0)


def test_a_repeated_failure_is_logged_once_and_recovery_is_logged(db, caplog):
    caplog.set_level(logging.INFO)
    for _ in range(3):
        poll(db, Scripted(WifiError("no_adapter")))
    poll(db, Scripted(GOOD))
    assert caplog.text.count("Wi-Fi poll failed: no_adapter") == 1
    assert "Wi-Fi poll works again" in caplog.text


def test_the_ssid_never_appears_in_the_log(db, caplog):
    caplog.set_level(logging.DEBUG)
    poll(db, Scripted(GOOD), WifiConfig(ssid_privacy="full"))
    poll(db, Scripted(WifiError("timeout")))
    assert "HomeWiFi" not in caplog.text


def test_staleness_grows_with_the_interval():
    assert wifi_stale_after_s(5) < wifi_stale_after_s(30)


# ------------------------------------------------------------------ the loop
def test_the_loop_keeps_polling_prunes_and_survives_database_errors(db, monkeypatch):
    source = Scripted(GOOD)
    cfg = WifiConfig(poll_interval_s=0.02, retention_days=1)
    real_prune = db.prune_wifi
    calls = []

    def flaky(older_than, max_rows):
        calls.append((older_than, max_rows))
        if len(calls) == 2:
            raise RuntimeError(f"disk {SECRET}")
        return real_prune(older_than, max_rows)

    monkeypatch.setattr(db, "prune_wifi", flaky)

    async def go():
        task = asyncio.create_task(run_wifi_forever(source, db, cfg))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert source.calls >= 4 and len(calls) >= 4  # went on after the failed cycle
    assert all(rows == MAX_OBSERVATIONS for _, rows in calls)
    assert all(abs((time.time() - 86400) - cutoff) < 60 for cutoff, _ in calls)


def test_the_loop_prunes_observations_older_than_the_retention(db):
    old = time.time() - 3 * 86400
    db.save_wifi_observation(old, GOOD)
    cfg = WifiConfig(poll_interval_s=0.02, retention_days=2)

    async def go():
        task = asyncio.create_task(run_wifi_forever(Scripted(GOOD), db, cfg))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert all(ts > old for _, ts in db.latest_wifi(100))


# ------------------------------------------------------------------ isolation from ICMP and SNMP
def snmp_capable(name):
    return Device(name, "192.0.2.5", sim=SimProfile(interfaces=(SimInterface("Gi0/1", in_mbps=5),)))


def inventory_for(devices):
    return Inventory(
        mode="simulated", poll_interval_s=0.05, devices=tuple(devices), snmp_poll_interval_s=0.05,
        wifi=WifiConfig(enabled=True, poll_interval_s=0.05),
    )


@pytest.mark.parametrize("failure", [
    "hang", RuntimeError(f"boom {SECRET}"), WifiError("no_adapter"), WifiError("unsupported_os"),
    TimeoutError(), OSError("x"),
])
def test_icmp_snmp_alerts_and_charts_keep_working_while_wifi_fails(db, failure):
    devices = [snmp_capable("alpha"), snmp_capable("beta")]
    db.sync_devices(devices)
    prober = ScriptedProber({"alpha": [Status.UP] * 3 + [Status.DOWN], "beta": [Status.UP]})
    app = create_app(inventory_for(devices), db, prober, snmp_client=SimulatedSnmpClient(),
                     wifi_source=Scripted(failure))
    with TestClient(app) as client:
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = client.get("/api/devices").json()["devices"]
            snmp = client.get("/api/interfaces").json()["devices"]
            if (rows[0]["status"] == "down" and client.get("/api/alerts").json()["alerts"]
                    and all(d["snmp"]["status"] == "ok" for d in snmp)):
                break
            time.sleep(0.05)
        assert [r["status"] for r in rows] == ["down", "up"]  # ICMP is unaffected
        alerts = client.get("/api/alerts").json()["alerts"]
        assert [(a["device"], a["to_status"]) for a in alerts] == [("alpha", "down")]
        assert client.get("/api/devices/alpha/latency").json()["points"]
        assert all(d["snmp"]["status"] == "ok"
                   for d in client.get("/api/interfaces").json()["devices"])
        wifi_state = client.get("/api/wifi").json()
    assert SECRET not in str(wifi_state)
    assert wifi_state["status"]["status"] in ("timeout", "no_adapter", "unsupported_os", "error",
                                              "pending")


def test_a_slow_wifi_query_does_not_delay_icmp_checks(db):
    devices = [Device("alpha", "192.0.2.1")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     wifi_source=Scripted("hang"))
    with TestClient(app):
        time.sleep(0.6)  # a hung Wi-Fi query would stall a shared loop for its whole timeout
    checks = db.history("alpha", 100)
    assert len(checks) >= 5  # polled every 50 ms the whole time


def test_app_shutdown_is_prompt_even_if_wifi_is_hung(db):
    devices = [Device("alpha", "192.0.2.1")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     wifi_source=Scripted("hang"))

    async def run_app():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run_app(), 5))


def test_no_wifi_task_is_started_unless_it_is_enabled(db):
    devices = [Device("alpha", "192.0.2.1")]
    db.sync_devices(devices)
    source = Scripted(GOOD)
    off = Inventory(mode="simulated", poll_interval_s=0.05, devices=tuple(devices))
    with TestClient(create_app(off, db, ScriptedProber({"alpha": [Status.UP]}),
                               wifi_source=source)):
        time.sleep(0.3)
    assert source.calls == 0 and db.get_wifi_status() is None  # opt-in: nothing was read


def test_no_wifi_task_is_started_without_a_source(db):
    devices = [Device("alpha", "192.0.2.1")]
    db.sync_devices(devices)
    with TestClient(create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}))):
        time.sleep(0.2)
    assert db.get_wifi_status() is None


def test_the_simulated_source_runs_end_to_end_through_the_app(db):
    devices = [Device("alpha", "192.0.2.1")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     wifi_source=SimulatedWifiSource())
    with TestClient(app) as client:
        deadline = time.time() + 5
        while time.time() < deadline and not client.get("/api/wifi").json()["current"]:
            time.sleep(0.05)
        body = client.get("/api/wifi").json()
    assert body["simulated"] is True and body["current"]["state"] == "connected"
    assert body["current"]["ssid"] == "H*******"  # masked by default
