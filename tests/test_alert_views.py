"""Recent alerts views: Unresolved (default), Archived / resolved and All alerts. Filtering, the row
limit and pagination happen in SQL; no alert is ever changed or removed."""

import pytest

from incident_helpers import (
    T0,
    D,
    U,
    X,
    do_import,
    headers,
    local_client,
    make_app,
    process,
    raise_alert,
    snapshot,
    treat_as_history,
)
from netmonlt import incidents as inc
from netmonlt.domain import IncidentsConfig


@pytest.fixture
def synced(db, devices):
    db.sync_devices(devices)
    return db


def ids(db, view="unresolved", **kw):
    return [r["id"] for r in db.list_alerts(view=view, **kw)["rows"]]


def episode(db, start, device="alpha"):
    """A DOWN then UP pair; returns their alert ids."""
    raise_alert(db, T0 + start, X, device=device)
    raise_alert(db, T0 + start + 60, U, device=device)
    last = db._conn.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
    return last - 1, last


def resolve(db, device="alpha", now=T0 + 90_000):
    i = next(x for x in db.list_incidents(view="active") if x.device == device)
    return db.resolve_incident(i.id, "alice", "Reseated the cable; link stable since.", None, now)


def test_everything_is_unresolved_until_an_incident_is_resolved(synced):
    a, b = episode(synced, 0)
    process(synced)
    assert ids(synced) == [b, a] and ids(synced, "all") == [b, a] and ids(synced, "archived") == []
    assert synced.alert_view_counts() == {"all": 2, "archived": 0, "unresolved": 2}


def test_resolving_moves_only_that_incidents_alerts_to_archived(synced):
    a, b = episode(synced, 0)
    c, d = episode(synced, 5000)  # a separate later incident on the same device
    e, f = episode(synced, 100, device="beta")
    process(synced)
    resolve(synced, "beta")  # not alpha
    assert ids(synced, "archived") == [f, e]
    assert ids(synced, "unresolved") == [d, c, b, a]
    # resolve alpha's newest incident only: its two alerts move, the older alpha alerts stay
    newest = max((i for i in synced.list_incidents(view="active") if i.device == "alpha"),
                 key=lambda i: i.opened_ts)
    synced.resolve_incident(newest.id, "alice", "Replaced the faulty patch cable.", None, T0 + 99)
    assert sorted(ids(synced, "archived")) == sorted([c, d, e, f])
    assert sorted(ids(synced, "unresolved")) == sorted([a, b])
    assert sorted(ids(synced, "all")) == sorted([a, b, c, d, e, f])
    assert synced.alert_view_counts() == {"all": 6, "archived": 4, "unresolved": 2}


def test_unrelated_alerts_from_the_same_device_are_never_hidden(synced):
    a, b = episode(synced, 0)
    raise_alert(synced, T0 + 9000, D, U)  # a later, still-open incident
    c = synced._conn.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
    process(synced)
    first = min(synced.list_incidents(view="active"), key=lambda i: i.opened_ts)
    synced.resolve_incident(first.id, "alice", "Replaced the faulty patch cable.", None, T0 + 99)
    assert sorted(ids(synced, "archived")) == sorted([a, b])
    assert ids(synced, "unresolved") == [c]  # same device, different incident: still visible


def test_reopening_brings_the_alerts_back_and_resolving_again_hides_them(synced):
    a, b = episode(synced, 0)
    process(synced)
    i = resolve(synced)
    assert ids(synced) == []
    synced.reopen_incident(i.id, "bob", "It dropped again on the same port.", T0 + 100_000)
    assert ids(synced) == [b, a] and ids(synced, "archived") == []
    synced.resolve_incident(i.id, "bob", "Moved the cable to another port.", None, T0 + 110_000)
    assert ids(synced) == [] and sorted(ids(synced, "archived")) == sorted([a, b])


def test_unmatched_alerts_stay_visible_until_placed_in_an_incident_and_resolved(synced):
    raise_alert(synced, T0, U, X)  # a recovery with no fault: can never be placed
    raise_alert(synced, T0 + 10, X)
    raise_alert(synced, T0 + 20, U)
    treat_as_history(synced)  # all three are "from before incident management"
    assert ids(synced) == [3, 2, 1]  # none is linked to an incident, so none is hidden
    do_import(synced)
    assert ids(synced) == [3, 2, 1]  # now linked, but the incident is unresolved: still shown
    rows = {r["id"]: r for r in synced.list_alerts(view="all")["rows"]}
    assert rows[1]["incident_id"] is None and rows[1]["skipped_reason"] is not None
    assert rows[2]["incident_id"] == rows[3]["incident_id"] is not None
    resolve(synced)
    assert ids(synced) == [1]  # only the unplaceable recovery is left in the default view
    assert sorted(ids(synced, "archived")) == [2, 3]
    assert sorted(ids(synced, "all")) == [1, 2, 3]


