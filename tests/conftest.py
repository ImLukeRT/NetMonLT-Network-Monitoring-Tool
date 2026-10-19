import pytest

from netmonlt.domain import Device, SimProfile
from netmonlt.storage import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def devices():
    return [
        Device("alpha", "192.0.2.1", "router", SimProfile(rtt_ms=5, jitter_ms=0)),
        Device("beta", "192.0.2.2", "switch", SimProfile(rtt_ms=7, jitter_ms=0)),
    ]
