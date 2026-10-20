"""Incident management: a human workflow layered ON TOP of the monitoring alert log.

NetMonLT's alert state machine (alerts.py) is unchanged: it still confirms status changes after
3 consecutive checks and records UP / DEGRADED / DOWN alerts, including "recovered". This module
only READS those alerts and groups them into incidents. Two things are kept strictly apart:

  * MONITORING state (technical): "active" while the device's confirmed status is not UP, and
    "recovered" once monitoring has confirmed it is UP again. Only the alert log can change it.
    A recovery alert does NOT mean anyone investigated or fixed anything.
  * REVIEW state (human): "open", "acknowledged" or "resolved". Only a person's explicit action
    can change it, and no action can alter monitoring state or the evidence behind it.

So an incident can be "auto-recovered, awaiting review". Resolving needs a written summary and is
only allowed once monitoring has recovered, so a person cannot hide an ongoing outage. Resolving
removes an incident from the active view; nothing is deleted, and it can be reopened.

Correlation: a fault for the same device and alert type that returns within the correlation
window of a recovery is the SAME incident (and counts as a recurrence, so flapping is visible).
Later, or after the incident was resolved, it is a new incident that links back to the previous
one. Incidents of different devices are never merged.

Everything in a report is labelled by where it came from: observed by NetMonLT, entered by an
operator, or (simulated mode only) fictional illustrative example content.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netmonlt.alerts import CONFIRM_CHECKS
from netmonlt.domain import DEGRADED_LOSS_PCT, DEGRADED_RTT_MS, IncidentsConfig, SimProfile

# ---- monitoring and review states
MON_ACTIVE = "active"
MON_RECOVERED = "recovered"
MON_UNKNOWN = "unknown"  # imported history: a fault whose recovery was never recorded
REV_OPEN = "open"
REV_ACKNOWLEDGED = "acknowledged"
REV_RESOLVED = "resolved"

KIND_ICMP = "icmp-status"  # the only alert type today; correlation never crosses types

# ---- event sources and kinds (the audit trail is append-only)
SRC_MONITORING = "monitoring"
SRC_OPERATOR = "operator"
EV_OPENED, EV_ALERT, EV_RECOVERED, EV_RECURRED = "opened", "alert", "recovered", "recurred"
EV_IMPORTED = "imported"  # the incident was reconstructed later from stored alerts
ORIGIN_LIVE, ORIGIN_IMPORTED = "live", "imported"
EV_ACK, EV_NOTE, EV_REPORT, EV_RESOLVED, EV_REOPENED = (
    "acknowledged", "note", "report_updated", "resolved", "reopened")
EVENT_LABELS = {
    EV_OPENED: "Detected", EV_ALERT: "Further alert", EV_RECOVERED: "Monitoring recovered",
    EV_RECURRED: "Fault recurred", EV_ACK: "Acknowledged", EV_NOTE: "Investigation note",
    EV_REPORT: "Write-up updated", EV_RESOLVED: "Resolved", EV_REOPENED: "Reopened",
    EV_IMPORTED: "Imported from alert history",
}

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
STATUS_FILTERS = ("open", "acknowledged", "recovered", "unknown", "reopened", "resolved")
MAX_EVIDENCE_ROWS = 5000

# ---- operator input
ROOT_CAUSE_STATUSES = ("unconfirmed", "suspected", "confirmed")
REPORT_TEXT_FIELDS = {  # field -> maximum length
    "root_cause": 2000, "actions_taken": 4000, "impact_assessment": 2000,
    "recovery_verification": 2000, "lessons_learned": 4000, "resolution_summary": 4000,
}
MAX_NOTE_LEN = 4000
MIN_SUMMARY_LEN = 10
MAX_REASON_LEN = 1000
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@'-]{0,39}$")
DEFAULT_ACTOR = "operator"


class IncidentError(Exception):
    """A request that cannot be honoured. `code` is one of the constants below."""

    NOT_FOUND = "not_found"
    INVALID_TRANSITION = "invalid_transition"
    INVALID_INPUT = "invalid_input"
    STALE = "stale_preview"

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Incident:
    id: int
    device: str
    kind: str
    severity: str  # the most severe level reached
    opened_ts: float  # the alert that detected it
    last_alert_ts: float
    monitoring_state: str
    recovered_ts: float | None  # the latest confirmed recovery
    flap_count: int  # recurrences grouped into this incident
    review_state: str
    acknowledged_ts: float | None
    acknowledged_by: str | None
    resolved_ts: float | None
    resolved_by: str | None
    reopened_count: int
    previous_incident_id: int | None
    simulated: bool
    run_started_ts: float | None  # when the simulation run that raised it began (simulated only)
    report: dict = field(default_factory=dict)  # operator-entered write-up fields
    evidence: dict = field(default_factory=dict)  # the stored snapshot of monitoring evidence
    origin: str = ORIGIN_LIVE  # "imported": reconstructed afterwards from stored alert history
    import_id: int | None = None


@dataclass(frozen=True)
class IncidentEvent:
    id: int
    incident_id: int
    ts: float
    source: str
    kind: str
    actor: str | None = None
    severity: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    alert_id: int | None = None
    text: str | None = None


def event_text(event: "IncidentEvent") -> str | None:
    """The text to show for an event. A write-up update is stored with its new values (so the
    earlier wording is never lost) but is shown as just the names of the fields it changed."""
    if event.kind != EV_REPORT:
        return event.text
    try:
        changed = json.loads(event.text or "{}")
    except ValueError:
        return None
    names = ", ".join(sorted(changed)) if isinstance(changed, dict) else ""
    return f"updated {names}" if names else None


def display_id(incident_id: int) -> str:
    return f"INC-{incident_id:04d}"


# ------------------------------------------------------------------ lifecycle
def lifecycle(inc: Incident) -> dict:
    """The combined, human-readable lifecycle of an incident (monitoring and review kept apart)."""
    recovered = inc.monitoring_state == MON_RECOVERED
    unknown = inc.monitoring_state == MON_UNKNOWN
    reopened = inc.reopened_count > 0
    if inc.review_state == REV_RESOLVED:
        code, label = "resolved", "Resolved"
    elif inc.review_state == REV_ACKNOWLEDGED:
        if recovered:
            code, label = "recovered_acknowledged", "Monitoring recovered — acknowledged"
        elif unknown:
            code, label = "unknown_acknowledged", "Recovery unknown — acknowledged"
        else:
            code, label = "acknowledged", "Acknowledged"
    elif reopened:
        if recovered:
            code, label = "reopened_recovered", "Reopened — monitoring recovered"
        elif unknown:
            code, label = "reopened_unknown", "Reopened — recovery unknown"
        else:
            code, label = "reopened", "Reopened"
    elif recovered:
        code, label = "recovered_awaiting_review", "Auto-recovered — awaiting review"
    elif unknown:
        code, label = "unknown_awaiting_review", "Recovery unknown — awaiting review"
    else:
        code, label = "open", "Open"
    return {
        "monitoring": inc.monitoring_state,
        "review": inc.review_state,
        "reopened": reopened,
        "code": code,
        "label": label,
        "awaiting_review": recovered and inc.review_state == REV_OPEN,
        "recovery_unknown": unknown,
        "imported": inc.origin == ORIGIN_IMPORTED,
        "in_active_view": inc.review_state != REV_RESOLVED,
    }


def is_flapping(inc: Incident, cfg: IncidentsConfig) -> bool:
    return inc.flap_count >= cfg.flapping_recurrences


def worse(a: str, b: str) -> str:
    """The more severe of two severity names."""
    return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b


# ------------------------------------------------------------------ correlation
def match_incident(
    candidates: Sequence[Incident], alert_ts: float, is_recovery: bool, window_s: float
) -> Incident | None:
    """The unresolved incident (newest first) an alert belongs to, or None.

    A fault alert joins an incident whose monitoring state is still active (a further alert in the
    same episode), or one that recovered no longer ago than the correlation window (a recurrence:
    the boundary is inclusive). A recovery alert only ever ends an active incident. Resolved
    incidents are never candidates, so a recurrence after closure becomes a new incident.
    """
    for inc in candidates:
        if inc.monitoring_state == MON_ACTIVE:
            return inc
        if (not is_recovery and inc.monitoring_state == MON_RECOVERED
                and inc.recovered_ts is not None and 0 <= alert_ts - inc.recovered_ts <= window_s):
            return inc
    return None


# ------------------------------------------------------------------ historical import
SKIP_RECOVERY_WITHOUT_FAULT = "recovery_without_recorded_fault"
SKIP_REASONS = {
    SKIP_RECOVERY_WITHOUT_FAULT:
        "a recovery alert with no recorded fault before it to attach to (nothing is invented)",
}


@dataclass(frozen=True)
class HistoricalAlert:
    """One stored alert, exactly as raised (read from the alert log, never changed)."""

    id: int
    device: str
    ts: float
    severity: str
    from_status: str
    to_status: str
    message: str


@dataclass
class DraftIncident:
    """An incident the import would create: its alerts (each with the event kind it becomes)
    and the state they imply. Built only from the alerts it was given."""

    device: str
    severity: str
    opened_ts: float
    last_alert_ts: float
    monitoring_state: str
    recovered_ts: float | None
    flap_count: int
    events: list[tuple[HistoricalAlert, str]]

    def as_candidate(self, ref: int) -> Incident:
        return Incident(
            id=ref, device=self.device, kind=KIND_ICMP, severity=self.severity,
            opened_ts=self.opened_ts, last_alert_ts=self.last_alert_ts,
            monitoring_state=self.monitoring_state, recovered_ts=self.recovered_ts,
            flap_count=self.flap_count, review_state=REV_OPEN, acknowledged_ts=None,
            acknowledged_by=None, resolved_ts=None, resolved_by=None, reopened_count=0,
            previous_incident_id=None, simulated=False, run_started_ts=None)


@dataclass(frozen=True)
class ImportPlan:
    drafts: tuple[DraftIncident, ...]  # in the order they would be created
    skipped: tuple[tuple[int, str], ...]  # (alert id, reason)
    considered: int
    fingerprint: str

    def summary(self) -> dict:
        recovered = sum(1 for d in self.drafts if d.monitoring_state == MON_RECOVERED)
        return {
            "alerts_considered": self.considered,
            "incidents_to_create": len(self.drafts),
            "recovered": recovered,
            "recovery_unknown": len(self.drafts) - recovered,
            "flapping_groups": sum(1 for d in self.drafts if d.flap_count > 0),
            "alerts_in_incidents": sum(len(d.events) for d in self.drafts),
            "alerts_to_skip": len(self.skipped),
            "skipped": [{"alert_id": a, "reason": r, "explanation": SKIP_REASONS.get(r, r)}
                        for a, r in self.skipped],
            "fingerprint": self.fingerprint,
        }


def plan_fingerprint(alert_ids: Sequence[int], window_s: float) -> str:
    """Identifies exactly which alerts a plan covers (and the rule that grouped them), so that
    an import can only be confirmed for the preview that was shown."""
    return hashlib.sha256(
        (f"{window_s!r}|" + ",".join(str(i) for i in alert_ids)).encode()).hexdigest()


def plan_import(alerts: Sequence[HistoricalAlert], *, window_s: float) -> ImportPlan:
    """Group stored alerts into the incidents live processing would have made, per device, by
    the SAME correlation rule (match_incident). Pure: nothing is read or written.

    A fault that never got a recovery alert ends as MON_UNKNOWN (recovery unknown), never
    "still down" and never "recovered". A recovery with no fault before it is skipped.
    """
    ordered_alerts = sorted(alerts, key=lambda a: a.id)
    per_device: dict[str, list[DraftIncident]] = {}
    created: list[DraftIncident] = []
    skipped: list[tuple[int, str]] = []
    for alert in ordered_alerts:
        drafts = per_device.setdefault(alert.device, [])
        is_recovery = alert.to_status == "up"
        candidates = [d.as_candidate(i) for i, d in reversed(list(enumerate(drafts)))]
        found = match_incident(candidates, alert.ts, is_recovery, window_s)
        target = None if found is None else drafts[found.id]
        if is_recovery:
            if target is None or target.monitoring_state != MON_ACTIVE:
                skipped.append((alert.id, SKIP_RECOVERY_WITHOUT_FAULT))
                continue
            target.monitoring_state, target.recovered_ts = MON_RECOVERED, alert.ts
            target.last_alert_ts = alert.ts
            target.events.append((alert, EV_RECOVERED))
        elif target is None:
            draft = DraftIncident(alert.device, alert.severity, alert.ts, alert.ts, MON_ACTIVE,
                                  None, 0, [(alert, EV_OPENED)])
            drafts.append(draft)
            created.append(draft)
        elif target.monitoring_state == MON_ACTIVE:
            target.severity = worse(target.severity, alert.severity)
            target.last_alert_ts = alert.ts
            target.events.append((alert, EV_ALERT))
        else:  # back within the correlation window of its recovery: a recurrence
            target.monitoring_state = MON_ACTIVE
            target.flap_count += 1
            target.severity = worse(target.severity, alert.severity)
            target.last_alert_ts = alert.ts
            target.events.append((alert, EV_RECURRED))
    for draft in created:
        if draft.monitoring_state == MON_ACTIVE:  # the stored alerts end without a recovery
            draft.monitoring_state, draft.recovered_ts = MON_UNKNOWN, None
    created.sort(key=lambda d: (d.opened_ts, d.events[0][0].id))
    return ImportPlan(
        drafts=tuple(created), skipped=tuple(skipped), considered=len(ordered_alerts),
        fingerprint=plan_fingerprint([a.id for a in ordered_alerts], window_s))


# ------------------------------------------------------------------ operator input
def clean_text(value: Any, field_name: str, *, max_len: int, min_len: int = 0) -> str:
    """Operator text, safe to store: only text, no control or invisible formatting characters
    (newlines and tabs are kept), trimmed and length-checked. It is still UNTRUSTED: it is only
    ever displayed as plain text, never as markup."""
    field_name = field_name.replace("_", " ")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise IncidentError(IncidentError.INVALID_INPUT, f"{field_name} must be text")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable()).strip()
    if len(text) > max_len:
        raise IncidentError(IncidentError.INVALID_INPUT,
                            f"{field_name} is too long (at most {max_len} characters)")
    if len(text) < min_len:
        raise IncidentError(IncidentError.INVALID_INPUT,
                            f"{field_name} must be at least {min_len} characters")
    return text


def clean_actor(value: Any) -> str:
    """Who is recorded as acting. It is self-declared (there are no user accounts), so it is a
    label for the audit trail, not proof of identity."""
    if value is None or value == "":
        return DEFAULT_ACTOR
    if not isinstance(value, str) or not _ACTOR_RE.fullmatch(value.strip()):
        raise IncidentError(
            IncidentError.INVALID_INPUT,
            "actor must be 1 to 40 characters: letters, digits, spaces and . _ @ ' -")
    return value.strip()


def normalize_report_update(updates: Mapping[str, Any], current: Mapping[str, Any]) -> dict:
    """The validated changes to an incident's operator write-up, as {field: new value}.

    Only fields that were given are changed. Unknown fields are rejected. A root cause cannot be
    called "confirmed" without a stated root cause.
    """
    allowed = set(REPORT_TEXT_FIELDS) | {"root_cause_status"}
    unknown = sorted(set(updates) - allowed)
    if unknown:
        raise IncidentError(IncidentError.INVALID_INPUT,
                            f"unknown write-up field(s): {', '.join(unknown)}")
    changes: dict[str, str] = {}
    for name, value in updates.items():
        if name == "root_cause_status":
            if value not in ROOT_CAUSE_STATUSES:
                allowed_values = ", ".join(ROOT_CAUSE_STATUSES)
                raise IncidentError(IncidentError.INVALID_INPUT,
                                    f"root_cause_status must be one of {allowed_values}")
            changes[name] = value
        else:
            changes[name] = clean_text(value, name, max_len=REPORT_TEXT_FIELDS[name])
    merged = {**current, **changes}
    if merged.get("root_cause_status") == "confirmed" and not merged.get("root_cause"):
        raise IncidentError(IncidentError.INVALID_INPUT,
                            "a root cause cannot be marked confirmed without stating it")
    return {k: v for k, v in changes.items() if current.get(k, "") != v}


# ------------------------------------------------------------------ transitions
def check_acknowledge(inc: Incident) -> None:
    if inc.review_state == REV_RESOLVED:
        raise IncidentError(IncidentError.INVALID_TRANSITION,
                            "this incident is resolved; reopen it to acknowledge it again")
    if inc.review_state == REV_ACKNOWLEDGED:
        raise IncidentError(IncidentError.INVALID_TRANSITION, "already acknowledged")


def check_resolve(inc: Incident, *, confirm_unknown: bool = False) -> None:
    if inc.review_state == REV_RESOLVED:
        raise IncidentError(IncidentError.INVALID_TRANSITION, "already resolved")
    if inc.monitoring_state == MON_ACTIVE:
        raise IncidentError(
            IncidentError.INVALID_TRANSITION,
            "monitoring still reports this fault: an incident can only be resolved after "
            "monitoring has confirmed recovery (acknowledge it meanwhile)")
    if inc.monitoring_state == MON_UNKNOWN and not confirm_unknown:
        raise IncidentError(
            IncidentError.INVALID_INPUT,
            "monitoring never recorded a recovery for this historical fault: confirm that you "
            "understand this (confirm_unknown_recovery) and explain in the summary how you know "
            "it ended")


def check_reopen(inc: Incident) -> None:
    if inc.review_state != REV_RESOLVED:
        raise IncidentError(IncidentError.INVALID_TRANSITION,
                            "only a resolved incident can be reopened")


# ------------------------------------------------------------------ evidence
def summarize_checks(
    rows: Sequence[Mapping[str, Any]], detected_ts: float, recovered_ts: float | None,
    end_ts: float, *, truncated: bool = False,
) -> dict:
    """The monitoring evidence for an incident from its stored ICMP checks (oldest first).

    `rows` are checks from the first non-up check of the episode onwards. Everything here is
    counted from stored checks; nothing is estimated.
    """
    down = sum(1 for r in rows if r["status"] == "down")
    degraded = sum(1 for r in rows if r["status"] == "degraded")
    up = sum(1 for r in rows if r["status"] == "up")
    sent = sum(r["sent"] for r in rows)
    received = sum(r["received"] for r in rows)
    non_up = [r["ts"] for r in rows if r["status"] != "up"]
    confirm = []
    if recovered_ts is not None:
        healthy = [r for r in rows if r["ts"] <= recovered_ts and r["status"] == "up"]
        confirm = healthy[-CONFIRM_CHECKS:]
    first_non_up = non_up[0] if non_up else None
    return {
        "first_non_up_ts": first_non_up,
        "detected_ts": detected_ts,
        "recovered_ts": recovered_ts,
        "window_end_ts": end_ts,
        "checks_total": len(rows), "checks_down": down, "checks_degraded": degraded,
        "checks_up": up, "probes_sent": sent, "probes_received": received,
        "loss_pct": None if sent == 0 else round(100.0 * (sent - received) / sent, 2),
        "last_non_up_ts": non_up[-1] if non_up else None,
        "recovery_checks": [
            {"ts": r["ts"], "sent": r["sent"], "received": r["received"],
             "rtt_avg_ms": r["rtt_avg_ms"]} for r in confirm
        ],
        "time_to_recovery_s": (None if recovered_ts is None
                               else round(recovered_ts - detected_ts, 1)),
        "fault_span_s": (None if recovered_ts is None or first_non_up is None
                         else round(recovered_ts - first_non_up, 1)),
        "truncated": truncated,
    }


# ------------------------------------------------------------------ formatting
def fmt_ts(ts: float | None) -> str | None:
    """A UTC timestamp, the same on every machine."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


