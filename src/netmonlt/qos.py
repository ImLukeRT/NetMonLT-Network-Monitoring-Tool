"""QoS telemetry: device-reported queue / traffic-class / drop counters, turned into rates without
inventing data. Pure functions (no network, no SNMP library), so every rule is unit-tested.

WHAT THIS IS, AND WHAT IT IS NOT
  * Genuine QoS counters are packets, octets and drops that a device itself counts for a
    policy element: a traffic class, a count action or a queue's dropper. They come from a
    documented MIB through an explicit adapter (qos_adapters.py), or from the simulator.
  * They are NOT the generic interface discards and errors (IF-MIB, shown in the interface
    panel): those count everything dropped on a port and say nothing about which class or queue.
  * They are NOT DSCP markings observed in traffic. Nothing here looks at packets, and a DSCP
    value is never inferred from anything, least of all from ICMP latency.
  * They are NOT the ICMP quality grades (latency, jitter, loss of ping): those measure how a
    ping fared end to end. QoS counters say what the device did with traffic inside itself.
  * Counters existing does not prove that QoS is enforced, correct or effective. They show what
    one device counted, and nothing about the rest of the path.

Rate rules (shared in spirit with ifstats.py): a rate needs two consecutive valid samples; the
first sample, a counter reset (a counter that went down without having been near the top of its
range), a device restart (sysUpTime went backwards), a counter discontinuity the device itself
reports, a long gap or a clock step all give NO rate for that sample, never a made-up one.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from netmonlt.domain import Device
from netmonlt.ifstats import (
    NOTE_ABSENT,
    NOTE_CLOCK_STEP,
    NOTE_COUNTER_RESET,
    NOTE_FIRST_SAMPLE,
    NOTE_GAP,
    counter_delta,
)

KIND_TRAFFIC = "traffic"  # packets and octets counted for a class or policy element
KIND_DROPS = "drops"  # packets and octets dropped by a queue or dropper
KINDS = (KIND_TRAFFIC, KIND_DROPS)

MAX_QOS_ROWS = 64  # per device; more are ignored (and counted in `truncated`)
NOTE_DISCONTINUITY = "discontinuity"  # the device says its counters were discontinuous
NOTE_NO_COUNTERS = "no_counters"  # the row carried no usable counter at all

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

DEFINITIONS = {
    "qos_counters": (
        "Packets, octets and drops that the DEVICE counts for a QoS policy element (a traffic "
        "class, a count action or a queue's dropper), read read-only over SNMPv3 through a "
        "documented adapter. The source MIB, adapter and counter width are shown per device."
    ),
    "not_interface_discards": (
        "Different from the generic interface discards and errors in the interface panel, which "
        "count everything dropped on a port and cannot say which class or queue."
    ),
    "not_dscp": (
        "Different from DSCP markings observed in traffic: NetMonLT never captures or inspects "
        "packets and never infers a DSCP value."
    ),
    "not_icmp_quality": (
        "Different from the ICMP network-quality grades (latency, jitter, loss), which describe "
        "how ping behaved end to end. QoS counters describe what one device did with traffic."
    ),
    "not_proof": (
        "Counters existing does not prove that QoS is enforced, correct or effective: they show "
        "what one device counted, not what happened elsewhere on the path."
    ),
}


@dataclass(frozen=True)
class QosRow:
    """One policy element in one poll, with its raw cumulative counters."""

    key: str  # stable within a device, e.g. "count:3" or "drop:2"
    kind: str  # KIND_TRAFFIC or KIND_DROPS
    name: str
    detail: str | None = None
    packets: int | None = None
    octets: int | None = None
    drop_packets: int | None = None
    drop_octets: int | None = None
    counter_bits: int = 64  # 32 or 64
    # A timestamp (sysUpTime ticks) the device gives for the last discontinuity of these counters.
    discontinuity: int | None = None


@dataclass(frozen=True)
class QosSnapshot:
    """Everything one successful poll of a device returned."""

    adapter: str  # the adapter that read it ("simulated" for the simulator)
    source_mib: str  # human-readable source, e.g. "DIFFSERV-MIB (RFC 3289)"
    rows: tuple[QosRow, ...]
    uptime_ticks: int | None = None
    truncated: int = 0


@dataclass(frozen=True)
class QosState:
    """The latest known state of one element: raw counters (to diff against next time) and what
    could be derived. Rates are None when they can't be known."""

    key: str
    kind: str
    name: str
    detail: str | None
    present: bool
    ts: float  # when this element was last reported
    counter_bits: int = 64
    discontinuity: int | None = None
    packets: int | None = None
    octets: int | None = None
    drop_packets: int | None = None
    drop_octets: int | None = None
    interval_s: float | None = None
    pps: float | None = None  # packets per second
    bps: float | None = None  # bits per second
    drop_pps: float | None = None
    drop_bps: float | None = None
    drop_pct: float | None = None  # drops as a percentage of packets, only if both are counted
    rate_note: str | None = None


