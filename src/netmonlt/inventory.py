"""Load and validate the YAML device inventory.

The YAML file is the source of truth for which devices exist. Validation is strict
(unknown keys are rejected) so typos fail loudly instead of silently using defaults.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from netmonlt.domain import Device, SimProfile

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")  # used in URLs, so keep it URL-safe
_TOP_KEYS = {"mode", "poll_interval_s", "devices"}
_DEVICE_KEYS = {"name", "address", "type", "sim"}
_SIM_KEYS = {"rtt_ms", "jitter_ms", "loss", "outage_start_s", "outage_duration_s"}


class InventoryError(ValueError):
    """The inventory file is missing or invalid."""


@dataclass(frozen=True)
class Inventory:
    mode: str
    poll_interval_s: float
    devices: tuple[Device, ...]


def load_inventory(path: str | Path) -> Inventory:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
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
    if mode != "simulated":
        raise InventoryError(f"mode {mode!r} is not supported yet; only 'simulated' is available")

    interval = _number(raw.get("poll_interval_s", 5), "poll_interval_s", minimum=0.1)

    raw_devices = raw.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise InventoryError("'devices' must be a non-empty list")

    devices: list[Device] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_devices):
        device = _parse_device(entry, index)
        if device.name in seen:
            raise InventoryError(f"duplicate device name {device.name!r}")
        seen.add(device.name)
        devices.append(device)
    return Inventory(mode=mode, poll_interval_s=interval, devices=tuple(devices))


def _parse_device(entry: Any, index: int) -> Device:
    where = f"devices[{index}]"
    if not isinstance(entry, dict):
        raise InventoryError(f"{where} must be a mapping")
    _reject_unknown(entry, _DEVICE_KEYS, where)

    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InventoryError(f"{where}.name must be 1-64 chars of letters, digits, '.', '_', '-'")
    where = f"device {name!r}"

    address = entry.get("address")
    if not isinstance(address, str) or not address.strip() or re.search(r"\s", address):
        raise InventoryError(f"{where}: 'address' must be a non-empty string without spaces")

    dev_type = entry.get("type", "host")
    if not isinstance(dev_type, str) or not dev_type.strip():
        raise InventoryError(f"{where}: 'type' must be a non-empty string")

    sim = _parse_sim(entry.get("sim"), where)
    return Device(name=name, address=address, type=dev_type, sim=sim)


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
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise InventoryError(f"{label} must be {bound}")
    return float(value)
