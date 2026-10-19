"""Tests for the ICMP prober. Nothing here touches a network or runs a real `ping`.

Sample outputs marked "captured" were recorded from iputils ping 20240117 on Linux. The rest
(partial loss, macOS, Windows, error cases) are constructed from each tool's documented output
format, because those systems weren't available when the tests were written.
"""

import asyncio
import subprocess

import pytest

from netmonlt.collector import poll_once
from netmonlt.domain import Device, PingResult, Status
from netmonlt.probes import IcmpProber, ProbeError
from netmonlt.probes.icmp import RunResult, TargetError, interpret_ping_output, validate_target

# ---- captured (Linux iputils)
LINUX_OK = """\
PING 127.0.0.1 (127.0.0.1) 56(84) bytes of data.
64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=1.93 ms
64 bytes from 127.0.0.1: icmp_seq=2 ttl=64 time=0.040 ms
64 bytes from 127.0.0.1: icmp_seq=3 ttl=64 time=0.041 ms

--- 127.0.0.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2008ms
rtt min/avg/max/mdev = 0.040/0.671/1.934/0.892 ms
"""
LINUX_ALL_LOST = """\
PING 203.0.113.1 (203.0.113.1) 56(84) bytes of data.

--- 203.0.113.1 ping statistics ---
2 packets transmitted, 0 received, 100% packet loss, time 1012ms
"""
LINUX_UNKNOWN_HOST = "ping: nonexistent.invalid: Name or service not known\n"
LINUX_BAD_OPTION = "ping: invalid option -- '-'\n\nUsage\n  ping [options] <destination>\n"

# ---- constructed
LINUX_PARTIAL = """\
PING 10.0.0.5 (10.0.0.5) 56(84) bytes of data.
64 bytes from 10.0.0.5: icmp_seq=1 ttl=63 time=4.10 ms
64 bytes from 10.0.0.5: icmp_seq=3 ttl=63 time=5.90 ms

--- 10.0.0.5 ping statistics ---
3 packets transmitted, 2 received, 33% packet loss, time 2003ms
"""
LINUX_DUPLICATES = """\
64 bytes from 10.0.0.5: icmp_seq=1 ttl=63 time=4.0 ms
64 bytes from 10.0.0.5: icmp_seq=1 ttl=63 time=4.1 ms (DUP!)
64 bytes from 10.0.0.5: icmp_seq=2 ttl=63 time=4.0 ms
64 bytes from 10.0.0.5: icmp_seq=3 ttl=63 time=4.0 ms
"""
LINUX_ROUTER_UNREACHABLE = """\
PING 10.0.0.9 (10.0.0.9) 56(84) bytes of data.
From 10.0.0.1 icmp_seq=1 Destination Host Unreachable

--- 10.0.0.9 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""
LINUX_NO_ROUTE = "ping: connect: Network is unreachable\n"
LINUX_NOT_PERMITTED = "ping: socket: Operation not permitted\n"
MACOS_PARTIAL = """\
PING 192.168.1.1 (192.168.1.1): 56 data bytes
64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=2.345 ms
Request timeout for icmp_seq 1
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=2.101 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 2.101/2.223/2.345/0.122 ms
"""
MACOS_ALL_LOST = """\
PING 192.168.1.50 (192.168.1.50): 56 data bytes
Request timeout for icmp_seq 0
Request timeout for icmp_seq 1

--- 192.168.1.50 ping statistics ---
2 packets transmitted, 0 packets received, 100.0% packet loss
"""
WINDOWS_OK = """\
Pinging 192.168.1.1 with 32 bytes of data:
Reply from 192.168.1.1: bytes=32 time=2ms TTL=64
Reply from 192.168.1.1: bytes=32 time<1ms TTL=64
Reply from 192.168.1.1: bytes=32 time=3ms TTL=64

Ping statistics for 192.168.1.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 3ms, Average = 1ms
"""
WINDOWS_TIMED_OUT = """\
Pinging 192.168.1.50 with 32 bytes of data:
Request timed out.
Request timed out.
Request timed out.

Ping statistics for 192.168.1.50:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),
"""
# Windows counts a router's "unreachable" message as a received packet; it is not a reply.
WINDOWS_UNREACHABLE = """\
Pinging 192.168.1.50 with 32 bytes of data:
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.
Reply from 192.168.1.1: Destination host unreachable.

