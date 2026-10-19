"""The interface between the collector and anything that can measure a device.

The collector only knows this protocol. A simulated implementation exists today; a real
ICMP implementation can be added later without touching the collector, storage or API.
"""

from typing import Protocol

from netmonlt.domain import Device, PingResult


class Prober(Protocol):
    async def ping(self, device: Device, count: int = 3) -> PingResult: ...
