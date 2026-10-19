"""Real ICMP echo ("ping") measurements, made by running the operating system's `ping` command.

Why not raw sockets? Sending ICMP from Python needs elevated rights (administrator on Windows;
root or CAP_NET_RAW on Linux). The system `ping` tool is already privileged on every major OS
(setuid or file capabilities), so this works for a normal user with no extra dependency.
The costs: its text output has to be parsed, and Windows only reports whole milliseconds.

Safety rules are enforced here, not just when the inventory is loaded, because this is the only
code that touches a network:
  * only devices explicitly marked `authorized: true` are probed;
  * a target must be one IPv4 address or one hostname: no CIDR ranges, wildcards, lists,
    multicast/broadcast/unspecified addresses, or anything that ping could read as an option;
  * the command is run without a shell, with the address as a single argument.
"""

import asyncio
import ipaddress
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from netmonlt.domain import Device, PingResult
from netmonlt.probes.base import ProbeError

_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)

# One reply line: Linux/macOS "ttl=64 time=0.5 ms", Windows "time<1ms TTL=128".
_RTT_RE = re.compile(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
# Output of a ping that ran fine but got no (or only error) replies.
_NO_REPLY_RE = re.compile(
    r"100(?:\.0)?% packet loss|\b0 (?:packets )?received|received = 0|100% loss"
    r"|timed out|unreachable",
    re.IGNORECASE,
)
# Each check runs one `ping` process (about 2-3 s). A dedicated pool, not asyncio's small default
# one (a few threads on a laptop), keeps devices from queueing behind each other and tripping
# the collector's per-probe timeout; the cap also bounds how many pings run at once.
MAX_CONCURRENT_PINGS = 64

_NUMERIC_PART_RE = re.compile(r"\d+|0[xX][0-9a-fA-F]+")
_RESOLVE_HINTS = (
    "name or service not known",
    "temporary failure in name resolution",
    "could not find host",
    "cannot resolve",
    "unknown host",
    "nodename nor servname",
)
_PERMISSION_HINTS = ("not permitted", "permission denied", "lacking privilege", "access is denied")
_SUB_MS_FACTOR = 0.5  # Windows "time<1ms": report half a millisecond


class TargetError(ValueError):
    """The address is not a single, acceptable monitoring target."""


def validate_target(address: str) -> str:
    """Return the address if it is one IPv4 address or one hostname, else raise TargetError."""
    if not isinstance(address, str) or not address:
        raise TargetError("address is empty")
    if "/" in address:
        raise TargetError("CIDR ranges are not allowed; list each device individually")
    if re.search(r"[\s,*?]", address):
        raise TargetError("wildcards, lists and ranges are not allowed; use one host per device")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version != 4:
            raise TargetError("only IPv4 addresses are supported")
        if ip.is_multicast or ip.is_unspecified or ip == ipaddress.IPv4Address("255.255.255.255"):
            raise TargetError("multicast, broadcast and unspecified addresses are not allowed")
        return address
    # ping resolves legacy shorthand (inet_aton) such as 0x7f.1 or 0xe0000001 as an IPv4 address,
    # which would bypass the multicast/broadcast check above. A real hostname never has an
    # all-numeric last label, so anything made only of numeric parts is rejected.
    parts = address.split(".")
    if re.fullmatch(r"[0-9.\-]+", address) or all(_NUMERIC_PART_RE.fullmatch(p) for p in parts):
        raise TargetError("not a valid IPv4 address (ranges and shorthand forms are not allowed)")
    if not _HOSTNAME_RE.fullmatch(address):
        raise TargetError("not a valid IPv4 address or hostname")
    return address


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: str


Runner = Callable[[list[str], float], RunResult]


def run_subprocess(argv: list[str], timeout_s: float) -> RunResult:
    """Run a command (no shell) and return its combined output. Raises on exec errors/timeout."""
    # LC_ALL=C forces English output on Linux/macOS so parsing doesn't depend on the locale.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    proc = subprocess.run(  # noqa: S603 - argv is a list; the address was validated
        argv,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout_s,
        env=env,
        check=False,
    )
    text = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", "replace")
    return RunResult(proc.returncode, text)


