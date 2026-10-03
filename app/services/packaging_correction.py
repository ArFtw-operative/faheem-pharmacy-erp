"""Correction dialog for a questionable pack: what one invoice Qty is, saved at the chosen scope.

    Supplier packing: 10X1
    Interpreted as:   purchase quantity / unit · retail quantity / unit · units per retail pack / base unit · container
    [Save for this invoice]  [Save as product packaging]  [Save supplier alias]

Saving writes data only (the line's confirmation, ProductPackaging, the supplier packing
alias); it never changes code. Billed and free quantities are never touched.
"""
from __future__ import annotations

from app.models import Purchase, PurchaseItem, User
from app.services import packaging_parser as pp

LEVELS = ("RETAIL", "OUTER", "BASE")
SCOPES = ("invoice", "product", "supplier")


def proposal(line: PurchaseItem) -> dict:
    """The dialog's starting point: the parser's reading plus the product's own definition."""
    item = line.item
    parsed = pp.parse(line.pack_size, master_form=(item.dosage_form if item else line.dosage_form or ""),
                      description=line.product_name)
    from app.services import receipt_decision

    d = receipt_decision.definition(line)
    upp = d.get("units_per_pack") or (parsed.retail_to_base or 1)
    outer = parsed.retail.quantity if parsed.retail and parsed.purchase and parsed.purchase.unit == "BOX" else 1
    stamp = (line.corrections or {}).get("_invoice_unit") or {}
    factor = stamp.get("units_per_invoice_unit")
    level = "RETAIL"
    if factor and factor == 1 and upp > 1:
        level = "BASE"
    elif factor and upp and factor > upp:
        level = "OUTER"
    return {"raw": line.pack_size, "reading": parsed.as_dict(), "units_per_retail": upp, "retail_per_outer": outer,
            "base_unit": d.get("base_unit") or (parsed.base.unit if parsed.base else "") or "UNIT",
            "pack_unit": d.get("pack_unit") or "PACK", "form": d.get("dosage_form") or parsed.dosage_form or "",
            "level": level, "scope": stamp.get("scope_choice", "supplier"),
            "container": parsed.as_dict()["content"]}


def apply(db, purchase: Purchase, line: PurchaseItem, values: dict, *, user: User | None = None) -> PurchaseItem:
    from app.services import form_service, mapping_store, packaging_store, purchase_adjustment, purchasing, receipt_decision

    purchasing._open(purchase)
    purchasing._open_line(line)
    level = str(values.get("level") or "RETAIL").upper()
    scope = str(values.get("scope") or "supplier").lower()
    if level not in LEVELS or scope not in SCOPES:
        raise purchasing.PurchaseError("Choose what one invoice Qty is and where to save it")
    try:
        n = int(str(values.get("units_per_retail") or "1"))
        outer = int(str(values.get("retail_per_outer") or "1"))
    except ValueError:
        raise purchasing.PurchaseError("Counts must be whole numbers")
    if not (1 <= n <= 10000 and 1 <= outer <= 1000):
        raise purchasing.PurchaseError("Units per retail pack must be 1–10000 and retail packs per box 1–1000")
    form = str(values.get("form") or "")
    spec = form_service.get(db, form) if form else None
    if form and spec is None:
        raise purchasing.PurchaseError("Choose the item form")
    paid, free = receipt_decision.quantities(receipt_decision.effective(line))
    current = receipt_decision.definition(line)
    want_def = None
    if spec is not None:
        want_def = dict(base_unit=spec["base_unit"], pack_unit=spec["pack_unit"], units_per_pack=n, dosage_form=spec["dosage_form"])
    changed = want_def is not None and want_def != {k: current.get(k) for k in want_def}
    if changed:
        # the product's own pack definition changes: staged and applied when the purchase is posted
        purchase_adjustment.adjust(db, purchase, line, {"form": form, "units_per_pack": str(n),
                                                        "quantity": str(paid), "free": str(free)}, user=user)
    elif line.item is None and not line.new_product:
        raise purchasing.PurchaseError("Match the product (F4) or create it as new (Shift+F4) first, or choose its form here")
    factor = {"RETAIL": n, "OUTER": n * outer, "BASE": 1}[level]
    reason = (values.get("reason") or "").strip() or f"Packing {line.pack_size!r} confirmed in the correction dialog"
    receipt_decision.confirm(db, purchase, line, factor=factor, mrp_basis="MASTER_PACK",
                             reason=reason if len(reason) >= 5 else reason + " (confirmed)", user=user)
    stamp = {**line.corrections["_invoice_unit"], "scope_choice": scope}
    if scope == "invoice":
        stamp["invoice_only"] = True               # not remembered for later invoices
    line.corrections = {**line.corrections, "_invoice_unit": stamp}
    if line.item is not None and scope in ("product", "supplier") and not changed:
        parsed = pp.parse(line.pack_size, master_form=line.item.dosage_form, description=line.product_name)
        packaging_store.upsert(db, line.item, parsed, source="USER_CORRECTION", verified=True,
                               purchase_to_retail=outer if level == "OUTER" else 1, retail_to_base=n,
                               retail_unit=line.item.pack_unit, base_unit=line.item.base_unit,
                               purchase_unit="BOX" if level == "OUTER" else line.item.pack_unit, user=user)
        if scope == "supplier" and purchase.supplier_id:
            mapping_store.save_packaging_alias(db, supplier_id=purchase.supplier_id, pack=line.pack_size, item=line.item,
                                               units_per_invoice_unit=factor, mrp_basis="MASTER_PACK",
                                               source="USER_CORRECTION", purchase=purchase, line=line, user=user)
    purchasing.refresh_line(db, purchase, line)
    purchasing._refresh_totals(purchase)
    db.flush()
    return line
