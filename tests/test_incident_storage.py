"""Incident management storage and engine: built from the alert log, restart-safe, append-only.

Alerts are written the way monitoring writes them (Database.save_alert_state), so these tests
also show that incident processing only ever READS the alert log.
"""

import asyncio
import logging
import sqlite3

import pytest

from helpers import ScriptedProber, run_cycles
from netmonlt import incidents as inc
from netmonlt.alerts import Alert, AlertState, Severity
from netmonlt.domain import CheckResult, IncidentsConfig, PingResult, Status
from netmonlt.incident_engine import process_once, run_incidents_forever
from netmonlt.storage import Database

U, D, X = Status.UP, Status.DEGRADED, Status.DOWN
CFG = IncidentsConfig()  # 900 s correlation window, 2 recurrences make it "flapping"
T0 = 10_000.0


def raise_alert(db, ts, new, old=None, device="alpha"):
    """Store an alert exactly as the alert state machine does."""
    old = old or (U if new is not U else X)
    severity = {X: Severity.CRITICAL, D: Severity.WARNING, U: Severity.INFO}[new]
    alert = Alert(device, ts, severity, old, new, f"{device} went {new.value} at {ts}")
    db.save_alert_state(device, AlertState(status=new, since=ts, updated=ts), alert)


def process(db, cfg=CFG, *, now=T0 + 5000, simulated=False, run_started_ts=None):
    return process_once(db, cfg, simulated=simulated, run_started_ts=run_started_ts,
                        clock=lambda: now)


def only_incident(db):
    rows = db.list_incidents(view="all")
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
def synced(db, devices):
    db.sync_devices(devices)
    return db


def snapshot(db, table):
    return [tuple(r) for r in db._conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


# ------------------------------------------------------------------ opening and de-duplication
def test_a_down_alert_opens_one_open_incident(synced):
    raise_alert(synced, T0, X)
    assert process(synced) == 1
    i = only_incident(synced)
    assert (i.device, i.severity, i.opened_ts) == ("alpha", "critical", T0)
    assert (i.monitoring_state, i.review_state) == (inc.MON_ACTIVE, inc.REV_OPEN)
    assert inc.lifecycle(i)["code"] == "open" and inc.lifecycle(i)["in_active_view"]
    assert [e.kind for e in synced.incident_events(i.id)] == [inc.EV_OPENED]


def test_repeated_processing_never_duplicates_an_incident_or_event(synced):
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 60, U)
    assert process(synced) == 2
    for _ in range(5):
        assert process(synced) == 0
    i = only_incident(synced)
    assert [e.kind for e in synced.incident_events(i.id)] == [inc.EV_OPENED, inc.EV_RECOVERED]


def test_a_further_alert_in_the_same_episode_joins_and_raises_severity(synced):
    raise_alert(synced, T0, D, U)
    raise_alert(synced, T0 + 30, X, D)
    process(synced)
    i = only_incident(synced)
    assert i.severity == "critical" and i.flap_count == 0
    assert [e.kind for e in synced.incident_events(i.id)] == [inc.EV_OPENED, inc.EV_ALERT]


def test_severity_never_goes_down_within_an_incident(synced):
    raise_alert(synced, T0, X, U)
    raise_alert(synced, T0 + 30, D, X)  # improved to degraded, not recovered
    process(synced)
    assert only_incident(synced).severity == "critical"


# ------------------------------------------------------------------ recovery is not closure
def test_recovery_marks_monitoring_recovered_but_never_resolves(synced):
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 90, U)
    process(synced)
    i = only_incident(synced)
    assert (i.monitoring_state, i.recovered_ts) == (inc.MON_RECOVERED, T0 + 90)
    assert i.review_state == inc.REV_OPEN  # no person has looked at it
    life = inc.lifecycle(i)
    assert life["label"] == "Auto-recovered — awaiting review"
    assert life["awaiting_review"] and life["in_active_view"]
    assert i.acknowledged_by is None and i.resolved_by is None
    assert synced.incident_counts() == {"active": 1, "history": 0, "awaiting_review": 1}


