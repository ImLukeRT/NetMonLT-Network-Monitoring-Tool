"""Wi-Fi signal telemetry for THIS computer's own wireless adapter: data model, parsing, the
simulated source, low-signal warnings and the history series.

Scope and honesty rules (see also the README's table of what is real, simulated or unavailable):
  * It only reads the state of the adapter that is already there. It never scans for nearby
    networks, never records anyone else's SSID, never joins or leaves a network, and never
    collects the access point's MAC address (BSSID), the saved profile name or any key.
  * Every value is either REPORTED by the operating system or driver, SIMULATED, or UNAVAILABLE
    (None). Nothing is estimated: in particular the signal percentage is never converted into
    dBm. An RSSI in dBm is shown only when the OS reports one, and it carries its source.
  * "Signal quality %" is what Windows reports (0 to 100). It is a vendor-defined figure, not a
    standard unit, and not comparable between adapters. "Link rate" is the negotiated rate, not
    measured throughput.
  * The SSID is stored and shown according to `ssid_privacy` (masked by default) and is never
    written to the log.
"""

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from netmonlt.domain import WifiConfig, WifiSimProfile

# ---- connection state of the adapter
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_DISCONNECTING = "disconnecting"
STATE_NOT_READY = "not_ready"  # the radio is off or the adapter is not ready
STATE_UNKNOWN = "unknown"
STATES = (
    STATE_CONNECTED, STATE_DISCONNECTED, STATE_CONNECTING, STATE_DISCONNECTING,
    STATE_NOT_READY, STATE_UNKNOWN,
)

# ---- why a poll produced no observation (stable codes, shown in the API and dashboard)
STATUS_OK = "ok"
CATEGORY_NO_ADAPTER = "no_adapter"
CATEGORY_UNSUPPORTED_OS = "unsupported_os"
CATEGORY_UNAVAILABLE = "unavailable"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_PARSE = "parse_error"
CATEGORY_ERROR = "error"

WIFI_MESSAGES = {
    CATEGORY_NO_ADAPTER: "no wireless adapter found on this computer (an Ethernet-only machine?)",
    CATEGORY_UNSUPPORTED_OS: (
        "Wi-Fi monitoring is only implemented for Windows; this operating system is not "
        "supported yet"
    ),
    CATEGORY_UNAVAILABLE: (
        "the Windows WLAN service or tool is not available (no wireless adapter, the WLAN "
        "AutoConfig service is stopped, or netsh is missing)"
    ),
    CATEGORY_TIMEOUT: "the wireless query did not answer in time",
    CATEGORY_PARSE: (
        "the wireless tool's output was not recognised (netsh output is language-specific: "
        "use the wlanapi backend)"
    ),
    CATEGORY_ERROR: "unexpected error reading the wireless adapter",
}

SOURCE_SIMULATED = "simulated"
SOURCE_WLANAPI = "wlanapi"
SOURCE_NETSH = "netsh"
SOURCE_LABELS = {
    SOURCE_SIMULATED: "simulated",
    SOURCE_WLANAPI: "Windows WLAN API",
    SOURCE_NETSH: "netsh wlan",
    "auto": "Windows (WLAN API or netsh)",  # before the first reading: which one is not known yet
}

# Plausible ranges: anything outside is treated as unavailable rather than shown.
RSSI_MIN_DBM, RSSI_MAX_DBM = -120, 0
MAX_RATE_MBPS = 100_000.0
_PRINTABLE_RE = re.compile(r"[^\x20-\x7e -￿]")
_RADIO_RE = re.compile(r"^[0-9A-Za-z./ \-]{1,24}$")


class WifiError(Exception):
    """A poll could not be completed. `message` is safe to show, store and log."""

    def __init__(self, category: str):
        self.category = category
        self.message = WIFI_MESSAGES.get(category, WIFI_MESSAGES[CATEGORY_ERROR])
        super().__init__(f"{category}: {self.message}")


