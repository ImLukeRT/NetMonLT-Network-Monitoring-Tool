"""Network-quality maths: latency, jitter, packet loss, grading, and the series for the charts.

Pure functions over check rows, so every case is built by hand and the expected numbers are
worked out in the comments.
"""

import random

import pytest

from netmonlt.domain import QualityConfig, QualityThresholds
from netmonlt.quality import (
    LEVEL_UNKNOWN,
    CheckSample,
    consecutive_pairs,
    find_gaps,
    grade,
    jitter,
    mean_latency,
    packet_loss,
    series,
    summarize,
)

GAP = 25.0  # a poll interval of 5 s: 3 * 5 + 10
CFG = QualityConfig()


def up(ts, rtt=10.0, received=3):
    return CheckSample(ts, 3, received, rtt)


def down(ts):
    return CheckSample(ts, 3, 0, None)


def run(rtts, start=100.0, step=5.0):
    """Consecutive successful checks `step` seconds apart."""
    return [up(start + i * step, r) for i, r in enumerate(rtts)]


def summary(samples, cfg=CFG, *, start=0.0, end=None, first=None, last=None, gap_s=GAP):
    end = (samples[-1].ts + 1 if samples else 1000.0) if end is None else end
    return summarize(
        samples, cfg, start=start, end=end, gap_s=gap_s,
        first_check_ts=samples[0].ts if first is None and samples else first,
        last_check_ts=samples[-1].ts if last is None and samples else last,
    )


# ------------------------------------------------------------------ grading
@pytest.mark.parametrize("value, expected", [
    (0, "excellent"), (5, "excellent"), (5.001, "good"), (15, "good"), (15.5, "fair"),
    (30, "fair"), (30.1, "poor"), (10_000, "poor"),
])
def test_each_threshold_is_an_inclusive_upper_bound(value, expected):
    assert grade(value, QualityThresholds(5, 15, 30)) == expected


# ------------------------------------------------------------------ jitter
def test_jitter_is_the_mean_absolute_difference_of_consecutive_checks():
    # |12-10| + |9-12| + |9-9| = 2 + 3 + 0 over 3 pairs
    value, pairs = jitter(run([10, 12, 9, 9]), GAP)
    assert pairs == 3 and value == pytest.approx(5 / 3)


def test_a_steady_link_has_zero_jitter_which_is_not_missing():
    value, pairs = jitter(run([20, 20, 20]), GAP)
    assert (value, pairs) == (0.0, 2)


def test_jitter_does_not_depend_on_the_absolute_latency():
    base = jitter(run([10, 14, 11, 20]), GAP)[0]
    shifted = jitter(run([510, 514, 511, 520]), GAP)[0]
    assert shifted == pytest.approx(base)


def test_a_down_check_between_two_good_ones_breaks_the_pair():
    # 10, down, 14, 16: only (14, 16) are consecutive successful checks
    samples = [up(100, 10), down(105), up(110, 14), up(115, 16)]
    assert jitter(samples, GAP) == (2.0, 1)


def test_a_monitoring_gap_breaks_the_pair():
    samples = [up(100, 10), up(105, 11), up(105 + 60, 200), up(170, 201)]  # 60 s: nothing measured
    value, pairs = jitter(samples, GAP)
    assert pairs == 2 and value == pytest.approx(1.0)  # the 11 -> 200 jump is not counted


def test_a_pair_exactly_at_the_gap_limit_still_counts_and_one_beyond_does_not():
    assert jitter([up(0, 10), up(GAP, 12)], GAP) == (2.0, 1)
    assert jitter([up(0, 10), up(GAP + 0.1, 12)], GAP) == (None, 0)


def test_one_sample_or_none_gives_no_jitter():
    assert jitter([], GAP) == (None, 0)
    assert jitter([up(0, 5)], GAP) == (None, 0)


def test_checks_with_no_replies_are_never_treated_as_zero_latency():
    # if the down checks counted as 0 ms the jitter would be huge; they are skipped instead
    samples = [down(100), down(105), down(110)]
    assert jitter(samples, GAP) == (None, 0)
    assert list(consecutive_pairs(samples, GAP)) == []


