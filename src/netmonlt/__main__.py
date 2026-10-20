"""Command line entry point: `netmonlt` or `python -m netmonlt`."""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import uvicorn

from netmonlt.api import create_app
from netmonlt.inventory import InventoryError, load_inventory
from netmonlt.probes import IcmpProber, ProbeError, SimulatedProber
from netmonlt.snmp import SimulatedSnmpClient, SnmpClient, SnmpConfigError
from netmonlt.storage import Database
from netmonlt.wifi import SimulatedWifiSource
from netmonlt.wifi_poller import WifiSource

DEFAULT_INVENTORY = Path("config") / "devices.sim.yaml"
DEFAULT_DB = Path("data") / "netmonlt.db"
# Real and simulated history are kept in separate files so they can never be mixed up.
DEFAULT_REAL_DB = Path("data") / "netmonlt-real.db"


def _real_snmp_client(devices):
    """Build the real SNMPv3 client, or explain what is missing. Imported lazily because it needs
    the optional `snmp` dependencies, which a simulated or ICMP-only setup doesn't."""
    try:
        from netmonlt.snmp_pysnmp import PySnmpClient
    except ImportError:
        raise ImportError(
            "SNMP polling needs the optional SNMP dependencies: install them with "
            "'uv sync --extra snmp' or \"pip install 'netmonlt[snmp]'\""
        ) from None
    return PySnmpClient.from_devices(devices)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netmonlt", description="Lightweight network monitor")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY,
                        help=f"YAML device inventory (default: {DEFAULT_INVENTORY})")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"SQLite database file (default: {DEFAULT_DB}, "
                             f"or {DEFAULT_REAL_DB} in real mode)")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        inventory = load_inventory(args.inventory)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not args.inventory.exists():
            print(
                f"hint: looked for {args.inventory.resolve()}. Run netmonlt from the repository "
                "root (where the config/ folder is), or pass --inventory PATH.",
                file=sys.stderr,
            )
        return 2

    if inventory.mode == "real":
        prober = IcmpProber()
        try:
            prober.preflight()
        except ProbeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        db_path = args.db or DEFAULT_REAL_DB
    else:
        prober = SimulatedProber()
        db_path = args.db or DEFAULT_DB

    snmp_client: SnmpClient | None = None
    if inventory.snmp_devices:
        if inventory.mode == "real":
            try:
                snmp_client = _real_snmp_client(inventory.snmp_devices)
            except (ImportError, SnmpConfigError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            snmp_client = SimulatedSnmpClient()

    wifi_source: WifiSource | None = None
    if inventory.wifi.enabled:
        if inventory.mode == "real":
            from netmonlt.wifi_windows import WindowsWifiSource

            wifi_source = WindowsWifiSource(inventory.wifi)
        else:
            wifi_source = SimulatedWifiSource(inventory.wifi.sim)

    try:
        db = Database(db_path)
        db.sync_devices(inventory.devices)
    except (sqlite3.Error, OSError) as exc:
        print(f"error: cannot open database {db_path}: {exc}", file=sys.stderr)
        return 2
    kwargs = {} if snmp_client is None else {"snmp_client": snmp_client}
    if wifi_source is not None:
        kwargs["wifi_source"] = wifi_source
    app = create_app(inventory, db, prober, **kwargs)
    if inventory.mode == "real":
        print("REAL ICMP monitoring is ON. Probing only these configured targets:")
        for device in inventory.devices:
            print(f"  - {device.name} ({device.address})")
    if snmp_client is not None:
        if inventory.mode == "real":
            print("REAL SNMPv3 interface polling is ON for these configured devices only:")
            for device in inventory.snmp_devices:
                cfg = device.snmp
                print(f"  - {device.name} ({device.address}:{cfg.port}, {cfg.security_level}, "
                      f"{cfg.auth_protocol}/{cfg.priv_protocol})")
        else:
            names = ", ".join(d.name for d in inventory.snmp_devices)
            print(f"Simulated SNMP interface statistics for: {names}")
    if wifi_source is not None:
        if inventory.mode == "real":
            print(f"REAL Wi-Fi monitoring is ON for this computer's own adapter only "
                  f"(backend {inventory.wifi.backend}, SSID {inventory.wifi.ssid_privacy}). "
                  "No scanning, no other networks.")
            if sys.platform != "win32":
                print("note: Wi-Fi monitoring is only implemented for Windows; the dashboard "
                      "will report it as unsupported on this operating system.")
        else:
            print("Simulated Wi-Fi telemetry is ON (no adapter is read).")
    print(f"NetMonLT ({inventory.mode} mode, {len(inventory.devices)} devices): "
          f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
