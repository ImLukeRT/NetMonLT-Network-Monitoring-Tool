"""SNMP interface polling: the client interface, errors, credentials and the simulated client.

The poller only knows the `SnmpClient` protocol. `SimulatedSnmpClient` (here) works offline for
the demo; the real SNMPv3 client lives in `snmp_pysnmp.py` and needs the optional `snmp` extra.

Security rules this module enforces:
  * credentials are read from environment variables, never from the inventory file;
  * only the variable *names* ever appear in messages, and no value is ever logged or stored;
  * an SnmpError carries a fixed, safe message per category, never raw library error text.
"""

import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from netmonlt.domain import Device, SimInterface, SnmpConfig
from netmonlt.ifstats import InterfaceReading, InterfaceSnapshot

# Why a poll failed. Stable codes, shown in the API and the dashboard.
CATEGORY_TIMEOUT = "timeout"
CATEGORY_AUTH = "auth_failure"
CATEGORY_UNSUPPORTED = "unsupported"
CATEGORY_CONFIG = "config"
CATEGORY_ERROR = "error"

SNMP_MESSAGES = {
    CATEGORY_TIMEOUT: (
        "no response before the timeout (device or SNMP service unreachable, or a wrong "
        "privacy key, which a device answers with silence)"
    ),
    CATEGORY_AUTH: "authentication failed (check the user name, authentication key and protocol)",
    CATEGORY_UNSUPPORTED: "the device did not return IF-MIB interface data",
    CATEGORY_CONFIG: "SNMP is not usable here (missing optional dependency or settings)",
    CATEGORY_ERROR: "unexpected SNMP error",
}

MIN_KEY_LENGTH = 8  # SNMPv3 (RFC 3414) requires pass-phrases of at least 8 characters


class SnmpError(Exception):
    """A poll could not be completed. `message` is safe to show, store and log."""

    def __init__(self, category: str):
        self.category = category
        self.message = SNMP_MESSAGES.get(category, SNMP_MESSAGES[CATEGORY_ERROR])
        super().__init__(f"{category}: {self.message}")


class SnmpConfigError(Exception):
    """SNMP is enabled but can't be set up (missing environment variables, short keys...).

    The message names environment variables, never their values.
    """


class SnmpClient(Protocol):
    async def poll(self, device: Device) -> InterfaceSnapshot: ...


# ------------------------------------------------------------------ credentials
@dataclass(frozen=True)
class SnmpCredentials:
    """SNMPv3 user name and keys. Never printed: repr() and str() show nothing sensitive."""

    user: str
    auth_key: str | None = None
    priv_key: str | None = None

    def __repr__(self) -> str:
        return "SnmpCredentials(<hidden>)"

    __str__ = __repr__


def credential_env_names(config: SnmpConfig) -> list[str]:
    """The environment variables a device's credentials are read from."""
    names = [f"{config.credentials_env}_USER", f"{config.credentials_env}_AUTH_KEY"]
    if config.security_level == "authPriv":
        names.append(f"{config.credentials_env}_PRIV_KEY")
    return names


def load_credentials(
    config: SnmpConfig, environ: Mapping[str, str] | None = None
) -> SnmpCredentials:
    """Read a device's SNMPv3 credentials from the environment, or explain what is missing."""
    env = os.environ if environ is None else environ
    names = credential_env_names(config)
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise SnmpConfigError(f"missing environment variable(s): {', '.join(missing)}")
    prefix = config.credentials_env
    short = [
        name for name in names
        if name.endswith("_KEY") and len(env[name]) < MIN_KEY_LENGTH
    ]
    if short:
        raise SnmpConfigError(
            f"{', '.join(short)} too short: SNMPv3 keys must be at least {MIN_KEY_LENGTH} "
            "characters"
        )
    return SnmpCredentials(
        user=env[f"{prefix}_USER"],
        auth_key=env[f"{prefix}_AUTH_KEY"],
        priv_key=env.get(f"{prefix}_PRIV_KEY") if config.security_level == "authPriv" else None,
    )


# ------------------------------------------------------------------ simulated client
_SIM_UPTIME_AT_START_S = 3 * 86400  # a simulated device that has been up for three days
_SIM_OCTETS_AT_START = 5 * 10**9  # and has already carried some traffic
_JITTER = 0.25  # traffic swings +/-25% around its average ...
_PERIOD_S = 60.0  # ... once a minute
_OMEGA = 2 * math.pi / _PERIOD_S


def _bytes_carried(mbps: float, t: float) -> float:
    """Bytes carried in the first t seconds at an average of `mbps`, with a gentle wave.

    This is the exact integral of rate(t) = r * (1 + _JITTER * sin(w t)), so counters built from
    it are deterministic, strictly increasing, and need no random state.
    """
    rate = mbps * 1_000_000 / 8
    return rate * (t + _JITTER * (1 - math.cos(_OMEGA * t)) / _OMEGA)


class SimulatedSnmpClient:
    """Deterministic fake interface counters, so SNMP statistics demo with no hardware.

    Counters are a pure function of the elapsed time, and scripted events from the device's
    `sim` profile are honoured: the ICMP outage makes SNMP time out too, `reboot_at_s` resets
    every counter and sysUpTime, and each interface may roll its 64-bit counters over
    (`wrap_at_s`) or vanish (`disappears_at_s`).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started = clock()

    async def poll(self, device: Device) -> InterfaceSnapshot:
        profile = device.sim
        if not profile.interfaces:
            raise SnmpError(CATEGORY_UNSUPPORTED)
        t = self._clock() - self._started
        start = profile.outage_start_s
        if start is not None and start <= t < start + profile.outage_duration_s:
            raise SnmpError(CATEGORY_TIMEOUT)  # the whole device is unreachable

        reboot = profile.reboot_at_s
        rebooted = reboot is not None and t >= reboot
        age = t - reboot if rebooted else t  # time since the counters last started from zero
        uptime_s = age if rebooted else _SIM_UPTIME_AT_START_S + t
        readings = [
            self._reading(index, iface, t, age, rebooted)
            for index, iface in enumerate(profile.interfaces, start=1)
            if iface.disappears_at_s is None or t < iface.disappears_at_s
        ]
        return InterfaceSnapshot(
            readings=tuple(readings), uptime_ticks=int(uptime_s * 100) % (1 << 32)
        )

    @staticmethod
    def _reading(
        index: int, iface: SimInterface, t: float, age: float, rebooted: bool
    ) -> InterfaceReading:
        up = iface.status == "up"

        def octets(mbps: float) -> int:
            if rebooted:
                offset = 0
            elif iface.wrap_at_s is not None:  # arrange for the counter to hit 2**64 at wrap_at_s
                offset = (1 << 64) - int(_bytes_carried(mbps, iface.wrap_at_s))
            else:
                offset = _SIM_OCTETS_AT_START
            return (offset + (int(_bytes_carried(mbps, age)) if up else 0)) % (1 << 64)

        def events(per_min: float) -> int:
            return int(per_min * age / 60) % (1 << 32) if up else 0

        return InterfaceReading(
            if_index=index,
            name=iface.name,
            oper_status=iface.status,
            in_octets=octets(iface.in_mbps),
            out_octets=octets(iface.out_mbps),
            in_errors=events(iface.in_errors_per_min),
            out_errors=events(iface.out_errors_per_min),
            in_discards=events(iface.in_discards_per_min),
            out_discards=events(iface.out_discards_per_min),
        )