def test_the_device_recovered_alert_is_unchanged_in_the_alert_log(synced):
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 90, U)
    before = snapshot(synced, "alerts")
    process(synced)
    assert snapshot(synced, "alerts") == before
    last = synced.recent_alerts()[0]
    assert (last["severity"], last["to_status"]) == ("info", "up")


def test_a_recovery_with_no_incident_is_ignored(synced):
    raise_alert(synced, T0, U, X)  # e.g. the fault predates incident management
    assert process(synced) == 1
    assert synced.list_incidents(view="all") == []


def test_acknowledged_then_recovered_keeps_both_facts_separate(synced):
    raise_alert(synced, T0, X)
    process(synced)
    i = only_incident(synced)
    synced.acknowledge_incident(i.id, "alice", "looking", T0 + 10)
    raise_alert(synced, T0 + 90, U)
    process(synced)
    i = only_incident(synced)
    assert (i.monitoring_state, i.review_state) == (inc.MON_RECOVERED, inc.REV_ACKNOWLEDGED)
    assert inc.lifecycle(i)["label"] == "Monitoring recovered — acknowledged"
    assert i.acknowledged_by == "alice" and i.acknowledged_ts == T0 + 10


# ------------------------------------------------------------------ correlation window
def recovered_incident(db, down=T0, up=T0 + 100):
    raise_alert(db, down, X)
    raise_alert(db, up, U)
    process(db)


def test_a_recurrence_exactly_at_the_window_boundary_joins_the_incident(synced):
    recovered_incident(synced)
    raise_alert(synced, T0 + 100 + CFG.correlation_window_s, X)
    process(synced)
    i = only_incident(synced)
    assert i.flap_count == 1 and i.monitoring_state == inc.MON_ACTIVE
    kinds = [e.kind for e in synced.incident_events(i.id)]
    assert kinds == [inc.EV_OPENED, inc.EV_RECOVERED, inc.EV_RECURRED]


def test_a_recurrence_just_after_the_window_is_a_new_incident(synced):
    recovered_incident(synced)
    raise_alert(synced, T0 + 100 + CFG.correlation_window_s + 0.001, X)
    process(synced)
    rows = synced.list_incidents(view="all")
    assert len(rows) == 2
    newest, oldest = rows[0], rows[1]
    assert newest.previous_incident_id == oldest.id and newest.flap_count == 0
    assert oldest.monitoring_state == inc.MON_RECOVERED  # the earlier one is left as it was


def test_the_window_is_configurable(synced):
    cfg = IncidentsConfig(correlation_window_s=60.0)
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 100, U)
    raise_alert(synced, T0 + 161, X)  # 61 s after recovery: outside a 60 s window
    process(synced, cfg)
    assert len(synced.list_incidents(view="all")) == 2


def test_flapping_is_grouped_and_flagged_with_every_alert_preserved(synced):
    raise_alert(synced, T0, X)
    for k in range(1, 4):  # recover then fail again, three times, each within the window
        raise_alert(synced, T0 + k * 120 - 60, U)
        raise_alert(synced, T0 + k * 120, X)
    raise_alert(synced, T0 + 500, U)
    process(synced)
    i = only_incident(synced)
    assert i.flap_count == 3 and inc.is_flapping(i, CFG)
    monitoring = [e for e in synced.incident_events(i.id) if e.source == inc.SRC_MONITORING]
    assert len(monitoring) == 8 == len(synced.recent_alerts(limit=50))
    assert [e.alert_id for e in monitoring] == sorted(e.alert_id for e in monitoring)
    assert synced.incident_alert_count(i.id) == 8


def test_one_recurrence_is_not_flapping_by_default_but_is_when_configured(synced):
    recovered_incident(synced)
    raise_alert(synced, T0 + 200, X)
    process(synced)
    i = only_incident(synced)
    assert not inc.is_flapping(i, CFG)
    assert inc.is_flapping(i, IncidentsConfig(flapping_recurrences=1))


