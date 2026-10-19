"""Interface statistics: turning IF-MIB / IF-X-MIB readings into rates, without inventing data.

This module is pure (no network, no SNMP library), so every rule below is unit-tested.

SNMP interface counters only ever go up (until they wrap or the device restarts), so a *rate*
needs two samples: rate = (counter now - counter before) / seconds between them. The rules:

  * the first sample of an interface has no rate (there is nothing to subtract from);
  * a counter that goes down is either a rollover (it was near the top of its range, so the
    difference is taken across the wrap) or a reset (it was not, e.g. a device restart or
    "clear counters"), and a reset has no rate for that sample;
  * if sysUpTime goes backwards the device restarted, so none of its counters are trusted;
  * if too long has passed since the previous sample, or the clock stepped backwards, there is
    no rate (an average over a long gap is not "current traffic");
  * counters a device does not provide (for example no 64-bit ifHCInOctets) stay empty;
  * an interface that stops being reported is marked absent, not given made-up values.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

# ifOperStatus values (IF-MIB). Anything else a device sends is reported as "unknown".
OPER_STATUS = {
    1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent",
    7: "lowerLayerDown",
}
ABSENT = "absent"  # an interface that was reported before and is not any more

OCTET_BITS = 64  # ifHCInOctets / ifHCOutOctets are Counter64
ERROR_BITS = 32  # ifInErrors, ifOutErrors, ifInDiscards, ifOutDiscards are Counter32
MAX_INTERFACES = 64  # per device; more are ignored (and counted in `truncated`)

# Why an interface has no rate right now (shown to the user, so these are stable codes).
NOTE_FIRST_SAMPLE = "first_sample"
NOTE_GAP = "gap"
NOTE_CLOCK_STEP = "clock_step"
NOTE_NO_HC_COUNTERS = "no_hc_counters"
NOTE_COUNTER_RESET = "counter_reset"
NOTE_ABSENT = "absent"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class InterfaceReading:
    """One interface as reported by a device in a single poll (raw cumulative counters)."""

    if_index: int
    name: str
    oper_status: str
    in_octets: int | None = None
    out_octets: int | None = None
    in_errors: int | None = None
    out_errors: int | None = None
    in_discards: int | None = None
    out_discards: int | None = None


@dataclass(frozen=True)
class InterfaceSnapshot:
    """Everything one successful poll of a device returned."""

    readings: tuple[InterfaceReading, ...]
    uptime_ticks: int | None = None  # sysUpTime in 1/100 s, or None if the device didn't say
    truncated: int = 0  # interfaces ignored because of MAX_INTERFACES


@dataclass(frozen=True)
class InterfaceState:
    """The latest known state of one interface: raw counters (to diff against next time) and
    what could be derived from them. Rates and deltas are None when they can't be known."""

    if_index: int
    name: str
    oper_status: str
    present: bool
    ts: float  # when this interface was last reported, unix epoch seconds
    in_octets: int | None = None
    out_octets: int | None = None
    in_errors: int | None = None
    out_errors: int | None = None
    in_discards: int | None = None
    out_discards: int | None = None
    interval_s: float | None = None  # the time span the figures below cover
    in_bps: float | None = None  # bits per second
    out_bps: float | None = None
    in_errors_delta: int | None = None  # increase over interval_s
    out_errors_delta: int | None = None
    in_discards_delta: int | None = None
    out_discards_delta: int | None = None
    rate_note: str | None = None


STATUS_OK = "ok"  # SnmpStatus.status when the last poll worked; otherwise an SnmpError category


@dataclass(frozen=True)
class SnmpStatus:
    """How a device's SNMP polling is going (kept even when interface data can't be fetched)."""

    ts: float  # the last poll attempt
    status: str  # STATUS_OK or an SnmpError category ("timeout", "auth_failure", ...)
    message: str | None = None  # a fixed, safe description of a failure
    last_ok_ts: float | None = None  # the last successful poll
    uptime_ticks: int | None = None  # sysUpTime at the last successful poll
    truncated: int = 0  # interfaces ignored by the per-device cap at the last successful poll


def counter_delta(prev: int | None, cur: int | None, bits: int) -> tuple[int | None, str | None]:
    """The increase of a cumulative counter between two samples, as (delta, event).

    event is None for an ordinary increase, "wrap" if the counter rolled over its `bits`-wide
    range, "reset" if it went down without having been near the top of its range (so the old
    value can't be compared), or "invalid" if a value is outside the counter's range. The delta
    is None whenever it can't be known.
    """
    if prev is None or cur is None:
        return None, None
    limit = 1 << bits
    if not (0 <= prev < limit and 0 <= cur < limit):
        return None, "invalid"
    if cur >= prev:
        return cur - prev, None
    if prev >= limit >> 1:  # it was in the top half of its range, so this is a rollover
        return cur + limit - prev, "wrap"
    return None, "reset"