@dataclass(frozen=True)
class WifiReading:
    """One look at the adapter. Any field the source cannot provide is None, never a guess."""

    state: str
    ssid: str | None = None
    interface: str | None = None
    signal_pct: int | None = None  # signal QUALITY as reported by Windows, 0 to 100
    rssi_dbm: int | None = None  # only if the OS reports it; never derived from signal_pct
    rssi_source: str | None = None
    rx_mbps: float | None = None  # negotiated receive link rate, not throughput
    tx_mbps: float | None = None
    radio_type: str | None = None
    channel: int | None = None
    source: str = ""


def _clean_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = _PRINTABLE_RE.sub("", value).strip()[:limit]
    return text or None


def _int_in(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(value)
    return number if low <= number <= high and number == value else None


def _rate(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 1) if 0 < value <= MAX_RATE_MBPS else None


def normalize_reading(
    state, *, source: str, ssid=None, interface=None, signal_pct=None, rssi_dbm=None,
    rssi_source=None, rx_mbps=None, tx_mbps=None, radio_type=None, channel=None,
) -> WifiReading:
    """Build a reading from untrusted values (a child process's JSON, parsed text).

    Out-of-range or malformed values become None. Only a connected adapter carries link figures,
    so every other state has no SSID, signal, rates, RSSI, radio type or channel.
    """
    state = state if state in STATES else STATE_UNKNOWN
    name = _clean_text(interface, 64)
    if state != STATE_CONNECTED:
        return WifiReading(state=state, interface=name, source=source)
    rssi = _int_in(rssi_dbm, RSSI_MIN_DBM, RSSI_MAX_DBM)
    radio = radio_type if isinstance(radio_type, str) and _RADIO_RE.match(radio_type) else None
    return WifiReading(
        state=state,
        ssid=_clean_text(ssid, 64),
        interface=name,
        signal_pct=_int_in(signal_pct, 0, 100),
        rssi_dbm=rssi,
        rssi_source=_clean_text(rssi_source, 40) if rssi is not None else None,
        rx_mbps=_rate(rx_mbps),
        tx_mbps=_rate(tx_mbps),
        radio_type=radio,
        channel=_int_in(channel, 1, 233),
        source=source,
    )


def reading_from_mapping(data: Mapping, source: str) -> WifiReading:
    """A WifiReading from one interface object of the native helper's JSON output."""
    if not isinstance(data, Mapping):
        raise WifiError(CATEGORY_ERROR)
    return normalize_reading(
        data.get("state"), source=source, ssid=data.get("ssid"), interface=data.get("interface"),
        signal_pct=data.get("signal_pct"), rssi_dbm=data.get("rssi_dbm"),
        rssi_source=data.get("rssi_source"), rx_mbps=data.get("rx_mbps"),
        tx_mbps=data.get("tx_mbps"), radio_type=data.get("radio_type"),
        channel=data.get("channel"),
    )


def choose_reading(readings: Sequence[WifiReading]) -> WifiReading:
    """The adapter to report: a connected one if any, otherwise the first. None means no adapter."""
    if not readings:
        raise WifiError(CATEGORY_NO_ADAPTER)
    return next((r for r in readings if r.state == STATE_CONNECTED), readings[0])


# ------------------------------------------------------------------ privacy
def mask_ssid(ssid: str) -> str:
    """The first character and one asterisk per remaining character (at most 11)."""
    return ssid[0] + "*" * min(len(ssid) - 1, 11)


def apply_ssid_privacy(reading: WifiReading, mode: str) -> WifiReading:
    """The reading with its SSID shown as the privacy mode allows: full, masked or hidden."""
    if reading.ssid is None or mode == "full":
        return reading
    ssid = mask_ssid(reading.ssid) if mode == "masked" else None
    return replace(reading, ssid=ssid)


# ------------------------------------------------------------------ netsh output (English only)
# `netsh wlan show interfaces` prints "Label : value" lines whose labels are translated into the
# Windows display language. Only the English labels are recognised; anything else is reported as
# unparseable rather than guessed at. The native WLAN API backend has no such dependency.
_LINE_RE = re.compile(r"^\s*(.+?)\s+:\s?(.*?)\s*$")
_SIGNAL_RE = re.compile(r"^(\d{1,3})\s*%$")
_NETSH_STATES = {
    "connected": STATE_CONNECTED,
    "disconnected": STATE_DISCONNECTED,
    "disconnecting": STATE_DISCONNECTING,
    "associating": STATE_CONNECTING,
    "discovering": STATE_CONNECTING,
    "authenticating": STATE_CONNECTING,
    "ad hoc network formed": STATE_UNKNOWN,
}
_NETSH_NOT_READY = ("powered down", "radio is off", "hardware off")
_NETSH_NONE = ("there is no wireless interface",)
_NETSH_SERVICE = ("wlansvc", "wireless autoconfig service", "wlan autoconfig service")


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_netsh_interfaces(text: str) -> list[WifiReading]:
    """Readings from `netsh wlan show interfaces` output (English Windows only).

    Raises WifiError: no_adapter when there is no wireless interface, unavailable when the WLAN
    service is not running, parse_error when the output is not recognised (for example because
    Windows is not set to English). BSSID, GUID, MAC address and profile are ignored on purpose.
    """
    lowered = text.lower()
    if any(hint in lowered for hint in _NETSH_NONE):
        raise WifiError(CATEGORY_NO_ADAPTER)
    if any(hint in lowered for hint in _NETSH_SERVICE) and "not running" in lowered:
        raise WifiError(CATEGORY_UNAVAILABLE)

    blocks: list[dict[str, str]] = []
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1).strip().lower(), match.group(2)
        if key == "name":
            blocks.append({})
        if blocks:
            blocks[-1].setdefault(key, value)

    readings = []
    for block in blocks:
        state = _NETSH_STATES.get(block.get("state", "").strip().lower())
        if state is None:
            continue
        signal = _SIGNAL_RE.match(block.get("signal", "").strip())
        channel = _number(block.get("channel", ""))
        rssi = _number(block.get("rssi", ""))
        readings.append(normalize_reading(
            state, source=SOURCE_NETSH, ssid=block.get("ssid"), interface=block.get("name"),
            signal_pct=int(signal.group(1)) if signal else None,
            rssi_dbm=rssi, rssi_source="netsh (Rssi field)" if rssi is not None else None,
            rx_mbps=_number(block.get("receive rate (mbps)", "")),
            tx_mbps=_number(block.get("transmit rate (mbps)", "")),
            radio_type=block.get("radio type"),
            channel=int(channel) if channel is not None and channel == int(channel) else None,
        ))
    if readings:
        return readings
    if any(hint in lowered for hint in _NETSH_NOT_READY):
        return [WifiReading(state=STATE_NOT_READY, source=SOURCE_NETSH)]
    raise WifiError(CATEGORY_PARSE)


