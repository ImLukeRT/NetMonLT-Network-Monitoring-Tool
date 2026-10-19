"""Persistence of SNMP interface state and status (no SNMP library, no network)."""

import sqlite3

import pytest

from netmonlt.ifstats import (
    InterfaceReading,
    InterfaceSnapshot,
    InterfaceState,
    advance_device,
    mark_absent,
)
from netmonlt.storage import Database

GAP = 1000.0
MAX64 = 2**64 - 1


def state(index=1, name="eth0", present=True, ts=100.0, **fields):
    return InterfaceState(
        if_index=index, name=name, oper_status="up", present=present, ts=ts, **fields
    )


FULL = state(
    1, "Gi0/1", in_octets=MAX64, out_octets=2**63, in_errors=2**32 - 1, out_errors=0,
    in_discards=7, out_discards=8, interval_s=10.0, in_bps=1.5e9, out_bps=0.0,
    in_errors_delta=3, out_errors_delta=0, in_discards_delta=1, out_discards_delta=2,
    rate_note=None,
)


def save(db, device="alpha", ts=100.0, uptime=5000, truncated=0, states=(FULL,)):
    db.save_snmp_success(device, ts, uptime, truncated, list(states))


# ------------------------------------------------------------------ round trips
def test_nothing_is_stored_before_the_first_poll(db, devices):
    db.sync_devices(devices)
    assert db.get_snmp_status("alpha") is None and db.get_interface_states("alpha") == {}
    assert db.interface_overview() == []


def test_a_successful_poll_round_trips_every_field(db, devices):
    db.sync_devices(devices)
    save(db, ts=123.5, uptime=999, truncated=2)
    status = db.get_snmp_status("alpha")
    assert (status.status, status.message) == ("ok", None)
    assert (status.ts, status.last_ok_ts) == (123.5, 123.5)
    assert (status.uptime_ticks, status.truncated) == (999, 2)
    assert db.get_interface_states("alpha") == {1: FULL}


def test_counters_beyond_signed_64_bit_survive_exactly(db, devices):
    # SQLite integers are signed 64-bit; a Counter64 can be larger, so counters are stored as text.
    db.sync_devices(devices)
    save(db, states=[FULL])
    loaded = db.get_interface_states("alpha")[1]
    assert loaded.in_octets == MAX64 and loaded.out_octets == 2**63


def test_unknown_values_stay_unknown(db, devices):
    db.sync_devices(devices)
    save(db, states=[state(rate_note="first_sample")])
    loaded = db.get_interface_states("alpha")[1]
    assert loaded.in_octets is None and loaded.in_bps is None and loaded.in_errors_delta is None
    assert loaded.rate_note == "first_sample" and loaded.interval_s is None


def test_a_damaged_stored_counter_reads_as_unknown_not_an_error(db, devices):
    db.sync_devices(devices)
    save(db)
    with db._lock, db._conn:
        db._conn.execute("UPDATE interface_state SET in_octets='not-a-number'")
    assert db.get_interface_states("alpha")[1].in_octets is None


def test_each_poll_replaces_the_previous_state(db, devices):
    db.sync_devices(devices)
    save(db, ts=100.0, states=[state(1, "a", in_octets=1), state(2, "b", in_octets=2)])
    newer = [state(1, "a", ts=110.0, in_octets=10), mark_absent(state(2, "b"))]
    save(db, ts=110.0, states=newer)
    states = db.get_interface_states("alpha")
    assert states[1].in_octets == 10 and states[1].ts == 110.0
    gone = states[2]
    assert (gone.present, gone.oper_status, gone.in_octets) == (False, "absent", None)


# ------------------------------------------------------------------ failures
def test_a_failure_before_any_success_records_only_the_status(db, devices):
    db.sync_devices(devices)
    db.save_snmp_failure("alpha", 50.0, "timeout", "no response")
    status = db.get_snmp_status("alpha")
    assert (status.status, status.message, status.ts) == ("timeout", "no response", 50.0)
    assert status.last_ok_ts is None and status.uptime_ticks is None
    assert db.get_interface_states("alpha") == {}
    assert [(d, s.status, i) for d, s, i in db.interface_overview()] == [("alpha", "timeout", [])]


