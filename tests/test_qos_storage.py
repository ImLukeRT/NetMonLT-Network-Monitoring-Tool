"""Persistence of QoS state, status and history: round trips, retention, upgrades, honesty."""

import sqlite3

import pytest

from netmonlt.qos import QosRow, QosSnapshot, advance_device
from netmonlt.storage import Database

GAP = 1000.0
M64 = 2**64 - 1


def snap(*rows, uptime=5000, adapter="diffserv-mib", source="DIFFSERV-MIB (RFC 3289)"):
    return QosSnapshot(adapter, source, tuple(rows), uptime)


def tr(key="c", pkts=0, octs=0, dpkts=None, docts=None, bits=64):
    return QosRow(key, "traffic", key.upper(), f"{key} detail", pkts, octs, dpkts, docts, bits)


def save(db, device="alpha", ts=100.0, snapshot=None, prev=None, uptime=None):
    snapshot = snapshot or snap(tr("a", 1000, 2000, 5, 6), tr("b", 3, 4))
    states = advance_device(prev or {}, uptime, snapshot, ts, GAP)
    db.save_qos_success(device, ts, snapshot, states)
    return states


# ------------------------------------------------------------------ round trips
def test_nothing_is_stored_before_the_first_poll(db, devices):
    db.sync_devices(devices)
    assert db.get_qos_status("alpha") is None and db.get_qos_states("alpha") == {}
    assert db.qos_overview() == [] and db.qos_samples("alpha", 0, 100) == ([], None, None)


def test_a_successful_poll_round_trips_status_and_state(db, devices):
    db.sync_devices(devices)
    save(db, ts=123.5)
    status = db.get_qos_status("alpha")
    assert (status.status, status.message, status.ts, status.last_ok_ts) == ("ok", None, 123.5,
                                                                              123.5)
    assert (status.adapter, status.source_mib, status.uptime_ticks, status.truncated) == (
        "diffserv-mib", "DIFFSERV-MIB (RFC 3289)", 5000, 0)
    a = db.get_qos_states("alpha")["a"]
    assert (a.name, a.detail, a.kind, a.counter_bits) == ("A", "a detail", "traffic", 64)
    assert (a.packets, a.octets, a.drop_packets, a.drop_octets) == (1000, 2000, 5, 6)
    assert a.rate_note == "first_sample" and a.pps is None


def test_counters_beyond_signed_64_bit_survive_exactly(db, devices):
    db.sync_devices(devices)
    save(db, snapshot=snap(tr("big", M64, 2**63, M64, 2**63 + 1)))
    big = db.get_qos_states("alpha")["big"]
    assert (big.packets, big.octets, big.drop_packets, big.drop_octets) == (M64, 2**63, M64,
                                                                            2**63 + 1)


def test_unknown_counters_stay_unknown_and_zero_stays_zero(db, devices):
    db.sync_devices(devices)
    save(db, snapshot=snap(QosRow("d", "drops", "D", None, None, None, 0, None)))
    d = db.get_qos_states("alpha")["d"]
    assert d.packets is None and d.octets is None and d.drop_packets == 0
    assert d.drop_octets is None and d.detail is None


def test_a_damaged_stored_counter_reads_as_unknown_not_an_error(db, devices):
    db.sync_devices(devices)
    save(db)
    with db._lock, db._conn:
        db._conn.execute("UPDATE qos_state SET packets='not-a-number'")
    assert db.get_qos_states("alpha")["a"].packets is None


def test_a_second_poll_stores_the_derived_rates(db, devices):
    db.sync_devices(devices)
    first = save(db, ts=100.0)
    save(db, ts=110.0, snapshot=snap(tr("a", 1500, 12_000, 25, 26), tr("b", 3, 4)),
         prev={s.key: s for s in first}, uptime=5000)
    a = db.get_qos_states("alpha")["a"]
    assert a.pps == 50.0 and a.bps == 10_000 * 8 / 10 and a.drop_pps == 2.0
    assert a.drop_pct == pytest.approx(4.0) and a.rate_note is None


