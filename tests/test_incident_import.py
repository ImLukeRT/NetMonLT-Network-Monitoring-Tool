"""Historical incident import: planning (pure), applying (one transaction), duplicates, restarts,
coexistence with live incidents, and honesty about incomplete history."""

import re
import sqlite3
import threading

import pytest

from incident_helpers import (
    CFG,
    T0,
    WINDOW,
    D,
    U,
    X,
    by_device,
    do_import,
    process,
    raise_alert,
    snapshot,
    treat_as_history,
)
from netmonlt import incidents as inc
from netmonlt.domain import Device, IncidentsConfig, SimProfile
from netmonlt.storage import Database


def A(id_, device, ts, new, old=None, severity=None, message=None):
    sev = severity or {"down": "critical", "degraded": "warning", "up": "info"}[new.value]
    old = old or (U if new is not U else X)
    return inc.HistoricalAlert(id_, device, ts, sev, old.value, new.value,
                               message or f"{device} went {new.value}")


def plan(alerts, window=WINDOW):
    return inc.plan_import(alerts, window_s=window)


def shape(p):
    return [(d.device, d.monitoring_state, d.severity, d.flap_count,
             [(a.id, k) for a, k in d.events]) for d in p.drafts]


@pytest.fixture
def synced(db, devices):
    db.sync_devices(devices)
    return db


# ------------------------------------------------------------------ the pure planner
def test_a_down_then_up_becomes_one_recovered_incident_with_original_alerts():
    p = plan([A(1, "alpha", T0, X), A(2, "alpha", T0 + 90, U)])
    assert shape(p) == [("alpha", inc.MON_RECOVERED, "critical", 0,
                         [(1, inc.EV_OPENED), (2, inc.EV_RECOVERED)])]
    d = p.drafts[0]
    assert (d.opened_ts, d.recovered_ts, d.last_alert_ts) == (T0, T0 + 90, T0 + 90)
    assert p.skipped == () and p.considered == 2


def test_degraded_then_down_then_up_is_one_incident_at_the_worst_severity():
    p = plan([A(1, "alpha", T0, D, severity="warning"),
              A(2, "alpha", T0 + 30, X, D), A(3, "alpha", T0 + 60, U)])
    assert shape(p) == [("alpha", inc.MON_RECOVERED, "critical", 0,
                         [(1, inc.EV_OPENED), (2, inc.EV_ALERT), (3, inc.EV_RECOVERED)])]


@pytest.mark.parametrize("gap, joins", [(WINDOW, True), (WINDOW - 0.001, True),
                                        (WINDOW + 0.001, False)])
def test_the_correlation_window_boundary_matches_live_processing(gap, joins):
    p = plan([A(1, "alpha", T0, X), A(2, "alpha", T0 + 100, U), A(3, "alpha", T0 + 100 + gap, X)])
    assert len(p.drafts) == (1 if joins else 2)
    if joins:
        assert p.drafts[0].flap_count == 1
        assert [k for _, k in p.drafts[0].events][-1] == inc.EV_RECURRED


def test_a_fault_with_no_recovery_is_recovery_unknown_not_down_and_not_recovered():
    p = plan([A(1, "alpha", T0, X)])
    d = p.drafts[0]
    assert (d.monitoring_state, d.recovered_ts) == (inc.MON_UNKNOWN, None)
    assert d.last_alert_ts == T0


def test_a_recurrence_that_never_recovered_is_unknown_and_keeps_its_flap_count():
    p = plan([A(1, "alpha", T0, X), A(2, "alpha", T0 + 60, U), A(3, "alpha", T0 + 120, X)])
    assert shape(p) == [("alpha", inc.MON_UNKNOWN, "critical", 1,
                         [(1, inc.EV_OPENED), (2, inc.EV_RECOVERED), (3, inc.EV_RECURRED)])]


