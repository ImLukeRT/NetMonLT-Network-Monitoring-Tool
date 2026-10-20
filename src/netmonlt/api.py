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

from netmonlt import __version__, qos, quality, wifi
from netmonlt.collector import run_forever
from netmonlt.collector import stale_after_s as compute_stale_after_s
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