# ------------------------------------------------------------------ failures
def test_a_failure_before_any_success_records_only_the_status(db, devices):
    db.sync_devices(devices)
    db.save_qos_failure("alpha", 50.0, "unsupported", "no counters")
    status = db.get_qos_status("alpha")
    assert (status.status, status.message, status.ts) == ("unsupported", "no counters", 50.0)
    assert status.last_ok_ts is None and status.adapter is None
    assert db.get_qos_states("alpha") == {}
    assert [(d, s.status, st) for d, s, st in db.qos_overview()] == [("alpha", "unsupported", [])]


def test_a_failure_keeps_the_last_good_states_and_history(db, devices):
    db.sync_devices(devices)
    save(db, ts=100.0)
    db.save_qos_failure("alpha", 130.0, "timeout", "no response")
    status = db.get_qos_status("alpha")
    assert (status.status, status.ts) == ("timeout", 130.0)
    assert (status.last_ok_ts, status.uptime_ticks, status.adapter) == (100.0, 5000,
                                                                         "diffserv-mib")
    assert set(db.get_qos_states("alpha")) == {"a", "b"}  # not wiped, not invented
    assert len(db.qos_samples("alpha", 0, 1000)[0]) == 2


def test_recovery_clears_the_failure_message(db, devices):
    db.sync_devices(devices)
    db.save_qos_failure("alpha", 50.0, "timeout", "no response")
    save(db, ts=60.0)
    status = db.get_qos_status("alpha")
    assert (status.status, status.message, status.last_ok_ts) == ("ok", None, 60.0)


# ------------------------------------------------------------------ history
def test_every_present_element_gets_one_history_row_per_poll_with_its_source(db, devices):
    db.sync_devices(devices)
    first = save(db, ts=100.0)
    save(db, ts=110.0, snapshot=snap(tr("a", 1500, 12_000), tr("b", 13, 14)),
         prev={s.key: s for s in first}, uptime=5000)
    samples, first_ts, last_ts = db.qos_samples("alpha", 0, 1000)
    assert [(s.ts, s.key) for s in samples] == [(100.0, "a"), (100.0, "b"), (110.0, "a"),
                                                (110.0, "b")]
    assert (first_ts, last_ts) == (100.0, 110.0)
    assert samples[0].rate_note == "first_sample" and samples[0].pps is None
    assert samples[2].pps == 50.0 and samples[2].name == "A"
    rows = db._conn.execute("SELECT DISTINCT source FROM qos_history").fetchall()
    assert [r[0] for r in rows] == ["diffserv-mib"]  # provenance is stored with the data


def test_history_stores_derived_rates_and_not_raw_counters(db, devices):
    db.sync_devices(devices)
    save(db)
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(qos_history)")}
    assert columns == {"id", "device", "row_key", "ts", "source", "pps", "bps", "drop_pps",
                       "drop_bps", "rate_note"}  # no counters: the table cannot grow with them


def test_an_absent_element_gets_no_history_row(db, devices):
    db.sync_devices(devices)
    first = save(db, ts=100.0)
    save(db, ts=110.0, snapshot=snap(tr("a", 1500, 12_000)), prev={s.key: s for s in first},
         uptime=5000)
    samples, _, _ = db.qos_samples("alpha", 105.0, 1000)
    assert [s.key for s in samples] == ["a"]
    assert db.get_qos_states("alpha")["b"].present is False


def test_history_respects_the_window_and_reports_the_whole_range(db, devices):
    db.sync_devices(devices)
    for ts in (10.0, 50.0, 90.0):
        save(db, ts=ts)
    samples, first, last = db.qos_samples("alpha", 40.0, 60.0)
    assert {s.ts for s in samples} == {50.0} and (first, last) == (10.0, 90.0)


def test_a_vanished_element_keeps_its_key_as_its_name_in_old_history(db, devices):
    db.sync_devices(devices)
    save(db, ts=100.0)
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM qos_state WHERE row_key='b'")
    names = {s.key: s.name for s in db.qos_samples("alpha", 0, 1000)[0]}
    assert names == {"a": "A", "b": "b"}


