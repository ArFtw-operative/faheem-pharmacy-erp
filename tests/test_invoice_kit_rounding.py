"""The studio invoice must reconcile exactly as the invoice engine checks it —
also for loose tablets, whose rounded per-unit MRP x qty is not what was charged
(₹32.28 strip / 15 = 2.152; 10 tablets bill ₹21.52, not 10 x 2.15)."""
from __future__ import annotations

from app.services import inventory_service as inv, invoice_kit, sales_service


def _engine_errors(view: dict) -> list[str]:
    """The same checks as invoice-engine.js validate()."""
    t, items, errors = view["totals"], view["items"], []
    if t["subtotalPaise"] - t["discountPaise"] + t["taxPaise"] + t["roundingPaise"] != t["totalPaise"]:
        errors.append("Invoice totals do not reconcile.")
    if sum(i["amount"] for i in items) + t["roundingPaise"] != t["totalPaise"]:
        errors.append("Line totals do not match net amount.")
    if t["totalPaise"] - t["paidPaise"] != t["duePaise"]:
        errors.append("Received and balance due do not reconcile.")
    return errors


def test_loose_units_with_discount_and_round_off_reconcile(db):
    item = inv.create_item(db, name="DOLO 650 MG TAB", pack_size="15 S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET", loose_sale=True)
    inv.add_or_update_batch(db, item, batch_no="DOBS4511", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="32.28")
    pan = inv.create_item(db, name="PAN IV INJ", pack_size="VIAL", base_unit="VIAL", pack_unit="VIAL", units_per_pack=1)
    inv.add_or_update_batch(db, pan, batch_no="PV1", quantity=5, unit="PACK", movement_type="OPENING_STOCK", mrp="54.24")
    db.commit()
    for lines in (
        [{"item_id": item.id, "quantity": 10, "discount_pct": 2}],
        [{"item_id": item.id, "quantity": 7, "discount_pct": 2}, {"item_id": pan.id, "quantity": 2, "discount_pct": 2}],
    ):
        sale = sales_service.create_sale(db, lines=lines, payment_mode="CASH", cash_received=500)
        db.commit()
        assert _engine_errors(invoice_kit.build_invoice_view(db, sale)) == []
