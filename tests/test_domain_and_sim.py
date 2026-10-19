import asyncio

from helpers import FakeClock
from netmonlt.domain import Device, PingResult, SimProfile, Status, classify
from netmonlt.probes import SimulatedProber


def ping(sent, received, avg):
    return PingResult(sent, received, avg, avg, avg)


def test_classify():
    assert classify(PingResult(3, 0, None, None, None)) == Status.DOWN
    assert classify(ping(3, 3, 10)) == Status.UP
    assert classify(ping(10, 7, 10)) == Status.DEGRADED  # 30% loss
    assert classify(ping(10, 9, 10)) == Status.UP  # 10% loss is tolerated
    assert classify(ping(3, 3, 200)) == Status.DEGRADED  # high latency


def run(prober, device, count=3):
    return asyncio.run(prober.ping(device, count))


def test_simulation_is_deterministic():
    dev = Device("a", "192.0.2.1", sim=SimProfile(rtt_ms=20, jitter_ms=5, loss=0.3))

    def sequence(seed):
        prober = SimulatedProber(seed=seed, clock=FakeClock())
        return [run(prober, dev) for _ in range(20)]

    assert sequence(1) == sequence(1)
    assert sequence(1) != sequence(2)  # the seed actually matters
    assert len({r.rtt_avg_ms for r in sequence(1)}) > 1  # and results are not constant


def test_rtt_close_to_profile():
    dev = Device("a", "192.0.2.1", sim=SimProfile(rtt_ms=20, jitter_ms=1))
    result = run(SimulatedProber(clock=FakeClock()), dev, count=50)
    assert result.received == 50
    assert 18 < result.rtt_avg_ms < 22
    assert result.rtt_min_ms <= result.rtt_avg_ms <= result.rtt_max_ms


def test_total_loss_means_nothing_received():
    dev = Device("a", "192.0.2.1", sim=SimProfile(loss=1.0))
    result = run(SimulatedProber(clock=FakeClock()), dev)
    assert result.received == 0 and result.rtt_avg_ms is None
    assert classify(result) == Status.DOWN


def test_outage_window_starts_and_ends():
    clock = FakeClock()
    dev = Device("a", "192.0.2.1", sim=SimProfile(outage_start_s=10, outage_duration_s=5))
    prober = SimulatedProber(clock=clock)
    seen = {}
    for t in (0, 9.9, 10, 14.9, 15, 30):
        clock.now = t
        seen[t] = run(prober, dev).received > 0
    assert seen == {0: True, 9.9: True, 10: False, 14.9: False, 15: True, 30: True}


def test_one_lost_packet_in_a_three_ping_check_is_degraded():
    # The collector sends 3 pings per check, so loss can only be 0/33/67/100%. The 20% threshold
    # therefore means "any loss at all"; this pins that behaviour so changing it is deliberate.
    assert classify(PingResult(3, 2, 5.0, 5.0, 5.0)) == Status.DEGRADED
    assert classify(PingResult(3, 3, 5.0, 5.0, 5.0)) == Status.UP
