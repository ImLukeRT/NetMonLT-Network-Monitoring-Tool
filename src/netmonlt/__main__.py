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
from netmonlt.storage import Database

DEFAULT_INVENTORY = Path("config") / "devices.sim.yaml"
DEFAULT_DB = Path("data") / "netmonlt.db"
# Real and simulated history are kept in separate files so they can never be mixed up.
DEFAULT_REAL_DB = Path("data") / "netmonlt-real.db"


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

    try:
        db = Database(db_path)
        db.sync_devices(inventory.devices)
    except (sqlite3.Error, OSError) as exc:
        print(f"error: cannot open database {db_path}: {exc}", file=sys.stderr)
        return 2
    app = create_app(inventory, db, prober)
    if inventory.mode == "real":
        print("REAL ICMP monitoring is ON. Probing only these configured targets:")
        for device in inventory.devices:
            print(f"  - {device.name} ({device.address})")
    print(f"NetMonLT ({inventory.mode} mode, {len(inventory.devices)} devices): "
          f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