def test_a_recovery_with_no_fault_before_it_is_skipped_with_a_reason_not_invented():
    p = plan([A(1, "alpha", T0, U, X), A(2, "alpha", T0 + 10, X), A(3, "alpha", T0 + 20, U),
              A(4, "alpha", T0 + 25, U, X)])  # a second recovery for the same fault
    assert p.skipped == ((1, inc.SKIP_RECOVERY_WITHOUT_FAULT), (4, inc.SKIP_RECOVERY_WITHOUT_FAULT))
    assert shape(p) == [("alpha", inc.MON_RECOVERED, "critical", 0,
                         [(2, inc.EV_OPENED), (3, inc.EV_RECOVERED)])]
    assert p.summary()["alerts_to_skip"] == 2


def test_devices_are_never_merged_and_input_order_does_not_matter():
    alerts = [A(1, "alpha", T0, X), A(2, "beta", T0 + 1, X), A(3, "alpha", T0 + 60, U)]
    p = plan(alerts)
    assert sorted(d.device for d in p.drafts) == ["alpha", "beta"]
    assert {d.device: d.monitoring_state for d in p.drafts} == {
        "alpha": inc.MON_RECOVERED, "beta": inc.MON_UNKNOWN}
    assert shape(plan(list(reversed(alerts)))) == shape(p)


def test_severity_message_and_transitions_are_carried_unchanged():
    alerts = [A(1, "alpha", T0, X, message="alpha is DOWN: no reply to 3 pings (was up for 30s)")]
    (alert, kind), = plan(alerts).drafts[0].events
    assert alert is alerts[0] and kind == inc.EV_OPENED
    assert (alert.severity, alert.from_status, alert.to_status) == ("critical", "up", "down")


def test_the_fingerprint_identifies_the_exact_alerts_and_rule():
    base = plan([A(1, "alpha", T0, X)])
    assert plan([A(1, "alpha", T0, X)]).fingerprint == base.fingerprint
    assert plan([A(1, "alpha", T0, X), A(2, "alpha", T0 + 9, U)]).fingerprint != base.fingerprint
    assert plan([A(1, "alpha", T0, X)], window=60).fingerprint != base.fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", base.fingerprint)


def test_an_empty_history_plans_nothing():
    p = plan([])
    assert (p.drafts, p.skipped, p.considered) == ((), (), 0)


def test_unknown_incidents_never_match_a_live_alert():
    unknown = inc.Incident(
        id=1, device="alpha", kind=inc.KIND_ICMP, severity="critical", opened_ts=T0,
        last_alert_ts=T0, monitoring_state=inc.MON_UNKNOWN, recovered_ts=T0 + 5, flap_count=1,
        review_state=inc.REV_OPEN, acknowledged_ts=None, acknowledged_by=None, resolved_ts=None,
        resolved_by=None, reopened_count=0, previous_incident_id=None, simulated=False,
        run_started_ts=None)
    assert inc.match_incident([unknown], T0 + 10, False, WINDOW) is None
    assert inc.match_incident([unknown], T0 + 10, True, WINDOW) is None


def test_the_plan_equals_what_live_processing_would_have_made(db, devices):
    """The same alerts, processed live in one database and imported in another, group the same."""
    db.sync_devices(devices)
    seq = [(0, "alpha", X), (60, "alpha", U), (100, "alpha", X), (500, "alpha", U),
           (3000, "alpha", D), (3100, "alpha", U), (10, "beta", X), (20, "beta", D),
           (90, "beta", U), (4000, "beta", X)]
    for ts, device, st in sorted(seq):
        raise_alert(db, T0 + ts, st, device=device)
    process(db)
    live = {(i.device, i.opened_ts): (i.flap_count, i.severity, i.monitoring_state)
            for i in db.list_incidents(view="all")}

    other = Database(":memory:")
    try:
        other.sync_devices(devices)
        for ts, device, st in sorted(seq):
            raise_alert(other, T0 + ts, st, device=device)
        treat_as_history(other)
        do_import(other)
        imported = {(i.device, i.opened_ts): (i.flap_count, i.severity, i.monitoring_state)
                    for i in other.list_incidents(view="all")}
    finally:
        other.close()
    # the only difference allowed: a fault still "active" live is "recovery unknown" imported
    assert {k: (f, s, "active" if m == inc.MON_UNKNOWN else m)
            for k, (f, s, m) in imported.items()} == live