# ------------------------------------------------------------------ illustrative content
# Fictional example text for SIMULATED incidents only. It is generated deterministically from the
# device type and the kind of fault, is always labelled as an example, and is never stored as
# something an operator did.
ILLUSTRATIVE_OUTAGE_STEPS = {
    "switch": ["Check the switch's power and the status LEDs.",
               "Check the uplink cable and port.",
               "Review the switch log around the detection time for link-down or power events."],
    "router": ["Check the router's power and WAN/LAN link lights.",
               "Check whether its upstream provider reports a fault.",
               "Review the router log around the detection time."],
    "firewall": ["Check the firewall's power and interface status.",
                 "Review its log for a restart or failover around the detection time.",
                 "Confirm its upstream link is up."],
    "access-point": ["Check the access point's power and its network cable.",
                     "Check the switch port it is connected to.",
                     "Review the controller log, if one is used."],
    "server": ["Check the server's power and network link.",
               "Check the operating system's network status from its console.",
               "Review its system log around the detection time."],
    "vpn-gateway": ["Check the gateway's WAN link and tunnel status.",
                    "Check whether the remote end is reachable.",
                    "Review the gateway's log for tunnel or interface events."],
}
ILLUSTRATIVE_DEFAULT_STEPS = [
    "Check the device's power and network link.",
    "Check the network path between NetMonLT and the device.",
    "Review the device's own log around the detection time.",
]
ILLUSTRATIVE_DEGRADED_STEPS = [
    "Check the link for errors, congestion or interference.",
    "Compare latency and loss from another vantage point, if available.",
    "Review the device's interface statistics for errors and discards.",
]
ILLUSTRATIVE_RECOMMENDATIONS = [
    "Record the real cause and the steps taken whenever a similar incident occurs.",
    "Decide whether repeated flapping of this kind should be escalated sooner.",
    "Review whether the monitoring interval and confirmation checks suit this device.",
]
ILLUSTRATIVE_LESSONS = [
    "Automatic recovery in monitoring does not explain the cause: review it anyway.",
    "Keep the evidence (checks and alerts) alongside the write-up.",
]
SIM_NOTICE = (
    "Everything in this block is fictional EXAMPLE content generated from the simulator's known "
    "events. It was not observed or performed by anyone, and no real device was involved."
)
NO_REMEDIATION = (
    "NetMonLT performed no repair, restart or reconfiguration of any device: it only observes "
    "and records."
)


