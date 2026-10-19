import sqlite3

import pytest

from helpers import PINGS, FakeClock, ScriptedProber, run_cycles
from netmonlt.alerts import (
    Alert,
    AlertState,
    Severity,
    _duration,
    advance,
    process_check,
)
from netmonlt.domain import CheckResult, Device, PingResult, SimProfile, Status
from netmonlt.probes import SimulatedProber
from netmonlt.storage import Database

U, D, X = Status.UP, Status.DEGRADED, Status.DOWN
GAP = 25.0


def chk(status, ts, device="alpha", ping=None):
    return CheckResult(device, ts, status, ping or PINGS[status])


def feed(statuses, *, start=0.0, step=5.0, state=None, device="alpha"):
    """Apply statuses `step` seconds apart. Returns (final state, [(index, alert), ...])."""
    alerts = []
    for i, status in enumerate(statuses):
        state, alert = advance(state, chk(status, start + i * step, device), GAP)
        if alert:
            alerts.append((i, alert))
    return state, alerts


# ------------------------------------------------------------------ baseline
def test_first_checks_set_a_silent_baseline():
    state, alerts = feed([U, U])
    assert state.status is None and alerts == []  # two agreeing checks aren't enough yet
    state, alerts = feed([U, U, U])
    assert (state.status, state.since) == (U, 0.0) and alerts == []


def test_baseline_waits_for_agreeing_checks_even_if_the_first_was_unrepresentative():
    state, alerts = feed([U, D, U, U, U])
    assert alerts == []
    assert (state.status, state.since) == (U, 10.0)


def test_a_device_first_seen_as_down_is_a_baseline_not_an_alert():
    state, alerts = feed([X, X, X, X, X])
    assert state.status is X and alerts == []


# ------------------------------------------------------------------ transitions
def test_up_to_down_alerts_after_three_consecutive_checks():
    state, alerts = feed([U] * 3 + [X] * 3)
    assert len(alerts) == 1
    index, alert = alerts[0]
    assert index == 5  # the third consecutive down check, not the first
    assert (alert.device, alert.severity) == ("alpha", Severity.CRITICAL)
    assert (alert.from_status, alert.to_status, alert.ts) == (U, X, 25.0)
    assert alert.message == "alpha is DOWN: no reply to 3 pings (was up for 15s)"
    assert (state.status, state.since) == (X, 15.0)  # the outage began at the first down check


def test_recovery_down_to_up():
    _, alerts = feed([U] * 3 + [X] * 3 + [U] * 3)
    assert len(alerts) == 2
    alert = alerts[1][1]
    assert (alert.severity, alert.from_status, alert.to_status) == (Severity.INFO, X, U)
    assert alert.ts == 40.0
    assert alert.message == "alpha recovered: 3/3 replies, avg 2 ms (was down for 15s)"


@pytest.mark.parametrize("old, new, severity", [
    (U, D, Severity.WARNING),
    (D, U, Severity.INFO),
    (D, X, Severity.CRITICAL),
    (X, D, Severity.WARNING),
    (U, X, Severity.CRITICAL),
    (X, U, Severity.INFO),
])
def test_every_transition_into_or_out_of_degraded_and_down(old, new, severity):
    _, alerts = feed([old] * 3 + [new] * 3)
    assert len(alerts) == 1
    alert = alerts[0][1]
    assert (alert.from_status, alert.to_status, alert.severity) == (old, new, severity)


def test_degraded_messages_explain_why():
    _, alerts = feed([U] * 3 + [D] * 3)
    assert "alpha is DEGRADED: 33% packet loss (was up for 15s)" == alerts[0][1].message
    _, alerts = feed([X] * 3 + [D] * 3)
    assert alerts[0][1].message.startswith("alpha is responding again but DEGRADED: 33% packet")


def test_high_latency_is_reported_as_the_reason():
    slow = PingResult(3, 3, 200.0, 200.0, 200.0)
    state, _ = feed([U] * 3)
    alert = None
    for i in range(3):
        state, alert = advance(state, chk(D, 15.0 + i * 5, ping=slow), GAP)
    assert alert is not None and "high latency (avg 200 ms)" in alert.message


# ------------------------------------------------------------------ noise and repetition
def test_short_blips_do_not_alert():
    blips = [X, X, U, X, X, U, D, U, D, D, U, X, D, X]
    _, alerts = feed([U] * 3 + blips)
    assert alerts == []


def test_a_pending_change_restarts_when_the_status_differs():
    state, alerts = feed([U] * 3 + [X, X, D, X, X])
    assert alerts == []
    _, alerts = feed([X], start=40.0, state=state)
    assert len(alerts) == 1  # now three down checks in a row


def test_staying_in_one_state_never_repeats_an_alert():
    _, alerts = feed([U] * 3 + [X] * 100)
    assert len(alerts) == 1
    _, alerts = feed([U] * 100)
    assert alerts == []
    _, alerts = feed([U] * 3 + [D] * 100)
    assert len(alerts) == 1


