import asyncio

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
