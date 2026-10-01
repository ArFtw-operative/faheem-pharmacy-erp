"""Clear legacy stock so the latest stock can be imported cleanly.

    python scripts/reset_inventory.py                 # preview only
    python scripts/reset_inventory.py --yes           # zero all stock (keep products)
    python scripts/reset_inventory.py --yes --mode catalog   # also move products to the recycle bin

A verified whole-ERP snapshot is taken before anything changes.
Then import the latest stock from Inventory → Import (opening stock sheet).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db  # noqa: E402
from app import snapshot  # noqa: E402
from app.services import inventory_reset  # noqa: E402


def backup() -> str:
    return snapshot.create("pre-reset", "before inventory reset")["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=inventory_reset.MODES, default="stock")
    parser.add_argument("--reason", default="")
    parser.add_argument("--yes", action="store_true", help="actually apply (otherwise preview only)")
    args = parser.parse_args()

    init_db()  # bring the schema (and ledger) up to date first
    with SessionLocal() as db:
        print("Current:", inventory_reset.preview(db))
        if not args.yes:
            print("Preview only. Re-run with --yes to apply.")
            return
    path = backup()
    print("Snapshot written (restore with scripts/snapshot.sh restore):", path)
    with SessionLocal() as db:
        result = inventory_reset.reset(db, mode=args.mode, reason=args.reason)
        db.commit()
    print("Done:", result)


if __name__ == "__main__":
    main()
