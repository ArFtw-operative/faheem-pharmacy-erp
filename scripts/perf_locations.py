"""Rack / box location performance at pharmacy scale (run by hand; not part of the test suite).

    PHARMACY_DATABASE_URL=sqlite:////tmp/perf.db PHARMACY_SKIP_MIGRATIONS=1 python scripts/perf_locations.py

Builds 10,000 products, 50,000 batches (with ledger rows), 100 racks and 500 boxes in an empty
database, then measures POS search before and after every product has a location, a 500-product
bulk move, the inventory list with a location filter, and the rack report as of today and as of a
past day. Prints a table; nothing is asserted (the numbers go into docs/LOCATIONS.md).
"""
from __future__ import annotations

import random
import statistics
import time
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import insert, select, text

from app.database import SessionLocal, reset_db_for_tests
from app.models import Batch, InventoryMovement, Item, Rack, RackBox
from app.services import location_service as loc
from app.utils import utcnow

N_ITEMS, N_BATCHES, N_RACKS, BOXES_PER_RACK = 10_000, 50_000, 100, 5
WORDS = ["DOLO", "CALPOL", "AZEE", "AUGMENTIN", "PAN", "SHELCAL", "ZINCOVIT", "BETADINE", "MOOV", "VOLINI", "CROCIN",
         "ALLEGRA", "MONTEK", "TELMA", "GLYCOMET", "ECOSPRIN", "ROSUVAS", "LIMCEE", "BECOSULES", "DIGENE"]


def timed(fn, repeat=1):
    out = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return statistics.median(out), max(out)


def build(db):
    random.seed(7)
    now = utcnow()
    db.execute(insert(Item), [{"article_id": f"P{i:06d}", "name": f"{random.choice(WORDS)} {random.randint(1, 999)} {random.choice(['TAB', 'SYP', 'CAP', 'GEL'])} {i}",
                                "category": random.choice(["PHARMA", "FMCG", "GENERIC", "SURGICAL"]), "base_unit": "TABLET", "pack_unit": "STRIP",
                                "units_per_pack": 10, "created_at": now, "updated_at": now} for i in range(N_ITEMS)])
    ids = list(db.scalars(select(Item.id)))
    rows = []
    for b in range(N_BATCHES):
        rows.append({"item_id": ids[b % N_ITEMS], "batch_no": f"B{b}", "batch_no_normalized": f"B{b}", "expiry_date": date(2027, 1, 1) + timedelta(days=b % 900),
                     "mrp": Decimal("50"), "unit_mrp": Decimal("5"), "units_per_pack": 10, "quantity": 20, "created_at": now - timedelta(days=40), "updated_at": now})
    db.execute(insert(Batch), rows)
    db.execute(insert(InventoryMovement), [{"item_id": b.item_id, "batch_id": b.id, "movement_type": "OPENING_STOCK", "quantity": 20, "balance_after": 20,
                                            "created_at": now - timedelta(days=40)} for b in db.scalars(select(Batch))])
    racks = [Rack(code=f"R-{r:03d}", name=f"Rack {r}", sort_order=r) for r in range(N_RACKS)]
    db.add_all(racks)
    db.flush()
    db.add_all([RackBox(rack_id=r.id, code=f"B{k:02d}") for r in racks for k in range(BOXES_PER_RACK)])
    db.commit()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("INSERT INTO items_fts(items_fts) VALUES('rebuild')"))
        db.commit()
    return ids, racks


def main():
    reset_db_for_tests()
    db = SessionLocal()
    t = time.perf_counter()
    ids, racks = build(db)
    print(f"built {N_ITEMS} products, {N_BATCHES} batches, {N_RACKS} racks, {N_RACKS * BOXES_PER_RACK} boxes in {time.perf_counter() - t:.1f}s")
    loc.set_config(db, {"location_boxes_enabled": True})
    db.commit()

    from fastapi.testclient import TestClient

    from app.main import app
    from tests.conftest import login
    client = TestClient(app)
    login(client)
    terms = ["dolo", "calpol 5", "azee", "betadine", "pan 4", "moov", "r-007"]

    def search():
        for q in terms:
            assert client.get(f"/api/erp/pos/search?q={q}").status_code == 200

    rows = []
    rows.append(("POS search ×7 terms, no locations", *timed(search, 5)))
    # every product gets a rack and a box (bulk, server side), 1,000 at a time
    boxes = {r.id: [b.id for b in r.boxes] for r in racks}
    start = time.perf_counter()
    for i in range(0, N_ITEMS, 1000):
        r = racks[(i // 1000) % N_RACKS]
        loc.assign(db, ids[i:i + 1000], r.id, boxes[r.id][0], source="BULK")
        db.commit()
    rows.append(("assign 10,000 products (10 ops of 1,000)", (time.perf_counter() - start) * 1000, 0))
    rows.append(("POS search ×7 terms, every product located", *timed(search, 5)))
    move = ids[:500]
    rows.append(("bulk move 500 products (one request)", *timed(lambda: client.post("/api/erp/locations/assign", json={
        "item_ids": move, "rack_id": racks[50].id, "box_id": boxes[racks[50].id][1]}).raise_for_status())))
    rows.append(("inventory list, location filter (200 rows)", *timed(lambda: client.get(f"/api/erp/inventory?location=rack:{racks[3].id}").raise_for_status(), 5)))
    rows.append(("inventory list sorted by rack (200 rows)", *timed(lambda: client.get("/api/erp/inventory?sort=rack").raise_for_status(), 5)))
    gen = lambda p: client.post("/reports/api/generate", json={"report": "rack-inventory", "parameters": p}).raise_for_status()
    rows.append(("rack report, one rack, today", *timed(lambda: gen({"racks": str(racks[3].id)}), 3)))
    rows.append(("rack report, all racks, today (50k rows)", *timed(lambda: gen({}), 1)))
    rows.append(("rack report, one rack, as of 10 days ago", *timed(lambda: gen({"racks": str(racks[3].id), "as_of": (date.today() - timedelta(days=10)).isoformat()}), 3)))
    rows.append(("rack timeline, 30 days", *timed(lambda: client.get(f"/api/erp/racks/{racks[3].id}/timeline?days=30").raise_for_status(), 3)))
    rows.append(("racks list with statistics (100 racks)", *timed(lambda: client.get("/api/erp/racks").raise_for_status(), 3)))
    print(f"\n{'measure':48} {'median ms':>10} {'max ms':>8}")
    for name, med, mx in rows:
        print(f"{name:48} {med:10.0f} {mx:8.0f}")


if __name__ == "__main__":
    main()
