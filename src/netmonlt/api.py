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
from netmonlt.collector import PROBE_TIMEOUT_S, run_forever
from netmonlt.inventory import Inventory
from netmonlt.probes.base import Prober
from netmonlt.storage import Database

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# A device is "stale" when its newest stored check is older than this many poll intervals plus
# the probe timeout (so one slow cycle isn't mistaken for a dead collector).
STALE_AFTER_INTERVALS = 3


def create_app(
    inventory: Inventory,
    db: Database,
    prober: Prober,
    *,
    start_collector: bool = True,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the app. Tests pass start_collector=False to control polling themselves."""
    stale_after_s = STALE_AFTER_INTERVALS * inventory.poll_interval_s + PROBE_TIMEOUT_S

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

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