def known_scenario_cause(
    inc: Incident, sim: SimProfile | None, poll_interval_s: float
) -> dict | None:
    """The cause the SIMULATOR itself defines for this incident, or None if it defines none.

    A scripted outage counts only if the incident's detection time falls inside it (allowing for
    the time confirmation takes). A loss or latency profile is the defined cause of unhealthy
    results from that device. A real device never gets one: its cause is unknown until an
    operator establishes it.
    """
    if sim is None or not inc.simulated:
        return None
    if sim.outage_start_s is not None and inc.run_started_ts is not None:
        elapsed = inc.opened_ts - inc.run_started_ts
        slack = (CONFIRM_CHECKS + 2) * poll_interval_s
        end = sim.outage_start_s + sim.outage_duration_s
        if sim.outage_start_s <= elapsed <= end + slack:
            return {
                "text": (f"The simulator scripted an outage for this device: no replies from "
                         f"{sim.outage_start_s:g} s to {end:g} s after the simulation started "
                         f"(outage_start_s={sim.outage_start_s:g}, "
                         f"outage_duration_s={sim.outage_duration_s:g})."),
                "source": "simulator profile",
            }
    if sim.loss * 100 >= DEGRADED_LOSS_PCT:
        return {
            "text": (f"The simulator defines {sim.loss * 100:g}% packet loss for this device. "
                     "Random drops against that profile can make its confirmed status change "
                     "between up and degraded."),
            "source": "simulator profile",
        }
    if sim.rtt_ms >= DEGRADED_RTT_MS:
        return {
            "text": (f"The simulator defines an average latency of {sim.rtt_ms:g} ms for this "
                     f"device, which is above the {DEGRADED_RTT_MS:g} ms degraded threshold."),
            "source": "simulator profile",
        }
    return None


