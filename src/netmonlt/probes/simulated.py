"""Deterministic fake devices, so the app can be demonstrated with no network at all."""

import random
import time
from collections.abc import Callable

from netmonlt.domain import Device, PingResult


class SimulatedProber:
    """Generates ping results from each device's SimProfile.

    Each device has its own seeded RNG, so a given seed and clock yield identical output.
    The clock is injectable so tests (and scripted outages) don't depend on real time.
    """

    def __init__(self, seed: int = 0, clock: Callable[[], float] = time.monotonic):
        self._seed = seed
        self._clock = clock
        self._started = clock()
        self._rngs: dict[str, random.Random] = {}

    async def ping(self, device: Device, count: int = 3) -> PingResult:
        profile = device.sim
        rng = self._rngs.setdefault(device.name, random.Random(f"{self._seed}:{device.name}"))
        elapsed = self._clock() - self._started
        start = profile.outage_start_s
        in_outage = start is not None and start <= elapsed < start + profile.outage_duration_s

        rtts: list[float] = []
        for _ in range(count):
            # Always draw both values so the random stream doesn't depend on outcomes.
            dropped = rng.random() < profile.loss
            rtt = max(0.1, rng.gauss(profile.rtt_ms, profile.jitter_ms))
            if not (in_outage or dropped):
                rtts.append(rtt)

        if not rtts:
            return PingResult(count, 0, None, None, None)
        return PingResult(
            sent=count,
            received=len(rtts),
            rtt_min_ms=round(min(rtts), 2),
            rtt_avg_ms=round(sum(rtts) / len(rtts), 2),
            rtt_max_ms=round(max(rtts), 2),
        )