def test_devices_do_not_share_qos_data(db, devices):
    db.sync_devices(devices)
    save(db, "alpha", snapshot=snap(tr("a", 1, 1)))
    save(db, "beta", snapshot=snap(tr("z", 2, 2)))
    assert set(db.get_qos_states("alpha")) == {"a"} and set(db.get_qos_states("beta")) == {"z"}
    assert [d for d, _, _ in db.qos_overview()] == ["alpha", "beta"]
    assert [d for d, _, _ in db.qos_overview("beta")] == ["beta"]


# ------------------------------------------------------------------ retention
def test_pruning_deletes_old_history_only(db, devices):
    db.sync_devices(devices)
    for ts in (10.0, 20.0, 30.0):
        save(db, ts=ts)
    assert db.prune_qos(older_than=25.0, max_rows=1000, absent_older_than=0.0) == 4
    assert {s.ts for s in db.qos_samples("alpha", 0, 100)[0]} == {30.0}
    assert set(db.get_qos_states("alpha")) == {"a", "b"}  # the latest states are untouched


def test_pruning_enforces_a_hard_row_cap(db, devices):
    db.sync_devices(devices)
    for ts in range(100):
        save(db, ts=float(ts))
    db.prune_qos(older_than=0.0, max_rows=50, absent_older_than=0.0)
    count = db._conn.execute("SELECT COUNT(*) FROM qos_history").fetchone()[0]
    assert count == 50
    assert db.qos_samples("alpha", 0, 1000)[2] == 99.0  # the newest rows were kept


def test_pruning_forgets_only_old_absent_elements(db, devices):
    db.sync_devices(devices)
    first = save(db, ts=100.0)
    save(db, ts=110.0, snapshot=snap(tr("a", 1500, 12_000)), prev={s.key: s for s in first},
         uptime=5000)
    db.prune_qos(older_than=0.0, max_rows=1000, absent_older_than=50.0)
    assert "b" in db.get_qos_states("alpha")  # absent, but seen recently enough
    db.prune_qos(older_than=0.0, max_rows=1000, absent_older_than=105.0)
    assert set(db.get_qos_states("alpha")) == {"a"}


# ------------------------------------------------------------------ safety of the data
def test_a_failed_write_leaves_nothing_behind(db, devices):
    db.sync_devices(devices)
    with pytest.raises(sqlite3.IntegrityError):
        save(db, "ghost")  # no such device: foreign key violation
    assert db.get_qos_status("ghost") is None and db.get_qos_states("ghost") == {}
    assert db._conn.execute("SELECT COUNT(*) FROM qos_history").fetchone()[0] == 0
    save(db, "alpha")  # and the database is still usable
    assert db.get_qos_status("alpha").status == "ok"


def test_removing_a_device_removes_its_qos_data(db, devices):
    db.sync_devices(devices)
    save(db, "alpha")
    save(db, "beta")
    db.sync_devices(devices[1:])
    assert db.get_qos_status("alpha") is None and db.get_qos_states("alpha") == {}
    assert db.qos_samples("alpha", 0, 1000)[0] == []
    assert db.get_qos_status("beta") is not None


def test_no_credential_or_address_column_exists(db):
    for table in ("qos_status", "qos_state", "qos_history"):
        columns = {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})")}
        assert not {c for c in columns if any(w in c for w in ("key", "user", "pass", "secret",
                                                               "community", "address"))
                    and c != "row_key"}


# ------------------------------------------------------------------ restarts and upgrades
def test_a_restart_continues_the_rate_calculation(tmp_path, devices):
    path = tmp_path / "nm.db"
    db = Database(path)
    db.sync_devices(devices)
    save(db, ts=100.0)
    db.close()
    db = Database(path)  # the app was restarted
    db.sync_devices(devices)
    prev = db.get_qos_states("alpha")
    uptime = db.get_qos_status("alpha").uptime_ticks
    save(db, ts=110.0, snapshot=snap(tr("a", 1100, 2000, 5, 6), tr("b", 3, 4)), prev=prev,
         uptime=uptime)
    assert db.get_qos_states("alpha")["a"].pps == 10.0
    db.close()


def test_a_database_from_before_qos_support_is_upgraded_in_place(tmp_path):
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
    assert db.get_qos_status("alpha") is None and db.qos_overview() == []
    save(db)
    assert db.get_qos_states("alpha")["a"].name == "A"
    db.close()
