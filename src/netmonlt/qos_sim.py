"""Deterministic SIMULATED QoS telemetry, so the feature demos with no QoS-capable device at all.

Nothing here comes from real equipment and it is always labelled as simulated. The model is
deliberately simple and fully scripted: several traffic classes share an egress link; a
priority class is served first, the other classes share what is left by weight, and traffic
that does not fit is dropped (and counted against its own class). During the scripted
congestion period the classes marked `surges` offer more traffic, so the lower-priority
queues drop while the priority class stays clean.

Counters are a pure function of the whole seconds elapsed: the world advances in fixed one-second
steps with integer arithmetic, so a given clock gives identical counters no matter how often it
is polled. It honours the device's scripted outage (a timeout), restart (all counters and the
uptime reset) and each class's counter width and rollover.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from netmonlt.domain import Device, SimQosClass, SimQosProfile
from netmonlt.qos import KIND_TRAFFIC, QosRow, QosSnapshot
from netmonlt.snmp import CATEGORY_TIMEOUT, CATEGORY_UNSUPPORTED, SnmpError

SIM_ADAPTER = "simulated"
SIM_SOURCE = "simulated (no real device: scripted, deterministic counters)"
_UPTIME_AT_START_S = 3 * 86400
# A device that has been counting for a while: counters do not start at zero.
_START_OCTETS = 2_500_000_000
_START_PACKETS = 3_000_000
_START_DROP_OCTETS = 15_000_000
_START_DROP_PACKETS = 12_000
_OCTETS, _PACKETS, _DROP_OCTETS, _DROP_PACKETS = range(4)


@dataclass
class _Totals:
    """Cumulative counts since second 0 for one class: offered and dropped octets and packets."""

    offered_octets: int = 0
    offered_packets: int = 0
    dropped_octets: int = 0
    dropped_packets: int = 0

    def copy(self) -> "_Totals":
        return _Totals(self.offered_octets, self.offered_packets, self.dropped_octets,
                       self.dropped_packets)


def allocate(profile: SimQosProfile, offered: list[float]) -> list[float]:
    """How much of each class's offered rate (Mbit/s) the link carries.

    Priority classes are served first, in order. The rest share what remains by weight, and a
    class that offers less than its share frees the difference for the others (weighted max-min
    fairness). Anything not carried is dropped.
    """
    served = [0.0] * len(offered)
    remaining = profile.capacity_mbps
    for i, cls in enumerate(profile.classes):
        if cls.priority:
            served[i] = min(offered[i], remaining)
            remaining -= served[i]
    active = [i for i, cls in enumerate(profile.classes) if not cls.priority]
    while active and remaining > 1e-9:
        total_weight = sum(profile.classes[i].weight for i in active)
        share = {i: remaining * profile.classes[i].weight / total_weight for i in active}
        satisfied = [i for i in active if offered[i] <= share[i]]
        if not satisfied:
            for i in active:
                served[i] = share[i]
            break
        for i in satisfied:
            served[i] = offered[i]
            remaining -= offered[i]
            active.remove(i)
    return served


def in_congestion(profile: SimQosProfile, t: float) -> bool:
    start = profile.congestion_start_s
    return start is not None and start <= t < start + profile.congestion_duration_s


def _bytes(mbps: float) -> int:
    return round(mbps * 1_000_000 / 8)  # one second of traffic at this rate


def _packets(octets: int, cls: SimQosClass) -> int:
    return round(octets / cls.avg_packet_bytes)


class _World:
    """The simulated link, advanced one whole second at a time."""

    def __init__(self, profile: SimQosProfile):
        self.profile = profile
        self.second = 0
        self.totals = [_Totals() for _ in profile.classes]

    def advance_to(self, second: int) -> None:
        while self.second < second:
            self._step(self.second + 1)

    def _step(self, second: int) -> None:
        p = self.profile
        congested = in_congestion(p, second - 0.5)
        offered = [
            cls.offered_mbps * (1 + 0.1 * math.sin(2 * math.pi * second / 29 + i))
            * (p.surge_factor if congested and cls.surges else 1.0)
            for i, cls in enumerate(p.classes)
        ]
        served = allocate(p, offered)
        for i, cls in enumerate(p.classes):
            octets = _bytes(offered[i])
            dropped = min(octets, max(0, _bytes(offered[i] - served[i])))
            totals = self.totals[i]
            totals.offered_octets += octets
            totals.offered_packets += _packets(octets, cls)
            totals.dropped_octets += dropped
            totals.dropped_packets += _packets(dropped, cls)
        self.second = second

    def snapshot(self) -> list[_Totals]:
        return [t.copy() for t in self.totals]


class _DeviceSim:
    """The world for one device plus the checkpoints that restarts and rollovers need."""

    def __init__(self, profile: SimQosProfile, reboot_at_s: float | None):
        self.profile = profile
        self.reboot_second = None if reboot_at_s is None else int(reboot_at_s)
        wanted = {int(c.wrap_at_s) for c in profile.classes if c.wrap_at_s is not None}
        if self.reboot_second is not None:
            wanted.add(self.reboot_second)
        self._checkpoints: dict[int, list[_Totals]] = {}
        probe = _World(profile)
        for second in sorted(wanted):
            probe.advance_to(second)
            self._checkpoints[second] = probe.snapshot()
        self._world = _World(profile)

    def totals_at(self, second: int) -> list[_Totals]:
        if second < self._world.second:  # the clock went backwards (tests): start again
            self._world = _World(self.profile)
        self._world.advance_to(second)
        return self._world.totals

    def counters(self, second: int, rebooted: bool) -> list[tuple[int, int, int, int]]:
        """Each class's four counters (octets, packets, drop octets, drop packets) now."""
        totals = self.totals_at(second)
        base = self._checkpoints[self.reboot_second] if rebooted else None
        result = []
        for i, cls in enumerate(self.profile.classes):
            modulus = 1 << cls.counter_bits
            now = totals[i]
            if base is not None:
                now = _Totals(
                    now.offered_octets - base[i].offered_octets,
                    now.offered_packets - base[i].offered_packets,
                    now.dropped_octets - base[i].dropped_octets,
                    now.dropped_packets - base[i].dropped_packets,
                )
                starts = (0, 0, 0, 0)  # a restarted device counts from zero
            else:
                starts = [_START_OCTETS, _START_PACKETS, _START_DROP_OCTETS, _START_DROP_PACKETS]
                if cls.wrap_at_s is not None:  # arrange to hit the top of the range at wrap_at_s
                    at_wrap = self._checkpoints[int(cls.wrap_at_s)][i]
                    starts[_OCTETS] = modulus - at_wrap.offered_octets
                    starts[_PACKETS] = modulus - at_wrap.offered_packets
            values = (now.offered_octets, now.offered_packets, now.dropped_octets,
                      now.dropped_packets)
            result.append(tuple((start + value) % modulus for start, value in zip(starts, values,
                                                                                 strict=True)))
        return result


