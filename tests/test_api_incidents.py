"""Incident API: validation, the write-protection model, safe handling of hostile notes,
simulated versus real write-ups, and that monitoring endpoints are unaffected."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClock
from netmonlt import incidents as inc
from netmonlt.alerts import Alert, AlertState, Severity
from netmonlt.api import create_app
from netmonlt.domain import Device, IncidentsConfig, SimProfile, Status
from netmonlt.incident_engine import process_once
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber

U, D, X = Status.UP, Status.DEGRADED, Status.DOWN
T0 = 1_700_000_000.0
LOCAL = {"client": ("127.0.0.1", 50123), "base_url": "http://localhost"}
JSON = {"Content-Type": "application/json"}


def raise_alert(db, ts, new, old=None, device="alpha"):
    old = old or (U if new is not U else X)
    sev = {X: Severity.CRITICAL, D: Severity.WARNING, U: Severity.INFO}[new]
    db.save_alert_state(device, AlertState(status=new, since=ts, updated=ts),
                        Alert(device, ts, sev, old, new, f"{device} went {new.value}"))


def make_app(db, devices, *, mode="simulated", incidents=None, clock=None):
    inventory = Inventory(mode=mode, poll_interval_s=5, devices=tuple(devices),
                          incidents=incidents or IncidentsConfig())
    db.sync_devices(devices)
    return create_app(inventory, db, SimulatedProber(), start_collector=False,
                      clock=clock or FakeClock(T0 + 1000))


def local_client(app):
    return TestClient(app, **LOCAL)


def token_of(client):
    page = client.get("/").text
    marker = 'name="netmonlt-token" content="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def headers(client):
    return {**JSON, "X-NetMonLT-Token": token_of(client)}


@pytest.fixture
def app(db, devices):
    return make_app(db, devices)


@pytest.fixture
def seeded(db, app):
    """alpha went down and recovered; beta is still down."""
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 1, X, device="beta")
    raise_alert(db, T0 + 90, U)
    process_once(db, IncidentsConfig(), simulated=True, run_started_ts=T0 - 5,
                 clock=lambda: T0 + 1000)
    return app


def ids(client, **params):
    return {i["device"]: i["id"] for i in client.get("/api/incidents", params=params).json()
            ["incidents"]}


# ------------------------------------------------------------------ reading
def test_list_is_empty_until_something_happens(app):
    body = local_client(app).get("/api/incidents").json()
    assert body["incidents"] == [] and body["enabled"] is True and body["simulated"] is True
    assert body["counts"] == {"active": 0, "history": 0, "awaiting_review": 0,
                              "recovery_unknown": 0, "import_candidates": 0}
    assert (body["correlation_window_s"], body["flapping_recurrences"]) == (900.0, 2)


def test_list_shows_lifecycle_with_monitoring_and_review_kept_apart(seeded):
    client = local_client(seeded)
    rows = {i["device"]: i for i in client.get("/api/incidents").json()["incidents"]}
    alpha, beta = rows["alpha"], rows["beta"]
    assert alpha["display_id"].startswith("INC-") and alpha["simulated"] is True
    assert alpha["lifecycle"]["label"] == "Auto-recovered — awaiting review"
    assert (alpha["lifecycle"]["monitoring"], alpha["lifecycle"]["review"]) == ("recovered", "open")
    assert alpha["recovered_ts"] == T0 + 90 and alpha["alert_events"] == 2
    assert beta["lifecycle"]["label"] == "Open" and beta["recovered_ts"] is None
    assert client.get("/api/incidents").json()["counts"]["awaiting_review"] == 1


def test_filters_and_validation_of_query_parameters(seeded):
    client = local_client(seeded)
    assert set(ids(client, severity="critical")) == {"alpha", "beta"}
    assert set(ids(client, status="recovered")) == {"alpha"}
    assert set(ids(client, device="beta")) == {"beta"}
    assert client.get("/api/incidents", params={"view": "history"}).json()["incidents"] == []
    for bad in ({"view": "nope"}, {"status": "closed"}, {"severity": "fatal"}, {"limit": 0},
                {"limit": 501}, {"device": "x" * 65}):
        assert client.get("/api/incidents", params=bad).status_code == 422


def test_detail_has_timeline_evidence_and_a_copyable_writeup(seeded):
    client = local_client(seeded)
    alpha = ids(client)["alpha"]
    body = client.get(f"/api/incidents/{alpha}").json()
    assert [t["kind"] for t in body["report"]["observed"]["timeline"]] == ["opened", "recovered"]
    assert body["report"]["id"] == f"INC-{alpha:04d}"
    assert body["markdown"].startswith(f"# Incident INC-{alpha:04d}: alpha")
    assert body["can_modify"] is True and body["read_only_reason"] is None
    plain = client.get(f"/api/incidents/{alpha}/report")
    assert plain.headers["content-type"].startswith("text/markdown")
    assert plain.text == body["markdown"]
    assert client.get(f"/api/incidents/{alpha}/report?format=json").json()["device"] == "alpha"
    assert client.get(f"/api/incidents/{alpha}/report?format=pdf").status_code == 422


def test_unknown_incidents_are_404_everywhere(app):
    client = local_client(app)
    assert client.get("/api/incidents/999").status_code == 404
    assert client.get("/api/incidents/999/report").status_code == 404
    for path, body in (("acknowledge", {}), ("notes", {"text": "hi"}), ("reopen",
                                                                       {"reason": "because"})):
        assert client.post(f"/api/incidents/999/{path}", json=body,
                           headers=headers(client)).status_code == 404
    assert client.get("/api/incidents/not-a-number").status_code == 422


# ------------------------------------------------------------------ the workflow
def test_full_workflow_over_the_api(seeded):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    url = f"/api/incidents/{alpha}"

    ack = client.post(f"{url}/acknowledge", json={"actor": "alice", "note": "on it"}, headers=h)
    assert ack.status_code == 200
    assert ack.json()["incident"]["lifecycle"]["label"] == "Monitoring recovered — acknowledged"

    note = client.post(f"{url}/notes", json={"actor": "alice", "text": "Cable looked fine."},
                       headers=h)
    assert note.status_code == 200 and note.json()["event_id"]

    put = client.put(f"{url}/report", json={"actor": "alice", "root_cause": "Loose cable",
                                            "root_cause_status": "suspected"}, headers=h)
    assert put.status_code == 200

    res = client.post(f"{url}/resolve", json={
        "actor": "alice", "resolution_summary": "Reseated the cable; link stable since.",
        "actions_taken": "Reseated the patch cable."}, headers=h)
    assert res.status_code == 200 and res.json()["incident"]["lifecycle"]["code"] == "resolved"

    assert "alpha" not in ids(client)  # gone from the default Active view
    assert "alpha" in ids(client, view="history")  # ...and kept in History
    detail = client.get(url).json()
    assert detail["report"]["operator"]["actions_taken"] == "Reseated the patch cable."
    assert detail["report"]["resolved_at"] is not None
    kinds = [t["kind"] for t in detail["report"]["observed"]["timeline"]]
    assert kinds == ["opened", "recovered", "acknowledged", "note", inc.EV_REPORT, inc.EV_REPORT,
                     "resolved"]

    re = client.post(f"{url}/reopen", json={"actor": "bob", "reason": "Dropped again."}, headers=h)
    assert re.status_code == 200 and "alpha" in ids(client)
    assert client.get(url).json()["incident"]["reopened_count"] == 1


def test_invalid_transitions_return_409_with_a_code(seeded):
    client = local_client(seeded)
    h = headers(client)
    beta = ids(client)["beta"]  # still down
    r = client.post(f"/api/incidents/{beta}/resolve",
                    json={"resolution_summary": "Fixed it, honestly."}, headers=h)
    assert (r.status_code, r.json()["code"]) == (409, "invalid_transition")
    assert client.post(f"/api/incidents/{beta}/reopen", json={"reason": "never closed"},
                       headers=h).status_code == 409
    assert client.post(f"/api/incidents/{beta}/acknowledge", json={}, headers=h).status_code == 200
    assert client.post(f"/api/incidents/{beta}/acknowledge", json={},
                       headers=h).status_code == 409


@pytest.mark.parametrize("path, body", [
    ("resolve", {}),  # summary required
    ("resolve", {"resolution_summary": "short"}),
    ("resolve", {"resolution_summary": "x" * 4001}),
    ("resolve", {"resolution_summary": "Long enough summary.", "root_cause_status": "certain"}),
    ("resolve", {"resolution_summary": "Long enough summary.", "surprise": 1}),
    ("resolve", {"resolution_summary": "Long enough summary.", "actor": "<b>x</b>"}),
    ("notes", {}), ("notes", {"text": ""}), ("notes", {"text": "x" * 4001}),
    ("notes", {"text": 5}), ("notes", {"text": "ok", "actor": "x" * 41}),
    ("notes", {"text": "ok", "extra": True}),
    ("acknowledge", {"note": "x" * 4001}), ("acknowledge", {"nope": 1}),
    ("reopen", {}), ("reopen", {"reason": "no"}),
])
def test_bad_request_bodies_are_rejected_and_change_nothing(seeded, path, body):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    before = client.get(f"/api/incidents/{alpha}").json()["report"]["observed"]["timeline"]
    r = client.post(f"/api/incidents/{alpha}/{path}", json=body, headers=h)
    assert r.status_code in (409, 422), r.text
    assert client.get(f"/api/incidents/{alpha}").json()["report"]["observed"]["timeline"] == before


def test_report_update_needs_at_least_one_field_and_a_valid_root_cause(seeded):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    url = f"/api/incidents/{alpha}/report"
    assert client.put(url, json={}, headers=h).status_code == 422
    assert client.put(url, json={"root_cause_status": "confirmed"}, headers=h).status_code == 422
    ok = client.put(url, json={"root_cause": "ISP outage", "root_cause_status": "confirmed"},
                    headers=h)
    assert ok.status_code == 200


def test_sql_metacharacters_are_data_not_code(seeded, db):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    evil = "'); DROP TABLE incidents; --"
    assert client.post(f"/api/incidents/{alpha}/notes", json={"text": evil}, headers=h
                       ).status_code == 200
    assert client.get("/api/incidents", params={"device": evil[:60]}).json()["incidents"] == []
    assert client.get("/api/incidents/1%20OR%201=1").status_code == 422
    texts = [t["text"] for t in client.get(f"/api/incidents/{alpha}").json()["report"]
             ["observed"]["timeline"]]
    assert evil in texts and db.incident_counts()["active"] == 2


# ------------------------------------------------------------------ protection of state changes
POSTS = [("acknowledge", {}), ("notes", {"text": "hello"}),
         ("resolve", {"resolution_summary": "Long enough summary."}),
         ("reopen", {"reason": "because reasons"})]


def all_state(db):
    return (db._conn.execute("SELECT COUNT(*) FROM incident_events").fetchone()[0],
            [tuple(r) for r in db._conn.execute("SELECT * FROM incidents ORDER BY id")])


def attempt(client, alpha, headers_, **kw):
    out = []
    for path, body in POSTS:
        out.append(client.post(f"/api/incidents/{alpha}/{path}", json=body, headers=headers_,
                               **kw).status_code)
    out.append(client.put(f"/api/incidents/{alpha}/report", json={"root_cause": "x"},
                          headers=headers_, **kw).status_code)
    return out


def test_a_remote_client_can_read_but_never_change_incidents(seeded, db):
    local = local_client(seeded)
    alpha = ids(local)["alpha"]
    good = headers(local)
    before = all_state(db)
    remote = TestClient(seeded, client=("192.0.2.50", 40000), base_url="http://localhost")
    assert remote.get("/api/incidents").status_code == 200  # reading is allowed
    detail = remote.get(f"/api/incidents/{alpha}").json()
    assert detail["can_modify"] is False and "only accepted from the machine" in detail[
        "read_only_reason"]
    assert attempt(remote, alpha, good) == [403] * 5  # even with a valid token
    assert all_state(db) == before


def test_the_token_is_never_handed_to_a_remote_page_view(seeded):
    local, remote = local_client(seeded), TestClient(
        seeded, client=("10.0.0.9", 1234), base_url="http://localhost")
    assert len(token_of(local)) >= 32
    assert 'name="netmonlt-token" content=""' in remote.get("/").text
    assert seeded.state.incident_token not in remote.get("/").text
    assert remote.get("/").headers["cache-control"] == "no-store"
    assert local.get("/").headers["cache-control"] == "no-store"


def test_changes_need_the_per_process_token(seeded, db):
    client = local_client(seeded)
    alpha = ids(client)["alpha"]
    before = all_state(db)
    assert attempt(client, alpha, JSON) == [403] * 5  # no token
    assert attempt(client, alpha, {**JSON, "X-NetMonLT-Token": "guess"}) == [403] * 5
    assert attempt(client, alpha, {**JSON, "X-NetMonLT-Token": ""}) == [403] * 5
    assert all_state(db) == before
    assert client.post(f"/api/incidents/{alpha}/acknowledge", json={},
                       headers=headers(client)).status_code == 200


def test_changes_refuse_foreign_origins_hosts_and_non_json_bodies(seeded, db):
    client = local_client(seeded)
    alpha = ids(client)["alpha"]
    h = headers(client)
    before = all_state(db)
    url = f"/api/incidents/{alpha}/notes"
    cross = client.post(url, json={"text": "hi"}, headers={**h, "Origin": "http://evil.example"})
    assert cross.status_code == 403
    rebinding = TestClient(seeded, client=("127.0.0.1", 1), base_url="http://evil.example")
    assert rebinding.post(url, json={"text": "hi"}, headers=h).status_code == 403
    token = {"X-NetMonLT-Token": h["X-NetMonLT-Token"]}
    form = client.post(url, data={"text": "hi"}, headers=token)
    assert form.status_code == 415  # a cross-site HTML form cannot reach the handler
    plain = client.post(url, content='{"text":"hi"}', headers={
        "Content-Type": "text/plain", **token})
    assert plain.status_code == 415
    assert all_state(db) == before
    same_site = client.post(url, json={"text": "hi"}, headers={**h, "Origin": "http://localhost:8000"})
    assert same_site.status_code == 200


def test_ipv6_loopback_and_mapped_addresses_count_as_local(seeded):
    for host in ("::1", "::ffff:127.0.0.1"):
        client = TestClient(seeded, client=(host, 5), base_url="http://localhost")
        assert client.get("/api/incidents").json()["can_modify"] is True


def test_there_are_no_delete_routes_for_incidents(seeded):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    for url in (f"/api/incidents/{alpha}", f"/api/incidents/{alpha}/notes", "/api/incidents"):
        assert client.delete(url, headers=h).status_code in (404, 405)
    paths = {(m, r.path) for r in seeded.routes if hasattr(r, "methods") for m in r.methods}
    assert not [p for p in paths if p[0] == "DELETE" and "incident" in p[1]]


def test_operator_actions_can_be_switched_off(db, devices):
    app = make_app(db, devices, incidents=IncidentsConfig(operator_actions=False))
    raise_alert(db, T0, X)
    process_once(db, IncidentsConfig(), simulated=True, run_started_ts=None, clock=lambda: T0)
    client = local_client(app)
    body = client.get("/api/incidents").json()
    assert body["can_modify"] is False and "read-only" in body["read_only_reason"]
    assert client.post("/api/incidents/1/acknowledge", json={}, headers=headers(client)
                       ).status_code == 403
    assert len(body["incidents"]) == 1  # still readable
    assert 'name="netmonlt-token" content=""' in client.get("/").text


def test_incident_management_can_be_disabled_entirely(db, devices):
    app = make_app(db, devices, incidents=IncidentsConfig(enabled=False))
    client = local_client(app)
    body = client.get("/api/incidents").json()
    assert body["enabled"] is False and body["incidents"] == []
    assert client.get("/api/incidents/1").status_code == 404
    assert client.post("/api/incidents/1/acknowledge", json={}, headers=headers(client)
                       ).status_code == 403
    assert client.get("/api/devices").status_code == 200  # the rest of the app is unaffected


# ------------------------------------------------------------------ hostile notes
HOSTILE = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
           '"><svg/onload=alert(1)>', "javascript:alert(1)", "{{7*7}}", "${7*7}",
           "<!--", "]]></script><script>alert(1)</script>"]


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_notes_are_stored_as_text_and_never_become_markup_in_the_page(seeded, payload):
    client = local_client(seeded)
    h = headers(client)
    alpha = ids(client)["alpha"]
    assert client.post(f"/api/incidents/{alpha}/notes", json={"actor": "alice", "text": payload},
                       headers=h).status_code == 200
    assert client.put(f"/api/incidents/{alpha}/report", json={"root_cause": payload},
                      headers=h).status_code == 200
    detail = client.get(f"/api/incidents/{alpha}")
    assert detail.headers["content-type"].startswith("application/json")  # data, not a page
    assert payload in detail.json()["markdown"]
    page = client.get("/").text
    assert payload not in page  # notes are not rendered server-side into the page at all
    assert client.get(f"/api/incidents/{alpha}/report").headers["content-type"].startswith(
        "text/markdown")  # served as text, never text/html


def test_the_dashboard_script_never_writes_incident_text_as_html():
    from helpers import REPO_ROOT

    page = (REPO_ROOT / "src/netmonlt/templates/dashboard.html").read_text(encoding="utf-8")
    assert "function refreshIncidents" in page
    script = page[page.index("<script>"):]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                      "new Function"):
        assert forbidden not in script


# ------------------------------------------------------------------ simulated versus real write-ups
def test_simulated_incidents_carry_a_labelled_illustrative_example(db):
    devs = [Device("alpha", "192.0.2.1", "router",
                   SimProfile(outage_start_s=5, outage_duration_s=60)),
            Device("beta", "192.0.2.2", "switch", SimProfile())]
    app = make_app(db, devs, mode="simulated")
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    process_once(db, IncidentsConfig(), simulated=True, run_started_ts=T0 - 10,
                 clock=lambda: T0 + 1000)
    client = local_client(app)
    alpha = ids(client)["alpha"]
    report = client.get(f"/api/incidents/{alpha}/report?format=json").json()
    example = report["simulated_example"]
    assert report["simulated"] is True and report["data_label"].startswith("SIMULATED")
    assert "outage_start_s=5" in example["known_scenario_cause"]["text"]
    assert example["illustrative_troubleshooting_steps"][0].startswith("Check the router")
    assert all(report["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)
    md = client.get(f"/api/incidents/{alpha}/report").text
    assert "SIMULATED / ILLUSTRATIVE EXAMPLE" in md and "NetMonLT performed no repair" in md
    assert client.get(f"/api/incidents/{alpha}/report").text == md  # deterministic


def test_real_incidents_get_a_template_with_no_invented_cause_or_steps(db):
    devs = [Device("alpha", "192.0.2.1", "router", SimProfile(outage_start_s=5,
                                                              outage_duration_s=60),
                   authorized=True)]
    app = make_app(db, devs, mode="real")
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    process_once(db, IncidentsConfig(), simulated=False, run_started_ts=None,
                 clock=lambda: T0 + 1000)
    client = local_client(app)
    alpha = ids(client)["alpha"]
    report = client.get(f"/api/incidents/{alpha}/report?format=json").json()
    assert report["simulated"] is False and report["simulated_example"] is None
    assert report["data_label"] == "Real monitoring data"
    assert all(report["operator"][f] == "" for f in inc.REPORT_TEXT_FIELDS)
    assert report["operator"]["root_cause_status"] == "unconfirmed"
    md = client.get(f"/api/incidents/{alpha}/report").text
    assert "ILLUSTRATIVE" not in md and "fictional" not in md and "outage_start_s" not in md
    assert md.count("_Not yet documented by an operator._") == 6


# ------------------------------------------------------------------ other monitoring is unaffected
def test_monitoring_endpoints_are_unchanged_by_incidents(seeded):
    client = local_client(seeded)
    assert [d["name"] for d in client.get("/api/devices").json()["devices"]] == ["alpha", "beta"]
    alerts = client.get("/api/alerts").json()["alerts"]
    assert [(a["device"], a["to_status"]) for a in alerts] == [
        ("alpha", "up"), ("beta", "down"), ("alpha", "down")]
    alpha = ids(client)["alpha"]
    client.post(f"/api/incidents/{alpha}/acknowledge", json={}, headers=headers(client))
    after = client.get("/api/alerts").json()["alerts"]
    link = ("incident_review",)  # the only thing that may differ: the linked incident's state
    assert [{k: v for k, v in a.items() if k not in link} for a in after] == [
        {k: v for k, v in a.items() if k not in link} for a in alerts]  # no alert changed


def test_the_background_task_builds_incidents_while_the_app_runs(db, devices):
    app = make_app(db, devices, clock=FakeClock(T0))
    raise_alert(db, T0, X)

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)

    # With the collector off nothing runs in the background (tests drive polling themselves)...
    asyncio.run(run())
    assert db.list_incidents(view="all") == []
    # ...and with it on, the incident task is started alongside it.
    inventory = Inventory(mode="simulated", poll_interval_s=5, devices=tuple(devices))
    live = create_app(inventory, db, SimulatedProber(), start_collector=True, clock=FakeClock(T0))

    async def run_live():
        async with live.router.lifespan_context(live):
            for _ in range(100):
                await asyncio.sleep(0.05)
                if db.list_incidents(view="all"):
                    return

    asyncio.run(asyncio.wait_for(run_live(), 10))
    assert [i.device for i in db.list_incidents(view="all")] == ["alpha"]


def test_the_guard_runs_before_validation_so_outsiders_learn_nothing(seeded):
    remote = TestClient(seeded, client=("192.0.2.50", 40000), base_url="http://localhost")
    for url in ("/api/incidents/1/notes", "/api/incidents/99999/notes"):
        assert remote.post(url, json={"bogus": 1}).status_code == 403  # not 422 or 404
    local = local_client(seeded)
    assert local.post("/api/incidents/99999/notes", json={"text": "x"}).status_code == 403


# ------------------------------------------------------------------ real mode is opt-in for changes
REAL_DEVICES = [Device("alpha", "192.0.2.1", "router", SimProfile(), authorized=True)]


def real_app(db, **cfg):
    app = make_app(db, REAL_DEVICES, mode="real", incidents=IncidentsConfig(**cfg))
    raise_alert(db, T0, X)
    raise_alert(db, T0 + 90, U)
    process_once(db, IncidentsConfig(), simulated=False, run_started_ts=None,
                 clock=lambda: T0 + 1000)
    return app


def test_real_mode_incidents_are_read_only_until_the_inventory_opts_in(db):
    client = local_client(real_app(db))  # nothing configured
    body = client.get("/api/incidents").json()
    assert body["simulated"] is False and body["can_modify"] is False
    assert "off by default in real mode" in body["read_only_reason"]
    assert "operator_actions: true" in body["read_only_reason"]
    assert len(body["incidents"]) == 1  # still fully readable
    assert client.get("/api/incidents/1").status_code == 200
    assert client.get("/api/incidents/1/report").status_code == 200
    before = all_state(db)
    assert attempt(client, 1, headers(client)) == [403] * 5  # even from localhost with a token
    assert all_state(db) == before
    assert 'name="netmonlt-token" content=""' in client.get("/").text  # no token handed out


def test_real_mode_actions_work_once_the_inventory_opts_in(db):
    client = local_client(real_app(db, operator_actions=True))
    assert client.get("/api/incidents").json()["can_modify"] is True
    ack = client.post("/api/incidents/1/acknowledge", json={"actor": "alice"},
                      headers=headers(client))
    assert ack.status_code == 200
    remote = TestClient(client.app, client=("192.0.2.50", 40000), base_url="http://localhost")
    assert remote.get("/api/incidents").json()["can_modify"] is False  # still local-only


def test_real_mode_can_be_explicitly_read_only(db):
    body = local_client(real_app(db, operator_actions=False)).get("/api/incidents").json()
    assert body["can_modify"] is False and "disabled in the inventory" in body["read_only_reason"]


def test_the_simulated_demo_allows_changes_by_default_and_can_be_made_read_only(db, devices):
    assert local_client(make_app(db, devices)).get("/api/incidents").json()["can_modify"] is True
    off = make_app(db, devices, incidents=IncidentsConfig(operator_actions=False))
    assert local_client(off).get("/api/incidents").json()["can_modify"] is False


def test_binding_to_all_interfaces_does_not_open_changes_to_the_network(db, devices):
    """Binding is irrelevant to the check: it looks at who is connecting, so a client on another
    address is refused whether the app listens on 127.0.0.1 or 0.0.0.0, in either mode."""
    for app in (make_app(db, devices), real_app(db, operator_actions=True)):
        local = local_client(app)
        good = headers(local)
        for client_ip in ("192.0.2.50", "10.1.2.3", "203.0.113.9", "2001:db8::1"):
            for host in ("localhost", "0.0.0.0", "192.0.2.2", "netmon.lan"):
                remote = TestClient(app, client=(client_ip, 1234), base_url=f"http://{host}")
                r = remote.post("/api/incidents/1/acknowledge", json={}, headers=good)
                assert r.status_code == 403, (client_ip, host)
        assert db.get_incident(1).review_state == inc.REV_OPEN