# ------------------------------------------------------------------ the report
def build_report(
    inc: Incident,
    events: Sequence[IncidentEvent],
    evidence: Mapping[str, Any],
    *,
    cfg: IncidentsConfig,
    device_type: str = "host",
    sim: SimProfile | None = None,
    poll_interval_s: float = 5.0,
) -> dict:
    """The structured incident write-up. Keys are grouped by provenance:

      observed           verified by NetMonLT from stored checks and alerts
      operator           entered by a person (blank until they write it; never prefilled)
      simulated_example  fictional illustration, present only for simulated incidents

    Nothing in `observed` is guessed, and a cause appears only if the simulator defines one.
    """
    life = lifecycle(inc)
    imported = inc.origin == ORIGIN_IMPORTED
    unknown = inc.monitoring_state == MON_UNKNOWN
    first = next((e for e in events if e.source == SRC_MONITORING and e.alert_id is not None),
                 None)
    start_status = (first.to_status if first and first.to_status else "unhealthy").upper()
    summary = [f"{inc.device} entered a fault state (confirmed {start_status}) at "
               f"{fmt_ts(inc.opened_ts)}."]
    if inc.recovered_ts is not None and inc.monitoring_state == MON_RECOVERED:
        summary.append(f"Monitoring confirmed it had recovered at {fmt_ts(inc.recovered_ts)}, "
                       f"{fmt_duration(inc.recovered_ts - inc.opened_ts)} after detection.")
    elif unknown:
        summary.append("No recovery alert was recorded for this fault, so NetMonLT does not know "
                       "whether or when it ended.")
    else:
        summary.append("Monitoring has not confirmed recovery.")
    if imported:
        imp = next((e for e in events if e.kind == EV_IMPORTED), None)
        summary.append(
            "This incident was reconstructed afterwards from the stored alert history"
            + (f" ({fmt_ts(imp.ts)})" if imp else "")
            + ": the alert times, severities and messages are the original ones, but it was not "
            "open at the time and no one is recorded as having looked at it then.")
    if inc.flap_count:
        summary.append(f"The fault recurred {inc.flap_count} time(s) within the "
                       f"{fmt_duration(cfg.correlation_window_s)} correlation window, so this "
                       "incident groups them.")
    if inc.previous_incident_id:
        summary.append(f"An earlier incident on this device exists: "
                       f"{display_id(inc.previous_incident_id)}.")

    timeline = [
        {"ts": e.ts, "time": fmt_ts(e.ts), "source": e.source, "kind": e.kind,
         "label": EVENT_LABELS.get(e.kind, e.kind), "actor": e.actor, "text": event_text(e)}
        for e in events
    ]

    alerts = [e for e in events if e.source == SRC_MONITORING and e.kind in (EV_OPENED, EV_ALERT)]
    actions = []
    if first is not None:
        actions.append(
            f"Confirmed the change after {CONFIRM_CHECKS} consecutive checks agreed and raised a "
            f"{(first.severity or 'unknown').upper()} alert ({fmt_ts(first.ts)})"
            + (", from which this incident was later created by a historical import."
               if imported else " and opened this incident automatically."))
    if len(alerts) > 1:
        actions.append(f"Raised {len(alerts) - 1} further alert(s) as the status changed.")
    if inc.flap_count:
        actions.append(f"Grouped {inc.flap_count} recurrence(s) into this incident instead of "
                       "opening new ones.")
    if not imported:  # the interval then in force is not known for imported history
        actions.append(f"Kept checking the device every {poll_interval_s:g} s.")
    if inc.recovered_ts is not None and inc.monitoring_state == MON_RECOVERED:
        actions.append(
            f"Confirmed recovery after {CONFIRM_CHECKS} consecutive successful checks "
            f"({fmt_ts(inc.recovered_ts)}), raised an INFO alert and "
            + ("recorded the monitoring condition as recovered from it."
               if imported else "marked the monitoring condition as recovered.")
            + " This does not close the incident: a person must review and resolve it.")
    elif unknown:
        actions.append("Recorded no recovery for this fault: the stored alerts end without one, "
                       "so the recovery is unknown. NetMonLT has not assumed that it recovered "
                       "or that it is still down.")
    actions.append(NO_REMEDIATION)

    recovered_now = inc.monitoring_state == MON_RECOVERED
    confirm = (evidence.get("recovery_checks") or []) if recovered_now else []
    if confirm:
        parts = ", ".join(f"{c['received']}/{c['sent']}"
                          + (f" (avg {c['rtt_avg_ms']:g} ms)" if c.get("rtt_avg_ms") is not None
                             else "") for c in confirm)
        verification = (f"The {len(confirm)} checks that confirmed recovery received replies "
                        f"{parts}.")
    elif recovered_now:
        verification = "Recovery was confirmed, but the confirming checks are no longer stored."
    elif unknown:
        verification = ("No recovery alert was recorded, so there is no recorded recovery to "
                        "verify. NetMonLT does not know whether or when the fault ended.")
    else:
        verification = "Not yet recovered: there is no recovery to verify."

    total, sent = evidence.get("checks_total", 0), evidence.get("probes_sent", 0)
    if total:
        lost = sent - evidence.get("probes_received", 0)
        first_bad = fmt_ts(evidence.get("first_non_up_ts"))
        impact = (f"{evidence.get('checks_down', 0)} down and {evidence.get('checks_degraded', 0)} "
                  f"degraded of {total} ICMP checks between {first_bad} "
                  f"and {fmt_ts(evidence.get('window_end_ts'))}; {lost} of {sent} probes were "
                  f"unanswered ({evidence.get('loss_pct')}%). What this meant for users or "
                  "services is not measured by NetMonLT.")
    else:
        impact = "No stored checks are available for this incident."

    report: dict[str, Any] = {
        "id": display_id(inc.id),
        "incident_id": inc.id,
        "title": f"Incident {display_id(inc.id)}: {inc.device}",
        "device": inc.device,
        "device_type": device_type,
        "severity": inc.severity,
        "simulated": inc.simulated,
        "imported": imported,
        "data_label": ("SIMULATED monitoring data: no real device was involved" if inc.simulated
                       else "Real monitoring data"),
        "detected_at": fmt_ts(inc.opened_ts),
        "recovered_at": fmt_ts(inc.recovered_ts) if inc.monitoring_state == MON_RECOVERED else None,
        "recovery_unknown": unknown,
        "resolved_at": fmt_ts(inc.resolved_ts) if inc.review_state == REV_RESOLVED else None,
        "lifecycle": life,
        "flapping": is_flapping(inc, cfg),
        "flap_count": inc.flap_count,
        "observed": {
            "summary": " ".join(summary),
            "timeline": timeline,
            "evidence": dict(evidence),
            "automated_actions": actions,
            "recovery_verification": verification,
            "impact": impact,
        },
        "operator": {
            "root_cause_status": inc.report.get("root_cause_status", "unconfirmed"),
            **{name: inc.report.get(name, "") for name in REPORT_TEXT_FIELDS},
        },
        "simulated_example": None,
    }
    if inc.simulated:
        steps = (ILLUSTRATIVE_OUTAGE_STEPS.get(device_type, ILLUSTRATIVE_DEFAULT_STEPS)
                 if inc.severity == "critical" else ILLUSTRATIVE_DEGRADED_STEPS)
        cause = known_scenario_cause(inc, sim, poll_interval_s)
        recovered = inc.monitoring_state == MON_RECOVERED
        report["simulated_example"] = {
            "label": "SIMULATED / ILLUSTRATIVE EXAMPLE",
            "notice": SIM_NOTICE,
            "known_scenario_cause": cause,
            "no_cause_note": (None if cause else
                              "The simulator defines no cause for this incident, so none is "
                              "stated."),
            "illustrative_troubleshooting_steps": list(steps),
            "illustrative_resolution_summary": (
                "EXAMPLE ONLY (fictional): in this simulation the condition cleared by itself "
                "according to its script, so no repair was needed. For a real incident, record "
                "what you actually found and did." if recovered else
                "EXAMPLE ONLY (fictional): no recovery was recorded for this historical fault, "
                "so state in your own words how you know it ended." if unknown else
                "EXAMPLE ONLY (fictional): monitoring has not yet confirmed recovery, so there "
                "is nothing to resolve."),
            "illustrative_recommendations": list(ILLUSTRATIVE_RECOMMENDATIONS),
            "illustrative_lessons_learned": list(ILLUSTRATIVE_LESSONS),
        }
    return report


