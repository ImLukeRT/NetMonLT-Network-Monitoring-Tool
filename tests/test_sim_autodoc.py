"""Simulation-only auto-documentation and optional auto-resolution (Stage 10.1).

Auto-documentation stores AUTO-GENERATED example documentation once, when a simulated incident is
acknowledged. Auto-resolution is a separate feature, off unless enabled, that resolves an
acknowledged, documented, simulated incident whose recovery monitoring confirmed. Neither ever
touches operator-written content, real monitoring, or the evidence and audit trail."""

import dataclasses
import re
import sqlite3
import threading

import pytest

from incident_helpers import (
    CFG,
    T0,
    U,
    X,
    by_device,
    do_import,
    headers,
    local_client,
    make_app,
    process,
    raise_alert,
    remote_client,
    snapshot,
    treat_as_history,
)
from netmonlt import incidents as inc
from netmonlt.domain import Device, IncidentsConfig, SimProfile
from netmonlt.incident_engine import process_once
from netmonlt.inventory import InventoryError, parse_inventory
from netmonlt.storage import Database

AUTO = IncidentsConfig(simulation_auto_resolve=True)
CLAIMS = re.compile(
    r"\b(repaired|fixed|replaced|restarted|rebooted|reconfigured|swapped|reseated|restored|we|i)\b",
    re.IGNORECASE)


def builder(i):
    return inc.build_auto_documentation(i, device_type="router",
                                        sim=SimProfile(outage_start_s=5, outage_duration_s=60))


@pytest.fixture
def synced(db, devices):
    db.sync_devices(devices)
    return db


def recovered(db, device="alpha", simulated=True, at=0):
    raise_alert(db, T0 + at, X, device=device)
    raise_alert(db, T0 + at + 90, U, device=device)
    process(db, simulated=simulated)
    return next(i for i in db.list_incidents(view="all") if i.device == device
                and i.opened_ts == T0 + at)


def active(db, device="alpha"):
    raise_alert(db, T0 + 500, X, device=device)
    process(db)
    return next(i for i in db.list_incidents(view="all") if i.device == device)


def events(db, incident, kind=None):
    return [e for e in db.incident_events(incident.id) if kind is None or e.kind == kind]


def ack(db, incident, doc=builder, now=T0 + 200):
    return db.acknowledge_incident(incident.id, "alice", "on it", now, auto_doc=doc)


# ------------------------------------------------------------------ the generated documentation
def test_the_documentation_is_deterministic_labelled_and_claims_nothing_was_done(synced):
    i = dataclasses.replace(recovered(synced), run_started_ts=T0 - 10)  # the run the outage is in
    a, b = builder(i), builder(i)
    assert a == b and a["marker"] == "SIMULATED EXAMPLE — AUTO-GENERATED"
    assert "fictional" in a["notice"] and "not observed or performed by anyone" in a["notice"]
    text = "\n".join(a["investigation_notes"] + a["steps"] + a["recommendations"] + a["lessons"])
    assert not CLAIMS.search(text), CLAIMS.search(text).group(0)
    assert all(n.startswith(("Example note", "Example check", "Simulator-defined", "No cause",
                             "This incident was reconstructed", "At the time"))
               for n in a["investigation_notes"])
    assert "outage_start_s=5" in a["scenario_cause"]["text"]
    assert a["monitoring_at_generation"] == "recovered" and not a["imported"]


def test_a_scenario_cause_appears_only_if_the_simulator_defines_one(synced):
    i = recovered(synced)
    plain = inc.build_auto_documentation(i, device_type="router", sim=SimProfile())
    assert plain["scenario_cause"] is None
    assert any("No cause is defined by the simulator" in n for n in plain["investigation_notes"])