def advance_interface(
    prev: InterfaceState | None,
    reading: InterfaceReading,
    ts: float,
    *,
    rebooted: bool,
    max_gap_s: float,
) -> InterfaceState:
    """Combine the previous state with a new reading to get the new state (with rates if known)."""
    state = InterfaceState(
        if_index=reading.if_index, name=reading.name, oper_status=reading.oper_status,
        present=True, ts=ts, in_octets=reading.in_octets, out_octets=reading.out_octets,
        in_errors=reading.in_errors, out_errors=reading.out_errors,
        in_discards=reading.in_discards, out_discards=reading.out_discards,
    )
    if prev is None or not prev.present or prev.name != reading.name:
        return replace(state, rate_note=NOTE_FIRST_SAMPLE)  # nothing trustworthy to diff against
    elapsed = ts - prev.ts
    if elapsed <= 0:
        return replace(state, rate_note=NOTE_CLOCK_STEP)
    if elapsed > max_gap_s:
        return replace(state, rate_note=NOTE_GAP)
    if rebooted:
        return replace(state, rate_note=NOTE_COUNTER_RESET)

    in_oct, in_event = counter_delta(prev.in_octets, reading.in_octets, OCTET_BITS)
    out_oct, out_event = counter_delta(prev.out_octets, reading.out_octets, OCTET_BITS)
    if reading.in_octets is None or reading.out_octets is None:
        note = NOTE_NO_HC_COUNTERS
    elif "reset" in (in_event, out_event) or "invalid" in (in_event, out_event):
        note = NOTE_COUNTER_RESET
    else:
        note = None

    def delta(old: int | None, new: int | None) -> int | None:
        return counter_delta(old, new, ERROR_BITS)[0]

    return replace(
        state,
        interval_s=elapsed,
        in_bps=None if in_oct is None else in_oct * 8 / elapsed,
        out_bps=None if out_oct is None else out_oct * 8 / elapsed,
        in_errors_delta=delta(prev.in_errors, reading.in_errors),
        out_errors_delta=delta(prev.out_errors, reading.out_errors),
        in_discards_delta=delta(prev.in_discards, reading.in_discards),
        out_discards_delta=delta(prev.out_discards, reading.out_discards),
        rate_note=note,
    )


def advance_device(
    prev_states: Mapping[int, InterfaceState],
    prev_uptime_ticks: int | None,
    snapshot: InterfaceSnapshot,
    ts: float,
    max_gap_s: float,
) -> list[InterfaceState]:
    """Apply one successful poll to a device's stored interface states.

    Returns the new state of every interface that is reported now, plus an "absent" state for
    each previously known interface that is no longer reported.
    """
    rebooted = (
        prev_uptime_ticks is not None
        and snapshot.uptime_ticks is not None
        and snapshot.uptime_ticks < prev_uptime_ticks
    )
    states = [
        advance_interface(
            prev_states.get(r.if_index), r, ts, rebooted=rebooted, max_gap_s=max_gap_s
        )
        for r in snapshot.readings
    ]
    reported = {r.if_index for r in snapshot.readings}
    for if_index, prev in prev_states.items():
        if if_index not in reported:
            states.append(mark_absent(prev))
    return sorted(states, key=lambda s: s.if_index)


def mark_absent(prev: InterfaceState) -> InterfaceState:
    """The state of an interface that is no longer reported: no figures, last-seen time kept."""
    if not prev.present:
        return prev
    return InterfaceState(
        if_index=prev.if_index, name=prev.name, oper_status=ABSENT, present=False, ts=prev.ts,
        rate_note=NOTE_ABSENT,
    )


# ---------------------------------------------------------------- parsing IF-MIB / IF-X-MIB tables
def _clean_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    return _CONTROL_CHARS.sub("", str(value)).strip()[:64]


def _counter(value: Any, bits: int) -> int | None:
    """A counter value as an int, or None if it isn't a valid unsigned counter of that size."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < (1 << bits) else None


def _oper_status(value: Any) -> str:
    return OPER_STATUS.get(value, "unknown") if isinstance(value, int) else "unknown"


def build_snapshot(
    tables: Mapping[str, Mapping[int, Any]], uptime_ticks: int | None = None
) -> InterfaceSnapshot:
    """Merge the per-column tables of a walk (each {ifIndex: value}) into interface readings.

    Expected columns: ifOperStatus (an interface is only listed if it has one), ifName (or ifDescr
    as a fallback), ifHCInOctets, ifHCOutOctets, ifInErrors, ifOutErrors, ifInDiscards and
    ifOutDiscards. Any missing column leaves those fields empty rather than guessing.
    """
    names, descrs = tables.get("ifName", {}), tables.get("ifDescr", {})
    oper = tables.get("ifOperStatus", {})
    readings = []
    for index in sorted(oper):
        name = _clean_text(names.get(index, "")) or _clean_text(descrs.get(index, ""))
        status = oper[index]
        readings.append(InterfaceReading(
            if_index=index,
            name=name or f"if{index}",
            oper_status=_oper_status(status),
            in_octets=_counter(tables.get("ifHCInOctets", {}).get(index), OCTET_BITS),
            out_octets=_counter(tables.get("ifHCOutOctets", {}).get(index), OCTET_BITS),
            in_errors=_counter(tables.get("ifInErrors", {}).get(index), ERROR_BITS),
            out_errors=_counter(tables.get("ifOutErrors", {}).get(index), ERROR_BITS),
            in_discards=_counter(tables.get("ifInDiscards", {}).get(index), ERROR_BITS),
            out_discards=_counter(tables.get("ifOutDiscards", {}).get(index), ERROR_BITS),
        ))
    kept = readings[:MAX_INTERFACES]
    return InterfaceSnapshot(
        readings=tuple(kept), uptime_ticks=uptime_ticks, truncated=len(readings) - len(kept)
    )
