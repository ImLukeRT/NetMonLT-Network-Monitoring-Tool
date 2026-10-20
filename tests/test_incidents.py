"""Incident logic that needs no database: lifecycle labels, correlation, input cleaning, and the
write-up (real versus simulated, observed versus operator versus illustrative)."""

import dataclasses
import re

import pytest

from netmonlt import incidents as inc
from netmonlt.domain import IncidentsConfig, SimProfile
from netmonlt.inventory import InventoryError, parse_inventory

CFG = IncidentsConfig()
T0 = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC


def make(**kw):
    base = dict(
        id=7, device="alpha", kind=inc.KIND_ICMP, severity="critical", opened_ts=T0,
        last_alert_ts=T0, monitoring_state=inc.MON_ACTIVE, recovered_ts=None, flap_count=0,
        review_state=inc.REV_OPEN, acknowledged_ts=None, acknowledged_by=None, resolved_ts=None,
        resolved_by=None, reopened_count=0, previous_incident_id=None, simulated=False,
        run_started_ts=None,
    )
    return inc.Incident(**{**base, **kw})


def event(kind, ts, **kw):
    base = dict(id=1, incident_id=7, ts=ts, source=inc.SRC_MONITORING, kind=kind)
    return inc.IncidentEvent(**{**base, **kw})


# ------------------------------------------------------------------ lifecycle labels
@pytest.mark.parametrize("kw, label, active, awaiting", [
    ({}, "Open", True, False),
    ({"monitoring_state": inc.MON_RECOVERED, "recovered_ts": T0 + 60},
     "Auto-recovered — awaiting review", True, True),
    ({"review_state": inc.REV_ACKNOWLEDGED}, "Acknowledged", True, False),
    ({"review_state": inc.REV_ACKNOWLEDGED, "monitoring_state": inc.MON_RECOVERED},
     "Monitoring recovered — acknowledged", True, False),
    ({"review_state": inc.REV_RESOLVED, "monitoring_state": inc.MON_RECOVERED},
     "Resolved", False, False),
    ({"reopened_count": 1}, "Reopened", True, False),
    ({"reopened_count": 1, "monitoring_state": inc.MON_RECOVERED},
     "Reopened — monitoring recovered", True, True),
])
def test_lifecycle_labels_keep_monitoring_and_review_apart(kw, label, active, awaiting):
    life = inc.lifecycle(make(**kw))
    assert (life["label"], life["in_active_view"], life["awaiting_review"]) == (
        label, active, awaiting)
    assert life["monitoring"] == kw.get("monitoring_state", inc.MON_ACTIVE)
    assert life["review"] == kw.get("review_state", inc.REV_OPEN)


def test_a_recovered_incident_is_never_labelled_resolved_until_a_person_resolves_it():
    recovered = make(monitoring_state=inc.MON_RECOVERED, recovered_ts=T0 + 60)
    assert inc.lifecycle(recovered)["code"] != "resolved"


# ------------------------------------------------------------------ correlation
def test_match_prefers_the_active_incident_and_ignores_unrelated_ones():
    active = make(id=2)
    old = make(id=1, monitoring_state=inc.MON_RECOVERED, recovered_ts=T0)
    assert inc.match_incident([active, old], T0 + 5, False, 900).id == 2
    assert inc.match_incident([], T0, False, 900) is None


@pytest.mark.parametrize("gap, joins", [(0, True), (899.999, True), (900, True),
                                        (900.001, False), (-1, False)])
def test_match_window_boundary_is_inclusive(gap, joins):
    recovered = make(monitoring_state=inc.MON_RECOVERED, recovered_ts=T0)
    found = inc.match_incident([recovered], T0 + gap, False, 900)
    assert (found is not None) is joins


def test_a_recovery_alert_only_ever_ends_an_active_incident():
    recovered = make(monitoring_state=inc.MON_RECOVERED, recovered_ts=T0)
    assert inc.match_incident([recovered], T0 + 10, True, 900) is None
    active = make(id=3)
    assert inc.match_incident([recovered, active], T0 + 10, True, 900).id == 3