# ------------------------------------------------------------------ applying an import
def history(db):
    """alpha: down then up; beta: degraded, no recovery; plus a stray recovery for alpha."""
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    raise_alert(db, T0 + 200, D, U, device="beta")
    treat_as_history(db)


def test_importing_creates_open_review_incidents_from_the_original_alerts(synced):
    history(synced)
    assert process(synced) == 0  # live processing ignores history
    alerts_before = snapshot(synced, "alerts")
    result = do_import(synced, actor="alice")
    assert (result["incidents_created"], result["alerts_linked"], result["alerts_skipped"]) == (
        2, 3, 0)
    assert snapshot(synced, "alerts") == alerts_before  # every alert unchanged
    found = by_device(synced)
    alpha, beta = found["alpha"][0], found["beta"][0]
    assert alpha.origin == inc.ORIGIN_IMPORTED and alpha.import_id == result["import_id"]
    assert (alpha.monitoring_state, alpha.review_state) == (inc.MON_RECOVERED, inc.REV_OPEN)
    assert (alpha.opened_ts, alpha.recovered_ts) == (T0, T0 + 90)
    assert inc.lifecycle(alpha)["label"] == "Auto-recovered — awaiting review"
    assert (beta.monitoring_state, beta.review_state, beta.recovered_ts) == (
        inc.MON_UNKNOWN, inc.REV_OPEN, None)
    assert inc.lifecycle(beta)["label"] == "Recovery unknown — awaiting review"
    assert beta.severity == "warning" and beta.simulated is not False  # simulated DB: simulated


def test_imported_events_keep_every_original_alert_field_and_add_one_import_note(synced):
    history(synced)
    do_import(synced, actor="alice", now=T0 + 9000)
    alpha = by_device(synced)["alpha"][0]
    events = synced.incident_events(alpha.id)
    originals = {r["id"]: dict(r) for r in synced._conn.execute("SELECT * FROM alerts")}
    linked = [e for e in events if e.alert_id is not None]
    assert [e.kind for e in linked] == [inc.EV_OPENED, inc.EV_RECOVERED]
    for e in linked:
        a = originals[e.alert_id]
        assert (e.ts, e.severity, e.from_status, e.to_status, e.text, e.source) == (
            a["ts"], a["severity"], a["from_status"], a["to_status"], a["message"],
            inc.SRC_MONITORING)
    note = events[-1]
    assert (note.kind, note.ts, note.actor, note.alert_id) == (inc.EV_IMPORTED, T0 + 9000,
                                                               "alice", None)
    assert "import #" in note.text
    assert synced.incident_alert_count(alpha.id) == 2  # the import note is not an alert


def test_nothing_is_invented_operator_fields_blank_and_no_fabricated_checks(synced):
    history(synced)
    do_import(synced)
    for i in synced.list_incidents(view="all"):
        assert i.report == {} and i.acknowledged_by is None and i.resolved_by is None
        assert not [e for e in synced.incident_events(i.id) if e.source == inc.SRC_OPERATOR]
        assert i.evidence["checks_total"] == 0  # no stored checks, so none are counted
    assert synced._conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0


def test_evidence_counts_only_checks_that_are_really_stored(synced):
    from netmonlt.domain import CheckResult, PingResult, Status

    history(synced)
    for ts, st, rx in [(T0 - 5, Status.DOWN, 0), (T0, Status.DOWN, 0), (T0 + 60, Status.UP, 3),
                       (T0 + 70, Status.UP, 3), (T0 + 80, Status.UP, 3)]:
        synced.record_check(CheckResult("alpha", ts, st, PingResult(3, rx, None, None, None)))
    do_import(synced)
    ev = by_device(synced)["alpha"][0].evidence
    assert (ev["checks_total"], ev["checks_down"], ev["checks_up"]) == (5, 2, 3)
    assert ev["recovered_ts"] == T0 + 90


