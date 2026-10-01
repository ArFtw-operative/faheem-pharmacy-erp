"""Phase 7 reporting, expiry, purchases/returns."""
from __future__ import annotations

from datetime import date

from app.models import Batch
from app.services import inventory_service as inv
from app.services import purchase_service, reports_service, sales_service
from app.utils import utcnow


def seed_item(db, name, qty=100, selling="10.00", category="PHARMA", manufacturer="Cipla"):
    item = inv.create_item(db, name=name, category=category, manufacturer=manufacturer)
    batch = inv.add_or_update_batch(
        db, item, batch_no="R1", expiry_date=date(2027, 1, 1), quantity=qty,
        purchase_rate="5.00", selling_rate=selling, mrp="12.00",
    )
    db.commit()
    return item, batch


def test_sales_summary_includes_discount_and_voucher(db):
    item, batch = seed_item(db, "ReportMed A")
    batch.mrp = batch.unit_mrp = 10  # sales are charged at batch MRP
    db.commit()
    sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 10, "rate": "10.00"}],
        discount="20.00", voucher="5.00", payment_mode="UPI",
    )
    sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 5, "rate": "10.00"}],
        payment_mode="CASH",
    )
    db.commit()
    start, end = reports_service.period_range("all")
    summary = reports_service.sales_summary(db, start, end)
    assert summary["count"] == 2
    assert summary["discount"] == 20
    assert summary["voucher"] == 5
    modes = {m["mode"]: m for m in summary["by_payment_mode"]}
    assert "UPI" in modes and "CASH" in modes
    assert modes["CASH"]["total"] == 50


def test_sales_by_category_and_brand(db):
    item, batch = seed_item(db, "Baby Wipes", category="BABY", manufacturer="Himalaya")
    sales_service.create_sale(db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 3, "rate": "50.00"}])
    db.commit()
    start, end = reports_service.period_range("all")
    cats = {c["category"]: c for c in reports_service.sales_by_category(db, start, end)}
    assert "BABY" in cats
    brands = {b["brand"]: b for b in reports_service.sales_by_brand(db, start, end)}
    assert "Himalaya" in brands


def test_purchase_agency_and_returns(db):
    from app.models import Purchase, PurchaseItem, Supplier

    supplier = Supplier(name="MedPlus Distributors")
    db.add(supplier)
    db.flush()
    item, batch = seed_item(db, "ReturnMed")
    purchase = Purchase(supplier_id=supplier.id, invoice_no="P-1", total="100.00")
    db.add(purchase)
    db.flush()
    db.add(PurchaseItem(purchase_id=purchase.id, item_id=item.id, product_name=item.name, quantity=10, rate="10.00", line_total="100.00"))
    db.commit()

    start, end = reports_service.period_range("all")
    agencies = reports_service.purchases_by_agency(db, start, end)
    assert any(a["agency"] == "MedPlus Distributors" for a in agencies)

    purchase_service.create_return(
        db, item_id=item.id, batch_id=batch.id, quantity=4, value="40.00",
        purchase_id=purchase.id, supplier_id=supplier.id, reason="damaged in transit",
    )
    db.commit()
    returns = reports_service.returns_summary(db, start, end)
    assert returns["count"] == 1
    assert returns["total"] == 40
    assert db.get(Batch, batch.id).quantity == 96


def test_adjustments_summary_loose_and_damage(db):
    item, batch = seed_item(db, "AdjustMed", qty=10)
    inv.record_adjustment(db, batch=batch, quantity=2, category="LOOSE", reason="torn strip")
    inv.record_adjustment(db, batch=batch, quantity=1, category="DAMAGE", reason="crushed")
    db.commit()
    start, end = reports_service.period_range("all")
    summary = reports_service.adjustments_summary(db, start, end)
    assert summary["LOOSE"]["quantity"] == 2
    assert summary["DAMAGE"]["quantity"] == 1
    assert db.get(Batch, batch.id).quantity == 7