def test_incidents_never_merge_across_devices(synced):
    raise_alert(synced, T0, X, device="alpha")
    raise_alert(synced, T0 + 1, X, device="beta")
    raise_alert(synced, T0 + 60, U, device="alpha")
    process(synced)
    by_device = {i.device: i for i in synced.list_incidents(view="all")}
    assert set(by_device) == {"alpha", "beta"}
    assert by_device["alpha"].monitoring_state == inc.MON_RECOVERED
    assert by_device["beta"].monitoring_state == inc.MON_ACTIVE  # alpha's recovery did not touch it


def test_incidents_never_merge_across_alert_types(synced):
    raise_alert(synced, T0, X)
    process(synced)
    with synced._lock, synced._conn:  # an incident of some other (future) type, still active
        synced._conn.execute("UPDATE incidents SET kind='other-type'")
    raise_alert(synced, T0 + 10, X, D)
    process(synced)
    assert sorted(i.kind for i in synced.list_incidents(view="all")) == [
        inc.KIND_ICMP, "other-type"]


def test_a_recurrence_after_closure_opens_a_new_incident(synced):
    recovered_incident(synced)
    first = only_incident(synced)
    synced.resolve_incident(first.id, "alice", "Replaced the faulty patch cable.", None, T0 + 150)
    raise_alert(synced, T0 + 200, X)  # well inside the window, but the incident is closed
    process(synced)
    rows = synced.list_incidents(view="all")
    assert len(rows) == 2
    new = next(i for i in rows if i.id != first.id)
    assert new.previous_incident_id == first.id and new.review_state == inc.REV_OPEN
    closed = synced.get_incident(first.id)
    assert closed.review_state == inc.REV_RESOLVED and closed.flap_count == 0
    assert [e.kind for e in synced.incident_events(first.id)][-1] == inc.EV_RESOLVED


# ------------------------------------------------------------------ restart safety and migration
def test_progress_survives_a_restart_without_duplicates(tmp_path):
    path = tmp_path / "netmonlt.db"
    from netmonlt.domain import Device, SimProfile

    devs = [Device("alpha", "192.0.2.1", "router", SimProfile())]
    db = Database(path)
    db.sync_devices(devs)
    raise_alert(db, T0, X)
    process(db)
    first = only_incident(db)
    db.acknowledge_incident(first.id, "alice", "on it", T0 + 5)
    db.add_incident_note(first.id, "alice", "checked the cable", T0 + 6)
    raise_alert(db, T0 + 60, U)  # arrives while the application is "down"
    db.close()

    db = Database(path)  # restart
    try:
        assert process(db) == 1  # only the alert it has not seen
        assert process(db) == 0
        i = only_incident(db)
        assert i.id == first.id and i.acknowledged_by == "alice"
        assert i.monitoring_state == inc.MON_RECOVERED
        kinds = [e.kind for e in db.incident_events(i.id)]
        assert kinds == [inc.EV_OPENED, inc.EV_ACK, inc.EV_NOTE, inc.EV_RECOVERED]
    finally:
        db.close()

    db = Database(path)  # and again: nothing changes
    try:
        assert process(db) == 0 and len(db.incident_events(first.id)) == 4
    finally:
        db.close()


def test_a_database_from_before_incidents_keeps_its_alerts_and_adds_no_incidents(tmp_path):
    from netmonlt.domain import Device, SimProfile

    path = tmp_path / "old.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 60, U)
    conn = db._conn
    for trigger in ("incident_events_no_update", "incident_events_no_delete",
                    "incidents_no_delete"):
        conn.execute(f"DROP TRIGGER {trigger}")
    for table in ("incident_events", "incidents", "incident_cursor"):  # the pre-Stage-10 schema
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    alerts_before = snapshot(db, "alerts")
    db.close()

    db = Database(path)  # the upgrade
    try:
        assert snapshot(db, "alerts") == alerts_before  # history untouched
        assert process(db) == 0 and db.list_incidents(view="all") == []
        raise_alert(db, T0 + 5000, X)  # new alerts are picked up from here on
        assert process(db) == 1
        assert only_incident(db).opened_ts == T0 + 5000
    finally:
        db.close()


