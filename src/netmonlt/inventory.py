"""Load and validate the YAML device inventory.

The YAML file is the source of truth for which devices exist. Validation is strict
(unknown keys are rejected) so typos fail loudly instead of silently using defaults.
"""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from netmonlt.domain import (
    SNMP_AUTH_PROTOCOLS,
    SNMP_PRIV_PROTOCOLS,
    SNMP_SECURITY_LEVELS,
    WIFI_BACKENDS,
    WIFI_SSID_PRIVACY,
    Device,
    IncidentsConfig,
    QosConfig,
    QualityConfig,
    QualityThresholds,
    SimInterface,
    SimProfile,
    SimQosClass,
    SimQosProfile,
    SnmpConfig,
    WifiConfig,
    WifiSimProfile,
)
from netmonlt.probes.icmp import TargetError, validate_target
from netmonlt.qos_adapters import ADAPTERS

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # used in URLs, so keep it URL-safe
_TOP_KEYS = {
    "mode", "poll_interval_s", "snmp_poll_interval_s", "qos_poll_interval_s",
    "qos_retention_hours", "quality", "wifi", "incidents", "devices",
}
_INCIDENTS_KEYS = {
    "enabled", "correlation_window_s", "flapping_recurrences", "operator_actions",
    "simulation_auto_documentation", "simulation_auto_resolve",
}
_WIFI_KEYS = {
    "enabled", "poll_interval_s", "backend", "ssid_privacy", "low_signal_pct",
    "warn_after_polls", "retention_days", "sim",
}
_WIFI_SIM_KEYS = {
    "ssid", "base_signal_pct", "dip_at_s", "dip_duration_s", "dip_signal_pct",
    "disconnect_at_s", "disconnect_duration_s",
}
REAL_MIN_WIFI_INTERVAL_S = 5.0
_QUALITY_KEYS = {
    "latency_ms", "jitter_ms", "loss_pct", "window_s", "min_checks", "min_jitter_pairs",
    "max_gap_fraction",
}
_THRESHOLD_KEYS = ("excellent", "good", "fair")
MAX_QUALITY_WINDOW_S = 7 * 86400  # the longest history window the API serves
_DEVICE_KEYS = {"name", "address", "type", "sim", "authorized", "snmp", "qos"}
_QOS_KEYS = {"enabled", "adapter"}
_SIM_QOS_KEYS = {"interface", "capacity_mbps", "congestion", "classes"}
_SIM_QOS_CONGESTION_KEYS = {"start_s", "duration_s", "surge_factor"}
_SIM_QOS_CLASS_KEYS = {
    "name", "offered_mbps", "priority", "weight", "surges", "avg_packet_bytes",
    "counter_bits", "wrap_at_s",
}
MAX_SIM_QOS_CLASSES = 16
# QoS polling reads several tables per device, so real polling is kept gentle.
REAL_MIN_QOS_INTERVAL_S = 10.0
_MODES = ("simulated", "real")
# Real probing sends traffic to someone's equipment, so don't allow hammering it.
REAL_MIN_INTERVAL_S = 5.0
_SIM_KEYS = {
    "rtt_ms", "jitter_ms", "loss", "outage_start_s", "outage_duration_s", "interfaces",
    "reboot_at_s", "qos",
}
_SIM_IF_KEYS = {
    "name", "in_mbps", "out_mbps", "status", "in_errors_per_min", "out_errors_per_min",
    "in_discards_per_min", "out_discards_per_min", "wrap_at_s", "disappears_at_s",
}
_SNMP_KEYS = {
    "enabled", "credentials_env", "security_level", "auth_protocol", "priv_protocol", "port",
    "timeout_s", "retries",
}
# Keys that would put a secret or a weaker protocol version into the inventory file.
_SNMP_SECRET_KEYS = {
    "community", "version", "user", "username", "password", "auth_key", "priv_key",
    "auth_password", "priv_password", "passphrase", "key",
}
_IF_NAME_RE = re.compile(r"^[A-Za-z0-9/._:-]{1,64}$")
_ENV_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")
MAX_SIM_INTERFACES = 64
# SNMP asks a device for many values per poll, so real polling is kept gentle.
REAL_MIN_SNMP_INTERVAL_S = 10.0