def test_the_resolution_statement_never_claims_a_repair_or_a_human_investigation(synced):
    i = recovered(synced)
    text = inc.auto_resolution_text(i)
    assert text.startswith(inc.AUTO_MARKER)
    assert "recovered in simulation" in text and "without any verified repair" in text
    assert "No human investigation, root cause or repair was performed or verified" in text
    assert "not an operator resolution" in text
    assert not CLAIMS.search(text)
    assert inc.fmt_ts(i.recovered_ts) in text  # the recorded recovery time, not an invented one


# ------------------------------------------------------------------ acknowledging stores it, once
def test_acknowledging_a_simulated_incident_stores_the_documentation_without_resolving(synced):
    i = recovered(synced)
    after = ack(synced, i)
    assert after.review_state == inc.REV_ACKNOWLEDGED and after.resolved_ts is None
    stored = synced.get_auto_doc(i.id)
    assert stored["generated_ts"] == T0 + 200 and stored["monitoring_state"] == "recovered"
    assert stored["doc"]["marker"] == inc.AUTO_MARKER
    (event,) = events(synced, i, inc.EV_AUTO_DOC)
    assert (event.source, event.actor, event.alert_id) == (inc.SRC_SIMULATION, None, None)
    assert inc.AUTO_MARKER in event.text and "not observed or performed" in event.text


def test_without_the_builder_nothing_is_generated(synced):
    i = recovered(synced)
    ack(synced, i, doc=None)  # the feature switched off
    assert synced.get_auto_doc(i.id) is None and not events(synced, i, inc.EV_AUTO_DOC)


def test_a_non_simulated_incident_never_gets_documentation_even_if_asked(synced):
    i = recovered(synced, simulated=False)
    assert i.simulated is False
    ack(synced, i)  # a builder is passed, but the incident is not a simulated one
    assert synced.get_auto_doc(i.id) is None and not events(synced, i, inc.EV_AUTO_DOC)
    with pytest.raises(inc.IncidentError) as exc:
        synced.generate_auto_documentation(i.id, builder, T0 + 300)
    assert exc.value.code == inc.IncidentError.INVALID_TRANSITION
    assert synced._conn.execute("SELECT COUNT(*) FROM incident_auto_docs").fetchone()[0] == 0


def test_generation_is_idempotent_across_repeats_reopening_and_restarts(tmp_path):
    path = tmp_path / "auto.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    i = recovered(db)
    ack(db, i)
    assert db.generate_auto_documentation(i.id, builder, T0 + 210) is False
    db.resolve_incident(i.id, "alice", "Reseated the patch cable; verified.", None, T0 + 300)
    db.reopen_incident(i.id, "bob", "It dropped again on the same port.", T0 + 310)
    db.acknowledge_incident(i.id, "bob", None, T0 + 320, auto_doc=builder)  # acknowledge again
    db.close()
    for _ in range(2):  # restarts
        db = Database(path)
        try:
            assert db.generate_auto_documentation(i.id, builder, T0 + 400) is False
            assert db._conn.execute("SELECT COUNT(*) FROM incident_auto_docs").fetchone()[0] == 1
            assert len(events(db, i, inc.EV_AUTO_DOC)) == 1
            assert db.get_auto_doc(i.id)["generated_ts"] == T0 + 200  # the original, untouched
        finally:
            db.close()


def test_concurrent_generation_creates_exactly_one(synced):
    i = recovered(synced)
    results, errors = [], []

    def run():
        try:
            results.append(synced.generate_auto_documentation(i.id, builder, T0 + 250))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results.count(True) == 1 and results.count(False) == 9
    assert len(events(synced, i, inc.EV_AUTO_DOC)) == 1


