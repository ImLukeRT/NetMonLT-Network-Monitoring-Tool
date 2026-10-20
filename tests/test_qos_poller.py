"""The QoS poller: storing results, failure handling, unsupported devices, and isolation from
ICMP, SNMP interface polling, Wi-Fi monitoring and alerts."""

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock, ScriptedProber
from netmonlt.api import create_app
from netmonlt.domain import (
    Device,
    SimInterface,
    SimProfile,
    SimQosClass,
    SimQosProfile,
    Status,
    WifiConfig,
)
from netmonlt.inventory import Inventory
from netmonlt.qos import QosRow, QosSnapshot
from netmonlt.qos_poller import (
    MAX_QOS_HISTORY_ROWS,
    poll_device_qos,
    qos_poll_once,
    qos_stale_after_s,
    run_qos_forever,
)
from netmonlt.qos_sim import SimulatedQosClient
from netmonlt.snmp import SimulatedSnmpClient, SnmpError
from netmonlt.wifi import SimulatedWifiSource

SECRET = "SECRET-sentinel-value-9f3a"
GAP = 1000.0


def snapshot(pkts=0, octs=0, uptime=1000):
    row = QosRow("c", "traffic", "Class", "d", pkts, octs, 0, 0)
    return QosSnapshot("diffserv-mib", "DIFFSERV-MIB (RFC 3289)", (row,), uptime)


class Scripted:
    """A client that returns or raises the next scripted item per device; "hang" never returns."""

    def __init__(self, script):
        self.script = script
        self.calls = {name: 0 for name in script}

    async def poll(self, device):
        seq = self.script[device.name]
        item = seq[min(self.calls[device.name], len(seq) - 1)]
        self.calls[device.name] += 1
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return item


def poll(db, device, client, ts=100.0, **kw):
    return asyncio.run(
        poll_device_qos(device, client, db, clock=FakeClock(ts), max_gap_s=GAP, **kw)
    )


# ------------------------------------------------------------------ storing results
def test_a_successful_poll_is_stored(db, devices):
    db.sync_devices(devices)
    assert poll(db, devices[0], Scripted({"alpha": [snapshot(5, 6)]})) == "ok"
    assert db.get_qos_status("alpha").status == "ok"
    assert db.get_qos_states("alpha")["c"].packets == 5


def test_rates_appear_from_the_second_poll_and_a_restart_voids_one(db, devices):
    db.sync_devices(devices)
    client = Scripted({"alpha": [snapshot(0, 0, 1000), snapshot(100, 800, 1100),
                                 snapshot(300, 2400, 50), snapshot(400, 3200, 150)]})
    for ts in (100.0, 110.0, 120.0, 130.0):
        poll(db, devices[0], client, ts=ts)
        state = db.get_qos_states("alpha")["c"]
        if ts == 100.0:
            assert state.rate_note == "first_sample"
        elif ts == 110.0:
            assert state.pps == 10.0 and state.rate_note is None
        elif ts == 120.0:  # sysUpTime went from 1100 to 50: the device restarted
            assert state.rate_note == "counter_reset" and state.pps is None
        else:
            assert state.pps == 10.0 and state.rate_note is None


def test_a_gap_since_the_last_poll_gives_no_rate(db, devices):
    db.sync_devices(devices)
    client = Scripted({"alpha": [snapshot(0, 0), snapshot(100, 800)]})
    poll(db, devices[0], client, ts=100.0)
    poll(db, devices[0], client, ts=100.0 + GAP + 1)
    assert db.get_qos_states("alpha")["c"].rate_note == "gap"


# ------------------------------------------------------------------ unsupported and failures
def test_a_device_that_returns_no_elements_is_unsupported_and_stores_no_qos_data(db, devices):
    db.sync_devices(devices)
    empty = QosSnapshot("diffserv-mib", "src", ())
    assert poll(db, devices[0], Scripted({"alpha": [empty]})) == "unsupported"
    status = db.get_qos_status("alpha")
    assert status.status == "unsupported" and "no QoS data is shown" in status.message
    assert db.get_qos_states("alpha") == {} and db.qos_samples("alpha", 0, 1e9)[0] == []


@pytest.mark.parametrize("category", ["timeout", "auth_failure", "unsupported", "config", "error"])
def test_a_known_failure_stores_its_category_and_a_fixed_message(db, devices, category):
    db.sync_devices(devices)
    assert poll(db, devices[0], Scripted({"alpha": [SnmpError(category)]})) == category
    status = db.get_qos_status("alpha")
    assert status.status == category and status.message
    assert db.get_qos_states("alpha") == {}  # nothing is invented


