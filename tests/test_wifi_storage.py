"""Persistence of Wi-Fi observations and status: round trips, retention, upgrades, privacy."""

import sqlite3

import pytest

from netmonlt.storage import Database
from netmonlt.wifi import STATE_CONNECTED, STATE_DISCONNECTED, WifiReading


def reading(**fields):
    base = dict(state=STATE_CONNECTED, ssid="H*******", interface="Wi-Fi", signal_pct=72,
                rssi_dbm=-58, rssi_source="driver", rx_mbps=866.7, tx_mbps=433.3,
                radio_type="802.11ax", channel=36, source="wlanapi")
    base.update(fields)
    return WifiReading(**base)


def test_nothing_is_stored_before_the_first_poll(db):
    assert db.get_wifi_status() is None and db.latest_wifi() == []
    assert db.wifi_samples(0, 100) == ([], None, None)


def test_an_observation_round_trips_every_field(db):
    db.save_wifi_observation(100.5, reading())
    ((r, ts),) = db.latest_wifi()
    assert ts == 100.5 and r == reading()
    status = db.get_wifi_status()
    assert (status.status, status.message, status.ts, status.last_ok_ts) == ("ok", None, 100.5,
                                                                              100.5)


def test_unavailable_values_stay_unavailable(db):
    db.save_wifi_observation(1.0, WifiReading(STATE_DISCONNECTED, interface="Wi-Fi",
                                              source="wlanapi"))
    ((r, _),) = db.latest_wifi()
    assert r.state == "disconnected" and r.signal_pct is None and r.rssi_dbm is None
    assert r.ssid is None and r.rx_mbps is None and r.channel is None


def test_zero_is_stored_as_zero_and_missing_as_null(db):
    db.save_wifi_observation(1.0, reading(signal_pct=0))
    db.save_wifi_observation(2.0, reading(signal_pct=None))
    rows = db._conn.execute("SELECT signal_pct FROM wifi_observations ORDER BY ts").fetchall()
    assert [r[0] for r in rows] == [0, None]


def test_newest_first_and_limit(db):
    for ts in (3.0, 1.0, 2.0):
        db.save_wifi_observation(ts, reading(signal_pct=int(ts)))
    assert [ts for _, ts in db.latest_wifi(2)] == [3.0, 2.0]


def test_a_failure_records_only_the_status_and_keeps_the_history(db):
    db.save_wifi_observation(100.0, reading())
    db.save_wifi_failure(130.0, "no_adapter", "no wireless adapter found")
    status = db.get_wifi_status()
    assert (status.status, status.message, status.ts) == ("no_adapter", "no wireless adapter "
                                                                         "found", 130.0)
    assert status.last_ok_ts == 100.0  # still the last good observation
    assert len(db.latest_wifi(10)) == 1  # nothing invented, nothing deleted


def test_recovery_clears_the_failure_message(db):
    db.save_wifi_failure(50.0, "timeout", "slow")
    db.save_wifi_observation(60.0, reading())
    status = db.get_wifi_status()
    assert (status.status, status.message, status.last_ok_ts) == ("ok", None, 60.0)


def test_a_failure_before_any_success_has_no_last_ok(db):
    db.save_wifi_failure(50.0, "unsupported_os", "not supported")
    assert db.get_wifi_status().last_ok_ts is None


def test_there_is_only_ever_one_status_row(db):
    for i in range(5):
        db.save_wifi_failure(float(i), "timeout", "x")
        db.save_wifi_observation(float(i) + 0.5, reading())
    assert db._conn.execute("SELECT COUNT(*) FROM wifi_status").fetchone()[0] == 1


def test_samples_cover_a_window_and_report_the_whole_history_bounds(db):
    for ts in (10, 50, 90):
        db.save_wifi_observation(float(ts), reading(signal_pct=ts))
    samples, first, last = db.wifi_samples(40, 60)
    assert [s.ts for s in samples] == [50] and (first, last) == (10, 90)
    assert samples[0].signal_pct == 50 and samples[0].rssi_dbm == -58


