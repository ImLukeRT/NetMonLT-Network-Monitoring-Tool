"""FastAPI app: JSON API plus the dashboard page."""

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from netmonlt import __version__
from netmonlt.collector import run_forever
from netmonlt.inventory import Inventory
from netmonlt.probes.base import Prober
from netmonlt.storage import Database

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    inventory: Inventory, db: Database, prober: Prober, *, start_collector: bool = True
) -> FastAPI:
    """Build the app. Tests pass start_collector=False to control polling themselves."""

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
        return {
            "mode": inventory.mode,
            "poll_interval_s": inventory.poll_interval_s,
            "devices": db.latest_status(),
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
