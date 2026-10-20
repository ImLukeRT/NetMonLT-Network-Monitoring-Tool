"""Polls this computer's Wi-Fi adapter through a WifiSource and stores what it finds.

Like the SNMP poller, this is deliberately separate from the ICMP collector: its own task,
interval, timeout and error handling. A slow, hung, missing or failing wireless query therefore
cannot delay or break ICMP monitoring, SNMP polling, alerting or the latency charts. Nothing
here raises into the caller except cancellation, and no exception text from the source is logged
or stored (it could contain paths or the network name): only a fixed category such as
"no_adapter" or "timeout".
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

from netmonlt.domain import WifiConfig
from netmonlt.storage import Database
from netmonlt.wifi import (
    CATEGORY_ERROR,
    CATEGORY_TIMEOUT,
    STATUS_OK,
    WIFI_MESSAGES,
    WifiError,
    WifiReading,
    apply_ssid_privacy,
)

log = logging.getLogger(__name__)

# Overall limit for one query; also used to decide when observations have gone stale.
WIFI_POLL_TIMEOUT_S = 20.0
STALE_AFTER_INTERVALS = 3
MAX_OBSERVATIONS = 200_000  # a hard cap, whatever the retention setting and interval


class WifiSource(Protocol):
    async def read(self) -> WifiReading: ...


def wifi_stale_after_s(interval_s: float) -> float:
    """Wi-Fi data is stale after this long without a successful poll."""
    return STALE_AFTER_INTERVALS * interval_s + WIFI_POLL_TIMEOUT_S


async def poll_wifi(
    source: WifiSource,
    db: Database,
    cfg: WifiConfig,
    *,
    clock: Callable[[], float] = time.time,
    timeout_s: float = WIFI_POLL_TIMEOUT_S,
) -> str:
    """Poll once and store the outcome. Returns "ok" or the failure category."""
    category = None
    reading = None
    try:
        reading = await asyncio.wait_for(source.read(), timeout_s)
    except WifiError as exc:
        category = exc.category
    except TimeoutError:
        category = CATEGORY_TIMEOUT
    except Exception as exc:  # never log the message: it may name paths or the network
        log.error("Wi-Fi poll failed unexpectedly (%s)", type(exc).__name__)
        category = CATEGORY_ERROR

    ts = clock()
    if category is not None:
        previous = db.get_wifi_status()
        if previous is None or previous.status != category:  # say it once, not every cycle
            log.warning("Wi-Fi poll failed: %s", category)
        message = WIFI_MESSAGES.get(category, WIFI_MESSAGES[CATEGORY_ERROR])
        db.save_wifi_failure(ts, category, message)
        return category

    previous = db.get_wifi_status()
    # The SSID is reduced to what the privacy setting allows BEFORE it reaches the database.
    db.save_wifi_observation(ts, apply_ssid_privacy(reading, cfg.ssid_privacy))
    if previous is not None and previous.status != STATUS_OK:
        log.info("Wi-Fi poll works again")
    return STATUS_OK


async def run_wifi_forever(source: WifiSource, db: Database, cfg: WifiConfig) -> None:
    """Poll on a fixed schedule until cancelled, pruning old observations as it goes."""
    while True:
        started = time.monotonic()
        try:
            await poll_wifi(source, db, cfg)
            db.prune_wifi(time.time() - cfg.retention_days * 86400, MAX_OBSERVATIONS)
        except Exception as exc:  # e.g. a database error: keep going
            log.error("Wi-Fi polling cycle failed (%s)", type(exc).__name__)
        await asyncio.sleep(max(0.0, cfg.poll_interval_s - (time.monotonic() - started)))