class InventoryError(ValueError):
    """The inventory file is missing or invalid."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently keeping the last.

    Without this, a second `devices:` block quietly replaces the first and those devices are
    never monitored.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable key: let the base class report it
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class Inventory:
    mode: str
    poll_interval_s: float
    devices: tuple[Device, ...]
    snmp_poll_interval_s: float = 30.0
    quality: QualityConfig = field(default_factory=QualityConfig)
    wifi: WifiConfig = field(default_factory=WifiConfig)
    incidents: IncidentsConfig = field(default_factory=IncidentsConfig)
    qos_poll_interval_s: float = 30.0
    qos_retention_hours: float = 24.0

    @property
    def qos_devices(self) -> tuple[Device, ...]:
        """Devices whose QoS telemetry is read: simulated ones with a sim.qos policy in simulated
        mode, and only devices that explicitly enable `qos` (with SNMPv3) in real mode."""
        if self.mode == "real":
            return tuple(d for d in self.devices if d.qos is not None and d.snmp is not None)
        return tuple(d for d in self.devices if d.sim.qos is not None)

    @property
    def snmp_devices(self) -> tuple[Device, ...]:
        """Devices whose interfaces are polled: simulated ones with sim.interfaces in simulated
        mode, and only devices that explicitly enable `snmp` in real mode."""
        if self.mode == "real":
            return tuple(d for d in self.devices if d.snmp is not None)
        return tuple(d for d in self.devices if d.sim.interfaces)


def load_inventory(path: str | Path) -> Inventory:
    path = Path(path)
    try:
        # Read as a binary stream so PyYAML detects UTF-8/UTF-16 and BOMs (Windows editors and
        # PowerShell redirection write these) and reports bad bytes as a YAML error.
        with path.open("rb") as stream:
            raw = yaml.load(stream, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except FileNotFoundError:
        raise InventoryError(f"inventory file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise InventoryError(f"invalid YAML in {path}: {exc}") from None
    return parse_inventory(raw)


def parse_inventory(raw: Any) -> Inventory:
    if not isinstance(raw, dict):
        raise InventoryError("inventory must be a YAML mapping with a 'devices' list")
    _reject_unknown(raw, _TOP_KEYS, "inventory")

    mode = raw.get("mode", "simulated")
    if mode not in _MODES:
        raise InventoryError(f"mode must be 'simulated' or 'real', not {mode!r}")

    minimum = REAL_MIN_INTERVAL_S if mode == "real" else 0.1
    interval = _number(raw.get("poll_interval_s", 5), "poll_interval_s", minimum=minimum)

    snmp_minimum = REAL_MIN_SNMP_INTERVAL_S if mode == "real" else 1.0
    snmp_interval = _number(
        raw.get("snmp_poll_interval_s", 30), "snmp_poll_interval_s", minimum=snmp_minimum
    )

    quality = _parse_quality(raw.get("quality"))
    wifi = _parse_wifi(raw.get("wifi"), mode)
    incidents = _parse_incidents(raw.get("incidents"), mode)

    qos_minimum = REAL_MIN_QOS_INTERVAL_S if mode == "real" else 1.0
    qos_interval = _number(
        raw.get("qos_poll_interval_s", 30), "qos_poll_interval_s", minimum=qos_minimum
    )
    qos_retention = _number(
        raw.get("qos_retention_hours", 24), "qos_retention_hours", minimum=1, maximum=168
    )

    raw_devices = raw.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise InventoryError("'devices' must be a non-empty list")

    devices: list[Device] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_devices):
        device = _parse_device(entry, index, mode)
        if device.name in seen:
            raise InventoryError(f"duplicate device name {device.name!r}")
        seen.add(device.name)
        devices.append(device)
    return Inventory(
        mode=mode,
        poll_interval_s=interval,
        devices=tuple(devices),
        snmp_poll_interval_s=snmp_interval,
        quality=quality,
        wifi=wifi,
        incidents=incidents,
        qos_poll_interval_s=qos_interval,
        qos_retention_hours=qos_retention,
    )


def _parse_quality(raw: Any) -> QualityConfig:
    """The optional `quality:` block: grading thresholds and the minimum data needed to grade."""
    if raw is None:
        return QualityConfig()
    if not isinstance(raw, dict):
        raise InventoryError("quality must be a mapping")
    _reject_unknown(raw, _QUALITY_KEYS, "quality")
    defaults = QualityConfig()

    def whole(key: str, minimum: int, maximum: int | None = None) -> int:
        value = _number(raw.get(key, getattr(defaults, key)), f"quality.{key}", minimum, maximum)
        if value != int(value):
            raise InventoryError(f"quality.{key} must be a whole number")
        return int(value)

    fraction = _number(
        raw.get("max_gap_fraction", defaults.max_gap_fraction), "quality.max_gap_fraction", 0.01, 1
    )
    return QualityConfig(
        latency_ms=_parse_thresholds(raw, "latency_ms", defaults.latency_ms, None),
        jitter_ms=_parse_thresholds(raw, "jitter_ms", defaults.jitter_ms, None),
        loss_pct=_parse_thresholds(raw, "loss_pct", defaults.loss_pct, 100.0),
        window_s=whole("window_s", 60, MAX_QUALITY_WINDOW_S),
        min_checks=whole("min_checks", 2, 100_000),
        min_jitter_pairs=whole("min_jitter_pairs", 1, 100_000),
        max_gap_fraction=fraction,
    )


def _parse_incidents(raw: Any, mode: str = "simulated") -> IncidentsConfig:
    """The optional `incidents:` block: correlation window and whether operator actions exist."""
    if raw is None:
        return IncidentsConfig()
    if not isinstance(raw, dict):
        raise InventoryError("incidents must be a mapping")
    _reject_unknown(raw, _INCIDENTS_KEYS, "incidents")
    defaults = IncidentsConfig()
    flags = {}
    for key in ("enabled", "operator_actions", "simulation_auto_documentation",
                "simulation_auto_resolve"):
        value = raw.get(key, getattr(defaults, key))
        if value is not None and not isinstance(value, bool):
            raise InventoryError(f"incidents.{key} must be true or false")
        flags[key] = value
    if flags["enabled"] is None:
        raise InventoryError("incidents.enabled must be true or false")
    if flags["simulation_auto_resolve"] is None:
        raise InventoryError("incidents.simulation_auto_resolve must be true or false")
    for key in ("simulation_auto_documentation", "simulation_auto_resolve"):
        if mode == "real" and flags[key] is True:
            raise InventoryError(
                f"incidents.{key} is a simulation-only feature and cannot be enabled in real "
                "mode: it would write example (fictional) documentation about real devices")
    if flags["simulation_auto_resolve"] and flags["simulation_auto_documentation"] is False:
        raise InventoryError(
            "incidents.simulation_auto_resolve needs incidents.simulation_auto_documentation "
            "(an incident is only auto-resolved once its example documentation exists)")
    window = _number(
        raw.get("correlation_window_s", defaults.correlation_window_s),
        "incidents.correlation_window_s", minimum=60, maximum=86400,
    )
    recurrences = _number(
        raw.get("flapping_recurrences", defaults.flapping_recurrences),
        "incidents.flapping_recurrences", minimum=1, maximum=100,
    )
    if recurrences != int(recurrences):
        raise InventoryError("incidents.flapping_recurrences must be a whole number")
    return IncidentsConfig(
        enabled=flags["enabled"], correlation_window_s=window,
        flapping_recurrences=int(recurrences), operator_actions=flags["operator_actions"],
        simulation_auto_documentation=flags["simulation_auto_documentation"],
        simulation_auto_resolve=flags["simulation_auto_resolve"],
    )


def _parse_wifi(raw: Any, mode: str) -> WifiConfig:
    """The optional `wifi:` block. Monitoring this computer's Wi-Fi is off unless enabled."""
    if raw is None:
        return WifiConfig()
    if not isinstance(raw, dict):
        raise InventoryError("wifi must be a mapping")
    _reject_unknown(raw, _WIFI_KEYS, "wifi")
    defaults = WifiConfig()
    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise InventoryError("wifi.enabled must be true or false")

    def choice(key: str, allowed: tuple[str, ...]) -> str:
        value = raw.get(key, getattr(defaults, key))
        if value not in allowed:
            raise InventoryError(f"wifi.{key} must be one of {', '.join(allowed)}, not {value!r}")
        return value

    def whole(key: str, minimum: int, maximum: int) -> int:
        value = _number(raw.get(key, getattr(defaults, key)), f"wifi.{key}", minimum, maximum)
        if value != int(value):
            raise InventoryError(f"wifi.{key} must be a whole number")
        return int(value)

    minimum = REAL_MIN_WIFI_INTERVAL_S if mode == "real" else 1.0
    interval = _number(
        raw.get("poll_interval_s", defaults.poll_interval_s), "wifi.poll_interval_s", minimum
    )
    return WifiConfig(
        enabled=enabled,
        poll_interval_s=interval,
        backend=choice("backend", WIFI_BACKENDS),
        ssid_privacy=choice("ssid_privacy", WIFI_SSID_PRIVACY),
        low_signal_pct=whole("low_signal_pct", 0, 100),
        warn_after_polls=whole("warn_after_polls", 1, 1000),
        retention_days=whole("retention_days", 1, 90),
        sim=_parse_wifi_sim(raw.get("sim")),
    )


