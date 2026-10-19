"""Load and validate the YAML device inventory.

The YAML file is the source of truth for which devices exist. Validation is strict
(unknown keys are rejected) so typos fail loudly instead of silently using defaults.
"""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from netmonlt.domain import Device, SimProfile
from netmonlt.probes.icmp import TargetError, validate_target

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # used in URLs, so keep it URL-safe
_TOP_KEYS = {"mode", "poll_interval_s", "devices"}
_DEVICE_KEYS = {"name", "address", "type", "sim", "authorized"}
_MODES = ("simulated", "real")
# Real probing sends traffic to someone's equipment, so don't allow hammering it.
REAL_MIN_INTERVAL_S = 5.0
_SIM_KEYS = {"rtt_ms", "jitter_ms", "loss", "outage_start_s", "outage_duration_s"}


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
    return Inventory(mode=mode, poll_interval_s=interval, devices=tuple(devices))


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
    return Device(name=name, address=address, type=dev_type, sim=sim, authorized=authorized)


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

    return SimProfile(
        rtt_ms=_number(raw.get("rtt_ms", 10), f"{where}.sim.rtt_ms", minimum=0.1),
        jitter_ms=_number(raw.get("jitter_ms", 1), f"{where}.sim.jitter_ms", minimum=0),
        loss=_number(raw.get("loss", 0), f"{where}.sim.loss", minimum=0, maximum=1),
        outage_start_s=start,
        outage_duration_s=float(duration or 0),
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