def interpret_ping_output(output: str, sent: int) -> PingResult:
    """Turn ping's text output into a PingResult.

    Replies are counted from per-packet reply lines (those containing "ttl="), which is more
    portable than the summary line. Raises ProbeError if the output isn't recognisable, so an
    unknown failure is never reported as "device down".
    """
    rtts: list[float] = []
    replies = 0
    for line in output.splitlines():
        # Error lines such as Windows "Reply from <router>: Destination host unreachable" or
        # "TTL expired in transit" carry no "ttl=", so they are not counted as replies.
        if "ttl=" not in line.lower() or "dup!" in line.lower():
            continue
        replies += 1
        match = _RTT_RE.search(line)
        if match:
            value = float(match.group(2).replace(",", "."))
            rtts.append(value * _SUB_MS_FACTOR if match.group(1) == "<" else value)

    if replies:
        received = min(replies, sent)
        rtts = rtts[:received]
        if not rtts:  # replies seen but no parsable time (e.g. a non-English Windows)
            return PingResult(sent, received, None, None, None)
        return PingResult(
            sent=sent,
            received=received,
            rtt_min_ms=round(min(rtts), 2),
            rtt_avg_ms=round(sum(rtts) / len(rtts), 2),
            rtt_max_ms=round(max(rtts), 2),
        )

    lower = output.lower()
    if "general failure" in lower:
        raise ProbeError("ping reported a general failure (is a network adapter available?)")
    if any(hint in lower for hint in _RESOLVE_HINTS):
        raise ProbeError(f"cannot resolve host name: {_first_line(output)}")
    if any(hint in lower for hint in _PERMISSION_HINTS):
        raise ProbeError(f"ping is not permitted to send ICMP here: {_first_line(output)}")
    if _NO_REPLY_RE.search(output):
        return PingResult(sent, 0, None, None, None)
    raise ProbeError(f"unrecognised ping output: {_first_line(output)}")


def _first_line(output: str) -> str:
    for line in output.splitlines():
        if line.strip():
            return line.strip()[:200]
    return "(no output)"


class IcmpProber:
    """Measures real devices with the system `ping` command. See the module docstring."""

    def __init__(
        self,
        *,
        timeout_s: float = 1.0,
        ping_path: str | None = None,
        platform: str = sys.platform,
        runner: Runner = run_subprocess,
    ):
        self._timeout_s = timeout_s
        self._ping_path = ping_path
        self._platform = platform
        self._runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_PINGS, thread_name_prefix="netmonlt-ping"
        )

    async def ping(self, device: Device, count: int = 3) -> PingResult:
        if not device.authorized:
            raise ProbeError(
                f"refusing to probe {device.name!r}: it is not marked 'authorized: true'"
            )
        # ping blocks for up to a few seconds; run it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._measure, device.address, count)

    def preflight(self) -> None:
        """Check that ping exists and works by pinging this machine (loopback) once.

        Raises ProbeError with a user-facing message if real monitoring can't work here.
        """
        try:
            result = self._measure("127.0.0.1", 1)
        except ProbeError as exc:
            raise ProbeError(f"ICMP self-test failed: {exc}") from None
        if result.received == 0:
            raise ProbeError(
                "ICMP self-test failed: ping got no reply from 127.0.0.1 (loopback); "
                "check that a firewall is not blocking ICMP on this machine"
            )

    def _resolve_ping(self) -> str:
        path = self._ping_path or shutil.which("ping")
        if not path:
            raise ProbeError(
                "the 'ping' command was not found on PATH. Windows and macOS include it; on "
                "Linux install it (for example: sudo apt install iputils-ping)"
            )
        return path

    def _command(self, ping_path: str, address: str, count: int) -> list[str]:
        ms = str(max(1, int(self._timeout_s * 1000)))
        if self._platform.startswith("win"):
            return [ping_path, "-n", str(count), "-w", ms, address]
        if self._platform == "darwin" or "bsd" in self._platform:
            return [ping_path, "-n", "-c", str(count), "-W", ms, address]
        seconds = str(max(1, math.ceil(self._timeout_s)))  # Linux -W is whole seconds
        return [ping_path, "-n", "-c", str(count), "-W", seconds, address]

    def _measure(self, address: str, count: int) -> PingResult:
        if count < 1:
            raise ValueError("count must be at least 1")
        try:
            address = validate_target(address)
        except TargetError as exc:
            raise ProbeError(f"refusing to probe {address!r}: {exc}") from None

        argv = self._command(self._resolve_ping(), address, count)
        # Total allowance: every packet may wait the full timeout, plus headroom for name lookup.
        allowance = count * self._timeout_s + 3
        try:
            result = self._runner(argv, allowance)
        except FileNotFoundError:
            raise ProbeError("the 'ping' command could not be started (not found)") from None
        except PermissionError:
            raise ProbeError("permission denied when starting the 'ping' command") from None
        except subprocess.TimeoutExpired:
            raise ProbeError(f"ping did not finish within {allowance:g}s") from None
        except OSError as exc:
            raise ProbeError(f"could not run ping: {exc}") from None
        except Exception as exc:
            raise ProbeError(f"unexpected error running ping: {exc!r}") from exc
        return interpret_ping_output(result.output, count)
