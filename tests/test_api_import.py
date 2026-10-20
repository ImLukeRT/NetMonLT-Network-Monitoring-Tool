"""Historical import API: read-only preview, protected import, real-mode opt-in, resolving
"recovery unknown" incidents."""

import pytest

from incident_helpers import (
    T0,
    D,
    U,
    X,
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

REAL = [Device("alpha", "192.0.2.1", "router", SimProfile(), authorized=True),
        Device("beta", "192.0.2.2", "switch", SimProfile(), authorized=True)]


def history(db):
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    raise_alert(db, T0 + 200, D, U, device="beta")
    treat_as_history(db)


@pytest.fixture
def app(db, devices):
    a = make_app(db, devices)
    history(db)
    return a


def state(db):
    return {t: snapshot(db, t) for t in ("alerts", "incidents", "incident_events",
                                         "incident_imports", "incident_import_skips")}


def test_incidents_list_offers_the_import_only_when_there_is_history(db, devices):
    app = make_app(db, devices)
    assert local_client(app).get("/api/incidents").json()["counts"]["import_candidates"] == 0
    history(db)
    counts = local_client(app).get("/api/incidents").json()["counts"]
    assert counts["import_candidates"] == 3 and counts["active"] == 0


def test_the_preview_describes_the_import_and_writes_nothing(app, db):
    before = state(db)
    for client in (local_client(app), remote_client(app)):  # reading is allowed for anyone
        body = client.get("/api/incidents/import/preview").json()
        assert (body["alerts_considered"], body["incidents_to_create"], body["recovered"],
                body["recovery_unknown"], body["alerts_to_skip"]) == (3, 2, 1, 1, 0)
        assert body["correlation_window_s"] == 900.0 and len(body["fingerprint"]) == 64
        labels = {i["device"]: i["label"] for i in body["incidents"]}
        assert labels == {"alpha": "Auto-recovered — awaiting review",
                          "beta": "Recovery unknown — awaiting review"}
    assert state(db) == before


def test_importing_through_the_api_end_to_end(app, db):
    client = local_client(app)
    fp = client.get("/api/incidents/import/preview").json()["fingerprint"]
    r = client.post("/api/incidents/import", json={"confirm": fp, "actor": "alice"},
                    headers=headers(client))
    assert r.status_code == 200
    body = r.json()
    assert (body["incidents_created"], body["alerts_linked"], body["alerts_skipped"]) == (2, 3, 0)
    assert body["counts"]["import_candidates"] == 0 and body["counts"]["active"] == 2
    listing = client.get("/api/incidents").json()
    rows = {i["device"]: i for i in listing["incidents"]}
    assert rows["alpha"]["origin"] == "imported"
    assert rows["alpha"]["lifecycle"]["code"] == "recovered_awaiting_review"
    assert rows["beta"]["lifecycle"]["code"] == "unknown_awaiting_review"
    assert rows["beta"]["recovered_ts"] is None and rows["beta"]["alert_events"] == 1
    assert [i["device"] for i in client.get(
        "/api/incidents", params={"status": "unknown"}).json()["incidents"]] == ["beta"]
    # a repeat finds nothing to do
    again = client.get("/api/incidents/import/preview").json()
    assert (again["alerts_considered"], again["incidents_to_create"]) == (0, 0)
    r2 = client.post("/api/incidents/import", json={"confirm": again["fingerprint"]},
                     headers=headers(client))
    assert r2.status_code == 200 and r2.json()["incidents_created"] == 0
    assert len(client.get("/api/incidents", params={"view": "all"}).json()["incidents"]) == 2


def test_a_stale_or_malformed_confirmation_is_refused(app, db):
    client = local_client(app)
    h = headers(client)
    before = state(db)
    fp = client.get("/api/incidents/import/preview").json()["fingerprint"]
    wrong = ("0" if fp[0] != "0" else "1") + fp[1:]
    r = client.post("/api/incidents/import", json={"confirm": wrong}, headers=h)
    assert (r.status_code, r.json()["code"]) == (409, "stale_preview")
    for bad in ({}, {"confirm": "abc"}, {"confirm": fp.upper()}, {"confirm": fp, "extra": 1},
                {"confirm": fp, "actor": "<script>"}, {"confirm": 5}):
        assert client.post("/api/incidents/import", json=bad, headers=h).status_code == 422
    assert state(db) == before


def test_the_import_is_protected_like_every_other_change(app, db):
    local = local_client(app)
    h = headers(local)
    fp = local.get("/api/incidents/import/preview").json()["fingerprint"]
    body = {"confirm": fp}
    before = state(db)
    assert remote_client(app).post("/api/incidents/import", json=body, headers=h
                                   ).status_code == 403  # valid token, wrong machine
    assert local.post("/api/incidents/import", json=body, headers=JSON_ONLY).status_code == 403
    assert local.post("/api/incidents/import", json=body, headers={
        **JSON_ONLY, "X-NetMonLT-Token": "guess"}).status_code == 403
    assert local.post("/api/incidents/import", json=body,
                      headers={**h, "Origin": "http://evil.example"}).status_code == 403
    assert local.post("/api/incidents/import", data=body, headers={
        "X-NetMonLT-Token": h["X-NetMonLT-Token"]}).status_code == 415
    assert remote_client(app).post("/api/incidents/import", json={"bogus": 1}
                                   ).status_code == 403  # the guard runs before validation
    assert state(db) == before
    assert local.delete("/api/incidents/import").status_code in (404, 405)


JSON_ONLY = {"Content-Type": "application/json"}


def test_real_mode_import_is_opt_in_but_the_preview_is_always_readable(db):
    history_app = make_app(db, REAL, mode="real")
    history(db)
    client = local_client(history_app)
    assert client.get("/api/incidents/import/preview").json()["can_modify"] is False
    fp = client.get("/api/incidents/import/preview").json()["fingerprint"]
    before = state(db)
    r = client.post("/api/incidents/import", json={"confirm": fp}, headers=headers(client))
    assert r.status_code == 403 and "off by default in real mode" in r.json()["detail"]
    assert state(db) == before


def test_real_mode_import_works_once_the_inventory_opts_in(db):
    app = make_app(db, REAL, mode="real", incidents=IncidentsConfig(operator_actions=True))
    history(db)
    client = local_client(app)
    fp = client.get("/api/incidents/import/preview").json()["fingerprint"]
    r = client.post("/api/incidents/import", json={"confirm": fp}, headers=headers(client))
    assert r.status_code == 200 and r.json()["incidents_created"] == 2
    assert {i.simulated for i in db.list_incidents(view="all")} == {False}  # real mode
    rep = client.get(f"/api/incidents/{db.list_incidents(view='all')[0].id}/report?format=json"
                     ).json()
    assert rep["simulated_example"] is None and rep["imported"] is True


def test_the_import_endpoints_are_unavailable_when_incidents_are_disabled(db, devices):
    app = make_app(db, devices, incidents=IncidentsConfig(enabled=False))
    history(db)
    client = local_client(app)
    assert client.get("/api/incidents/import/preview").status_code == 404
    assert client.post("/api/incidents/import", json={"confirm": "0" * 64},
                       headers=headers(client)).status_code in (403, 404)


def test_the_preview_route_is_not_mistaken_for_an_incident_id(app):
    client = local_client(app)
    assert client.get("/api/incidents/import/preview").status_code == 200
    assert client.get("/api/incidents/import").status_code in (404, 405, 422)


# ------------------------------------------------------------------ resolving "recovery unknown"
def imported(app, db):
    client = local_client(app)
    fp = client.get("/api/incidents/import/preview").json()["fingerprint"]
    client.post("/api/incidents/import", json={"confirm": fp}, headers=headers(client))
    return client, {i.device: i for i in db.list_incidents(view="all")}


def test_an_unknown_recovery_incident_needs_an_explicit_confirmation_to_resolve(app, db):
    client, found = imported(app, db)
    beta = found["beta"]
    url = f"/api/incidents/{beta.id}/resolve"
    summary = {"resolution_summary": "Device was reachable when I checked the next morning."}
    r = client.post(url, json=summary, headers=headers(client))
    assert r.status_code == 422 and "never recorded a recovery" in r.json()["detail"]
    assert db.get_incident(beta.id).review_state == inc.REV_OPEN
    short = client.post(url, json={"resolution_summary": "short",
                                   "confirm_unknown_recovery": True}, headers=headers(client))
    assert short.status_code == 422
    ok = client.post(url, json={**summary, "confirm_unknown_recovery": True,
                                "root_cause": "Not established"}, headers=headers(client))
    assert ok.status_code == 200
    life = ok.json()["incident"]["lifecycle"]
    assert (life["code"], life["monitoring"], life["review"]) == ("resolved", "unknown",
                                                                   "resolved")
    md = client.get(f"/api/incidents/{beta.id}/report").text
    assert "recovery unknown" in md.lower() and "not a monitoring confirmation" in md
    assert [i["device"] for i in client.get(
        "/api/incidents", params={"view": "history"}).json()["incidents"]] == ["beta"]
    assert "unknown" in str(client.get(f"/api/incidents/{beta.id}").json()["incident"]["lifecycle"])


def test_the_confirmation_flag_does_not_unlock_a_live_active_fault(db, devices):
    app = make_app(db, devices)
    raise_alert(db, T0 + 100, X)
    process(db)
    client = local_client(app)
    live = db.list_incidents(view="all")[0]
    r = client.post(f"/api/incidents/{live.id}/resolve", json={
        "resolution_summary": "Fixed it, honestly.", "confirm_unknown_recovery": True},
        headers=headers(client))
    assert r.status_code == 409


def test_the_confirmation_flag_is_not_stored_as_a_write_up_field(app, db):
    client, found = imported(app, db)
    alpha = found["alpha"]  # recovered, so no confirmation is needed
    r = client.post(f"/api/incidents/{alpha.id}/resolve", json={
        "resolution_summary": "Reseated the cable; verified.", "confirm_unknown_recovery": False},
        headers=headers(client))
    assert r.status_code == 200
    assert "confirm_unknown_recovery" not in db.get_incident(alpha.id).report


def test_the_dashboard_page_has_the_import_and_alert_filter_controls(app):
    page = local_client(app).get("/").text
    for needle in ("inc-import-preview", "/api/incidents/import/preview", "alert-tabs",
                   "Archived / resolved", "All alerts", "Recovery unknown"):
        assert needle in page
    script = page[page.index("<script>"):]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in script
