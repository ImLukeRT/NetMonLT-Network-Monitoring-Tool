import sys

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
    assert len(db.latest_status()) == 7
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
        "netmonlt.__main__.create_app", lambda inv, db, prober, **kw: captured.append(prober)
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


# ------------------------------------------------------------------ SNMP wiring
def write_inventory(tmp_path, text):
    path = tmp_path / "inv.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


SIM_SNMP = """\
mode: simulated
snmp_poll_interval_s: 5
devices:
  - name: sw1
    address: 192.0.2.1
    sim: {interfaces: [{name: Gi0/1, in_mbps: 5}]}
  - name: plain
    address: 192.0.2.2
"""
REAL_SNMP = """\
mode: real
poll_interval_s: 10
devices:
  - name: r1
    address: 127.0.0.1
    authorized: true
    snmp: {enabled: true, credentials_env: NETMONLT_SNMP_R1, port: 1161}
"""
SECRET = "SECRET-sentinel-value-9f3a"


def test_simulated_interfaces_start_a_simulated_snmp_client(tmp_path, served, monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        "netmonlt.__main__.create_app",
        lambda inv, db, prober, **kw: captured.update(kw) or object(),
    )
    rc = main(["--inventory", write_inventory(tmp_path, SIM_SNMP), "--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert type(captured["snmp_client"]).__name__ == "SimulatedSnmpClient"
    assert "Simulated SNMP interface statistics for: sw1" in capsys.readouterr().out


def test_no_snmp_client_is_created_when_no_device_uses_snmp(tmp_path, served, app_capture):
    inventory = write_inventory(tmp_path, "devices:\n  - {name: a, address: x}\n")
    assert main(["--inventory", inventory, "--db", str(tmp_path / "x.db")]) == 0


def test_real_snmp_with_missing_credentials_stops_and_names_only_the_variables(
    tmp_path, served, app_capture, monkeypatch, capsys
):
    pytest.importorskip("pysnmp")  # without the optional extra the dependency error comes first
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETMONLT_SNMP_R1_USER", "monitor")
    monkeypatch.delenv("NETMONLT_SNMP_R1_AUTH_KEY", raising=False)
    monkeypatch.delenv("NETMONLT_SNMP_R1_PRIV_KEY", raising=False)
    rc = main(["--inventory", write_inventory(tmp_path, REAL_SNMP)])
    err = capsys.readouterr().err
    assert rc == 2 and not served and not app_capture
    assert "NETMONLT_SNMP_R1_AUTH_KEY" in err and "NETMONLT_SNMP_R1_PRIV_KEY" in err
    assert "monitor" not in err
    assert not (tmp_path / "data").exists()  # nothing was created


def test_real_snmp_without_the_optional_dependencies_explains_how_to_install_them(
    tmp_path, served, app_capture, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "netmonlt.snmp_pysnmp", None)  # makes the import fail
    rc = main(["--inventory", write_inventory(tmp_path, REAL_SNMP)])
    err = capsys.readouterr().err
    assert rc == 2 and not served
    assert "uv sync --extra snmp" in err and "netmonlt[snmp]" in err


def test_real_snmp_starts_with_credentials_and_never_prints_them(
    tmp_path, served, monkeypatch, capsys
):
    pytest.importorskip("pysnmp")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETMONLT_SNMP_R1_USER", SECRET + "-user")
    monkeypatch.setenv("NETMONLT_SNMP_R1_AUTH_KEY", SECRET + "-auth")
    monkeypatch.setenv("NETMONLT_SNMP_R1_PRIV_KEY", SECRET + "-priv")
    captured = {}
    monkeypatch.setattr(
        "netmonlt.__main__.create_app",
        lambda inv, db, prober, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr("netmonlt.__main__.IcmpProber", FakePinger)
    FakePinger.instances, FakePinger.fail_with = [], None
    assert main(["--inventory", write_inventory(tmp_path, REAL_SNMP)]) == 0
    out = capsys.readouterr()
    assert "REAL SNMPv3 interface polling is ON" in out.out
    assert "r1 (127.0.0.1:1161, authPriv, SHA256/AES128)" in out.out
    assert SECRET not in out.out and SECRET not in out.err
    assert "snmp_client" in captured and SECRET not in repr(captured["snmp_client"])


def test_real_mode_without_snmp_enabled_never_loads_the_snmp_library(
    tmp_path, served, app_capture, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "netmonlt.snmp_pysnmp", None)  # would fail if it were imported
    assert main(["--inventory", REAL_EXAMPLE]) == 0


# ------------------------------------------------------------------ Wi-Fi wiring
SIM_WIFI = """\
mode: simulated
wifi: {enabled: true, poll_interval_s: 5}
devices:
  - name: plain
    address: 192.0.2.2
"""
REAL_WIFI = """\
mode: real
poll_interval_s: 10
wifi: {enabled: true, backend: wlanapi}
devices:
  - name: lo
    address: 127.0.0.1
    authorized: true
"""


@pytest.fixture
def captured_app(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "netmonlt.__main__.create_app",
        lambda inv, db, prober, **kw: captured.update(kw) or object(),
    )
    return captured


def test_enabled_wifi_in_simulated_mode_uses_the_simulated_source(
    tmp_path, served, captured_app, capsys
):
    rc = main(["--inventory", write_inventory(tmp_path, SIM_WIFI), "--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert type(captured_app["wifi_source"]).__name__ == "SimulatedWifiSource"
    assert "Simulated Wi-Fi telemetry is ON" in capsys.readouterr().out


def test_wifi_is_off_unless_the_inventory_enables_it(tmp_path, served, captured_app):
    plain = "devices:\n  - {name: a, address: 192.0.2.1}\n"
    assert main(["--inventory", write_inventory(tmp_path, plain),
                 "--db", str(tmp_path / "x.db")]) == 0
    assert "wifi_source" not in captured_app


def test_the_shipped_demo_inventory_starts_simulated_wifi(tmp_path, served, captured_app):
    assert main(["--inventory", SHIPPED_INVENTORY, "--db", str(tmp_path / "x.db")]) == 0
    assert type(captured_app["wifi_source"]).__name__ == "SimulatedWifiSource"


def test_real_mode_wifi_uses_the_windows_source_and_says_it_is_local_only(
    tmp_path, served, app_capture, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    assert main(["--inventory", write_inventory(tmp_path, REAL_WIFI)]) == 0
    out = capsys.readouterr().out
    assert "REAL Wi-Fi monitoring is ON for this computer's own adapter only" in out
    assert "No scanning, no other networks" in out and "SSID masked" in out
    if sys.platform != "win32":
        assert "only implemented for Windows" in out


def test_simulated_mode_never_creates_the_windows_source(tmp_path, served, captured_app,
                                                         monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the Windows source must not be created in simulated mode")

    monkeypatch.setattr("netmonlt.wifi_windows.WindowsWifiSource", forbidden)
    assert main(["--inventory", write_inventory(tmp_path, SIM_WIFI),
                 "--db", str(tmp_path / "x.db")]) == 0


# ------------------------------------------------------------------ QoS wiring
SIM_QOS = """\
mode: simulated
qos_poll_interval_s: 5
devices:
  - name: fw
    address: 192.0.2.1
    sim:
      qos:
        classes: [{name: Voice, offered_mbps: 5, priority: true}]
  - name: plain
    address: 192.0.2.2
"""
REAL_QOS = """\
mode: real
poll_interval_s: 10
devices:
  - name: r1
    address: 127.0.0.1
    authorized: true
    snmp: {enabled: true, credentials_env: NETMONLT_SNMP_R1, port: 1161}
    qos: {enabled: true, adapter: diffserv-mib}
"""


def test_simulated_qos_policies_start_a_simulated_qos_client(
    tmp_path, served, captured_app, capsys
):
    rc = main(["--inventory", write_inventory(tmp_path, SIM_QOS), "--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert type(captured_app["qos_client"]).__name__ == "SimulatedQosClient"
    assert "Simulated QoS telemetry for: fw" in capsys.readouterr().out


def test_no_qos_client_is_created_when_no_device_has_qos(tmp_path, served, captured_app):
    plain = "devices:\n  - {name: a, address: 192.0.2.1}\n"
    assert main(["--inventory", write_inventory(tmp_path, plain),
                 "--db", str(tmp_path / "x.db")]) == 0
    assert "qos_client" not in captured_app


def test_the_shipped_demo_inventory_starts_simulated_qos(tmp_path, served, captured_app):
    assert main(["--inventory", SHIPPED_INVENTORY, "--db", str(tmp_path / "x.db")]) == 0
    assert type(captured_app["qos_client"]).__name__ == "SimulatedQosClient"


def test_real_qos_with_missing_credentials_stops_and_names_only_the_variables(
    tmp_path, served, app_capture, monkeypatch, capsys
):
    pytest.importorskip("pysnmp")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETMONLT_SNMP_R1_USER", "monitor")
    monkeypatch.delenv("NETMONLT_SNMP_R1_AUTH_KEY", raising=False)
    monkeypatch.delenv("NETMONLT_SNMP_R1_PRIV_KEY", raising=False)
    rc = main(["--inventory", write_inventory(tmp_path, REAL_QOS)])
    err = capsys.readouterr().err
    assert rc == 2 and not served and not app_capture
    assert "NETMONLT_SNMP_R1_AUTH_KEY" in err and "monitor" not in err
    assert not (tmp_path / "data").exists()  # nothing was created


def test_real_qos_without_the_optional_dependencies_explains_how_to_install_them(
    tmp_path, served, app_capture, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "netmonlt.snmp_pysnmp", None)  # makes the import fail
    rc = main(["--inventory", write_inventory(tmp_path, REAL_QOS)])
    err = capsys.readouterr().err
    # QoS needs SNMP on the same device, so the shared dependency check reports it first
    assert rc == 2 and not served and "uv sync --extra snmp" in err


def test_the_qos_client_builder_names_the_missing_dependency_itself(monkeypatch):
    from netmonlt.__main__ import _real_qos_client

    monkeypatch.setitem(sys.modules, "netmonlt.snmp_pysnmp", None)
    with pytest.raises(ImportError, match="QoS polling needs the optional SNMP dependencies"):
        _real_qos_client([])


def test_real_qos_starts_with_credentials_and_never_prints_them(
    tmp_path, served, monkeypatch, capsys
):
    pytest.importorskip("pysnmp")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NETMONLT_SNMP_R1_USER", SECRET + "-user")
    monkeypatch.setenv("NETMONLT_SNMP_R1_AUTH_KEY", SECRET + "-auth")
    monkeypatch.setenv("NETMONLT_SNMP_R1_PRIV_KEY", SECRET + "-priv")
    captured = {}
    monkeypatch.setattr(
        "netmonlt.__main__.create_app",
        lambda inv, db, prober, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr("netmonlt.__main__.IcmpProber", FakePinger)
    FakePinger.instances, FakePinger.fail_with = [], None
    assert main(["--inventory", write_inventory(tmp_path, REAL_QOS)]) == 0
    out = capsys.readouterr()
    assert "REAL read-only QoS telemetry (SNMPv3) is ON" in out.out
    assert "for these configured devices only" in out.out
    assert "r1 (127.0.0.1, adapter diffserv-mib)" in out.out
    assert SECRET not in out.out and SECRET not in out.err
    assert type(captured["qos_client"]).__name__ == "PySnmpQosClient"
    assert SECRET not in repr(captured["qos_client"])


def test_real_mode_with_snmp_but_without_qos_never_creates_a_qos_client(
    tmp_path, served, monkeypatch
):
    pytest.importorskip("pysnmp")
    monkeypatch.chdir(tmp_path)
    for suffix in ("USER", "AUTH_KEY", "PRIV_KEY"):
        monkeypatch.setenv(f"NETMONLT_SNMP_R1_{suffix}", SECRET + suffix)
    captured = {}
    monkeypatch.setattr(
        "netmonlt.__main__.create_app",
        lambda inv, db, prober, **kw: captured.update(kw) or object(),
    )
    monkeypatch.setattr("netmonlt.__main__.IcmpProber", FakePinger)
    FakePinger.instances, FakePinger.fail_with = [], None
    assert main(["--inventory", write_inventory(tmp_path, REAL_SNMP)]) == 0
    assert "snmp_client" in captured and "qos_client" not in captured


def test_simulated_mode_never_creates_the_real_qos_client(tmp_path, served, captured_app,
                                                          monkeypatch):
    monkeypatch.setitem(sys.modules, "netmonlt.snmp_pysnmp", None)  # would fail if imported
    assert main(["--inventory", write_inventory(tmp_path, SIM_QOS),
                 "--db", str(tmp_path / "x.db")]) == 0
