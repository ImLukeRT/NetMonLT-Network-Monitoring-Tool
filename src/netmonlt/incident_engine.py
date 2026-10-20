"""Builds incidents from the alert log in the background.

A separate task from the ICMP collector, the SNMP, Wi-Fi and QoS pollers and the alert state
machine: it only READS the alerts those produce, so it cannot delay or change monitoring, and a
failure here is logged (by exception type only) and retried on the next cycle. All progress is
kept in SQLite (a cursor committed together with the incident changes), so a restart carries on
where it stopped without duplicating anything.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from netmonlt.domain import IncidentsConfig
from netmonlt.storage import Database

log = logging.getLogger(__name__)

INCIDENT_POLL_INTERVAL_S = 2.0


def process_once(
    db: Database, cfg: IncidentsConfig, *, simulated: bool, run_started_ts: float | None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Process every alert not yet seen (in batches). Returns how many were processed."""
    total = 0
    while True:
        done = db.process_new_alerts(
            cfg, simulated=simulated, run_started_ts=run_started_ts, now=clock()
        )
        total += done
        if done < 500:
            break
    if simulated and cfg.simulation_auto_resolve:  # simulation only, and off unless enabled
        db.auto_resolve_incidents(cfg, simulated_mode=True, now=clock())
    return total


async def run_incidents_forever(
    db: Database, cfg: IncidentsConfig, *, simulated: bool, run_started_ts: float | None,
    interval_s: float = INCIDENT_POLL_INTERVAL_S,
) -> None:
    """Keep incidents up to date until cancelled."""
    while True:
        try:
            process_once(db, cfg, simulated=simulated, run_started_ts=run_started_ts)
        except Exception as exc:  # e.g. a database error: never take the app down
            log.error("incident processing failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval_s)