def test_opening_a_database_twice_is_harmless(tmp_path):
    path = tmp_path / "twice.db"
    Database(path).close()
    Database(path).close()
    db = Database(path)
    try:
        assert db.incident_counts() == {"active": 0, "history": 0, "awaiting_review": 0}
    finally:
        db.close()


# ------------------------------------------------------------------ evidence is never destroyed
def test_events_are_append_only(synced):
    raise_alert(synced, T0, X)
    process(synced)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        synced._conn.execute("UPDATE incident_events SET text='edited'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        synced._conn.execute("DELETE FROM incident_events")
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        synced._conn.execute("DELETE FROM incidents")
    assert len(synced.incident_events(only_incident(synced).id)) == 1


def test_incidents_survive_removing_the_device_from_the_inventory(synced, devices):
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 60, U)
    process(synced)
    i = only_incident(synced)
    synced.sync_devices([devices[1]])  # alpha removed: its checks and alerts cascade away
    assert synced.get_incident(i.id).device == "alpha"
    assert len(synced.incident_events(i.id)) == 2
    synced.acknowledge_incident(i.id, "alice", None, T0 + 100)  # still workable


def test_evidence_is_counted_from_stored_checks_and_frozen_when_resolved(synced):
    prober = ScriptedProber({"alpha": [U] * 3 + [X] * 6 + [U] * 6, "beta": [U]})
    run_cycles(_devs(synced), prober, synced, 15)
    process(synced)
    i = only_incident(synced)
    assert i.monitoring_state == inc.MON_RECOVERED
    ev = synced.incident_evidence(i, T0 + 99999)
    assert ev["checks_down"] >= 3 and ev["probes_sent"] == 3 * ev["checks_total"]
    assert ev["probes_received"] < ev["probes_sent"] and len(ev["recovery_checks"]) == 3
    synced.resolve_incident(i.id, "alice", "Cable reseated; verified by ping.", None, T0 + 1)
    frozen = synced.incident_evidence(synced.get_incident(i.id), T0 + 123456)
    assert frozen == synced.get_incident(i.id).evidence
    assert frozen["checks_down"] == ev["checks_down"]
    assert frozen["window_end_ts"] == i.recovered_ts


def _devs(db):
    from netmonlt.domain import Device, SimProfile

    return [Device("alpha", "192.0.2.1", "router", SimProfile()),
            Device("beta", "192.0.2.2", "switch", SimProfile())]


def test_real_alert_pipeline_end_to_end_keeps_the_recovered_alert(synced):
    """Real state machine -> alerts -> incident; the recovery alert still exists as before."""
    prober = ScriptedProber({"alpha": [U] * 3 + [X] * 5 + [U] * 5, "beta": [U]})
    run_cycles(_devs(synced), prober, synced, 14)
    alerts = list(reversed(synced.recent_alerts()))
    assert [(a["severity"], a["to_status"]) for a in alerts] == [("critical", "down"),
                                                                  ("info", "up")]
    process(synced)
    i = only_incident(synced)
    assert (i.monitoring_state, i.review_state) == (inc.MON_RECOVERED, inc.REV_OPEN)
    assert i.opened_ts == alerts[0]["ts"] and i.recovered_ts == alerts[1]["ts"]


def test_processing_changes_no_monitoring_data(synced):
    prober = ScriptedProber({"alpha": [U] * 3 + [X] * 5 + [U] * 5, "beta": [U]})
    run_cycles(_devs(synced), prober, synced, 14)
    tables = ("checks", "alerts", "device_state", "devices")
    before = {t: snapshot(synced, t) for t in tables}
    process(synced)
    synced.acknowledge_incident(only_incident(synced).id, "alice", None, T0)
    assert {t: snapshot(synced, t) for t in tables} == before


# ------------------------------------------------------------------ operator transitions
def open_incident(db, recovered=False):
    raise_alert(db, T0, X)
    if recovered:
        raise_alert(db, T0 + 100, U)
    process(db)
    return only_incident(db)


def code(exc):
    return exc.value.code


