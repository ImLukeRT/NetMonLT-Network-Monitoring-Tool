"""Polls devices through a Prober and stores the results."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from netmonlt.domain import CheckResult, Device, classify
from netmonlt.probes.base import Prober
from netmonlt.storage import Database

log = logging.getLogger(__name__)

PINGS_PER_CHECK = 3
# Safety net so one stuck probe can't stall the whole cycle. Probers should enforce their own
# (shorter) network timeouts and report an unreachable device as received=0.
PROBE_TIMEOUT_S = 10.0


async def poll_device(
    device: Device,
    prober: Prober,
    clock: Callable[[], float] = time.time,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> CheckResult:
    ping = await asyncio.wait_for(prober.ping(device, PINGS_PER_CHECK), timeout_s)
    return CheckResult(device=device.name, ts=clock(), status=classify(ping), ping=ping)


async def poll_once(
    devices: Sequence[Device],
    prober: Prober,
    db: Database,
    clock: Callable[[], float] = time.time,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> list[CheckResult]:
    """Check every device concurrently and store the results.

    A prober *error or timeout* (as opposed to a device being down) is logged and skipped, so one
    broken device can't stop the others or be recorded as a false "down". The dashboard flags
    devices whose last stored check has gone stale.
    """
    outcomes = await asyncio.gather(
        *(poll_device(d, prober, clock, timeout_s) for d in devices), return_exceptions=True
    )
    results: list[CheckResult] = []
    for device, outcome in zip(devices, outcomes, strict=True):
        if isinstance(outcome, TimeoutError):
            log.error("probe for %s timed out (limit %gs)", device.name, timeout_s)
            continue
        if isinstance(outcome, BaseException):
            log.error("probe failed for %s: %r", device.name, outcome)
            continue
        db.record_check(outcome)
        results.append(outcome)
    return results


async def run_forever(
    devices: Sequence[Device], prober: Prober, db: Database, interval_s: float
) -> None:
    """Poll on a fixed schedule until cancelled."""
    while True:
        started = time.monotonic()
        try:
            await poll_once(devices, prober, db)
        except Exception:
            log.exception("polling cycle failed")
        await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))