class SimulatedQosClient:
    """Fake, deterministic QoS counters for devices with a `sim.qos` profile."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started = clock()
        self._sims: dict[str, _DeviceSim] = {}

    async def poll(self, device: Device) -> QosSnapshot:
        profile = device.sim.qos
        if profile is None or not profile.classes:
            raise SnmpError(CATEGORY_UNSUPPORTED)
        t = self._clock() - self._started
        outage = device.sim.outage_start_s
        if outage is not None and outage <= t < outage + device.sim.outage_duration_s:
            raise SnmpError(CATEGORY_TIMEOUT)  # the whole device is unreachable
        sim = self._sims.get(device.name)
        if sim is None:
            sim = self._sims[device.name] = _DeviceSim(profile, device.sim.reboot_at_s)
        reboot = device.sim.reboot_at_s
        rebooted = reboot is not None and t >= reboot
        counters = sim.counters(int(t), rebooted)
        age = t - reboot if rebooted else t
        uptime_s = age if rebooted else _UPTIME_AT_START_S + t
        rows = tuple(
            QosRow(
                key=f"class:{cls.name}", kind=KIND_TRAFFIC, name=cls.name,
                detail=f"{profile.interface} egress policy (simulated)",
                octets=values[_OCTETS], packets=values[_PACKETS],
                drop_octets=values[_DROP_OCTETS], drop_packets=values[_DROP_PACKETS],
                counter_bits=cls.counter_bits,
            )
            for cls, values in zip(profile.classes, counters, strict=True)
        )
        return QosSnapshot(
            adapter=SIM_ADAPTER, source_mib=SIM_SOURCE, rows=rows,
            uptime_ticks=int(uptime_s * 100) % (1 << 32),
        )
