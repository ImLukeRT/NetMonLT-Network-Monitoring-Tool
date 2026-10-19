"""Polls devices' interface statistics through an SnmpClient and stores the results.

Deliberately separate from the ICMP collector: its own loop, interval, timeouts and error
handling. A slow, failing or misconfigured SNMP device therefore cannot delay or break ICMP
monitoring, alerting or the latency charts. Nothing here ever raises into the caller except
cancellation, and no exception text from SNMP code is logged or stored (it could contain
credentials): only a fixed category, such as "timeout" or "auth_failure".
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from netmonlt.domain import Device
from netmonlt.ifstats import STATUS_OK, advance_device
from netmonlt.snmp import CATEGORY_ERROR, CATEGORY_TIMEOUT, SNMP_MESSAGES, SnmpClient, SnmpError
from netmonlt.storage import Database

log = logging.getLogger(__name__)

# Overall limit for polling one device (a real poll makes several requests, each with its own
# shorter timeout). Also used to decide when interface data has gone stale.
SNMP_POLL_TIMEOUT_S = 30.0
STALE_AFTER_INTERVALS = 3
ABSENT_KEEP_S = 7 * 86400  # how long an interface that stopped being reported stays listed


def snmp_stale_after_s(interval_s: float) -> float:
    """Interface data is stale after this long without a successful poll; rates are also not
    calculated across a longer gap."""
    return STALE_AFTER_INTERVALS * interval_s + SNMP_POLL_TIMEOUT_S


async def poll_device_snmp(
    device: Device,
    client: SnmpClient,
    db: Database,
    *,
    clock: Callable[[], float] = time.time,
    timeout_s: float = SNMP_POLL_TIMEOUT_S,
    max_gap_s: float,
) -> str:
    """Poll one device and store the outcome. Returns the stored status ("ok" or a category)."""
    category = None
    snapshot = None
    try:
        snapshot = await asyncio.wait_for(client.poll(device), timeout_s)
    except SnmpError as exc:
        category = exc.category
    except TimeoutError:
        category = CATEGORY_TIMEOUT
    except Exception as exc:  # never log the message: SNMP libraries may echo credentials
        log.error("SNMP poll of %s failed unexpectedly (%s)", device.name, type(exc).__name__)
        category = CATEGORY_ERROR

    ts = clock()
    if category is not None:
        previous = db.get_snmp_status(device.name)
        if previous is None or previous.status != category:  # say it once, not every cycle
            log.warning("SNMP poll of %s failed: %s", device.name, category)
        db.save_snmp_failure(device.name, ts, category, SNMP_MESSAGES.get(
            category, SNMP_MESSAGES[CATEGORY_ERROR]))
        return category

    previous_status = db.get_snmp_status(device.name)
    previous_uptime = None if previous_status is None else previous_status.uptime_ticks
    states = advance_device(
        db.get_interface_states(device.name), previous_uptime, snapshot, ts, max_gap_s
    )
    db.save_snmp_success(device.name, ts, snapshot.uptime_ticks, snapshot.truncated, states)
    if previous_status is not None and previous_status.status != STATUS_OK:
        log.info("SNMP poll of %s works again", device.name)
    return STATUS_OK


async def _safe_poll(device: Device, client: SnmpClient, db: Database, **kwargs) -> str:
    try:
        return await poll_device_snmp(device, client, db, **kwargs)
    except Exception as exc:  # e.g. a database error: keep the other devices going
        log.error("SNMP result for %s could not be processed (%s)", device.name, type(exc).__name__)
        return CATEGORY_ERROR


async def snmp_poll_once(
    devices: Sequence[Device],
    client: SnmpClient,
    db: Database,
    *,
    clock: Callable[[], float] = time.time,
    timeout_s: float = SNMP_POLL_TIMEOUT_S,
    max_gap_s: float,
) -> dict[str, str]:
    """Poll every device concurrently. Returns {device name: stored status}."""
    statuses = await asyncio.gather(*(
        _safe_poll(d, client, db, clock=clock, timeout_s=timeout_s, max_gap_s=max_gap_s)
        for d in devices
    ))
    return {d.name: s for d, s in zip(devices, statuses, strict=True)}


async def run_snmp_forever(
    devices: Sequence[Device], client: SnmpClient, db: Database, interval_s: float
) -> None:
    """Poll on a fixed schedule until cancelled."""
    max_gap_s = snmp_stale_after_s(interval_s)
    while True:
        started = time.monotonic()
        try:
            await snmp_poll_once(devices, client, db, max_gap_s=max_gap_s)
            db.prune_absent_interfaces(time.time() - ABSENT_KEEP_S)
        except Exception as exc:
            log.error("SNMP polling cycle failed (%s)", type(exc).__name__)
        await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))