@dataclass(frozen=True)
class QosStatus:
    """How a device's QoS polling is going (kept even when no counters can be fetched)."""

    ts: float
    status: str  # "ok" or a failure category
    message: str | None = None
    last_ok_ts: float | None = None
    uptime_ticks: int | None = None
    adapter: str | None = None
    source_mib: str | None = None
    truncated: int = 0


class QosClient(Protocol):
    async def poll(self, device: Device) -> QosSnapshot: ...


# ------------------------------------------------------------------ rates
def _clean_text(value: Any, limit: int = 64) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    return _CONTROL_CHARS.sub("", str(value)).strip()[:limit]


def clean_label(value: Any, fallback: str) -> str:
    """A display label safe to store and show (no control characters, bounded length)."""
    return fallback if value is None else (_clean_text(value) or fallback)


def advance_row(
    prev: QosState | None, row: QosRow, ts: float, *, rebooted: bool, max_gap_s: float
) -> QosState:
    """Combine the previous state with a new reading to get the new state (with rates if known)."""
    state = QosState(
        key=row.key, kind=row.kind, name=row.name, detail=row.detail, present=True, ts=ts,
        counter_bits=row.counter_bits, discontinuity=row.discontinuity, packets=row.packets,
        octets=row.octets, drop_packets=row.drop_packets, drop_octets=row.drop_octets,
    )
    counters = (row.packets, row.octets, row.drop_packets, row.drop_octets)
    if all(c is None for c in counters):
        return replace(state, rate_note=NOTE_NO_COUNTERS)
    if prev is None or not prev.present or prev.kind != row.kind:
        return replace(state, rate_note=NOTE_FIRST_SAMPLE)
    elapsed = ts - prev.ts
    if elapsed <= 0:
        return replace(state, rate_note=NOTE_CLOCK_STEP)
    if elapsed > max_gap_s:
        return replace(state, rate_note=NOTE_GAP)
    if rebooted or prev.counter_bits != row.counter_bits:
        return replace(state, rate_note=NOTE_COUNTER_RESET)
    if (row.discontinuity is not None and prev.discontinuity is not None
            and row.discontinuity != prev.discontinuity):
        return replace(state, rate_note=NOTE_DISCONTINUITY)

    bits = row.counter_bits
    pkts, pkts_event = counter_delta(prev.packets, row.packets, bits)
    octs, octs_event = counter_delta(prev.octets, row.octets, bits)
    dpkts, dpkts_event = counter_delta(prev.drop_packets, row.drop_packets, bits)
    docts, docts_event = counter_delta(prev.drop_octets, row.drop_octets, bits)
    if {"reset", "invalid"} & {pkts_event, octs_event, dpkts_event, docts_event}:
        return replace(state, rate_note=NOTE_COUNTER_RESET)

    def per_second(delta: int | None, factor: int = 1) -> float | None:
        return None if delta is None else delta * factor / elapsed

    drop_pct = None
    if pkts is not None and dpkts is not None:
        drop_pct = 100.0 * dpkts / pkts if pkts > 0 else (0.0 if dpkts == 0 else None)
    return replace(
        state, interval_s=elapsed, pps=per_second(pkts), bps=per_second(octs, 8),
        drop_pps=per_second(dpkts), drop_bps=per_second(docts, 8),
        drop_pct=None if drop_pct is None else min(100.0, drop_pct),
    )