def test_a_failure_keeps_the_last_good_interface_data_and_its_history(db, devices):
    db.sync_devices(devices)
    save(db, ts=100.0, uptime=777)
    db.save_snmp_failure("alpha", 130.0, "auth_failure", "authentication failed")
    status = db.get_snmp_status("alpha")
    assert (status.status, status.ts) == ("auth_failure", 130.0)
    assert (status.last_ok_ts, status.uptime_ticks) == (100.0, 777)  # still the last good poll
    assert db.get_interface_states("alpha") == {1: FULL}  # not wiped, not invented


def test_recovery_clears_the_failure_message(db, devices):
    db.sync_devices(devices)
    db.save_snmp_failure("alpha", 50.0, "timeout", "no response")
    save(db, ts=60.0)
    status = db.get_snmp_status("alpha")
    assert (status.status, status.message, status.last_ok_ts) == ("ok", None, 60.0)


# ------------------------------------------------------------------ queries
def test_overview_is_ordered_and_can_be_filtered(db, devices):
    db.sync_devices(devices)
    save(db, "beta", states=[state(2, "b"), state(1, "a")])
    save(db, "alpha", states=[state(1, "x")])
    overview = db.interface_overview()
    assert [d for d, _, _ in overview] == ["alpha", "beta"]
    assert [s.if_index for s in overview[1][2]] == [1, 2]
    only = db.interface_overview("beta")
    assert [d for d, _, _ in only] == ["beta"]
    assert db.interface_overview("nobody") == []


def test_the_devices_do_not_share_interface_state(db, devices):
    db.sync_devices(devices)
    save(db, "alpha", states=[state(1, "a", in_octets=1)])
    save(db, "beta", states=[state(1, "b", in_octets=2)])
    assert db.get_interface_states("alpha")[1].in_octets == 1
    assert db.get_interface_states("beta")[1].in_octets == 2


# ------------------------------------------------------------------ safety of the data
def test_a_failed_write_leaves_nothing_behind(db, devices):
    db.sync_devices(devices)
    with pytest.raises(sqlite3.IntegrityError):
        save(db, "ghost")  # no such device: foreign key violation
    assert db.get_snmp_status("ghost") is None and db.get_interface_states("ghost") == {}
    save(db, "alpha")  # and the database is still usable
    assert db.get_snmp_status("alpha").status == "ok"


def test_removing_a_device_removes_its_interface_data(db, devices):
    db.sync_devices(devices)
    save(db, "alpha")
    save(db, "beta")
    db.sync_devices(devices[1:])
    assert db.get_snmp_status("alpha") is None and db.get_interface_states("alpha") == {}
    assert db.get_snmp_status("beta") is not None


def test_prune_forgets_only_old_absent_interfaces(db, devices):
    db.sync_devices(devices)
    gone_old = mark_absent(state(1, "old", ts=100.0))
    gone_new = mark_absent(state(2, "new", ts=900.0))
    alive = state(3, "alive", ts=50.0)  # old timestamp, but still present
    save(db, states=[gone_old, gone_new, alive])
    assert db.prune_absent_interfaces(older_than=500.0) == 1
    assert sorted(db.get_interface_states("alpha")) == [2, 3]
    assert db.prune_absent_interfaces(older_than=500.0) == 0


# ------------------------------------------------------------------ restarts and upgrades
def test_a_restart_continues_the_rate_calculation(tmp_path, devices):
    path = tmp_path / "nm.db"
    db = Database(path)
    db.sync_devices(devices)
    reading = InterfaceReading(1, "eth0", "up", in_octets=0, out_octets=0)
    states = advance_device({}, None, InterfaceSnapshot((reading,), 1000), 100.0, GAP)
    db.save_snmp_success("alpha", 100.0, 1000, 0, states)
    db.close()

    db = Database(path)  # the app was restarted
    db.sync_devices(devices)
    prev = db.get_interface_states("alpha")
    prev_uptime = db.get_snmp_status("alpha").uptime_ticks
    later = InterfaceReading(1, "eth0", "up", in_octets=1_250_000, out_octets=0)
    now = advance_device(prev, prev_uptime, InterfaceSnapshot((later,), 2000), 110.0, GAP)
    assert now[0].in_bps == 1_000_000.0 and now[0].rate_note is None
    db.close()


def test_a_database_from_before_snmp_support_is_upgraded_in_place(tmp_path):
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
    assert db.get_snmp_status("alpha") is None and db.interface_overview() == []
    save(db)
    assert db.get_interface_states("alpha")[1].name == "Gi0/1"
    db.close()
