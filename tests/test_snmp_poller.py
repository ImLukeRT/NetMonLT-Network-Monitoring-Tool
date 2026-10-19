"""The SNMP poller: storing results, failure handling, and isolation from ICMP monitoring."""

import asyncio
import contextlib
import logging
import time

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock, ScriptedProber
from netmonlt import snmp_poller
from netmonlt.api import create_app
from netmonlt.domain import Device, SimInterface, SimProfile, Status
from netmonlt.ifstats import InterfaceReading, InterfaceSnapshot
from netmonlt.inventory import Inventory
from netmonlt.snmp import SNMP_MESSAGES, SnmpError
from netmonlt.snmp_poller import poll_device_snmp, run_snmp_forever, snmp_poll_once

GAP = 1000.0
SECRET = "SECRET-sentinel-value-9f3a"


def snap(in_octets=0, out_octets=0, uptime=1000, *names):
    names = names or ("eth0",)
    readings = tuple(
        InterfaceReading(i, n, "up", in_octets=in_octets, out_octets=out_octets,
                         in_errors=0, out_errors=0, in_discards=0, out_discards=0)
        for i, n in enumerate(names, start=1)
    )
    return InterfaceSnapshot(readings, uptime_ticks=uptime)


class Scripted:
    """An SnmpClient answering each device's n-th poll with script[device][n].

    An entry is a snapshot (returned), an exception (raised) or "hang". The last entry repeats.
    """

    def __init__(self, script):
        self.script, self.calls = script, {name: 0 for name in script}

    async def poll(self, device):
        seq = self.script[device.name]
        outcome = seq[min(self.calls[device.name], len(seq) - 1)]
        self.calls[device.name] += 1
        if outcome == "hang":
            await asyncio.sleep(3600)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(devices, client, db, polls, *, step=10.0, start=1000.0, **kwargs):
    """Run `polls` polling cycles `step` seconds apart on a fake clock."""
    clock = FakeClock(start)
    gap = kwargs.pop("gap", GAP)

    async def go():
        results = []
        for i in range(polls):
            clock.now = start + i * step
            results.append(
                await snmp_poll_once(devices, client, db, clock=clock, max_gap_s=gap, **kwargs)
            )
        return results

    return asyncio.run(go())


@pytest.fixture
def snmp_devices():
    return [Device("alpha", "192.0.2.1"), Device("beta", "192.0.2.2")]


@pytest.fixture
def sdb(db, snmp_devices):
    db.sync_devices(snmp_devices)
    return db


# ------------------------------------------------------------------ storing results
def test_the_second_poll_computes_rates_that_the_first_could_not(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(0, 0), snap(12_500_000, 2_500_000)], "beta": [snap()]})
    (first, second) = run(snmp_devices, client, sdb, 2)
    assert first == {"alpha": "ok", "beta": "ok"}
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.in_bps == 10_000_000.0 and state.out_bps == 2_000_000.0 and state.rate_note is None
    status = sdb.get_snmp_status("alpha")
    assert (status.status, status.ts, status.last_ok_ts) == ("ok", 1010.0, 1010.0)


def test_the_very_first_sample_is_labelled_not_guessed(sdb, snmp_devices):
    run(snmp_devices, Scripted({"alpha": [snap(5, 5)], "beta": [snap()]}), sdb, 1)
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.rate_note == "first_sample" and state.in_bps is None


def test_a_device_restart_between_polls_gives_no_rate(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(10**9, 10**9, uptime=90_000), snap(500, 500, uptime=300)],
                       "beta": [snap()]})
    run(snmp_devices, client, sdb, 2)
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.in_bps is None and state.rate_note == "counter_reset"


def test_an_interface_that_stops_being_reported_becomes_absent(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(0, 0, 1000, "eth0", "usb0"), snap(10, 10, 1100, "eth0")],
                       "beta": [snap()]})
    run(snmp_devices, client, sdb, 2)
    states = sdb.get_interface_states("alpha")
    assert states[2].present is False and states[2].oper_status == "absent"
    assert states[1].present is True


def test_rates_are_not_calculated_across_a_long_gap(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(0, 0), snap(10**9, 10**9)], "beta": [snap()]})
    run(snmp_devices, client, sdb, 2, step=500.0, gap=100.0)
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.in_bps is None and state.rate_note == "gap"