def mark_absent(prev: QosState) -> QosState:
    """The state of an element that is no longer reported: no figures, last-seen time kept."""
    if not prev.present:
        return prev
    return QosState(
        key=prev.key, kind=prev.kind, name=prev.name, detail=prev.detail, present=False,
        ts=prev.ts, counter_bits=prev.counter_bits, rate_note=NOTE_ABSENT,
    )


def advance_device(
    prev_states: Mapping[str, QosState], prev_uptime_ticks: int | None, snapshot: QosSnapshot,
    ts: float, max_gap_s: float,
) -> list[QosState]:
    """Apply one successful poll to a device's stored QoS states (plus "absent" states for
    elements that were reported before and are not any more)."""
    rebooted = (
        prev_uptime_ticks is not None and snapshot.uptime_ticks is not None
        and snapshot.uptime_ticks < prev_uptime_ticks
    )
    states = [
        advance_row(prev_states.get(r.key), r, ts, rebooted=rebooted, max_gap_s=max_gap_s)
        for r in snapshot.rows
    ]
    reported = {r.key for r in snapshot.rows}
    states.extend(mark_absent(p) for k, p in prev_states.items() if k not in reported)
    return sorted(states, key=lambda s: (s.kind, s.key))


# ------------------------------------------------------------------ history for the charts
@dataclass(frozen=True)
class QosSample:
    """One stored observation of one element: its derived rates (None when not derivable)."""

    ts: float
    key: str
    name: str
    pps: float | None
    bps: float | None
    drop_pps: float | None
    drop_bps: float | None
    rate_note: str | None


def history_series(
    samples: Sequence[QosSample], *, start: float, end: float, first_ts: float | None,
    poll_interval_s: float, max_points: int,
) -> tuple[dict[str, dict], float]:
    """Per-element history in equal time buckets: ({key: {"name", "points"}}, bucket_s).

    A bucket's rates are the means of the valid rates in it; `samples` counts every observation
    and `no_rate` those that had none (first sample, reset, restart...). A bucket with no valid
    rate has null rates, never zero. Buckets with no observations are omitted.
    """
    x0 = start if first_ts is None else max(start, first_ts)
    bucket_s = max((end - x0) / max_points, 2 * poll_interval_s)
    grouped: dict[str, dict[int, list[QosSample]]] = {}
    names: dict[str, str] = {}
    for sample in samples:
        names[sample.key] = sample.name
        grouped.setdefault(sample.key, {}).setdefault(
            int((sample.ts - x0) // bucket_s), []
        ).append(sample)

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    result: dict[str, dict] = {}
    for key, buckets in grouped.items():
        points = []
        for index in sorted(buckets):
            group = buckets[index]
            pps = mean([s.pps for s in group if s.pps is not None])
            bps = mean([s.bps for s in group if s.bps is not None])
            drop_pps = mean([s.drop_pps for s in group if s.drop_pps is not None])
            drop_bps = mean([s.drop_bps for s in group if s.drop_bps is not None])
            drop_pct = None
            if pps is not None and drop_pps is not None and pps > 0:
                drop_pct = min(100.0, 100.0 * drop_pps / pps)
            points.append({
                "start": x0 + index * bucket_s,
                "end": min(x0 + (index + 1) * bucket_s, end),
                "samples": len(group),
                "no_rate": sum(1 for s in group if s.rate_note is not None),
                "pps": pps, "bps": bps, "drop_pps": drop_pps, "drop_bps": drop_bps,
                "drop_pct": drop_pct,
            })
        result[key] = {"name": names[key], "points": points}
    return result, bucket_s