def test_repeating_an_import_adds_nothing(synced):
    history(synced)
    first = do_import(synced)
    state = (snapshot(synced, "incidents"), snapshot(synced, "incident_events"))
    again = do_import(synced)
    assert again == {"import_id": None, "incidents_created": 0, "alerts_linked": 0,
                     "alerts_skipped": 0}
    assert (snapshot(synced, "incidents"), snapshot(synced, "incident_events")) == state
    assert synced.import_candidate_count() == 0
    assert synced.preview_import(CFG).considered == 0
    assert first["incidents_created"] == 2


def test_a_preview_changes_nothing(synced):
    history(synced)
    before = [snapshot(synced, t) for t in ("alerts", "incidents", "incident_events")]
    plan_ = synced.preview_import(CFG)
    assert plan_.summary()["incidents_to_create"] == 2
    assert [snapshot(synced, t) for t in ("alerts", "incidents", "incident_events")] == before
    assert synced._conn.execute("SELECT COUNT(*) FROM incident_imports").fetchone()[0] == 0


def test_a_stale_preview_cannot_be_confirmed(synced):
    history(synced)
    stale = synced.preview_import(CFG).fingerprint
    raise_alert(synced, T0 + 500, U, device="beta")  # then something changes: an orphan recovery
    process(synced)  # live processing passes it by (no live incident), so it becomes history
    with pytest.raises(inc.IncidentError) as exc:
        synced.apply_import(CFG, simulated=True, actor="alice", fingerprint=stale, now=T0 + 1)
    assert exc.value.code == inc.IncidentError.STALE
    assert synced.list_incidents(view="all") == []  # nothing was created


def test_unplaceable_recoveries_are_recorded_as_skipped_and_never_offered_again(synced):
    raise_alert(synced, T0, U, X)  # a recovery with no recorded fault
    raise_alert(synced, T0 + 10, X)
    raise_alert(synced, T0 + 20, U)
    treat_as_history(synced)
    result = do_import(synced)
    assert (result["incidents_created"], result["alerts_skipped"]) == (1, 1)
    skip = synced._conn.execute("SELECT * FROM incident_import_skips").fetchall()
    assert [(r["alert_id"], r["reason"], r["import_id"]) for r in skip] == [
        (1, inc.SKIP_RECOVERY_WITHOUT_FAULT, result["import_id"])]
    assert synced.import_candidate_count() == 0  # not a candidate any more
    assert synced._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 3  # kept
    for statement in ("UPDATE incident_import_skips SET reason='x'",
                      "DELETE FROM incident_import_skips"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            synced._conn.execute(statement)
    assert synced.preview_import(CFG).considered == 0
    again = do_import(synced)
    assert again["incidents_created"] == 0
    # the skipped alert stays in the unresolved Recent alerts view, flagged as not in an incident
    row = next(r for r in synced.list_alerts(view="unresolved")["rows"] if r["id"] == 1)
    assert row["incident_id"] is None and row["skipped_reason"] == inc.SKIP_RECOVERY_WITHOUT_FAULT


def test_only_skipped_alerts_still_get_recorded_so_they_stop_reappearing(synced):
    raise_alert(synced, T0, U, X)
    treat_as_history(synced)
    plan_ = synced.preview_import(CFG)
    assert (plan_.summary()["incidents_to_create"], plan_.summary()["alerts_to_skip"]) == (0, 1)
    assert do_import(synced) == {"import_id": 1, "incidents_created": 0, "alerts_linked": 0,
                                 "alerts_skipped": 1}
    assert synced.import_candidate_count() == 0


def test_a_live_ignored_recovery_pairs_with_the_older_fault_when_imported(synced):
    raise_alert(synced, T0, X)  # raised before upgrading
    treat_as_history(synced)
    raise_alert(synced, T0 + 90, U)  # raised after upgrading: no live incident, so it is ignored
    assert process(synced) == 1 and synced.list_incidents(view="all") == []
    result = do_import(synced)
    assert (result["incidents_created"], result["alerts_skipped"]) == (1, 0)
    only = synced.list_incidents(view="all")[0]
    assert (only.monitoring_state, only.recovered_ts) == (inc.MON_RECOVERED, T0 + 90)


def test_an_interrupted_import_leaves_nothing_behind_and_can_simply_be_run_again(synced,
                                                                                  monkeypatch):
    history(synced)
    before = {t: snapshot(synced, t) for t in (
        "alerts", "incidents", "incident_events", "incident_imports", "incident_import_skips",
        "incident_cursor")}
    real = synced._store_evidence
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return real(*args, **kwargs)

    monkeypatch.setattr(synced, "_store_evidence", flaky)
    plan_ = synced.preview_import(CFG)
    with pytest.raises(sqlite3.OperationalError):
        synced.apply_import(CFG, simulated=True, actor="alice", fingerprint=plan_.fingerprint,
                            now=T0 + 1)
    assert {t: snapshot(synced, t) for t in before} == before  # rolled back completely
    monkeypatch.setattr(synced, "_store_evidence", real)
    assert do_import(synced)["incidents_created"] == 2  # and it works the next time
    assert do_import(synced)["incidents_created"] == 0


def test_the_import_survives_restarts_without_duplicates(tmp_path):
    path = tmp_path / "import.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile()),
                     Device("beta", "192.0.2.2", "switch", SimProfile())])
    history(db)
    do_import(db, actor="alice")
    target = by_device(db)["alpha"][0]
    db.acknowledge_incident(target.id, "alice", "on it", T0 + 9100)
    db.close()
    for _ in range(2):  # restart twice
        db = Database(path)
        try:
            assert process(db) == 0
            assert db.import_candidate_count() == 0
            assert do_import(db)["incidents_created"] == 0
            assert len(db.list_incidents(view="all")) == 2
            kept = db.get_incident(target.id)
            assert (kept.origin, kept.review_state, kept.acknowledged_by) == (
                inc.ORIGIN_IMPORTED, inc.REV_ACKNOWLEDGED, "alice")
        finally:
            db.close()