# ------------------------------------------------------------------ failures
@pytest.mark.parametrize("category", ["timeout", "auth_failure", "unsupported", "config", "error"])
def test_each_failure_category_is_stored_with_its_fixed_safe_message(sdb, snmp_devices, category):
    client = Scripted({"alpha": [SnmpError(category)], "beta": [snap()]})
    result = run(snmp_devices, client, sdb, 1)[0]
    assert result == {"alpha": category, "beta": "ok"}
    status = sdb.get_snmp_status("alpha")
    assert (status.status, status.message) == (category, SNMP_MESSAGES[category])
    assert sdb.get_interface_states("alpha") == {}  # nothing invented


def test_a_failure_keeps_the_last_good_data_and_does_not_mark_interfaces_absent(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(100, 100), SnmpError("timeout")], "beta": [snap()]})
    run(snmp_devices, client, sdb, 2)
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.present and state.oper_status == "up" and state.in_octets == 100
    assert sdb.get_snmp_status("alpha").status == "timeout"


def test_recovery_after_a_failure_computes_a_rate_across_the_outage(sdb, snmp_devices):
    client = Scripted({"alpha": [snap(0, 0), SnmpError("timeout"), snap(25_000_000, 0)],
                       "beta": [snap()]})
    run(snmp_devices, client, sdb, 3)  # polls at t=1000, 1010 (fails), 1020
    (state,) = sdb.get_interface_states("alpha").values()
    assert state.in_bps == 10_000_000.0 and state.interval_s == 20.0  # true elapsed time
    assert sdb.get_snmp_status("alpha").status == "ok"


def test_a_client_that_raises_timeout_error_is_a_timeout(sdb, snmp_devices):
    client = Scripted({"alpha": [TimeoutError()], "beta": [snap()]})
    assert run(snmp_devices, client, sdb, 1)[0]["alpha"] == "timeout"


def test_a_hung_client_is_cut_off_and_does_not_delay_the_others(sdb, snmp_devices):
    client = Scripted({"alpha": ["hang"], "beta": [snap()]})
    started = time.monotonic()
    result = run(snmp_devices, client, sdb, 1, timeout_s=0.2)[0]
    assert time.monotonic() - started < 3
    assert result == {"alpha": "timeout", "beta": "ok"}
    assert sdb.get_interface_states("beta")


def test_an_unexpected_error_never_leaks_its_text(sdb, snmp_devices, caplog):
    boom = RuntimeError(f"failed with community {SECRET}")
    client = Scripted({"alpha": [boom], "beta": [snap()]})
    with caplog.at_level(logging.DEBUG):
        result = run(snmp_devices, client, sdb, 1)[0]
    assert result["alpha"] == "error"
    status = sdb.get_snmp_status("alpha")
    assert status.message == SNMP_MESSAGES["error"]
    assert SECRET not in caplog.text and SECRET not in status.message
    assert "RuntimeError" in caplog.text  # the type is logged, the text is not


def test_one_failing_device_does_not_affect_another(sdb, snmp_devices):
    client = Scripted({"alpha": [SnmpError("auth_failure")],
                       "beta": [snap(0, 0), snap(1_250_000, 0)]})
    run(snmp_devices, client, sdb, 2)
    (state,) = sdb.get_interface_states("beta").values()
    assert state.in_bps == 1_000_000.0 and sdb.get_snmp_status("beta").status == "ok"


def test_a_database_error_for_one_device_is_contained(sdb, snmp_devices, monkeypatch, caplog):
    real_save = sdb.save_snmp_success

    def flaky(device, *args, **kwargs):
        if device == "alpha":
            raise OSError(f"disk full {SECRET}")
        return real_save(device, *args, **kwargs)

    monkeypatch.setattr(sdb, "save_snmp_success", flaky)
    client = Scripted({"alpha": [snap()], "beta": [snap()]})
    result = run(snmp_devices, client, sdb, 1)[0]
    assert result == {"alpha": "error", "beta": "ok"}
    assert SECRET not in caplog.text and "OSError" in caplog.text


def test_a_failure_is_logged_once_per_change_not_every_cycle(sdb, snmp_devices, caplog):
    client = Scripted({"alpha": [SnmpError("timeout")] * 5 + [snap()] , "beta": [snap()]})
    with caplog.at_level(logging.INFO):
        run(snmp_devices, client, sdb, 6)
    warnings = [r for r in caplog.records if "SNMP poll of alpha failed" in r.getMessage()]
    assert len(warnings) == 1
    assert any("alpha works again" in r.getMessage() for r in caplog.records)


