"""The Windows Wi-Fi backends: native WLAN API decoding, the helper process, netsh fallback.

Nothing here needs Windows or an adapter: the structure layouts are checked directly, the API is
replaced by a fake, and the commands by a fake runner. One test runs the real helper process, and
accepts whatever honest answer the machine gives (an adapter, none, or "not supported here").
"""

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import netmonlt
from netmonlt import wifi_wlanapi as api
from netmonlt.domain import WifiConfig
from netmonlt.wifi import WifiError
from netmonlt.wifi_windows import CommandResult, WindowsWifiSource, run_command


# ------------------------------------------------------------------ structure layouts
def test_structure_sizes_match_the_windows_headers():
    assert ctypes.sizeof(api.DOT11_SSID) == 36
    assert ctypes.sizeof(api.WLAN_ASSOCIATION_ATTRIBUTES) == 68
    assert ctypes.sizeof(api.WLAN_SECURITY_ATTRIBUTES) == 16
    assert ctypes.sizeof(api.WLAN_CONNECTION_ATTRIBUTES) == 604
    assert ctypes.sizeof(api.WLAN_INTERFACE_INFO) == 532


def test_field_offsets_inside_the_association_attributes():
    f = api.WLAN_ASSOCIATION_ATTRIBUTES
    assert (f.dot11BssType.offset, f.dot11Bssid.offset, f.dot11PhyType.offset) == (36, 40, 48)
    assert (f.wlanSignalQuality.offset, f.ulRxRate.offset, f.ulTxRate.offset) == (56, 60, 64)
    assert api.WLAN_CONNECTION_ATTRIBUTES.wlanAssociationAttributes.offset == 520


def connection_bytes(ssid=b"HomeWiFi", quality=87, rx=866_700, tx=780_000, phy=10,
                     bssid=b"\x11\x22\x33\x44\x55\x66"):
    attrs = api.WLAN_CONNECTION_ATTRIBUTES()
    attrs.isState = 1
    association = attrs.wlanAssociationAttributes
    association.dot11Ssid.uSSIDLength = len(ssid)
    for i, byte in enumerate(ssid[:32]):
        association.dot11Ssid.ucSSID[i] = byte
    for i, byte in enumerate(bssid):
        association.dot11Bssid[i] = byte
    association.dot11PhyType = phy
    association.wlanSignalQuality = quality
    association.ulRxRate, association.ulTxRate = rx, tx
    for i, ch in enumerate("HomeWiFi"):  # the profile name: must never be read
        attrs.strProfileName[i] = ord(ch)
    return bytes(attrs)


def test_a_connection_buffer_is_decoded():
    d = api.decode_connection(connection_bytes())
    assert d == {"ssid": "HomeWiFi", "signal_pct": 87, "rx_mbps": 866.7, "tx_mbps": 780.0,
                 "radio_type": "802.11ax"}


def test_the_access_point_mac_and_profile_name_are_never_returned():
    d = api.decode_connection(connection_bytes())
    assert "bssid" not in json.dumps(d).lower()
    assert "11:22" not in json.dumps(d) and set(d) == {
        "ssid", "signal_pct", "rx_mbps", "tx_mbps", "radio_type"}


def test_zero_rates_and_unknown_phy_types_are_unavailable_not_zero():
    d = api.decode_connection(connection_bytes(rx=0, tx=0, phy=0))
    assert d["rx_mbps"] is None and d["tx_mbps"] is None and d["radio_type"] is None


def test_a_long_ssid_length_is_clamped_and_bad_utf8_is_replaced():
    attrs = api.WLAN_CONNECTION_ATTRIBUTES()
    attrs.wlanAssociationAttributes.dot11Ssid.uSSIDLength = 999
    for i in range(32):
        attrs.wlanAssociationAttributes.dot11Ssid.ucSSID[i] = 0xFF
    ssid = api.decode_connection(bytes(attrs))["ssid"]
    assert len(ssid) == 32 and set(ssid) == {"\ufffd"}


def test_a_short_buffer_is_an_error_not_a_crash():
    with pytest.raises(WifiError):
        api.decode_connection(b"\x00" * 10)


def interface_list_bytes(items):
    header = len(items).to_bytes(4, "little") + (0).to_bytes(4, "little")
    body = b""
    for guid, name, state in items:
        info = api.WLAN_INTERFACE_INFO()
        for i, byte in enumerate(guid):
            info.InterfaceGuid[i] = byte
        for i, ch in enumerate(name):
            info.strInterfaceDescription[i] = ord(ch)
        info.isState = state
        body += bytes(info)
    return header + body