def test_import_records_who_when_and_what(synced):
    history(synced)
    do_import(synced, actor="alice", now=T0 + 9000)
    row = synced._conn.execute("SELECT * FROM incident_imports").fetchone()
    assert (row["ts"], row["actor"], row["alerts_considered"], row["incidents_created"],
            row["alerts_skipped"]) == (T0 + 9000, "alice", 3, 2, 0)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        synced._conn.execute("UPDATE incident_imports SET actor='mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        synced._conn.execute("DELETE FROM incident_imports")


def test_imported_incidents_on_one_device_link_to_the_previous_one(synced):
    for ts, st in [(0, X), (60, U), (WINDOW + 1000, X), (WINDOW + 1060, U)]:
        raise_alert(synced, T0 + ts, st)
    treat_as_history(synced)
    do_import(synced)
    newer, older = by_device(synced)["alpha"]  # newest activity first
    assert older.previous_incident_id is None and newer.previous_incident_id == older.id


# ------------------------------------------------------------------ coexistence with live incidents
def test_importing_never_modifies_existing_incidents_notes_reports_or_audit_events(synced):
    history(synced)
    raise_alert(synced, T0 + 5000, X, device="beta")  # a LIVE incident after the upgrade
    process(synced)
    live = by_device(synced)["beta"][0]
    synced.acknowledge_incident(live.id, "alice", "on it", T0 + 5010)
    synced.add_incident_note(live.id, "alice", "checked the uplink", T0 + 5020)
    synced.update_incident_report(live.id, "alice", {"root_cause": "ISP", "root_cause_status":
                                                     "suspected"}, T0 + 5030)
    before_inc = {r[0]: r for r in snapshot(synced, "incidents")}
    before_ev = snapshot(synced, "incident_events")
    before_alerts = snapshot(synced, "alerts")
    result = do_import(synced)
    assert result["incidents_created"] == 2
    after_inc = {r[0]: r for r in snapshot(synced, "incidents")}
    assert {k: v for k, v in after_inc.items() if k in before_inc} == before_inc
    assert snapshot(synced, "incident_events")[:len(before_ev)] == before_ev
    assert snapshot(synced, "alerts") == before_alerts
    assert len(after_inc) == len(before_inc) + 2
    assert {r[0] for r in snapshot(synced, "incident_events")} >= {r[0] for r in before_ev}


def test_live_processing_carries_on_after_an_import(synced):
    history(synced)
    do_import(synced)
    raise_alert(synced, T0 + 6000, X, device="beta")  # beta has an imported unknown incident
    raise_alert(synced, T0 + 6100, X, device="alpha")  # alpha's imported one is long recovered
    assert process(synced) == 2
    imported_beta = next(i for i in synced.list_incidents(view="all")
                         if i.device == "beta" and i.origin == inc.ORIGIN_IMPORTED)
    live_beta = next(i for i in synced.list_incidents(view="all")
                     if i.device == "beta" and i.origin == inc.ORIGIN_LIVE)
    assert live_beta.previous_incident_id == imported_beta.id  # linked, not merged
    assert live_beta.monitoring_state == inc.MON_ACTIVE
    assert imported_beta.monitoring_state == inc.MON_UNKNOWN  # untouched by the live alert
    assert len(synced.incident_events(imported_beta.id)) == 2  # the alert and the import note
    live_alpha = next(i for i in synced.list_incidents(view="all")
                      if i.device == "alpha" and i.origin == inc.ORIGIN_LIVE)
    assert live_alpha.flap_count == 0  # never joined the long-recovered imported incident
    raise_alert(synced, T0 + 6200, U, device="beta")  # recovers the live one only
    process(synced)
    assert synced.get_incident(live_beta.id).monitoring_state == inc.MON_RECOVERED
    assert synced.get_incident(imported_beta.id).monitoring_state == inc.MON_UNKNOWN


def test_import_and_live_processing_running_together_never_duplicate(synced):
    for k in range(40):  # a longer history
        raise_alert(synced, T0 + k * 4000, X)
        raise_alert(synced, T0 + k * 4000 + 100, U)
    treat_as_history(synced)
    stop = threading.Event()
    errors = []

    def live():
        n = 0
        try:
            while not stop.is_set():
                raise_alert(synced, T0 + 500_000 + n * 3000, X, device="beta")
                raise_alert(synced, T0 + 500_000 + n * 3000 + 100, U, device="beta")
                process(synced)
                n += 1
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=live)
    thread.start()
    try:
        results = [do_import(synced) for _ in range(5)]
    finally:
        stop.set()
        thread.join()
    process(synced)
    assert not errors
    assert sum(r["incidents_created"] for r in results) == 40
    dup = synced._conn.execute(
        "SELECT alert_id, kind, COUNT(*) FROM incident_events WHERE alert_id IS NOT NULL "
        "GROUP BY alert_id, kind HAVING COUNT(*) > 1").fetchall()
    assert dup == []
    assert synced._conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE origin='imported'").fetchone()[0] == 40


