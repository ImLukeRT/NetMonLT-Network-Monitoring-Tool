"""Polls devices' QoS telemetry through a QosClient and stores the results.

Deliberately separate from the ICMP collector, the SNMP interface poller and the Wi-Fi poller:
its own task, interval, timeouts and error handling. A slow, failing, unsupported or
misconfigured QoS device therefore cannot delay or break ICMP monitoring, interface polling,
Wi-Fi monitoring, alerting or the latency charts. Nothing here raises into the caller except
cancellation, and no exception text from SNMP code is logged or stored (it could contain
credentials): only a fixed category such as "timeout", "auth_failure" or "unsupported".
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from netmonlt.domain import Device
from netmonlt.ifstats import STATUS_OK
from netmonlt.qos import QosClient, advance_device
from netmonlt.snmp import (
    CATEGORY_ERROR,
    CATEGORY_TIMEOUT,
    CATEGORY_UNSUPPORTED,
    SNMP_MESSAGES,
    SnmpError,
)
from netmonlt.storage import Database

log = logging.getLogger(__name__)

QOS_POLL_TIMEOUT_S = 30.0  # overall limit for one device (a poll makes several walks)
STALE_AFTER_INTERVALS = 3
MAX_QOS_HISTORY_ROWS = 500_000  # a hard cap, whatever the retention setting and interval
ABSENT_KEEP_S = 7 * 86400  # how long an element that stopped being reported stays listed

QOS_MESSAGES = {
    **SNMP_MESSAGES,
    CATEGORY_UNSUPPORTED: (
        "the device returned no counters for the selected QoS adapter (it does not expose QoS "
        "telemetry this way, or no policy with counters is configured): no QoS data is shown"
    ),
}


def qos_stale_after_s(interval_s: float) -> float:
    """QoS data is stale after this long without a successful poll; rates are also not
    calculated across a longer gap."""
    return STALE_AFTER_INTERVALS * interval_s + QOS_POLL_TIMEOUT_S


async def poll_device_qos(
    device: Device,
    client: QosClient,
    db: Database,
    *,
    clock: Callable[[], float] = time.time,
    timeout_s: float = QOS_POLL_TIMEOUT_S,
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
        log.error("QoS poll of %s failed unexpectedly (%s)", device.name, type(exc).__name__)
        category = CATEGORY_ERROR

    ts = clock()
    previous = db.get_qos_status(device.name)
    if category is not None:
        if previous is None or previous.status != category:  # say it once, not every cycle
            log.warning("QoS poll of %s failed: %s", device.name, category)
        db.save_qos_failure(
            device.name, ts, category, QOS_MESSAGES.get(category, QOS_MESSAGES[CATEGORY_ERROR])
        )
        return category

    if not snapshot.rows:  # a device that answers but has nothing to show is "unsupported"
        log.warning("QoS poll of %s returned no elements", device.name)
        db.save_qos_failure(device.name, ts, CATEGORY_UNSUPPORTED,
                            QOS_MESSAGES[CATEGORY_UNSUPPORTED])
        return CATEGORY_UNSUPPORTED
    previous_uptime = None if previous is None else previous.uptime_ticks
    states = advance_device(
        db.get_qos_states(device.name), previous_uptime, snapshot, ts, max_gap_s
    )
    db.save_qos_success(device.name, ts, snapshot, states)
    if previous is not None and previous.status != STATUS_OK:
        log.info("QoS poll of %s works again", device.name)
    return STATUS_OK


async def _safe_poll(device: Device, client: QosClient, db: Database, **kwargs) -> str:
    try:
        return await poll_device_qos(device, client, db, **kwargs)
    except Exception as exc:  # e.g. a database error: keep the other devices going
        log.error("QoS result for %s could not be processed (%s)", device.name, type(exc).__name__)
        return CATEGORY_ERROR


async def qos_poll_once(
    devices: Sequence[Device],
    client: QosClient,
    db: Database,
    *,
    clock: Callable[[], float] = time.time,
    timeout_s: float = QOS_POLL_TIMEOUT_S,
    max_gap_s: float,
) -> dict[str, str]:
    """Poll every device concurrently. Returns {device name: stored status}."""
    statuses = await asyncio.gather(*(
        _safe_poll(d, client, db, clock=clock, timeout_s=timeout_s, max_gap_s=max_gap_s)
        for d in devices
    ))
    return {d.name: s for d, s in zip(devices, statuses, strict=True)}


async def run_qos_forever(
    devices: Sequence[Device], client: QosClient, db: Database, interval_s: float,
    retention_hours: float,
) -> None:
    """Poll on a fixed schedule until cancelled, pruning old history as it goes."""
    max_gap_s = qos_stale_after_s(interval_s)
    while True:
        started = time.monotonic()
        try:
            await qos_poll_once(devices, client, db, max_gap_s=max_gap_s)
            now = time.time()
            db.prune_qos(now - retention_hours * 3600, MAX_QOS_HISTORY_ROWS, now - ABSENT_KEEP_S)
        except Exception as exc:
            log.error("QoS polling cycle failed (%s)", type(exc).__name__)
        await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - started)))