def test_worse_picks_the_more_severe_level():
    assert inc.worse("warning", "critical") == "critical"
    assert inc.worse("critical", "info") == "critical"
    assert inc.worse("info", "warning") == "warning"


# ------------------------------------------------------------------ operator input
def test_clean_text_keeps_text_and_removes_control_and_bidi_characters():
    assert inc.clean_text("  a‮b\x00c\r\nd\te  ", "n", max_len=50) == "abc\nd\te"
    assert inc.clean_text(None, "n", max_len=5) == ""
    assert inc.clean_text("<b>x</b>", "n", max_len=50) == "<b>x</b>"  # markup is data, not removed


@pytest.mark.parametrize("value, kw", [
    ("x" * 11, {"max_len": 10}), ("abc", {"max_len": 10, "min_len": 5}),
    ("   ", {"max_len": 10, "min_len": 1}), (123, {"max_len": 10}), (["a"], {"max_len": 10}),
])
def test_clean_text_rejects_bad_input(value, kw):
    with pytest.raises(inc.IncidentError) as exc:
        inc.clean_text(value, "field", **kw)
    assert exc.value.code == inc.IncidentError.INVALID_INPUT


@pytest.mark.parametrize("value", [None, ""])
def test_actor_defaults_when_not_given(value):
    assert inc.clean_actor(value) == inc.DEFAULT_ACTOR


@pytest.mark.parametrize("value", ["alice", "Bob Smith", "o'neil", "a.b-c_d@x", "x" * 40])
def test_reasonable_actor_labels_are_accepted(value):
    assert inc.clean_actor(value) == value


@pytest.mark.parametrize("value", ["<script>", "a/b", "x" * 41, "-lead", "line\nbreak", 5, "a;b"])
def test_odd_actor_labels_are_rejected(value):
    with pytest.raises(inc.IncidentError):
        inc.clean_actor(value)


# ------------------------------------------------------------------ evidence arithmetic
def rows(*specs):
    return [{"ts": ts, "status": st, "sent": 3, "received": rx, "rtt_avg_ms": ms}
            for ts, st, rx, ms in specs]


def test_summarize_counts_stored_checks_and_never_estimates():
    data = rows((0, "down", 0, None), (5, "down", 0, None), (10, "degraded", 2, 40.0),
                (15, "up", 3, 5.0), (20, "up", 3, 6.0), (25, "up", 3, 7.0), (30, "up", 3, 8.0))
    out = inc.summarize_checks(data, 5, 30, 30)
    assert (out["checks_total"], out["checks_down"], out["checks_degraded"], out["checks_up"]) == (
        7, 2, 1, 4)
    assert (out["probes_sent"], out["probes_received"], out["loss_pct"]) == (21, 14, 33.33)
    assert out["first_non_up_ts"] == 0 and out["last_non_up_ts"] == 10
    assert out["time_to_recovery_s"] == 25 and out["fault_span_s"] == 30
    assert [c["ts"] for c in out["recovery_checks"]] == [20, 25, 30]  # the confirming checks


def test_summarize_with_no_checks_gives_nulls_not_zeros_for_ratios():
    out = inc.summarize_checks([], T0, None, T0)
    assert out["checks_total"] == 0 and out["loss_pct"] is None
    assert out["time_to_recovery_s"] is None and out["recovery_checks"] == []


def test_formatting_is_deterministic_utc():
    assert inc.fmt_ts(T0) == "2023-11-14 22:13:20 UTC" and inc.fmt_ts(None) is None
    assert [inc.fmt_duration(s) for s in (0, 45, 60, 125, 3600, 3725, None)] == [
        "0 s", "45 s", "1 min 00 s", "2 min 05 s", "1 h 00 min", "1 h 02 min", None]
    assert inc.display_id(7) == "INC-0007"


