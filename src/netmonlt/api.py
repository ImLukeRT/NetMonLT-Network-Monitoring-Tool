"""FastAPI app: JSON API plus the dashboard page."""

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from netmonlt import __version__
from netmonlt.collector import run_forever
from netmonlt.collector import stale_after_s as compute_stale_after_s
from netmonlt.inventory import Inventory
from netmonlt.probes.base import Prober
from netmonlt.storage import Database

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    inventory: Inventory,
    db: Database,
    prober: Prober,
    *,
    start_collector: bool = True,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the app. Tests pass start_collector=False to control polling themselves."""
    stale_after_s = compute_stale_after_s(inventory.poll_interval_s)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if start_collector:
            task = asyncio.create_task(
                run_forever(inventory.devices, prober, db, inventory.poll_interval_s)
            )
        try:
            yield
        finally:
            if task:
                task.cancel()
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