def test_flapping_between_down_and_degraded_does_not_alert_either_way():
    _, alerts = feed([U] * 3 + [X, D] * 20)
    assert alerts == []


def test_custom_confirmation_threshold():
    state, alert = advance(None, chk(U, 0.0), GAP, confirm_checks=1)
    assert state.status is U and alert is None  # baseline
    state, alert = advance(state, chk(X, 5.0), GAP, confirm_checks=1)
    assert alert is not None and alert.to_status is X


# ------------------------------------------------------------------ no reliable previous state
def test_a_long_gap_discards_the_old_state_and_invents_no_transition():
    state, alerts = feed([U] * 3)
    state, alerts = feed([X] * 3, start=1000.0, state=state)  # an hour-ish later
    assert alerts == []  # we can't know when it went down
    assert state.status is X  # it simply becomes the new baseline


def test_a_gap_exactly_at_the_limit_is_still_reliable():
    state, _ = feed([U] * 3)  # last check at ts=10
    state, alerts = feed([X] * 3, start=10.0 + GAP, state=state)
    assert len(alerts) == 1


def test_a_clock_stepped_far_backwards_is_not_trusted():
    state, _ = feed([U] * 3, start=100_000.0)
    state, alerts = feed([X] * 3, start=0.0, state=state)
    assert alerts == [] and state.status is X


@pytest.mark.parametrize("seconds, text", [
    (-5, "0s"), (0, "0s"), (45, "45s"), (59.6, "1m 00s"), (65, "1m 05s"),
    (3599, "59m 59s"), (3660, "1h 01m"), (90000, "25h 00m"),
])
def test_duration_formatting(seconds, text):
    assert _duration(seconds) == text


# ------------------------------------------------------------------ persistence
def test_state_round_trips_through_the_database(db, devices):
    db.sync_devices(devices)
    assert db.get_alert_state("alpha") is None
    full = AlertState(U, 1.0, X, 2, 5.0, 6.0)
    empty = AlertState(updated=7.0)
    for state in (full, empty):
        db.save_alert_state("alpha", state)
        assert db.get_alert_state("alpha") == state


def test_alerts_are_stored_with_every_field(db, devices):
    db.sync_devices(devices)
    for i, status in enumerate([U] * 3 + [X] * 3):
        process_check(db, chk(status, 1000.0 + i * 5), GAP)
    (row,) = db.recent_alerts()
    assert (row["device"], row["ts"], row["severity"]) == ("alpha", 1025.0, "critical")
    assert (row["from_status"], row["to_status"]) == ("up", "down")
    assert row["message"] == "alpha is DOWN: no reply to 3 pings (was up for 15s)"
    assert isinstance(row["id"], int)


def test_recent_alerts_are_newest_first_and_limited(db, devices):
    db.sync_devices(devices)
    for ts in (1.0, 2.0, 3.0, 4.0, 5.0):
        alert = Alert("alpha", ts, Severity.INFO, X, U, f"alert at {ts}")
        db.save_alert_state("alpha", AlertState(updated=ts), alert)
    assert [a["ts"] for a in db.recent_alerts()] == [5.0, 4.0, 3.0, 2.0, 1.0]
    assert [a["ts"] for a in db.recent_alerts(limit=2)] == [5.0, 4.0]


def test_state_and_alert_are_saved_atomically(db, devices):
    db.sync_devices(devices)
    broken = Alert("alpha", 1.0, Severity.INFO, X, U, message=object())  # can't be stored
    with pytest.raises(sqlite3.Error):
        db.save_alert_state("alpha", AlertState(status=U, since=1.0, updated=1.0), broken)
    assert db.get_alert_state("alpha") is None  # the state change was rolled back too
    assert db.recent_alerts() == []


def test_removing_a_device_removes_its_alerts_and_state(db, devices):
    db.sync_devices(devices)
    for i, status in enumerate([U] * 3 + [X] * 3):
        process_check(db, chk(status, i * 5.0), GAP)
    assert db.recent_alerts()
    db.sync_devices(devices[1:])
    assert db.recent_alerts() == [] and db.get_alert_state("alpha") is None


# ------------------------------------------------------------------ restarts
def reopen(path, devices):
    database = Database(path)
    database.sync_devices(devices)  # what main() does on every start
    return database


def test_a_restart_in_the_middle_of_a_change_keeps_counting(tmp_path, devices):
    path = tmp_path / "nm.db"
    db = reopen(path, devices)
    for i, status in enumerate([U] * 3 + [X] * 2):
        process_check(db, chk(status, i * 5.0), GAP)
    assert db.recent_alerts() == []
    db.close()

    db = reopen(path, devices)  # restart, well inside the allowed gap
    process_check(db, chk(X, 25.0), GAP)
    assert [a["to_status"] for a in db.recent_alerts()] == ["down"]
    for i in range(10):  # and no repeats afterwards, even across another restart
        process_check(db, chk(X, 30.0 + i * 5), GAP)
    db.close()
    db = reopen(path, devices)
    process_check(db, chk(X, 100.0), GAP)
    assert len(db.recent_alerts()) == 1
    db.close()


