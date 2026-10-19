"""Alerting: turn device status changes into stored alerts.

Each check's status is noisy (one lost packet in three marks a device "degraded"), so a device's
*confirmed* status only changes after CONFIRM_CHECKS consecutive checks agree on a new status.
That is what makes a transition an alert, and it is why a device that stays in one state never
raises a second alert.

The confirmed status lives in the database (table device_state), so restarts continue where they
left off. When there is no reliable previous state, nothing is invented:
  * a device with no stored state (first run, or newly added) gets a silent baseline from its first
    CONFIRM_CHECKS agreeing checks;
  * if the last check that updated the state is older than max_gap_s (the app was stopped, or the
    device couldn't be measured for a while), the old state is discarded and re-baselined silently,
    because we can't know what happened in between.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from netmonlt.domain import DEGRADED_LOSS_PCT, DEGRADED_RTT_MS, CheckResult, PingResult, Status

if TYPE_CHECKING:
    from netmonlt.storage import Database

CONFIRM_CHECKS = 3


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Alert:
    device: str
    ts: float  # unix epoch seconds, UTC: the check that confirmed the change
    severity: Severity
    from_status: Status
    to_status: Status
    message: str


@dataclass(frozen=True)
class AlertState:
    """Per-device alerting state, stored between checks (and restarts)."""

    status: Status | None = None  # confirmed status; None until a baseline is established
    since: float | None = None  # when the confirmed status began
    candidate: Status | None = None  # a different status seen recently, awaiting confirmation
    candidate_count: int = 0  # consecutive checks showing the candidate
    candidate_since: float | None = None
    updated: float = 0.0  # ts of the last check applied to this state


def severity_for(to_status: Status) -> Severity:
    if to_status is Status.DOWN:
        return Severity.CRITICAL
    if to_status is Status.DEGRADED:
        return Severity.WARNING
    return Severity.INFO


def advance(
    state: AlertState | None,
    check: CheckResult,
    max_gap_s: float,
    confirm_checks: int = CONFIRM_CHECKS,
) -> tuple[AlertState, Alert | None]:
    """Apply one check to a device's state. Returns the new state and an Alert if one was raised."""
    if state is None or abs(check.ts - state.updated) > max_gap_s:
        state = AlertState()  # no reliable previous state: start over, silently

    status = check.status
    if status == state.status:  # (back) in the confirmed status: any pending change is dropped
        return replace(
            state, candidate=None, candidate_count=0, candidate_since=None, updated=check.ts
        ), None

    if status == state.candidate:
        count, since = state.candidate_count + 1, state.candidate_since
    else:
        count, since = 1, check.ts
    if count < confirm_checks:
        pending = replace(
            state, candidate=status, candidate_count=count, candidate_since=since, updated=check.ts
        )
        return pending, None

    confirmed = AlertState(status=status, since=since, updated=check.ts)
    if state.status is None:  # first confirmed status: a baseline, not a transition
        return confirmed, None
    return confirmed, _alert(check, state.status, state.since, since)


def process_check(db: "Database", check: CheckResult, max_gap_s: float) -> Alert | None:
    """Load the device's state, apply the check, and save the new state (and alert) atomically."""
    state, alert = advance(db.get_alert_state(check.device), check, max_gap_s)
    db.save_alert_state(check.device, state, alert)
    return alert


def _alert(check: CheckResult, old: Status, old_since: float | None, new_since: float) -> Alert:
    new = check.status
    started = new_since if old_since is None else old_since
    was = f"was {old.value} for {_duration(new_since - started)}"
    name, ping = check.device, check.ping
    if new is Status.DOWN:
        message = f"{name} is DOWN: no reply to {ping.sent} pings ({was})"
    elif new is Status.DEGRADED and old is Status.DOWN:
        message = f"{name} is responding again but DEGRADED: {_reasons(ping)} ({was})"
    elif new is Status.DEGRADED:
        message = f"{name} is DEGRADED: {_reasons(ping)} ({was})"
    else:
        rtt = f", avg {ping.rtt_avg_ms:g} ms" if ping.rtt_avg_ms is not None else ""
        message = f"{name} recovered: {ping.received}/{ping.sent} replies{rtt} ({was})"
    return Alert(name, check.ts, severity_for(new), old, new, message)


def _reasons(ping: PingResult) -> str:
    reasons = []
    if ping.loss_pct >= DEGRADED_LOSS_PCT:
        reasons.append(f"{ping.loss_pct:.0f}% packet loss")
    if ping.rtt_avg_ms is not None and ping.rtt_avg_ms >= DEGRADED_RTT_MS:
        reasons.append(f"high latency (avg {ping.rtt_avg_ms:g} ms)")
    return " and ".join(reasons) or "unhealthy"


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
