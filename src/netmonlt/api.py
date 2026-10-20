"""FastAPI app: JSON API plus the dashboard page."""

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from netmonlt import __version__, quality
from netmonlt.collector import run_forever
from netmonlt.collector import stale_after_s as compute_stale_after_s
from netmonlt.inventory import Inventory
from netmonlt.probes.base import Prober
from netmonlt.snmp import SnmpClient
from netmonlt.snmp_poller import run_snmp_forever, snmp_stale_after_s
from netmonlt.storage import Database

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    inventory: Inventory,
    db: Database,
    prober: Prober,
    *,
    start_collector: bool = True,
    clock: Callable[[], float] = time.time,
    snmp_client: SnmpClient | None = None,
) -> FastAPI:
    """Build the app. Tests pass start_collector=False to control polling themselves.

    `snmp_client` (optional) polls interface statistics for `inventory.snmp_devices` in a
    separate task, so SNMP problems can't affect the ICMP collector or anything it feeds.
    """
    stale_after_s = compute_stale_after_s(inventory.poll_interval_s)
    snmp_stale_s = snmp_stale_after_s(inventory.snmp_poll_interval_s)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = []
        if start_collector:
            tasks.append(asyncio.create_task(
                run_forever(inventory.devices, prober, db, inventory.poll_interval_s)
            ))
            if snmp_client is not None and inventory.snmp_devices:
                tasks.append(asyncio.create_task(run_snmp_forever(
                    inventory.snmp_devices, snmp_client, db, inventory.snmp_poll_interval_s
                )))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="NetMonLT", version=__version__, lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request):
        return TEMPLATES.TemplateResponse(request, "dashboard.html", {"mode": inventory.mode})

    @app.get("/api/devices")
    def list_devices():
        # Ages are computed here, with the server's clock, so a browser with a skewed clock (or
        # a dashboard opened from another machine) can't hide stale data.
        now = clock()
        devices = []
        for row in db.latest_status():
            age = None if row["ts"] is None else max(0.0, now - row["ts"])
            devices.append({**row, "age_s": age, "stale": age is not None and age > stale_after_s})
        return {
            "mode": inventory.mode,
            "poll_interval_s": inventory.poll_interval_s,
            "server_time": now,
            "stale_after_s": stale_after_s,
            "devices": devices,
        }

    @app.get("/api/devices/{name}/history")
    def device_history(name: str, limit: int = Query(100, ge=1, le=1000)):
        rows = db.history(name, limit)
        if rows is None:
            raise HTTPException(status_code=404, detail=f"unknown device {name!r}")
        return {"device": name, "checks": rows}

    @app.get("/api/devices/{name}/latency")
    def device_latency(
        name: str,
        window_s: int = Query(900, ge=60, le=7 * 86400),
        max_points: int = Query(300, ge=10, le=1000),
    ):
        """Latency history for charting: the last `window_s` seconds, at most ~max_points points.

        Down checks, missing measurements (`gaps`) and real 0 ms replies are reported separately;
        see Database.latency_series.
        """
        now = clock()
        series = db.latency_series(name, now - window_s, now, max_points, stale_after_s)
        if series is None:
            raise HTTPException(status_code=404, detail=f"unknown device {name!r}")
        last = series["last_check_ts"]
        age = None if last is None else max(0.0, now - last)
        bucket = series["bucket_s"]
        return {
            "device": name,
            "server_time": now,
            "window_s": window_s,
            "start": now - window_s,
            "end": now,
            "poll_interval_s": inventory.poll_interval_s,
            # Time covered by one point: a bucket when downsampled, else one polling interval.
            "resolution_s": inventory.poll_interval_s if bucket is None else bucket,
            "aggregated": bucket is not None,
            "gap_threshold_s": stale_after_s,
            "first_check_ts": series["first_check_ts"],
            "last_check_ts": last,
            "last_check_age_s": age,
            "stale": age is not None and age > stale_after_s,
            "checks": series["checks"],
            "points": series["points"],
            "gaps": series["gaps"],
        }

    def quality_view(name: str, window_s: int, now: float):
        """(summary, samples, first_ts, last_ts) for a device; None if it does not exist."""
        found = db.quality_samples(name, now - window_s, now)
        if found is None:
            return None
        samples, first, last = found
        summary = quality.summarize(
            samples, inventory.quality, start=now - window_s, end=now, gap_s=stale_after_s,
            first_check_ts=first, last_check_ts=last,
        )
        return summary, samples, first, last

    def quality_context(now: float, window_s: int) -> dict:
        cfg = inventory.quality
        return {
            "mode": inventory.mode,
            "simulated": inventory.mode == "simulated",
            "server_time": now,
            "window_s": window_s,
            "start": now - window_s,
            "end": now,
            "poll_interval_s": inventory.poll_interval_s,
            "gap_threshold_s": stale_after_s,
            "thresholds": {
                "latency_ms": dataclasses.asdict(cfg.latency_ms),
                "jitter_ms": dataclasses.asdict(cfg.jitter_ms),
                "loss_pct": dataclasses.asdict(cfg.loss_pct),
            },
            "min_checks": cfg.min_checks,
            "min_jitter_pairs": cfg.min_jitter_pairs,
            "max_gap_fraction": cfg.max_gap_fraction,
            "definitions": quality.DEFINITIONS,
            "scope": (
                "Network performance measured with ICMP echo. It does not verify or enforce QoS "
                "(DSCP marking, shaping, queueing); thresholds are heuristics, not a standard."
            ),
        }

    @app.get("/api/quality")
    def quality_overview(window_s: int | None = Query(None, ge=60, le=7 * 86400)):
        """Network-quality summary (latency, jitter, packet loss, grade) for every device over
        the last `window_s` seconds (default: the inventory's quality.window_s). Read-only."""
        window = inventory.quality.window_s if window_s is None else window_s
        now = clock()
        devices = []
        for device in inventory.devices:
            view = quality_view(device.name, window, now)
            if view is not None:
                devices.append({"device": device.name, "type": device.type, **view[0]})
        return {**quality_context(now, window), "devices": devices}

    @app.get("/api/devices/{name}/quality")
    def device_quality(
        name: str,
        window_s: int | None = Query(None, ge=60, le=7 * 86400),
        max_points: int = Query(60, ge=10, le=500),
    ):
        """One device's network-quality summary plus packet loss and jitter over time.

        `points` are equal time buckets (`bucket_s` wide, never zero-filled: empty buckets are
        omitted); `gaps` are periods with no checks. See netmonlt.quality for the definitions.
        """
        window = inventory.quality.window_s if window_s is None else window_s
        now = clock()
        view = quality_view(name, window, now)
        if view is None:
            raise HTTPException(status_code=404, detail=f"unknown device {name!r}")
        summary, samples, first, last = view
        points, bucket_s = quality.series(
            samples, start=now - window, end=now, gap_s=stale_after_s, first_check_ts=first,
            poll_interval_s=inventory.poll_interval_s, max_points=max_points,
        )
        gaps = quality.find_gaps(samples, now - window, now, stale_after_s, first)
        return {
            **quality_context(now, window),
            "device": name,
            "first_check_ts": first,
            "last_check_ts": last,
            "summary": summary,
            "bucket_s": bucket_s,
            "points": points,
            "gaps": [{"start": a, "end": b} for a, b in gaps],
        }

    @app.get("/api/interfaces")
    def list_interfaces(device: str | None = Query(None, max_length=64)):
        """Latest SNMP interface statistics per device (read-only). Rates are bits per second and
        error/discard figures are the increase over `interval_s`; both are null when unknown."""
        if device is not None and device not in {d.name for d in inventory.devices}:
            raise HTTPException(status_code=404, detail=f"unknown device {device!r}")
        now = clock()
        polled = {name: (status, states) for name, status, states in db.interface_overview(device)}
        entries = []
        for dev in inventory.snmp_devices:
            if device is not None and dev.name != device:
                continue
            status, states = polled.get(dev.name, (None, []))
            last_ok = None if status is None else status.last_ok_ts
            entries.append({
                "device": dev.name,
                "snmp": {
                    "status": "pending" if status is None else status.status,
                    "message": None if status is None else status.message,
                    "last_poll_age_s": None if status is None else max(0.0, now - status.ts),
                    "last_ok_age_s": None if last_ok is None else max(0.0, now - last_ok),
                    "stale": last_ok is not None and now - last_ok > snmp_stale_s,
                    "truncated": 0 if status is None else status.truncated,
                },
                "interfaces": [
                    {
                        "if_index": s.if_index, "name": s.name, "oper_status": s.oper_status,
                        "present": s.present, "age_s": max(0.0, now - s.ts),
                        "interval_s": s.interval_s, "in_bps": s.in_bps, "out_bps": s.out_bps,
                        "in_errors_delta": s.in_errors_delta,
                        "out_errors_delta": s.out_errors_delta,
                        "in_discards_delta": s.in_discards_delta,
                        "out_discards_delta": s.out_discards_delta,
                        "rate_note": s.rate_note,
                    }
                    for s in states
                ],
            })
        return {
            "mode": inventory.mode,
            "simulated": inventory.mode == "simulated",
            "server_time": now,
            "poll_interval_s": inventory.snmp_poll_interval_s,
            "stale_after_s": snmp_stale_s,
            "devices": entries,
        }

    @app.get("/api/alerts")
    def list_alerts(limit: int = Query(50, ge=1, le=500)):
        """Recent state-change alerts, newest first (read-only)."""
        now = clock()
        alerts = [
            {
                **row,
                "transition": f"{row['from_status']} -> {row['to_status']}",
                "age_s": max(0.0, now - row["ts"]),
            }
            for row in db.recent_alerts(limit)
        ]
        return {"server_time": now, "alerts": alerts}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
