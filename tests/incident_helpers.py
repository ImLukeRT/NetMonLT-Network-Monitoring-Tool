"""Shared by the incident tests: store alerts the way monitoring does, and mark history as "from
before incident management" the way an upgrade does (the incident cursor starts at the newest
alert, so those alerts are never processed live)."""

from fastapi.testclient import TestClient

from helpers import FakeClock
from netmonlt import incidents as inc
from netmonlt.alerts import Alert, AlertState, Severity
from netmonlt.api import create_app
from netmonlt.domain import IncidentsConfig, Status
from netmonlt.incident_engine import process_once
from netmonlt.inventory import Inventory
from netmonlt.probes import SimulatedProber

U, D, X = Status.UP, Status.DEGRADED, Status.DOWN
CFG = IncidentsConfig()
T0 = 1_700_000_000.0
WINDOW = CFG.correlation_window_s


def raise_alert(db, ts, new, old=None, device="alpha"):
    old = old or (U if new is not U else X)
    severity = {X: Severity.CRITICAL, D: Severity.WARNING, U: Severity.INFO}[new]
    alert = Alert(device, ts, severity, old, new, f"{device} went {new.value} at {ts}")
    db.save_alert_state(device, AlertState(status=new, since=ts, updated=ts), alert)


def treat_as_history(db):
    """What opening a database for the first time after upgrading does to the cursor."""
    with db._lock, db._conn:
        db._conn.execute("UPDATE incident_cursor SET last_alert_id = "
                         "(SELECT COALESCE(MAX(id), 0) FROM alerts) WHERE id = 1")


def process(db, cfg=CFG, *, now=T0 + 5000, simulated=True):
    return process_once(db, cfg, simulated=simulated, run_started_ts=None, clock=lambda: now)


def snapshot(db, table, order="1"):
    return [tuple(r) for r in db._conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def previewed(db, cfg=CFG):
    return db.preview_import(cfg)


def do_import(db, cfg=CFG, *, actor="alice", now=T0 + 9000, simulated=True):
    plan = db.preview_import(cfg)
    return db.apply_import(cfg, simulated=simulated, actor=actor, fingerprint=plan.fingerprint,
                           now=now)


def by_device(db, view="all"):
    out = {}
    for i in db.list_incidents(view=view):
        out.setdefault(i.device, []).append(i)
    return out


JSON = {"Content-Type": "application/json"}


def make_app(db, devices, *, mode="simulated", incidents=None, now=T0 + 20_000):
    db.sync_devices(devices)
    inventory = Inventory(mode=mode, poll_interval_s=5, devices=tuple(devices),
                          incidents=incidents or IncidentsConfig())
    return create_app(inventory, db, SimulatedProber(), start_collector=False,
                      clock=FakeClock(now))


def local_client(app):
    return TestClient(app, client=("127.0.0.1", 50123), base_url="http://localhost")


def remote_client(app, ip="192.0.2.50"):
    return TestClient(app, client=(ip, 40000), base_url="http://localhost")


def token_of(client):
    page = client.get("/").text
    marker = 'name="netmonlt-token" content="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def headers(client):
    return {**JSON, "X-NetMonLT-Token": token_of(client)}


__all__ = ["inc", "U", "D", "X", "CFG", "T0", "WINDOW", "raise_alert", "treat_as_history",
           "process", "snapshot", "previewed", "do_import", "by_device"]