# ------------------------------------------------------------------ resolving "recovery unknown"
def test_resolving_an_unknown_recovery_needs_a_summary_and_an_explicit_confirmation(synced):
    history(synced)
    do_import(synced)
    beta = by_device(synced)["beta"][0]
    summary = "Device answered normally by the time I looked on the next day."
    with pytest.raises(inc.IncidentError) as exc:
        synced.resolve_incident(beta.id, "alice", summary, None, T0 + 9500)
    assert exc.value.code == inc.IncidentError.INVALID_INPUT
    assert "never recorded a recovery" in exc.value.message
    for bad in ("", "short"):
        with pytest.raises(inc.IncidentError):
            synced.resolve_incident(beta.id, "alice", bad, None, T0 + 9500, confirm_unknown=True)
    done = synced.resolve_incident(beta.id, "alice", summary, None, T0 + 9500,
                                   confirm_unknown=True)
    assert done.review_state == inc.REV_RESOLVED and done.resolved_by == "alice"
    # monitoring's own record is untouched: it is still "recovery unknown"
    assert (done.monitoring_state, done.recovered_ts) == (inc.MON_UNKNOWN, None)
    event = synced.incident_events(beta.id)[-1]
    assert event.kind == inc.EV_RESOLVED and summary in event.text
    assert "never recorded a recovery" in event.text
    assert done.evidence["window_end_ts"] == beta.last_alert_ts  # nothing assumed beyond the alert


