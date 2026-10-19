from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now
