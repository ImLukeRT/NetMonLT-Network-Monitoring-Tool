"""The simulated QoS telemetry: deterministic, scripted, honest about being simulated."""

import asyncio

import pytest

from helpers import FakeClock
from netmonlt.domain import Device, SimProfile, SimQosClass, SimQosProfile
from netmonlt.qos import advance_device
from netmonlt.qos_sim import SIM_ADAPTER, SimulatedQosClient, allocate, in_congestion
from netmonlt.snmp import SnmpError

CLASSES = (
    SimQosClass("Voice", 6, priority=True, avg_packet_bytes=200),
    SimQosClass("Video", 18, weight=3, avg_packet_bytes=1100),
    SimQosClass("Business", 22, weight=2, surges=True, avg_packet_bytes=700),
    SimQosClass("Best effort", 24, weight=1, surges=True, avg_packet_bytes=900,
                counter_bits=32, wrap_at_s=75),
    SimQosClass("Scavenger", 6, weight=0.5, avg_packet_bytes=1200),
)
PROFILE = SimQosProfile("wan0", 100.0, 60.0, 55.0, 2.0, CLASSES)


def device(profile=PROFILE, **sim):
    return Device("fw", "192.0.2.2", sim=SimProfile(qos=profile, **sim))


class Run:
    """Polls a simulated device at chosen simulated times, deriving rates as the app does."""

    def __init__(self, dev=None, clock=None):
        self.dev = dev or device()
        self.clock = clock or FakeClock(0.0)
        self.client = SimulatedQosClient(self.clock)
        self.states = {}
        self.uptime = None

    def poll(self, t):
        self.clock.now = float(t)
        snapshot = asyncio.run(self.client.poll(self.dev))
        new = advance_device(self.states, self.uptime, snapshot, float(t), 100.0)
        self.states = {s.key: s for s in new}
        self.uptime = snapshot.uptime_ticks
        return snapshot, {s.name: s for s in new}


# ------------------------------------------------------------------ allocation
def test_the_link_never_carries_more_than_its_capacity_or_more_than_offered():
    for load in (0.2, 1.0, 1.5, 3.0):
        offered = [c.offered_mbps * load for c in CLASSES]
        served = allocate(PROFILE, offered)
        assert sum(served) <= PROFILE.capacity_mbps + 1e-6
        assert all(0 <= s <= o + 1e-9 for s, o in zip(served, offered, strict=True))


def test_when_there_is_room_nothing_is_dropped():
    offered = [c.offered_mbps for c in CLASSES]
    assert allocate(PROFILE, offered) == pytest.approx(offered)


def test_the_priority_class_is_served_first_when_the_link_is_overloaded():
    offered = [6, 40, 80, 80, 40]
    served = allocate(PROFILE, offered)
    assert served[0] == 6  # voice untouched
    assert sum(served) == pytest.approx(100.0)


def test_leftover_capacity_is_shared_by_weight_and_unused_share_is_redistributed():
    profile = SimQosProfile("x", 100.0, classes=(
        SimQosClass("a", 0, weight=1), SimQosClass("b", 0, weight=3)))
    even = allocate(profile, [100.0, 100.0])
    assert even == pytest.approx([25.0, 75.0])  # 1:3
    # a only wants 10, so b may take the rest
    assert allocate(profile, [10.0, 100.0]) == pytest.approx([10.0, 90.0])


def test_a_priority_class_alone_cannot_exceed_the_link():
    profile = SimQosProfile("x", 10.0, classes=(SimQosClass("p", 0, priority=True),))
    assert allocate(profile, [50.0]) == [10.0]


def test_congestion_window():
    assert not in_congestion(PROFILE, 59.9) and in_congestion(PROFILE, 60.0)
    assert in_congestion(PROFILE, 114.9) and not in_congestion(PROFILE, 115.0)
    assert not in_congestion(SimQosProfile(classes=CLASSES), 70.0)  # none scripted


# ------------------------------------------------------------------ the scripted story
def test_it_is_labelled_simulated_everywhere():
    snapshot, _ = Run().poll(10)
    assert snapshot.adapter == SIM_ADAPTER == "simulated"
    assert "simulated" in snapshot.source_mib and "no real device" in snapshot.source_mib
    assert all("simulated" in r.detail for r in snapshot.rows)


def test_each_class_is_a_row_with_all_four_counters():
    snapshot, _ = Run().poll(10)
    assert [r.name for r in snapshot.rows] == [c.name for c in CLASSES]
    for r in snapshot.rows:
        assert None not in (r.packets, r.octets, r.drop_packets, r.drop_octets)
        assert r.kind == "traffic" and r.key == f"class:{r.name}"


def test_a_normal_period_has_traffic_and_no_drops():
    run = Run()
    run.poll(10)
    _, rows = run.poll(40)
    for name, s in rows.items():
        assert s.rate_note is None and s.bps > 0 and s.pps > 0
        assert s.drop_pct == 0.0, name


def test_congestion_makes_the_lower_queues_drop_while_priority_and_video_stay_clean():
    run = Run()
    run.poll(50)
    _, rows = run.poll(100)
    assert rows["Voice"].drop_pct == 0.0 and rows["Video"].drop_pct == 0.0
    assert rows["Scavenger"].drop_pct == 0.0
    assert rows["Best effort"].drop_pct > 20  # the weakest surging class drops the most
    assert rows["Best effort"].drop_pct > rows["Business"].drop_pct
    assert rows["Best effort"].drop_pps > 0