def test_acknowledge_records_who_and_when_without_touching_monitoring_state(synced):
    i = open_incident(synced)
    after = synced.acknowledge_incident(i.id, "  alice ", "taking a look", T0 + 20)
    assert (after.review_state, after.acknowledged_by, after.acknowledged_ts) == (
        inc.REV_ACKNOWLEDGED, "alice", T0 + 20)
    assert after.monitoring_state == inc.MON_ACTIVE  # a person cannot change what monitoring sees
    event = synced.incident_events(i.id)[-1]
    assert (event.kind, event.source, event.actor, event.text) == (
        inc.EV_ACK, inc.SRC_OPERATOR, "alice", "taking a look")


def test_invalid_transitions_are_rejected_with_a_clear_code(synced):
    i = open_incident(synced)
    synced.acknowledge_incident(i.id, None, None, T0 + 1)
    with pytest.raises(inc.IncidentError) as exc:
        synced.acknowledge_incident(i.id, None, None, T0 + 2)  # twice
    assert code(exc) == inc.IncidentError.INVALID_TRANSITION
    with pytest.raises(inc.IncidentError) as exc:
        synced.resolve_incident(i.id, None, "A perfectly good summary.", None, T0 + 3)  # active
    assert code(exc) == inc.IncidentError.INVALID_TRANSITION and "recovery" in exc.value.message
    with pytest.raises(inc.IncidentError) as exc:
        synced.reopen_incident(i.id, None, "because I want to", T0 + 4)  # not resolved
    assert code(exc) == inc.IncidentError.INVALID_TRANSITION
    assert synced.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED  # nothing changed


def test_an_unrecovered_fault_cannot_be_hidden_by_resolving_it(synced):
    i = open_incident(synced)
    with pytest.raises(inc.IncidentError):
        synced.resolve_incident(i.id, None, "Fixed it, honest, definitely.", None, T0 + 1)
    assert [x.id for x in synced.list_incidents(view="active")] == [i.id]


def test_resolving_requires_a_real_summary_and_a_recovered_fault(synced):
    i = open_incident(synced, recovered=True)
    for bad in (None, "", "   ", "short", "x" * 4001):
        with pytest.raises(inc.IncidentError) as exc:
            synced.resolve_incident(i.id, None, bad, None, T0 + 200)
        assert code(exc) == inc.IncidentError.INVALID_INPUT
    done = synced.resolve_incident(i.id, "alice", "Power supply swapped; link stable.", None,
                                   T0 + 300)
    assert (done.review_state, done.resolved_by, done.resolved_ts) == (
        inc.REV_RESOLVED, "alice", T0 + 300)
    assert done.monitoring_state == inc.MON_RECOVERED and done.recovered_ts == T0 + 100
    assert done.report["resolution_summary"] == "Power supply swapped; link stable."
    assert not inc.lifecycle(done)["in_active_view"]
    assert synced.list_incidents(view="active") == []
    assert len(synced.list_incidents(view="history")) == 1


def test_resolve_twice_and_acknowledge_when_resolved_are_rejected(synced):
    i = open_incident(synced, recovered=True)
    synced.resolve_incident(i.id, None, "Resolved after checking the uplink.", None, T0 + 300)
    with pytest.raises(inc.IncidentError) as exc:
        synced.resolve_incident(i.id, None, "Resolved after checking the uplink.", None, T0 + 301)
    assert code(exc) == inc.IncidentError.INVALID_TRANSITION
    with pytest.raises(inc.IncidentError) as exc:
        synced.acknowledge_incident(i.id, None, None, T0 + 302)
    assert code(exc) == inc.IncidentError.INVALID_TRANSITION


