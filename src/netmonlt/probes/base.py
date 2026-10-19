"""The interface between the collector and anything that can measure a device.

The collector only knows this protocol. Probers return a PingResult for a device that was
measured (including "no reply"), and raise ProbeError when the measurement itself could not
be made, so the collector never records a broken prober as a device being down.
"""

from typing import Protocol

from netmonlt.domain import Device, PingResult


class ProbeError(Exception):
    """The probe could not be carried out (missing tool, no permission, refused target...)."""


class Prober(Protocol):
    async def ping(self, device: Device, count: int = 3) -> PingResult: ...
