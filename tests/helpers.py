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
