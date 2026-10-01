"""Read-only corpus audit. Never imports into the ERP database or posts stock."""
import argparse
import collections
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.purchase_import import parse
from app.services.receipt_decision import quantities
from app.services.units import parse_pack


def audit_file(path):
    data = path.read_bytes()
    doc = parse(path.name, data)
    parts = doc.parts or [doc]
    report = {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "invoices": len(parts),
              "rows": sum(len(p.lines) for p in parts), "documents": [], "pack_kinds": {}, "quantity_errors": []}
    packs = collections.Counter()
    for part in parts:
        paid_total, free_total, amount_total = Decimal(0), Decimal(0), Decimal(0)
        examples = []
        for line in part.lines:
            raw = line.raw
            pack = parse_pack(raw.get("pack"))
            packs[pack.kind] += 1
            try:
                paid, free = quantities(raw)
                paid_total += paid
                free_total += free
                if (paid % 1 or free % 1) and len(examples) < 3:
                    examples.append({"name":raw.get("name"), "pack":raw.get("pack"), "paid":str(paid), "free":str(free),
                                     "total_invoice_units":str(paid+free)})
            except ValueError as exc:
                report["quantity_errors"].append({"row":line.row,"name":raw.get("name"),"quantity":raw.get("quantity"),"error":str(exc)})
            try:
                amount_total += Decimal(raw.get("amount") or "0")
            except Exception:
                pass
        report["documents"].append({"invoice_no":part.invoice_no,"supplier":part.supplier_name,
            "invoice_date":part.invoice_date,"rows":len(part.lines),"printed_total":str(part.declared_total) if part.declared_total is not None else None,
            "sum_printed_line_amounts":str(amount_total),"sum_valid_paid_quantity":str(paid_total),"sum_valid_free_quantity":str(free_total),
            "source_controls":part.charges,"warnings":part.warnings,"fractional_examples":examples})
    report["pack_kinds"] = dict(packs)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, action="append", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = {"mode":"read_only_no_stock_effect","files":[audit_file(p) for p in args.file]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps({"files":len(result["files"]),"rows":sum(f["rows"] for f in result["files"]),"output":str(args.output)}))


if __name__ == "__main__":
    main()