def test_the_surging_classes_offer_more_traffic_during_congestion():
    run = Run()
    run.poll(40)
    _, calm = run.poll(55)
    run.poll(70)
    _, busy = run.poll(100)
    assert busy["Business"].bps > calm["Business"].bps * 1.4
    assert busy["Video"].bps == pytest.approx(calm["Video"].bps, rel=0.3)  # video does not surge


def test_the_link_is_never_oversubscribed_in_the_carried_throughput():
    run = Run()
    run.poll(50)
    _, rows = run.poll(100)
    carried = sum(s.bps * (1 - (s.drop_pct or 0) / 100) for s in rows.values())
    assert carried <= 100e6 * 1.05  # capacity, within the wobble of a one-poll average


def test_recovery_after_congestion_stops_the_drops():
    run = Run()
    run.poll(100)
    run.poll(118)
    _, rows = run.poll(124)
    assert all(s.drop_pct == 0.0 for s in rows.values())


def test_a_32_bit_class_rolls_over_without_a_false_reset_or_a_spike():
    run = Run()
    run.poll(60)
    _, before = run.poll(70)
    _, across = run.poll(80)  # the Best-effort counters wrap at 75 s
    be_before, be_across = before["Best effort"], across["Best effort"]
    assert be_across.rate_note is None  # recognised as a rollover, not a reset
    assert be_across.bps == pytest.approx(be_before.bps, rel=0.5)  # continuous, no spike or zero
    assert be_across.counter_bits == 32


def test_a_32_bit_class_really_does_wrap_its_raw_counters():
    run = Run()
    snapshot_before, _ = run.poll(70)
    snapshot_after, _ = run.poll(80)
    by_name = lambda s, n: next(r for r in s.rows if r.name == n)  # noqa: E731
    assert by_name(snapshot_before, "Best effort").octets > 2**31
    assert by_name(snapshot_after, "Best effort").octets < 2**31
    assert by_name(snapshot_after, "Business").octets > 2**31  # a 64-bit class does not wrap


def test_a_restart_resets_every_counter_and_the_uptime_and_voids_one_sample():
    run = Run(device(reboot_at_s=130.0))
    run.poll(100)
    snapshot_before, _ = run.poll(125)
    snapshot_after, rows = run.poll(135)
    assert all(s.rate_note == "counter_reset" and s.bps is None for s in rows.values())
    assert snapshot_after.uptime_ticks < snapshot_before.uptime_ticks
    assert all(r.octets < 2_000_000_000 for r in snapshot_after.rows)  # counting from zero again
    _, later = run.poll(145)
    assert all(s.rate_note is None and s.bps > 0 for s in later.values())


def test_a_scripted_outage_times_out_like_an_unreachable_device():
    run = Run(device(outage_start_s=30.0, outage_duration_s=20.0))
    run.poll(10)
    with pytest.raises(SnmpError) as caught:
        run.poll(35)
    assert caught.value.category == "timeout"
    run.poll(55)  # and it comes back


# ------------------------------------------------------------------ determinism
def counters(snapshot):
    return [(r.name, r.packets, r.octets, r.drop_packets, r.drop_octets) for r in snapshot.rows]


def test_counters_are_a_pure_function_of_the_elapsed_whole_seconds():
    times = [3.0, 18.4, 47.9, 66.0, 91.5, 116.2]
    first = [counters(Run().poll(t)[0]) for t in times]
    second = [counters(Run().poll(t)[0]) for t in times]
    assert first == second


def test_how_often_it_is_polled_does_not_change_the_counters():
    sparse, dense = Run(), Run()
    dense_result = None
    for t in range(0, 101, 5):
        dense_result = dense.poll(t)[0]
    assert counters(sparse.poll(100)[0]) == counters(dense_result)


def test_the_counters_only_ever_increase_between_restarts_apart_from_scripted_wraps():
    run = Run()
    previous = {r.name: r for r in run.poll(1)[0].rows}
    for t in range(2, 70, 3):
        current = {r.name: r for r in run.poll(t)[0].rows}
        for name, row in current.items():
            if name != "Best effort":  # the 32-bit class wraps at 75 s, later than this loop
                assert row.octets >= previous[name].octets and row.packets >= previous[name].packets
        previous = current


def test_going_back_in_time_recomputes_the_same_values():
    clock = FakeClock(0.0)
    client = SimulatedQosClient(clock)
    dev = device()
    clock.now = 90.0
    late = asyncio.run(client.poll(dev))
    clock.now = 30.0
    asyncio.run(client.poll(dev))
    clock.now = 90.0
    again = asyncio.run(client.poll(dev))
    assert counters(late) == counters(again)


# ------------------------------------------------------------------ no QoS, no data
@pytest.mark.parametrize("dev", [
    Device("plain", "192.0.2.9"),
    Device("empty", "192.0.2.9", sim=SimProfile(qos=SimQosProfile())),
])
def test_a_device_without_a_qos_policy_is_unsupported_not_faked(dev):
    client = SimulatedQosClient(FakeClock(0.0))
    with pytest.raises(SnmpError) as caught:
        asyncio.run(client.poll(dev))
    assert caught.value.category == "unsupported"
