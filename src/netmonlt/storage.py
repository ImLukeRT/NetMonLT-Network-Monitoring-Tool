"""SQLite persistence, using the standard library and plain SQL.

One shared connection guarded by a lock: the collector (event loop) and the API
(thread pool) both use it. WAL mode is enabled so readers don't block the writer.
"""

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from netmonlt.alerts import Alert, AlertState
from netmonlt.domain import CheckResult, Device, Status
from netmonlt.ifstats import STATUS_OK, InterfaceState, SnmpStatus
from netmonlt.quality import CheckSample

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
"""

_STATE_COLUMNS = (
    "if_index, name, oper_status, present, ts, in_octets, out_octets, in_errors, out_errors, "
    "in_discards, out_discards, interval_s, in_bps, out_bps, in_errors_delta, out_errors_delta, "
    "in_discards_delta, out_discards_delta, rate_note"
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
