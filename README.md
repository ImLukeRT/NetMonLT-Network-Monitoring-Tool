# NetMonLT

A lightweight network monitoring platform: device inventory in YAML, a background
collector that measures reachability and latency, SQLite persistence, and a live web
dashboard. It has two modes:

- **Simulated (default):** fake devices with scripted behaviour, so it can be demonstrated
  with no networking equipment, no network access and no special permissions.
- **Real ICMP (opt-in):** pings devices you list explicitly and are authorised to monitor.

> Status: early MVP. ICMP reachability and latency, with state-change alerts. SNMP and charts
> are not built yet (see [Roadmap](#roadmap)).

## Quick start (simulated demo)

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
(high latency) stay **degraded**. The page is labelled **SIMULATED DATA**.

Without uv: `python -m venv .venv`, activate it, `pip install -e . pytest httpx2 ruff`,
then `netmonlt`.

Options: `netmonlt --inventory my.yaml --db data/my.db --port 9000`
(it binds to 127.0.0.1 by default; it has no authentication, so don't expose it).

## Real ICMP monitoring (opt-in)

Real probing never happens by default. It needs a different inventory file with
`mode: real`, and every device in it must be acknowledged with `authorized: true`.

### Try it safely first (monitors only your own computer)

```powershell
uv run netmonlt --inventory config/devices.real.example.yaml
```

This pings `127.0.0.1` (loopback), so no traffic leaves your machine. The page is labelled
**LIVE · ICMP**.

### Monitor your own devices

1. Copy the example (the copy is git-ignored, so your private addresses stay out of the repo):

   ```powershell
   Copy-Item config\devices.real.example.yaml config\devices.real.yaml
   ```

   (macOS/Linux: `cp config/devices.real.example.yaml config/devices.real.yaml`)

2. Edit it and list only devices you own or are explicitly permitted to monitor:

   ```yaml
   mode: real
   poll_interval_s: 10        # minimum 5 in real mode
   devices:
     - name: home-router
       address: 192.168.1.1   # one IPv4 address or one hostname
       type: router
       authorized: true       # required: your acknowledgement for this device
   ```

3. Run it: `uv run netmonlt --inventory config/devices.real.yaml`

At startup it prints exactly which targets will be probed, and checks that `ping` works by
pinging loopback once. If that fails it stops with a clear message instead of starting.

### Safety rules (enforced in code)

- **Opt-in twice:** `mode: real` for the file, and `authorized: true` on every device.
- **Explicit targets only:** nothing is scanned or discovered. Each address must be a single
  IPv4 address or hostname. CIDR ranges, wildcards, lists, IP ranges, multicast, broadcast
  and `0.0.0.0` are rejected when the file is loaded and again inside the prober.
- **No shell, no option injection:** the address is validated and passed to `ping` as a single
  argument, so an address like `-f` or `1.2.3.4; id` is refused.
- **Gentle:** minimum poll interval of 5 s, three echo requests per check, 1 s per-packet timeout.
- **Separate history:** real mode stores data in `data/netmonlt-real.db`, never in the simulated
  database, so demo and real results can't be mixed up.

`authorized: true` is an acknowledgement, not a technical check: it is on you to only monitor
devices you are permitted to.

### How results are interpreted

NetMonLT runs the operating system's `ping` (3 echo requests per check) and returns the same
result as the simulator, so the collector, database and dashboard are unchanged:

| What happens | Result |
|---|---|
| All replies, low latency | `up` |
| Some replies lost, or avg latency 150 ms or more | `degraded` |
| No replies, or "host unreachable" | `down` |
| `ping` can't run, host name can't be resolved, no permission, unrecognised output, or timeout | **not recorded**; logged as an error. The device shows `unknown` (never measured) or `stale` (last result too old). It is never shown as `down`. |

### Why the system `ping` command?

Sending ICMP from Python needs administrator/root rights (raw sockets). The system `ping`
tool is already privileged on Windows, macOS and Linux, so this works as a normal user with no
extra dependency. The cost is that its text output has to be parsed.

### Platform notes and limitations

- **Linux:** needs the `ping` command (`sudo apt install iputils-ping` if missing). Minimal
  containers often lack it or lack permission to send ICMP; the startup self-test will say so.
  The per-packet timeout is passed in whole seconds.
- **Windows:** built in, no admin rights needed. Windows reports whole milliseconds, so
  `time<1ms` is shown as 0.5 ms and latencies are coarse.
- **macOS:** built in, no admin rights needed.
- **Verification status:** parsing was verified against real output from Linux (iputils) only.
  The Windows and macOS parsers are tested against sample output built from each tool's
  documented format, and the CI test suite on Windows runs mocked tests, not a real `ping`.
  Please report any output that isn't recognised.
- **Non-English Windows:** replies are recognised by their `TTL=` field and latency by the
  `time=Nms` / `time<1ms` pattern (tested with a German-style sample). Where the `ms` unit is
  itself translated, the device is still `up` but latency shows as `–`. A device that does
  *not* answer is recognised from English phrases ("timed out", "unreachable", "Received = 0"),
  so on a localised system it may show `unknown`/`stale` instead of `down`.
- **IPv4 only:** IPv6 addresses are rejected. Hostnames are resolved by the OS `ping`; prefer
  IPv4 addresses. On Windows a hostname that resolves to IPv6 (for example `localhost`) prints
  replies without a `TTL=` field, which isn't recognised, so such a device shows `unknown`.
  This has not been tested on Windows.
- **ICMP only measures reachability and latency.** A firewall that drops ICMP makes a healthy
  device look `down`; a host that answers ping can still have a failed service.
- **Scale:** one `ping` process per device per check (about 2-3 s each). Up to 64 run at once;
  checked with 60-150 fake devices, not with a real network of that size. It is meant for a
  home or lab network, not thousands of devices.
- **Latency above ~1 s** per packet is reported as lost, because the timeout is fixed.

## Device inventory

The YAML file is the source of truth for which devices exist. Simulated example
(`config/devices.sim.yaml`):

```yaml
mode: simulated            # the default
poll_interval_s: 5
devices:
  - name: access-switch
    address: 192.0.2.10    # 192.0.2.0/24 is reserved for documentation
    type: switch
    sim: {rtt_ms: 3, jitter_ms: 0.5, outage_start_s: 30, outage_duration_s: 30}
```

Unknown keys, duplicate keys and invalid values are rejected at startup with a message naming
the offending device. Devices removed from the file are removed from the database, with their
history. `sim:` is ignored in real mode and `authorized` is ignored in simulated mode, so
switching modes is a one-line change.

## How it works

```
devices.yaml ─► Inventory ─► Collector ─► Prober (interface) ─┬─► SimulatedProber (default)
                                 │  │                         └─► IcmpProber ─► system ping
                                 │  └─► alert state machine
                                 ▼            │
                              SQLite ◄────────┘
                                 ▲
                              FastAPI ◄── dashboard (polls /api/devices and /api/alerts)
```

- **Prober** is the only thing that measures a device. The collector, storage and API
  don't know whether results are simulated or real.
- **Status** is derived from three pings per check: `down` if none reply, `degraded`
  at 20% loss or more, or average latency of 150 ms or more, otherwise `up`. With three pings
  per check, any single lost packet marks a single check `degraded`; alerts ignore such
  blips (see [Alerts](#alerts)).
- **Simulation** is deterministic: each device has a seeded random generator, and outages
  are scripted relative to start time.
- A probe *error or timeout* (a broken or hung prober) is logged and not recorded, so it is
  never mistaken for a device being down. The dashboard marks devices whose last result is
  too old as `stale`, using the server's clock.

API: `GET /api/devices`, `GET /api/devices/{name}/history?limit=N`,
`GET /api/alerts?limit=N`, `GET /healthz`, interactive docs at `/docs`. All endpoints are
read-only.

## Alerts

NetMonLT raises an alert when a device **changes state** and stays changed. Alerts are stored
in the same SQLite database, listed on the dashboard under **Recent alerts**, and also logged
to the console.

| Change | Severity |
|---|---|
| to `down` (from `up` or `degraded`) | `critical` |
| to `degraded` (from `up` or `down`) | `warning` |
| to `up` (recovery, from `down` or `degraded`) | `info` |

Each alert records the device, time, severity, the change (`from_status` → `to_status`) and a
message such as `access-switch is DOWN: no reply to 3 pings (was up for 30s)` or
`access-switch recovered: 3/3 replies, avg 2.57 ms (was down for 30s)`.

**No alert storms.** A single check is noisy (one lost packet marks it `degraded`), so a
device's state only changes after **3 consecutive checks** agree on the new status. While a device
stays in one state it raises nothing, however many times it is polled. With the 5 s demo
interval an alert therefore appears about 10 s after a change starts, and a one- or two-check
blip raises nothing. In the simulated demo, comparing each check with the previous one gives
about 110 state changes per 20 minutes (almost all from the deliberately lossy `wifi-ap`, which
has 40% packet loss); with confirmation it raises about 7 alerts: the 2 for the scripted
`access-switch` outage, plus a few for `wifi-ap`, whose link really does change state every few
minutes. (Measured over 8 simulator seeds.)

**Restarts.** The confirmed state is stored in the database, so a restart continues where it left
off: a change that was half-confirmed keeps counting, and nothing is alerted twice. NetMonLT
does not invent history it can't know:

- A device with no stored state (first run, or newly added) gets a silent *baseline* once 3
  consecutive checks agree. A device that is already down when monitoring starts is not an alert.
- If the last check is older than the stale limit (3 poll intervals plus the 10 s probe timeout),
  because the app was stopped or the device couldn't be measured for a while, the old state is
  discarded and re-baselined silently. A device that went down while the app was off, or came
  back while it was off, produces no alert for that change.
- Probe errors (no `ping`, unresolvable host, timeouts) are not outages and never produce alerts.

`GET /api/alerts?limit=N` (default 50, maximum 500) returns the newest alerts first, each with
`id`, `device`, `ts`, `severity`, `from_status`, `to_status`, `transition`, `message` and
`age_s`. Removing a device from the inventory also removes its alerts. Databases created
before alerting existed are upgraded automatically.

Not included: notifications (email, SMS, webhooks), acknowledgement, per-device rules, custom
thresholds or alert pruning.

## Development

```powershell
uv run pytest -q
uv run ruff check .
```

Tests never touch a network or run a real `ping`; the ICMP prober is tested with a fake
command runner and recorded/constructed sample output.

## Known limitations

- Check history is never pruned (fine for a demo, not for long-running use).
- Alert history is never pruned, and alerts are shown but not acknowledged or sent anywhere.
- The dashboard shows current status and recent alerts only, with no charts.
- No authentication on the dashboard or API. In real mode the page lists your device
  addresses and status, so keep the default `127.0.0.1` binding.

## Roadmap

1. Latency history charts on the dashboard.
2. SNMP interface counters (throughput, errors), with ICMP-only fallback when no
   credentials exist.
3. Alert notifications (for example a webhook) and acknowledgement.