def _parse_wifi_sim(raw: Any) -> WifiSimProfile:
    if raw is None:
        return WifiSimProfile()
    if not isinstance(raw, dict):
        raise InventoryError("wifi.sim must be a mapping")
    _reject_unknown(raw, _WIFI_SIM_KEYS, "wifi.sim")
    d = WifiSimProfile()
    ssid = raw.get("ssid", d.ssid)
    if not isinstance(ssid, str) or not 1 <= len(ssid) <= 32:
        raise InventoryError("wifi.sim.ssid must be a text of 1 to 32 characters")

    def pct(key: str, default: int) -> int:
        value = _number(raw.get(key, default), f"wifi.sim.{key}", 1, 100)
        if value != int(value):
            raise InventoryError(f"wifi.sim.{key} must be a whole number")
        return int(value)

    def optional(key: str, default: float | None) -> float | None:
        value = raw.get(key, default)
        return None if value is None else _number(value, f"wifi.sim.{key}", 0)

    def duration(key: str, default: float) -> float:
        return _number(raw.get(key, default), f"wifi.sim.{key}", 0.1)

    return WifiSimProfile(
        ssid=ssid,
        base_signal_pct=pct("base_signal_pct", d.base_signal_pct),
        dip_at_s=optional("dip_at_s", d.dip_at_s),
        dip_duration_s=duration("dip_duration_s", d.dip_duration_s),
        dip_signal_pct=pct("dip_signal_pct", d.dip_signal_pct),
        disconnect_at_s=optional("disconnect_at_s", d.disconnect_at_s),
        disconnect_duration_s=duration("disconnect_duration_s", d.disconnect_duration_s),
    )