def test_a_row_with_replies_but_no_stored_rtt_is_ignored_not_zeroed():
    corrupt = CheckSample(105, 3, 3, None)
    assert jitter([up(100, 10), corrupt, up(110, 12)], GAP) == (None, 0)
    assert mean_latency([up(100, 10), corrupt]) == 10.0


def test_jitter_matches_a_plain_reference_on_random_data():
    rng = random.Random(7)
    rtts = [rng.uniform(1, 80) for _ in range(200)]
    expected = sum(abs(b - a) for a, b in zip(rtts, rtts[1:], strict=False)) / (len(rtts) - 1)
    value, pairs = jitter(run(rtts), GAP)
    assert pairs == 199 and value == pytest.approx(expected)


# ------------------------------------------------------------------ latency and loss
def test_latency_is_weighted_by_replies_and_ignores_down_checks():
    # 3 replies averaging 10 ms and 1 reply of 30 ms: (30 + 30) / 4 = 15; the down check adds none
    samples = [up(100, 10, received=3), down(105), up(110, 30, received=1)]
    assert mean_latency(samples) == 15.0


def test_latency_is_none_when_nothing_replied():
    assert mean_latency([down(100), down(105)]) is None
    assert mean_latency([]) is None


def test_a_real_zero_ms_reply_is_zero_latency_not_missing():
    assert mean_latency([up(100, 0.0), up(105, 0.0)]) == 0.0


def test_loss_is_unanswered_probes_over_probes_sent():
    # 9 sent, 3 + 2 + 0 received
    samples = [up(100, 5, 3), up(105, 5, 2), down(110)]
    assert packet_loss(samples) == (pytest.approx(100 * 4 / 9), 9, 5)


def test_loss_is_none_not_zero_without_any_probes():
    assert packet_loss([]) == (None, 0, 0)


def test_a_fully_down_period_is_100_percent_loss():
    assert packet_loss([down(100), down(105)])[0] == 100.0


def test_missing_checks_do_not_change_loss():
    # the same checks, with a long hole in the middle: loss only counts probes actually sent
    dense = [up(100, 5, 3), up(105, 5, 2)]
    holey = [up(100, 5, 3), up(5000, 5, 2)]
    assert packet_loss(dense)[0] == packet_loss(holey)[0]


# ------------------------------------------------------------------ gaps
def test_find_gaps_uses_the_same_rules_as_the_latency_chart():
    samples = [up(10), up(15), up(100), up(105)]
    gaps = find_gaps(samples, 0, 106, GAP, first_check_ts=10)
    assert gaps == [(15, 100)]  # history starts at 10, so 0-10 is not a gap; the end is fresh


def test_a_window_with_earlier_history_but_no_checks_is_one_gap():
    assert find_gaps([], 500, 900, GAP, first_check_ts=100) == [(500, 900)]
    assert find_gaps([], 500, 900, GAP, first_check_ts=None) == []


def test_a_window_that_ends_without_a_recent_check_has_a_trailing_gap():
    assert find_gaps([up(10), up(15)], 0, 200, GAP, first_check_ts=10) == [(15, 200)]


# ------------------------------------------------------------------ classification
def healthy(n=20, rtt=10.0):
    return run([rtt + (i % 2) for i in range(n)])  # 10, 11, 10, ...: jitter 1 ms


def test_a_healthy_link_is_excellent_with_all_figures():
    s = summary(healthy())
    assert s["level"] == "excellent" and s["reason"] is None
    assert s["jitter_ms"] == 1.0 and s["loss_pct"] == 0.0 and s["latency_ms"] == 10.5
    assert s["grades"] == {"latency": "excellent", "jitter": "excellent", "loss": "excellent"}
    assert s["checks"] == 20 and s["probes_sent"] == 60 and s["jitter_pairs"] == 19


def test_the_grade_is_the_worst_figure_and_says_which_one_limits_it():
    samples = run([10 + (20 if i % 2 else 0) for i in range(20)])  # jitter 20 ms: fair
    s = summary(samples)
    assert s["grades"]["latency"] == "excellent" and s["grades"]["jitter"] == "fair"
    assert s["level"] == "fair" and s["limiting"] == ["jitter"]


