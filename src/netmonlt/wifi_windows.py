"""The real Wi-Fi source for Windows: the native WLAN API (preferred) or `netsh wlan`.

Both backends run as separate short-lived processes with a hard timeout, so a hung or crashing
Windows component can only end that process. Nothing here scans, connects or changes anything:
the commands are read-only queries about the adapter that already exists, run without a shell.

  * wlanapi: `python -m netmonlt.wifi_wlanapi` (ctypes, language independent). Can report a
    driver-supplied RSSI in dBm, the channel and the radio type.
  * netsh: `netsh wlan show interfaces` (text, English labels only; see parse_netsh_interfaces).
  * auto (default): wlanapi first; netsh only if the wlanapi helper itself failed.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from netmonlt.domain import WifiConfig
from netmonlt.wifi import (
    CATEGORY_ERROR,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNSUPPORTED_OS,
    SOURCE_WLANAPI,
    WIFI_MESSAGES,
    WifiError,
    WifiReading,
    choose_reading,
    parse_netsh_interfaces,
    reading_from_mapping,
)

WLANAPI_TIMEOUT_S = 8.0
NETSH_TIMEOUT_S = 8.0
MAX_OUTPUT_BYTES = 256 * 1024  # a runaway tool cannot make us read forever
MAX_INTERFACES = 16
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


Runner = Callable[[list[str], float], CommandResult]


def run_command(argv: list[str], timeout_s: float) -> CommandResult:
    """Run a command with no shell, no console window and a hard timeout (the child is killed).

    Raises subprocess.TimeoutExpired or OSError; the caller maps both to safe categories.
    """
    env = dict(os.environ)
    # The helper is run as `-m netmonlt.wifi_wlanapi`, so it must be able to import this package
    # even when it was started from a source checkout that is not installed.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = _PACKAGE_ROOT + (os.pathsep + existing if existing else "")  # no "" entry
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-supplied text
        argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout_s, env=env,
        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return CommandResult(proc.returncode, proc.stdout[:MAX_OUTPUT_BYTES])


def decode_console_output(raw: bytes) -> str:
    """netsh writes the console's OEM code page when piped; fall back to UTF-8 elsewhere."""
    for encoding in ("oem", "utf-8"):
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            continue
    return raw.decode("latin-1", errors="replace")


class WindowsWifiSource:
    """Reads the local adapter on Windows. Raises WifiError with a safe category on any failure."""

    def __init__(
        self, config: WifiConfig, *, platform: str = sys.platform, runner: Runner = run_command,
        python: str | None = None, netsh_path: str | None = None,
    ):
        self._backend = config.backend
        self._platform = platform
        self._runner = runner
        self._python = python or sys.executable
        self._netsh_path = netsh_path
        # A small dedicated pool: queries never wait behind (or starve) other work.
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="netmonlt-wifi")

    async def read(self) -> WifiReading:
        if self._platform != "win32":
            raise WifiError(CATEGORY_UNSUPPORTED_OS)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._read_blocking)

    def _read_blocking(self) -> WifiReading:
        if self._backend == "netsh":
            return self._via_netsh()
        if self._backend == "wlanapi":
            return self._via_wlanapi()
        try:
            return self._via_wlanapi()
        except WifiError as exc:
            if exc.category != CATEGORY_ERROR:  # a clear answer (no adapter, service off...)
                raise
        return self._via_netsh()

    # ---- native API, in a child process
    def _via_wlanapi(self) -> WifiReading:
        argv = [self._python, "-m", "netmonlt.wifi_wlanapi"]
        result = self._run(argv, WLANAPI_TIMEOUT_S)
        try:
            payload = json.loads(result.stdout.decode("utf-8", errors="replace").strip() or "x")
        except ValueError:
            raise WifiError(CATEGORY_ERROR) from None
        if not isinstance(payload, dict):
            raise WifiError(CATEGORY_ERROR)
        if payload.get("ok") is not True:
            category = payload.get("category")
            raise WifiError(category if category in WIFI_MESSAGES else CATEGORY_ERROR)
        interfaces = payload.get("interfaces")
        if not isinstance(interfaces, list):
            raise WifiError(CATEGORY_ERROR)
        return choose_reading(
            [reading_from_mapping(item, SOURCE_WLANAPI) for item in interfaces[:MAX_INTERFACES]]
        )

    # ---- netsh text
    def _via_netsh(self) -> WifiReading:
        path = self._netsh_path or shutil.which("netsh")
        if not path:
            system_root = os.environ.get("SystemRoot")
            candidate = Path(system_root, "System32", "netsh.exe") if system_root else None
            path = str(candidate) if candidate and candidate.is_file() else None
        if not path:
            raise WifiError(CATEGORY_UNAVAILABLE)
        result = self._run([path, "wlan", "show", "interfaces"], NETSH_TIMEOUT_S)
        text = decode_console_output(result.stdout)
        if result.returncode != 0 and not text.strip():
            raise WifiError(CATEGORY_ERROR)
        return choose_reading(parse_netsh_interfaces(text)[:MAX_INTERFACES])

    def _run(self, argv: list[str], timeout_s: float) -> CommandResult:
        try:
            return self._runner(argv, timeout_s)
        except subprocess.TimeoutExpired:
            raise WifiError(CATEGORY_TIMEOUT) from None
        except OSError:
            raise WifiError(CATEGORY_UNAVAILABLE) from None

