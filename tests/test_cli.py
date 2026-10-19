import pytest

from helpers import REPO_ROOT
from netmonlt.__main__ import main
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
    rc = main(["--inventory", str(tmp_path / "nope.yaml"), "--db", str(tmp_path / "x.db")])
    assert rc == 2 and not served
    assert "not found" in capsys.readouterr().err


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