# ------------------------------------------------------------------ simulated source
class SimulatedWifiSource:
    """Deterministic fake Wi-Fi telemetry, so the feature demos on any machine with no adapter.

    Every value is a closed-form function of the elapsed time and the scripted profile: a
    steady link with a gentle wobble, a dip to a weak signal, then a short disconnection. All of
    it, including the RSSI, is SIMULATED and labelled so.
    """

    def __init__(self, profile: WifiSimProfile | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._profile = profile or WifiSimProfile()
        self._clock = clock
        self._started = clock()

    @staticmethod
    def _within(t: float, start: float | None, duration: float) -> bool:
        return start is not None and start <= t < start + duration

    async def read(self) -> WifiReading:
        p = self._profile
        t = self._clock() - self._started
        interface = "Wi-Fi (simulated)"
        if self._within(t, p.disconnect_at_s, p.disconnect_duration_s):
            return WifiReading(STATE_DISCONNECTED, interface=interface, source=SOURCE_SIMULATED)
        weak = self._within(t, p.dip_at_s, p.dip_duration_s)
        level = p.dip_signal_pct if weak else p.base_signal_pct
        wobble = 5 * math.sin(2 * math.pi * t / 37) + 3 * math.sin(2 * math.pi * t / 11)
        signal = max(1, min(100, round(level + (wobble * 0.4 if weak else wobble))))
        return WifiReading(
            state=STATE_CONNECTED, ssid=p.ssid, interface=interface, signal_pct=signal,
            rssi_dbm=round(-100 + signal / 2), rssi_source="simulated",
            rx_mbps=round(866.7 * signal / 100, 1), tx_mbps=round(780.0 * signal / 100, 1),
            radio_type="802.11ax", channel=36, source=SOURCE_SIMULATED,
        )


# ------------------------------------------------------------------ stored history and warnings
@dataclass(frozen=True)
class WifiSample:
    """One stored observation (the privacy-processed reading and when it was taken)."""

    ts: float
    state: str
    signal_pct: int | None
    rssi_dbm: int | None
    rx_mbps: float | None
    tx_mbps: float | None


def low_signal_warning(
    recent: Sequence[WifiSample], cfg: WifiConfig, *, now: float, stale_after_s: float
) -> dict:
    """Whether the signal quality has been at or below the threshold for `warn_after_polls`
    consecutive observations ending now. `recent` is newest first.

    A disconnection, a gap in the observations, or stale data ends the warning: it speaks only
    about a link that is connected and currently being observed.
    """
    run: list[WifiSample] = []
    for sample in recent:
        if (sample.state != STATE_CONNECTED or sample.signal_pct is None
                or sample.signal_pct > cfg.low_signal_pct):
            break
        run.append(sample)
    fresh = bool(recent) and now - recent[0].ts <= stale_after_s
    active = fresh and len(run) >= cfg.warn_after_polls
    return {
        "low_signal": active,
        "threshold_pct": cfg.low_signal_pct,
        "warn_after_polls": cfg.warn_after_polls,
        "consecutive_low": len(run) if fresh else 0,
        "since": run[-1].ts if active else None,
    }


def series(
    samples: Sequence[WifiSample], *, start: float, end: float, first_ts: float | None,
    poll_interval_s: float, max_points: int,
) -> tuple[list[dict], float]:
    """Signal history in equal time buckets: (buckets, bucket_s). Empty buckets are omitted.

    Each bucket reports the observations in it, how many were connected or not, and the mean,
    minimum and maximum signal quality (and mean RSSI and rates) of the CONNECTED ones. A
    disconnected observation contributes no signal figure: it is never averaged in as 0 %.
    """
    x0 = start if first_ts is None else max(start, first_ts)
    bucket_s = max((end - x0) / max_points, 3 * poll_interval_s)
    acc: dict[int, list[WifiSample]] = {}
    for sample in samples:
        acc.setdefault(int((sample.ts - x0) // bucket_s), []).append(sample)

    def mean(values: list[float], digits: int = 1) -> float | None:
        return round(sum(values) / len(values), digits) if values else None

    points = []
    for index in sorted(acc):
        group = acc[index]
        up = [s for s in group if s.state == STATE_CONNECTED]
        signals = [s.signal_pct for s in up if s.signal_pct is not None]
        points.append({
            "start": x0 + index * bucket_s,
            "end": min(x0 + (index + 1) * bucket_s, end),
            "observations": len(group),
            "connected": len(up),
            "not_connected": len(group) - len(up),
            "signal_mean": mean(signals),
            "signal_min": min(signals) if signals else None,
            "signal_max": max(signals) if signals else None,
            "rssi_mean": mean([s.rssi_dbm for s in up if s.rssi_dbm is not None]),
            "rx_mean": mean([s.rx_mbps for s in up if s.rx_mbps is not None]),
            "tx_mean": mean([s.tx_mbps for s in up if s.tx_mbps is not None]),
        })
    return points, bucket_s


def field_provenance(reading: WifiReading) -> dict[str, str]:
    """For each figure: "reported" (read from the OS), "simulated" or "unavailable"."""
    labels = {}
    for name in ("ssid", "interface", "signal_pct", "rssi_dbm", "rx_mbps", "tx_mbps",
                 "radio_type", "channel"):
        if getattr(reading, name) is None:
            labels[name] = "unavailable"
        else:
            labels[name] = "simulated" if reading.source == SOURCE_SIMULATED else "reported"
    return labels
