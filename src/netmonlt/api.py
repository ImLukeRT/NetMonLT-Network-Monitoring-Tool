"""FastAPI app: JSON API plus the dashboard page."""

import asyncio
import contextlib
import dataclasses
import hmac
import ipaddress
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from netmonlt import __version__, qos, quality, wifi
from netmonlt import incidents as inc
from netmonlt.collector import run_forever
from netmonlt.collector import stale_after_s as compute_stale_after_s
from netmonlt.incident_engine import run_incidents_forever
from netmonlt.inventory import Inventory
from netmonlt.probes.base import Prober
from netmonlt.qos import QosClient
from netmonlt.qos_adapters import ADAPTERS
from netmonlt.qos_poller import qos_stale_after_s, run_qos_forever
from netmonlt.snmp import SnmpClient
from netmonlt.snmp_poller import run_snmp_forever, snmp_stale_after_s
from netmonlt.storage import Database
from netmonlt.wifi_poller import WifiSource, run_wifi_forever, wifi_stale_after_s

MAX_QOS_HISTORY_SERIES = 16
LOOPBACK_NAMES = {"localhost"}
TOKEN_HEADER = "x-netmonlt-token"


class _Body(BaseModel):
    """Request bodies reject unknown fields, so a typo or an injected field is an error."""

    model_config = ConfigDict(extra="forbid")


class AckBody(_Body):
    actor: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=inc.MAX_NOTE_LEN)


class NoteBody(_Body):
    actor: str | None = Field(None, max_length=40)
    text: str = Field(max_length=inc.MAX_NOTE_LEN)


class ReportBody(_Body):
    actor: str | None = Field(None, max_length=40)
    root_cause_status: str | None = Field(None, max_length=20)
    root_cause: str | None = Field(None, max_length=inc.REPORT_TEXT_FIELDS["root_cause"])
    actions_taken: str | None = Field(None, max_length=inc.REPORT_TEXT_FIELDS["actions_taken"])
    impact_assessment: str | None = Field(
        None, max_length=inc.REPORT_TEXT_FIELDS["impact_assessment"])
    recovery_verification: str | None = Field(
        None, max_length=inc.REPORT_TEXT_FIELDS["recovery_verification"])
    lessons_learned: str | None = Field(None, max_length=inc.REPORT_TEXT_FIELDS["lessons_learned"])


class ResolveBody(ReportBody):
    resolution_summary: str = Field(max_length=inc.REPORT_TEXT_FIELDS["resolution_summary"])


class ReopenBody(_Body):
    actor: str | None = Field(None, max_length=40)
    reason: str = Field(max_length=inc.MAX_REASON_LEN)