def test_an_unexpected_exception_is_an_error_and_its_text_is_never_stored_or_logged(
    db, devices, caplog
):
    db.sync_devices(devices)
    caplog.set_level(logging.DEBUG)
    assert poll(db, devices[0], Scripted({"alpha": [RuntimeError(f"boom {SECRET}")]})) == "error"
    assert SECRET not in caplog.text and "boom" not in caplog.text
    assert SECRET not in " ".join(str(r) for r in db._conn.iterdump())
    assert "RuntimeError" in caplog.text


def test_a_hung_poll_times_out_instead_of_blocking(db, devices):
    db.sync_devices(devices)
    started = time.monotonic()
    assert poll(db, devices[0], Scripted({"alpha": ["hang"]}), timeout_s=0.1) == "timeout"
    assert time.monotonic() - started < 5


def test_a_failure_keeps_the_previous_data(db, devices):
    db.sync_devices(devices)
    client = Scripted({"alpha": [snapshot(5, 6), SnmpError("timeout")]})
    poll(db, devices[0], client, ts=100.0)
    poll(db, devices[0], client, ts=130.0)
    status = db.get_qos_status("alpha")
    assert (status.status, status.last_ok_ts) == ("timeout", 100.0)
    assert db.get_qos_states("alpha")["c"].packets == 5


def test_a_repeated_failure_is_logged_once_and_recovery_is_logged(db, devices, caplog):
    db.sync_devices(devices)
    caplog.set_level(logging.INFO)
    client = Scripted({"alpha": [SnmpError("timeout")] * 3 + [snapshot()]})
    for ts in (1.0, 2.0, 3.0, 4.0):
        poll(db, devices[0], client, ts=ts)
    assert caplog.text.count("QoS poll of alpha failed: timeout") == 1
    assert "QoS poll of alpha works again" in caplog.text


def test_one_failing_device_does_not_stop_the_others(db, devices):
    db.sync_devices(devices)
    client = Scripted({"alpha": [RuntimeError("x")], "beta": [snapshot(1, 2)]})
    result = asyncio.run(qos_poll_once(devices, client, db, clock=FakeClock(5.0), max_gap_s=GAP))
    assert result == {"alpha": "error", "beta": "ok"}


def test_a_database_error_for_one_device_is_contained(db, devices, monkeypatch):
    db.sync_devices(devices)
    real = db.save_qos_success

    def flaky(device, *args):
        if device == "alpha":
            raise RuntimeError(f"disk {SECRET}")
        return real(device, *args)

    monkeypatch.setattr(db, "save_qos_success", flaky)
    client = Scripted({"alpha": [snapshot()], "beta": [snapshot()]})
    result = asyncio.run(qos_poll_once(devices, client, db, clock=FakeClock(5.0), max_gap_s=GAP))
    assert result == {"alpha": "error", "beta": "ok"}


def test_staleness_grows_with_the_interval():
    assert qos_stale_after_s(5) < qos_stale_after_s(30)


# ------------------------------------------------------------------ the loop
def test_the_loop_keeps_polling_and_prunes_history(db, devices, monkeypatch):
    db.sync_devices(devices)
    client = Scripted({"alpha": [snapshot()]})
    calls = []
    real = db.prune_qos

    def spy(older_than, max_rows, absent_older_than):
        calls.append((older_than, max_rows))
        if len(calls) == 2:
            raise RuntimeError(f"disk {SECRET}")  # a bad cycle must not end the loop
        return real(older_than, max_rows, absent_older_than)

    monkeypatch.setattr(db, "prune_qos", spy)

    async def go():
        task = asyncio.create_task(run_qos_forever(devices[:1], client, db, 0.02, 2.0))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert client.calls["alpha"] >= 4 and len(calls) >= 4
    assert all(rows == MAX_QOS_HISTORY_ROWS for _, rows in calls)
    assert all(abs((time.time() - 2 * 3600) - cutoff) < 60 for cutoff, _ in calls)


# ------------------------------------------------------------------ isolation
def qos_capable(name):
    profile = SimQosProfile(classes=(SimQosClass("a", 5),))
    return Device(name, "192.0.2.5",
                  sim=SimProfile(interfaces=(SimInterface("Gi0/1", in_mbps=5),), qos=profile))


def inventory_for(devices):
    return Inventory(
        mode="simulated", poll_interval_s=0.05, devices=tuple(devices), snmp_poll_interval_s=0.05,
        wifi=WifiConfig(enabled=True, poll_interval_s=0.05), qos_poll_interval_s=0.05,
    )