def test_reopen_returns_it_to_the_active_view_and_keeps_the_whole_history(synced):
    i = open_incident(synced, recovered=True)
    synced.resolve_incident(i.id, "alice", "Replaced the switch port cable.", None, T0 + 300)
    with pytest.raises(inc.IncidentError) as exc:
        synced.reopen_incident(i.id, None, "no", T0 + 310)  # reason too short
    assert code(exc) == inc.IncidentError.INVALID_INPUT
    again = synced.reopen_incident(i.id, "bob", "It dropped again on the same port.", T0 + 400)
    assert (again.review_state, again.reopened_count, again.resolved_ts) == (
        inc.REV_OPEN, 1, None)
    assert inc.lifecycle(again)["label"] == "Reopened — monitoring recovered"
    assert inc.lifecycle(again)["in_active_view"]
    kinds = [e.kind for e in synced.incident_events(i.id)]
    assert kinds[-3:] == [inc.EV_REPORT, inc.EV_RESOLVED, inc.EV_REOPENED]
    assert again.report["resolution_summary"] == "Replaced the switch port cable."  # kept


def test_notes_are_stored_cleaned_and_attributed(synced):
    i = open_incident(synced)
    event = synced.add_incident_note(i.id, "alice", "  Checked‮ the\x00 cable \r\nok  ", T0 + 9)
    assert event.text == "Checked the cable \nok" and event.actor == "alice"
    assert event.source == inc.SRC_OPERATOR
    for bad in ("", "   ", "x" * (inc.MAX_NOTE_LEN + 1), 5):
        with pytest.raises(inc.IncidentError):
            synced.add_incident_note(i.id, None, bad, T0 + 10)
    with pytest.raises(inc.IncidentError) as exc:
        synced.add_incident_note(i.id, "bad/actor<script>", "hello", T0 + 10)
    assert code(exc) == inc.IncidentError.INVALID_INPUT
    with pytest.raises(inc.IncidentError) as exc:
        synced.add_incident_note(9999, None, "hello", T0 + 10)
    assert code(exc) == inc.IncidentError.NOT_FOUND


def test_operator_markup_is_stored_verbatim_as_text(synced):
    i = open_incident(synced)
    payload = '<img src=x onerror="alert(1)"> & <script>alert(2)</script>'
    event = synced.add_incident_note(i.id, "alice", payload, T0 + 9)
    assert event.text == payload  # stored as typed; the UI only ever shows it as text


def test_write_up_updates_validate_fields_and_do_not_overclaim_a_root_cause(synced):
    i = open_incident(synced)
    out = synced.update_incident_report(
        i.id, "alice", {"root_cause": "Loose cable", "root_cause_status": "suspected"}, T0 + 5)
    assert out.report == {"root_cause": "Loose cable", "root_cause_status": "suspected"}
    with pytest.raises(inc.IncidentError):  # confirmed with nothing stated
        synced.update_incident_report(i.id, None, {"root_cause_status": "confirmed",
                                                   "root_cause": ""}, T0 + 6)
    for bad in ({"root_cause_status": "certain"}, {"surprise": "x"}, {"root_cause": 5}):
        with pytest.raises(inc.IncidentError):
            synced.update_incident_report(i.id, None, bad, T0 + 7)
    confirmed = synced.update_incident_report(
        i.id, None, {"root_cause_status": "confirmed"}, T0 + 8)
    assert confirmed.report["root_cause_status"] == "confirmed"
    # An unchanged value is not recorded as a change.
    before = len(synced.incident_events(i.id))
    synced.update_incident_report(i.id, None, {"root_cause": "Loose cable"}, T0 + 9)
    assert len(synced.incident_events(i.id)) == before


def test_every_operator_action_is_timestamped_in_order(synced):
    i = open_incident(synced, recovered=True)
    synced.acknowledge_incident(i.id, "alice", None, T0 + 110)
    synced.add_incident_note(i.id, "alice", "port looked fine", T0 + 120)
    synced.resolve_incident(i.id, "alice", "Nothing to fix: transient; monitored.", None, T0 + 130)
    events = synced.incident_events(i.id)
    assert [e.ts for e in events] == sorted(e.ts for e in events)
    assert [e.kind for e in events] == [
        inc.EV_OPENED, inc.EV_RECOVERED, inc.EV_ACK, inc.EV_NOTE, inc.EV_REPORT, inc.EV_RESOLVED]


