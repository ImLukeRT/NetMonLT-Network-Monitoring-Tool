"""Chromium test of the simulation auto-documentation and auto-resolution workflow.

It drives the real page against the real app: Acknowledge stores the AUTO-GENERATED example
documentation, a person's note sits beside it, the "Generate example documentation" button serves
an incident acknowledged earlier, nothing is resolved unless auto-resolution is enabled, and then
it is shown as a simulation resolution rather than an operator one. The Recent alerts and incident
view tabs are exercised too (the HTTP 422 regression). Skipped where Node.js, playwright or a
Chromium build is missing (as in CI)."""

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

from incident_helpers import U, X, process_once, raise_alert
from netmonlt import incidents as inc
from netmonlt.api import create_app
from netmonlt.domain import Device, IncidentsConfig, SimProfile
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber
from netmonlt.storage import Database

NODE = shutil.which("node")
NODE_PATHS = [p for p in (os.environ.get("NODE_PATH"), "/opt/node-tools/node_modules") if p]
CHROMIUM = next((p for p in (os.environ.get("NETMONLT_CHROMIUM"), "/opt/pw-browsers/chromium")
                 if p and os.path.exists(p)), None)
SCRIPT = Path(__file__).parent / "browser" / "autodoc_flow.js"


def _node_env():
    for path in NODE_PATHS:
        if os.path.isdir(os.path.join(path, "playwright")):
            return {**os.environ, "NODE_PATH": path}
    return None