def _parse_thresholds(
    raw: dict, key: str, default: QualityThresholds, maximum: float | None
) -> QualityThresholds:
    block = raw.get(key)
    if block is None:
        return default
    where = f"quality.{key}"
    if not isinstance(block, dict):
        raise InventoryError(f"{where} must be a mapping with excellent, good and fair")
    _reject_unknown(block, set(_THRESHOLD_KEYS), where)
    missing = [k for k in _THRESHOLD_KEYS if k not in block]
    if missing:
        raise InventoryError(f"{where}: missing {', '.join(missing)} (give all three bounds)")
    values = [_number(block[k], f"{where}.{k}", 0.0, maximum) for k in _THRESHOLD_KEYS]
    if not values[0] < values[1] < values[2]:
        raise InventoryError(f"{where}: need excellent < good < fair (upper bounds, in order)")
    return QualityThresholds(*values)


def _parse_device(entry: Any, index: int, mode: str) -> Device:
    where = f"devices[{index}]"
    if not isinstance(entry, dict):
        raise InventoryError(f"{where} must be a mapping")
    _reject_unknown(entry, _DEVICE_KEYS, where)

    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InventoryError(f"{where}.name must be 1-64 chars of letters, digits, '.', '_', '-'")
    where = f"device {name!r}"

    address = entry.get("address")
    if not isinstance(address, str) or not address.strip() or re.search(r"\s", address):
        raise InventoryError(f"{where}: 'address' must be a non-empty string without spaces")

    dev_type = entry.get("type", "host")
    if not isinstance(dev_type, str) or not dev_type.strip():
        raise InventoryError(f"{where}: 'type' must be a non-empty string")

    authorized = entry.get("authorized", False)
    if not isinstance(authorized, bool):
        raise InventoryError(f"{where}: 'authorized' must be true or false")
    if mode == "real":
        # Real probing is opt-in per device: each target must be acknowledged explicitly, and
        # must be a single host (never a range), so nothing is scanned or discovered.
        if not authorized:
            raise InventoryError(
                f"{where}: real mode requires 'authorized: true' (only list devices you own "
                "or are explicitly permitted to monitor)"
            )
        try:
            validate_target(address)
        except TargetError as exc:
            raise InventoryError(f"{where}: address {address!r}: {exc}") from None

    sim = _parse_sim(entry.get("sim"), where)  # validated in both modes, used only when simulated
    # Validated in both modes, but only ever used to poll a real device in real mode.
    snmp = _parse_snmp(entry.get("snmp"), where)
    qos = _parse_qos(entry.get("qos"), where, mode, snmp)
    return Device(
        name=name, address=address, type=dev_type, sim=sim, authorized=authorized, snmp=snmp,
        qos=qos,
    )


