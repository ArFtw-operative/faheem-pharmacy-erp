"""SupplierRecognizer and SupplierInvoiceProfile.

The supplier is identified, in order, from the seller GSTIN printed on the invoice (exact),
the printed supplier name (exact, normalised), and the supplier's learned invoice profile:
file layout (header tokens and column roles) together with the shape of its invoice
numbers (``NR03897`` → ``NR#####``). A layout alone is not enough — many distributors
export the same billing-software layout — so a profile assigns the supplier only when the
layout and the invoice-number shape both point to exactly one supplier; anything weaker is
offered as a suggestion. Profiles are learned from posted invoices (confirmed by a person).
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Purchase, Supplier, SupplierInvoiceProfile
from app.utils import utcnow

ASSIGN_CONFIDENCE = 0.96          # two independent signals agree: assigned, shown with a warning


def _header_tokens(purchase: Purchase) -> list[str]:
    from app.services import column_mapper

    return sorted({column_mapper.header_key(c.get("column", "")) for c in (purchase.column_map or []) if c.get("column")})


def layout_key(purchase: Purchase) -> str:
    tokens = _header_tokens(purchase)
    if not tokens:
        return ""
    return hashlib.sha256(json.dumps([purchase.source_format, tokens]).encode()).hexdigest()[:32]


def roles(purchase: Purchase) -> list:
    from app.services import purchase_automation

    return [list(r) for r in purchase_automation.layout(purchase)]


def invoice_shape(invoice_no: str) -> str:
    """``NR03897`` → ``NR#####``; ``0000000028_C1`` → ``##########_C#``; blank → blank."""
    text = str(invoice_no or "").strip().upper()
    return re.sub(r"\d", "#", text)[:30] if re.search(r"\d", text) else ""


def name_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return " ".join(w for w in words if w not in {"m", "s", "ms", "pvt", "ltd", "limited", "private", "the", "and", "co"})


def learn(db: Session, purchase: Purchase) -> SupplierInvoiceProfile | None:
    """A posted invoice teaches its supplier's layout and invoice-number shape."""
    if not purchase.supplier_id or purchase.source_format == "MANUAL" or not purchase.column_map:
        return None
    key = layout_key(purchase)
    if not key:
        return None
    row = db.scalar(select(SupplierInvoiceProfile).where(SupplierInvoiceProfile.supplier_id == purchase.supplier_id,
                                                         SupplierInvoiceProfile.layout_key == key))
    if row is None:
        row = SupplierInvoiceProfile(supplier_id=purchase.supplier_id, layout_key=key, source_format=purchase.source_format,
                                     invoices=0, invoice_patterns=[])
        db.add(row)
    row.header_tokens = _header_tokens(purchase)
    row.column_roles = roles(purchase)
    shape = invoice_shape(purchase.invoice_no)
    if shape and shape not in (row.invoice_patterns or []):
        row.invoice_patterns = [*(row.invoice_patterns or []), shape][-10:]
    meta = purchase.extraction_meta or ""
    method = re.search(r"read by (\w+)", meta)
    row.parser = (method.group(1) if method else purchase.source_format)[:30]
    row.invoices = (row.invoices or 0) + 1
    row.confidence = min(1, float(row.confidence or 0.9) + 0.01)
    row.last_seen_at = utcnow()
    db.flush()
    return row


def _patterns_by_supplier(db: Session) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for p in db.scalars(select(SupplierInvoiceProfile)):
        out.setdefault(p.supplier_id, set()).update(p.invoice_patterns or [])
    return out


def recognize(db: Session, purchase: Purchase) -> dict | None:
    """Identify the supplier of a draft that has none. Assigns only on two agreeing signals."""
    if purchase.supplier_id:
        return None
    evidence = (purchase.charges or {}).get("_supplier_evidence") or {}
    if evidence.get("name"):
        want = name_key(evidence["name"])
        hits = [s for s in db.scalars(select(Supplier).where(Supplier.is_active.is_(True))) if name_key(s.name) == want]
        if len(hits) == 1:
            return _assign(db, purchase, hits[0].id, 0.98, ["printed supplier name matches the supplier master"])
    key, my_roles, shape = layout_key(purchase), roles(purchase), invoice_shape(purchase.invoice_no)
    layout_hits: set[int] = set()
    for p in db.scalars(select(SupplierInvoiceProfile)):
        if (key and p.layout_key == key) or (my_roles and p.column_roles == my_roles):
            layout_hits.add(p.supplier_id)
    shape_hits = {sid for sid, pats in _patterns_by_supplier(db).items() if shape and shape in pats}
    both = layout_hits & shape_hits
    if len(both) == 1 and len(shape_hits) == 1:
        sid = next(iter(both))
        return _assign(db, purchase, sid, ASSIGN_CONFIDENCE,
                       ["file layout matches this supplier's earlier invoices", f"invoice number shape {shape} is this supplier's"])
    suggestions = sorted(shape_hits | layout_hits)
    if suggestions:
        names = {s.id: s.name for s in db.scalars(select(Supplier).where(Supplier.id.in_(suggestions)))}
        info = {"assigned": False, "suggestions": [
            {"supplier_id": sid, "name": names.get(sid, ""), "signals": [x for x, ok in (("same file layout", sid in layout_hits),
                                                                                         (f"invoice numbers like {shape}", sid in shape_hits)) if ok]}
            for sid in suggestions]}
        purchase.charges = {**(purchase.charges or {}), "_supplier_recognition": info}
        return info
    return None


def _assign(db: Session, purchase: Purchase, supplier_id: int, confidence: float, signals: list[str]) -> dict:
    from app.services import mapping_store

    info = {"assigned": True, "supplier_id": supplier_id, "confidence": confidence, "signals": signals}
    purchase.supplier_id = supplier_id
    purchase.supplier = db.get(Supplier, supplier_id)
    purchase.charges = {**(purchase.charges or {}), "_supplier_recognition": info}
    mapping_store.history(db, "SUPPLIER", "CREATE", supplier_id=supplier_id, key=purchase.invoice_no,
                          after=info, purchase_id=purchase.id)
    return info


def bootstrap(db: Session) -> int:
    """Profiles from every posted invoice that has a supplier and a column layout."""
    n = 0
    for p in db.scalars(select(Purchase).where(Purchase.status.in_(("POSTED", "PARTIAL")), Purchase.supplier_id.is_not(None))):
        if learn(db, p) is not None:
            n += 1
    from app.services import settings_service

    # layouts preserved from reviewed receipts before a data reset (line columns only)
    seen = {(r.supplier_id, r.layout_key) for r in db.scalars(select(SupplierInvoiceProfile))}
    for fact in json.loads(settings_service.get_setting(db, "purchase_reviewed_pack_knowledge", "[]") or "[]"):
        sid, layout = fact.get("supplier_id"), [list(x) for x in fact.get("layout") or []]
        if not sid or not layout or db.get(Supplier, sid) is None:
            continue
        lk = "roles:" + hashlib.sha256(json.dumps(layout).encode()).hexdigest()[:24]
        if (sid, lk) in seen:
            continue
        seen.add((sid, lk))
        db.add(SupplierInvoiceProfile(supplier_id=sid, layout_key=lk, column_roles=layout, invoice_patterns=[],
                                      parser="REVIEWED_HISTORY", invoices=0, source_format=""))
        n += 1
    db.flush()
    return n