pytestmark = pytest.mark.skipif(
    NODE is None or _node_env() is None or CHROMIUM is None,
    reason="needs Node.js, playwright and a Chromium executable")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(cfg, real=False):
    db = Database(":memory:")
    devices = [Device("alpha", "192.0.2.1", "router", SimProfile(outage_start_s=5,
                                                                 outage_duration_s=60),
                      authorized=real),
               Device("beta", "192.0.2.2", "switch", SimProfile(), authorized=real)]
    db.sync_devices(devices)
    now = time.time()
    for device, ago in (("alpha", 2000), ("beta", 1500)):
        raise_alert(db, now - ago, X, device=device)
        raise_alert(db, now - ago + 90, U, device=device)
    process_once(db, cfg, simulated=not real, run_started_ts=None, clock=time.time)
    beta = next(i for i in db.list_incidents(view="all") if i.device == "beta")
    db.add_incident_note(beta.id, "alice", "A note written by a person.", now - 1000)
    db.acknowledge_incident(beta.id, "alice", None, now - 900)  # the old behaviour: no builder
    inventory = Inventory(mode="real" if real else "simulated", poll_interval_s=10 if real else 5,
                          devices=tuple(devices), incidents=cfg)
    app = create_app(inventory, db, SimulatedProber(), start_collector=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return db, server, thread, f"http://127.0.0.1:{port}/"


@pytest.fixture
def run_flow():
    started = []

    def run(cfg, mode):
        db, server, thread, base = start(cfg, real=mode == "real")
        started.append((db, server, thread))
        proc = subprocess.run([NODE, str(SCRIPT), base, CHROMIUM, mode], env=_node_env(),
                              text=True, capture_output=True, timeout=180)
        if proc.returncode == 3:
            pytest.skip(proc.stdout.strip()[:200])
        assert proc.returncode == 0, proc.stderr[:500]
        return db, json.loads(proc.stdout)

    yield run
    for db, server, thread in started:
        server.should_exit = True
        thread.join(5)
        db.close()


def _no_bad_requests(out):
    assert out["errors"] == []
    assert not [r for r in out["requests"] if "undefined" in r[1]]
    assert not [r for r in out["requests"] if r[2] >= 400]


def test_acknowledge_stores_example_documentation_and_never_resolves_by_default(run_flow):
    db, out = run_flow(IncidentsConfig(), "default")
    steps = out["steps"]
    _no_bad_requests(out)

    # before acknowledging: a preview, nothing stored, no resolution
    assert "Preview only" in steps["before"]["example"]
    assert steps["before"]["ackVisible"] and not steps["before"]["resolutionVisible"]

    # acknowledging stores the AUTO-GENERATED documentation but resolves nothing
    after = steps["afterAck"]
    assert "Acknowledged. Example documentation generated" in after["message"]
    assert "SIMULATED EXAMPLE — AUTO-GENERATED" in after["example"]
    assert "Generated automatically at" in after["example"]
    assert "Example investigation notes" in after["example"]
    assert "Monitoring recovered — acknowledged" in after["badges"]
    assert "Monitoring: recovered" in after["badges"]
    assert after["resolutionVisible"] is False and after["ackVisible"] is False
    tags = [t.lower() for t in after["tags"]]  # the page upper-cases these labels
    assert "simulation (auto)" in tags and "operator" in tags
    assert all(v == "" for v in after["operatorFields"])  # nothing was written for a person
    assert after["generateButton"] == 0  # already generated

    # a person's note sits next to the automatic entries, tagged differently
    note = steps["afterNote"]
    assert {t.lower() for t in note["tags"]} >= {"netmonlt", "operator", "simulation (auto)"}
    lines = [line.lower() for line in note["timeline"]]
    assert any("operator" in line and "checked the uplink myself" in line for line in lines)
    assert any("simulation (auto)" in line and "example documentation generated" in line
               for line in lines)
    assert all(v == "" for v in note["operatorFields"])

    # the view tabs still send valid requests, through the periodic refresh too
    assert steps["tabs"]["incidentTabs"] == ["active:true", "history:false"]
    assert steps["tabs"]["incidentRequests"] >= 3

    # the incident acknowledged before the feature: the button generates it, once
    assert "Preview only" in steps["beta"]["example"] and steps["beta"]["button"] == 1
    beta = steps["betaAfter"]
    assert "Example documentation generated" in beta["message"]
    assert "Generated automatically at" in beta["example"] and beta["button"] == 0
    assert any("A note written by a person." in line for line in beta["timeline"])
    assert all(v == "" for v in beta["operatorFields"])

    # in the database: one document each, no resolution, operator content intact
    for i in db.list_incidents(view="all"):
        assert db.get_auto_doc(i.id) is not None and i.review_state == inc.REV_ACKNOWLEDGED
        assert not [e for e in db.incident_events(i.id) if e.kind == inc.EV_AUTO_RESOLVED]
    assert db._conn.execute("SELECT COUNT(*) FROM incident_auto_docs").fetchone()[0] == 2


def test_enabled_auto_resolution_is_shown_as_a_simulation_resolution_not_an_operator_one(run_flow):
    db, out = run_flow(IncidentsConfig(simulation_auto_resolve=True), "auto")
    steps = out["steps"]
    _no_bad_requests(out)
    after = steps["afterAck"]
    assert "The simulation then auto-resolved it (no operator resolution)" in after["message"]
    assert "Resolution: simulation auto-resolved (no operator)" in after["badges"]
    assert "Monitoring: recovered" in after["badges"]  # monitoring's own record stays separate
    assert after["resolutionVisible"] and "SIMULATION AUTO-RESOLUTION" in after["resolutionText"]
    assert "no operator reviewed it" in after["resolutionText"]
    assert "without any verified repair" in after["resolutionText"]
    assert all(v == "" for v in after["operatorFields"])
    assert "simulation (auto)" in [t.lower() for t in after["tags"]]
    assert not any("alpha" in r for r in steps["activeRows"])  # gone from Active
    assert any("alpha" in r and "Auto-resolved (simulation)" in r for r in steps["historyRows"])
    assert any(b.startswith("badge auto|Auto-resolved (simulation)") for b in steps["historyBadge"])
    md = steps["markdown"]
    assert "SIMULATION AUTO-RESOLUTION (simulation-auto)" in md
    assert "## 9. Resolution (SIMULATION AUTO-RESOLUTION: not an operator)" in md
    # the alert views follow an auto-resolution like any other resolution
    assert steps["alertViews"]["archived"] == 2 and steps["alertViews"]["unresolved"] >= 2
    alpha = next(i for i in db.list_incidents(view="all") if i.device == "alpha")
    assert (alpha.resolved_by, alpha.report) == ("simulation-auto", {})


def test_real_mode_shows_no_auto_documentation_and_no_simulation_entries(run_flow):
    db, out = run_flow(IncidentsConfig(operator_actions=True), "real")
    steps = out["steps"]
    assert out["errors"] == [] and not [r for r in out["requests"] if r[2] >= 400]
    before, after = steps["before"], steps["afterAck"]
    assert before["example"] == "" and after["example"] == ""  # no example block at all
    assert after["message"] == "Acknowledged."  # no "example documentation generated"
    assert after["generateButton"] == 0
    assert "simulation (auto)" not in [t.lower() for t in after["tags"]]
    assert "Monitoring recovered — acknowledged" in after["badges"]
    assert after["resolutionVisible"] is False
    assert db._conn.execute("SELECT COUNT(*) FROM incident_auto_docs").fetchone()[0] == 0
    assert not db._conn.execute(
        "SELECT 1 FROM incident_events WHERE source='simulation'").fetchone()
    assert {i.simulated for i in db.list_incidents(view="all")} == {False}
