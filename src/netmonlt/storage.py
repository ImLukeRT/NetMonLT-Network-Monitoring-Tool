"""SQLite persistence, using the standard library and plain SQL.

One shared connection guarded by a lock: the collector (event loop) and the API
(thread pool) both use it. WAL mode is enabled so readers don't block the writer.
"""

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from netmonlt.domain import CheckResult, Device

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
"""

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
