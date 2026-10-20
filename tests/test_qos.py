"""QoS counter maths: rates from consecutive samples, rollover, resets, restarts, discontinuities,
gaps and missing values. Pure functions: no SNMP, no network, no device."""

import pytest

from netmonlt.qos import (
    KIND_DROPS,
    KIND_TRAFFIC,
    NOTE_DISCONTINUITY,
    NOTE_NO_COUNTERS,
    QosRow,
    QosSample,
    QosSnapshot,
    QosState,
    advance_device,
    advance_row,
    clean_label,
    history_series,
    mark_absent,
)

GAP = 100.0
M64, M32 = 2**64, 2**32


def row(key="c", pkts=0, octs=0, dpkts=None, docts=None, bits=64, kind=KIND_TRAFFIC, disc=None):
    return QosRow(key, kind, key.upper(), None, pkts, octs, dpkts, docts, bits, disc)


def step(prev, new, ts, **kw):
    return advance_row(prev, new, ts, rebooted=kw.get("rebooted", False),
                       max_gap_s=kw.get("max_gap_s", GAP))


def two(old, new, dt=10.0, **kw):
    first = step(None, old, 100.0)
    return step(first, new, 100.0 + dt, **kw)


# ------------------------------------------------------------------ rates
def test_the_first_sample_has_no_rate_and_says_why():
    s = step(None, row(pkts=1000, octs=1_000_000), 100.0)
    assert s.rate_note == "first_sample"
    assert (s.pps, s.bps, s.drop_pps, s.drop_pct, s.interval_s) == (None,) * 5
    assert s.packets == 1000 and s.octets == 1_000_000  # the raw counters are kept for next time


def test_two_samples_give_packets_and_bits_per_second():
    s = two(row(pkts=1000, octs=1_000_000), row(pkts=1500, octs=1_250_000), dt=10)
    assert s.rate_note is None and s.interval_s == 10.0
    assert s.pps == 50.0 and s.bps == 250_000 * 8 / 10


def test_drops_and_drop_percentage_come_from_the_same_interval():
    old = row(pkts=1000, octs=0, dpkts=10, docts=0)
    new = row(pkts=2000, octs=0, dpkts=210, docts=21_000)
    s = two(old, new, dt=10)
    assert s.drop_pps == 20.0 and s.drop_bps == 21_000 * 8 / 10
    assert s.drop_pct == pytest.approx(20.0)  # 200 of 1000 packets


def test_a_quiet_element_with_no_traffic_and_no_drops_is_zero_percent_not_unknown():
    s = two(row(pkts=5, octs=5, dpkts=0, docts=0), row(pkts=5, octs=5, dpkts=0, docts=0))
    assert s.pps == 0.0 and s.drop_pct == 0.0


def test_drops_without_traffic_have_no_percentage():
    s = two(row(pkts=5, octs=5, dpkts=0, docts=0), row(pkts=5, octs=5, dpkts=3, docts=300))
    assert s.drop_pps == 0.3 and s.drop_pct is None


def test_a_drops_only_element_has_no_traffic_rates_or_percentage():
    old = row("d", kind=KIND_DROPS, pkts=None, octs=None, dpkts=100, docts=10_000)
    new = row("d", kind=KIND_DROPS, pkts=None, octs=None, dpkts=150, docts=15_000)
    s = two(old, new, dt=5)
    assert s.drop_pps == 10.0 and s.drop_bps == 5000 * 8 / 5
    assert (s.pps, s.bps, s.drop_pct) == (None, None, None)  # not counted, so not invented


def test_the_percentage_never_exceeds_100():
    s = two(row(pkts=0, octs=0, dpkts=0, docts=0), row(pkts=10, octs=0, dpkts=500, docts=0))
    assert s.drop_pct == 100.0


# ------------------------------------------------------------------ rollover and resets
def test_a_64_bit_counter_that_wraps_still_gives_the_right_rate():
    s = two(row(pkts=M64 - 100, octs=M64 - 1000), row(pkts=400, octs=3000), dt=10)
    assert s.rate_note is None and s.pps == 50.0 and s.bps == 4000 * 8 / 10


def test_a_32_bit_counter_that_wraps_uses_its_own_width():
    s = two(row(pkts=M32 - 100, octs=M32 - 1000, bits=32), row(pkts=400, octs=3000, bits=32))
    assert s.rate_note is None and s.pps == 50.0 and s.bps == 3200.0


def test_the_same_values_on_a_64_bit_counter_are_a_reset_not_a_wrap():
    s = two(row(pkts=M32 - 100, octs=1, bits=64), row(pkts=400, octs=2, bits=64))
    assert s.rate_note == "counter_reset" and s.pps is None  # 2**32 is far from the 64-bit top


def test_a_counter_that_drops_from_a_small_value_is_a_reset_with_no_rate():
    s = two(row(pkts=5000, octs=9000), row(pkts=10, octs=20))
    assert s.rate_note == "counter_reset"
    assert (s.pps, s.bps, s.drop_pps) == (None, None, None)


