"""SQLite persistence, using the standard library and plain SQL.

One shared connection guarded by a lock: the collector (event loop) and the API
(thread pool) both use it. WAL mode is enabled so readers don't block the writer.
"""

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from netmonlt import incidents as inc
from netmonlt.alerts import Alert, AlertState
from netmonlt.domain import CheckResult, Device, IncidentsConfig, Status
from netmonlt.ifstats import STATUS_OK, InterfaceState, SnmpStatus
from netmonlt.qos import QosSample, QosSnapshot, QosState, QosStatus
from netmonlt.quality import CheckSample
from netmonlt.wifi import WifiReading, WifiSample

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    name    TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    type    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL REFERENCES devices(name) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    status     TEXT NOT NULL,
    sent       INTEGER NOT NULL,
    received   INTEGER NOT NULL,
    rtt_min_ms REAL,
    rtt_avg_ms REAL,
    rtt_max_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_checks_device_ts ON checks(device, ts);
CREATE TABLE IF NOT EXISTS device_state (
    device          TEXT PRIMARY KEY REFERENCES devices(name) ON DELETE CASCADE,
    status          TEXT,
    since           REAL,
    candidate       TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    candidate_since REAL,
    updated         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    device      TEXT NOT NULL REFERENCES devices(name) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    severity    TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS snmp_status (
    device       TEXT PRIMARY KEY REFERENCES devices(name) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    status       TEXT NOT NULL,
    message      TEXT,
    last_ok_ts   REAL,
    uptime_ticks INTEGER,
    truncated    INTEGER NOT NULL DEFAULT 0
);
-- Raw counters are TEXT because SQLite integers are signed 64-bit and a Counter64 can exceed that.
CREATE TABLE IF NOT EXISTS interface_state (
    device             TEXT NOT NULL REFERENCES devices(name) ON DELETE CASCADE,
    if_index           INTEGER NOT NULL,
    name               TEXT NOT NULL,
    oper_status        TEXT NOT NULL,
    present            INTEGER NOT NULL,
    ts                 REAL NOT NULL,
    in_octets          TEXT,
    out_octets         TEXT,
    in_errors          TEXT,
    out_errors         TEXT,
    in_discards        TEXT,
    out_discards       TEXT,
    interval_s         REAL,
    in_bps             REAL,
    out_bps            REAL,
    in_errors_delta    INTEGER,
    out_errors_delta   INTEGER,
    in_discards_delta  INTEGER,
    out_discards_delta INTEGER,
    rate_note          TEXT,
    PRIMARY KEY (device, if_index)
);
-- This computer's own Wi-Fi link (opt-in). One status row, plus one row per successful look at
-- the adapter, kept for a bounded time. The SSID is stored as the privacy setting allows
-- (masked by default); the access point's MAC address and the profile name are never stored.
CREATE TABLE IF NOT EXISTS wifi_status (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    ts         REAL NOT NULL,
    status     TEXT NOT NULL,
    message    TEXT,
    last_ok_ts REAL
);
CREATE TABLE IF NOT EXISTS wifi_observations (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    state       TEXT NOT NULL,
    ssid        TEXT,
    interface   TEXT,
    signal_pct  INTEGER,
    rssi_dbm    INTEGER,
    rssi_source TEXT,
    rx_mbps     REAL,
    tx_mbps     REAL,
    radio_type  TEXT,
    channel     INTEGER,
    source      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wifi_ts ON wifi_observations(ts);
-- Incident management: a human workflow layered on the alert log. Incidents are plain text on
-- `device` (no foreign key), so removing a device from the inventory never deletes its incident
-- records. The event trail is append-only: triggers refuse any update or delete, so nothing an
-- incident's evidence or an operator's notes recorded can be altered or removed.
CREATE TABLE IF NOT EXISTS incidents (
    id                   INTEGER PRIMARY KEY,
    device               TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    severity             TEXT NOT NULL,
    opened_ts            REAL NOT NULL,
    last_alert_ts        REAL NOT NULL,
    monitoring_state     TEXT NOT NULL,
    recovered_ts         REAL,
    flap_count           INTEGER NOT NULL DEFAULT 0,
    review_state         TEXT NOT NULL,
    acknowledged_ts      REAL,
    acknowledged_by      TEXT,
    resolved_ts          REAL,
    resolved_by          TEXT,
    reopened_count       INTEGER NOT NULL DEFAULT 0,
    previous_incident_id INTEGER,
    simulated            INTEGER NOT NULL DEFAULT 0,
    run_started_ts       REAL,
    report               TEXT NOT NULL DEFAULT '{}',
    evidence             TEXT NOT NULL DEFAULT '{}',
    origin               TEXT NOT NULL DEFAULT 'live',
    import_id            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_incidents_device ON incidents(device, opened_ts);
CREATE INDEX IF NOT EXISTS idx_incidents_review ON incidents(review_state, last_alert_ts);
CREATE TABLE IF NOT EXISTS incident_events (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    ts          REAL NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    actor       TEXT,
    severity    TEXT,
    from_status TEXT,
    to_status   TEXT,
    alert_id    INTEGER,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_incident_events ON incident_events(incident_id, ts, id);
-- One monitoring event per alert and kind, so reprocessing an alert can never duplicate it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_events_alert
    ON incident_events(alert_id, kind) WHERE alert_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS incident_events_no_update BEFORE UPDATE ON incident_events
BEGIN SELECT RAISE(ABORT, 'incident events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS incident_events_no_delete BEFORE DELETE ON incident_events
BEGIN SELECT RAISE(ABORT, 'incident events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS incidents_no_delete BEFORE DELETE ON incidents
BEGIN SELECT RAISE(ABORT, 'incidents are never deleted'); END;
-- Historical import: every import is recorded, and so is every alert it chose not to place in an
-- incident (with the reason), so such alerts are not offered as import candidates again. Both
-- tables are append-only, like the event trail.
CREATE TABLE IF NOT EXISTS incident_imports (
    id                 INTEGER PRIMARY KEY,
    ts                 REAL NOT NULL,
    actor              TEXT NOT NULL,
    alerts_considered  INTEGER NOT NULL,
    incidents_created  INTEGER NOT NULL,
    alerts_skipped     INTEGER NOT NULL,
    fingerprint        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_import_skips (
    alert_id  INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES incident_imports(id),
    reason    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS incident_imports_no_update BEFORE UPDATE ON incident_imports
BEGIN SELECT RAISE(ABORT, 'import records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS incident_imports_no_delete BEFORE DELETE ON incident_imports
BEGIN SELECT RAISE(ABORT, 'import records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS incident_import_skips_no_update BEFORE UPDATE ON incident_import_skips
BEGIN SELECT RAISE(ABORT, 'import records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS incident_import_skips_no_delete BEFORE DELETE ON incident_import_skips
BEGIN SELECT RAISE(ABORT, 'import records are append-only'); END;
-- How far through the alert log incidents have been built. On a database that already holds
-- alerts it starts at the newest one: earlier alerts stay in the event log, unchanged, and are
-- not retroactively turned into "awaiting review" incidents.
CREATE TABLE IF NOT EXISTS incident_cursor (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_alert_id INTEGER NOT NULL
);
INSERT OR IGNORE INTO incident_cursor(id, last_alert_id)
    SELECT 1, COALESCE(MAX(id), 0) FROM alerts;
-- QoS telemetry (opt-in, read-only SNMPv3). Raw counters are kept only for the LATEST poll of each
-- element (needed to derive the next rate); history holds derived rates, one row per element
-- per poll, for a bounded time. Counters are TEXT because a Counter64 can exceed signed 64-bit.
CREATE TABLE IF NOT EXISTS qos_status (
    device       TEXT PRIMARY KEY REFERENCES devices(name) ON DELETE CASCADE,
    ts           REAL NOT NULL,
    status       TEXT NOT NULL,
    message      TEXT,
    last_ok_ts   REAL,
    uptime_ticks INTEGER,
    adapter      TEXT,
    source_mib   TEXT,
    truncated    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS qos_state (
    device        TEXT NOT NULL REFERENCES devices(name) ON DELETE CASCADE,
    row_key       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    detail        TEXT,
    present       INTEGER NOT NULL,
    ts            REAL NOT NULL,
    counter_bits  INTEGER NOT NULL,
    discontinuity INTEGER,
    packets       TEXT,
    octets        TEXT,
    drop_packets  TEXT,
    drop_octets   TEXT,
    interval_s    REAL,
    pps           REAL,
    bps           REAL,
    drop_pps      REAL,
    drop_bps      REAL,
    drop_pct      REAL,
    rate_note     TEXT,
    PRIMARY KEY (device, row_key)
);
CREATE TABLE IF NOT EXISTS qos_history (
    id        INTEGER PRIMARY KEY,
    device    TEXT NOT NULL REFERENCES devices(name) ON DELETE CASCADE,
    row_key   TEXT NOT NULL,
    ts        REAL NOT NULL,
    source    TEXT NOT NULL,
    pps       REAL,
    bps       REAL,
    drop_pps  REAL,
    drop_bps  REAL,
    rate_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_qos_history ON qos_history(device, ts);
"""

_STATE_COLUMNS = (
    "if_index, name, oper_status, present, ts, in_octets, out_octets, in_errors, out_errors, "
    "in_discards, out_discards, interval_s, in_bps, out_bps, in_errors_delta, out_errors_delta, "
    "in_discards_delta, out_discards_delta, rate_note"
)


@dataclass(frozen=True)
class WifiStatus:
    """The outcome of the latest Wi-Fi poll."""

    ts: float
    status: str
    message: str | None
    last_ok_ts: float | None


_WIFI_COLUMNS = (
    "ts, state, ssid, interface, signal_pct, rssi_dbm, rssi_source, rx_mbps, tx_mbps, "
    "radio_type, channel, source"
)


def _wifi_reading_from_row(row: sqlite3.Row) -> WifiReading:
    return WifiReading(
        state=row["state"], ssid=row["ssid"], interface=row["interface"],
        signal_pct=row["signal_pct"], rssi_dbm=row["rssi_dbm"], rssi_source=row["rssi_source"],
        rx_mbps=row["rx_mbps"], tx_mbps=row["tx_mbps"], radio_type=row["radio_type"],
        channel=row["channel"], source=row["source"],
    )


_INCIDENT_COLUMNS = (
    "id, device, kind, severity, opened_ts, last_alert_ts, monitoring_state, recovered_ts, "
    "flap_count, review_state, acknowledged_ts, acknowledged_by, resolved_ts, resolved_by, "
    "reopened_count, previous_incident_id, simulated, run_started_ts, report, evidence, "
    "origin, import_id"
)
_EVENT_COLUMNS = (
    "id, incident_id, ts, source, kind, actor, severity, from_status, to_status, alert_id, text"
)


def _json_dict(text: str | None) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}  # a damaged value is treated as empty, never guessed
    return value if isinstance(value, dict) else {}


def _incident_from_row(row: sqlite3.Row) -> inc.Incident:
    return inc.Incident(
        id=row["id"], device=row["device"], kind=row["kind"], severity=row["severity"],
        opened_ts=row["opened_ts"], last_alert_ts=row["last_alert_ts"],
        monitoring_state=row["monitoring_state"], recovered_ts=row["recovered_ts"],
        flap_count=row["flap_count"], review_state=row["review_state"],
        acknowledged_ts=row["acknowledged_ts"], acknowledged_by=row["acknowledged_by"],
        resolved_ts=row["resolved_ts"], resolved_by=row["resolved_by"],
        reopened_count=row["reopened_count"], previous_incident_id=row["previous_incident_id"],
        simulated=bool(row["simulated"]), run_started_ts=row["run_started_ts"],
        report=_json_dict(row["report"]), evidence=_json_dict(row["evidence"]),
        origin=row["origin"], import_id=row["import_id"],
    )


def _event_from_row(row: sqlite3.Row) -> inc.IncidentEvent:
    return inc.IncidentEvent(
        id=row["id"], incident_id=row["incident_id"], ts=row["ts"], source=row["source"],
        kind=row["kind"], actor=row["actor"], severity=row["severity"],
        from_status=row["from_status"], to_status=row["to_status"], alert_id=row["alert_id"],
        text=row["text"],
    )


def _text(value: int | None) -> str | None:
    return None if value is None else str(value)


def _int(value: str | int | None) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None  # a damaged value is treated as unknown, never guessed


def _state_from_row(row: sqlite3.Row) -> InterfaceState:
    return InterfaceState(
        if_index=row["if_index"], name=row["name"], oper_status=row["oper_status"],
        present=bool(row["present"]), ts=row["ts"],
        in_octets=_int(row["in_octets"]), out_octets=_int(row["out_octets"]),
        in_errors=_int(row["in_errors"]), out_errors=_int(row["out_errors"]),
        in_discards=_int(row["in_discards"]), out_discards=_int(row["out_discards"]),
        interval_s=row["interval_s"], in_bps=row["in_bps"], out_bps=row["out_bps"],
        in_errors_delta=row["in_errors_delta"], out_errors_delta=row["out_errors_delta"],
        in_discards_delta=row["in_discards_delta"], out_discards_delta=row["out_discards_delta"],
        rate_note=row["rate_note"],
    )


_QOS_COLUMNS = (
    "row_key, kind, name, detail, present, ts, counter_bits, discontinuity, packets, octets, "
    "drop_packets, drop_octets, interval_s, pps, bps, drop_pps, drop_bps, drop_pct, rate_note"
)


def _qos_state_from_row(row: sqlite3.Row) -> QosState:
    return QosState(
        key=row["row_key"], kind=row["kind"], name=row["name"], detail=row["detail"],
        present=bool(row["present"]), ts=row["ts"], counter_bits=row["counter_bits"],
        discontinuity=row["discontinuity"], packets=_int(row["packets"]),
        octets=_int(row["octets"]), drop_packets=_int(row["drop_packets"]),
        drop_octets=_int(row["drop_octets"]), interval_s=row["interval_s"], pps=row["pps"],
        bps=row["bps"], drop_pps=row["drop_pps"], drop_bps=row["drop_bps"],
        drop_pct=row["drop_pct"], rate_note=row["rate_note"],
    )


def _qos_status_from_row(row: sqlite3.Row) -> QosStatus:
    return QosStatus(
        ts=row["ts"], status=row["status"], message=row["message"], last_ok_ts=row["last_ok_ts"],
        uptime_ticks=row["uptime_ticks"], adapter=row["adapter"], source_mib=row["source_mib"],
        truncated=row["truncated"],
    )


def _status_from_row(row: sqlite3.Row) -> SnmpStatus:
    return SnmpStatus(
        ts=row["ts"], status=row["status"], message=row["message"], last_ok_ts=row["last_ok_ts"],
        uptime_ticks=row["uptime_ticks"], truncated=row["truncated"],
    )

_LATEST = """
SELECT d.name, d.address, d.type,
       c.ts, c.status, c.sent, c.received, c.rtt_min_ms, c.rtt_avg_ms, c.rtt_max_ms
FROM devices d
LEFT JOIN checks c ON c.id = (
    SELECT id FROM checks WHERE device = d.name ORDER BY ts DESC, id DESC LIMIT 1
)
ORDER BY d.name
"""


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._upgrade_incident_columns()

    def _upgrade_incident_columns(self) -> None:
        """Add the columns a database created by an earlier Stage 10 build lacks. Adding a column
        with a default leaves every existing row and value exactly as it was."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(incidents)")}
        with self._conn:
            if "origin" not in existing:
                self._conn.execute(
                    "ALTER TABLE incidents ADD COLUMN origin TEXT NOT NULL DEFAULT 'live'")
            if "import_id" not in existing:
                self._conn.execute("ALTER TABLE incidents ADD COLUMN import_id INTEGER")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def sync_devices(self, devices: Iterable[Device]) -> None:
        """Make the devices table match the inventory (YAML is the source of truth).

        Devices removed from the inventory are deleted along with their check history.
        """
        devices = list(devices)
        names = [d.name for d in devices]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO devices(name, address, type) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET address=excluded.address, type=excluded.type",
                [(d.name, d.address, d.type) for d in devices],
            )
            placeholders = ",".join("?" * len(names))
            self._conn.execute(f"DELETE FROM devices WHERE name NOT IN ({placeholders})", names)

    def record_check(self, check: CheckResult) -> None:
        p = check.ping
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO checks(device, ts, status, sent, received, rtt_min_ms, rtt_avg_ms, "
                "rtt_max_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (check.device, check.ts, check.status.value, p.sent, p.received,
                 p.rtt_min_ms, p.rtt_avg_ms, p.rtt_max_ms),
            )

    def latency_series(
        self, device: str, start: float, end: float, max_points: int, gap_s: float
    ) -> dict | None:
        """Latency history for a device over [start, end], ready to chart. None if no such device.

        Returns chronological `points`, the periods with no measurements (`gaps`), and the
        device's first/last check times. The three cases a chart must tell apart are kept apart:
          * a down check is a point with no latency (rtt_* is None, status "down");
          * a missing measurement (probe errors, app stopped) is no point at all, reported as a
            gap whenever consecutive checks are more than `gap_s` apart;
          * a genuine 0 ms reply is a point with rtt 0.0.
        If the window holds more than `max_points` checks, they are grouped into equal time
        buckets: latency is the reply-weighted mean and min/max are the extremes. A bucket's status
        is "down" only if every check in it was down, "degraded" if any check was down or
        degraded (its `down` count says how many), else "up". Empty buckets are omitted, never
        filled with zeros.
        """
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM devices WHERE name=?", (device,)).fetchone():
                return None
            first_ts, last_ts = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM checks WHERE device=?", (device,)
            ).fetchone()
            count, first_in, last_in = self._conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM checks "
                "WHERE device=? AND ts BETWEEN ? AND ?",
                (device, start, end),
            ).fetchone()

            bucket_s = None
            if count <= max_points:
                rows = self._conn.execute(
                    "SELECT ts, status, sent, received, rtt_min_ms, rtt_avg_ms, rtt_max_ms "
                    "FROM checks WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts, id",
                    (device, start, end),
                ).fetchall()
                points = [
                    {
                        "ts": r["ts"], "checks": 1, "status": r["status"],
                        "down": int(r["status"] == "down"),
                        "degraded": int(r["status"] == "degraded"),
                        "sent": r["sent"], "received": r["received"],
                        "rtt_min_ms": r["rtt_min_ms"], "rtt_avg_ms": r["rtt_avg_ms"],
                        "rtt_max_ms": r["rtt_max_ms"],
                    }
                    for r in rows
                ]
            else:
                bucket_s = (end - start) / max_points
                rows = self._conn.execute(
                    "SELECT AVG(ts) AS ts, COUNT(*) AS checks, SUM(status='down') AS down, "
                    "SUM(status='degraded') AS degraded, SUM(sent) AS sent, "
                    "SUM(received) AS received, MIN(rtt_min_ms) AS rtt_min_ms, "
                    "SUM(rtt_avg_ms * received) * 1.0 / "
                    "NULLIF(SUM(CASE WHEN rtt_avg_ms IS NOT NULL THEN received END), 0) "
                    "AS rtt_avg_ms, MAX(rtt_max_ms) AS rtt_max_ms "
                    "FROM checks WHERE device=? AND ts BETWEEN ? AND ? "
                    "GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY ts",
                    (device, start, end, start, bucket_s),
                ).fetchall()
                points = []
                for r in rows:
                    point = dict(r)
                    point["status"] = (
                        "down" if r["down"] == r["checks"]
                        else "degraded" if r["down"] or r["degraded"]
                        else "up"
                    )
                    if point["rtt_avg_ms"] is not None:
                        point["rtt_avg_ms"] = round(point["rtt_avg_ms"], 2)
                    points.append(point)

            gaps = []
            if count:
                if first_ts < start and first_in - start > gap_s:
                    gaps.append({"start": start, "end": first_in})  # nothing near the window start
                for prev, ts in self._conn.execute(
                    "SELECT prev, ts FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev "
                    "FROM checks WHERE device=? AND ts BETWEEN ? AND ?) "
                    "WHERE prev IS NOT NULL AND ts - prev > ?",
                    (device, start, end, gap_s),
                ):
                    gaps.append({"start": prev, "end": ts})
                if end - last_in > gap_s:
                    gaps.append({"start": last_in, "end": end})  # measurements stopped
            elif first_ts is not None and first_ts <= end:
                gaps.append({"start": start, "end": end})  # history exists, but only before this

            return {
                "points": points, "gaps": gaps, "checks": count, "bucket_s": bucket_s,
                "first_check_ts": first_ts, "last_check_ts": last_ts,
            }

    def quality_samples(
        self, device: str, start: float, end: float
    ) -> tuple[list[CheckSample], float | None, float | None] | None:
        """The stored checks in [start, end], oldest first, for network-quality figures.

        Returns (samples, first_check_ts, last_check_ts), where the last two cover the device's
        whole history, or None if there is no such device. Reads the existing `checks` table;
        nothing extra is stored.
        """
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM devices WHERE name=?", (device,)).fetchone():
                return None
            first_ts, last_ts = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM checks WHERE device=?", (device,)
            ).fetchone()
            rows = self._conn.execute(
                "SELECT ts, sent, received, rtt_avg_ms FROM checks "
                "WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts, id",
                (device, start, end),
            ).fetchall()
        samples = [CheckSample(r["ts"], r["sent"], r["received"], r["rtt_avg_ms"]) for r in rows]
        return samples, first_ts, last_ts

    # ---- incident management (built from the alert log; never changes monitoring)
    def process_new_alerts(
        self, cfg: IncidentsConfig, *, simulated: bool, run_started_ts: float | None,
        now: float, batch: int = 500,
    ) -> int:
        """Turn alerts not yet seen into incident events, in order, in one transaction with the
        cursor, so a restart or a repeated call can never duplicate an incident or an event.
        Returns the number of alerts processed. The alert log itself is only ever read."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "SELECT last_alert_id FROM incident_cursor WHERE id=1"
            ).fetchone()[0]
            alerts = self._conn.execute(
                "SELECT id, device, ts, severity, from_status, to_status, message FROM alerts "
                "WHERE id > ? ORDER BY id LIMIT ?", (cursor, batch),
            ).fetchall()
            for alert in alerts:
                self._apply_alert(alert, cfg, simulated, run_started_ts)
            if alerts:
                self._conn.execute(
                    "UPDATE incident_cursor SET last_alert_id=? WHERE id=1", (alerts[-1]["id"],)
                )
        return len(alerts)

    def _apply_alert(self, alert, cfg, simulated, run_started_ts) -> None:
        """Attach one alert to an incident (lock and transaction held by the caller)."""
        device, ts, to_status = alert["device"], alert["ts"], alert["to_status"]
        is_recovery = to_status == Status.UP.value
        candidates = [
            _incident_from_row(r) for r in self._conn.execute(
                f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE device=? AND kind=? "
                "AND review_state != ? ORDER BY opened_ts DESC, id DESC",
                (device, inc.KIND_ICMP, inc.REV_RESOLVED),
            )
        ]
        match = inc.match_incident(candidates, ts, is_recovery, cfg.correlation_window_s)
        severity = alert["severity"]

        def event(incident_id, kind):
            self._insert_incident_event(
                incident_id, ts, inc.SRC_MONITORING, kind, severity=severity,
                from_status=alert["from_status"], to_status=to_status, alert_id=alert["id"],
                text=alert["message"],
            )

        if is_recovery:
            if match is None or match.monitoring_state != inc.MON_ACTIVE:
                return  # a recovery with no active incident (e.g. a fault from before upgrading)
            self._conn.execute(
                "UPDATE incidents SET monitoring_state=?, recovered_ts=?, last_alert_ts=? "
                "WHERE id=?", (inc.MON_RECOVERED, ts, ts, match.id),
            )
            event(match.id, inc.EV_RECOVERED)
            self._store_evidence(match.id, device, match.opened_ts, ts, ts)
        elif match is None:
            previous = self._conn.execute(
                "SELECT id FROM incidents WHERE device=? AND kind=? ORDER BY opened_ts DESC, "
                "id DESC LIMIT 1", (device, inc.KIND_ICMP),
            ).fetchone()
            new_id = self._conn.execute(
                "INSERT INTO incidents(device, kind, severity, opened_ts, last_alert_ts, "
                "monitoring_state, review_state, previous_incident_id, simulated, run_started_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device, inc.KIND_ICMP, severity, ts, ts, inc.MON_ACTIVE, inc.REV_OPEN,
                 None if previous is None else previous["id"], int(simulated), run_started_ts),
            ).lastrowid
            event(new_id, inc.EV_OPENED)
            self._store_evidence(new_id, device, ts, None, ts)
        elif match.monitoring_state == inc.MON_ACTIVE:
            self._conn.execute(
                "UPDATE incidents SET severity=?, last_alert_ts=? WHERE id=?",
                (inc.worse(match.severity, severity), ts, match.id),
            )
            event(match.id, inc.EV_ALERT)
        else:  # the fault is back within the correlation window: one incident, a recurrence
            self._conn.execute(
                "UPDATE incidents SET monitoring_state=?, flap_count=flap_count+1, severity=?, "
                "last_alert_ts=? WHERE id=?",
                (inc.MON_ACTIVE, inc.worse(match.severity, severity), ts, match.id),
            )
            event(match.id, inc.EV_RECURRED)
            self._store_evidence(match.id, device, match.opened_ts, None, ts)

    def _insert_incident_event(
        self, incident_id: int, ts: float, source: str, kind: str, *, actor: str | None = None,
        severity: str | None = None, from_status: str | None = None, to_status: str | None = None,
        alert_id: int | None = None, text: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO incident_events(incident_id, ts, source, kind, actor, severity, "
            "from_status, to_status, alert_id, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, ts, source, kind, actor, severity, from_status, to_status, alert_id,
             text),
        )

    def _compute_evidence(
        self, device: str, opened_ts: float, recovered_ts: float | None, end_ts: float
    ) -> dict:
        """The incident's monitoring evidence, counted from the stored checks (lock held)."""
        before = self._conn.execute(
            "SELECT ts, status FROM checks WHERE device=? AND ts<=? ORDER BY ts DESC, id DESC "
            "LIMIT 200", (device, opened_ts),
        ).fetchall()
        run_start = opened_ts
        for row in before:  # walk back over the unbroken run of unhealthy checks
            if row["status"] == Status.UP.value:
                break
            run_start = row["ts"]
        rows = self._conn.execute(
            "SELECT ts, status, sent, received, rtt_avg_ms FROM checks WHERE device=? AND ts>=? "
            "AND ts<=? ORDER BY ts, id LIMIT ?",
            (device, run_start, end_ts, inc.MAX_EVIDENCE_ROWS + 1),
        ).fetchall()
        truncated = len(rows) > inc.MAX_EVIDENCE_ROWS
        return inc.summarize_checks(
            [dict(r) for r in rows[:inc.MAX_EVIDENCE_ROWS]], opened_ts, recovered_ts, end_ts,
            truncated=truncated,
        )

    def _store_evidence(self, incident_id, device, opened_ts, recovered_ts, end_ts) -> None:
        evidence = self._compute_evidence(device, opened_ts, recovered_ts, end_ts)
        self._conn.execute(
            "UPDATE incidents SET evidence=? WHERE id=?", (json.dumps(evidence), incident_id)
        )

    def incident_evidence(self, incident: inc.Incident, now: float) -> dict:
        """The evidence to show: counted live from the stored checks while the incident is
        unresolved, and the stored snapshot once it is resolved (so it stays as it was)."""
        if incident.review_state == inc.REV_RESOLVED and incident.evidence:
            return incident.evidence
        if incident.monitoring_state == inc.MON_RECOVERED and incident.recovered_ts is not None:
            end = incident.recovered_ts
        elif incident.monitoring_state == inc.MON_UNKNOWN:
            end = incident.last_alert_ts  # nothing is known beyond the last recorded alert
        else:
            end = now
        recovered = (incident.recovered_ts if incident.monitoring_state == inc.MON_RECOVERED
                     else None)
        with self._lock:
            live = self._compute_evidence(incident.device, incident.opened_ts, recovered, end)
        # checks may have been removed with their device: fall back to what was stored then
        return live if live["checks_total"] or not incident.evidence else incident.evidence

    def get_incident(self, incident_id: int) -> inc.Incident | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id=?", (incident_id,)
            ).fetchone()
        return None if row is None else _incident_from_row(row)

    def incident_events(self, incident_id: int) -> list[inc.IncidentEvent]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM incident_events WHERE incident_id=? "
                "ORDER BY ts, id", (incident_id,),
            ).fetchall()
        return [_event_from_row(r) for r in rows]

    def list_incidents(
        self, *, view: str = "active", status: str | None = None, severity: str | None = None,
        device: str | None = None, limit: int = 100,
    ) -> list[inc.Incident]:
        """Incidents, newest activity first. `view` is "active" (not resolved), "history"
        (resolved) or "all"; `status` is one of inc.STATUS_FILTERS."""
        where, args = [], []
        if view == "active":
            where.append("review_state != ?")
            args.append(inc.REV_RESOLVED)
        elif view == "history":
            where.append("review_state = ?")
            args.append(inc.REV_RESOLVED)
        status_sql = {
            "open": "review_state = 'open' AND reopened_count = 0",
            "acknowledged": "review_state = 'acknowledged'",
            "recovered": "monitoring_state = 'recovered' AND review_state != 'resolved'",
            "unknown": "monitoring_state = 'unknown' AND review_state != 'resolved'",
            "reopened": "reopened_count > 0 AND review_state != 'resolved'",
            "resolved": "review_state = 'resolved'",
        }
        if status is not None:
            where.append(status_sql[status])
        if severity is not None:
            where.append("severity = ?")
            args.append(severity)
        if device is not None:
            where.append("device = ?")
            args.append(device)
        order = "resolved_ts DESC, id DESC" if view == "history" else "last_alert_ts DESC, id DESC"
        sql = f"SELECT {_INCIDENT_COLUMNS} FROM incidents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._conn.execute(f"{sql} ORDER BY {order} LIMIT ?", (*args, limit)).fetchall()
        return [_incident_from_row(r) for r in rows]

    def incident_alert_count(self, incident_id: int) -> int:
        """How many monitoring alerts (not operator actions) an incident groups."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM incident_events WHERE incident_id=? AND alert_id IS NOT NULL",
                (incident_id,),
            ).fetchone()[0]

    def incident_counts(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT SUM(review_state != 'resolved'), SUM(review_state = 'resolved'), "
                "SUM(review_state = 'open' AND monitoring_state = 'recovered'), "
                "SUM(review_state != 'resolved' AND monitoring_state = 'unknown') FROM incidents"
            ).fetchone()
        return {"active": row[0] or 0, "history": row[1] or 0, "awaiting_review": row[2] or 0,
                "recovery_unknown": row[3] or 0}

    # ---- historical import: turn stored alerts that no incident covers into incidents
    def _import_candidates(self, cfg: IncidentsConfig) -> list[inc.HistoricalAlert]:
        """Alerts the live engine has already passed (id <= cursor) that no incident references and
        no earlier import skipped, oldest first (lock held by the caller)."""
        cursor = self._conn.execute(
            "SELECT last_alert_id FROM incident_cursor WHERE id=1").fetchone()[0]
        rows = self._conn.execute(
            "SELECT a.id, a.device, a.ts, a.severity, a.from_status, a.to_status, a.message "
            "FROM alerts a WHERE a.id <= ? "
            "AND NOT EXISTS (SELECT 1 FROM incident_events e WHERE e.alert_id = a.id) "
            "AND NOT EXISTS (SELECT 1 FROM incident_import_skips s WHERE s.alert_id = a.id) "
            "ORDER BY a.id", (cursor,)).fetchall()
        return [inc.HistoricalAlert(r["id"], r["device"], r["ts"], r["severity"],
                                    r["from_status"], r["to_status"], r["message"]) for r in rows]

    def import_candidate_count(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT last_alert_id FROM incident_cursor WHERE id=1").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM alerts a WHERE a.id <= ? "
                "AND NOT EXISTS (SELECT 1 FROM incident_events e WHERE e.alert_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM incident_import_skips s WHERE s.alert_id = a.id)",
                (cursor,)).fetchone()[0]

    def preview_import(self, cfg: IncidentsConfig) -> inc.ImportPlan:
        """What an import would create right now. Reads only."""
        with self._lock:
            alerts = self._import_candidates(cfg)
        return inc.plan_import(alerts, window_s=cfg.correlation_window_s)

    def apply_import(
        self, cfg: IncidentsConfig, *, simulated: bool, actor, fingerprint: str, now: float,
    ) -> dict:
        """Create the incidents the preview described, in ONE transaction (so an interrupted
        import leaves nothing behind) and under the same lock as live processing (so the two can
        never interleave). Only inserts: no existing incident, event or alert is changed. The
        alerts it cannot place are recorded, with the reason, and are not offered again."""
        actor = inc.clean_actor(actor)
        with self._lock, self._conn:
            plan = inc.plan_import(self._import_candidates(cfg),
                                   window_s=cfg.correlation_window_s)
            if plan.fingerprint != fingerprint:
                raise inc.IncidentError(
                    inc.IncidentError.STALE,
                    "the alert history changed since this preview: preview the import again")
            if not plan.considered:
                return {"import_id": None, "incidents_created": 0, "alerts_linked": 0,
                        "alerts_skipped": 0}
            import_id = self._conn.execute(
                "INSERT INTO incident_imports(ts, actor, alerts_considered, incidents_created, "
                "alerts_skipped, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
                (now, actor, plan.considered, len(plan.drafts), len(plan.skipped),
                 plan.fingerprint)).lastrowid
            previous: dict[str, int] = {}
            linked = 0
            for draft in plan.drafts:
                new_id = self._conn.execute(
                    "INSERT INTO incidents(device, kind, severity, opened_ts, last_alert_ts, "
                    "monitoring_state, recovered_ts, flap_count, review_state, "
                    "previous_incident_id, simulated, run_started_ts, origin, import_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (draft.device, inc.KIND_ICMP, draft.severity, draft.opened_ts,
                     draft.last_alert_ts, draft.monitoring_state, draft.recovered_ts,
                     draft.flap_count, inc.REV_OPEN, previous.get(draft.device), int(simulated),
                     inc.ORIGIN_IMPORTED, import_id)).lastrowid
                previous[draft.device] = new_id
                for alert, kind in draft.events:
                    self._insert_incident_event(
                        new_id, alert.ts, inc.SRC_MONITORING, kind, severity=alert.severity,
                        from_status=alert.from_status, to_status=alert.to_status,
                        alert_id=alert.id, text=alert.message)
                    linked += 1
                self._insert_incident_event(
                    new_id, now, inc.SRC_MONITORING, inc.EV_IMPORTED, actor=actor,
                    text=f"Created from {len(draft.events)} stored alert(s) by import "
                         f"#{import_id}; the alerts above are the original ones.")
                end = (draft.recovered_ts if draft.monitoring_state == inc.MON_RECOVERED
                       else draft.last_alert_ts)
                self._store_evidence(new_id, draft.device, draft.opened_ts, draft.recovered_ts,
                                     end)
            self._conn.executemany(
                "INSERT INTO incident_import_skips(alert_id, import_id, reason) VALUES (?, ?, ?)",
                [(alert_id, import_id, reason) for alert_id, reason in plan.skipped])
        return {"import_id": import_id, "incidents_created": len(plan.drafts),
                "alerts_linked": linked, "alerts_skipped": len(plan.skipped)}

    # ---- the Recent alerts view: filtered by what the linked incident's review state is
    _ARCHIVED = (
        "EXISTS (SELECT 1 FROM incident_events e JOIN incidents i ON i.id = e.incident_id "
        "WHERE e.alert_id = a.id AND i.review_state = 'resolved')"
    )

    def list_alerts(self, *, view: str = "unresolved", limit: int = 50, offset: int = 0) -> dict:
        """Alerts newest first, with their incident link. `view`: "unresolved" (everything except
        alerts linked to a RESOLVED incident: unmatched alerts always appear), "archived" (alerts
        linked to a resolved incident) or "all". Filtering, the row limit and the offset are all
        done here in SQL, and `total` is the size of the chosen view. Alerts are only read."""
        where = {"unresolved": f"WHERE NOT {self._ARCHIVED}", "archived": f"WHERE {self._ARCHIVED}",
                 "all": ""}[view]
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.id, a.device, a.ts, a.severity, a.from_status, a.to_status, a.message, "
                "(SELECT e.incident_id FROM incident_events e WHERE e.alert_id = a.id LIMIT 1) "
                "AS incident_id, "
                "(SELECT i.review_state FROM incident_events e JOIN incidents i "
                "ON i.id = e.incident_id WHERE e.alert_id = a.id LIMIT 1) AS incident_review, "
                "(SELECT s.reason FROM incident_import_skips s WHERE s.alert_id = a.id) "
                f"AS skipped_reason FROM alerts a {where} "
                "ORDER BY a.ts DESC, a.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM alerts a {where}").fetchone()[0]
        return {"rows": [dict(r) for r in rows], "total": total}

    def alert_view_counts(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM({self._ARCHIVED}), 0) FROM alerts a").fetchone()
        return {"all": row[0], "archived": row[1], "unresolved": row[0] - row[1]}

    # ---- operator actions: each validates the transition, then records it in one transaction
    def _load_for_update(self, incident_id: int) -> inc.Incident:
        row = self._conn.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if row is None:
            raise inc.IncidentError(inc.IncidentError.NOT_FOUND, "no such incident")
        return _incident_from_row(row)

    def _apply_report_changes(self, incident: inc.Incident, changes: dict, actor, now) -> None:
        if not changes:
            return
        merged = {**incident.report, **changes}
        self._conn.execute(
            "UPDATE incidents SET report=? WHERE id=?", (json.dumps(merged), incident.id)
        )
        self._insert_incident_event(
            incident.id, now, inc.SRC_OPERATOR, inc.EV_REPORT, actor=actor,
            text=json.dumps(changes, ensure_ascii=False),
        )

    def acknowledge_incident(self, incident_id: int, actor, note, now: float) -> inc.Incident:
        actor = inc.clean_actor(actor)
        note = inc.clean_text(note, "note", max_len=inc.MAX_NOTE_LEN)
        with self._lock, self._conn:
            incident = self._load_for_update(incident_id)
            inc.check_acknowledge(incident)
            self._conn.execute(
                "UPDATE incidents SET review_state=?, acknowledged_ts=?, acknowledged_by=? "
                "WHERE id=?", (inc.REV_ACKNOWLEDGED, now, actor, incident_id),
            )
            self._insert_incident_event(incident_id, now, inc.SRC_OPERATOR, inc.EV_ACK,
                                        actor=actor, text=note or None)
            return self._load_for_update(incident_id)

    def add_incident_note(self, incident_id: int, actor, text, now: float) -> inc.IncidentEvent:
        actor = inc.clean_actor(actor)
        text = inc.clean_text(text, "text", max_len=inc.MAX_NOTE_LEN, min_len=1)
        with self._lock, self._conn:
            self._load_for_update(incident_id)
            self._insert_incident_event(incident_id, now, inc.SRC_OPERATOR, inc.EV_NOTE,
                                        actor=actor, text=text)
            row = self._conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM incident_events WHERE id=last_insert_rowid()"
            ).fetchone()
        return _event_from_row(row)

    def update_incident_report(self, incident_id: int, actor, updates, now: float) -> inc.Incident:
        actor = inc.clean_actor(actor)
        with self._lock, self._conn:
            incident = self._load_for_update(incident_id)
            changes = inc.normalize_report_update(updates, incident.report)
            self._apply_report_changes(incident, changes, actor, now)
            return self._load_for_update(incident_id)

    def resolve_incident(
        self, incident_id: int, actor, summary, updates, now: float, *,
        confirm_unknown: bool = False,
    ) -> inc.Incident:
        """Close an incident. Needs a written resolution summary, and monitoring must already
        have confirmed recovery. The monitoring state and evidence are left exactly as they are."""
        actor = inc.clean_actor(actor)
        summary = inc.clean_text(summary, "resolution_summary",
                                 max_len=inc.REPORT_TEXT_FIELDS["resolution_summary"],
                                 min_len=inc.MIN_SUMMARY_LEN)
        with self._lock, self._conn:
            incident = self._load_for_update(incident_id)
            inc.check_resolve(incident, confirm_unknown=confirm_unknown)
            changes = inc.normalize_report_update(
                {**dict(updates or {}), "resolution_summary": summary}, incident.report
            )
            self._apply_report_changes(incident, changes, actor, now)
            self._conn.execute(
                "UPDATE incidents SET review_state=?, resolved_ts=?, resolved_by=? WHERE id=?",
                (inc.REV_RESOLVED, now, actor, incident_id),
            )
            unknown = incident.monitoring_state == inc.MON_UNKNOWN
            self._insert_incident_event(
                incident_id, now, inc.SRC_OPERATOR, inc.EV_RESOLVED, actor=actor,
                text=summary + ("\n[Monitoring never recorded a recovery for this fault; the "
                                "operator confirmed they understand this.]" if unknown else ""))
            self._store_evidence(
                incident_id, incident.device, incident.opened_ts, incident.recovered_ts,
                incident.last_alert_ts if unknown else (incident.recovered_ts or now))
            return self._load_for_update(incident_id)

    def reopen_incident(self, incident_id: int, actor, reason, now: float) -> inc.Incident:
        actor = inc.clean_actor(actor)
        reason = inc.clean_text(reason, "reason", max_len=inc.MAX_REASON_LEN, min_len=5)
        with self._lock, self._conn:
            incident = self._load_for_update(incident_id)
            inc.check_reopen(incident)
            self._conn.execute(
                "UPDATE incidents SET review_state=?, resolved_ts=NULL, resolved_by=NULL, "
                "acknowledged_ts=NULL, acknowledged_by=NULL, reopened_count=reopened_count+1 "
                "WHERE id=?", (inc.REV_OPEN, incident_id),
            )
            self._insert_incident_event(incident_id, now, inc.SRC_OPERATOR, inc.EV_REOPENED,
                                        actor=actor, text=reason)
            return self._load_for_update(incident_id)

    # ---- QoS telemetry
    def get_qos_status(self, device: str) -> QosStatus | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM qos_status WHERE device=?", (device,)
            ).fetchone()
        return None if row is None else _qos_status_from_row(row)

    def get_qos_states(self, device: str) -> dict[str, QosState]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_QOS_COLUMNS} FROM qos_state WHERE device=?", (device,)
            ).fetchall()
        return {r["row_key"]: _qos_state_from_row(r) for r in rows}

    def save_qos_success(
        self, device: str, ts: float, snapshot: QosSnapshot, states: list[QosState]
    ) -> None:
        """Record a successful poll in one transaction: the status, the latest state of every
        element, and one history row (derived rates, or why there are none) per element that is
        reported now. Absent elements keep their last-seen time and get no history row."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO qos_status(device, ts, status, message, last_ok_ts, uptime_ticks, "
                "adapter, source_mib, truncated) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
                "message=NULL, last_ok_ts=excluded.last_ok_ts, "
                "uptime_ticks=excluded.uptime_ticks, adapter=excluded.adapter, "
                "source_mib=excluded.source_mib, truncated=excluded.truncated",
                (device, ts, STATUS_OK, ts, snapshot.uptime_ticks, snapshot.adapter,
                 snapshot.source_mib, snapshot.truncated),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO qos_state(device, " + _QOS_COLUMNS + ") "
                "VALUES (?, " + ", ".join("?" * 19) + ")",
                [
                    (device, s.key, s.kind, s.name, s.detail, int(s.present), s.ts,
                     s.counter_bits, s.discontinuity, _text(s.packets), _text(s.octets),
                     _text(s.drop_packets), _text(s.drop_octets), s.interval_s, s.pps, s.bps,
                     s.drop_pps, s.drop_bps, s.drop_pct, s.rate_note)
                    for s in states
                ],
            )
            self._conn.executemany(
                "INSERT INTO qos_history(device, row_key, ts, source, pps, bps, drop_pps, "
                "drop_bps, rate_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (device, s.key, ts, snapshot.adapter, s.pps, s.bps, s.drop_pps, s.drop_bps,
                     s.rate_note)
                    for s in states if s.present
                ],
            )

    def save_qos_failure(self, device: str, ts: float, category: str, message: str) -> None:
        """Record that a poll failed. The last good states and the history are left as they were."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO qos_status(device, ts, status, message) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(device) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
                "message=excluded.message",
                (device, ts, category, message),
            )

    def qos_overview(
        self, device: str | None = None
    ) -> list[tuple[str, QosStatus, list[QosState]]]:
        """(device, status, states) for every device with QoS data, by device name."""
        with self._lock:
            sql, args = "SELECT * FROM qos_status", ()
            if device is not None:
                sql, args = sql + " WHERE device=?", (device,)
            statuses = self._conn.execute(sql + " ORDER BY device", args).fetchall()
            result = []
            for status_row in statuses:
                rows = self._conn.execute(
                    f"SELECT {_QOS_COLUMNS} FROM qos_state WHERE device=? ORDER BY kind, row_key",
                    (status_row["device"],),
                ).fetchall()
                result.append((status_row["device"], _qos_status_from_row(status_row),
                               [_qos_state_from_row(r) for r in rows]))
        return result

    def qos_samples(
        self, device: str, start: float, end: float
    ) -> tuple[list[QosSample], float | None, float | None]:
        """The stored history rows of a device in [start, end], oldest first, plus the first and
        last time anything was stored for it."""
        with self._lock:
            first, last = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM qos_history WHERE device=?", (device,)
            ).fetchone()
            rows = self._conn.execute(
                "SELECT h.ts, h.row_key, COALESCE(s.name, h.row_key) AS name, h.pps, h.bps, "
                "h.drop_pps, h.drop_bps, h.rate_note FROM qos_history h "
                "LEFT JOIN qos_state s ON s.device = h.device AND s.row_key = h.row_key "
                "WHERE h.device=? AND h.ts BETWEEN ? AND ? ORDER BY h.ts, h.id",
                (device, start, end),
            ).fetchall()
        samples = [QosSample(r["ts"], r["row_key"], r["name"], r["pps"], r["bps"], r["drop_pps"],
                             r["drop_bps"], r["rate_note"]) for r in rows]
        return samples, first, last

    def prune_qos(self, older_than: float, max_rows: int, absent_older_than: float) -> int:
        """Delete QoS history older than `older_than`, then the oldest beyond `max_rows`, and
        elements that stopped being reported before `absent_older_than`."""
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "DELETE FROM qos_history WHERE ts < ?", (older_than,)
            ).rowcount
            deleted += self._conn.execute(
                "DELETE FROM qos_history WHERE id <= "
                "(SELECT id FROM qos_history ORDER BY id DESC LIMIT 1 OFFSET ?)", (max_rows,)
            ).rowcount
            deleted += self._conn.execute(
                "DELETE FROM qos_state WHERE present=0 AND ts < ?", (absent_older_than,)
            ).rowcount
        return deleted

    # ---- this computer's Wi-Fi link
    def save_wifi_observation(self, ts: float, reading: WifiReading) -> None:
        """Store a successful look at the adapter and mark the poll as ok. The reading must
        already have had the SSID privacy setting applied."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO wifi_observations(" + _WIFI_COLUMNS + ") "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, reading.state, reading.ssid, reading.interface, reading.signal_pct,
                 reading.rssi_dbm, reading.rssi_source, reading.rx_mbps, reading.tx_mbps,
                 reading.radio_type, reading.channel, reading.source),
            )
            self._conn.execute(
                "INSERT INTO wifi_status(id, ts, status, message, last_ok_ts) "
                "VALUES (1, ?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, "
                "status=excluded.status, message=NULL, last_ok_ts=excluded.last_ok_ts",
                (ts, "ok", ts),
            )

    def save_wifi_failure(self, ts: float, category: str, message: str) -> None:
        """Record that a poll produced no observation. Stored history is left as it was."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO wifi_status(id, ts, status, message) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
                "message=excluded.message",
                (ts, category, message),
            )

    def get_wifi_status(self) -> WifiStatus | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM wifi_status WHERE id=1").fetchone()
        return None if row is None else WifiStatus(
            row["ts"], row["status"], row["message"], row["last_ok_ts"]
        )

    def latest_wifi(self, limit: int = 1) -> list[tuple[WifiReading, float]]:
        """The newest stored observations as (reading, ts), newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_WIFI_COLUMNS} FROM wifi_observations ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(_wifi_reading_from_row(r), r["ts"]) for r in rows]

    def wifi_samples(
        self, start: float, end: float
    ) -> tuple[list[WifiSample], float | None, float | None]:
        """Observations in [start, end], oldest first, plus the first and last ever stored."""
        with self._lock:
            first, last = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM wifi_observations"
            ).fetchone()
            rows = self._conn.execute(
                "SELECT ts, state, signal_pct, rssi_dbm, rx_mbps, tx_mbps FROM wifi_observations "
                "WHERE ts BETWEEN ? AND ? ORDER BY ts, id", (start, end),
            ).fetchall()
        samples = [WifiSample(r["ts"], r["state"], r["signal_pct"], r["rssi_dbm"], r["rx_mbps"],
                              r["tx_mbps"]) for r in rows]
        return samples, first, last

    def prune_wifi(self, older_than: float, max_rows: int) -> int:
        """Delete observations older than `older_than`, then the oldest beyond `max_rows`."""
        with self._lock, self._conn:
            deleted = self._conn.execute(
                "DELETE FROM wifi_observations WHERE ts < ?", (older_than,)
            ).rowcount
            deleted += self._conn.execute(
                "DELETE FROM wifi_observations WHERE id <= "
                "(SELECT id FROM wifi_observations ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (max_rows,),
            ).rowcount
        return deleted

    # ---- SNMP interface statistics
    def get_snmp_status(self, device: str) -> SnmpStatus | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snmp_status WHERE device=?", (device,)
            ).fetchone()
        return None if row is None else _status_from_row(row)

    def get_interface_states(self, device: str) -> dict[int, InterfaceState]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM interface_state WHERE device=?", (device,)
            ).fetchall()
        return {row["if_index"]: _state_from_row(row) for row in rows}

    def save_snmp_success(
        self,
        device: str,
        ts: float,
        uptime_ticks: int | None,
        truncated: int,
        states: list[InterfaceState],
    ) -> None:
        """Record a successful poll: the new state of every interface, and the device status."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO snmp_status(device, ts, status, message, last_ok_ts, uptime_ticks, "
                "truncated) VALUES (?, ?, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(device) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
                "message=NULL, last_ok_ts=excluded.last_ok_ts, "
                "uptime_ticks=excluded.uptime_ticks, truncated=excluded.truncated",
                (device, ts, STATUS_OK, ts, uptime_ticks, truncated),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO interface_state(device, " + _STATE_COLUMNS + ") "
                "VALUES (?, " + ", ".join("?" * 19) + ")",
                [
                    (
                        device, s.if_index, s.name, s.oper_status, int(s.present), s.ts,
                        _text(s.in_octets), _text(s.out_octets), _text(s.in_errors),
                        _text(s.out_errors), _text(s.in_discards), _text(s.out_discards),
                        s.interval_s, s.in_bps, s.out_bps, s.in_errors_delta, s.out_errors_delta,
                        s.in_discards_delta, s.out_discards_delta, s.rate_note,
                    )
                    for s in states
                ],
            )

    def save_snmp_failure(self, device: str, ts: float, category: str, message: str) -> None:
        """Record a failed poll. Interface data is left exactly as it was (it is now stale)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO snmp_status(device, ts, status, message) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(device) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
                "message=excluded.message",
                (device, ts, category, message),
            )

    def interface_overview(
        self, device: str | None = None
    ) -> list[tuple[str, SnmpStatus, list[InterfaceState]]]:
        """(device, SNMP status, interfaces) for every device that has been polled."""
        with self._lock:
            sql, args = "SELECT * FROM snmp_status", ()
            if device is not None:
                sql, args = sql + " WHERE device=?", (device,)
            statuses = self._conn.execute(sql + " ORDER BY device", args).fetchall()
            result = []
            for status_row in statuses:
                rows = self._conn.execute(
                    f"SELECT {_STATE_COLUMNS} FROM interface_state WHERE device=? "
                    "ORDER BY if_index", (status_row["device"],),
                ).fetchall()
                result.append(
                    (status_row["device"], _status_from_row(status_row),
                     [_state_from_row(r) for r in rows])
                )
        return result

    def prune_absent_interfaces(self, older_than: float) -> int:
        """Forget interfaces that stopped being reported before `older_than`; returns the count."""
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM interface_state WHERE present=0 AND ts < ?", (older_than,)
            ).rowcount

    def get_alert_state(self, device: str) -> AlertState | None:
        """The stored alerting state for a device, or None if it has never been checked."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status, since, candidate, candidate_count, candidate_since, updated "
                "FROM device_state WHERE device=?",
                (device,),
            ).fetchone()
        if row is None:
            return None
        return AlertState(
            status=Status(row["status"]) if row["status"] else None,
            since=row["since"],
            candidate=Status(row["candidate"]) if row["candidate"] else None,
            candidate_count=row["candidate_count"],
            candidate_since=row["candidate_since"],
            updated=row["updated"],
        )

    def save_alert_state(self, device: str, state: AlertState, alert: Alert | None = None) -> None:
        """Save a device's alerting state and, if given, its new alert in one transaction.

        Doing both together means a crash can't leave an alert without the state change that
        caused it (which would repeat the alert after a restart).
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO device_state(device, status, since, candidate, candidate_count, "
                "candidate_since, updated) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device) DO UPDATE SET status=excluded.status, since=excluded.since, "
                "candidate=excluded.candidate, candidate_count=excluded.candidate_count, "
                "candidate_since=excluded.candidate_since, updated=excluded.updated",
                (
                    device,
                    state.status.value if state.status else None,
                    state.since,
                    state.candidate.value if state.candidate else None,
                    state.candidate_count,
                    state.candidate_since,
                    state.updated,
                ),
            )
            if alert is not None:
                self._conn.execute(
                    "INSERT INTO alerts(device, ts, severity, from_status, to_status, message) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        alert.device,
                        alert.ts,
                        alert.severity.value,
                        alert.from_status.value,
                        alert.to_status.value,
                        alert.message,
                    ),
                )

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        """Newest-first alerts across all devices."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, device, ts, severity, from_status, to_status, message "
                "FROM alerts ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in rows]

    def latest_status(self) -> list[dict]:
        """One row per device with its most recent check (check fields are None if unchecked)."""
        with self._lock:
            return [dict(row) for row in self._conn.execute(_LATEST)]

    def history(self, device: str, limit: int = 100) -> list[dict] | None:
        """Newest-first checks for a device, or None if the device doesn't exist."""
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM devices WHERE name=?", (device,)).fetchone():
                return None
            rows = self._conn.execute(
                "SELECT ts, status, sent, received, rtt_min_ms, rtt_avg_ms, rtt_max_ms "
                "FROM checks WHERE device=? ORDER BY ts DESC, id DESC LIMIT ?",
                (device, limit),
            )
            return [dict(row) for row in rows]
