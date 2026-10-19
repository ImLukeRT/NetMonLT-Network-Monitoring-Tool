"""Rate maths and table parsing for SNMP interface statistics (no SNMP library, no network)."""

import pytest

from netmonlt.ifstats import (
    MAX_INTERFACES,
    InterfaceReading,
    InterfaceSnapshot,
    advance_device,
    advance_interface,
    build_snapshot,
    counter_delta,
    mark_absent,
)

GAP = 100.0
MB = 1_000_000


def reading(index=1, name="eth0", oper="up", **counters):
    return InterfaceReading(if_index=index, name=name, oper_status=oper, **counters)


def first(r, ts=0.0):
    return advance_interface(None, r, ts, rebooted=False, max_gap_s=GAP)


def nxt(prev, r, ts, rebooted=False, gap=GAP):
    return advance_interface(prev, r, ts, rebooted=rebooted, max_gap_s=gap)


# ------------------------------------------------------------------ counter_delta
@pytest.mark.parametrize("prev, cur, bits, delta, event", [
    (100, 150, 64, 50, None),
    (0, 0, 64, 0, None),
    (7, 7, 32, 0, None),
    (2**64 - 100, 50, 64, 150, "wrap"),  # 64-bit rollover
    (2**32 - 10, 5, 32, 15, "wrap"),  # 32-bit rollover
    (3_000_000_000, 10, 32, 10 + 2**32 - 3_000_000_000, "wrap"),
    (5_000, 100, 64, None, "reset"),  # went down from a low value: a reset, not a rollover
    (1_000_000, 10, 32, None, "reset"),
    (None, 5, 64, None, None),
    (5, None, 64, None, None),
    (None, None, 32, None, None),
    (-1, 5, 64, None, "invalid"),
    (2**64, 1, 64, None, "invalid"),
    (5, 2**32, 32, None, "invalid"),
])
def test_counter_delta(prev, cur, bits, delta, event):
    assert counter_delta(prev, cur, bits) == (delta, event)


# ------------------------------------------------------------------ rates
def test_first_sample_has_no_rate_but_keeps_the_counters():
    state = first(reading(in_octets=5 * MB, out_octets=MB, in_errors=3), ts=100.0)
    assert state.rate_note == "first_sample"
    assert (state.in_bps, state.out_bps, state.interval_s) == (None, None, None)
    assert state.in_errors_delta is None
    assert (state.in_octets, state.out_octets, state.in_errors) == (5 * MB, MB, 3)  # for next time
    assert state.present and state.ts == 100.0


def test_rate_is_counter_difference_over_elapsed_time_in_bits_per_second():
    a = first(reading(in_octets=1_000_000, out_octets=0, in_errors=2, out_discards=10), ts=0.0)
    b = nxt(a, reading(in_octets=13_500_000, out_octets=2_500_000, in_errors=5,
                       out_discards=10), 10.0)
    assert b.in_bps == 10_000_000.0  # 12.5 MB in 10 s = 10 Mbit/s exactly
    assert b.out_bps == 2_000_000.0
    assert b.interval_s == 10.0 and b.rate_note is None
    assert (b.in_errors_delta, b.out_discards_delta) == (3, 0)


def test_zero_traffic_is_a_real_zero_not_missing():
    a = first(reading(in_octets=500, out_octets=500), ts=0.0)
    b = nxt(a, reading(in_octets=500, out_octets=500), 5.0)
    assert b.in_bps == 0.0 and b.in_bps is not None
    assert b.out_bps == 0.0 and b.rate_note is None


def test_octet_counter_rollover_keeps_the_rate_continuous():
    a = first(reading(in_octets=2**64 - 2_500_000, out_octets=0), ts=0.0)
    b = nxt(a, reading(in_octets=10_000_000, out_octets=0), 10.0)  # wrapped past 2**64
    assert b.in_bps == 10_000_000.0  # (10 MB + 2.5 MB) over 10 s
    assert b.rate_note is None


def test_error_counter_rollover_uses_its_32_bit_range():
    a = first(reading(in_octets=0, out_octets=0, in_errors=2**32 - 5), ts=0.0)
    b = nxt(a, reading(in_octets=0, out_octets=0, in_errors=10), 10.0)
    assert b.in_errors_delta == 15


def test_octet_counter_reset_gives_no_rate_then_recovers_from_the_new_baseline():
    a = first(reading(in_octets=9 * 10**12, out_octets=7 * 10**12), ts=0.0)
    b = nxt(a, reading(in_octets=1_000, out_octets=2_000), 10.0)  # counters were cleared
    assert (b.in_bps, b.out_bps) == (None, None) and b.rate_note == "counter_reset"
    assert (b.in_octets, b.out_octets) == (1_000, 2_000)  # new baseline is kept
    c = nxt(b, reading(in_octets=1_251_000, out_octets=2_000), 20.0)
    assert c.in_bps == 1_000_000.0 and c.out_bps == 0.0 and c.rate_note is None