def test_one_reset_counter_voids_the_whole_row_not_just_that_counter():
    old = row(pkts=1000, octs=1000, dpkts=500, docts=500)
    new = row(pkts=2000, octs=2000, dpkts=3, docts=600)  # only the drop counter went backwards
    s = two(old, new)
    assert s.rate_note == "counter_reset"
    assert (s.pps, s.bps, s.drop_pps, s.drop_pct) == (None, None, None, None)


@pytest.mark.parametrize("bad", [-1, M64, M64 + 5])
def test_a_value_outside_the_counters_range_is_a_reset_not_a_rate(bad):
    s = two(row(pkts=10, octs=10), row(pkts=bad, octs=20))
    assert s.rate_note == "counter_reset" and s.pps is None


def test_after_a_reset_rates_resume_from_the_new_baseline():
    reset = two(row(pkts=5000, octs=0), row(pkts=10, octs=0))
    resumed = step(reset, row(pkts=110, octs=0), 120.0)
    assert resumed.rate_note is None and resumed.pps == 10.0


def test_a_device_restart_voids_the_sample_even_if_the_counters_look_ordinary():
    s = two(row(pkts=100, octs=100), row(pkts=200, octs=200), rebooted=True)
    assert s.rate_note == "counter_reset" and s.pps is None


def test_a_change_of_counter_width_is_not_compared():
    s = two(row(pkts=100, octs=100, bits=32), row(pkts=200, octs=200, bits=64))
    assert s.rate_note == "counter_reset" and s.pps is None


# ------------------------------------------------------------------ discontinuities
def test_a_discontinuity_timestamp_that_changes_voids_the_sample():
    s = two(row(pkts=100, octs=100, disc=500), row(pkts=200, octs=200, disc=900))
    assert s.rate_note == NOTE_DISCONTINUITY and s.pps is None


def test_an_unchanged_discontinuity_timestamp_is_fine():
    s = two(row(pkts=100, octs=100, disc=500), row(pkts=200, octs=200, disc=500))
    assert s.rate_note is None and s.pps == 10.0


def test_a_missing_discontinuity_timestamp_is_not_treated_as_a_change():
    s = two(row(pkts=100, octs=100, disc=500), row(pkts=200, octs=200, disc=None))
    assert s.rate_note is None and s.pps == 10.0


# ------------------------------------------------------------------ time
def test_a_long_gap_gives_no_rate():
    s = two(row(pkts=0, octs=0), row(pkts=1000, octs=1000), dt=GAP + 1)
    assert s.rate_note == "gap" and s.pps is None


def test_a_gap_exactly_at_the_limit_still_counts():
    s = two(row(pkts=0, octs=0), row(pkts=1000, octs=1000), dt=GAP)
    assert s.rate_note is None and s.pps == 10.0


@pytest.mark.parametrize("dt", [0.0, -5.0])
def test_a_clock_that_did_not_advance_gives_no_rate(dt):
    s = two(row(pkts=0, octs=0), row(pkts=10, octs=10), dt=dt)
    assert s.rate_note == "clock_step" and s.pps is None


# ------------------------------------------------------------------ missing values and elements
def test_a_row_with_no_counters_is_reported_as_such_never_as_zero():
    s = step(None, row(pkts=None, octs=None), 100.0)
    assert s.rate_note == NOTE_NO_COUNTERS and s.packets is None
    again = step(s, row(pkts=None, octs=None), 110.0)
    assert again.rate_note == NOTE_NO_COUNTERS and again.pps is None


def test_a_counter_that_disappears_gives_no_rate_for_it_but_keeps_the_others():
    s = two(row(pkts=100, octs=100), row(pkts=200, octs=None))
    assert s.pps == 10.0 and s.bps is None


def test_a_row_that_changes_kind_starts_again():
    prev = step(None, row("x", pkts=1, octs=1), 100.0)
    s = step(prev, row("x", pkts=None, octs=None, dpkts=5, docts=5, kind=KIND_DROPS), 110.0)
    assert s.rate_note == "first_sample"


def test_an_absent_element_has_no_figures_and_keeps_its_last_seen_time():
    prev = two(row(pkts=100, octs=100), row(pkts=200, octs=200))
    gone = mark_absent(prev)
    assert gone.present is False and gone.rate_note == "absent" and gone.ts == prev.ts
    assert (gone.pps, gone.packets, gone.octets) == (None, None, None)
    assert mark_absent(gone) is gone


def test_an_element_that_comes_back_is_a_first_sample():
    prev = two(row(pkts=100, octs=100), row(pkts=200, octs=200))
    back = step(mark_absent(prev), row(pkts=300, octs=300), 130.0)
    assert back.rate_note == "first_sample" and back.present is True


# ------------------------------------------------------------------ whole devices
def snapshot(*rows, uptime=1000, adapter="a"):
    return QosSnapshot(adapter, "src", tuple(rows), uptime)


