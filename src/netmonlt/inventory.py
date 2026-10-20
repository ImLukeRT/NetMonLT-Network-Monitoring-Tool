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
    Device,
    QualityConfig,
    QualityThresholds,
    SimInterface,
    SimProfile,
    SnmpConfig,
)
from netmonlt.probes.icmp import TargetError, validate_target

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # used in URLs, so keep it URL-safe
_TOP_KEYS = {"mode", "poll_interval_s", "snmp_poll_interval_s", "quality", "devices"}
_QUALITY_KEYS = {
    "latency_ms", "jitter_ms", "loss_pct", "window_s", "min_checks", "min_jitter_pairs",
    "max_gap_fraction",
}
_THRESHOLD_KEYS = ("excellent", "good", "fair")
MAX_QUALITY_WINDOW_S = 7 * 86400  # the longest history window the API serves
_DEVICE_KEYS = {"name", "address", "type", "sim", "authorized", "snmp"}
_MODES = ("simulated", "real")
# Real probing sends traffic to someone's equipment, so don't allow hammering it.
REAL_MIN_INTERVAL_S = 5.0
_SIM_KEYS = {
    "rtt_ms", "jitter_ms", "loss", "outage_start_s", "outage_duration_s", "interfaces",
    "reboot_at_s",
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
    return Device(
        name=name, address=address, type=dev_type, sim=sim, authorized=authorized, snmp=snmp
    )


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