def _parse_qos(raw: Any, where: str, mode: str, snmp: SnmpConfig | None) -> QosConfig | None:
    """A device's `qos` block. Returns None unless QoS telemetry is explicitly enabled.

    Only adapters that NetMonLT supports are accepted, and in real mode the device must also
    have SNMPv3 enabled: QoS polling reuses its credentials and security settings.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InventoryError(f"{where}: 'qos' must be a mapping")
    _reject_unknown(raw, _QOS_KEYS, f"{where}.qos")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InventoryError(f"{where}.qos.enabled must be true or false")
    adapter = raw.get("adapter")
    if adapter is not None and not (isinstance(adapter, str) and adapter in ADAPTERS):
        raise InventoryError(
            f"{where}.qos.adapter {adapter!r} is not supported. Supported adapters: "
            f"{', '.join(sorted(ADAPTERS))}. NetMonLT never guesses OIDs: a device's QoS MIB "
            "needs an explicit, verified adapter"
        )
    if not enabled:
        return None  # a `qos` block alone never turns polling on
    if adapter is None:
        raise InventoryError(
            f"{where}.qos.adapter is required when QoS is enabled "
            f"(one of: {', '.join(sorted(ADAPTERS))})"
        )
    if mode == "real" and snmp is None:
        raise InventoryError(
            f"{where}.qos: QoS telemetry reuses the device's SNMPv3 credentials, so the device "
            "needs an enabled 'snmp' block too"
        )
    return QosConfig(adapter=adapter)


def _parse_sim(raw: Any, where: str) -> SimProfile:
    if raw is None:
        return SimProfile()
    if not isinstance(raw, dict):
        raise InventoryError(f"{where}: 'sim' must be a mapping")
    _reject_unknown(raw, _SIM_KEYS, f"{where}.sim")

    start = raw.get("outage_start_s")
    duration = raw.get("outage_duration_s", 0)
    if start is not None:
        start = _number(start, f"{where}.sim.outage_start_s", minimum=0)
        duration = _number(duration, f"{where}.sim.outage_duration_s", minimum=0.1)
    elif duration:
        raise InventoryError(f"{where}.sim: outage_duration_s needs outage_start_s")

    reboot = raw.get("reboot_at_s")
    if reboot is not None:
        reboot = _number(reboot, f"{where}.sim.reboot_at_s", minimum=0)

    return SimProfile(
        rtt_ms=_number(raw.get("rtt_ms", 10), f"{where}.sim.rtt_ms", minimum=0.1),
        jitter_ms=_number(raw.get("jitter_ms", 1), f"{where}.sim.jitter_ms", minimum=0),
        loss=_number(raw.get("loss", 0), f"{where}.sim.loss", minimum=0, maximum=1),
        outage_start_s=start,
        outage_duration_s=float(duration or 0),
        interfaces=_parse_sim_interfaces(raw.get("interfaces"), where),
        reboot_at_s=reboot,
        qos=_parse_sim_qos(raw.get("qos"), where),
    )


def _parse_sim_qos(raw: Any, where: str) -> SimQosProfile | None:
    """The simulated QoS policy of a device (a scripted demo; ignored in real mode)."""
    if raw is None:
        return None
    here = f"{where}.sim.qos"
    if not isinstance(raw, dict):
        raise InventoryError(f"{where}: 'sim.qos' must be a mapping")
    _reject_unknown(raw, _SIM_QOS_KEYS, here)
    interface = raw.get("interface", "wan0")
    if not isinstance(interface, str) or not _IF_NAME_RE.fullmatch(interface):
        raise InventoryError(f"{here}.interface must be 1-64 chars: letters, digits, / . _ : -")
    capacity = _number(raw.get("capacity_mbps", 100), f"{here}.capacity_mbps", minimum=0.1)

    start = duration = None
    factor = 1.0
    congestion = raw.get("congestion")
    if congestion is not None:
        if not isinstance(congestion, dict):
            raise InventoryError(f"{here}.congestion must be a mapping")
        _reject_unknown(congestion, _SIM_QOS_CONGESTION_KEYS, f"{here}.congestion")
        if "start_s" not in congestion or "duration_s" not in congestion:
            raise InventoryError(f"{here}.congestion needs start_s and duration_s")
        start = _number(congestion["start_s"], f"{here}.congestion.start_s", minimum=0)
        duration = _number(congestion["duration_s"], f"{here}.congestion.duration_s", minimum=0.1)
        factor = _number(congestion.get("surge_factor", 2), f"{here}.congestion.surge_factor",
                         minimum=1, maximum=20)

    classes_raw = raw.get("classes")
    if not isinstance(classes_raw, list) or not classes_raw:
        raise InventoryError(f"{here}.classes must be a non-empty list")
    if len(classes_raw) > MAX_SIM_QOS_CLASSES:
        raise InventoryError(f"{here}.classes has more than {MAX_SIM_QOS_CLASSES} entries")
    classes, seen = [], set()
    for i, entry in enumerate(classes_raw):
        classes.append(_parse_sim_qos_class(entry, f"{here}.classes[{i}]", seen))
    return SimQosProfile(
        interface=interface, capacity_mbps=capacity, congestion_start_s=start,
        congestion_duration_s=0.0 if duration is None else duration, surge_factor=factor,
        classes=tuple(classes),
    )


def _parse_sim_qos_class(entry: Any, label: str, seen: set[str]) -> SimQosClass:
    if not isinstance(entry, dict):
        raise InventoryError(f"{label} must be a mapping")
    _reject_unknown(entry, _SIM_QOS_CLASS_KEYS, label)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 40 or not name.isprintable():
        raise InventoryError(f"{label}.name must be 1-40 printable characters")
    if name in seen:
        raise InventoryError(f"{label}: duplicate class name {name!r}")
    seen.add(name)
    for key in ("priority", "surges"):
        if not isinstance(entry.get(key, False), bool):
            raise InventoryError(f"{label}.{key} must be true or false")
    bits = entry.get("counter_bits", 64)
    if isinstance(bits, bool) or bits not in (32, 64):
        raise InventoryError(f"{label}.counter_bits must be 32 or 64")
    packet = _number(entry.get("avg_packet_bytes", 800), f"{label}.avg_packet_bytes", 40, 9000)
    if packet != int(packet):
        raise InventoryError(f"{label}.avg_packet_bytes must be a whole number")
    wrap = entry.get("wrap_at_s")
    if wrap is not None:
        wrap = _number(wrap, f"{label}.wrap_at_s", minimum=1, maximum=86400)
    return SimQosClass(
        name=name,
        offered_mbps=_number(entry.get("offered_mbps"), f"{label}.offered_mbps", minimum=0),
        priority=entry.get("priority", False),
        weight=_number(entry.get("weight", 1), f"{label}.weight", minimum=0.01, maximum=1000),
        surges=entry.get("surges", False),
        avg_packet_bytes=int(packet),
        counter_bits=bits,
        wrap_at_s=wrap,
    )


def _parse_sim_interfaces(raw: Any, where: str) -> tuple[SimInterface, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InventoryError(f"{where}.sim.interfaces must be a list")
    if len(raw) > MAX_SIM_INTERFACES:
        raise InventoryError(f"{where}.sim.interfaces: at most {MAX_SIM_INTERFACES} interfaces")
    interfaces: list[SimInterface] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"{where}.sim.interfaces[{index}]"
        if not isinstance(entry, dict):
            raise InventoryError(f"{label} must be a mapping")
        _reject_unknown(entry, _SIM_IF_KEYS, label)
        name = entry.get("name")
        if not isinstance(name, str) or not _IF_NAME_RE.fullmatch(name):
            raise InventoryError(f"{label}.name must be 1-64 chars of letters, digits, / . _ : -")
        if name in seen:
            raise InventoryError(f"{where}.sim.interfaces: duplicate interface name {name!r}")
        seen.add(name)
        status = entry.get("status", "up")
        if status not in ("up", "down"):
            raise InventoryError(f"{label}.status must be 'up' or 'down'")

        def number(key: str, label=label, entry=entry) -> float:
            return _number(entry.get(key, 0), f"{label}.{key}", minimum=0)

        def optional(key: str, label=label, entry=entry) -> float | None:
            value = entry.get(key)
            return None if value is None else _number(value, f"{label}.{key}", minimum=0)

        interfaces.append(SimInterface(
            name=name,
            in_mbps=number("in_mbps"),
            out_mbps=number("out_mbps"),
            status=status,
            in_errors_per_min=number("in_errors_per_min"),
            out_errors_per_min=number("out_errors_per_min"),
            in_discards_per_min=number("in_discards_per_min"),
            out_discards_per_min=number("out_discards_per_min"),
            wrap_at_s=optional("wrap_at_s"),
            disappears_at_s=optional("disappears_at_s"),
        ))
    return tuple(interfaces)


def _parse_snmp(raw: Any, where: str) -> SnmpConfig | None:
    """Parse a device's `snmp` block. Returns None unless SNMP is explicitly enabled."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InventoryError(f"{where}: 'snmp' must be a mapping")
    secret_keys = sorted(set(raw) & _SNMP_SECRET_KEYS)
    if secret_keys:
        raise InventoryError(
            f"{where}.snmp: {', '.join(secret_keys)} must not appear in the inventory. Only "
            "SNMPv3 is supported, and its user name and keys come from environment variables "
            "named by 'credentials_env' (<NAME>_USER, <NAME>_AUTH_KEY, <NAME>_PRIV_KEY)"
        )
    _reject_unknown(raw, _SNMP_KEYS, f"{where}.snmp")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InventoryError(f"{where}.snmp.enabled must be true or false")
    level = raw.get("security_level", "authPriv")
    if level not in SNMP_SECURITY_LEVELS:
        raise InventoryError(
            f"{where}.snmp.security_level must be one of {', '.join(SNMP_SECURITY_LEVELS)} "
            "(noAuthNoPriv is not supported)"
        )
    auth = raw.get("auth_protocol", "SHA256")
    if auth not in SNMP_AUTH_PROTOCOLS:
        raise InventoryError(
            f"{where}.snmp.auth_protocol must be one of {', '.join(SNMP_AUTH_PROTOCOLS)} "
            "(MD5 is not supported because it is weak)"
        )
    priv = raw.get("priv_protocol", "AES128")
    if priv not in SNMP_PRIV_PROTOCOLS:
        raise InventoryError(
            f"{where}.snmp.priv_protocol must be one of {', '.join(SNMP_PRIV_PROTOCOLS)} "
            "(DES is not supported because it is weak)"
        )
    port = _number(raw.get("port", 161), f"{where}.snmp.port", minimum=1, maximum=65535)
    timeout = _number(raw.get("timeout_s", 5), f"{where}.snmp.timeout_s", minimum=1, maximum=30)
    retries = _number(raw.get("retries", 1), f"{where}.snmp.retries", minimum=0, maximum=3)
    if int(port) != port or int(retries) != retries:
        raise InventoryError(f"{where}.snmp: port and retries must be whole numbers")

    if not enabled:
        return None  # an `snmp` block alone never turns polling on
    prefix = raw.get("credentials_env")
    if not isinstance(prefix, str) or not _ENV_PREFIX_RE.fullmatch(prefix):
        raise InventoryError(
            f"{where}.snmp.credentials_env must name the environment-variable prefix that "
            "holds the credentials, e.g. NETMONLT_SNMP_ROUTER (capital letters, digits, _)"
        )
    return SnmpConfig(
        credentials_env=prefix,
        security_level=level,
        auth_protocol=auth,
        priv_protocol=priv,
        port=int(port),
        timeout_s=timeout,
        retries=int(retries),
    )


def _reject_unknown(mapping: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise InventoryError(
            f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )


def _number(value: Any, label: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InventoryError(f"{label} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise InventoryError(f"{label} is out of range") from None
    if not math.isfinite(number):
        raise InventoryError(f"{label} must be a finite number")
    if number < minimum or (maximum is not None and number > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise InventoryError(f"{label} must be {bound}")
    return number