def test_alerts_are_never_modified_or_deleted_by_any_of_this(synced):
    episode(synced, 0)
    episode(synced, 9000, device="beta")
    treat_as_history(synced)
    before = snapshot(synced, "alerts")
    do_import(synced)
    i = resolve(synced)
    synced.reopen_incident(i.id, "bob", "Reopened for the test.", T0 + 120_000)
    resolve(synced, now=T0 + 130_000)
    for view in ("unresolved", "archived", "all"):
        synced.list_alerts(view=view, limit=1)
    assert snapshot(synced, "alerts") == before


def test_limit_offset_and_total_are_applied_per_view(synced):
    for n in range(5):  # five alpha episodes, 10 alerts
        episode(synced, n * 5000)
    process(synced)
    for incident in synced.list_incidents(view="active")[:2]:  # archive two of them
        synced.resolve_incident(incident.id, "alice", "Documented and closed it.", None, T0 + 9e5)
    assert synced.alert_view_counts() == {"all": 10, "archived": 4, "unresolved": 6}
    everything = ids(synced, "unresolved", limit=500)
    assert len(everything) == 6
    # a limit counts rows of the CHOSEN view, so archived alerts never use up the page
    assert ids(synced, "unresolved", limit=4) == everything[:4]
    assert ids(synced, "unresolved", limit=4, offset=4) == everything[4:]
    assert ids(synced, "unresolved", limit=4, offset=6) == []
    page = synced.list_alerts(view="unresolved", limit=4, offset=2)
    assert (page["total"], len(page["rows"])) == (6, 4)
    assert synced.list_alerts(view="archived", limit=3)["total"] == 4
    assert synced.list_alerts(view="all", limit=3)["total"] == 10
    allrows = synced.list_alerts(view="all", limit=500)["rows"]
    assert [r["ts"] for r in allrows] == sorted((r["ts"] for r in allrows), reverse=True)


# ------------------------------------------------------------------ the API
def test_without_a_view_the_api_returns_every_alert_as_it_always_did(db, devices):
    """Existing API consumers send no `view`: they must still get all recent alerts, including the
    alerts of resolved incidents, newest first, with the original fields."""
    app = make_app(db, devices)
    a, b = episode(db, 0)
    c, d = episode(db, 5000)
    process(db)
    first = min(db.list_incidents(view="active"), key=lambda i: i.opened_ts)
    db.resolve_incident(first.id, "alice", "Reseated and verified it.", None, T0 + 9e5)
    client = local_client(app)
    body = client.get("/api/alerts").json()
    assert [x["id"] for x in body["alerts"]] == [d, c, b, a]  # nothing hidden
    assert (body["view"], body["total"]) == ("all", 4)
    assert body["alerts"] == client.get("/api/alerts?view=all").json()["alerts"]
    for row in body["alerts"]:  # the original fields are all still there
        assert {"id", "device", "ts", "severity", "from_status", "to_status", "transition",
                "message", "age_s"} <= set(row)
    assert [x["id"] for x in client.get("/api/alerts?limit=2").json()["alerts"]] == [d, c]