def test_poll_device_snmp_returns_the_stored_status(sdb, snmp_devices):
    status = asyncio.run(poll_device_snmp(
        snmp_devices[0], Scripted({"alpha": [snap()]}), sdb, clock=FakeClock(5.0), max_gap_s=GAP))
    assert status == "ok"


# ------------------------------------------------------------------ the loop
def test_the_loop_survives_failures_and_keeps_pruning(sdb, snmp_devices, monkeypatch):
    pruned = []
    monkeypatch.setattr(sdb, "prune_absent_interfaces", lambda t: pruned.append(t) or 0)
    client = Scripted({"alpha": [RuntimeError("x"), SnmpError("timeout"), snap()],
                       "beta": [snap()]})

    async def scenario():
        task = asyncio.create_task(run_snmp_forever(snmp_devices, client, sdb, 0.05))
        deadline = time.monotonic() + 5
        while client.calls["alpha"] < 4 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.calls["alpha"] >= 4 and len(pruned) >= 3
    assert sdb.get_snmp_status("alpha").status == "ok"


def test_the_loop_stops_promptly_when_cancelled_mid_poll(sdb, snmp_devices):
    client = Scripted({"alpha": ["hang"], "beta": ["hang"]})

    async def scenario():
        task = asyncio.create_task(run_snmp_forever(snmp_devices, client, sdb, 0.05))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_stale_threshold_scales_with_the_interval():
    assert snmp_poller.snmp_stale_after_s(30) == 3 * 30 + snmp_poller.SNMP_POLL_TIMEOUT_S


# ------------------------------------------------------------------ SNMP can't harm ICMP monitoring
def icmp_inventory(devices):
    return Inventory(mode="simulated", poll_interval_s=0.05, devices=tuple(devices),
                     snmp_poll_interval_s=0.05)


def snmp_capable(name):
    return Device(name, "192.0.2.5", sim=SimProfile(interfaces=(SimInterface("Gi0/1", in_mbps=5),)))


@pytest.mark.parametrize("failure", [
    "hang", RuntimeError(f"boom {SECRET}"), SnmpError("auth_failure"), TimeoutError(), OSError("x"),
])
def test_icmp_monitoring_alerts_and_charts_keep_working_while_snmp_fails(db, failure):
    devices = [snmp_capable("alpha"), snmp_capable("beta")]
    db.sync_devices(devices)
    prober = ScriptedProber({"alpha": [Status.UP] * 3 + [Status.DOWN], "beta": [Status.UP]})
    app = create_app(icmp_inventory(devices), db, prober,
                     snmp_client=Scripted({"alpha": [failure], "beta": [failure]}))
    with TestClient(app) as client:
        deadline = time.time() + 8
        while time.time() < deadline:
            rows = client.get("/api/devices").json()["devices"]
            if rows[0]["status"] == "down" and client.get("/api/alerts").json()["alerts"]:
                break
            time.sleep(0.05)
        assert [r["status"] for r in rows] == ["down", "up"]  # ICMP monitoring is unaffected
        alerts = client.get("/api/alerts").json()["alerts"]
        assert [(a["device"], a["to_status"]) for a in alerts] == [("alpha", "down")]
        latency = client.get("/api/devices/alpha/latency")
        assert latency.status_code == 200 and latency.json()["points"]
        assert client.get("/api/devices/alpha/history").status_code == 200
        body = client.get("/api/interfaces").json()
    assert SECRET not in str(body)  # and the failure is reported, safely
    assert all(d["snmp"]["status"] in ("timeout", "auth_failure", "error", "pending")
               for d in body["devices"])


def test_app_shutdown_is_prompt_even_if_snmp_is_hung(db):
    devices = [snmp_capable("alpha")]
    db.sync_devices(devices)
    app = create_app(icmp_inventory(devices), db, ScriptedProber({"alpha": [Status.UP]}),
                     snmp_client=Scripted({"alpha": ["hang"]}))

    async def run_app():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.2)

    asyncio.run(asyncio.wait_for(run_app(), 5))


def test_no_snmp_task_is_started_without_a_client_or_snmp_devices(db):
    plain = [Device("alpha", "192.0.2.1")]
    db.sync_devices(plain)
    app = create_app(icmp_inventory(plain), db, ScriptedProber({"alpha": [Status.UP]}),
                     snmp_client=Scripted({"alpha": [snap()]}))  # no SNMP-enabled devices
    with TestClient(app) as client:
        time.sleep(0.2)
        assert client.get("/api/interfaces").json()["devices"] == []
    assert db.get_snmp_status("alpha") is None