def test_an_unknown_incident_can_be_reopened_and_stays_unknown(synced):
    history(synced)
    do_import(synced)
    beta = by_device(synced)["beta"][0]
    synced.resolve_incident(beta.id, "alice", "Confirmed by a person that it ended.", None,
                            T0 + 9500, confirm_unknown=True)
    again = synced.reopen_incident(beta.id, "bob", "It was not over after all.", T0 + 9600)
    assert (again.monitoring_state, again.review_state) == (inc.MON_UNKNOWN, inc.REV_OPEN)
    assert inc.lifecycle(again)["label"] == "Reopened — recovery unknown"


def test_a_live_active_fault_still_cannot_be_resolved(synced):
    raise_alert(synced, T0 + 100, X)
    treat_as_history(synced)
    raise_alert(synced, T0 + 200, X, device="beta")
    process(synced)
    live = synced.list_incidents(view="all")[0]
    with pytest.raises(inc.IncidentError) as exc:
        synced.resolve_incident(live.id, None, "Fixed it, honestly.", None, T0 + 300,
                                confirm_unknown=True)
    assert exc.value.code == inc.IncidentError.INVALID_TRANSITION


def test_lifecycle_labels_for_unknown_recovery():
    base = dict(
        id=1, device="a", kind=inc.KIND_ICMP, severity="critical", opened_ts=T0, last_alert_ts=T0,
        monitoring_state=inc.MON_UNKNOWN, recovered_ts=None, flap_count=0,
        review_state=inc.REV_OPEN, acknowledged_ts=None, acknowledged_by=None, resolved_ts=None,
        resolved_by=None, reopened_count=0, previous_incident_id=None, simulated=False,
        run_started_ts=None)
    open_ = inc.lifecycle(inc.Incident(**base))
    assert open_["label"] == "Recovery unknown — awaiting review"
    assert open_["recovery_unknown"] and open_["in_active_view"] and not open_["awaiting_review"]
    ack = inc.lifecycle(inc.Incident(**{**base, "review_state": inc.REV_ACKNOWLEDGED}))
    assert ack["label"] == "Recovery unknown — acknowledged"
    done = inc.lifecycle(inc.Incident(**{**base, "review_state": inc.REV_RESOLVED}))
    assert done["label"] == "Resolved" and not done["in_active_view"]


# ------------------------------------------------------------------ the write-up stays honest
CLAIMS = re.compile(
    r"\b(repaired|fixed|replaced|restarted|rebooted|reconfigured|swapped|reseated|restored)\b",
    re.IGNORECASE)


def report_of(db, i, simulated=True):
    return inc.build_report(i, db.incident_events(i.id), db.incident_evidence(i, T0 + 99999),
                            cfg=CFG, device_type="router")


