import asyncio
import contextlib
import sqlite3
import time

from netmonlt.collector import poll_once
from netmonlt.domain import PingResult, Status


class BrokenForBeta:
    async def ping(self, device, count=3):
        if device.name == "beta":
            raise RuntimeError("raw socket permission denied")
        return PingResult(count, count, 5.0, 5.0, 5.0)


def test_poll_once_stores_a_check_per_device(db, devices):
    from netmonlt.probes import SimulatedProber

    db.sync_devices(devices)
    results = asyncio.run(poll_once(devices, SimulatedProber(), db, clock=lambda: 1000.0))
    assert {r.device for r in results} == {"alpha", "beta"}
    for row in db.latest_status():
        assert row["status"] == Status.UP and row["ts"] == 1000.0 and row["rtt_avg_ms"] > 0


def test_prober_error_is_not_recorded_as_down(db, devices, caplog):
    db.sync_devices(devices)
    results = asyncio.run(poll_once(devices, BrokenForBeta(), db))
    assert [r.device for r in results] == ["alpha"]
    assert db.history("beta") == []  # no false "down"
    assert len(db.history("alpha")) == 1
    assert "probe failed for beta" in caplog.text


def test_hung_probe_does_not_block_other_devices(db, devices, caplog):
    from helpers import HangingProber

    db.sync_devices(devices)
    prober = HangingProber({"alpha"})
    results = asyncio.run(asyncio.wait_for(poll_once(devices, prober, db, timeout_s=0.2), 5))
    assert [r.device for r in results] == ["beta"]
    assert db.history("alpha") == []  # a timeout is not recorded as "down"
    assert "alpha" in caplog.text and "timed out" in caplog.text


def test_run_forever_stops_promptly_when_cancelled_mid_probe(db, devices):
    import pytest

    from helpers import HangingProber
    from netmonlt.collector import run_forever

    async def scenario():
        task = asyncio.create_task(run_forever(devices, HangingProber(), db, 0.05))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


class FailsFirstCycle:
    def __init__(self):
        self.calls = 0

    async def ping(self, device, count=3):
        self.calls += 1
        if self.calls <= 2:  # both devices in the first cycle
            raise RuntimeError("boom")
        return PingResult(count, count, 5.0, 5.0, 5.0)


class RecordFailsOnce:
    """Wraps a Database; the first write raises like a locked database would."""

    def __init__(self, db):
        self._db = db
        self.failed = False

    def record_check(self, check):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        self._db.record_check(check)


async def _run_until_recorded(devices, prober, db, recorded_db):
    from netmonlt.collector import run_forever

    task = asyncio.create_task(run_forever(devices, prober, recorded_db, 0.05))
    deadline = time.monotonic() + 5
    while not (db.history("alpha") and db.history("beta")) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_run_forever_recovers_after_probe_errors(db, devices):
    db.sync_devices(devices)
    asyncio.run(_run_until_recorded(devices, FailsFirstCycle(), db, db))
    assert db.history("alpha") and db.history("beta")


def test_run_forever_recovers_after_a_database_error(db, devices, caplog):
    from netmonlt.probes import SimulatedProber

    db.sync_devices(devices)
    asyncio.run(_run_until_recorded(devices, SimulatedProber(), db, RecordFailsOnce(db)))
    assert "polling cycle failed" in caplog.text
    assert db.history("alpha") and db.history("beta")