Ping statistics for 192.168.1.50:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
"""
WINDOWS_UNKNOWN_HOST = (
    "Ping request could not find host nope.invalid. Please check the name and try again.\n"
)
WINDOWS_GENERAL_FAILURE = """\
Pinging 192.168.1.1 with 32 bytes of data:
PING: transmit failed. General failure.
Ping statistics for 192.168.1.1:
    Packets: Sent = 1, Received = 0, Lost = 1 (100% loss),
"""
WINDOWS_GERMAN = (
    "Antwort von 192.168.1.1: Bytes=32 Zeit=2ms TTL=64\n"
    "Antwort von 192.168.1.1: Bytes=32 Zeit<1ms TTL=64\n"
)
WINDOWS_RUSSIAN = "Ответ от 192.168.1.1: число байт=32 время=2мс TTL=64\n"


# --------------------------------------------------------------------------- output parsing
def test_linux_all_replies():
    r = interpret_ping_output(LINUX_OK, sent=3)
    assert (r.sent, r.received) == (3, 3)
    assert (r.rtt_min_ms, r.rtt_avg_ms, r.rtt_max_ms) == (0.04, 0.67, 1.93)


def test_partial_packet_loss_is_reported():
    r = interpret_ping_output(LINUX_PARTIAL, sent=3)
    assert (r.sent, r.received, r.loss_pct) == (3, 2, pytest.approx(33.33, abs=0.01))
    assert (r.rtt_min_ms, r.rtt_avg_ms, r.rtt_max_ms) == (4.1, 5.0, 5.9)


def test_macos_partial_loss():
    r = interpret_ping_output(MACOS_PARTIAL, sent=3)
    assert (r.received, r.rtt_min_ms, r.rtt_max_ms) == (2, 2.1, 2.35)


@pytest.mark.parametrize("text", [LINUX_ALL_LOST, MACOS_ALL_LOST, WINDOWS_TIMED_OUT])
def test_no_reply_is_a_result_not_an_error(text):
    r = interpret_ping_output(text, sent=3)
    assert (r.sent, r.received) == (3, 0)
    assert r.rtt_avg_ms is None and r.loss_pct == 100


@pytest.mark.parametrize("text", [LINUX_ROUTER_UNREACHABLE, LINUX_NO_ROUTE, WINDOWS_UNREACHABLE])
def test_unreachable_counts_as_down_even_when_ping_reports_replies(text):
    assert interpret_ping_output(text, sent=3).received == 0


def test_windows_sub_millisecond_replies():
    r = interpret_ping_output(WINDOWS_OK, sent=3)
    assert (r.received, r.rtt_min_ms, r.rtt_avg_ms, r.rtt_max_ms) == (3, 0.5, 1.83, 3.0)


def test_non_english_windows_output_still_parses():
    r = interpret_ping_output(WINDOWS_GERMAN, sent=2)
    assert (r.received, r.rtt_min_ms, r.rtt_max_ms) == (2, 0.5, 2.0)


def test_unparsable_times_still_count_replies():
    # The device answered, but we can't read the time (localised "ms"): up, latency unknown.
    r = interpret_ping_output(WINDOWS_RUSSIAN, sent=1)
    assert (r.received, r.rtt_avg_ms) == (1, None)


def test_duplicate_replies_never_exceed_packets_sent():
    r = interpret_ping_output(LINUX_DUPLICATES, sent=3)
    assert r.received == 3


def test_a_duplicate_reply_does_not_hide_a_lost_packet():
    # Packet 2 was lost and packet 1 answered twice: the truth is 2 of 3, not 3 of 3.
    text = (
        "64 bytes from 10.0.0.5: icmp_seq=1 ttl=63 time=4.0 ms\n"
        "64 bytes from 10.0.0.5: icmp_seq=1 ttl=63 time=4.1 ms (DUP!)\n"
        "64 bytes from 10.0.0.5: icmp_seq=3 ttl=63 time=4.0 ms\n"
    )
    assert interpret_ping_output(text, sent=3).received == 2


def test_more_reply_lines_than_packets_sent_is_clamped():
    text = "".join(f"64 bytes from h: icmp_seq={i} ttl=63 time=4.0 ms\n" for i in range(5))
    r = interpret_ping_output(text, sent=3)
    assert (r.sent, r.received, r.loss_pct) == (3, 3, 0)


@pytest.mark.parametrize("text, message", [
    (LINUX_UNKNOWN_HOST, "cannot resolve"),
    (WINDOWS_UNKNOWN_HOST, "cannot resolve"),
    (LINUX_NOT_PERMITTED, "not permitted"),
    (WINDOWS_GENERAL_FAILURE, "general failure"),
    (LINUX_BAD_OPTION, "unrecognised ping output: ping: invalid option"),
    ("", "no output"),
    ("something completely unexpected\n", "unrecognised"),
])
def test_failures_to_measure_are_errors_not_down(text, message):
    with pytest.raises(ProbeError, match=message):
        interpret_ping_output(text, sent=3)


# --------------------------------------------------------------------------- commands built
def make(runner, platform="linux", timeout_s=1.0):
    return IcmpProber(ping_path="PING", platform=platform, runner=runner, timeout_s=timeout_s)


class Recorder:
    def __init__(self, output=LINUX_OK, returncode=0):
        self.calls, self.output, self.returncode = [], output, returncode

    def __call__(self, argv, timeout_s):
        self.calls.append((argv, timeout_s))
        return RunResult(self.returncode, self.output)


def authorized(address="192.0.2.7", name="d"):
    return Device(name, address, authorized=True)


@pytest.mark.parametrize("platform, timeout_s, expected", [
    ("linux", 1.0, ["PING", "-n", "-c", "3", "-W", "1", "192.0.2.7"]),
    ("linux", 1.5, ["PING", "-n", "-c", "3", "-W", "2", "192.0.2.7"]),  # Linux: whole seconds
    ("darwin", 1.0, ["PING", "-n", "-c", "3", "-W", "1000", "192.0.2.7"]),  # macOS: milliseconds
    ("freebsd14", 2.0, ["PING", "-n", "-c", "3", "-W", "2000", "192.0.2.7"]),
    ("win32", 1.0, ["PING", "-n", "3", "-w", "1000", "192.0.2.7"]),  # Windows: -n count, -w ms
])
def test_command_line_per_platform(platform, timeout_s, expected):
    rec = Recorder()
    asyncio.run(make(rec, platform, timeout_s).ping(authorized(), 3))
    argv, allowance = rec.calls[0]
    assert argv == expected
    assert allowance == 3 * timeout_s + 3  # bounded, and below the collector's own safety net


def test_ping_returns_the_same_result_type_as_simulation():
    result = asyncio.run(make(Recorder(LINUX_OK)).ping(authorized(), 3))
    assert isinstance(result, PingResult) and result.received == 3


# --------------------------------------------------------------------------- error handling
class Raises:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, argv, timeout_s):
        raise self.exc


@pytest.mark.parametrize("exc, message", [
    (FileNotFoundError("ping"), "not found"),
    (PermissionError("denied"), "permission denied"),
    (subprocess.TimeoutExpired("ping", 6), "did not finish within 6s"),
    (OSError("exec format error"), "could not run ping"),
    (RuntimeError("boom"), "unexpected error running ping"),
])
def test_runner_failures_become_probe_errors(exc, message):
    with pytest.raises(ProbeError, match=message):
        asyncio.run(make(Raises(exc)).ping(authorized(), 3))


def test_missing_ping_command_is_a_clear_error(monkeypatch):
    monkeypatch.setattr("netmonlt.probes.icmp.shutil.which", lambda name: None)
    prober = IcmpProber(runner=Recorder())
    with pytest.raises(ProbeError, match="'ping' command was not found on PATH"):
        asyncio.run(prober.ping(authorized(), 3))


# --------------------------------------------------------------------------- default runner
def test_default_runner_uses_no_shell_forces_english_and_decodes_bytes(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b"ttl=64 time=1 ms \xff\n", stderr=b"")

    monkeypatch.setattr("netmonlt.probes.icmp.subprocess.run", fake_run)
    result = IcmpProber(ping_path="/bin/ping", platform="linux").preflight()
    assert result is None  # the replacement output contains a reply, so the self-test passed
    assert isinstance(seen["argv"], list) and seen["argv"][-1] == "127.0.0.1"
    assert not seen.get("shell")
    assert seen["env"]["LC_ALL"] == "C" and seen["stdin"] == subprocess.DEVNULL
    assert seen["timeout"] > 0


# --------------------------------------------------------------------------- safety rules
@pytest.mark.parametrize("bad", [
    "", "-c", "--help", "-f", "10.0.0.0/24", "10.0.0.1-50", "10.0.0.*", "10.0.0.1,10.0.0.2",
    "10.0.0.1 10.0.0.2", "224.0.0.1", "255.255.255.255", "0.0.0.0", "::1", "fe80::1",
    "10.1", "2130706433", "010.0.0.1", "a;b", "$(id)", "`id`", "evil.com\n", "host name",
    "-bad.example.com", "bad-.example.com", "a" * 64 + ".example.com",
    # inet_aton shorthand that ping resolves to a real address (multicast, broadcast, loopback)
    "0xe0.0.0.1", "0xE0000001", "0xffffffff", "0x7f.1", "0x7f.0x0.0x0.0x1", "0xe0.1",
])
def test_unacceptable_targets_are_rejected_and_never_executed(bad):
    with pytest.raises(TargetError):
        validate_target(bad)
    rec = Recorder()
    with pytest.raises(ProbeError, match="refusing to probe"):
        asyncio.run(make(rec).ping(Device("d", bad, authorized=True), 3))
    assert rec.calls == []


@pytest.mark.parametrize("good", [
    "192.168.1.1", "127.0.0.1", "router.local", "my-host", "a.b.c", "0xdeadbeef.example.com",
    "1host.example.com",
])
def test_single_hosts_are_accepted(good):
    assert validate_target(good) == good


def test_unauthorized_devices_are_never_probed():
    rec = Recorder()
    with pytest.raises(ProbeError, match="not marked 'authorized: true'"):
        asyncio.run(make(rec).ping(Device("d", "192.0.2.7"), 3))
    assert rec.calls == []


# --------------------------------------------------------------------------- preflight
def test_preflight_passes_when_loopback_answers():
    rec = Recorder(LINUX_OK)
    make(rec).preflight()
    assert rec.calls[0][0][-1] == "127.0.0.1"  # only ever pings this machine


def test_preflight_fails_if_loopback_is_silent():
    with pytest.raises(ProbeError, match="self-test failed.*loopback"):
        make(Recorder(LINUX_ALL_LOST)).preflight()


def test_preflight_reports_underlying_error():
    with pytest.raises(ProbeError, match="self-test failed: .*not permitted"):
        make(Recorder(LINUX_NOT_PERMITTED, returncode=2)).preflight()


# --------------------------------------------------------------------------- concurrency
class PeakTracker:
    """Fake runner that records how many pings are running at the same moment."""

    def __init__(self, hold_s=0.3):
        import threading

        self._lock, self._hold_s = threading.Lock(), hold_s
        self.running = self.peak = 0

    def __call__(self, argv, timeout_s):
        import time

        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self._hold_s)
        with self._lock:
            self.running -= 1
        return RunResult(0, LINUX_OK)


def test_many_devices_are_probed_concurrently_not_queued_behind_a_small_pool(db):
    # With asyncio's default pool (cpu_count + 4 threads) only a few would run at once and the
    # rest would wait long enough to hit the collector's per-probe timeout.
    tracker = PeakTracker()
    devices = [authorized("192.0.2.7", f"d{i}") for i in range(60)]
    db.sync_devices(devices)
    results = asyncio.run(poll_once(devices, make(tracker), db, timeout_s=5))
    assert len(results) == 60
    assert tracker.peak == 60


def test_concurrent_pings_are_capped():
    from netmonlt.probes.icmp import MAX_CONCURRENT_PINGS

    tracker = PeakTracker(hold_s=0.05)
    prober = make(tracker)

    async def burst():
        await asyncio.gather(*(prober.ping(authorized(), 3) for _ in range(150)))

    asyncio.run(burst())
    assert tracker.peak == MAX_CONCURRENT_PINGS


# --------------------------------------------------------------------------- with the collector
def test_collector_stores_icmp_results_and_skips_probe_errors(db):
    outputs = {
        "192.0.2.1": LINUX_OK,
        "192.0.2.2": LINUX_PARTIAL,
        "192.0.2.3": LINUX_ALL_LOST,
        "192.0.2.4": LINUX_UNKNOWN_HOST,
    }

    def runner(argv, timeout_s):
        return RunResult(0, outputs[argv[-1]])

    devices = [authorized(f"192.0.2.{i}", f"d{i}") for i in range(1, 5)]
    db.sync_devices(devices)
    asyncio.run(poll_once(devices, make(runner), db))

    rows = {r["name"]: r for r in db.latest_status()}
    assert rows["d1"]["status"] == Status.UP and rows["d1"]["rtt_avg_ms"] == 0.67
    assert rows["d2"]["status"] == Status.DEGRADED and rows["d2"]["received"] == 2
    assert rows["d3"]["status"] == Status.DOWN and rows["d3"]["rtt_avg_ms"] is None
    assert rows["d4"]["status"] is None  # a lookup failure is not recorded as "down"