@pytest.mark.parametrize("failure", [
    "hang", RuntimeError(f"boom {SECRET}"), SnmpError("auth_failure"), SnmpError("unsupported"),
    TimeoutError(), OSError("x"),
])
def test_icmp_snmp_wifi_alerts_and_charts_keep_working_while_qos_fails(db, failure):
    devices = [qos_capable("alpha"), qos_capable("beta")]
    db.sync_devices(devices)
    prober = ScriptedProber({"alpha": [Status.UP] * 3 + [Status.DOWN], "beta": [Status.UP]})
    app = create_app(
        inventory_for(devices), db, prober, snmp_client=SimulatedSnmpClient(),
        wifi_source=SimulatedWifiSource(),
        qos_client=Scripted({"alpha": [failure], "beta": [failure]}),
    )
    with TestClient(app) as client:
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = client.get("/api/devices").json()["devices"]
            snmp = client.get("/api/interfaces").json()["devices"]
            wifi = client.get("/api/wifi").json()["current"]
            if (rows[0]["status"] == "down" and client.get("/api/alerts").json()["alerts"]
                    and all(d["snmp"]["status"] == "ok" for d in snmp) and wifi):
                break
            time.sleep(0.05)
        assert [r["status"] for r in rows] == ["down", "up"]  # ICMP is unaffected
        alerts = client.get("/api/alerts").json()["alerts"]
        assert [(a["device"], a["to_status"]) for a in alerts] == [("alpha", "down")]
        assert client.get("/api/devices/alpha/latency").json()["points"]
        assert all(d["snmp"]["status"] == "ok" for d in snmp)  # interface polling unaffected
        assert wifi["state"] == "connected"  # Wi-Fi monitoring unaffected
        body = client.get("/api/qos").json()
    assert SECRET not in str(body)  # and the failure is reported, safely
    assert all(d["status"]["status"] in ("timeout", "auth_failure", "unsupported", "error",
                                         "pending") for d in body["devices"])
    assert all(d["rows"] == [] for d in body["devices"])  # no fabricated QoS rows


def test_a_slow_qos_poll_does_not_delay_icmp_checks(db):
    devices = [qos_capable("alpha")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     qos_client=Scripted({"alpha": ["hang"]}))
    with TestClient(app):
        time.sleep(0.6)
    assert len(db.history("alpha", 100)) >= 5  # polled every 50 ms the whole time


def test_app_shutdown_is_prompt_even_if_qos_is_hung(db):
    devices = [qos_capable("alpha")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     qos_client=Scripted({"alpha": ["hang"]}))

    async def run_app():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run_app(), 5))


def test_no_qos_task_is_started_without_a_client_or_qos_devices(db):
    plain = [Device("alpha", "192.0.2.1")]
    db.sync_devices(plain)
    source = Scripted({"alpha": [snapshot()]})
    with TestClient(create_app(inventory_for(plain), db, ScriptedProber({"alpha": [Status.UP]}),
                               qos_client=source)):
        time.sleep(0.3)
    assert source.calls["alpha"] == 0 and db.get_qos_status("alpha") is None  # opt-in per device
    devices = [qos_capable("alpha")]
    db.sync_devices(devices)
    with TestClient(create_app(inventory_for(devices), db,
                               ScriptedProber({"alpha": [Status.UP]}))):
        time.sleep(0.2)
    assert db.get_qos_status("alpha") is None  # no client: nothing is polled


def test_a_device_without_qos_is_never_polled_even_if_others_are(db):
    devices = [qos_capable("alpha"), Device("beta", "192.0.2.6")]
    db.sync_devices(devices)
    client = Scripted({"alpha": [snapshot()], "beta": [snapshot()]})
    app = create_app(inventory_for(devices), db,
                     ScriptedProber({"alpha": [Status.UP], "beta": [Status.UP]}),
                     qos_client=client)
    with TestClient(app):
        time.sleep(0.4)
    assert client.calls["alpha"] > 0 and client.calls["beta"] == 0


def test_the_simulated_client_runs_end_to_end_through_the_app(db):
    devices = [qos_capable("alpha")]
    db.sync_devices(devices)
    app = create_app(inventory_for(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     qos_client=SimulatedQosClient())
    with TestClient(app) as client:
        deadline = time.time() + 6
        body = client.get("/api/qos").json()
        # the simulated counters advance in whole seconds, so wait until traffic has been counted
        while time.time() < deadline and not (
            body["devices"][0]["rows"] and (body["devices"][0]["rows"][0]["bps"] or 0) > 0
        ):
            time.sleep(0.05)
            body = client.get("/api/qos").json()
    entry = body["devices"][0]
    assert body["simulated"] is True and entry["adapter"] == "simulated"
    assert entry["rows"][0]["name"] == "a" and entry["rows"][0]["bps"] > 0
