import pytest

from helpers import REPO_ROOT
from netmonlt.__main__ import main
from netmonlt.probes import ProbeError
from netmonlt.storage import Database

SHIPPED_INVENTORY = str(REPO_ROOT / "config" / "devices.sim.yaml")


@pytest.fixture
def served(monkeypatch):
    """Replace uvicorn.run so main() returns instead of blocking; records how it was called."""
    calls = []
    monkeypatch.setattr("netmonlt.__main__.uvicorn.run", lambda app, **kw: calls.append(kw))
    return calls


def test_main_syncs_inventory_and_starts_server(tmp_path, served):
    db_path = tmp_path / "nested" / "nm.db"
    rc = main(["--inventory", SHIPPED_INVENTORY, "--db", str(db_path), "--port", "9999"])
    assert rc == 0
    assert served == [{"host": "127.0.0.1", "port": 9999, "log_level": "warning"}]
    db = Database(db_path)
    assert len(db.latest_status()) == 6
    db.close()


def test_missing_inventory_is_a_clean_error(tmp_path, served, capsys):
    missing = tmp_path / "nope.yaml"
    rc = main(["--inventory", str(missing), "--db", str(tmp_path / "x.db")])
    err = capsys.readouterr().err
    assert rc == 2 and not served
    assert "not found" in err
    assert "hint:" in err and str(missing) in err and "--inventory" in err  # says where it looked


def test_a_broken_inventory_that_exists_gets_no_location_hint(tmp_path, served, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("devices: []\n", encoding="utf-8")
    assert main(["--inventory", str(bad), "--db", str(tmp_path / "x.db")]) == 2
    assert "hint:" not in capsys.readouterr().err


def test_invalid_inventory_is_a_clean_error(tmp_path, served, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("devices: []\n", encoding="utf-8")
    rc = main(["--inventory", str(bad), "--db", str(tmp_path / "x.db")])
    assert rc == 2 and not served
    assert "non-empty list" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["directory", "parent_is_a_file"])
def test_unusable_database_path_is_a_clean_error(tmp_path, served, capsys, kind):
    if kind == "directory":
        db_path = tmp_path
    else:
        (tmp_path / "file").write_text("x", encoding="utf-8")
        db_path = tmp_path / "file" / "nm.db"
    rc = main(["--inventory", SHIPPED_INVENTORY, "--db", str(db_path)])
    err = capsys.readouterr().err
    assert rc == 2 and not served
    assert err.startswith("error: cannot open database")


# ------------------------------------------------------------------ real mode wiring
REAL_EXAMPLE = str(REPO_ROOT / "config" / "devices.real.example.yaml")


class FakePinger:
    instances = []
    fail_with = None

    def __init__(self):
        self.preflights = 0
        FakePinger.instances.append(self)

    def preflight(self):
        self.preflights += 1
        if FakePinger.fail_with:
            raise ProbeError(FakePinger.fail_with)


@pytest.fixture
def app_capture(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "netmonlt.__main__.create_app", lambda inv, db, prober: captured.append(prober)
    )
    FakePinger.instances, FakePinger.fail_with = [], None
    monkeypatch.setattr("netmonlt.__main__.IcmpProber", FakePinger)
    return captured


def test_real_mode_uses_the_icmp_prober_and_its_own_database(tmp_path, served, app_capture,
                                                              monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--inventory", REAL_EXAMPLE]) == 0
    assert isinstance(app_capture[0], FakePinger) and app_capture[0].preflights == 1
    assert (tmp_path / "data" / "netmonlt-real.db").exists()
    assert not (tmp_path / "data" / "netmonlt.db").exists()  # never mixed with simulated history
    out = capsys.readouterr().out
    assert "REAL ICMP monitoring is ON" in out and "this-machine (127.0.0.1)" in out


def test_explicit_db_path_wins_in_real_mode(tmp_path, served, app_capture):
    db_path = tmp_path / "mine.db"
    assert main(["--inventory", REAL_EXAMPLE, "--db", str(db_path)]) == 0
    assert db_path.exists()


def test_real_mode_stops_with_a_clean_error_if_ping_is_unusable(tmp_path, served, app_capture,
                                                                 monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    FakePinger.fail_with = "ICMP self-test failed: the 'ping' command was not found on PATH"
    assert main(["--inventory", REAL_EXAMPLE]) == 2
    assert "error: ICMP self-test failed" in capsys.readouterr().err
    assert not served and not app_capture
    assert not (tmp_path / "data").exists()  # nothing was created


def test_simulated_mode_never_touches_icmp(tmp_path, served, monkeypatch):
    def forbidden():
        raise AssertionError("IcmpProber must not be created in simulated mode")

    monkeypatch.setattr("netmonlt.__main__.IcmpProber", forbidden)
    monkeypatch.chdir(tmp_path)
    assert main(["--inventory", SHIPPED_INVENTORY]) == 0
    assert (tmp_path / "data" / "netmonlt.db").exists()