# ------------------------------------------------------------------ retention
def test_pruning_deletes_old_observations_only(db):
    for ts in (10, 20, 30, 40):
        db.save_wifi_observation(float(ts), reading())
    assert db.prune_wifi(older_than=25.0, max_rows=1000) == 2
    assert [ts for _, ts in db.latest_wifi(10)] == [40.0, 30.0]
    assert db.prune_wifi(older_than=25.0, max_rows=1000) == 0


def test_pruning_also_enforces_a_hard_row_cap(db):
    for ts in range(100):
        db.save_wifi_observation(float(ts), reading())
    deleted = db.prune_wifi(older_than=0.0, max_rows=30)
    count = db._conn.execute("SELECT COUNT(*) FROM wifi_observations").fetchone()[0]
    assert deleted == 70 and count == 30  # exactly the cap is kept: the newest rows
    assert db.latest_wifi(1)[0][1] == 99.0


def test_the_cap_does_nothing_when_under_it(db):
    for ts in range(5):
        db.save_wifi_observation(float(ts), reading())
    assert db.prune_wifi(older_than=0.0, max_rows=100) == 0


# ------------------------------------------------------------------ safety of the data
def test_only_privacy_safe_columns_exist():
    # no BSSID / access point MAC, adapter MAC, GUID, profile name or key can be stored
    db = Database(":memory:")
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(wifi_observations)")}
    assert columns == {"id", "ts", "state", "ssid", "interface", "signal_pct", "rssi_dbm",
                       "rssi_source", "rx_mbps", "tx_mbps", "radio_type", "channel", "source"}
    status = {r[1] for r in db._conn.execute("PRAGMA table_info(wifi_status)")}
    assert status == {"id", "ts", "status", "message", "last_ok_ts"}
    db.close()


def test_the_database_holds_exactly_the_ssid_it_is_given(db):
    # privacy is applied before storage (see the poller tests); storage does not alter it
    db.save_wifi_observation(1.0, reading(ssid="H*******"))
    assert db._conn.execute("SELECT ssid FROM wifi_observations").fetchone()[0] == "H*******"


# ------------------------------------------------------------------ restarts and upgrades
def test_history_survives_a_restart(tmp_path):
    path = tmp_path / "nm.db"
    db = Database(path)
    db.save_wifi_observation(100.0, reading())
    db.close()
    db = Database(path)
    assert len(db.latest_wifi(10)) == 1 and db.get_wifi_status().status == "ok"
    db.close()


def test_a_database_from_before_wifi_support_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE devices (name TEXT PRIMARY KEY, address TEXT NOT NULL, type TEXT NOT NULL);"
        "CREATE TABLE checks (id INTEGER PRIMARY KEY, device TEXT NOT NULL REFERENCES "
        "devices(name) ON DELETE CASCADE, ts REAL NOT NULL, status TEXT NOT NULL, sent INTEGER "
        "NOT NULL, received INTEGER NOT NULL, rtt_min_ms REAL, rtt_avg_ms REAL, rtt_max_ms REAL);"
        "INSERT INTO devices VALUES ('alpha', '192.0.2.1', 'router');"
        "INSERT INTO checks(device, ts, status, sent, received) VALUES ('alpha', 1, 'up', 3, 3);"
    )
    old.commit()
    old.close()
    db = Database(path)
    assert db.history("alpha")[0]["status"] == "up"  # existing data untouched
    assert db.get_wifi_status() is None
    db.save_wifi_observation(5.0, reading())
    assert len(db.latest_wifi(5)) == 1
    db.close()


def test_opening_twice_does_not_disturb_wifi_data(tmp_path):
    path = tmp_path / "nm.db"
    for _ in range(2):
        db = Database(path)
        db.close()
    db = Database(path)
    db.save_wifi_observation(1.0, reading())
    db.close()
    assert len(Database(path).latest_wifi(5)) == 1


def test_a_failed_write_leaves_nothing_behind(db):
    with pytest.raises(sqlite3.Error):
        db.save_wifi_observation(1.0, reading(source=None))  # source is NOT NULL
    assert db.latest_wifi() == [] and db.get_wifi_status() is None
