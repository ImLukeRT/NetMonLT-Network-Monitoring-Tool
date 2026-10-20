"""Chromium regression test for the dashboard's view tabs (Stage 10.1).

The bug: clicking a Recent alerts tab set the incident view to `undefined`, and every incident
refresh after that requested /api/incidents?view=undefined (HTTP 422). This drives the real page in
a real browser against the real app. It needs Node.js, the `playwright` package and a Chromium
build, so it is skipped where those are not installed (CI relies on the static tests in
test_dashboard_page.py); set NODE_PATH to a node_modules folder containing playwright, and
NETMONLT_CHROMIUM to a Chromium executable, to run it elsewhere.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from incident_helpers import U, X, raise_alert, snapshot, treat_as_history
from netmonlt import incidents as inc
from netmonlt.api import create_app
from netmonlt.domain import Device, IncidentsConfig, SimProfile
from netmonlt.incident_engine import process_once
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber
from netmonlt.storage import Database

NODE = shutil.which("node")
NODE_PATHS = [p for p in (os.environ.get("NODE_PATH"), "/opt/node-tools/node_modules") if p]
CHROMIUM = next((p for p in (os.environ.get("NETMONLT_CHROMIUM"), "/opt/pw-browsers/chromium")
                 if p and os.path.exists(p)), None)


def _node_env():
    for path in NODE_PATHS:
        if os.path.isdir(os.path.join(path, "playwright")):
            return {**os.environ, "NODE_PATH": path}
    return None


pytestmark = pytest.mark.skipif(
    NODE is None or _node_env() is None or CHROMIUM is None,
    reason="needs Node.js, playwright and a Chromium executable")

SCRIPT = Path(__file__).parent / "browser" / "dashboard_views.js"

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_app(tmp_path):
    db = Database(":memory:")
    devices = [Device("alpha", "192.0.2.1", "router", SimProfile()),
               Device("beta", "192.0.2.2", "switch", SimProfile())]
    db.sync_devices(devices)
    now = time.time()
    raise_alert(db, now - 9000, X)  # history, from before incident management: alpha down ...
    raise_alert(db, now - 8900, U)  # ... and back
    raise_alert(db, now - 8000, X, device="beta")  # a fault that never got a recovery alert
    treat_as_history(db)
    raise_alert(db, now - 300, X, device="beta")  # a live incident (the history above is not)
    process_once(db, IncidentsConfig(), simulated=True, run_started_ts=None, clock=time.time)
    inventory = Inventory(mode="simulated", poll_interval_s=5, devices=tuple(devices))
    app = create_app(inventory, db, SimulatedProber(), start_collector=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/", db
    server.should_exit = True
    thread.join(5)
    db.close()


def _run(base, tmp_path):
    proc = subprocess.run([NODE, str(SCRIPT), base, CHROMIUM], env=_node_env(), text=True,
                          capture_output=True, timeout=120)
    if proc.returncode == 3:  # only "the browser would not start" is a skip: anything else fails
        pytest.skip(proc.stdout.strip()[:200])
    assert proc.returncode == 0, proc.stderr[:500]
    return json.loads(proc.stdout)


def _incident_views(requests):
    return [(dict(p.split("=", 1) for p in search.lstrip("?").split("&"))["view"], status)
            for path, search, status in requests]


def test_incident_requests_always_carry_a_valid_view_through_every_interaction(live_app, tmp_path):
    base, db = live_app
    out = _run(base, tmp_path)
    steps = out["steps"]
    assert out["errors"] == []

    # nothing, anywhere, ever sends "undefined", and no API request failed
    assert not [r for r in out["requests"] if "undefined" in r[1]]
    assert not [r for r in out["requests"] if r[2] >= 400]

    # initial load: Active is selected and requested
    assert steps["initial"]["incidentTabs"] == ["active:true", "history:false"]
    assert _incident_views(steps["initial"]["incidentRequests"]) == [("active", 200)]
    # the periodic refresh keeps asking for Active
    assert steps["periodic"]["incidentRequests"]
    assert {v for v, _ in _incident_views(steps["periodic"]["incidentRequests"])} == {"active"}

    # clicking the ALERT tabs (the regression) leaves the incident view and requests alone
    for view in ("archived", "all", "unresolved"):
        step = steps["alertTabs"][view]
        assert step["incidentTabs"] == ["active:true", "history:false"], view
        assert step["alertTabs"] == [f"{v}:{'true' if v == view else 'false'}"
                                     for v in ("unresolved", "archived", "all")]
        assert step["incidentRequests"], f"the incident refresh after the {view} tab"
        assert {v for v, _ in _incident_views(step["incidentRequests"])} == {"active"}
        assert step["incidentRows"] >= 1 and step["note"] == ""

    # switching incident views sends valid values, including after an alert tab click
    assert steps["history"]["incidentTabs"] == ["active:false", "history:true"]
    assert {v for v, _ in _incident_views(steps["history"]["incidentRequests"])} == {"history"}
    assert steps["historyThenAlertTab"]["incidentTabs"] == ["active:false", "history:true"]
    assert {v for v, _ in _incident_views(steps["historyThenAlertTab"]["incidentRequests"])} == {
        "history"}
    assert steps["activeAgain"]["incidentTabs"] == ["active:true", "history:false"]
    assert {v for v, _ in _incident_views(steps["activeAgain"]["incidentRequests"])} == {"active"}

    # the alert requests carry only the three valid views
    alert_views = {dict(p.split("=", 1) for p in s.lstrip("?").split("&"))["view"]
                   for path, s, _ in out["requests"] if path == "/api/alerts"}
    assert alert_views == {"unresolved", "archived", "all"}


def test_the_import_preview_works_and_changes_nothing(live_app, tmp_path):
    base, db = live_app
    tables = ("alerts", "incidents", "incident_events", "incident_imports",
              "incident_import_skips")
    before = {t: snapshot(db, t) for t in tables}
    out = _run(base, tmp_path)
    steps = out["steps"]
    assert "earlier alerts are" in steps["importNotice"] or "earlier alert is" in steps[
        "importNotice"]
    preview = steps["importPreview"]
    assert "would become" in preview["summary"] and "recovery unknown" in preview["summary"]
    assert preview["goDisabled"] is False and preview["goText"].startswith("Import ")
    assert preview["items"] == 2  # alpha (recovered) and beta (no recovery alert)
    assert {t: snapshot(db, t) for t in tables} == before  # a preview writes nothing
    assert db.import_candidate_count() == 3
    assert inc.MON_UNKNOWN == "unknown"
