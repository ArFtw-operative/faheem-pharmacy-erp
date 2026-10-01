"""Freeze a sample shop database at one release's schema (for the upgrade test suite).

    python scripts/make_release_fixture.py 1.0.0 tests/fixtures/releases/1.0.0.db [--revision REV]

Run it once when a release is cut, with that release's code, and commit the
file. tests/test_upgrades.py then proves that every later release upgrades each
frozen database to the newest schema with its entire history unchanged — the
"v1.0 → v1.1 → v2.0 keeps everything" acceptance test. Never regenerate an
existing fixture: it stands for databases already in the field.

The data is synthetic (no real customer is ever put in a fixture) but covers
every kind of history: products and batches, opening stock, a posted supplier
purchase, loose and strip sales, discounts, split payments, a customer return,
a voided bill, a manual bill, a stock adjustment, customers and a follow-up.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(out: Path, revision: str) -> None:
    from datetime import date

    from alembic import command

    from app.database import SessionLocal
    from app.models import Purchase, PurchaseItem, Supplier
    from app.seed import seed_defaults
    from app.services import (adjustment_service, customer_service, followup_service, inventory_service as inv,
                              refund_service, sales_service, upgrade_service)

    command.upgrade(upgrade_service._alembic_config(f"sqlite:///{out}"), revision)
    seed_defaults()
    with SessionLocal() as db:
        dolo = inv.create_item(db, name="DOLO 650 MG TAB", pack_size="15 S", base_unit="TABLET", pack_unit="STRIP",
                               units_per_pack=15, loose_sale=True, manufacturer="MICRO", category="MEDICINE")
        syp = inv.create_item(db, name="COUGH RELIEF SYP 100ML", pack_size="100ML", base_unit="BOTTLE", pack_unit="BOTTLE",
                              units_per_pack=1, manufacturer="ACME", category="MEDICINE")
        belt = inv.create_item(db, name="ABDOMINAL BELT (L)", pack_size="1", base_unit="UNIT", pack_unit="UNIT", units_per_pack=1)
        d1 = inv.add_or_update_batch(db, dolo, batch_no="D-001", expiry_date=date(2030, 5, 1), quantity=10, unit="PACK",
                                     movement_type="OPENING_STOCK", mrp="32.28", purchase_rate="22.10")
        inv.add_or_update_batch(db, syp, batch_no="S-01", expiry_date=date(2029, 1, 1), quantity=12, unit="PACK",
                                movement_type="OPENING_STOCK", mrp="118.50")               # legacy: no purchase rate
        inv.add_or_update_batch(db, belt, batch_no="B-1", quantity=3, unit="PACK", movement_type="OPENING_STOCK",
                                mrp="1010", purchase_rate="585")
        supplier = Supplier(name="Sample Distributors")
        db.add(supplier)
        db.flush()
        purchase = Purchase(supplier_id=supplier.id, invoice_no="SD-1001", total=442, status="POSTED", purchase_date=date(2026, 9, 1))
        db.add(purchase)
        db.flush()
        db.add(PurchaseItem(purchase_id=purchase.id, item_id=dolo.id, product_name=dolo.name, quantity=20, rate="22.10", line_total=442))
        cust = customer_service.create_customer(db, name="Sample Customer", mobile="9000000001")
        other = customer_service.create_customer(db, name="Second Customer", mobile="9000000002")
        db.commit()
        a = sales_service.create_sale(db, lines=[{"item_id": dolo.id, "quantity": 20, "discount_pct": 5},
                                                 {"item_id": syp.id, "quantity": 2}], customer_id=cust.id,
                                      payment_mode="CASH", cash_received=500)
        sales_service.create_sale(db, lines=[{"item_id": belt.id, "quantity": 1}], customer_id=other.id, payment_mode="SPLIT",
                                  payments=[{"mode": "CASH", "amount": 10}, {"mode": "UPI", "amount": 1000}])
        v = sales_service.create_sale(db, lines=[{"item_id": syp.id, "quantity": 1}])
        sales_service.create_sale(db, lines=[{"name": "Crepe bandage", "quantity": 1, "rate": "85"}], invoice_type="MANUAL")
        db.commit()
        sales_service.void_sale(db, v, reason="Wrong bill")
        refund_service.create_return(db, a, lines=[{"sale_item_id": a.items[0].id, "quantity": 5}], refund_method="CASH")
        adjustment_service.create(db, item=dolo, direction="OUT", category="DAMAGE", quantity=3, reason="Strip damaged", batch=d1)
        followup_service.create(db, customer_id=cust.id, preset="7", reason="REFILL", note="monthly refill", source_sale_id=a.id)
        db.commit()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version")
    p.add_argument("out")
    p.add_argument("--revision", default="head", help="schema revision to freeze (default: this code's head)")
    a = p.parse_args()
    out = Path(a.out).resolve()
    if os.environ.get("_FIXTURE_CHILD") != "1":   # fresh interpreter bound to the fixture database
        if out.exists():
            raise SystemExit(f"{out} exists — release fixtures are never regenerated")
        out.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "_FIXTURE_CHILD": "1", "PHARMACY_DATABASE_URL": f"sqlite:///{out}", "PHARMACY_DB": str(out),
               "PHARMACY_SKIP_MIGRATIONS": "1", "PHARMACY_BACKUPS": "0"}
        subprocess.run([sys.executable, __file__, *sys.argv[1:]], env=env, check=True, cwd=ROOT)
        print(f"Release {a.version} fixture written: {out}")
        return
    sys.path.insert(0, str(ROOT))
    build(out, a.revision)
    import sqlite3

    from app.database import engine

    engine.dispose()
    con = sqlite3.connect(out)          # one self-contained file: no -wal / -shm side files
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()


if __name__ == "__main__":
    main()
