from netmonlt.domain import CheckResult, Device, PingResult, Status


def check(device, ts, status=Status.UP, avg=5.0):
    return CheckResult(device, ts, status, PingResult(3, 3, avg, avg, avg))


def test_sync_adds_updates_and_removes(db, devices):
    db.sync_devices(devices)
    assert [r["name"] for r in db.latest_status()] == ["alpha", "beta"]

    db.sync_devices([Device("alpha", "192.0.2.99", "firewall")])
    rows = db.latest_status()
    assert [r["name"] for r in rows] == ["alpha"]
    assert (rows[0]["address"], rows[0]["type"]) == ("192.0.2.99", "firewall")


def test_unchecked_device_has_no_status(db, devices):
    db.sync_devices(devices)
    assert all(r["status"] is None and r["ts"] is None for r in db.latest_status())


def test_latest_status_returns_newest_check(db, devices):
    db.sync_devices(devices)
    db.record_check(check("alpha", 100, Status.UP))
    db.record_check(check("alpha", 200, Status.DOWN))
    db.record_check(check("alpha", 150, Status.UP))  # older than 200, inserted later
    alpha = next(r for r in db.latest_status() if r["name"] == "alpha")
    assert (alpha["ts"], alpha["status"]) == (200, "down")


def test_history_is_newest_first_and_limited(db, devices):
    db.sync_devices(devices)
    for ts in (1, 2, 3, 4):
        db.record_check(check("alpha", ts))
    assert [c["ts"] for c in db.history("alpha", limit=3)] == [4, 3, 2]
    assert db.history("beta") == []
    assert db.history("missing") is None


def test_removing_a_device_deletes_its_history(db, devices):
    db.sync_devices(devices)
    db.record_check(check("beta", 1))
    db.sync_devices(devices[:1])
    db.sync_devices(devices)  # re-add: must not resurrect old checks
    assert db.history("beta") == []


def test_data_persists_across_connections(tmp_path, devices):
    from netmonlt.storage import Database

    path = tmp_path / "sub" / "nm.db"
    first = Database(path)
    first.sync_devices(devices)
    first.record_check(check("alpha", 42))
    first.close()

    second = Database(path)
    assert second.history("alpha")[0]["ts"] == 42
    second.close()


def test_recording_for_an_unknown_device_is_rejected_and_db_stays_usable(db, devices):
    import sqlite3

    import pytest

    db.sync_devices(devices)
    with pytest.raises(sqlite3.IntegrityError):
        db.record_check(check("ghost", 1))
    db.record_check(check("alpha", 2))  # the failed write must not poison the connection
    assert [c["ts"] for c in db.history("alpha")] == [2]


def test_concurrent_writer_and_readers_do_not_collide(db, devices):
    import threading

    db.sync_devices(devices)
    writes, errors, done = 300, [], threading.Event()

    def writer():
        try:
            for i in range(writes):
                db.record_check(check("alpha", i))
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    def reader():
        try:
            while not done.is_set():
                db.latest_status()
                db.history("alpha", 50)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(db.history("alpha", 1000)) == writes