def test_the_database_itself_refuses_duplicates_and_changes(synced):
    i = recovered(synced)
    ack(synced, i)
    with pytest.raises(sqlite3.IntegrityError):  # a second automatic event for the incident
        synced._insert_incident_event(i.id, T0 + 1, inc.SRC_SIMULATION, inc.EV_AUTO_DOC, text="x")
    for statement in ("UPDATE incident_auto_docs SET doc='{}'", "DELETE FROM incident_auto_docs"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            synced._conn.execute(statement)


def test_operator_notes_reports_evidence_alerts_and_audit_events_are_untouched(synced):
    i = recovered(synced)
    synced.add_incident_note(i.id, "alice", "My own note.", T0 + 100)
    synced.update_incident_report(i.id, "alice", {"root_cause": "Loose cable",
                                                  "root_cause_status": "suspected"}, T0 + 110)
    before_events = snapshot(synced, "incident_events")
    before_alerts = snapshot(synced, "alerts")
    before = synced.get_incident(i.id)
    ack(synced, i)
    synced.generate_auto_documentation(i.id, builder, T0 + 300)
    after = synced.get_incident(i.id)
    assert after.report == before.report == {"root_cause": "Loose cable",
                                             "root_cause_status": "suspected"}
    assert after.evidence == before.evidence
    assert (after.opened_ts, after.recovered_ts, after.monitoring_state) == (
        before.opened_ts, before.recovered_ts, before.monitoring_state)
    now_events = snapshot(synced, "incident_events")
    assert now_events[:len(before_events)] == before_events  # nothing earlier was changed
    assert [e[4] for e in now_events[len(before_events):]] == [inc.EV_ACK, inc.EV_AUTO_DOC]
    assert snapshot(synced, "alerts") == before_alerts


# ------------------------------------------------------------------ automatic resolution
def resolve_all(db, cfg=AUTO, **kw):
    return db.auto_resolve_incidents(cfg, simulated_mode=kw.get("simulated_mode", True),
                                     now=T0 + 999)


def test_acknowledging_never_resolves_by_default(synced):
    i = recovered(synced)
    ack(synced, i)
    assert resolve_all(synced, CFG) == []  # the setting is off by default
    assert process_once(synced, CFG, simulated=True, run_started_ts=None,
                        clock=lambda: T0 + 999) == 0
    assert synced.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED
    assert not events(synced, i, inc.EV_AUTO_RESOLVED)
    assert IncidentsConfig().simulation_auto_resolve is False


def test_an_acknowledged_documented_recovered_incident_is_auto_resolved_as_the_simulation(synced):
    i = recovered(synced)
    synced.add_incident_note(i.id, "alice", "My own note.", T0 + 100)
    ack(synced, i)
    before_report = synced.get_incident(i.id).report
    assert resolve_all(synced) == [i.id]
    done = synced.get_incident(i.id)
    assert (done.review_state, done.resolved_by, done.resolved_ts) == (
        inc.REV_RESOLVED, "simulation-auto", T0 + 999)
    assert done.monitoring_state == inc.MON_RECOVERED and done.recovered_ts == T0 + 90
    assert done.report == before_report == {}  # no operator field is written
    assert inc.lifecycle(done)["label"] == "Auto-resolved (simulation)"
    assert inc.lifecycle(done)["resolution"] == "simulation-auto"
    (event,) = events(synced, i, inc.EV_AUTO_RESOLVED)
    assert (event.source, event.actor) == (inc.SRC_SIMULATION, "simulation-auto")
    assert "recovered in simulation" in event.text and "without any verified repair" in event.text
    assert [e.text for e in events(synced, i, inc.EV_NOTE)] == ["My own note."]
    assert done.evidence["window_end_ts"] == T0 + 90  # frozen at monitoring's own recovery time


def test_recovery_after_the_acknowledgement_is_resolved_on_the_next_engine_tick(synced):
    raise_alert(synced, T0, X)
    process(synced)
    i = synced.list_incidents(view="all")[0]
    ack(synced, i)  # acknowledged while the fault is still active
    assert synced.get_auto_doc(i.id)["monitoring_state"] == "active"
    run = lambda now: process_once(  # noqa: E731
        synced, AUTO, simulated=True, run_started_ts=None, clock=lambda: now)
    run(T0 + 50)
    assert synced.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED  # still active: no
    raise_alert(synced, T0 + 90, U)
    run(T0 + 100)
    done = synced.get_incident(i.id)
    assert (done.review_state, done.resolved_by) == (inc.REV_RESOLVED, "simulation-auto")
    assert done.recovered_ts == T0 + 90


@pytest.mark.parametrize("case", ["active", "unacknowledged", "no_documentation", "reopened",
                                  "not_simulated", "real_mode", "disabled"])
def test_these_are_never_auto_resolved(synced, case):
    if case == "active":
        i = active(synced)
        ack(synced, i)
    elif case == "not_simulated":
        i = recovered(synced, simulated=False)
        ack(synced, i)
    else:
        i = recovered(synced)
        if case != "unacknowledged":
            ack(synced, i, doc=None if case == "no_documentation" else builder)
        if case == "reopened":
            synced.resolve_incident(i.id, "alice", "Reseated the patch cable.", None, T0 + 300)
            synced.reopen_incident(i.id, "bob", "It dropped again, not over.", T0 + 310)
            synced.acknowledge_incident(i.id, "bob", None, T0 + 320, auto_doc=builder)
    cfg = IncidentsConfig() if case == "disabled" else AUTO
    assert resolve_all(synced, cfg, simulated_mode=case != "real_mode") == []
    assert not events(synced, i, inc.EV_AUTO_RESOLVED)
    assert synced.get_incident(i.id).resolved_by != "simulation-auto"


def test_a_recovery_unknown_incident_is_never_auto_resolved(synced):
    raise_alert(synced, T0, X)  # history with no recovery alert
    treat_as_history(synced)
    do_import(synced)
    unknown = by_device(synced)["alpha"][0]
    assert unknown.monitoring_state == inc.MON_UNKNOWN
    ack(synced, unknown)
    assert synced.get_auto_doc(unknown.id) is not None  # it can be documented ...
    assert resolve_all(synced) == []  # ... but never auto-resolved
    assert synced.get_incident(unknown.id).review_state == inc.REV_ACKNOWLEDGED


def test_a_fault_that_recurs_before_the_tick_is_not_resolved(synced):
    i = recovered(synced)
    ack(synced, i)
    raise_alert(synced, T0 + 200, X)  # back inside the correlation window: active again
    process(synced)
    assert synced.get_incident(i.id).monitoring_state == inc.MON_ACTIVE
    assert resolve_all(synced) == []


def test_auto_resolution_is_idempotent_and_survives_restarts(tmp_path):
    path = tmp_path / "auto.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    i = recovered(db)
    ack(db, i)
    assert resolve_all(db) == [i.id] and resolve_all(db) == []
    with pytest.raises(sqlite3.IntegrityError):
        db._insert_incident_event(i.id, T0 + 1, inc.SRC_SIMULATION, inc.EV_AUTO_RESOLVED, text="x")
    db.close()
    for _ in range(2):
        db = Database(path)
        try:
            assert resolve_all(db) == []
            assert len(events(db, i, inc.EV_AUTO_RESOLVED)) == 1
            assert db.get_incident(i.id).resolved_ts == T0 + 999
        finally:
            db.close()


def test_concurrent_auto_resolution_and_processing_resolve_once(synced):
    ids = []
    for k in range(6):
        i = recovered(synced, at=k * 5000)
        ack(synced, i)
        ids.append(i.id)
    out, errors = [], []

    def run():
        try:
            out.append(resolve_all(synced))
            process_once(synced, AUTO, simulated=True, run_started_ts=None,
                         clock=lambda: T0 + 999)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(x for batch in out for x in batch) == sorted(ids)  # each exactly once
    assert synced._conn.execute(
        "SELECT COUNT(*) FROM incident_events WHERE kind='auto_resolved'").fetchone()[0] == 6


def test_a_person_who_resolved_first_takes_priority(synced):
    i = recovered(synced)
    ack(synced, i)
    synced.resolve_incident(i.id, "alice", "Reseated the patch cable; verified by ping.", None,
                            T0 + 300)
    assert resolve_all(synced) == []
    done = synced.get_incident(i.id)
    assert (done.resolved_by, done.report["resolution_summary"]) == (
        "alice", "Reseated the patch cable; verified by ping.")
    assert inc.lifecycle(done)["resolution"] == "operator"


def test_a_human_cannot_use_the_reserved_simulation_actor(synced):
    i = recovered(synced)
    for name in ("simulation-auto", "Simulation-Auto"):
        with pytest.raises(inc.IncidentError):
            synced.acknowledge_incident(i.id, name, None, T0 + 1)
        with pytest.raises(inc.IncidentError):
            inc.clean_actor(name)
    assert inc.clean_actor("simulation-bot") == "simulation-bot"  # other names are fine


def test_upgrading_an_existing_database_does_not_enable_or_apply_auto_resolution(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    db.sync_devices([Device("alpha", "192.0.2.1", "router", SimProfile())])
    i = recovered(db)
    db.acknowledge_incident(i.id, "alice", None, T0 + 100)  # acknowledged before this feature
    db.close()
    db = Database(path)
    try:
        process_once(db, parse_inventory({"devices": [{"name": "a", "address": "x"}]}).incidents,
                     simulated=True, run_started_ts=None, clock=lambda: T0 + 999)
        assert db.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED
        assert db.get_auto_doc(i.id) is None
    finally:
        db.close()


# ------------------------------------------------------------------ imported historical incidents
def test_imported_incidents_are_documented_as_reconstructed_and_can_auto_resolve(synced):
    raise_alert(synced, T0, X)
    raise_alert(synced, T0 + 90, U)
    treat_as_history(synced)
    do_import(synced)
    i = by_device(synced)["alpha"][0]
    assert i.origin == inc.ORIGIN_IMPORTED
    ack(synced, i)
    doc = synced.get_auto_doc(i.id)["doc"]
    assert doc["imported"] is True
    assert any("reconstructed from historical monitoring events" in n
               for n in doc["investigation_notes"])
    assert not CLAIMS.search("\n".join(doc["investigation_notes"]))
    assert resolve_all(synced) == [i.id]  # recovered, acknowledged, documented, never reopened
    rep = inc.build_report(synced.get_incident(i.id), synced.incident_events(i.id),
                           synced.incident_evidence(synced.get_incident(i.id), T0 + 9999), cfg=CFG,
                           auto_doc=synced.get_auto_doc(i.id))
    assert rep["imported"] and rep["resolution"]["kind"] == "simulation-auto"
    assert "reconstructed afterwards" in rep["observed"]["summary"]


# ------------------------------------------------------------------ configuration
def parse(block, mode="simulated"):
    devices = [{"name": "a", "address": "192.0.2.1", "authorized": True}]
    raw = {"mode": mode, "devices": devices, "incidents": block}
    if mode == "real":
        raw["poll_interval_s"] = 10
    return parse_inventory(raw).incidents


def test_the_defaults_leave_auto_resolution_off_and_documentation_on_in_simulation():
    cfg = parse(None)
    assert cfg.simulation_auto_resolve is False and cfg.simulation_auto_documentation is None
    assert parse({}).simulation_auto_resolve is False


def test_the_simulation_settings_can_be_set_in_simulated_mode():
    cfg = parse({"simulation_auto_documentation": True, "simulation_auto_resolve": True})
    assert (cfg.simulation_auto_documentation, cfg.simulation_auto_resolve) == (True, True)
    assert parse({"simulation_auto_documentation": False}).simulation_auto_documentation is False


@pytest.mark.parametrize("key", ["simulation_auto_documentation", "simulation_auto_resolve"])
def test_enabling_either_simulation_feature_in_real_mode_is_rejected(key):
    with pytest.raises(InventoryError, match="simulation-only"):
        parse({key: True}, mode="real")
    assert parse({key: False}, mode="real") is not None  # switching them off is harmless
    assert parse(None, mode="real").simulation_auto_resolve is False


@pytest.mark.parametrize("block", [{"simulation_auto_resolve": "yes"},
                                   {"simulation_auto_documentation": 1},
                                   {"simulation_auto_resolve": None}])
def test_the_simulation_settings_must_be_booleans(block):
    with pytest.raises(InventoryError):
        parse(block)


def test_auto_resolution_needs_the_documentation_feature():
    with pytest.raises(InventoryError, match="needs incidents.simulation_auto_documentation"):
        parse({"simulation_auto_resolve": True, "simulation_auto_documentation": False})


# ------------------------------------------------------------------ the API
def new_app(db, devices, **cfg):
    return make_app(db, devices, incidents=IncidentsConfig(**cfg))


def test_the_api_acknowledge_generates_the_documentation_and_does_not_resolve(db, devices):
    app = new_app(db, devices)
    i = recovered(db)
    client = local_client(app)
    r = client.post(f"/api/incidents/{i.id}/acknowledge", json={"actor": "alice"},
                    headers=headers(client))
    assert r.status_code == 200
    body = r.json()
    assert body["auto_documentation"] == {"enabled": True, "generated": True}
    assert body["auto_resolved"] is False
    assert body["incident"]["lifecycle"]["review"] == "acknowledged"
    again = client.post(f"/api/incidents/{i.id}/acknowledge", json={}, headers=headers(client))
    assert again.status_code == 409  # an incident is acknowledged once
    rep = client.get(f"/api/incidents/{i.id}/report?format=json").json()
    ex = rep["simulated_example"]
    assert ex["label"] == inc.AUTO_MARKER and ex["stored"] is True and ex["generated_at"]
    assert ex["investigation_notes"] and rep["resolution"]["kind"] is None
    assert [t["source"] for t in rep["observed"]["timeline"]][-2:] == ["operator", "simulation"]
    md = client.get(f"/api/incidents/{i.id}/report").text
    assert "## SIMULATED EXAMPLE — AUTO-GENERATED" in md
    assert "Generated automatically at" in md and "[simulation (auto)]" in md
    assert "[operator] Acknowledged (alice)" in md
    listing = client.get("/api/incidents").json()
    assert (listing["auto_documentation"], listing["auto_resolve"]) == (True, False)


def test_a_preview_is_shown_before_acknowledging_and_is_not_stored(db, devices):
    app = new_app(db, devices)
    i = recovered(db)
    rep = local_client(app).get(f"/api/incidents/{i.id}/report?format=json").json()
    assert rep["simulated_example"]["stored"] is False
    assert "Preview only" in local_client(app).get(f"/api/incidents/{i.id}/report").text
    assert db.get_auto_doc(i.id) is None  # reading a report never writes


def test_the_documentation_can_be_switched_off(db, devices):
    app = new_app(db, devices, simulation_auto_documentation=False)
    i = recovered(db)
    client = local_client(app)
    r = client.post(f"/api/incidents/{i.id}/acknowledge", json={}, headers=headers(client))
    assert r.json()["auto_documentation"] == {"enabled": False, "generated": False}
    assert db.get_auto_doc(i.id) is None
    gen = client.post(f"/api/incidents/{i.id}/auto-documentation", json={},
                      headers=headers(client))
    assert gen.status_code == 409 and "off here" in gen.json()["detail"]


def test_the_generate_button_endpoint_serves_incidents_acknowledged_before_the_feature(db,
                                                                                         devices):
    app = new_app(db, devices)
    i = recovered(db)
    db.add_incident_note(i.id, "alice", "A note written by a person.", T0 + 100)
    db.acknowledge_incident(i.id, "alice", None, T0 + 110)  # the old behaviour: no builder
    assert db.get_auto_doc(i.id) is None
    client = local_client(app)
    before = db.get_incident(i.id)
    r = client.post(f"/api/incidents/{i.id}/auto-documentation", json={}, headers=headers(client))
    assert r.status_code == 200 and r.json()["auto_documentation"]["generated"] is True
    assert db.get_auto_doc(i.id) is not None
    again = client.post(f"/api/incidents/{i.id}/auto-documentation", json={},
                        headers=headers(client))
    assert again.json()["auto_documentation"]["generated"] is False  # idempotent
    assert len(events(db, i, inc.EV_AUTO_DOC)) == 1
    after = db.get_incident(i.id)
    assert (after.report, after.review_state, after.acknowledged_by) == (
        before.report, before.review_state, before.acknowledged_by)
    assert [e.text for e in events(db, i, inc.EV_NOTE)] == ["A note written by a person."]


def test_the_new_endpoint_is_protected_like_every_other_change(db, devices):
    app = new_app(db, devices)
    i = recovered(db)
    local = local_client(app)
    good = headers(local)
    url = f"/api/incidents/{i.id}/auto-documentation"
    before = snapshot(db, "incident_events"), snapshot(db, "incident_auto_docs")
    assert remote_client(app).post(url, json={}, headers=good).status_code == 403
    assert local.post(url, json={}).status_code == 403  # no token
    assert local.post(url, json={}, headers={**good, "Origin": "http://evil.example"}
                      ).status_code == 403
    assert local.post(url, data={}, headers={"X-NetMonLT-Token": good["X-NetMonLT-Token"]}
                      ).status_code == 415
    assert remote_client(app).post(url, json={"bogus": 1}).status_code == 403
    assert local.post(url, json={"bogus": 1}, headers=good).status_code == 422
    assert (snapshot(db, "incident_events"), snapshot(db, "incident_auto_docs")) == before
    assert local.post("/api/incidents/999/auto-documentation", json={}, headers=good
                      ).status_code == 404


def test_the_reserved_actor_is_refused_by_the_api(db, devices):
    app = new_app(db, devices)
    i = recovered(db)
    client = local_client(app)
    for path, body in (("acknowledge", {}), ("notes", {"text": "hi"}),
                       ("reopen", {"reason": "because"})):
        r = client.post(f"/api/incidents/{i.id}/{path}", json={**body, "actor": "simulation-auto"},
                        headers=headers(client))
        assert r.status_code == 422, path
    assert db.get_incident(i.id).review_state == inc.REV_OPEN


def test_the_api_ack_with_auto_resolution_enabled_resolves_a_recovered_incident(db, devices):
    app = new_app(db, devices, simulation_auto_resolve=True)
    i = recovered(db)
    db.add_incident_note(i.id, "alice", "My own note.", T0 + 100)
    client = local_client(app)
    r = client.post(f"/api/incidents/{i.id}/acknowledge", json={"actor": "alice"},
                    headers=headers(client))
    body = r.json()
    assert body["auto_resolved"] is True
    assert body["incident"]["lifecycle"]["code"] == "auto_resolved"
    assert body["incident"]["resolved_by"] == "simulation-auto"
    rep = client.get(f"/api/incidents/{i.id}/report?format=json").json()
    assert rep["resolution"]["kind"] == "simulation-auto"
    assert "without any verified repair" in rep["resolution"]["auto_statement"]
    assert all(rep["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)
    assert rep["recovered_at"] and rep["lifecycle"]["monitoring"] == "recovered"
    md = client.get(f"/api/incidents/{i.id}/report").text
    assert "SIMULATION AUTO-RESOLUTION (simulation-auto): no operator reviewed or resolved" in md
    assert "## 9. Resolution (SIMULATION AUTO-RESOLUTION: not an operator)" in md
    assert "Auto-resolution statement (SIMULATED EXAMPLE — AUTO-GENERATED)" in md
    assert "- **Status:** Auto-resolved (simulation)" in md
    assert "[operator] Investigation note (alice): My own note." in md  # human, kept apart
    history = client.get("/api/incidents?view=history").json()["incidents"]
    assert [x["lifecycle"]["label"] for x in history] == ["Auto-resolved (simulation)"]
    assert client.get("/api/incidents").json()["incidents"] == []
    assert client.get("/api/incidents").json()["auto_resolve"] is True


def test_a_human_resolution_reads_differently_in_the_report(db, devices):
    app = new_app(db, devices)
    i = recovered(db)
    client = local_client(app)
    client.post(f"/api/incidents/{i.id}/acknowledge", json={"actor": "alice"},
                headers=headers(client))
    client.post(f"/api/incidents/{i.id}/resolve", headers=headers(client), json={
        "actor": "alice", "resolution_summary": "Reseated the patch cable; verified by ping."})
    md = client.get(f"/api/incidents/{i.id}/report").text
    assert "- **Resolved by:** operator (alice)" in md
    assert "## 9. Resolution (operator)" in md and "SIMULATION AUTO-RESOLUTION" not in md
    assert "[operator] Resolved (alice)" in md
    rep = client.get(f"/api/incidents/{i.id}/report?format=json").json()
    assert rep["resolution"]["kind"] == "operator"
    assert rep["lifecycle"]["label"] == "Resolved"


# ------------------------------------------------------------------ strict isolation from real mode
REAL = [Device("alpha", "192.0.2.1", "router", SimProfile(), authorized=True)]


def real_app(db, **cfg):
    return make_app(db, REAL, mode="real", incidents=IncidentsConfig(operator_actions=True, **cfg))


def test_real_mode_never_generates_documentation_or_resolves_automatically(db):
    app = real_app(db)
    i = recovered(db, simulated=False)
    client = local_client(app)
    listing = client.get("/api/incidents").json()
    assert (listing["simulated"], listing["auto_documentation"], listing["auto_resolve"]) == (
        False, False, False)
    r = client.post(f"/api/incidents/{i.id}/acknowledge", json={"actor": "alice"},
                    headers=headers(client))
    assert r.status_code == 200
    assert r.json()["auto_documentation"] == {"enabled": False, "generated": False}
    assert r.json()["auto_resolved"] is False
    assert db.get_auto_doc(i.id) is None and not events(db, i, inc.EV_AUTO_DOC)
    gen = client.post(f"/api/incidents/{i.id}/auto-documentation", json={},
                      headers=headers(client))
    assert gen.status_code == 409
    assert db._conn.execute("SELECT COUNT(*) FROM incident_auto_docs").fetchone()[0] == 0
    rep = client.get(f"/api/incidents/{i.id}/report?format=json").json()
    assert rep["simulated_example"] is None and rep["resolution"]["kind"] is None
    md = client.get(f"/api/incidents/{i.id}/report").text
    assert "AUTO-GENERATED" not in md and "simulation" not in md.lower().replace(
        "no real device", "")
    assert [e.kind for e in db.incident_events(i.id)] == [inc.EV_OPENED, inc.EV_RECOVERED,
                                                          inc.EV_ACK]


def test_real_mode_stays_isolated_even_if_a_simulated_incident_is_in_the_database(db):
    app = real_app(db)
    i = recovered(db, simulated=True)  # a simulated incident left in the same database
    client = local_client(app)
    r = client.post(f"/api/incidents/{i.id}/acknowledge", json={}, headers=headers(client))
    assert r.status_code == 200 and db.get_auto_doc(i.id) is None
    assert process_once(db, AUTO, simulated=False, run_started_ts=None,
                        clock=lambda: T0 + 999) == 0
    assert db.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED
    assert resolve_all(db, AUTO, simulated_mode=False) == []


def test_real_mode_engine_never_auto_resolves(synced):
    db = synced
    i = recovered(db, simulated=False)
    db.acknowledge_incident(i.id, "alice", None, T0 + 100)
    for _ in range(2):
        process_once(db, AUTO, simulated=False, run_started_ts=None, clock=lambda: T0 + 999)
    assert db.get_incident(i.id).review_state == inc.REV_ACKNOWLEDGED
    assert not events(db, i, inc.EV_AUTO_RESOLVED)
