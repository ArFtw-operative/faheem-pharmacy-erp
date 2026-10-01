"""Remove all test business data (stock, products, customers, sales, purchases).

    python scripts/wipe_test_data.py         # preview row counts
    python scripts/wipe_test_data.py --yes   # whole-ERP snapshot, then wipe

Users, roles and settings are kept. See app/services/data_reset.py.
Stop the app before running this.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATABASE_URL  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app import snapshot  # noqa: E402
from app.services import data_reset  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="actually wipe (otherwise preview only)")
    args = parser.parse_args()
    if not DATABASE_URL.startswith("sqlite:///"):
        raise SystemExit("Only SQLite databases are supported by this script.")

    init_db()  # schema up to date first
    with SessionLocal() as db:
        print("Rows:", {k: v for k, v in data_reset.counts(db).items() if v})
    if not args.yes:
        print("Preview only. Re-run with --yes to wipe.")
        return
    shot = snapshot.create("pre-reset", "before wiping business data")
    print("Snapshot written (restore with scripts/snapshot.sh restore):", shot["id"])
    with SessionLocal() as db:
        data_reset.wipe(db)
        db.commit()
        print("After:", {k: v for k, v in data_reset.counts(db).items() if v} or "all business tables empty")
    with sqlite3.connect(snapshot.db_path()) as c:
        c.execute("VACUUM")


if __name__ == "__main__":
    main()