def _is_loopback(host: str | None) -> bool:
    """True only for a literal loopback address (never a hostname such as "testclient")."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _local_name(name: str | None) -> bool:
    return bool(name) and (name in LOOPBACK_NAMES or _is_loopback(name))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    inventory: Inventory,
    db: Database,
    prober: Prober,
    *,
    start_collector: bool = True,
    clock: Callable[[], float] = time.time,
    snmp_client: SnmpClient | None = None,
    wifi_source: WifiSource | None = None,
    qos_client: QosClient | None = None,
    start_incidents: bool | None = None,
) -> FastAPI:
    """Build the app. Tests pass start_collector=False to control polling themselves.

    `snmp_client` (optional) polls interface statistics for `inventory.snmp_devices` in a
    separate task, so SNMP problems can't affect the ICMP collector or anything it feeds.
    `wifi_source` (optional, used only if `inventory.wifi.enabled`) does the same for this
    computer's own Wi-Fi link, and `qos_client` (optional) for `inventory.qos_devices`'s QoS
    telemetry. Each runs in its own task, so none can delay the others.
    """
    stale_after_s = compute_stale_after_s(inventory.poll_interval_s)
    snmp_stale_s = snmp_stale_after_s(inventory.snmp_poll_interval_s)
    wifi_cfg = inventory.wifi
    wifi_stale_s = wifi_stale_after_s(wifi_cfg.poll_interval_s)
    qos_stale_s = qos_stale_after_s(inventory.qos_poll_interval_s)
    incidents_cfg = inventory.incidents
    simulated_mode = inventory.mode == "simulated"
    # Changing incidents is on by default in the simulated demo, but opt-in for real monitoring.
    actions_enabled = (simulated_mode if incidents_cfg.operator_actions is None
                       else incidents_cfg.operator_actions)
    run_started_ts = clock()  # when this run began: lets a simulated incident be tied to its script
    change_token = secrets.token_urlsafe(32)  # per process; only the local dashboard page gets it
    devices_by_name = {d.name: d for d in inventory.devices}
    qos_devices = {d.name: d for d in inventory.qos_devices}

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
            if (start_incidents if start_incidents is not None else True) and incidents_cfg.enabled:
                tasks.append(asyncio.create_task(run_incidents_forever(
                    db, incidents_cfg, simulated=simulated_mode, run_started_ts=run_started_ts
                )))
            if qos_client is not None and qos_devices:
                tasks.append(asyncio.create_task(run_qos_forever(
                    inventory.qos_devices, qos_client, db, inventory.qos_poll_interval_s,
                    inventory.qos_retention_hours,
                )))
            if wifi_source is not None and wifi_cfg.enabled:
                tasks.append(asyncio.create_task(run_wifi_forever(wifi_source, db, wifi_cfg)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="NetMonLT", version=__version__, lifespan=lifespan)
    app.state.incident_token = change_token

    def change_access(request: Request) -> tuple[bool, str | None]:
        """Whether this request may change incidents, and why not. Changes are accepted only
        from a loopback client addressed as localhost: the machine running NetMonLT."""
        if not incidents_cfg.enabled:
            return False, "incident management is disabled in the inventory"
        if not actions_enabled:
            if incidents_cfg.operator_actions is None:
                return False, ("incident changes are off by default in real mode: set "
                               "incidents.operator_actions: true in your inventory to allow them")
            return False, "operator actions are disabled in the inventory (read-only)"
        if not _is_loopback(request.client.host if request.client else None):
            return False, "incident changes are only accepted from the machine running NetMonLT"
        if not _local_name(urlsplit("//" + request.headers.get("host", "")).hostname):
            return False, "open the dashboard as http://localhost or http://127.0.0.1 to change"
        return True, None

    def require_change(request: Request) -> None:
        """Guard for every state-changing incident endpoint (raises HTTPException)."""
        allowed, reason = change_access(request)
        if not allowed:
            raise HTTPException(status_code=403, detail=reason)
        origin = request.headers.get("origin")
        if origin is not None and not _local_name(urlsplit(origin).hostname):
            raise HTTPException(status_code=403, detail="cross-site request refused")
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="send Content-Type: application/json")
        token = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), change_token.encode()):
            raise HTTPException(status_code=403, detail="missing or invalid change token")

    @app.exception_handler(inc.IncidentError)
    async def incident_error(_: Request, exc: inc.IncidentError):
        status = {inc.IncidentError.NOT_FOUND: 404,
                  inc.IncidentError.INVALID_TRANSITION: 409}.get(exc.code, 422)
        return JSONResponse(status_code=status, content={"detail": exc.message, "code": exc.code})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request):
        # The change token goes only to a page the machine running NetMonLT itself is viewing.
        allowed, _ = change_access(request)
        response = TEMPLATES.TemplateResponse(
            request, "dashboard.html",
            {"mode": inventory.mode, "incident_token": change_token if allowed else ""},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

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

    def qos_adapter_info(device) -> dict:
        """Which adapter and source a QoS device is read with (simulated, or a real adapter)."""
        if inventory.mode == "simulated":
            return {"adapter": "simulated", "adapter_label": "simulated",
                    "source_mib": "simulated (no real device)", "requirements": None}
        adapter = ADAPTERS.get(device.qos.adapter)
        return {
            "adapter": device.qos.adapter,
            "adapter_label": device.qos.adapter if adapter is None else adapter.title,
            "source_mib": None if adapter is None else adapter.source_mib,
            "requirements": None if adapter is None else adapter.requirements,
        }

    @app.get("/api/qos")
    def qos_state(device: str | None = Query(None, max_length=64)):
        """The latest QoS telemetry per configured device (read-only): traffic classes, count
        actions and droppers with packet and throughput rates and drops, plus where each figure
        comes from (adapter, source MIB, counter width, polling time). Rates are null when they
        cannot be known, and a device that exposes no QoS counters is "unsupported": nothing is
        invented. Raw counters are never returned. See `definitions` and `scope` for what QoS
        telemetry does and does not show."""
        if device is not None and device not in {d.name for d in inventory.devices}:
            raise HTTPException(status_code=404, detail=f"unknown device {device!r}")
        now = clock()
        polled = {name: (status, states) for name, status, states in db.qos_overview(device)}
        entries = []
        for name, dev in qos_devices.items():
            if device is not None and name != device:
                continue
            status, states = polled.get(name, (None, []))
            info = qos_adapter_info(dev)
            last_ok = None if status is None else status.last_ok_ts
            entries.append({
                "device": name,
                **info,
                "source_mib": (status.source_mib if status and status.source_mib
                               else info["source_mib"]),
                "status": {
                    "status": "pending" if status is None else status.status,
                    "message": None if status is None else status.message,
                    "last_poll_age_s": None if status is None else max(0.0, now - status.ts),
                    "last_ok_age_s": None if last_ok is None else max(0.0, now - last_ok),
                    "stale": last_ok is not None and now - last_ok > qos_stale_s,
                },
                "truncated": 0 if status is None else status.truncated,
                "rows": [
                    {
                        "key": s.key, "kind": s.kind, "name": s.name, "detail": s.detail,
                        "present": s.present, "polled_at": s.ts, "age_s": max(0.0, now - s.ts),
                        "counter_bits": s.counter_bits,
                        "counter_type": f"Counter{s.counter_bits}",
                        "interval_s": s.interval_s, "pps": s.pps, "bps": s.bps,
                        "drop_pps": s.drop_pps, "drop_bps": s.drop_bps,
                        "drop_pct": s.drop_pct, "rate_note": s.rate_note,
                    }
                    for s in states
                ],
            })
        return {
            "enabled": bool(qos_devices),
            "mode": inventory.mode,
            "simulated": inventory.mode == "simulated",
            "server_time": now,
            "poll_interval_s": inventory.qos_poll_interval_s,
            "stale_after_s": qos_stale_s,
            "definitions": qos.DEFINITIONS,
            "scope": (
                "Counters a device reports for its own QoS policy. They do not prove that QoS "
                "is enforced or effective, are not DSCP markings observed in traffic, and are "
                "not the ICMP quality grades."
            ),
            "devices": entries,
        }

    @app.get("/api/qos/history")
    def qos_history(
        device: str = Query(..., max_length=64),
        window_s: int = Query(900, ge=60, le=7 * 86400),
        max_points: int = Query(60, ge=10, le=500),
    ):
        """Derived QoS rates over time for one device, one series per element, in time buckets
        (empty buckets omitted; a bucket with no valid rate has null rates, never zero) plus
        `gaps` where nothing was polled."""
        if device not in qos_devices:
            raise HTTPException(status_code=404, detail=f"no QoS telemetry for {device!r}")
        now = clock()
        samples, first, last = db.qos_samples(device, now - window_s, now)
        by_key, bucket_s = qos.history_series(
            samples, start=now - window_s, end=now, first_ts=first,
            poll_interval_s=inventory.qos_poll_interval_s, max_points=max_points,
        )
        kinds = {s.key: s.kind for s in db.get_qos_states(device).values()}
        ordered = sorted(by_key.items(), key=lambda item: (kinds.get(item[0], ""), item[1]["name"]))
        gaps = quality.find_gaps(samples, now - window_s, now, qos_stale_s, first)
        return {
            "device": device,
            "simulated": inventory.mode == "simulated",
            "server_time": now,
            "window_s": window_s,
            "start": now - window_s,
            "end": now,
            "poll_interval_s": inventory.qos_poll_interval_s,
            "gap_threshold_s": qos_stale_s,
            "first_ts": first,
            "last_ts": last,
            "last_age_s": None if last is None else max(0.0, now - last),
            "bucket_s": bucket_s,
            "series": [
                {"key": key, "kind": kinds.get(key), "name": data["name"],
                 "points": data["points"]}
                for key, data in ordered[:MAX_QOS_HISTORY_SERIES]
            ],
            "series_truncated": max(0, len(ordered) - MAX_QOS_HISTORY_SERIES),
            "gaps": [{"start": a, "end": b} for a, b in gaps],
        }

    @app.get("/api/wifi")
    def wifi_state():
        """This computer's Wi-Fi link: the latest observation, the outcome of the latest poll and
        the low-signal warning. `{"enabled": false}` unless wireless monitoring is switched on.

        Every figure is reported by the OS, simulated, or null (unavailable): `fields` says which.
        `rssi_dbm` is only ever a value the OS reported (see `rssi_source`), never derived from
        `signal_pct`. Read-only.
        """
        if not wifi_cfg.enabled:
            return {"enabled": False}
        now = clock()
        status = db.get_wifi_status()
        recent = db.latest_wifi(100)
        samples = [
            wifi.WifiSample(ts, r.state, r.signal_pct, r.rssi_dbm, r.rx_mbps, r.tx_mbps)
            for r, ts in recent
        ]
        current = None
        source = "simulated" if inventory.mode == "simulated" else wifi_cfg.backend
        if recent:
            reading, ts = recent[0]
            reading = wifi.apply_ssid_privacy(reading, wifi_cfg.ssid_privacy)  # honour changes
            source = reading.source
            current = {
                "state": reading.state, "ssid": reading.ssid, "interface": reading.interface,
                "signal_pct": reading.signal_pct, "rssi_dbm": reading.rssi_dbm,
                "rssi_source": reading.rssi_source, "rx_mbps": reading.rx_mbps,
                "tx_mbps": reading.tx_mbps, "radio_type": reading.radio_type,
                "channel": reading.channel, "age_s": max(0.0, now - ts),
                "fields": wifi.field_provenance(reading),
            }
        last_ok = None if status is None else status.last_ok_ts
        return {
            "enabled": True,
            "mode": inventory.mode,
            "simulated": inventory.mode == "simulated",
            "source": source,
            "source_label": wifi.SOURCE_LABELS.get(source, source),
            "server_time": now,
            "poll_interval_s": wifi_cfg.poll_interval_s,
            "stale_after_s": wifi_stale_s,
            "ssid_privacy": wifi_cfg.ssid_privacy,
            "status": {
                "status": "pending" if status is None else status.status,
                "message": None if status is None else status.message,
                "last_poll_age_s": None if status is None else max(0.0, now - status.ts),
                "last_ok_age_s": None if last_ok is None else max(0.0, now - last_ok),
                "stale": last_ok is not None and now - last_ok > wifi_stale_s,
            },
            "current": current,
            "warning": wifi.low_signal_warning(
                samples, wifi_cfg, now=now, stale_after_s=wifi_stale_s
            ),
        }

    @app.get("/api/wifi/history")
    def wifi_history(
        window_s: int = Query(900, ge=60, le=7 * 86400),
        max_points: int = Query(60, ge=10, le=500),
    ):
        """Signal-quality history in time buckets (never zero-filled: empty buckets are omitted
        and a disconnected observation is counted, not averaged in as 0 %), plus `gaps`."""
        if not wifi_cfg.enabled:
            raise HTTPException(status_code=404, detail="Wi-Fi monitoring is not enabled")
        now = clock()
        samples, first, last = db.wifi_samples(now - window_s, now)
        points, bucket_s = wifi.series(
            samples, start=now - window_s, end=now, first_ts=first,
            poll_interval_s=wifi_cfg.poll_interval_s, max_points=max_points,
        )
        gaps = quality.find_gaps(samples, now - window_s, now, wifi_stale_s, first)
        return {
            "enabled": True,
            "simulated": inventory.mode == "simulated",
            "server_time": now,
            "window_s": window_s,
            "start": now - window_s,
            "end": now,
            "poll_interval_s": wifi_cfg.poll_interval_s,
            "gap_threshold_s": wifi_stale_s,
            "low_signal_pct": wifi_cfg.low_signal_pct,
            "first_ts": first,
            "last_ts": last,
            "last_age_s": None if last is None else max(0.0, now - last),
            "observations": len(samples),
            "bucket_s": bucket_s,
            "points": points,
            "gaps": [{"start": a, "end": b} for a, b in gaps],
        }

    # ---- incident management (a workflow on top of the alert log)
    def incident_summary(i: inc.Incident, alerts: int = 0) -> dict:
        return {
            "id": i.id, "display_id": inc.display_id(i.id), "device": i.device, "kind": i.kind,
            "severity": i.severity, "lifecycle": inc.lifecycle(i),
            "flapping": inc.is_flapping(i, incidents_cfg), "flap_count": i.flap_count,
            "opened_ts": i.opened_ts, "last_alert_ts": i.last_alert_ts,
            "recovered_ts": i.recovered_ts if i.monitoring_state == inc.MON_RECOVERED else None,
            "acknowledged_ts": i.acknowledged_ts, "acknowledged_by": i.acknowledged_by,
            "resolved_ts": i.resolved_ts, "resolved_by": i.resolved_by,
            "reopened_count": i.reopened_count, "previous_incident_id": i.previous_incident_id,
            "previous_display_id": (None if i.previous_incident_id is None
                                    else inc.display_id(i.previous_incident_id)),
            "simulated": i.simulated, "alert_events": alerts,
        }

    def incident_access(request: Request) -> dict:
        allowed, reason = change_access(request)
        return {"can_modify": allowed, "read_only_reason": reason}

    def require_incident(incident_id: int) -> inc.Incident:
        if not incidents_cfg.enabled:
            raise inc.IncidentError(inc.IncidentError.NOT_FOUND, "incident management is disabled")
        found = db.get_incident(incident_id)
        if found is None:
            raise inc.IncidentError(inc.IncidentError.NOT_FOUND, "no such incident")
        return found

    def build_incident_report(i: inc.Incident, now: float) -> tuple[dict, list]:
        device = devices_by_name.get(i.device)
        events = db.incident_events(i.id)
        report = inc.build_report(
            i, events, db.incident_evidence(i, now), cfg=incidents_cfg,
            device_type=device.type if device else "host", sim=device.sim if device else None,
            poll_interval_s=inventory.poll_interval_s,
        )
        return report, events

    @app.get("/api/incidents")
    def list_incidents(
        request: Request,
        view: Literal["active", "history", "all"] = "active",
        status: Literal["open", "acknowledged", "recovered", "reopened", "resolved"] | None = None,
        severity: Literal["critical", "warning", "info"] | None = None,
        device: str | None = Query(None, max_length=64),
        limit: int = Query(100, ge=1, le=500),
    ):
        """Incidents built from the alert log: `view` "active" (not resolved), "history"
        (resolved) or "all", optionally filtered. Each has a `lifecycle` that keeps the monitoring
        state (active or recovered, set only by monitoring) apart from the review state (open,
        acknowledged or resolved, set only by people). Read-only."""
        base = {"enabled": incidents_cfg.enabled, "simulated": simulated_mode,
                "server_time": clock(), **incident_access(request),
                "correlation_window_s": incidents_cfg.correlation_window_s,
                "flapping_recurrences": incidents_cfg.flapping_recurrences, "view": view}
        if not incidents_cfg.enabled:
            return {**base, "counts": {"active": 0, "history": 0, "awaiting_review": 0},
                    "incidents": []}
        rows = db.list_incidents(view=view, status=status, severity=severity, device=device,
                                 limit=limit)
        return {**base, "counts": db.incident_counts(),
                "incidents": [incident_summary(r, db.incident_alert_count(r.id)) for r in rows]}

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(request: Request, incident_id: int):
        """One incident with its full timeline, monitoring evidence and write-up (observed
        evidence, operator-entered fields and, in simulated mode, a labelled fictional example)."""
        found = require_incident(incident_id)
        now = clock()
        report, events = build_incident_report(found, now)
        return {"server_time": now, **incident_access(request),
                "incident": incident_summary(found, db.incident_alert_count(found.id)),
                "report": report, "markdown": inc.render_markdown(report)}

    @app.get("/api/incidents/{incident_id}/report")
    def incident_report(incident_id: int, format: Literal["json", "markdown"] = "markdown"):
        """The write-up on its own, as Markdown (for copying into a report) or JSON."""
        found = require_incident(incident_id)
        report, _ = build_incident_report(found, clock())
        if format == "json":
            return report
        return PlainTextResponse(inc.render_markdown(report), media_type="text/markdown")

    def changed(incident_id: int, extra: dict | None = None) -> dict:
        found = db.get_incident(incident_id)
        return {"incident": incident_summary(found, db.incident_alert_count(found.id)),
                **(extra or {})}

    @app.post("/api/incidents/{incident_id}/acknowledge", dependencies=[Depends(require_change)])
    def acknowledge(incident_id: int, body: AckBody):
        """Record that a person has seen this incident. Does not change monitoring state."""
        db.acknowledge_incident(incident_id, body.actor, body.note, clock())
        return changed(incident_id)

    @app.post("/api/incidents/{incident_id}/notes", dependencies=[Depends(require_change)])
    def add_note(incident_id: int, body: NoteBody):
        """Add an investigation note (kept for good; notes are never edited or deleted)."""
        event = db.add_incident_note(incident_id, body.actor, body.text, clock())
        return changed(incident_id, {"event_id": event.id})

    @app.put("/api/incidents/{incident_id}/report", dependencies=[Depends(require_change)])
    def update_report(incident_id: int, body: ReportBody):
        """Edit the operator-entered write-up fields. Every change is appended to the audit
        trail with its new value, so earlier wording is never lost."""
        updates = body.model_dump(exclude_unset=True, exclude={"actor"})
        if not updates:
            raise inc.IncidentError(inc.IncidentError.INVALID_INPUT, "no write-up fields given")
        db.update_incident_report(incident_id, body.actor, updates, clock())
        return changed(incident_id)

    @app.post("/api/incidents/{incident_id}/resolve", dependencies=[Depends(require_change)])
    def resolve(incident_id: int, body: ResolveBody):
        """Close an incident. Needs a written resolution summary, and only works once monitoring
        has confirmed recovery. Monitoring evidence is left exactly as it is."""
        updates = body.model_dump(exclude_unset=True, exclude={"actor", "resolution_summary"})
        db.resolve_incident(incident_id, body.actor, body.resolution_summary, updates, clock())
        return changed(incident_id)

    @app.post("/api/incidents/{incident_id}/reopen", dependencies=[Depends(require_change)])
    def reopen(incident_id: int, body: ReopenBody):
        """Reopen a resolved incident, with a reason. Its history stays."""
        db.reopen_incident(incident_id, body.actor, body.reason, clock())
        return changed(incident_id)

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
