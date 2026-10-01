"""Generate a synthetic medicine catalogue and benchmark FTS search at scale.

The real India medicine database (300k+ items) is not bundled with the repo.
When it is supplied as CSV/Excel, use scripts/import_medicines.py instead. This
script exists to prove that search stays fast at 300k+ rows using SQLite FTS5.

Usage:
    .venv/bin/python scripts/benchmark_inventory.py --count 300000
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MANUFACTURERS = [
    "Cipla", "Sun Pharma", "Dr Reddy's", "Lupin", "Aurobindo", "Torrent",
    "Mankind", "Zydus", "Alkem", "Intas", "Abbott", "GSK", "Himalaya",
    "Dabur", "Micro Labs", "USV", "Emcure", "Glenmark", "Ajanta", "Hetero",
]
FORMS = ["Tablet", "Capsule", "Syrup", "Injection", "Ointment", "Drops", "Suspension", "Gel", "Sachet"]
ROOTS = [
    "Paracetamol", "Amoxicillin", "Azithromycin", "Cetirizine", "Pantoprazole",
    "Omeprazole", "Metformin", "Atorvastatin", "Amlodipine", "Losartan",
    "Ibuprofen", "Diclofenac", "Ranitidine", "Ondansetron", "Domperidone",
    "Cefixime", "Cefpodoxime", "Levofloxacin", "Montelukast", "Levocetirizine",
    "Vitamin C", "Vitamin D3", "Calcium", "Iron", "Zinc", "ORS", "Albendazole",
    "Fluconazole", "Acyclovir", "Prednisolone", "Salbutamol", "Budesonide",
    "Insulin", "Thyroxine", "Clopidogrel", "Aspirin", "Metoprolol", "Telmisartan",
    "Glimepiride", "Sitagliptin", "Tramadol", "Amitriptyline", "Sertraline",
]
STRENGTHS = ["5mg", "10mg", "20mg", "25mg", "40mg", "50mg", "100mg", "125mg",
             "250mg", "500mg", "650mg", "1g", "2mg", "4mg", "8mg", "10ml", "5ml", "100ml"]
CATEGORIES = ["PHARMA", "GENERIC", "FMCG", "SURGICAL", "BABY", "BEVERAGES", "AYURVEDIC"]


def generate(count: int):
    rng = random.Random(42)
    seen = set()
    i = 0
    now = datetime.utcnow().isoformat(sep=" ")
    while i < count:
        name = (
            f"{rng.choice(ROOTS)} {rng.choice(STRENGTHS)} {rng.choice(FORMS)} "
            f"{rng.choice(['', 'DS', 'Plus', 'Forte', 'SR', 'XR', 'CV'])} {i}"
        ).strip()
        if name in seen:
            i += 1
            continue
        seen.add(name)
        yield {
            "article_id": f"ART{i:07d}",
            "name": name,
            "generic_name": name.split()[0],
            "manufacturer": rng.choice(MANUFACTURERS),
            "category": rng.choice(CATEGORIES),
            "pack_size": rng.choice(["1x10", "1x15", "10x10", "1x1", "100ml", "1x30"]),
            "strength": rng.choice(STRENGTHS),
            "unit": "unit",
            "hsn_code": "3004",
            "gst_rate": 12,
            "mrp": round(rng.uniform(5, 800), 2),
            "barcode": f"89{i:011d}",
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        }
        i += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=300000)
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    from sqlalchemy import create_engine, text

    db_path = args.db or str(Path(tempfile.mkdtemp(prefix="pharmacy_bench_")) / "bench.db")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    print(f"DB: {db_path}")

    # Build schema via metadata + FTS
    import app.models  # noqa: F401
    from app.database import Base, FTS_DDL

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in FTS_DDL:
            conn.execute(text(ddl))
        # Drop FTS triggers during bulk load for speed.
        for trig in ("items_fts_au", "items_fts_ad", "items_fts_ai"):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trig}"))

    rows = list(generate(args.count))
    from app.models import Item

    cols = [c.name for c in Item.__table__.columns if c.name != "id"]
    insert_sql = text(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    )

    t0 = time.time()
    batch_size = 5000
    with engine.begin() as conn:
        for start in range(0, len(rows), batch_size):
            conn.execute(insert_sql, rows[start:start + batch_size])
    load_time = time.time() - t0
    print(f"Inserted {len(rows):,} items in {load_time:.1f}s")

    t0 = time.time()
    with engine.begin() as conn:
        for ddl in FTS_DDL:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO items_fts(items_fts) VALUES('rebuild')"))
    print(f"FTS rebuild in {time.time() - t0:.1f}s")

    queries = ["paracetamol", "azithromycin 500mg", "vitamin", "metformin 500mg tablet", "cetirizine"]
    print("\nSearch timings (FTS5):")
    for q in queries:
        t0 = time.time()
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(
                text("SELECT rowid FROM items_fts WHERE items_fts MATCH :m LIMIT 2000"),
                {"m": " ".join(f"{t}*" for t in q.split())},
            ).fetchall()]
            if ids:
                rows_found = conn.execute(
                    text(f"SELECT name FROM items WHERE id IN ({','.join(str(i) for i in ids[:20])})")
                ).fetchall()
            else:
                rows_found = []
        print(f"  {q!r:32} -> {len(ids)} hits in {(time.time()-t0)*1000:.1f} ms (e.g. {rows_found[0][0] if rows_found else '—'})")

    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
    print(f"\nTotal items: {total:,}")


if __name__ == "__main__":
    main()
