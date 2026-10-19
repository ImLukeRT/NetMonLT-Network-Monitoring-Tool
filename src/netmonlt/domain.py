"""Core data types shared by every layer."""

from dataclasses import dataclass, field
from enum import StrEnum

# A device is "degraded" (reachable but unhealthy) past either threshold.
DEGRADED_LOSS_PCT = 20.0
DEGRADED_RTT_MS = 150.0


class Status(StrEnum):
    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


# SNMPv3 settings that may be named in the inventory. Weak algorithms (MD5, DES) and SNMPv1/v2c
# (which send a community string in clear text) are deliberately not supported.
SNMP_SECURITY_LEVELS = ("authPriv", "authNoPriv")
SNMP_AUTH_PROTOCOLS = ("SHA", "SHA224", "SHA256", "SHA384", "SHA512")
SNMP_PRIV_PROTOCOLS = ("AES128", "AES192", "AES256")


@dataclass(frozen=True)
class SimInterface:
    """A simulated network interface, reported by the simulated SNMP client."""

    name: str
    in_mbps: float = 0.0  # average inbound traffic, megabits per second
    out_mbps: float = 0.0
    status: str = "up"  # "up" or "down"
    in_errors_per_min: float = 0.0
    out_errors_per_min: float = 0.0
    in_discards_per_min: float = 0.0
    out_discards_per_min: float = 0.0
    wrap_at_s: float | None = None  # the 64-bit octet counters roll over this long after start
    disappears_at_s: float | None = None  # the interface vanishes this long after start


@dataclass(frozen=True)
class SimProfile:
    """How a simulated device behaves. Ignored by real probers."""

    rtt_ms: float = 10.0
    jitter_ms: float = 1.0
    loss: float = 0.0  # fraction of packets dropped, 0.0-1.0
    outage_start_s: float | None = None  # seconds after the simulation starts
    outage_duration_s: float = 0.0
    interfaces: tuple[SimInterface, ...] = ()  # simulated SNMP interfaces; none = no SNMP
    reboot_at_s: float | None = None  # all counters and the uptime restart at this time


@dataclass(frozen=True)
class SnmpConfig:
    """SNMPv3 polling settings for one real device. Holds no secrets, only where to find them.

    The user name and keys are read from environment variables named
    `<credentials_env>_USER`, `_AUTH_KEY` and `_PRIV_KEY`.
    """

    credentials_env: str
    security_level: str = "authPriv"
    auth_protocol: str = "SHA256"
    priv_protocol: str = "AES128"
    port: int = 161
    timeout_s: float = 5.0  # per request
    retries: int = 1


@dataclass(frozen=True)
class Device:
    name: str
    address: str
    type: str = "host"
    sim: SimProfile = field(default_factory=SimProfile)
    # Real probers only measure devices explicitly marked authorized in the inventory.
    authorized: bool = False
    # Real SNMP polling; None unless the device explicitly enables it (never the default).
    snmp: SnmpConfig | None = None


@dataclass(frozen=True)
class PingResult:
    sent: int
    received: int
    rtt_min_ms: float | None
    rtt_avg_ms: float | None
    rtt_max_ms: float | None

    @property
    def loss_pct(self) -> float:
        return 100.0 * (self.sent - self.received) / self.sent if self.sent else 100.0


@dataclass(frozen=True)
class CheckResult:
    device: str
    ts: float  # unix epoch seconds, UTC
    status: Status
    ping: PingResult


def classify(ping: PingResult) -> Status:
    if ping.received == 0:
        return Status.DOWN
    if ping.loss_pct >= DEGRADED_LOSS_PCT:
        return Status.DEGRADED
    if ping.rtt_avg_ms is not None and ping.rtt_avg_ms >= DEGRADED_RTT_MS:
        return Status.DEGRADED
    return Status.UP