def test_an_interface_list_is_decoded():
    buf = interface_list_bytes([(b"\x01" * 16, "Intel Wi-Fi", 1), (b"\x02" * 16, "USB Wi-Fi", 4)])
    assert api.decode_interface_list(buf) == [
        (b"\x01" * 16, "Intel Wi-Fi", 1), (b"\x02" * 16, "USB Wi-Fi", 4)]
    assert api.decode_interface_list(interface_list_bytes([])) == []


@pytest.mark.parametrize("buf", [b"", b"\x01\x00", (1).to_bytes(4, "little") + b"\x00" * 100,
                                 (1000).to_bytes(4, "little") + b"\x00" * 50])
def test_a_damaged_interface_list_is_an_error(buf):
    with pytest.raises(WifiError):
        api.decode_interface_list(buf)


# ------------------------------------------------------------------ collect() with a fake API
class FakeApi:
    def __init__(self, interfaces, answers=None, open_error=None):
        self._interfaces = interfaces
        self.answers = answers or {}
        self.open_error = open_error
        self.queries = []
        self.closed = False

    def open(self):
        if self.open_error:
            raise self.open_error
        return "handle"

    def close(self, handle):
        assert handle == "handle"
        self.closed = True

    def interfaces(self, handle):
        return self._interfaces

    def query(self, handle, guid, opcode):
        self.queries.append(opcode)
        return self.answers.get(opcode)


GUID = b"\x01" * 16


def rssi_bytes(value):
    return value.to_bytes(4, "little", signed=True)


def test_a_connected_adapter_reports_link_figures_rssi_and_channel():
    fake = FakeApi([(GUID, "Intel Wi-Fi", 1)], {
        api.OPCODE_CURRENT_CONNECTION: connection_bytes(),
        api.OPCODE_RSSI: rssi_bytes(-52), api.OPCODE_CHANNEL_NUMBER: (36).to_bytes(4, "little"),
    })
    (reading,) = api.collect(fake)
    assert reading == {
        "state": "connected", "interface": "Intel Wi-Fi", "ssid": "HomeWiFi", "signal_pct": 87,
        "rx_mbps": 866.7, "tx_mbps": 780.0, "radio_type": "802.11ax", "rssi_dbm": -52,
        "rssi_source": api.RSSI_SOURCE, "channel": 36,
    }
    assert fake.closed


def test_an_adapter_that_does_not_report_rssi_or_channel_simply_has_none():
    fake = FakeApi([(GUID, "Wi-Fi", 1)], {api.OPCODE_CURRENT_CONNECTION: connection_bytes()})
    (reading,) = api.collect(fake)
    assert "rssi_dbm" not in reading and "channel" not in reading
    assert reading["signal_pct"] == 87  # and the percentage is NOT turned into an RSSI


@pytest.mark.parametrize("code, state", [
    (0, "not_ready"), (3, "disconnecting"), (4, "disconnected"), (5, "connecting"),
    (6, "connecting"), (7, "connecting"), (2, "unknown"), (99, "unknown"),
])
def test_other_states_are_reported_without_any_further_query(code, state):
    fake = FakeApi([(GUID, "Wi-Fi", code)])
    (reading,) = api.collect(fake)
    assert reading == {"state": state, "interface": "Wi-Fi"} and fake.queries == []


def test_no_interfaces_gives_an_empty_list_and_the_handle_is_closed():
    fake = FakeApi([])
    assert api.collect(fake) == [] and fake.closed


def test_the_handle_is_closed_even_when_decoding_fails():
    fake = FakeApi([(GUID, "Wi-Fi", 1)], {api.OPCODE_CURRENT_CONNECTION: b"\x00"})
    with pytest.raises(WifiError):
        api.collect(fake)
    assert fake.closed


def test_a_service_that_cannot_be_opened_propagates_its_category():
    fake = FakeApi([], open_error=WifiError("unavailable"))
    with pytest.raises(WifiError) as caught:
        api.collect(fake)
    assert caught.value.category == "unavailable" and not fake.closed


def test_only_read_only_query_opcodes_are_ever_used():
    fake = FakeApi([(GUID, "Wi-Fi", 1)], {api.OPCODE_CURRENT_CONNECTION: connection_bytes()})
    api.collect(fake)
    assert set(fake.queries) <= {7, 8, 0x10000102}  # connection, channel, RSSI: no scanning


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_the_real_api_refuses_to_load_off_windows():
    with pytest.raises(WifiError) as caught:
        api.CtypesWlanApi()
    assert caught.value.category == "unsupported_os"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows behaviour")
