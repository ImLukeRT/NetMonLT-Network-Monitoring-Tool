"""Network-quality figures (latency, jitter, packet loss) and a grade, from stored ICMP checks.

Everything here is a pure function of check rows already in SQLite: nothing is probed and
nothing new is stored. What the figures are, and are not:

  * Latency: the mean round-trip time of the replies received in the window. A check with no
    reply has no latency and is left out. It is never counted as 0 ms.
  * Jitter: the mean absolute difference between the average round-trip times of two
    consecutive checks that both got replies (|rtt[n] - rtt[n-1]|, in ms). Each check's average
    is over up to 3 pings, so this is a coarser, smoother figure than per-packet jitter. It is
    NOT the RFC 3550 interarrival jitter used for RTP media streams, and it says nothing about
    one-way delay: ICMP echo only measures the round trip. Two checks are a pair only if they
    are adjacent in time (no missing check between them, no monitoring gap).
  * Packet loss: 100 * (probes sent - replies received) / probes sent, over the checks actually
    made. Time with no checks (the app stopped, probe errors) is not counted as loss and not as
    success; it shows up as a gap instead.
  * Grade: Excellent, Good, Fair or Poor is the WORST of the three figures' grades against the
    thresholds in the inventory's `quality:` block. They are application-level heuristics, not
    an industry QoS standard. "Unknown" means there is not enough trustworthy data to grade.

This measures network performance as seen by ICMP. It does not verify, enforce or prove any QoS
policy (DSCP marking, traffic shaping, queue management): ICMP echo may be treated differently
from other traffic.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from netmonlt.domain import QUALITY_LEVELS, QualityConfig, QualityThresholds

LEVEL_UNKNOWN = "unknown"
MIN_BUCKET_CHECKS = 6  # a chart bucket spans at least this many polling intervals

DEFINITIONS = {
    "latency": (
        "Mean round-trip time of the replies received in the window. Checks with no reply have "
        "no latency and are excluded, never counted as 0 ms."
    ),
    "jitter": (
        "Mean absolute difference between the average round-trip times of consecutive checks "
        "that both received replies, in ms. Not RFC 3550 RTP jitter, and not one-way delay."
    ),
    "loss": (
        "Probes sent minus replies received, as a percentage of probes sent, over the checks "
        "actually made. Periods with no checks are gaps, not loss."
    ),
    "grade": (
        "The worst of the latency, jitter and loss grades against configurable thresholds. "
        "An application-level heuristic, not a QoS standard, and not proof that any QoS policy "
        "is applied."
    ),
}


@dataclass(frozen=True)
class CheckSample:
    """One stored check: three pings sent to a device at one moment."""

    ts: float
    sent: int
    received: int
    rtt_avg_ms: float | None  # mean of the replies in this check; None if there were none

    @property
    def has_rtt(self) -> bool:
        return self.received > 0 and self.rtt_avg_ms is not None


def grade(value: float, thresholds: QualityThresholds) -> str:
    """Excellent/Good/Fair/Poor for one figure (lower is better; each bound is inclusive)."""
    if value <= thresholds.excellent:
        return QUALITY_LEVELS[0]
    if value <= thresholds.good:
        return QUALITY_LEVELS[1]
    if value <= thresholds.fair:
        return QUALITY_LEVELS[2]
    return QUALITY_LEVELS[3]


def consecutive_pairs(
    samples: Sequence[CheckSample], gap_s: float
) -> Iterator[tuple[CheckSample, CheckSample]]:
    """Pairs of adjacent checks that both have a round-trip time and are at most `gap_s` apart.

    `samples` must be in time order. A check without replies, or a gap, between two checks means
    they are not a pair, so jitter is never measured across missing data.
    """
    for prev, cur in zip(samples, samples[1:], strict=False):
        if prev.has_rtt and cur.has_rtt and 0 < cur.ts - prev.ts <= gap_s:
            yield prev, cur


def jitter(samples: Sequence[CheckSample], gap_s: float) -> tuple[float | None, int]:
    """(mean absolute difference in ms, number of pairs); the mean is None with no pairs."""
    diffs = [abs(b.rtt_avg_ms - a.rtt_avg_ms) for a, b in consecutive_pairs(samples, gap_s)]
    return (sum(diffs) / len(diffs) if diffs else None), len(diffs)


def mean_latency(samples: Sequence[CheckSample]) -> float | None:
    """Reply-weighted mean round-trip time, or None if no check got a reply."""
    replies = sum(s.received for s in samples if s.has_rtt)
    if not replies:
        return None
    return sum(s.rtt_avg_ms * s.received for s in samples if s.has_rtt) / replies


def packet_loss(samples: Sequence[CheckSample]) -> tuple[float | None, int, int]:
    """(loss percent, probes sent, replies received); loss is None if nothing was sent."""
    sent = sum(s.sent for s in samples)
    received = sum(s.received for s in samples)
    if sent <= 0:
        return None, sent, received
    return 100.0 * (sent - received) / sent, sent, received


def find_gaps(
    samples: Sequence[CheckSample], start: float, end: float, gap_s: float,
    first_check_ts: float | None,
) -> list[tuple[float, float]]:
    """Periods inside [start, end] with no checks, using the same rules as the latency chart:
    consecutive checks more than `gap_s` apart, a window that opens or closes without a check
    near its edge (when earlier history exists), or a window with no checks at all."""
    if not samples:
        if first_check_ts is not None and first_check_ts <= end:
            return [(start, end)]  # history exists, but only before this window
        return []
    gaps = []
    if first_check_ts is not None and first_check_ts < start and samples[0].ts - start > gap_s:
        gaps.append((start, samples[0].ts))
    for prev, cur in zip(samples, samples[1:], strict=False):
        if cur.ts - prev.ts > gap_s:
            gaps.append((prev.ts, cur.ts))
    if end - samples[-1].ts > gap_s:
        gaps.append((samples[-1].ts, end))
    return gaps


def effective_start(start: float, first_check_ts: float | None) -> float:
    """Where the measurable part of the window begins: history shorter than the window is not
    treated as missing data before the first check."""
    return start if first_check_ts is None else max(start, first_check_ts)


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def summarize(
    samples: Sequence[CheckSample],
    cfg: QualityConfig,
    *,
    start: float,
    end: float,
    gap_s: float,
    first_check_ts: float | None,
    last_check_ts: float | None,
) -> dict:
    """The quality figures and grade for the checks in [start, end].

    `first_check_ts` and `last_check_ts` are the device's first and last check ever (not just in
    the window): they tell a short history or a stale device apart from a healthy one.
    """
    latency = mean_latency(samples)
    jitter_ms, pairs = jitter(samples, gap_s)
    loss, sent, received = packet_loss(samples)
    gaps = find_gaps(samples, start, end, gap_s, first_check_ts)
    span = end - effective_start(start, first_check_ts)
    gap_time = sum(min(b, end) - max(a, start) for a, b in gaps)
    gap_fraction = min(1.0, max(0.0, gap_time / span)) if span > 0 else 0.0
    age = None if last_check_ts is None else max(0.0, end - last_check_ts)
    stale = age is not None and age > gap_s

    level, reason, grades, limiting = _classify(
        cfg, checks=len(samples), replies=received, latency=latency, jitter_ms=jitter_ms,
        pairs=pairs, loss=loss, gap_fraction=gap_fraction, stale=stale, age=age,
        has_history=first_check_ts is not None,
    )
    return {
        "level": level,
        "reason": reason,
        "grades": grades,
        "limiting": limiting,
        "latency_ms": _r(latency),
        "jitter_ms": _r(jitter_ms),
        "loss_pct": _r(loss),
        "checks": len(samples),
        "probes_sent": sent,
        "probes_received": received,
        "latency_checks": sum(1 for s in samples if s.has_rtt),
        "jitter_pairs": pairs,
        "gap_count": len(gaps),
        "gap_fraction": round(gap_fraction, 3),
        "last_check_age_s": age,
        "stale": stale,
    }


def _classify(
    cfg: QualityConfig, *, checks: int, replies: int, latency: float | None,
    jitter_ms: float | None, pairs: int, loss: float | None, gap_fraction: float, stale: bool,
    age: float | None, has_history: bool,
) -> tuple[str, str | None, dict | None, list[str]]:
    """(level, reason it is Unknown or Poor-by-outage, per-figure grades, limiting figures)."""

    def unknown(reason: str):
        return LEVEL_UNKNOWN, reason, None, []

    if not has_history:
        return unknown("no measurements yet")
    if stale:
        return unknown(f"stale: no new measurement for {round(age)} s")
    if checks == 0:
        return unknown("no measurements in this window")
    if checks < cfg.min_checks:
        return unknown(f"insufficient samples: {checks} checks, at least {cfg.min_checks} needed")
    if gap_fraction > cfg.max_gap_fraction:
        return unknown(
            f"sparse history: {round(100 * gap_fraction)}% of the window has no measurements"
        )
    if replies == 0 or latency is None or loss is None:
        return (
            QUALITY_LEVELS[-1], "no replies to any probe in this window (device unreachable)",
            {"loss": QUALITY_LEVELS[-1]}, ["loss"],
        )
    if jitter_ms is None or pairs < cfg.min_jitter_pairs:
        return unknown(
            f"insufficient samples for jitter: {pairs} pairs of consecutive successful checks, "
            f"at least {cfg.min_jitter_pairs} needed"
        )
    grades = {
        "latency": grade(latency, cfg.latency_ms),
        "jitter": grade(jitter_ms, cfg.jitter_ms),
        "loss": grade(loss, cfg.loss_pct),
    }
    worst = max(grades.values(), key=QUALITY_LEVELS.index)
    return worst, None, grades, [k for k, v in grades.items() if v == worst]


def series(
    samples: Sequence[CheckSample],
    *,
    start: float,
    end: float,
    gap_s: float,
    first_check_ts: float | None,
    poll_interval_s: float,
    max_points: int,
) -> tuple[list[dict], float]:
    """Packet loss and jitter over time, in equal time buckets. Returns (buckets, bucket_s).

    Buckets are at least MIN_BUCKET_CHECKS polling intervals wide, so each holds enough probes for
    a loss percentage to mean something. Empty buckets are omitted (never zero-filled); a bucket's
    `jitter_ms` is None when it holds no pair of consecutive successful checks. A pair belongs to
    the bucket of its later check.
    """
    x0 = effective_start(start, first_check_ts)
    bucket_s = max((end - x0) / max_points, MIN_BUCKET_CHECKS * poll_interval_s)
    acc: dict[int, dict] = {}
    for s in samples:
        b = acc.setdefault(
            int((s.ts - x0) // bucket_s),
            {"checks": 0, "sent": 0, "received": 0, "diff_sum": 0.0, "pairs": 0},
        )
        b["checks"] += 1
        b["sent"] += s.sent
        b["received"] += s.received
    for prev, cur in consecutive_pairs(samples, gap_s):
        b = acc[int((cur.ts - x0) // bucket_s)]
        b["diff_sum"] += abs(cur.rtt_avg_ms - prev.rtt_avg_ms)
        b["pairs"] += 1
    points = []
    for index in sorted(acc):
        b = acc[index]
        points.append({
            "start": x0 + index * bucket_s,
            "end": min(x0 + (index + 1) * bucket_s, end),
            "checks": b["checks"],
            "sent": b["sent"],
            "received": b["received"],
            "loss_pct": _r(100.0 * (b["sent"] - b["received"]) / b["sent"]) if b["sent"] else None,
            "jitter_ms": _r(b["diff_sum"] / b["pairs"]) if b["pairs"] else None,
            "jitter_pairs": b["pairs"],
        })
    return points, bucket_s
