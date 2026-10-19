from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class HangingProber:
    """Never returns for the named devices; answers instantly for the rest."""

    def __init__(self, hang_for: set[str] | None = None):
        self.hang_for = hang_for

    async def ping(self, device, count=3):
        import asyncio

        from netmonlt.domain import PingResult

        if self.hang_for is None or device.name in self.hang_for:
            await asyncio.sleep(3600)
        return PingResult(count, count, 5.0, 5.0, 5.0)


def _pings():
    from netmonlt.domain import PingResult, Status

    return {
        Status.UP: PingResult(3, 3, 2.0, 2.0, 2.0),
        Status.DEGRADED: PingResult(3, 2, 5.0, 5.0, 5.0),  # 33% loss
        Status.DOWN: PingResult(3, 0, None, None, None),
    }


PINGS = _pings()


class ScriptedProber:
    """Answers each device's call number n with script[device][n]; None raises a ProbeError.

    The last entry repeats once the script runs out.
    """

    def __init__(self, script):
        self.script = script
        self.calls = {name: 0 for name in script}

    async def ping(self, device, count=3):
        from netmonlt.probes import ProbeError

        seq = self.script[device.name]
        status = seq[min(self.calls[device.name], len(seq) - 1)]
        self.calls[device.name] += 1
        if status is None:
            raise ProbeError("scripted probe failure")
        return PINGS[status]


def run_cycles(devices, prober, db, cycles, *, step=5.0, start=1000.0):
    """Run `cycles` polling cycles `step` seconds apart on a fake clock."""
    import asyncio

    from netmonlt.collector import poll_once

    clock = FakeClock(start)

    async def go():
        for i in range(cycles):
            clock.now = start + i * step
            await poll_once(devices, prober, db, clock=clock)

    asyncio.run(go())
