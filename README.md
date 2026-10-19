# NetMonLT

A lightweight network monitoring platform: device inventory in YAML, a background
collector that measures reachability and latency, SQLite persistence, and a live web
dashboard. It ships with a **simulated-device mode** so it can be demonstrated without
any networking equipment.

> Status: early MVP. Only simulated monitoring is implemented. Real ICMP/SNMP monitoring
> is planned as an opt-in, allowlist-restricted feature (see [Roadmap](#roadmap)).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`
on Windows). No Docker, no admin rights, no network access needed.

```powershell
git clone https://github.com/ImLukeRT/Test-.git
cd Test-
uv sync
uv run netmonlt
```

Then open <http://127.0.0.1:8000>. Within about 30 seconds the simulated `access-switch`
goes **down** for 30 seconds and recovers, while `wifi-ap` (packet loss) and `branch-vpn`
(high latency) stay **degraded**.

Without uv: `python -m venv .venv`, activate it, `pip install -e . pytest httpx2 ruff`,
then `netmonlt`.

Options: `netmonlt --inventory my.yaml --db data/my.db --port 9000`
(it binds to 127.0.0.1 by default; it has no authentication, so don't expose it).

## Device inventory

`config/devices.sim.yaml` is the source of truth for which devices exist:

```yaml
mode: simulated
poll_interval_s: 5
devices:
  - name: access-switch
    address: 192.0.2.10          # 192.0.2.0/24 is reserved for documentation
    type: switch
    sim: {rtt_ms: 3, jitter_ms: 0.5, outage_start_s: 30, outage_duration_s: 30}
```

Unknown keys and invalid values are rejected at startup with a message naming the
offending device. Devices removed from the file are removed from the database, with
their history.

## How it works

```
devices.yaml ─► Inventory ─► Collector ─► Prober (interface) ─► SimulatedProber
                                 │                              (real prober: planned)
                                 ▼
                              SQLite ◄── FastAPI ◄── dashboard (polls /api/devices)
```

- **Prober** is the only thing that measures a device. The collector, storage and API
  don't know whether results are simulated or real.
- **Status** is derived from three pings per check: `down` if none reply, `degraded`
  at 20% loss or more, or average latency of 150 ms or more, otherwise `up`.
- **Simulation** is deterministic: each device has a seeded random generator, and outages
  are scripted relative to start time.
- A probe *error* (a broken prober) is logged and not recorded, so it is never mistaken for
  a device being down.

API: `GET /api/devices`, `GET /api/devices/{name}/history?limit=N`, `GET /healthz`,
interactive docs at `/docs`.

## Development

```powershell
uv run pytest -q
uv run ruff check .
```

## Known limitations

- Check history is never pruned (fine for a demo, not for long-running use).
- The dashboard shows current status only, with no charts or alerts yet.
- Tested on Linux so far; CI runs the suite on Ubuntu and Windows.

## Roadmap

1. Alerting: threshold rules with a pending/firing/resolved state machine.
2. Latency history charts on the dashboard.
3. Real ICMP probing (opt-in): `SimulatedProber` is swapped for a real prober, gated by a
   local scope file listing the devices you are authorised to monitor.
4. SNMP interface counters (throughput, errors), with ICMP-only fallback when no
   credentials exist.
