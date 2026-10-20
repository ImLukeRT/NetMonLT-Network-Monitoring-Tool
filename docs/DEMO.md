# NetMonLT demo walkthrough (about 5 minutes)

Everything here uses the default **simulated** devices: no network, no admin rights and no
hardware. The dashboard is labelled **SIMULATED DATA** throughout.

**Set up** (once, from the repository root; see the [README](../README.md#quick-start-simulated-demo)):

```powershell
uv sync
uv run netmonlt
```

Open <http://127.0.0.1:8000> straight away. The scripted outage happens 30 seconds after the app
starts, so don't leave it for later (if you miss it, the history is still there; see step 5).

## What happens, and when

| Time since start | What happens | Where to look |
|---|---|---|
| 0 to about 15 s | Seven devices appear. The first checks only establish a baseline, so there are no alerts yet. | Device table |
| 0 s onward | `wifi-ap` (40% packet loss) and `branch-vpn` (about 170 ms) show **degraded** by design. | Device table |
| 30 s | `access-switch` stops replying (the scripted outage). | Device table turns red; chart zone turns red |
| about 40 s | A **critical** alert: *"access-switch is DOWN: no reply to 3 pings (was up for 30s)"*. It waits for 3 consecutive failed checks. | Recent alerts, and the console |
| 60 s | The switch answers again. | Device table |
| about 70 s | An **info** alert: *"access-switch recovered ... (was down for 30s)"*, again after 3 consecutive good checks. | Recent alerts |
| about 50 s onward | **Network quality** grades appear (10 checks are needed first): `voice-gateway` Fair because of jitter, `wifi-ap` Poor (loss), `branch-vpn` Poor (latency), the rest Excellent. | Network quality table |
| about 70 s | `access-switch` is Poor (its outage is counted as loss), then Fair after about 10 minutes, and Excellent again (via Good) as the outage leaves the 15-minute window. | Network quality table, loss chart |
| 60 s | **Wi-Fi (simulated):** the simulated signal drops from about 80% to about 25%. | Wi-Fi signal panel |
| about 75 s | A **Low signal** banner appears (3 consecutive readings at or below 40%), and the chart line crosses the dashed limit. | Wi-Fi signal panel |
| 100 s | The simulated signal recovers and the banner clears. | Wi-Fi signal panel |
| 130 to 150 s | The simulated link **disconnects**: a red "not connected" zone, no signal figure (never 0%), then it reconnects. | Wi-Fi signal panel |
| 30 to 60 s | SNMP to `access-switch` times out too (labelled as such); its interface rows dim and the last good values are kept, not zeroed. ICMP and the charts are unaffected. | Interfaces (SNMP) panel |
| about 75 s | `core-router` Gi0/1 crosses the 64-bit counter limit. The rate carries on unbroken instead of spiking or going negative. | Interfaces panel |
| about 100 s | `edge-firewall` vpn0 stops being reported and is shown as **absent**, with no rate. | Interfaces panel |
| about 130 s | `edge-firewall` "restarts": every counter and sysUpTime reset. One poll shows no rate (counter reset), then rates resume. | Interfaces panel |

## Steps

1. **Watch the device table.** Statuses, latency and loss refresh every 3 seconds. The "N s ago"
   labels count up every second in the browser from the age the API reported, without extra requests.
2. **See the outage (about 30 to 40 s).** `access-switch` goes DOWN with 100% loss and the
   critical alert appears. Meanwhile the lossy `wifi-ap` raises only the occasional alert: a change
   needs 3 consecutive checks to agree, so single lost packets don't cause alert noise.
3. **See the recovery (about 60 to 70 s).** The row turns green and the recovery alert appears.
4. **Open the history chart.** Under **Latency history**, `access-switch` is selected by default.
   The blue line stops where the outage starts, a **red zone** covers it, and the strip below
   shows red cells. Hover the red zone: *"No latency: no reply to any ping, Status: down"*. It is
   never drawn as 0 ms. Try the **1 h** and **24 h** buttons.
5. **Show NO DATA, which is not the same as DOWN.** Press **Ctrl+C** in the terminal, wait about a
   minute, then run `uv run netmonlt` again and refresh the page. The minute in between is a
   grey **hatched zone**. Hover it: *"No measurements ... not the same as down"*. There is no red
   zone, and **no alert was invented** for it. About 30 seconds later the scripted outage replays
   (a second red zone and alert), because the simulation restarts with the app.

   ![Chart with a red DOWN zone and a hatched NO DATA zone](images/down-vs-no-data.png)

6. **Interface statistics (SNMP, simulated).** Scroll to **Interfaces (SNMP)**. It is badged
   **SIMULATED SNMP DATA**. Each device lists its interfaces with status, in/out rates and
   error/discard counts per poll. Wait for the events in the table above: the access-switch
   timeout (30 to 60 s), the rollover (75 s), vpn0 disappearing (100 s) and the restart (130 s).
   A rate is shown as a dash with a tooltip whenever it cannot be calculated honestly (first
   sample, counter reset, gap): it is never invented. Raw counters are not exposed by the API
   (`/api/interfaces`).

   ![Interfaces panel with simulated SNMP statistics](images/interfaces.png)

7. **Network quality.** Scroll to **Network quality** (after about a minute). Each device has
   a latency, jitter and loss figure and a grade, with the **basis** column saying which figure
   limits it. `voice-gateway` has low latency and no loss but is **Fair** because of its jitter,
   which is the point of grading on more than latency. Pick `access-switch` in the device
   selector: the **loss chart** shows the outage as a red bar, and the **jitter line breaks**
   there instead of dropping to zero, because jitter cannot be measured without two consecutive
   replies. Use the window buttons to change the period for both the table and the charts. A
   device with too little data shows **Unknown** and says why; nothing is guessed.

   The grades come from thresholds in `config/devices.sim.yaml` (`quality:`). They are this
   app's own heuristics, not a QoS standard, and ICMP measurements cannot show that DSCP marking
   or traffic shaping works.

8. **Wi-Fi signal (simulated).** Scroll to **Wi-Fi signal (this computer)**, badged **SIMULATED
   WI-FI DATA**. The table says where each figure comes from ("simulated" here; on a real
   Windows PC "reported by Windows WLAN API"). The network name is masked (`H*******`) by
   default. RSSI is shown as its own row, labelled simulated: a real run shows a dash unless the
   driver reports an RSSI, and the app never calculates dBm from the percentage. Watch the dip
   and the low-signal banner (about 60 to 100 s), then the disconnection (about 130 s): it is a
   red zone, not a zero. This panel is local-only: it never scans for networks. On an
   Ethernet-only PC in real mode it would instead explain that there is no wireless adapter.

9. **Optional: the real mode, safely.** `uv run netmonlt --inventory config/devices.real.example.yaml`
   pings only your own computer (loopback). The badge changes to **LIVE · ICMP** and the data goes
   to a separate database. Latencies are tiny (well under 1 ms) because it is local.

To start over, stop the app and delete the `data/` folder (`Remove-Item -Recurse data` in
PowerShell, `rm -r data` elsewhere).

## Points to make

- **Measurement is separate from everything else.** The simulator and the real ICMP prober
  implement the same interface, so the collector, alerts, database and dashboard don't change.
- **Alerts need confirmation and survive restarts.** State changes are confirmed over 3 checks and
  stored, so noisy links don't flood the log and a restart neither repeats nor invents alerts.
- **"No data" is not "down".** A stopped monitor or a failing probe is shown as missing data, never
  as an outage, and never as a 0 ms reading.
- **SNMP is isolated and honest.** It has its own polling loop, so a failing device cannot delay
  ICMP or alerts. Rates come from counter differences over elapsed time, handling rollover,
  resets and missing interfaces. The real SNMPv3 client reads credentials from environment
  variables only; it was tested against a software agent on loopback, not network hardware.
- **Wi-Fi telemetry is honest about its sources.** Each figure is reported, simulated or
  unavailable. Nothing is estimated, RSSI is never derived from a percentage, and the SSID is
  masked before it is stored. It is opt-in and reads only this computer's own adapter.
- **Quality figures are defined, not vague.** Jitter is the mean absolute difference between
  consecutive successful checks' average round-trip times (not RFC 3550, not one-way delay).
  Loss is probes unanswered over probes sent. Missing data is a gap, never a zero.
- **Performance is not QoS.** The grades describe how ICMP behaves. They do not prove that any
  prioritisation, shaping or queueing is in place.
- **Real monitoring is opt-in and bounded.** It needs a separate file, `authorized: true` per
  device and single-host targets only. There is no scanning or discovery.