def test_loss_alone_can_make_a_link_poor():
    samples = healthy(20)
    samples[3] = down(samples[3].ts)  # one check with 3 lost probes out of 60
    s = summary(samples)
    assert s["loss_pct"] == pytest.approx(5.0) and s["grades"]["loss"] == "fair"
    samples[9] = down(samples[9].ts)
    s = summary(samples)
    assert s["loss_pct"] == pytest.approx(10.0) and s["level"] == "poor"
    assert s["limiting"] == ["loss"]


def test_thresholds_are_configurable():
    strict = QualityConfig(latency_ms=QualityThresholds(5, 8, 9))
    assert summary(healthy(), strict)["level"] == "poor"  # 10.5 ms is above a 9 ms fair limit
    lenient = QualityConfig(latency_ms=QualityThresholds(50, 100, 200))
    assert summary(healthy(), lenient)["level"] == "excellent"


def test_unknown_with_too_few_checks_says_why_and_still_reports_the_figures():
    s = summary(healthy(5))
    assert s["level"] == LEVEL_UNKNOWN and "insufficient samples" in s["reason"]
    assert s["grades"] is None and s["limiting"] == []
    assert s["latency_ms"] == 10.4 and s["checks"] == 5  # numbers are still shown


def test_unknown_when_the_device_has_never_been_checked():
    s = summary([], first=None, last=None, end=1000.0)
    assert s["level"] == LEVEL_UNKNOWN and s["reason"] == "no measurements yet"
    assert s["latency_ms"] is None and s["jitter_ms"] is None and s["loss_pct"] is None


def test_unknown_when_the_window_holds_no_checks_but_history_exists():
    s = summary([], first=10.0, last=20.0, start=500, end=900)
    assert s["level"] == LEVEL_UNKNOWN
    # stale takes precedence: the last check is far older than the gap limit
    assert s["stale"] is True and "stale" in s["reason"]


def test_unknown_when_stale_even_if_the_window_has_plenty_of_good_data():
    samples = healthy(30)  # checks from 100 to 245
    s = summarize(samples, CFG, start=0, end=400, gap_s=GAP, first_check_ts=100,
                  last_check_ts=245)
    assert s["stale"] and s["level"] == LEVEL_UNKNOWN and "stale" in s["reason"]


def test_a_fresh_device_is_not_stale_and_a_stale_one_is():
    fresh = summarize(healthy(), CFG, start=0, end=100 + 19 * 5 + 10, gap_s=GAP,
                      first_check_ts=100, last_check_ts=195)
    assert fresh["stale"] is False and fresh["last_check_age_s"] == 10


def test_sparse_history_is_unknown_even_with_enough_checks():
    # 12 checks in two bursts with a long hole: most of the window is unmeasured
    burst = run([10] * 6, start=0) + run([10] * 6, start=1000)
    s = summarize(burst, CFG, start=0, end=1030, gap_s=GAP, first_check_ts=0,
                  last_check_ts=1025)
    assert s["level"] == LEVEL_UNKNOWN and "sparse history" in s["reason"]
    assert s["gap_fraction"] > 0.9 and s["gap_count"] == 1


def test_history_shorter_than_the_window_is_not_sparse():
    # the device has only existed for 100 s of a 15-minute window; that is not a monitoring gap
    samples = healthy(20, 10.0)
    s = summarize(samples, CFG, start=195 - 900, end=196, gap_s=GAP, first_check_ts=100,
                  last_check_ts=195)
    assert s["gap_count"] == 0 and s["gap_fraction"] == 0.0 and s["level"] == "excellent"


def test_a_device_that_never_replies_is_poor_with_an_explanation_not_zero_latency():
    s = summary([down(100 + 5 * i) for i in range(20)])
    assert s["level"] == "poor" and "no replies" in s["reason"]
    assert s["loss_pct"] == 100.0 and s["latency_ms"] is None and s["jitter_ms"] is None
    assert s["grades"] == {"loss": "poor"} and s["limiting"] == ["loss"]