# ------------------------------------------------------------------ the write-up
def timeline(simulated=False):
    return [
        event(inc.EV_OPENED, T0, severity="critical", from_status="up", to_status="down",
              alert_id=1, text="alpha went down"),
        event(inc.EV_RECOVERED, T0 + 90, severity="info", from_status="down", to_status="up",
              alert_id=2, text="alpha recovered"),
    ]


EVIDENCE = inc.summarize_checks(
    rows((T0 - 5, "down", 0, None), (T0, "down", 0, None), (T0 + 60, "up", 3, 5.0),
         (T0 + 70, "up", 3, 5.0), (T0 + 80, "up", 3, 5.0)),
    T0, T0 + 90, T0 + 90)
RECOVERED = dict(monitoring_state=inc.MON_RECOVERED, recovered_ts=T0 + 90)


def report_for(incident, sim=None, device_type="router", events=None, evidence=EVIDENCE):
    return inc.build_report(incident, events or timeline(), evidence, cfg=CFG,
                            device_type=device_type, sim=sim, poll_interval_s=5.0)


def test_a_real_report_has_observed_facts_and_blank_operator_fields_only():
    rep = report_for(make(**RECOVERED))
    assert rep["simulated"] is False and rep["simulated_example"] is None
    assert rep["data_label"] == "Real monitoring data"
    assert rep["id"] == "INC-0007" and rep["detected_at"] == "2023-11-14 22:13:20 UTC"
    assert rep["recovered_at"] == "2023-11-14 22:14:50 UTC" and rep["resolved_at"] is None
    assert all(rep["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)
    assert rep["operator"]["root_cause_status"] == "unconfirmed"  # nothing guessed
    assert "1 min 30 s after detection" in rep["observed"]["summary"]
    text = inc.render_markdown(rep)
    assert text.count("_Not yet documented by an operator._") == 6
    assert "SIMULATED" not in text and "EXAMPLE" not in text
    assert "NetMonLT performed no repair" in text  # never claims it fixed anything


def test_a_real_report_never_invents_a_cause_even_with_a_simulation_profile_present():
    rep = report_for(make(**RECOVERED), sim=SimProfile(outage_start_s=0, outage_duration_s=50))
    assert rep["simulated_example"] is None
    assert "root cause" not in rep["observed"]["summary"].lower()


def test_operator_text_appears_only_in_the_operator_section():
    incident = make(**RECOVERED, report={"root_cause": "Loose patch cable",
                                         "root_cause_status": "suspected",
                                         "actions_taken": "Reseated the cable"})
    rep = report_for(incident)
    assert rep["operator"]["root_cause"] == "Loose patch cable"
    assert "Loose patch cable" not in str(rep["observed"])
    text = inc.render_markdown(rep)
    assert "status: suspected" in text and "Reseated the cable" in text


def test_the_markdown_carries_operator_markup_as_plain_text():
    payload = "<script>alert(1)</script>"
    rep = report_for(make(**RECOVERED, report={"actions_taken": payload}))
    assert payload in inc.render_markdown(rep)  # verbatim text; Markdown is shown as text too


def test_unrecovered_report_says_so_and_verifies_nothing():
    rep = report_for(make(), events=timeline()[:1])
    assert rep["recovered_at"] is None
    assert "has not confirmed recovery" in rep["observed"]["summary"]
    assert "Not yet recovered" in rep["observed"]["recovery_verification"]


def test_report_mentions_flapping_and_the_previous_incident():
    rep = report_for(make(**RECOVERED, flap_count=3, previous_incident_id=4))
    assert rep["flapping"] is True and "recurred 3 time(s)" in rep["observed"]["summary"]
    assert "INC-0004" in rep["observed"]["summary"]


def test_timeline_marks_who_acted_and_the_source():
    events = [*timeline(), event(inc.EV_ACK, T0 + 100, source=inc.SRC_OPERATOR, actor="alice",
                                 text="looking")]
    rep = report_for(make(**RECOVERED), events=events)
    last = rep["observed"]["timeline"][-1]
    assert (last["source"], last["actor"], last["text"]) == (inc.SRC_OPERATOR, "alice", "looking")
    assert "[operator] Acknowledged (alice): looking" in inc.render_markdown(rep)


def test_report_updates_show_the_changed_fields_not_raw_json():
    ev = event(inc.EV_REPORT, T0 + 5, source=inc.SRC_OPERATOR, actor="alice",
               text='{"root_cause": "x", "actions_taken": "y"}')
    assert inc.event_text(ev) == "updated actions_taken, root_cause"
    assert inc.event_text(dataclasses.replace(ev, text="not json")) is None


# ---- simulated incidents
def sim_report(sim, *, incident=None, device_type="switch"):
    incident = incident or make(**RECOVERED, simulated=True, run_started_ts=T0 - 10)
    return report_for(incident, sim=sim, device_type=device_type)


def test_a_simulated_report_is_clearly_labelled_and_deterministic():
    sim = SimProfile(outage_start_s=5, outage_duration_s=60)
    first, second = sim_report(sim), sim_report(sim)
    assert first == second
    assert first["data_label"].startswith("SIMULATED")
    example = first["simulated_example"]
    assert example["label"] == "SIMULATED EXAMPLE — AUTO-GENERATED"
    assert "fictional" in example["notice"] and "no real device" in example["notice"]
    assert all("EXAMPLE ONLY" in example[k] for k in ("illustrative_resolution_summary",))
    assert example["illustrative_troubleshooting_steps"][0].startswith("Check the switch")
    text = inc.render_markdown(first)
    assert "SIMULATED EXAMPLE — AUTO-GENERATED" in text and "fictional, not performed" in text
    assert "NetMonLT performed no repair" in text
    assert all(first["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)  # not pre-filled


def test_the_simulated_cause_is_stated_only_when_the_simulator_defines_one():
    scripted = sim_report(SimProfile(outage_start_s=5, outage_duration_s=60))
    cause = scripted["simulated_example"]["known_scenario_cause"]
    assert "outage_start_s=5" in cause["text"] and cause["source"] == "simulator profile"
    plain = sim_report(SimProfile())  # no outage, no loss, normal latency: nothing defined
    assert plain["simulated_example"]["known_scenario_cause"] is None
    assert "defines no cause" in plain["simulated_example"]["no_cause_note"]
    assert "defines no cause" in inc.render_markdown(plain)


def test_a_scripted_outage_is_not_blamed_for_an_incident_that_began_outside_it():
    late = make(**RECOVERED, simulated=True, run_started_ts=T0 - 1000)  # detected 1000 s in
    rep = sim_report(SimProfile(outage_start_s=5, outage_duration_s=60), incident=late)
    assert rep["simulated_example"]["known_scenario_cause"] is None


def test_simulated_loss_and_latency_profiles_can_be_the_defined_cause():
    lossy = sim_report(SimProfile(loss=0.4), incident=make(simulated=True, severity="warning"))
    assert "40% packet loss" in lossy["simulated_example"]["known_scenario_cause"]["text"]
    slow = sim_report(SimProfile(rtt_ms=500), incident=make(simulated=True, severity="warning"))
    assert "500 ms" in slow["simulated_example"]["known_scenario_cause"]["text"]


def test_an_unrecovered_simulated_incident_has_nothing_to_resolve_in_its_example():
    rep = sim_report(SimProfile(), incident=make(simulated=True))
    assert "nothing to resolve" in rep["simulated_example"]["illustrative_resolution_summary"]


def test_degraded_simulated_incidents_get_degradation_steps():
    rep = sim_report(SimProfile(loss=0.3), incident=make(simulated=True, severity="warning"))
    assert "errors, congestion or interference" in str(
        rep["simulated_example"]["illustrative_troubleshooting_steps"])


# ------------------------------------------------------------------ configuration
def parse(block):
    return parse_inventory({"devices": [{"name": "a", "address": "192.0.2.1"}],
                            **({} if block is None else {"incidents": block})}).incidents


def test_incident_configuration_is_optional_and_defaults_are_conservative():
    cfg = parse(None)
    assert cfg == IncidentsConfig() and cfg.enabled
    assert cfg.operator_actions is None  # not configured: on when simulated, off in real mode
    assert (cfg.correlation_window_s, cfg.flapping_recurrences) == (900.0, 2)
    assert parse({}) == IncidentsConfig()


def test_incident_configuration_can_be_overridden():
    cfg = parse({"enabled": True, "correlation_window_s": 300, "flapping_recurrences": 4,
                 "operator_actions": False})
    assert (cfg.correlation_window_s, cfg.flapping_recurrences, cfg.operator_actions) == (
        300.0, 4, False)
    assert parse({"enabled": False}).enabled is False


@pytest.mark.parametrize("block", [
    "yes", [], {"enabled": "yes"}, {"operator_actions": 1}, {"surprise": 1},
    {"correlation_window_s": 59}, {"correlation_window_s": 86401},
    {"correlation_window_s": "900"}, {"correlation_window_s": True},
    {"enabled": None}, {"operator_actions": "true"},
    {"flapping_recurrences": 0}, {"flapping_recurrences": 1.5}, {"flapping_recurrences": 101},
])
def test_bad_incident_configuration_is_rejected(block):
    with pytest.raises(InventoryError):
        parse(block)


def test_a_reopened_incident_labels_its_earlier_resolution_summary():
    kept = {"resolution_summary": "Replaced the cable."}
    reopened = inc.render_markdown(report_for(make(**RECOVERED, reopened_count=1, report=kept)))
    assert "Summary (written at an earlier resolution; the incident was reopened): " in reopened
    done = inc.render_markdown(report_for(make(
        **RECOVERED, review_state=inc.REV_RESOLVED, resolved_ts=T0 + 200, report=kept)))
    assert "- Summary: Replaced the cable." in done


# ---------------------------------------------------------- simulated reports never claim repairs
CLAIMS = re.compile(
    r"\b(repaired|fixed|replaced|restarted|rebooted|reconfigured|swapped|reseated|restored|"
    r"we|i)\b", re.IGNORECASE)


def example_text(rep):
    ex = rep["simulated_example"]
    parts = [ex["notice"], ex["no_cause_note"] or "", ex["illustrative_resolution_summary"],
             (ex["known_scenario_cause"] or {}).get("text", ""),
             *ex["illustrative_troubleshooting_steps"], *ex["illustrative_recommendations"],
             *ex["illustrative_lessons_learned"]]
    return "\n".join(parts)


@pytest.mark.parametrize("device_type", ["switch", "router", "firewall", "access-point",
                                         "server", "vpn-gateway", "host", "voip-gateway"])
@pytest.mark.parametrize("kw", [
    {"severity": "critical"}, {"severity": "warning"},
    {"severity": "critical", **RECOVERED}, {"severity": "critical", **RECOVERED, "flap_count": 3},
    {"severity": "critical", **RECOVERED, "review_state": inc.REV_RESOLVED,
     "resolved_ts": T0 + 300},
])
def test_simulated_example_text_never_claims_a_repair_or_an_operator_action(device_type, kw):
    rep = report_for(make(simulated=True, run_started_ts=T0 - 10, **kw), device_type=device_type,
                     sim=SimProfile(outage_start_s=5, outage_duration_s=60))
    text = example_text(rep)
    assert not CLAIMS.search(text), CLAIMS.search(text).group(0)
    # Every illustrative item is an instruction or an explicit example, never a past-tense deed.
    assert all(step.split()[0] in {"Check", "Review", "Compare", "Confirm", "Keep"}
               for step in rep["simulated_example"]["illustrative_troubleshooting_steps"])
    md = inc.render_markdown(rep)
    assert "fictional" in md and "NetMonLT performed no repair" in md
    # What NetMonLT itself did is limited to monitoring and bookkeeping.
    assert all(not CLAIMS.search(a) for a in rep["observed"]["automated_actions"])
    assert rep["operator"]["actions_taken"] == "" and rep["operator"]["root_cause"] == ""