def test_the_helper_prints_one_json_line_with_a_category_off_windows(capsys):
    assert api.main() == 0
    assert json.loads(capsys.readouterr().out) == {"ok": False, "category": "unsupported_os"}


# ------------------------------------------------------------------ WindowsWifiSource
CONNECTED_NETSH = (
    "There is 1 interface on the system:\n\n    Name : Wi-Fi\n    State : connected\n"
    "    SSID : NetshNet\n    Radio type : 802.11n\n    Channel : 6\n"
    "    Receive rate (Mbps) : 150\n    Transmit rate (Mbps) : 150\n    Signal : 64%\n"
)


class Runner:
    """Answers each command with the next scripted result (an exception is raised)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append((argv, timeout_s))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def helper_output(**payload):
    return CommandResult(0, (json.dumps(payload) + "\n").encode())


def source(runner, backend="auto", platform="win32", netsh_path="C:/netsh.exe"):
    return WindowsWifiSource(WifiConfig(backend=backend), platform=platform, runner=runner,
                             python="python-exe", netsh_path=netsh_path)


def run(src):
    return asyncio.run(src.read())


def failure_category(src):
    with pytest.raises(WifiError) as caught:
        run(src)
    return caught.value.category


@pytest.mark.parametrize("platform", ["linux", "darwin", "cygwin"])
def test_other_operating_systems_are_unsupported_and_nothing_is_run(platform):
    runner = Runner()
    assert failure_category(source(runner, platform=platform)) == "unsupported_os"
    assert runner.calls == []


def test_the_native_helper_result_becomes_a_reading_marked_as_wlanapi():
    runner = Runner(helper_output(ok=True, interfaces=[
        {"state": "connected", "interface": "Wi-Fi", "ssid": "Home", "signal_pct": 71,
         "rssi_dbm": -60, "rssi_source": "driver", "rx_mbps": 300.0, "tx_mbps": 200.0,
         "radio_type": "802.11ac", "channel": 44}]))
    r = run(source(runner))
    assert (r.state, r.ssid, r.signal_pct, r.rssi_dbm, r.channel) == ("connected", "Home", 71,
                                                                       -60, 44)
    assert r.source == "wlanapi"
    argv, timeout = runner.calls[0]
    assert argv == ["python-exe", "-m", "netmonlt.wifi_wlanapi"] and timeout <= 10


def test_the_helper_output_is_validated_not_trusted():
    runner = Runner(helper_output(ok=True, interfaces=[
        {"state": "connected", "ssid": "Ho\x00me", "signal_pct": 500, "rssi_dbm": 99,
         "rssi_source": "x", "rx_mbps": -1, "channel": "six", "radio_type": "<script>"}]))
    r = run(source(runner))
    assert r.signal_pct is None and r.rssi_dbm is None and r.rssi_source is None
    assert r.rx_mbps is None and r.channel is None and r.radio_type is None
    assert r.ssid == "Home"


def test_the_connected_interface_is_preferred_over_the_first():
    runner = Runner(helper_output(ok=True, interfaces=[
        {"state": "disconnected", "interface": "USB"},
        {"state": "connected", "interface": "Built-in", "signal_pct": 50}]))
    assert run(source(runner)).interface == "Built-in"


@pytest.mark.parametrize("category", ["no_adapter", "unavailable", "unsupported_os"])
def test_a_clear_helper_answer_is_reported_and_not_retried_with_netsh(category):
    runner = Runner(helper_output(ok=False, category=category))
    assert failure_category(source(runner)) == category
    assert len(runner.calls) == 1


def test_no_interfaces_means_no_adapter():
    assert failure_category(source(Runner(helper_output(ok=True, interfaces=[])))) == "no_adapter"


@pytest.mark.parametrize("output", [
    CommandResult(0, b""), CommandResult(0, b"not json"), CommandResult(1, b"Traceback"),
    CommandResult(0, b"[1, 2]"), CommandResult(0, b'{"ok": true}'),
    CommandResult(0, b'{"ok": true, "interfaces": "x"}'),
    CommandResult(0, b'{"ok": true, "interfaces": [5]}'),
])
def test_a_broken_helper_is_an_error_category_and_then_netsh_is_tried(output):
    runner = Runner(output, CommandResult(0, CONNECTED_NETSH.encode()))
    r = run(source(runner))  # auto mode falls back
    assert r.source == "netsh" and r.ssid == "NetshNet"


def test_an_unknown_helper_category_is_not_echoed():
    runner = Runner(helper_output(ok=False, category="C:/secret/path.py exploded"),
                    CommandResult(0, b"There is no wireless interface on the system.\n"))
    assert failure_category(source(runner)) == "no_adapter"  # came from the netsh fallback


def test_the_wlanapi_only_backend_never_falls_back():
    runner = Runner(CommandResult(0, b"garbage"))
    assert failure_category(source(runner, backend="wlanapi")) == "error"
    assert len(runner.calls) == 1


def test_the_netsh_only_backend_never_starts_the_helper():
    runner = Runner(CommandResult(0, CONNECTED_NETSH.encode()))
    assert run(source(runner, backend="netsh")).signal_pct == 64
    argv, _ = runner.calls[0]
    assert argv == ["C:/netsh.exe", "wlan", "show", "interfaces"]


def test_netsh_output_in_another_language_is_a_parse_error():
    german = "    Name : WLAN\n    Status : Verbunden\n    Signal : 80%\n"
    runner = Runner(CommandResult(0, german.encode()))
    assert failure_category(source(runner, backend="netsh")) == "parse_error"


@pytest.mark.parametrize("exc, category", [
    (subprocess.TimeoutExpired("x", 8), "timeout"), (FileNotFoundError("x"), "unavailable"),
    (PermissionError("x"), "unavailable"),
])
def test_timeouts_and_missing_programs_are_safe_categories(exc, category):
    assert failure_category(source(Runner(exc), backend="wlanapi")) == category
    assert failure_category(source(Runner(exc), backend="netsh")) == category


def test_netsh_missing_from_the_system_is_unavailable(monkeypatch):
    monkeypatch.setattr("netmonlt.wifi_windows.shutil.which", lambda name: None)
    monkeypatch.delenv("SystemRoot", raising=False)
    runner = Runner()
    assert failure_category(source(runner, backend="netsh", netsh_path=None)) == "unavailable"
    assert runner.calls == []


def test_a_netsh_failure_with_no_output_is_an_error():
    runner = Runner(CommandResult(1, b""))
    assert failure_category(source(runner, backend="netsh")) == "error"


def test_arguments_are_fixed_so_nothing_user_supplied_reaches_a_command_line():
    runner = Runner(helper_output(ok=True, interfaces=[]), CommandResult(0, b""))
    failure_category(source(runner))
    assert all(isinstance(part, str) for argv, _ in runner.calls for part in argv)
    assert runner.calls[0][0][1:] == ["-m", "netmonlt.wifi_wlanapi"]


# ------------------------------------------------------------------ the real command runner
def test_run_command_captures_output():
    result = run_command([sys.executable, "-c", "print('hello')"], 20)
    assert result.returncode == 0 and result.stdout.strip() == b"hello"


def test_the_helper_can_import_the_package_without_adding_the_current_directory(monkeypatch):
    for existing in (None, "", "/somewhere"):
        if existing is None:
            monkeypatch.delenv("PYTHONPATH", raising=False)
        else:
            monkeypatch.setenv("PYTHONPATH", existing)
        code = "import os, sys; sys.stdout.write(os.environ['PYTHONPATH'])"
        value = run_command([sys.executable, "-c", code], 20).stdout.decode()
        entries = value.split(os.pathsep)
        assert "" not in entries and "." not in entries  # never the current directory
        assert entries[0] == str(Path(netmonlt.__file__).resolve().parents[1])  # the package root
        assert ("/somewhere" in entries) == (existing == "/somewhere")


def test_a_hung_command_is_killed_at_the_timeout():
    import time

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_command([sys.executable, "-c", "import time; time.sleep(60)"], 0.5)
    assert time.monotonic() - started < 15


def test_output_is_capped():
    code = "import sys; sys.stdout.write('x' * 1_000_000)"
    assert len(run_command([sys.executable, "-c", code], 20).stdout) == 256 * 1024


def test_the_real_helper_process_gives_an_honest_answer_on_any_machine():
    """Runs `python -m netmonlt.wifi_wlanapi` for real. Linux: not supported. A Windows machine
    with no Wi-Fi: no adapter or service unavailable. With Wi-Fi: a reading."""
    src = WindowsWifiSource(WifiConfig(backend="wlanapi"), platform="win32")
    try:
        reading = run(src)
    except WifiError as exc:
        assert exc.category in {"unsupported_os", "unavailable", "no_adapter"}
    else:
        assert reading.source == "wlanapi" and reading.state in {
            "connected", "disconnected", "connecting", "disconnecting", "not_ready", "unknown"}
