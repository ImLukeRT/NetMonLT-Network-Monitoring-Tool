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


@dataclass(frozen=True)
class SimProfile:
    """How a simulated device behaves. Ignored by real probers."""

    rtt_ms: float = 10.0
    jitter_ms: float = 1.0
    loss: float = 0.0  # fraction of packets dropped, 0.0-1.0
    outage_start_s: float | None = None  # seconds after the simulation starts
    outage_duration_s: float = 0.0


@dataclass(frozen=True)
class Device:
    name: str
    address: str
    type: str = "host"
    sim: SimProfile = field(default_factory=SimProfile)
    # Real probers only measure devices explicitly marked authorized in the inventory.
    authorized: bool = False


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