# ------------------------------------------------------------------ listing and filters
def test_active_and_history_views_and_filters(synced):
    raise_alert(synced, T0, X, device="alpha")
    raise_alert(synced, T0 + 1, D, device="beta")
    raise_alert(synced, T0 + 50, U, device="alpha")
    process(synced)
    alpha = next(i for i in synced.list_incidents(view="all") if i.device == "alpha")
    beta = next(i for i in synced.list_incidents(view="all") if i.device == "beta")
    synced.acknowledge_incident(beta.id, None, None, T0 + 60)
    ids = lambda **kw: sorted(i.device for i in synced.list_incidents(**kw))  # noqa: E731
    assert ids() == ["alpha", "beta"]
    assert ids(status="acknowledged") == ["beta"]
    assert ids(status="recovered") == ["alpha"]
    assert ids(status="open") == ["alpha"]
    assert ids(severity="critical") == ["alpha"] and ids(severity="warning") == ["beta"]
    assert ids(device="beta") == ["beta"]
    synced.resolve_incident(alpha.id, None, "Transient; recovered by itself.", None, T0 + 70)
    assert ids(view="active") == ["beta"] and ids(view="history") == ["alpha"]
    assert ids(view="history", status="resolved") == ["alpha"] and ids(status="resolved") == []
    assert ids(view="all") == ["alpha", "beta"]


# ------------------------------------------------------------------ the background task
def test_the_engine_survives_errors_and_logs_only_the_exception_type(synced, monkeypatch, caplog):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise sqlite3.OperationalError("secret-path /home/me/private.db")

    monkeypatch.setattr(synced, "process_new_alerts", boom)

    async def run():
        task = asyncio.create_task(run_incidents_forever(
            synced, CFG, simulated=False, run_started_ts=None, interval_s=0.01))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())
    assert len(calls) >= 2  # it kept retrying
    assert "OperationalError" in caplog.text and "secret-path" not in caplog.text


def test_simulated_incidents_remember_the_run_that_raised_them(synced):
    raise_alert(synced, T0, X)
    process(synced, simulated=True, run_started_ts=T0 - 20)
    i = only_incident(synced)
    assert i.simulated is True and i.run_started_ts == T0 - 20


def test_checks_without_alerts_create_no_incidents(synced):
    ping = PingResult(3, 3, 1.0, 1.0, 1.0)
    synced.record_check(CheckResult("alpha", 1.0, Status.UP, ping))
    assert process(synced) == 0 and synced.list_incidents(view="all") == []


def test_notes_resolution_and_write_up_persist_across_restarts(tmp_path):
    from netmonlt.domain import Device, SimProfile

    path = tmp_path / "persist.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    process(db)
    i = only_incident(db)
    db.acknowledge_incident(i.id, "alice", "looking", T0 + 100)
    db.add_incident_note(i.id, "alice", "Checked the patch panel.\nPort looks fine.", T0 + 110)
    db.update_incident_report(i.id, "alice", {
        "root_cause": "Suspected loose cable", "root_cause_status": "suspected",
        "actions_taken": "Reseated the cable.", "lessons_learned": "Label the patch panel."},
        T0 + 120)
    db.resolve_incident(i.id, "alice", "Reseated the cable; link stable for an hour.",
                        {"impact_assessment": "Two users briefly offline."}, T0 + 130)
    db.close()

    db = Database(path)  # restart
    try:
        process(db)
        got = only_incident(db)
        assert got.review_state == inc.REV_RESOLVED and got.resolved_by == "alice"
        assert got.report == {
            "root_cause": "Suspected loose cable", "root_cause_status": "suspected",
            "actions_taken": "Reseated the cable.", "lessons_learned": "Label the patch panel.",
            "impact_assessment": "Two users briefly offline.",
            "resolution_summary": "Reseated the cable; link stable for an hour."}
        notes = [e.text for e in db.incident_events(got.id) if e.kind == inc.EV_NOTE]
        assert notes == ["Checked the patch panel.\nPort looks fine."]
        assert db.list_incidents(view="history") == [got]
    finally:
        db.close()