def test_the_unresolved_and_archived_views_are_opt_in_and_report_counts_and_paging(db, devices):
    app = make_app(db, devices)
    a, b = episode(db, 0)
    c, d = episode(db, 5000)
    process(db)
    first = min(db.list_incidents(view="active"), key=lambda i: i.opened_ts)
    db.resolve_incident(first.id, "alice", "Reseated and verified it.", None, T0 + 9e5)
    client = local_client(app)
    body = client.get("/api/alerts?view=unresolved").json()
    assert (body["view"], body["total"], body["has_more"], body["incidents_enabled"]) == (
        "unresolved", 2, False, True)
    assert [x["id"] for x in body["alerts"]] == [d, c]
    assert body["counts"] == {"all": 4, "archived": 2, "unresolved": 2}
    archived = client.get("/api/alerts?view=archived").json()
    assert [x["id"] for x in archived["alerts"]] == [b, a]
    assert {x["incident_review"] for x in archived["alerts"]} == {"resolved"}
    assert {x["incident_display_id"] for x in archived["alerts"]} == {f"INC-{first.id:04d}"}
    paged = client.get("/api/alerts?view=all&limit=3&offset=0").json()
    assert (len(paged["alerts"]), paged["total"], paged["has_more"]) == (3, 4, True)
    last = client.get("/api/alerts?view=all&limit=3&offset=3").json()
    assert [x["id"] for x in last["alerts"]] == [a] and last["has_more"] is False
    pages = [x["id"] for off in (0, 1) for x in client.get(
        f"/api/alerts?view=unresolved&limit=1&offset={off}").json()["alerts"]]
    assert pages == [d, c]


def test_the_dashboard_explicitly_asks_for_the_unresolved_view(db, devices):
    page = local_client(make_app(db, devices)).get("/").text
    assert '"&view=" + view' in page and 'let alertView = "unresolved"' in page
    assert 'data-alertview="unresolved" aria-selected="true"' in page  # the default tab
    assert "filter=" not in page.split("/api/alerts")[1][:80]


@pytest.mark.parametrize("query", ["view=nope", "view=", "offset=-1", "offset=abc",
                                   "limit=0", "limit=501", "offset=1000001"])
def test_the_alert_query_parameters_are_validated(db, devices, query):
    assert local_client(make_app(db, devices)).get(f"/api/alerts?{query}").status_code == 422


def test_the_alert_endpoint_stays_read_only(db, devices):
    client = local_client(make_app(db, devices))
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/alerts").status_code == 405


def test_resolving_through_the_api_moves_alerts_and_reopening_restores_them(db, devices):
    app = make_app(db, devices)
    a, b = episode(db, 0)
    process(db)
    client = local_client(app)
    h = headers(client)
    incident = db.list_incidents(view="active")[0]
    assert [x["id"] for x in client.get("/api/alerts?view=unresolved").json()["alerts"]] == [b, a]
    assert client.post(f"/api/incidents/{incident.id}/resolve", headers=h, json={
        "resolution_summary": "Reseated the cable; link stable."}).status_code == 200
    assert client.get("/api/alerts?view=unresolved").json()["alerts"] == []
    assert [x["id"] for x in client.get("/api/alerts?view=archived").json()["alerts"]] == [b, a]
    assert len(client.get("/api/alerts?view=all").json()["alerts"]) == 2
    assert client.post(f"/api/incidents/{incident.id}/reopen", headers=h, json={
        "reason": "It happened again."}).status_code == 200
    assert [x["id"] for x in client.get("/api/alerts?view=unresolved").json()["alerts"]] == [b, a]
    assert client.get("/api/alerts?view=archived").json()["alerts"] == []


def test_with_incident_management_disabled_every_alert_is_shown(db, devices):
    app = make_app(db, devices, incidents=IncidentsConfig(enabled=False))
    a, b = episode(db, 0)
    process(db)  # (nothing links alerts while disabled, but a resolved incident may exist)
    body = local_client(app).get("/api/alerts?view=archived").json()
    assert body["view"] == "all" and body["incidents_enabled"] is False
    assert [x["id"] for x in body["alerts"]] == [b, a]


def test_a_remote_client_sees_the_same_alert_views(db, devices):
    from incident_helpers import remote_client

    app = make_app(db, devices)
    episode(db, 0)
    process(db)
    assert remote_client(app).get("/api/alerts?view=all").json()["total"] == 2


def test_imported_alerts_follow_their_incident_through_the_views(db, devices):
    app = make_app(db, devices)
    a, b = episode(db, 0)
    treat_as_history(db)
    do_import(db)
    client = local_client(app)
    shown = client.get("/api/alerts?view=unresolved").json()["alerts"]
    assert [x["incident_display_id"] for x in shown] == ["INC-0001", "INC-0001"]
    incident = db.list_incidents(view="active")[0]
    client.post(f"/api/incidents/{incident.id}/resolve", headers=headers(client),
                json={"resolution_summary": "Closed after review of the history."})
    assert client.get("/api/alerts?view=unresolved").json()["alerts"] == []
    assert inc.display_id(incident.id) == "INC-0001"
    assert len(client.get("/api/alerts?view=archived").json()["alerts"]) == 2
