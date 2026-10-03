"""Run the purchase regression corpus and report straight-through rates and resource use.

    python scripts/purchase_corpus.py                      # synthetic corpus → CORPUS_REPORT.md
    PHARMACY_SUPPLIER_CORPUS_DIR=/path python scripts/purchase_corpus.py --private
                                                           # also stage private supplier files (counts only,
                                                           # never written to Git)

Runs on a throw-away database; the pharmacy's own data is never touched. Offline, CPU only.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="purchase_corpus_"))
os.environ["PHARMACY_DATABASE_URL"] = f"sqlite:///{_TMP / 'corpus.db'}"
os.environ["PHARMACY_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["PHARMACY_DATA_DIR"] = str(_TMP / "data")
os.environ["PHARMACY_LOG_DIR"] = str(_TMP / "logs")
os.environ["PHARMACY_SKIP_MIGRATIONS"] = "1"
os.environ.setdefault("PHARMACY_ADMIN_PASSWORD", secrets.token_urlsafe(16))
sys.path.insert(0, str(ROOT))


def _peak_mb() -> float:
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / 1024, 1) if sys.platform != "darwin" else round(peak / 1024 / 1024, 1)
    except ImportError:                       # Windows
        return 0.0


def private_corpus(db) -> list[dict]:
    from app.services import confidence_gate as gate, purchase_automation, purchasing

    folder = Path(os.environ.get("PHARMACY_SUPPLIER_CORPUS_DIR", ""))
    rows = []
    if not folder.is_dir():
        return rows
    sup = purchasing.save_supplier(db, {"name": "Private corpus supplier"})
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in (".csv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg"):
            continue
        started = time.perf_counter()
        try:
            drafts = purchase_automation.import_file(db, path.name, path.read_bytes(), supplier_id=sup.id)
        except purchasing.PurchaseError as exc:
            rows.append({"file": path.name, "error": str(exc)})
            continue
        states = [gate.line_state(l) for d in drafts for l in d.items]
        rows.append({"file": path.name, "lines": len(states), "auto": states.count(gate.AUTO_ACCEPT),
                     "warning": states.count(gate.WARNING), "review": states.count(gate.REVIEW), "blocked": states.count(gate.BLOCK),
                     "packs": sum(bool((l.receipt_decision or {}).get("resolved")) for d in drafts for l in d.items),
                     "ms": int((time.perf_counter() - started) * 1000)})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private", action="store_true", help="also stage files from PHARMACY_SUPPLIER_CORPUS_DIR")
    ap.add_argument("--out", default=str(ROOT / "CORPUS_REPORT.md"))
    args = ap.parse_args()

    from app.database import SessionLocal, reset_db_for_tests
    from app.services import ocr
    from tests import purchase_corpus as corpus

    reset_db_for_tests()
    db = SessionLocal()
    t0, wall0 = os.times(), time.perf_counter()
    results = corpus.run(db)
    t1, wall = os.times(), time.perf_counter() - wall0
    cpu = (t1.user + t1.system + t1.children_user + t1.children_system) - (t0.user + t0.system + t0.children_user + t0.children_system)
    summary = corpus.summarize(results)
    private = private_corpus(db) if args.private else []
    db.rollback()

    targets = {"STRUCTURED": 98.0, "PDF_TEXT": 95.0, "OCR": 90.0}
    names = {"STRUCTURED": "Known supplier, CSV / XLSX", "PDF_TEXT": "Known supplier, born-digital PDF", "OCR": "Known supplier, clear scan / photo"}
    out = ["# Purchase corpus report", "",
           f"Synthetic, anonymised corpus (`tests/purchase_corpus.py`): {len(results)} invoices from {len({r.supplier for r in results})} suppliers, "
           f"{sum(r.lines for r in results)} lines. OCR engine available: {'yes' if ocr.available() else 'no'}.", "",
           "## Straight-through on recurring invoices", "",
           "| Route | Target | Known-product lines | Straight-through (known) | All lines incl. traps | Wrong auto-accepts |",
           "|---|---|---|---|---|---|"]
    for route, t in targets.items():
        s = summary[route]
        st = "n/a" if s["known_straight_through"] is None else f"{s['known_straight_through']}%"
        out.append(f"| {names[route]} | {t}% | {s['known_lines']} | {st} | {s['all_lines_straight_through']}% | {s['wrong_auto_accepts']} |")
    out += ["", f"**Wrong auto-accepts on the whole corpus: {summary['wrong_auto_accepts_total']}.**", "",
            "## Per invoice", "", "| Invoice | Route | First / recurring | Lines | Auto | Warning | Review | Blocked | Packs resolved | Product right | ms |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        out.append(f"| {r.invoice} | {r.route} | {'recurring' if r.recurring else 'first'} | {r.lines} | {r.auto} | {r.warning} | {r.review} | "
                   f"{r.blocked} | {r.packs_resolved} | {r.product_right} | {r.ms} |")
    out += ["", "Lines left for a person on recurring invoices are the deliberate traps: a strength the catalogue does not "
            "carry (5 mg against 10 mg), a missing release marker (plain against SR), an unreadable pack (`10ML57`) and a "
            "product the pharmacy has never stocked. First invoices from a supplier go to review until a person confirms "
            "products and packs once; that is what makes the recurring ones pass.", "",
            "## Resource use", "",
            f"CPU time {cpu:.2f} s (including the OCR engine), wall time {wall:.2f} s for the whole corpus, "
            f"peak memory {_peak_mb()} MB (Python process, including the app). OCR runs only for scans; nothing runs while idle. No GPU, no network."]
    if private:
        out += ["", "## Private supplier files (counts only)", "", "| File | Lines | Auto | Warning | Review | Blocked | Packs resolved | ms |",
                "|---|---|---|---|---|---|---|---|"]
        for p in private:
            if "error" in p:
                out.append(f"| {p['file']} | refused: {p['error'][:80]} | | | | | | |")
            else:
                out.append(f"| {p['file']} | {p['lines']} | {p['auto']} | {p['warning']} | {p['review']} | {p['blocked']} | {p['packs']} | {p['ms']} |")
        out += ["", "Private files are staged against an empty catalogue with no supplier history, so every product is new "
                "here: these rows show reading and pack resolution, not straight-through."]
    Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main()