def test_a_device_poll_advances_every_element_and_marks_missing_ones_absent():
    first = advance_device({}, None, snapshot(row("a", pkts=0, octs=0), row("b", pkts=0, octs=0)),
                           100.0, GAP)
    states = {s.key: s for s in first}
    second = advance_device(states, 1000, snapshot(row("a", pkts=100, octs=0), uptime=1100),
                            110.0, GAP)
    by_key = {s.key: s for s in second}
    assert by_key["a"].pps == 10.0 and by_key["b"].present is False


def test_a_device_whose_uptime_went_backwards_has_every_rate_voided():
    states = {s.key: s for s in advance_device({}, None, snapshot(row("a", pkts=0, octs=0),
                                                                    uptime=5000), 100.0, GAP)}
    after = advance_device(states, 5000, snapshot(row("a", pkts=100, octs=0), uptime=20),
                           110.0, GAP)
    assert after[0].rate_note == "counter_reset" and after[0].pps is None


def test_an_unknown_previous_uptime_is_not_a_restart():
    states = {s.key: s for s in advance_device({}, None, snapshot(row("a", pkts=0, octs=0)),
                                                100.0, GAP)}
    after = advance_device(states, None, snapshot(row("a", pkts=100, octs=0), uptime=20),
                           110.0, GAP)
    assert after[0].pps == 10.0


def test_states_are_sorted_by_kind_then_key():
    snap = snapshot(row("z", pkts=1, octs=1), row("d", kind=KIND_DROPS, pkts=None, octs=None,
                                                   dpkts=1, docts=1), row("a", pkts=1, octs=1))
    keys = [s.key for s in advance_device({}, None, snap, 1.0, GAP)]
    assert keys == ["d", "a", "z"]  # "drops" sorts before "traffic"


def test_labels_are_cleaned():
    assert clean_label(b"Gi0/1\x00\x1b[31m", "x") == "Gi0/1[31m"
    assert clean_label("", "fallback") == "fallback" and len(clean_label("x" * 500, "y")) == 64
    assert clean_label(None, "fallback") == "fallback"  # never the text "None"


# ------------------------------------------------------------------ history series
def sample(ts, key="a", pps=10.0, bps=80.0, dpps=None, dbps=None, note=None):
    return QosSample(ts, key, key.upper(), pps, bps, dpps, dbps, note)


def test_history_buckets_average_only_valid_rates():
    samples = [sample(100, pps=10.0), sample(105, pps=20.0), sample(110, pps=None, bps=None,
                                                                     note="counter_reset")]
    series, bucket_s = history_series(samples, start=100, end=130, first_ts=100,
                                      poll_interval_s=5, max_points=10)
    first, second = series["a"]["points"]
    assert bucket_s == 10  # two polling intervals at least
    assert first["pps"] == 15.0 and first["samples"] == 2 and first["no_rate"] == 0
    assert second["pps"] is None and second["samples"] == 1 and second["no_rate"] == 1


def test_a_bucket_with_no_valid_rate_has_null_rates_not_zero():
    samples = [sample(100, pps=None, bps=None, note="first_sample"),
               sample(105, pps=None, bps=None, note="counter_reset")]
    series, _ = history_series(samples, start=100, end=130, first_ts=100, poll_interval_s=5,
                               max_points=10)
    (point,) = series["a"]["points"]
    assert point["pps"] is None and point["bps"] is None and point["drop_pct"] is None
    assert point["samples"] == 2 and point["no_rate"] == 2


def test_the_drop_percentage_of_a_bucket_is_drops_over_traffic():
    samples = [sample(100, pps=100.0, dpps=10.0), sample(105, pps=100.0, dpps=30.0)]
    series, _ = history_series(samples, start=100, end=130, first_ts=100, poll_interval_s=5,
                               max_points=10)
    assert series["a"]["points"][0]["drop_pct"] == pytest.approx(20.0)


def test_there_is_no_drop_percentage_without_traffic_figures():
    samples = [sample(100, pps=None, dpps=10.0), sample(105, pps=None, dpps=10.0)]
    series, _ = history_series(samples, start=100, end=130, first_ts=100, poll_interval_s=5,
                               max_points=10)
    assert series["a"]["points"][0]["drop_pct"] is None
    assert series["a"]["points"][0]["drop_pps"] == 10.0


def test_history_keeps_elements_apart_and_omits_empty_buckets():
    samples = [sample(100, "a"), sample(105, "b", pps=1.0), sample(500, "a")]
    series, _ = history_series(samples, start=0, end=600, first_ts=100, poll_interval_s=5,
                               max_points=100)
    assert set(series) == {"a", "b"} and series["a"]["name"] == "A"
    assert len(series["a"]["points"]) == 2 and len(series["b"]["points"]) == 1


def test_history_of_nothing_is_empty():
    assert history_series([], start=0, end=100, first_ts=None, poll_interval_s=5,
                          max_points=10)[0] == {}


def test_a_pre_existing_state_object_is_immutable():
    s = QosState("k", KIND_TRAFFIC, "n", None, True, 1.0)
    with pytest.raises(AttributeError):
        s.pps = 5.0