def test_an_error_counter_reset_alone_does_not_hide_the_traffic_rate():
    a = first(reading(in_octets=0, out_octets=0, in_errors=500), ts=0.0)
    b = nxt(a, reading(in_octets=1_250_000, out_octets=0, in_errors=3), 10.0)
    assert b.in_bps == 1_000_000.0 and b.rate_note is None
    assert b.in_errors_delta is None  # unknown, not negative and not guessed


def test_device_reboot_discards_all_counters_even_if_they_happen_to_look_higher():
    a = first(reading(in_octets=100, out_octets=100), ts=0.0)
    b = nxt(a, reading(in_octets=9_999_999, out_octets=9_999_999), 10.0, rebooted=True)
    assert (b.in_bps, b.out_bps) == (None, None) and b.rate_note == "counter_reset"


def test_gap_limit_is_inclusive_and_longer_gaps_have_no_rate():
    a = first(reading(in_octets=0, out_octets=0), ts=0.0)
    at_limit = nxt(a, reading(in_octets=12_500_000, out_octets=0), GAP)
    assert at_limit.in_bps == 12_500_000 * 8 / GAP
    too_long = nxt(a, reading(in_octets=12_500_000, out_octets=0), GAP + 0.1)
    assert too_long.in_bps is None and too_long.rate_note == "gap"


@pytest.mark.parametrize("ts", [0.0, -5.0])
def test_a_clock_that_did_not_move_forward_gives_no_rate(ts):
    a = first(reading(in_octets=0, out_octets=0), ts=0.0)
    b = nxt(a, reading(in_octets=100, out_octets=100), ts)
    assert b.in_bps is None and b.rate_note == "clock_step"


def test_missing_high_capacity_counters_are_not_invented():
    a = first(reading(in_octets=None, out_octets=None, in_errors=1), ts=0.0)
    b = nxt(a, reading(in_octets=None, out_octets=None, in_errors=4), 10.0)
    assert (b.in_bps, b.out_bps) == (None, None) and b.rate_note == "no_hc_counters"
    assert b.in_errors_delta == 3  # the counters that do exist are still used


def test_one_direction_unsupported_still_reports_the_other():
    a = first(reading(in_octets=0, out_octets=None), ts=0.0)
    b = nxt(a, reading(in_octets=1_250_000, out_octets=None), 10.0)
    assert b.in_bps == 1_000_000.0 and b.out_bps is None and b.rate_note == "no_hc_counters"


def test_a_renamed_interface_on_the_same_index_starts_again():
    a = first(reading(name="eth0", in_octets=0, out_octets=0), ts=0.0)
    b = nxt(a, reading(name="wlan0", in_octets=5, out_octets=5), 10.0)
    assert b.rate_note == "first_sample" and b.in_bps is None


# ------------------------------------------------------------------ whole devices
def rd(index=1, name="eth0", i=0, o=0):
    """A reading with just in/out octet counters."""
    return reading(index, name, in_octets=i, out_octets=o)


def poll(prev, ts, *readings, uptime=None, prev_uptime=None):
    """Apply one poll to the previous states ({if_index: state}); returns {if_index: state}."""
    snap = InterfaceSnapshot(readings=tuple(readings), uptime_ticks=uptime)
    return {s.if_index: s for s in advance_device(prev, prev_uptime, snap, ts, GAP)}


def test_device_poll_advances_every_interface_and_sorts_by_index():
    a = poll({}, 0.0, rd(2, "b"), rd(1, "a"))
    b = advance_device(a, None, InterfaceSnapshot((rd(2, "b", 1_250_000), rd(1, "a"))), 10.0, GAP)
    assert [s.if_index for s in b] == [1, 2]
    assert [s.in_bps for s in b] == [0.0, 1_000_000.0]


def test_uptime_going_backwards_means_a_restart_for_every_interface():
    a = poll({}, 0.0, rd(1, i=100, o=100), rd(2, "b", 100, 100), uptime=500_000)
    b = poll(a, 10.0, rd(1, i=900, o=900), rd(2, "b", 900, 900), uptime=1_200, prev_uptime=500_000)
    assert all(s.in_bps is None and s.rate_note == "counter_reset" for s in b.values())


def test_uptime_going_forwards_or_unknown_is_not_a_restart():
    a = poll({}, 0.0, rd(), uptime=100)
    fine = poll(a, 10.0, rd(i=1_250_000), uptime=1_100, prev_uptime=100)
    assert fine[1].in_bps == 1_000_000.0
    unknown = poll(a, 10.0, rd(i=1_250_000), uptime=5, prev_uptime=None)
    assert unknown[1].in_bps == 1_000_000.0  # can't tell, so don't discard a good measurement


def test_a_vanished_interface_is_marked_absent_with_no_invented_figures():
    a = poll({}, 0.0, rd(1, "eth0", 10, 10), rd(2, "usb0", 10, 10))
    b = poll(a, 10.0, rd(1, "eth0", 20, 20))
    gone = b[2]
    assert (gone.present, gone.oper_status, gone.rate_note) == (False, "absent", "absent")
    assert gone.ts == 0.0  # when it was last seen, not now
    assert (gone.in_bps, gone.out_bps, gone.in_octets, gone.interval_s) == (None, None, None, None)
    assert gone.name == "usb0"
    again = poll(b, 20.0, rd(1, "eth0", 30, 30))
    assert again[2] == gone  # stays absent, unchanged


