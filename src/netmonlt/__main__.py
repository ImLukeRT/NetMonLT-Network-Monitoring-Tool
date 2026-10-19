"""Command line entry point: `netmonlt` or `python -m netmonlt`."""

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from netmonlt.api import create_app
from netmonlt.inventory import InventoryError, load_inventory
from netmonlt.probes import SimulatedProber
from netmonlt.storage import Database

DEFAULT_INVENTORY = Path("config") / "devices.sim.yaml"
DEFAULT_DB = Path("data") / "netmonlt.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netmonlt", description="Lightweight network monitor")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY,
                        help=f"YAML device inventory (default: {DEFAULT_INVENTORY})")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite database file (default: {DEFAULT_DB})")
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
        return 2

    db = Database(args.db)
    db.sync_devices(inventory.devices)
    app = create_app(inventory, db, SimulatedProber())
    print(f"NetMonLT ({inventory.mode} mode, {len(inventory.devices)} devices): "
          f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