def test_after_long_downtime_no_historical_transition_is_invented(tmp_path, devices):
    path = tmp_path / "nm.db"
    db = reopen(path, devices)
    for i, status in enumerate([U] * 3 + [X] * 3):  # device goes down; alert raised
        process_check(db, chk(status, i * 5.0), GAP)
    assert len(db.recent_alerts()) == 1
    db.close()

    db = reopen(path, devices)  # app was off for an hour; the device is up again
    for i in range(6):
        process_check(db, chk(U, 3600.0 + i * 5), GAP)
    assert [a["to_status"] for a in db.recent_alerts()] == ["down"]  # no made-up "recovered"
    assert db.get_alert_state("alpha").status is U  # but the new baseline is in place
    process_check(db, chk(X, 3640.0), GAP)
    process_check(db, chk(X, 3645.0), GAP)
    process_check(db, chk(X, 3650.0), GAP)
    assert [a["to_status"] for a in db.recent_alerts()] == ["down", "down"]  # real change alerts
    db.close()


def test_a_database_from_before_alerting_is_upgraded_in_place(tmp_path):
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
    assert db.get_alert_state("alpha") is None and db.recent_alerts() == []
    process_check(db, chk(U, 100.0), GAP)
    assert db.get_alert_state("alpha").candidate is U
    db.close()


# ------------------------------------------------------------------ with the collector
def test_collector_alerts_only_for_the_device_that_changed(db, devices):
    db.sync_devices(devices)
    prober = ScriptedProber({"alpha": [U] * 3 + [X], "beta": [U]})
    run_cycles(devices, prober, db, 8)
    alerts = db.recent_alerts()
    assert [(a["device"], a["severity"], a["to_status"]) for a in alerts] == [
        ("alpha", "critical", "down")
    ]


def test_collector_repeated_polling_in_one_state_raises_one_alert(db, devices):
    db.sync_devices(devices)
    run_cycles(devices, ScriptedProber({"alpha": [U] * 3 + [X], "beta": [D]}), db, 40)
    assert len(db.recent_alerts()) == 1  # beta stays degraded from its baseline: nothing


def test_short_probe_errors_do_not_break_the_streak_or_count_as_down(db, devices):
    db.sync_devices(devices)
    script = {"alpha": [U, U, U, None, None, X, X, X], "beta": [U]}
    run_cycles(devices, ScriptedProber(script), db, 8)
    (alert,) = db.recent_alerts()
    assert (alert["device"], alert["to_status"]) == ("alpha", "down")


def test_a_long_run_of_probe_errors_is_not_an_outage_and_resets_the_baseline(db, devices):
    db.sync_devices(devices)
    script = {"alpha": [U, U, U] + [None] * 10 + [X, X, X, X], "beta": [U]}
    run_cycles(devices, ScriptedProber(script), db, 17)
    assert db.recent_alerts() == []  # 50s without any measurement: nothing can be claimed
    assert db.get_alert_state("alpha").status is X


def test_an_alerting_failure_never_stops_monitoring(db, devices, monkeypatch, caplog):
    db.sync_devices(devices)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "save_alert_state", boom)
    run_cycles(devices, ScriptedProber({"alpha": [U], "beta": [U]}), db, 2)
    assert len(db.history("alpha")) == 2 and len(db.history("beta")) == 2
    assert "alert evaluation failed for alpha" in caplog.text
    assert "alert evaluation failed for beta" in caplog.text  # beta still processed after alpha


def test_scripted_simulated_outage_produces_exactly_one_down_and_one_recovery(db):
    clock = FakeClock(1000.0)
    devs = [
        Device("switch", "192.0.2.10", sim=SimProfile(3, 0.5, 0.0, 30, 30)),
        Device("server", "192.0.2.30", sim=SimProfile(1, 0.2)),
    ]
    db.sync_devices(devs)
    prober = SimulatedProber(clock=clock)

    import asyncio

    from netmonlt.collector import poll_once

    async def go():
        for i in range(24):  # 2 minutes of 5s polling
            clock.now = 1000.0 + i * 5
            await poll_once(devs, prober, db, clock=clock)

    asyncio.run(go())
    alerts = list(reversed(db.recent_alerts()))
    assert [(a["device"], a["severity"], a["from_status"], a["to_status"]) for a in alerts] == [
        ("switch", "critical", "up", "down"),
        ("switch", "info", "down", "up"),
    ]
    assert [a["ts"] for a in alerts] == [1040.0, 1070.0]  # confirmed on the third check each time
    assert "was down for 30s" in alerts[1]["message"]
