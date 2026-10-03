"""PurchaseProcessingEngine: one orchestration for supplier invoices.

    file → FileTypeRouter (purchase_import: CSV/XLSX/XLS · text PDF · scanned PDF/photo → OCR)
         → canonical invoice (RawDocument → staged PurchaseItem lines, raw values kept)
         → SupplierRecognizer (GSTIN → name → learned layout + invoice-number shape)
         → ColumnRoleMapper (column_mapper, learned per supplier)
         → ProductMatcher (aliases → codes → exact → normalised attributes; fuzzy only suggests)
         → PackagingResolver (packaging_parser + product master + packaging aliases + reviewed history)
         → BatchExpiryResolver, InvoiceReconciler (purchasing._normalise, reconciliation)
         → ConfidenceGate (per-field confidence → AUTO_ACCEPT / WARNING / REVIEW / BLOCK)
         → post (atomic, idempotent) or the exception inbox; corrections feed the mapping store.

UI code never matches, parses or converts; it reads the structured results.
"""
from __future__ import annotations

import json
import time
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import LOG_DIR
from app.models import ImportMetric, Purchase
from app.services import settings_service

BOOTSTRAP_VERSION = "1"
REPORT = LOG_DIR / "purchase-engine-bootstrap.json"


# --------------------------------------------------------------------------- bootstrap
def bootstrap(db: Session) -> dict:
    from app.services import mapping_store, packaging_store

    packaging = packaging_store.bootstrap(db)
    aliases = mapping_store.bootstrap(db)
    report = {"product_packaging": packaging, "aliases": aliases}
    try:
        REPORT.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass
    settings_service.set_setting(db, "purchase_engine_bootstrap", BOOTSTRAP_VERSION)
    return report


def bootstrap_once(db: Session) -> dict | None:
    if settings_service.get_setting(db, "purchase_engine_bootstrap", "") == BOOTSTRAP_VERSION:
        return None
    return bootstrap(db)


def summary(report: dict) -> str:
    p, a = report["product_packaging"], report["aliases"]
    return (f"{p['created']} product packs recorded, {len(p['skipped'])} left for review (pack text ambiguous or "
            f"disagreeing with the product); {a['created']} supplier aliases created, {a['confirmed']} confirmed from history, "
            f"{len(a['ambiguous'])} ambiguous pairs listed, {a['packaging_aliases']} packaging aliases from reviewed receipts")


# --------------------------------------------------------------------------- metrics
def route_of(purchase: Purchase) -> str:
    if (purchase.charges or {}).get("_ocr_confidence") is not None:
        return "OCR"
    return "PDF_TEXT" if purchase.source_format == "PDF" else ("MANUAL" if purchase.source_format == "MANUAL" else "STRUCTURED")


def record(db: Session, purchase: Purchase, *, route: str, started: float, ocr_confidence=None) -> ImportMetric:
    """One row per processed invoice: how much went straight through, and how long it took."""
    from app.services import confidence_gate

    counts = Counter(confidence_gate.line_state(l) for l in purchase.items)
    row = ImportMetric(purchase_id=purchase.id, supplier_id=purchase.supplier_id, source_format=purchase.source_format,
                       route=route, lines=len(purchase.items), auto_accepted=counts[confidence_gate.AUTO_ACCEPT],
                       with_warning=counts[confidence_gate.WARNING], review=counts[confidence_gate.REVIEW],
                       blocked=counts[confidence_gate.BLOCK],
                       corrected=sum(1 for l in purchase.items if any(not k.startswith("_") for k in (l.corrections or {}))),
                       packaging_resolved=sum(1 for l in purchase.items if (l.receipt_decision or {}).get("resolved")),
                       ocr_confidence=ocr_confidence, processing_ms=int((time.perf_counter() - started) * 1000))
    db.add(row)
    db.flush()
    return row


def metrics(db: Session) -> dict:
    rows = db.execute(select(ImportMetric.supplier_id, func.count(ImportMetric.id), func.sum(ImportMetric.lines),
                             func.sum(ImportMetric.auto_accepted), func.sum(ImportMetric.with_warning),
                             func.sum(ImportMetric.review), func.sum(ImportMetric.blocked), func.sum(ImportMetric.corrected),
                             func.sum(ImportMetric.packaging_resolved), func.avg(ImportMetric.processing_ms))
                      .group_by(ImportMetric.supplier_id)).all()
    out = []
    for sid, n, lines, auto, warn, review, blocked, corrected, packs, ms in rows:
        lines = lines or 0
        out.append({"supplier_id": sid, "invoices": n, "lines": lines, "auto_accepted": auto or 0, "with_warning": warn or 0,
                    "review": review or 0, "blocked": blocked or 0, "corrected": corrected or 0,
                    "straight_through": round(100 * (auto or 0) / lines, 1) if lines else 0.0,
                    "packaging_resolved": round(100 * (packs or 0) / lines, 1) if lines else 0.0,
                    "avg_ms": int(ms or 0)})
    return {"suppliers": out}


def metrics_text(db: Session) -> str:
    data = metrics(db)["suppliers"]
    if not data:
        return "No invoices processed yet."
    lines = ["supplier  invoices  lines  auto  warn  review  blocked  straight-through  packs-resolved  avg-ms"]
    for r in data:
        lines.append(f"{str(r['supplier_id'] or '-'):>8}  {r['invoices']:>8}  {r['lines']:>5}  {r['auto_accepted']:>4}  {r['with_warning']:>4}  "
                     f"{r['review']:>6}  {r['blocked']:>7}  {r['straight_through']:>15}%  {r['packaging_resolved']:>13}%  {r['avg_ms']:>6}")
    return "\n".join(lines)
