"""Polls devices through a Prober and stores the results."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from netmonlt.alerts import process_check
from netmonlt.domain import CheckResult, Device, classify
from netmonlt.probes.base import Prober
from netmonlt.storage import Database

log = logging.getLogger(__name__)

PINGS_PER_CHECK = 3
# Safety net so one stuck probe can't stall the whole cycle. Probers should enforce their own
# (shorter) network timeouts and report an unreachable device as received=0.
PROBE_TIMEOUT_S = 10.0
# A device's newest check is "stale" (and its alerting state unreliable) once it is older than
# this many poll intervals plus the probe timeout, so one slow cycle isn't mistaken for a gap.
STALE_AFTER_INTERVALS = 3


def stale_after_s(poll_interval_s: float) -> float:
    return STALE_AFTER_INTERVALS * poll_interval_s + PROBE_TIMEOUT_S


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
    max_gap_s: float = stale_after_s(5.0),
) -> list[CheckResult]:
    """Check every device concurrently and store the results.

    A prober *error or timeout* (as opposed to a device being down) is logged and skipped, so one
    broken device can't stop the others or be recorded as a false "down". The dashboard flags
    devices whose last stored check has gone stale. Each stored check is also fed to the alerting
    state machine; a failure there is logged and never stops monitoring.
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
        try:
            alert = process_check(db, outcome, max_gap_s)
        except Exception:
            log.exception("alert evaluation failed for %s", device.name)
            continue
        if alert is not None:
            log.log(logging.INFO if alert.severity == "info" else logging.WARNING,
                    "ALERT [%s] %s", alert.severity.value, alert.message)
    return results


async def run_forever(
    devices: Sequence[Device], prober: Prober, db: Database, interval_s: float
) -> None:
    """Poll on a fixed schedule until cancelled."""
    while True:
        started = time.monotonic()
        try:
            await poll_once(devices, prober, db, max_gap_s=stale_after_s(interval_s))
        except Exception:
            log.exception("polling cycle failed")
        await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))