def test_an_interface_that_comes_back_has_no_rate_across_its_absence():
    a = poll({}, 0.0, rd(2, "usb0", 100, 100))
    gone = poll(a, 10.0, rd(1, "eth0"))
    back = poll(gone, 20.0, rd(1, "eth0", 1, 1), rd(2, "usb0", 5_000_000, 5_000_000))
    assert back[2].present and back[2].rate_note == "first_sample" and back[2].in_bps is None


def test_a_new_interface_starts_with_a_first_sample():
    a = poll({}, 0.0, rd(1))
    b = poll(a, 10.0, rd(1), rd(9, "new0", 7, 7))
    assert b[9].rate_note == "first_sample" and b[1].rate_note is None


def test_mark_absent_is_idempotent():
    state = first(reading(in_octets=1, out_octets=1))
    once = mark_absent(state)
    assert mark_absent(once) is once


# ------------------------------------------------------------------ parsing the tables
FULL = {
    "ifName": {1: b"lo", 2: "Gi0/1"},
    "ifOperStatus": {1: 1, 2: 2},
    "ifHCInOctets": {1: 10, 2: 20},
    "ifHCOutOctets": {1: 30, 2: 40},
    "ifInErrors": {1: 1, 2: 2},
    "ifOutErrors": {1: 3, 2: 4},
    "ifInDiscards": {1: 5, 2: 6},
    "ifOutDiscards": {1: 7, 2: 8},
}


def test_build_snapshot_merges_the_columns():
    snap = build_snapshot(FULL, uptime_ticks=12345)
    assert snap.uptime_ticks == 12345 and snap.truncated == 0
    lo, gi = snap.readings
    assert (lo.if_index, lo.name, lo.oper_status) == (1, "lo", "up")
    assert (lo.in_octets, lo.out_octets, lo.in_errors, lo.out_errors) == (10, 30, 1, 3)
    assert (lo.in_discards, lo.out_discards) == (5, 7)
    assert (gi.name, gi.oper_status, gi.in_octets) == ("Gi0/1", "down", 20)


def test_name_falls_back_to_ifdescr_then_to_the_index():
    tables = {"ifOperStatus": {1: 1, 2: 1, 3: 1}, "ifName": {1: "", 2: "  "},
              "ifDescr": {1: "Ethernet 1", 2: b"\xff\xfebad"}}
    names = [r.name for r in build_snapshot(tables).readings]
    assert names[0] == "Ethernet 1" and names[1].endswith("bad") and names[2] == "if3"


def test_names_are_cleaned_and_limited():
    tables = {"ifOperStatus": {1: 1}, "ifName": {1: "ev\x00il\n\x1b[31mname" + "x" * 100}}
    (r,) = build_snapshot(tables).readings
    assert "\x00" not in r.name and "\n" not in r.name and "\x1b" not in r.name
    assert len(r.name) <= 64


@pytest.mark.parametrize("raw, expected", [
    (1, "up"), (2, "down"), (3, "testing"), (4, "unknown"), (5, "dormant"), (6, "notPresent"),
    (7, "lowerLayerDown"), (99, "unknown"), (0, "unknown"), ("up", "unknown"), (None, "unknown"),
])
def test_oper_status_mapping(raw, expected):
    (r,) = build_snapshot({"ifOperStatus": {1: raw}}).readings
    assert r.oper_status == expected


def test_missing_columns_leave_fields_empty_rather_than_guessing():
    (r,) = build_snapshot({"ifOperStatus": {1: 1}, "ifName": {1: "eth0"}}).readings
    assert (r.in_octets, r.out_octets, r.in_errors, r.out_errors) == (None, None, None, None)
    assert (r.in_discards, r.out_discards) == (None, None)


@pytest.mark.parametrize("bad", [None, "noSuchInstance", -1, 2**64, True, 1.5, b"12"])
def test_invalid_counter_values_become_none(bad):
    (r,) = build_snapshot({"ifOperStatus": {1: 1}, "ifHCInOctets": {1: bad}}).readings
    assert r.in_octets is None


def test_32_bit_counters_reject_values_outside_32_bits():
    (r,) = build_snapshot({"ifOperStatus": {1: 1}, "ifInErrors": {1: 2**32}}).readings
    assert r.in_errors is None


def test_interfaces_not_in_the_status_table_are_ignored():
    tables = {"ifOperStatus": {1: 1}, "ifHCInOctets": {1: 5, 2: 6}, "ifName": {2: "ghost"}}
    snap = build_snapshot(tables)
    assert [r.if_index for r in snap.readings] == [1]


def test_an_empty_status_table_gives_no_interfaces():
    assert build_snapshot({}).readings == ()


def test_the_interface_cap_is_reported_not_silent():
    tables = {"ifOperStatus": {i: 1 for i in range(1, MAX_INTERFACES + 7)}}
    snap = build_snapshot(tables)
    assert len(snap.readings) == MAX_INTERFACES and snap.truncated == 6
    assert snap.readings[0].if_index == 1  # the lowest indexes are kept