def test_an_imported_report_says_it_was_reconstructed_and_claims_nothing_it_cannot_know(synced):
    history(synced)
    do_import(synced, now=T0 + 9000)
    for device in ("alpha", "beta"):
        i = by_device(synced)[device][0]
        rep = report_of(synced, i)
        md = inc.render_markdown(rep)
        obs = rep["observed"]
        assert rep["imported"] is True
        assert "reconstructed afterwards from the stored alert history" in obs["summary"]
        assert "not open at the time" in obs["summary"]
        actions = " ".join(obs["automated_actions"])
        assert "Kept checking" not in actions  # the interval then in force is not known
        assert "opened this incident automatically" not in actions
        assert "NetMonLT performed no repair" in md
        assert not CLAIMS.search(actions)
        assert all(rep["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)
        assert "Imported from alert history" in md  # the timeline says so


def test_an_unknown_recovery_report_never_claims_recovery_or_an_ongoing_outage(synced):
    history(synced)
    do_import(synced)
    beta = by_device(synced)["beta"][0]
    rep = report_of(synced, beta)
    md = inc.render_markdown(rep)
    assert rep["recovery_unknown"] is True and rep["recovered_at"] is None
    assert "does not know whether or when it ended" in rep["observed"]["summary"]
    assert "unknown (no recovery alert was recorded)" in md
    assert "no recorded recovery to verify" in rep["observed"]["recovery_verification"]
    low = md.lower()
    assert "has recovered" not in low and "confirmed it had recovered" not in low
    assert "assumed that it recovered or that it is still down" in low  # what it did NOT assume
    assert "is still down." not in low.replace("or that it is still down.", "")
    synced.resolve_incident(beta.id, "alice", "Operator statement: it ended by itself.", None,
                            T0 + 9500, confirm_unknown=True)
    done_md = inc.render_markdown(report_of(synced, synced.get_incident(beta.id)))
    assert "recovery unknown" in done_md and "not a monitoring confirmation" in done_md


def test_imported_simulated_reports_state_no_scripted_cause_and_no_repairs(synced):
    history(synced)
    do_import(synced)
    beta = by_device(synced)["beta"][0]
    rep = inc.build_report(
        beta, synced.incident_events(beta.id), synced.incident_evidence(beta, T0 + 9999),
        cfg=CFG, device_type="switch", sim=SimProfile(outage_start_s=5, outage_duration_s=60))
    example = rep["simulated_example"]
    assert example["known_scenario_cause"] is None  # no run to tie it to: nothing is claimed
    assert "defines no cause" in example["no_cause_note"]
    assert "no recovery was recorded" in example["illustrative_resolution_summary"]
    assert not CLAIMS.search(example["illustrative_resolution_summary"])


# --------------------------------------------------------- migration of a Stage 10 database
def test_an_existing_stage_10_database_gains_the_new_columns_without_losing_anything(tmp_path):
    path = tmp_path / "stage10.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    process(db)
    i = db.list_incidents(view="all")[0]
    db.acknowledge_incident(i.id, "alice", "on it", T0 + 100)
    db.add_incident_note(i.id, "alice", "kept note", T0 + 110)
    db.update_incident_report(i.id, "alice", {"root_cause": "Loose cable"}, T0 + 120)
    conn = db._conn
    conn.executescript("""
        DROP TRIGGER incident_imports_no_update; DROP TRIGGER incident_imports_no_delete;
        DROP TRIGGER incident_import_skips_no_update; DROP TRIGGER incident_import_skips_no_delete;
        DROP TABLE incident_import_skips; DROP TABLE incident_imports;
        ALTER TABLE incidents DROP COLUMN origin; ALTER TABLE incidents DROP COLUMN import_id;
    """)
    old_columns = [r["name"] for r in conn.execute("PRAGMA table_info(incidents)")]
    assert "origin" not in old_columns  # this really is the earlier schema
    before = {t: snapshot(db, t) for t in ("alerts", "incident_events", "incident_cursor")}
    before_inc = snapshot(db, "incidents")
    db.close()

    db = Database(path)  # the upgrade
    try:
        columns = [r["name"] for r in db._conn.execute("PRAGMA table_info(incidents)")]
        assert columns[:len(old_columns)] == old_columns and columns[-2:] == ["origin", "import_id"]
        assert {t: snapshot(db, t) for t in before} == before
        assert [r[:len(old_columns)] for r in snapshot(db, "incidents")] == before_inc
        kept = db.get_incident(i.id)
        assert (kept.origin, kept.import_id) == (inc.ORIGIN_LIVE, None)
        assert kept.report == {"root_cause": "Loose cable"} and kept.acknowledged_by == "alice"
        assert [e.text for e in db.incident_events(i.id) if e.kind == inc.EV_NOTE] == ["kept note"]
        assert db.import_candidate_count() == 0
    finally:
        db.close()
    Database(path).close()  # opening again changes nothing and does not fail


def test_the_import_can_be_disabled_with_incident_management(synced):
    history(synced)
    cfg = IncidentsConfig(enabled=False)
    assert synced.preview_import(cfg).considered == 3  # the data layer only follows the config
    # (the API refuses when disabled; see test_api_import)