def _block(text: str | None, empty: str) -> str:
    return text.strip() if text and text.strip() else empty


def render_markdown(report: Mapping[str, Any]) -> str:
    """The write-up as Markdown for copying into a report. Operator text is included verbatim,
    as text: nothing here is HTML."""
    obs, op = report["observed"], report["operator"]
    ev = obs["evidence"]
    life = report["lifecycle"]
    unset = "_Not yet documented by an operator._"
    summary_label = ("Summary" if report["resolved_at"] or not op["resolution_summary"]
                     else "Summary (written at an earlier resolution; the incident was reopened)")
    flapping = f"yes, {report['flap_count']} recurrence(s)" if report["flapping"] else "no"
    recovered_line = report["recovered_at"] or (
        "unknown (no recovery alert was recorded)" if report.get("recovery_unknown")
        else "not confirmed")
    lines = [
        f"# {report['title']}",
        "",
        f"> {report['data_label']}.",
        "",
        f"- **Severity:** {report['severity']}",
        f"- **Status:** {life['label']}",
        f"- **Detected:** {report['detected_at']}",
        f"- **Monitoring recovered:** {recovered_line}",
        f"- **Resolved:** {report['resolved_at'] or 'not resolved'}",
        f"- **Flapping:** {flapping}",
        "",
        "## 1. Summary (observed by NetMonLT)",
        obs["summary"],
        "",
        "## 2. Investigation timeline",
    ]
    for item in obs["timeline"]:
        who = f" ({item['actor']})" if item.get("actor") else ""
        origin = "NetMonLT" if item["source"] == SRC_MONITORING else "operator"
        text = (item.get("text") or "").replace("\n", " ")
        line = f"- {item['time']} [{origin}] {item['label']}{who}"
        lines.append(line + (f": {text}" if text else ""))
    lines += [
        "",
        "## 3. Monitoring evidence (observed)",
        f"- ICMP checks: {ev.get('checks_total', 0)} "
        f"({ev.get('checks_down', 0)} down, {ev.get('checks_degraded', 0)} degraded, "
        f"{ev.get('checks_up', 0)} up)",
        f"- Probes: {ev.get('probes_sent', 0)} sent, {ev.get('probes_received', 0)} answered "
        f"(loss {ev.get('loss_pct')}%)",
        f"- First unhealthy check: {fmt_ts(ev.get('first_non_up_ts')) or 'n/a'}",
        f"- Time from detection to confirmed recovery: "
        f"{fmt_duration(ev.get('time_to_recovery_s')) or 'not recovered'}",
        "",
        "## 4. Automated monitoring actions",
        *[f"- {a}" for a in obs["automated_actions"]],
        "",
        "## 5. Recovery verification",
        f"- Observed: {obs['recovery_verification']}",
        f"- Operator: {_block(op['recovery_verification'], unset)}",
        "",
        "## 6. Impact assessment",
        f"- Observed: {obs['impact']}",
        f"- Operator: {_block(op['impact_assessment'], unset)}",
        "",
        f"## 7. Root cause (operator-entered, status: {op['root_cause_status']})",
        _block(op["root_cause"], unset),
        "",
        "## 8. Actions taken (operator-entered)",
        _block(op["actions_taken"], unset),
        "",
        "## 9. Resolution",
        f"- Resolved at: {report['resolved_at'] or 'not resolved'}",
        *(["- Monitoring status: recovery unknown. NetMonLT never recorded a recovery for this "
           "fault; the resolution below is the operator's statement, not a monitoring "
           "confirmation."] if report.get("recovery_unknown") else []),
        f"- {summary_label}: {_block(op['resolution_summary'], unset)}",
        "",
        "## 10. Lessons learned and preventative actions (operator-entered)",
        _block(op["lessons_learned"], unset),
    ]
    example = report.get("simulated_example")
    if example:
        cause = example["known_scenario_cause"]
        lines += [
            "",
            f"## {example['label']}",
            f"**{example['notice']}**",
            "",
            "- Known scenario cause (defined by the simulator): "
            + (cause["text"] if cause else example["no_cause_note"]),
            "- Illustrative troubleshooting steps (fictional, not performed):",
            *[f"  - {s}" for s in example["illustrative_troubleshooting_steps"]],
            f"- Illustrative resolution summary: {example['illustrative_resolution_summary']}",
            "- Illustrative recommendations:",
            *[f"  - {s}" for s in example["illustrative_recommendations"]],
            "- Illustrative lessons learned:",
            *[f"  - {s}" for s in example["illustrative_lessons_learned"]],
        ]
    return "\n".join(lines) + "\n"
