# NetMonLT

[![CI](https://github.com/ImLukeRT/Test-/actions/workflows/ci.yml/badge.svg)](https://github.com/ImLukeRT/Test-/actions/workflows/ci.yml)

**A lightweight network monitoring platform in Python.** NetMonLT polls devices for
reachability and latency, reads interface traffic and error counters over SNMP, stores the
history in SQLite, grades network quality (latency, jitter and packet loss), raises alerts when
a device changes state, and serves a live web dashboard with historical charts. It runs fully offline with simulated devices, and can optionally
monitor real devices over ICMP and SNMPv3, restricted to targets you list explicitly and are
authorised to monitor.

![NetMonLT dashboard during a simulated outage: the access-switch is DOWN, with its latency history and alert log](docs/images/dashboard-overview.png)

*Simulated data (the SIMULATED DATA badge is always shown in that mode). The chart shows two
outages (red) and a period when the app was stopped (hatched).*

This is a portfolio project built to show how a monitoring system separates **measurement**
(probes), **storage**, **alerting** and **presentation**, and how it avoids two classic
monitoring mistakes: false alarms from noisy checks, and mistaking "no data" for "down".

## Key features

- **Simulated devices by default.** Deterministic, scripted outages, packet loss and high
  latency, so it demos anywhere with no hardware, network access or admin rights.
- **Opt-in real ICMP monitoring.** Runs the operating system's `ping`, only against devices
  marked `authorized: true`. Never scans or discovers: ranges, CIDR blocks, wildcards and
  multicast/broadcast addresses are rejected.
- **Pluggable probe interface.** The collector, database, alerts and dashboard don't know
  whether results are simulated or real.
- **SNMP interface statistics.** Per-interface status, in/out traffic rates and error/discard
  counts from the standard IF-MIB. Simulated by default (including counter rollover, a device
  restart and a vanishing interface); real polling is opt-in, SNMPv3 only, with credentials taken
  from environment variables and never written to a file, the database, the API or the log.
- **Network quality.** Average latency, jitter and packet loss per device, an Excellent / Good /
  Fair / Poor / Unknown grade from thresholds you can configure, and historical jitter and loss
  charts. All calculated from the ICMP checks already in SQLite, with the formulas stated and
  missing data never counted as zero. It measures performance; it does not verify or enforce QoS.
- **Alerts without alert storms.** A device's state changes only after 3 consecutive checks
  agree. Alerts are stored in SQLite, survive restarts, and never repeat while the state is
  unchanged.
- **Honest history.** The latency chart distinguishes **DOWN** (checked, no reply), **NO DATA**
  (nothing was measured) and a genuine **0 ms** reply. Long windows are downsampled on the
  server.
- **Dashboard with no framework.** Plain HTML, JavaScript and hand-built SVG, no CDN, so it works
  offline. Shows stale data and API failures clearly.
- **Tested.** 645 automated tests, run with lint on Ubuntu and Windows in CI. Tests never touch a
  network and never need an SNMP device.

## Quick start (simulated demo)

Needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`
on Windows). No Docker, admin rights or network access needed.

```powershell
git clone https://github.com/ImLukeRT/Test-.git
cd Test-
uv sync
uv run netmonlt
```

Open <http://127.0.0.1:8000>. The simulated `access-switch` fails 30 seconds after start (a
critical DOWN alert appears at about 40 s) and recovers at 60 s (a recovery alert at about 70 s).
`wifi-ap` (lossy) and `branch-vpn` (slow) stay degraded by design. The **Interfaces (SNMP)** table
fills in within about 10 s; see [SNMP interface monitoring](#snmp-interface-monitoring).

**Without uv:** `python -m venv .venv`, activate it (`.venv\Scripts\Activate.ps1` on Windows,
`source .venv/bin/activate` elsewhere), then `pip install -e . pytest httpx2 ruff` and `netmonlt`.
(Real SNMP polling also needs `pip install -e '.[snmp]'`; the simulated demo does not.)

Options: `--inventory PATH`, `--db PATH`, `--host ADDR` (default `127.0.0.1`), `--port N`
(default 8000). There is no authentication, so keep the default localhost binding.

**If something goes wrong**

- `inventory file not found`: run it from the repository root (where `config/` is), or pass
  `--inventory`.
- `address already in use`: another instance is running; stop it or use `--port 8001`.
- Chart says "No measurements yet": wait one polling interval (5 s).
- Start fresh: stop the app and delete the `data/` folder.

**Guided demo:** [docs/DEMO.md](docs/DEMO.md) is a 5-minute walkthrough of the outage, the
recovery alert, the history chart and the difference between DOWN and NO DATA.

![Latency chart: a red DOWN zone where the device did not reply, and a hatched NO DATA zone where nothing was measured](docs/images/down-vs-no-data.png)

## How it works

```mermaid
flowchart LR
    inv[("Device inventory<br/>YAML")] --> col
    subgraph probes["Probes (same interface)"]
        sim["SimulatedProber<br/>default, offline"]
        icmp["IcmpProber<br/>opt-in, system ping"]
    end
    inv --> snmp
    subgraph snmpc["SNMP clients (same interface)"]
        ssim["SimulatedSnmpClient<br/>default, offline"]
        spy["PySnmpClient<br/>opt-in, SNMPv3"]
    end
    col["Collector<br/>asyncio, 3 pings per check"] -->|"ping(device)"| probes
    probes -->|"PingResult"| col
    snmp["SNMP poller<br/>own loop and timeouts"] -->|"poll(device)"| snmpc
    snmpc -->|"interface counters"| snmp
    col -->|"status, latency"| db[("SQLite<br/>checks, alerts,<br/>interface state")]
    col -->|"each check"| alert["Alert state machine<br/>3 consecutive checks"]
    alert -->|"alerts, state"| db
    snmp -->|"rates, SNMP status"| db
    db -->|"stored checks"| qual["Quality figures<br/>latency, jitter, loss, grade"]
    qual --> api
    db --> api["FastAPI<br/>read-only JSON API"]
    api --> ui["Dashboard<br/>HTML, JS, SVG charts"]
```

| Component | File | Responsibility |
|---|---|---|
| Inventory | `inventory.py` | Loads and strictly validates the YAML (rejects unknown and duplicate keys, bad numbers, unsafe real-mode targets). |
| Probes | `probes/` | `Prober` interface; `SimulatedProber` and `IcmpProber` implement it. A probe that *cannot* measure raises `ProbeError`. |
| Collector | `collector.py` | Polls every device concurrently on a schedule, with a per-probe timeout. Stores each check, then feeds it to the alert state machine. |
| Alerts | `alerts.py` | Pure state machine: confirms state changes, raises alerts, handles restarts and gaps. |
| Interface maths | `ifstats.py` | Pure functions: IF-MIB table parsing and counter-to-rate maths (first sample, rollover, reset, gap, absent interfaces). |
| Network quality | `quality.py` | Pure functions over stored checks: mean latency, jitter, packet loss, grading and the bucketed series for the charts. Reads existing rows; stores nothing. |
| SNMP | `snmp.py`, `snmp_pysnmp.py`, `snmp_poller.py` | `SnmpClient` interface, the deterministic simulated client, the real SNMPv3 client (optional dependency) and a polling loop that is separate from the ICMP collector. |
| Storage | `storage.py` | SQLite with plain SQL: one shared connection, a lock, WAL mode. Windowed and downsampled history queries. |
| API and dashboard | `api.py`, `templates/dashboard.html` | Read-only JSON endpoints and a single-page dashboard that polls them. |

Design decisions worth knowing:

- **Probe interface.** One protocol separates *measuring* from everything else. That is what
  makes the simulator, the real prober and deterministic tests possible.
- **System `ping`, not raw sockets.** Raw ICMP needs administrator/root. The OS `ping` is already
  privileged on Windows, macOS and Linux, so it works for a normal user with no extra
  dependency. The cost is parsing its text output, which is why only Linux output is verified.
- **A probe error is not an outage.** If `ping` is missing, a hostname doesn't resolve or a probe
  times out, nothing is recorded as DOWN. The device becomes `unknown` or `stale` instead.
- **Confirmation before alerting.** One check is noisy (one lost packet in three marks it
  `degraded`). In the simulated demo, naive change-detection gives about 110 state changes per
  20 minutes against about 7 confirmed alerts (measured over 8 simulator seeds).
- **No invented history.** After a restart or a long gap the alert state is re-baselined
  silently, so a change that happened while the app was off never produces a fake alert.
- **Gaps are computed on the server,** so "no data" is testable in Python and the browser never
  has to guess.

Status rules: three pings per check; `down` if none reply, `degraded` at 20% loss or more or an
average of 150 ms or more, otherwise `up`. A device is `stale` when its newest check is older
than 3 poll intervals plus the 10 s probe timeout.

## Real ICMP monitoring (opt-in)

Real probing never happens by default. It needs a separate inventory file with `mode: real`, and
every device must carry `authorized: true`.

**Try it safely (pings only your own computer):**

```powershell
uv run netmonlt --inventory config/devices.real.example.yaml
```

The page is labelled **LIVE · ICMP**, and data goes to `data/netmonlt-real.db`, never into the
simulated database.

**Monitor your own devices:** copy the example to `config/devices.real.yaml` (git-ignored, so
private addresses stay out of the repo), list devices you own or are permitted to monitor, and
run `uv run netmonlt --inventory config/devices.real.yaml`:

```yaml
mode: real
poll_interval_s: 10        # minimum 5 in real mode
devices:
  - name: home-router
    address: 192.168.1.1   # one IPv4 address or one hostname
    type: router
    authorized: true       # required: your acknowledgement for this device
```

**Safety rules (enforced in code, at load time and again inside the prober):** opt-in per file
and per device; each address is one host, never a range; multicast, broadcast, `0.0.0.0` and
legacy shorthand such as `0xe0.0.0.1` are rejected; the address is passed to `ping` as a single
argument with no shell, so `-f` or `1.2.3.4; id` are refused; minimum 5 s poll interval, three
echo requests per check, a 1 s per-packet timeout and at most 64 pings at once. At startup it
prints the targets it will probe and pings loopback once; if `ping` doesn't work it stops with a
clear message. `authorized: true` is an acknowledgement, not a technical check.

| What `ping` shows | Result |
|---|---|
| All replies, low latency | `up` |
| Some lost, or average 150 ms or more | `degraded` |
| No replies, or "unreachable" | `down` |
| `ping` can't run, unresolvable host, no permission, unrecognised output | not recorded and logged: `unknown`/`stale`, never `down` |

**Platform notes:** Linux needs the `ping` command (`sudo apt install iputils-ping`). Windows
reports whole milliseconds (`time<1ms` is shown as 0.5 ms). On non-English Windows, a device that
does *not* answer is recognised from English phrases, so it may show `unknown`/`stale` instead of
`down`. On Windows a hostname that resolves to IPv6 may show `unknown`; prefer IPv4 addresses.
IPv4 and ICMP only: a firewall that drops ping makes a healthy host look down.

## Alerts

An alert is raised when a device's *confirmed* state changes: to `down` is `critical`, to
`degraded` is `warning`, to `up` (recovery) is `info`. Each alert stores the device, time,
severity, the change and a message such as
`access-switch is DOWN: no reply to 3 pings (was up for 30s)`, and is shown under **Recent
alerts** and logged to the console.

- **3 consecutive checks** must agree before the state changes, so with the 5 s demo interval an
  alert appears about 10 s after a change starts and a one- or two-check blip raises nothing.
- **Restarts:** the confirmed state is stored in SQLite, so a half-confirmed change keeps counting
  and nothing is alerted twice. A device with no stored state gets a silent baseline. If the last
  check is older than the stale limit, the old state is discarded and re-baselined silently.
  Probe errors never produce alerts.
- Not included: notifications (email, SMS, webhooks), acknowledgement, per-device rules,
  configurable thresholds and alert pruning.

## Latency history chart

Pick a device and a window (15 min default, 1 h, 6 h, 24 h, 7 d). The chart refreshes every 5 s.

| You see | It means |
|---|---|
| Blue line with a pale min-to-max band | The device replied; a reply of 0 ms sits on the 0 baseline with a marker. |
| Red zone, a break in the line, red strip cells | **DOWN:** the device was checked and did not reply. Never drawn as 0 ms. |
| Grey hatched zone | **NO DATA:** nothing was measured (app stopped, probes failing). Says nothing about the device. |

A gap is shown when two checks are more than 3 poll intervals plus 10 s apart. If a device's
history is shorter than the window, the chart shows just that history. Long windows are grouped
server-side into at most about 300 buckets; a bucket is `down` only if every check in it was
down, so a shorter outage appears as an amber cell (hover for "N of M checks were down").
Devices with no history, stale devices and API errors each have an explicit message.

## Network quality

The **Network quality** section turns the ICMP checks already stored in SQLite into three figures
and a grade per device. Nothing is probed or stored for it: the collector and database are
unchanged, and the figures are calculated on request from the `checks` table.

| Figure | Calculation | Not counted |
|---|---|---|
| **Latency** | Mean round-trip time of the replies received in the window (each check's average weighted by its replies). | Checks with no reply have no latency; they are never treated as 0 ms. |
| **Jitter** | Mean absolute difference, in ms, between the average round-trip times of two **consecutive checks that both received replies**: `mean(abs(rtt[n] - rtt[n-1]))`. | A pair is not formed across a down check or a monitoring gap (more than 3 poll intervals plus 10 s). |
| **Packet loss** | `100 * (probes sent - replies received) / probes sent`, over the checks actually made (3 probes each). | Time with no checks (app stopped, probe errors) is a **gap**: neither loss nor success. |

**What jitter is not.** It is a simple latency-variation figure, not the RFC 3550 interarrival
jitter used for RTP media, and ICMP echo measures the round trip, so it says nothing about
one-way delay. Each check averages up to 3 pings before the difference is taken, which smooths
short bursts: it will read lower than a per-packet figure on the same link.

### Grades

Each figure is graded against upper bounds (inclusive): up to the `excellent` bound is
Excellent, up to `good` is Good, up to `fair` is Fair, above that Poor. A device's grade is the
**worst of its three figures**, and the dashboard shows which one limits it.

| Figure | Excellent | Good | Fair | Poor |
|---|---|---|---|---|
| Latency (ms) | up to 30 | up to 80 | up to 150 | above 150 |
| Jitter (ms) | up to 5 | up to 15 | up to 30 | above 30 |
| Loss (%) | up to 0.5 | up to 2 | up to 5 | above 5 |

**These are application-level heuristics, not a QoS standard.** They are reasonable starting
points for a general-purpose link; a voice circuit, a satellite link and a LAN deserve different
numbers. Change them in the inventory:

```yaml
quality:
  latency_ms: {excellent: 30, good: 80, fair: 150}   # give all three, in increasing order
  jitter_ms: {excellent: 5, good: 15, fair: 30}
  loss_pct: {excellent: 0.5, good: 2, fair: 5}       # percent, at most 100
  window_s: 900            # default period (60 to 604800); the dashboard can change it
  min_checks: 10           # fewer checks in the window: Unknown
  min_jitter_pairs: 3      # pairs of consecutive successful checks needed to grade jitter
  max_gap_fraction: 0.5    # more of the window than this unmeasured: Unknown
```

The block is optional and every key is optional; bad values stop startup with a message.

### When the grade is Unknown (or Poor because nothing replies)

A grade is only given when there is enough trustworthy data, and the reason is shown:

| Situation | Result |
|---|---|
| The device has never been checked, or has no checks in the window | Unknown |
| The newest check is older than the stale limit (3 poll intervals plus 10 s) | Unknown, "stale" |
| Fewer than `min_checks` checks in the window | Unknown, "insufficient samples" (figures are still shown) |
| More than `max_gap_fraction` of the window has no checks | Unknown, "sparse history" |
| Fewer than `min_jitter_pairs` pairs of consecutive successful checks | Unknown (loss and latency are still shown) |
| Checks were made but **no probe was answered** | Poor, "no replies": latency and jitter show a dash, never 0 |
| Part of the window was down | Those checks count as loss; latency and jitter use only the replies |

A device whose history is shorter than the window is not penalised for the time before its
first check.

### Charts

Under the table, **Jitter over time** (line) and **Packet loss over time** (bars) use the device
and window chosen in Latency history. Each point or bar covers a bucket of at least six poll
intervals, so it holds enough probes for a percentage to mean something. Missing data is never
drawn as a value: empty buckets are left out, no-data periods are hatched, jitter that cannot
be measured breaks the line, and "measured, no loss" is a thin green line, which differs from
"not measured". Dashed lines mark your Excellent, Good and Fair limits. Hover for the exact
values and probe counts.

### Network performance is not QoS enforcement

This feature observes how ICMP echo traffic performs. It does **not** verify that DSCP marking,
traffic shaping, queue management or any other QoS policy is configured or working: ICMP may
be treated differently from the traffic a policy targets, the probes carry no particular DSCP
value, and routers often rate-limit or deprioritise ICMP. A good grade means "ping looked
healthy", not "the voice class gets priority".

### In the simulated demo

After a minute or two of data (10 checks are needed), `config/devices.sim.yaml` shows every
kind of grade, deterministically (the simulation is seeded):

| Device | Grade | Why |
|---|---|---|
| `core-router`, `edge-firewall`, `file-server` | Excellent | Low latency, steady, no loss |
| `voice-gateway` | Fair | About 30 ms latency (Excellent or Good) and no loss, but jitter of about 20 ms: **graded on jitter** |
| `access-switch` | Poor, then Fair, then Excellent | The scripted 30 s outage counts as loss: Poor at first, Fair once the window holds about 10 minutes of checks (under 5% loss), Excellent again (via Good) as the outage leaves the 15-minute window |
| `wifi-ap` | Poor | 40% simulated packet loss |
| `branch-vpn` | Poor | 170 ms latency |

## SNMP interface monitoring

For devices that have interfaces (routers, switches, firewalls), NetMonLT reads the standard
IF-MIB / IF-X-MIB counters and shows, per interface, the operational status, inbound and outbound
traffic rate, and error and discard counts under **Interfaces (SNMP)**.

![The Interfaces (SNMP) table with simulated data: traffic rates, one interface that is down, and a port with errors and discards](docs/images/interfaces.png)

*Simulated data (the panel says SIMULATED SNMP DATA). Real data is labelled SNMPv3.*

### Simulated (the default demo)

No hardware, network or credentials are needed. In `config/devices.sim.yaml`, the devices with
`sim.interfaces` get deterministic counters that also act out the awkward cases:

| Time after start | What happens | What you should see |
|---|---|---|
| 0 to 10 s | First poll | Interfaces listed, rates shown as "–" (a rate needs two polls) |
| 10 s | Second poll | Rates appear: `Gi0/1` about 180 Mbps in, the noisy `Gi0/4` shows 1 error and 1 discard per poll |
| 30 to 60 s | `access-switch` outage | Its SNMP polls time out: the header shows **SNMP timeout** and its rows are dimmed; the ICMP monitoring and charts keep working. On recovery the rates return |
| 75 s | `Gi0/1`'s 64-bit counters roll over | The rate stays correct (about 180 Mbps), with no gap or spike |
| 100 s | `edge-firewall`'s `vpn0` disappears | It stays listed as **absent**, with dashes and "last seen", and no made-up values |
| 130 s | `edge-firewall` "restarts" | One poll shows dashes (hover: counters were reset), then rates resume. Never a negative rate |

### Real devices (opt-in, SNMPv3 only)

SNMP polling is **off by default** and is only ever switched on per device, in real mode, for
devices that are also `authorized: true`. Nothing is discovered or scanned: only the configured
address is contacted, and only sysUpTime and nine named IF-MIB / IF-X-MIB columns are read (not
the whole MIB tree).

1. **Install the optional dependencies:** `uv sync --extra snmp` (or `pip install -e '.[snmp]'`).
2. **Give the device an SNMPv3 user** with read-only access. On a net-snmp agent this is the
   configuration the author tested against (use your own passphrases, at least 8 characters):

   ```
   createUser netmon SHA-256 "<authentication passphrase>" AES "<privacy passphrase>"
   rouser netmon authpriv
   ```

3. **Put the credentials in environment variables, never in a file in the repository.** Pick a
   prefix, for example `NETMONLT_SNMP_HOME_ROUTER`, and set three variables:

   ```powershell
   # PowerShell: type the secrets in instead of putting them in the command line or shell history
   $env:NETMONLT_SNMP_HOME_ROUTER_USER     = Read-Host "SNMPv3 user"
   $env:NETMONLT_SNMP_HOME_ROUTER_AUTH_KEY = Read-Host "Authentication passphrase"
   $env:NETMONLT_SNMP_HOME_ROUTER_PRIV_KEY = Read-Host "Privacy passphrase"
   ```

   ```bash
   # Linux / macOS
   read -rp  "SNMPv3 user: " NETMONLT_SNMP_HOME_ROUTER_USER
   read -rsp "Authentication passphrase: " NETMONLT_SNMP_HOME_ROUTER_AUTH_KEY; echo
   read -rsp "Privacy passphrase: " NETMONLT_SNMP_HOME_ROUTER_PRIV_KEY; echo
   export NETMONLT_SNMP_HOME_ROUTER_USER NETMONLT_SNMP_HOME_ROUTER_AUTH_KEY NETMONLT_SNMP_HOME_ROUTER_PRIV_KEY
   ```

   `config/snmp.env.example` is a template if you prefer a file; copy it to `config/snmp.env`
   (git-ignored) and load it into your shell yourself. NetMonLT never reads that file on its own.
4. **Enable SNMP for that device** in your own `config/devices.real.yaml`:

   ```yaml
   mode: real
   poll_interval_s: 10
   snmp_poll_interval_s: 30       # default 30, minimum 10
   devices:
     - name: home-router
       address: 192.168.1.1
       authorized: true
       snmp:
         enabled: true            # off unless you say so
         credentials_env: NETMONLT_SNMP_HOME_ROUTER   # the prefix from step 3
         security_level: authPriv # or authNoPriv
         auth_protocol: SHA256    # SHA, SHA224, SHA256, SHA384, SHA512
         priv_protocol: AES128    # AES128, AES192, AES256
   ```

5. `uv run netmonlt --inventory config/devices.real.yaml`. At startup it lists exactly which
   devices will be polled (never the credentials), and stops with a clear message if a variable is
   missing or the optional dependencies are not installed.

**Security rules (enforced in code and covered by tests)**

- **SNMPv3 only.** SNMPv1/v2c (a clear-text community string) is not supported, and `community`,
  `user`, `auth_key`, `priv_key` or `password` in the inventory are rejected with an explanation.
  MD5 and DES are refused as weak. `noAuthNoPriv` is not available.
- **Credentials live only in environment variables and in memory.** They are never written to the
  database, the API, the dashboard, error messages or the log, and `repr()` of the client and the
  credentials shows nothing sensitive. Only variable *names* appear in messages.
- **Only configured, authorised, single-host devices** are ever contacted, checked again inside the
  client. Ranges, multicast addresses and unconfigured names are refused before any traffic.
- **Failures are generic.** Library error text is never stored or logged; only a fixed category
  and message are (see below).
- Use a **dedicated read-only SNMPv3 user**, and restrict its view on the device if you can. Don't
  share screenshots or logs from a real network without checking them first.

**How rates are calculated.** Counters only increase, so a rate needs two samples: the counter
difference divided by the elapsed time, in bits per second. Nothing is invented:

| Situation | Result |
|---|---|
| First sample of an interface | No rate ("needs two polls") |
| Counter rolls over its 64-bit (octets) or 32-bit (errors, discards) range | The rate is computed across the wrap |
| Counter goes down without being near its maximum (cleared counters) | No rate for that poll, then it resumes from the new baseline |
| `sysUpTime` goes backwards (the device restarted) | No rate for any interface for that poll |
| Too long since the previous poll, or the clock moved backwards | No rate |
| Device has no 64-bit `ifHCInOctets` / `ifHCOutOctets` | Octet rates stay empty ("no 64-bit counters"); errors and discards still work |
| Interface stops being reported | Marked **absent**, last-seen time kept, no values; it is forgotten after 7 days |
| Interface reappears | Starts again from a first sample |
| A real 0 bps | Shown as `0 bps`, never as a dash |

**When SNMP fails** (one failing device never affects another, and none of it affects ICMP
monitoring, alerts or the latency chart, because SNMP has its own polling loop and timeouts):

| Shown as | Meaning |
|---|---|
| **timeout** | No response in time: device or service unreachable, a firewall, a wrong port, **or a wrong privacy key or protocol** (a device answers those with silence) |
| **auth failure** | Unknown user, wrong authentication key or protocol, or access denied |
| **unsupported** | The device answered but has no IF-MIB interface data |
| **error** | Anything unexpected (details are deliberately not shown) |

The last good interface data stays on screen, dimmed and marked stale after 3 poll intervals plus
30 s; it is never wiped and never replaced with guesses.

**Dependency.** Real polling needs [`pysnmp`](https://pypi.org/project/pysnmp/) 7.x plus
`cryptography`, in the optional `snmp` extra (they are also in the development group, so CI runs
their tests). pysnmp is the actively maintained, pure-Python SNMP library with SNMPv3 (USM)
support, SHA-2 authentication and AES privacy and an `asyncio` API; the alternative of writing SNMPv3
by hand would be a security risk. `cryptography` is needed because pysnmp fails every `authPriv`
request with "Ciphering services not available" without it (observed). Together they bring in
`pyasn1`, `cffi` and `pycparser`. The core install (simulated and ICMP) is unchanged.

**Limitations.** SNMPv3 only. Interface statistics only (no CPU, memory, traps or other MIBs). The
latest values and rates only, with no history charts yet. At most 64 interfaces per device (more
are ignored and counted). The nine columns are read one after another, so a poll makes about ten
requests. Rates use wall-clock time between polls, not the device's clock. IPv4 targets only, as
with ICMP.

## API

All endpoints are read-only JSON; interactive documentation is at `/docs`.

| Endpoint | Returns |
|---|---|
| `GET /api/devices` | Each device's latest check, its age and a `stale` flag. |
| `GET /api/devices/{name}/history?limit=N` | The newest N raw checks (1 to 1000, default 100). |
| `GET /api/devices/{name}/latency?window_s=N&max_points=M` | Chart data: points (latency is `null` for a down check), `gaps`, first/last check, `stale`. `window_s` 60 to 604800 (default 900), `max_points` 10 to 1000 (default 300). |
| `GET /api/quality?window_s=N` | Network-quality summary for every device over the last `window_s` seconds (60 to 604800, default `quality.window_s`): `level` (`excellent`, `good`, `fair`, `poor` or `unknown`), `reason`, per-figure `grades`, `limiting`, `latency_ms`, `jitter_ms`, `loss_pct` (each `null` when it cannot be calculated), check and probe counts, `jitter_pairs`, `gap_fraction`, `stale`, plus the thresholds, the definitions and a scope statement. |
| `GET /api/devices/{name}/quality?window_s=N&max_points=M` | One device's summary plus `points` (time buckets with `loss_pct`, `jitter_ms`, probe counts; empty buckets are omitted) and `gaps`. |
| `GET /api/interfaces?device=NAME` | Latest SNMP interface statistics per SNMP device (optionally one): SNMP `status` (`ok`, `pending`, `timeout`, `auth_failure`, `unsupported`, `error`), safe `message`, `stale`, and per interface `oper_status`, `present`, `in_bps`, `out_bps`, error and discard increases since the last poll, and a `rate_note` saying why a rate is `null`. Raw counters are never returned; `simulated` says whether the data is simulated. |
| `GET /api/alerts?limit=N` | Newest alerts first (1 to 500, default 50) with `severity`, `from_status`, `to_status`, `message`, `age_s`. |
| `GET /healthz` | `{"ok": true}` |

## Configuration reference

```yaml
mode: simulated            # simulated (default) or real
poll_interval_s: 5         # default 5; minimum 0.1 simulated, 5 real
snmp_poll_interval_s: 30   # interface polling; default 30; minimum 1 simulated, 10 real
quality:                   # optional; grading thresholds and minimum data (see Network quality)
  jitter_ms: {excellent: 5, good: 15, fair: 30}   # also latency_ms and loss_pct
  min_checks: 10           # also window_s, min_jitter_pairs, max_gap_fraction
devices:
  - name: access-switch    # 1-64 chars: letters, digits . _ -
    address: 192.0.2.10    # simulated: any label without spaces; real: one IPv4 address or hostname
    type: switch           # free text, default "host"
    authorized: true       # required in real mode, ignored in simulated mode
    sim:                   # simulated mode only (ignored in real mode)
      rtt_ms: 3            # defaults: rtt_ms 10, jitter_ms 1, loss 0 (0 to 1)
      jitter_ms: 0.5
      loss: 0.0
      outage_start_s: 30   # seconds after start; outage_duration_s must accompany it
      outage_duration_s: 30
      reboot_at_s: 130     # counters and sysUpTime restart at this time
      interfaces:          # simulated SNMP interfaces (up to 64)
        - {name: Gi0/1, in_mbps: 180, out_mbps: 60, status: up,
           in_errors_per_min: 0, out_errors_per_min: 0, in_discards_per_min: 0,
           out_discards_per_min: 0, wrap_at_s: 75, disappears_at_s: 100}
    snmp:                  # real mode only (off unless enabled); SNMPv3, see the SNMP section
      enabled: false
      credentials_env: NETMONLT_SNMP_ACCESS_SWITCH   # prefix of <PREFIX>_USER / _AUTH_KEY / _PRIV_KEY
      security_level: authPriv   # or authNoPriv; auth_protocol, priv_protocol, port, timeout_s, retries
```

Unknown or duplicate keys and invalid values are rejected at startup with a message naming the
device. Removing a device from the file also removes its history and alerts. The shipped
examples are `config/devices.sim.yaml` and `config/devices.real.example.yaml`; credentials never
belong in either (`config/snmp.env.example` shows the variable names).

## Testing

```powershell
uv run pytest -q
uv run ruff check .
```

645 tests cover the alert state machine (transitions, repeated polling, recovery, restarts,
persistence, schema upgrade), the ICMP prober and its safety rules (with a fake command runner
and recorded or constructed `ping` output), inventory validation, the collector's error handling,
storage, the history and downsampling queries, the API and the CLI, and for SNMP: the counter
and rate maths (rollover, reset, restart, gaps, unsupported counters, absent interfaces), table
parsing, the simulated client, configuration and credential rules, storage (including counters
above signed 64-bit), failure handling and isolation from ICMP, the interface API, and the real
SNMP client's logic with its pysnmp calls replaced by fakes. For network quality: the jitter,
latency and loss maths (consecutive pairs, down checks, gaps, weighting, a reference
implementation on random data), grading and thresholds, the Unknown cases, the series buckets,
the `quality:` configuration rules, the storage query and both endpoints, and the simulated
demo's deterministic grades. None touch a network, run a real
`ping` or need an SNMP device.

**What has and hasn't been verified**

| Claim | Evidence |
|---|---|
| Setup commands | Run from a fresh clone of `main` on Linux with Python 3.13 and 3.11; the pip-only install in a fresh virtual environment. |
| Demo timeline | Observed on real runs of the app (outage at 30 s, alerts at about 40 s and 70 s, a one-minute stop shown as NO DATA). |
| Tests and lint | Pass on Ubuntu and Windows in CI, and locally on Python 3.13 and 3.11. |
| Real ICMP | Run against the real `ping` on Linux (iputils), loopback only. |
| Real SNMPv3 | Run end to end (authPriv, SHA-256 and AES-128) against a **net-snmp agent on loopback on Linux**, including wrong key, wrong user and wrong privacy key, and a search of the log, API and database for the passphrases. **Not tested against real network hardware, other SNMP agents, other protocol combinations, or on Windows or macOS.** |
| Windows and macOS `ping` parsing | Tested only against sample output built from each tool's documented format; **not run on real Windows or macOS.** |
| Network quality | Grades observed on real runs of the simulated demo (see the table above) and on real ICMP against loopback on Linux (sub-millisecond latency, 0 loss). The Unknown, stale, sparse-history, no-reply, no-jitter-pairs and API-error displays were checked in Chromium by serving the real API's responses for each scenario to the page. **Not measured on real WAN or wireless links, and the grading thresholds are untuned heuristics.** |
| Dashboard | Checked by hand in headless Chromium (desktop, 375 and 390 px wide, dark mode). **Not tested in Firefox, Safari or Edge;** the one-second "ago" labels have Node.js tests, the rest of the JavaScript has none. |
| PowerShell commands | Not run on Windows by the author. |

## Project layout

```
config/            devices.sim.yaml, devices.real.example.yaml
docs/              DEMO.md walkthrough and screenshots
src/netmonlt/
  __main__.py      CLI entry point
  inventory.py     YAML loading and validation
  collector.py     polling loop
  alerts.py        alert state machine
  quality.py       latency, jitter, packet-loss maths and grading (pure functions)
  ifstats.py       interface counter maths and IF-MIB table parsing
  snmp.py          SNMP client interface, credentials, simulated client
  snmp_pysnmp.py   real SNMPv3 client (optional dependency)
  snmp_poller.py   SNMP polling loop, separate from the ICMP collector
  storage.py       SQLite queries
  api.py           FastAPI app
  domain.py        shared types and status rules
  probes/          base.py, simulated.py, icmp.py
  templates/       dashboard.html (inline JS and SVG)
tests/             pytest suite
```

Built with Python 3.11+, FastAPI and uvicorn, the standard-library `sqlite3`, PyYAML and Jinja2;
real SNMPv3 uses the optional `pysnmp` and `cryptography`; tests with pytest; lint with ruff. No
JavaScript framework or build step.

## Limitations

- **Network quality:** jitter is calculated from each check's average of 3 pings (stored data
  only), so it is smoother than per-packet jitter and is neither RFC 3550 jitter nor one-way
  delay. Loss resolution depends on the probes in the window (3 per check). ICMP can be
  rate-limited or treated differently from real traffic, so grades describe how ping behaves,
  not application experience or QoS policy. Default thresholds are generic heuristics. Quality is
  not used for alerts, and each request reads the window's checks (about 120,000 rows for 7 days
  at 5 s), which is fine for a lab but not for a large estate.
- **Scope:** ICMP reachability and latency, plus SNMPv3 interface counters. No CPU, memory,
  traps, SNMPv1/v2c or other MIBs, and interface figures are the latest values only (no history
  or charts, and no alerts on them yet).
- **Data:** history and alerts are never pruned (fine for a demo, not for long-running use).
- **Security:** no authentication or HTTPS. In real mode the page lists your device addresses, so
  keep the default `127.0.0.1` binding.
- **Alerts:** shown and logged only; no notifications or acknowledgement. Thresholds are fixed
  (3 pings, 20% loss, 150 ms, 3 confirming checks).
- **Scale:** one `ping` process per device per check, at most 64 at once. It is meant for a home
  or lab network, not thousands of devices.
- **Charts:** no zoom, pan or export; alerts aren't marked on the chart; axis times use the
  browser's time zone; the vertical scale starts at 0, so one spike flattens the rest.
- **Platforms:** see the verification table above for what is and isn't proven on Windows and
  macOS.

## What I would build next

1. Interface history charts and alerts on interface status or error rates.
2. Alert notifications (for example a webhook) and acknowledgement.
3. Data retention, and authentication before any non-localhost use.