def test_too_few_consecutive_successful_checks_for_jitter_is_unknown():
    # alternating up/down: plenty of checks and replies, but never two good checks in a row
    samples = [up(100 + 10 * i, 10) if i % 2 == 0 else down(105 + 10 * (i - 1)) for i in range(20)]
    samples.sort(key=lambda c: c.ts)
    s = summary(samples)
    assert s["jitter_pairs"] == 0 and s["jitter_ms"] is None
    assert s["level"] == LEVEL_UNKNOWN and "jitter" in s["reason"]
    assert s["loss_pct"] == pytest.approx(50.0)  # the loss figure itself is still reported


def test_the_minimums_are_configurable():
    cfg = QualityConfig(min_checks=3, min_jitter_pairs=1)
    assert summary(healthy(4), cfg)["level"] == "excellent"
    assert summary(healthy(2), cfg)["level"] == LEVEL_UNKNOWN


def test_an_outage_inside_the_window_is_loss_and_never_zero_latency():
    samples = healthy(10) + [down(150 + 5 * i) for i in range(5)] + [
        up(175 + 5 * i, 10 + (i % 2)) for i in range(10)
    ]
    s = summary(samples)
    assert s["loss_pct"] == pytest.approx(100 * 15 / 75)  # 5 down checks of 25
    assert s["latency_ms"] == pytest.approx(10.5, abs=0.1)  # unaffected by the outage
    assert s["jitter_ms"] == pytest.approx(1.0)  # pairs never span the down checks
    assert s["level"] == "poor"


# ------------------------------------------------------------------ series for the charts
def test_series_buckets_report_loss_and_jitter_and_omit_empty_buckets():
    # two bursts of six checks (30 s each) with nothing between 130 s and 580 s
    samples = run([10, 12, 10, 12, 10, 12], start=100) + run([10] * 6, start=580)
    points, bucket_s = series(
        samples, start=0, end=700, gap_s=GAP, first_check_ts=100, poll_interval_s=5,
        max_points=100,
    )
    assert bucket_s == 30  # six polling intervals: never narrower, so a bucket has enough probes
    assert [(p["start"], p["end"]) for p in points] == [(100, 130), (580, 610)]  # no zero fill
    first, second = points
    assert first["jitter_ms"] == 2.0 and first["jitter_pairs"] == 5 and first["loss_pct"] == 0.0
    assert second["jitter_ms"] == 0.0 and second["jitter_pairs"] == 5


def test_a_bucket_with_no_consecutive_successes_has_null_jitter_not_zero():
    samples = [up(100, 10), down(105), up(110, 50), down(115), up(120, 10), down(125)]
    (point,), _ = series(
        samples, start=100, end=130, gap_s=GAP, first_check_ts=100, poll_interval_s=5,
        max_points=10,
    )
    assert point["jitter_ms"] is None and point["jitter_pairs"] == 0
    assert point["loss_pct"] == 50.0  # 9 of 18 probes unanswered


def test_a_pair_belongs_to_the_bucket_of_its_later_check():
    # buckets are 100-130 and 130-160: the (125, 130) pair straddles them and goes to the second
    samples = [up(120, 10), up(125, 10), up(130, 40), up(135, 40)]
    points, _ = series(
        samples, start=100, end=160, gap_s=GAP, first_check_ts=100, poll_interval_s=5,
        max_points=100,
    )
    first, second = points
    assert (first["checks"], first["jitter_pairs"], first["jitter_ms"]) == (2, 1, 0.0)
    assert (second["checks"], second["jitter_pairs"], second["jitter_ms"]) == (2, 2, 15.0)


def test_series_loss_per_bucket_counts_only_probes_actually_sent():
    samples = [up(100, 5.0), down(105), up(110, 5.0)]
    (point,), _ = series(
        samples, start=100, end=125, gap_s=GAP, first_check_ts=100, poll_interval_s=5,
        max_points=10,
    )
    assert point["sent"] == 9 and point["received"] == 6
    assert point["loss_pct"] == pytest.approx(100 / 3, abs=0.01)


def test_series_of_nothing_is_empty():
    assert series([], start=0, end=100, gap_s=GAP, first_check_ts=None, poll_interval_s=5,
                  max_points=60)[0] == []
