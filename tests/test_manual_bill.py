"""Manual bills: their own documents (manual_bills), own number series, never a sale, never stock."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import InventoryMovement, ManualBill, Sale, SalePayment
from app.services import counter_service, manual_bill_service, sales_service
from app.services import inventory_service as inv
from tests.conftest import login

LINES = [{"name": "Crepe bandage 10cm", "quantity": 2, "rate": "85"},
         {"name": "Hot water bag", "quantity": 1, "rate": "240", "discount_pct": 10}]


def test_manual_bill_is_its_own_document_and_never_touches_stock_or_sales(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S")
    inv.add_or_update_batch(db, item, batch_no="B1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    before = db.query(InventoryMovement).count()
    bill = manual_bill_service.create(db, lines=LINES, payment_mode="CASH", cash_received="500")
    db.commit()
    assert bill.invoice_no.startswith("MB-") and isinstance(bill, ManualBill)
    assert db.query(InventoryMovement).count() == before
    assert db.query(Sale).count() == 0 and db.query(SalePayment).count() == 0          # no sale, no payment row
    assert bill.subtotal == Decimal("386.00") and bill.total == Decimal("386.00") and bill.change_amount == Decimal("114.00")
    stock = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}])
    assert stock.invoice_no.startswith("INV-")          # stock bills keep their own series


def test_manual_bill_rules(db):
    with pytest.raises(sales_service.SaleError, match="maximum"):
        manual_bill_service.create(db, lines=[{"name": "X", "quantity": 1, "rate": "100", "discount_pct": 50}])
    with pytest.raises(sales_service.SaleError, match="whole number"):
        manual_bill_service.create(db, lines=[{"name": "X", "quantity": "1.5", "rate": "10"}])
    with pytest.raises(sales_service.SaleError, match="rate"):
        manual_bill_service.create(db, lines=[{"name": "X", "quantity": 1, "rate": "0"}])
    with pytest.raises(sales_service.SaleError, match="no Udhaar"):
        manual_bill_service.create(db, lines=[{"name": "X", "quantity": 1, "rate": "10"}], payment_mode="UDHAAR")
    item = inv.create_item(db, name="PAN 40", pack_size="15S")
    with pytest.raises(sales_service.SaleError, match="batch"):
        manual_bill_service.create(db, lines=[{"item_id": item.id, "batch_id": 1, "name": "PAN 40", "quantity": 1, "rate": "5"}])


def test_manual_bills_are_in_no_sales_report_counter_or_customer_figure(db):
    from app.services import report_generator as reports

    out_of_stock = inv.create_item(db, name="AZEE 500 TAB", pack_size="3S")
    dolo = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)
    inv.add_or_update_batch(db, dolo, batch_no="D1", quantity=1, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    db.commit()
    moves = db.query(InventoryMovement).count()
    bill = manual_bill_service.create(db, round_off_mode="NONE", lines=[
        {"item_id": out_of_stock.id, "name": "AZEE 500 TAB", "quantity": 2, "rate": "98.50", "discount_pct": 5},
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "quantity": 4, "rate": "30"},          # more than the 1 strip in stock
        {"name": "Crepe bandage 10cm", "quantity": 1, "rate": "85"}])
    db.commit()
    assert db.query(InventoryMovement).count() == moves
    assert [l.item_id for l in bill.items] == [out_of_stock.id, dolo.id, None]
    assert bill.items[0].pack_size == "3S" and bill.items[0].discount == Decimal("9.85")
    for rid in ("item-wise-sales", "bill-register", "sales-summary", "payments", "item-sales"):
        rep = reports.generate(db, rid, {"period": "this_month"}, user=None)
        assert bill.invoice_no not in str(rep["rows"]) and "Crepe" not in str(rep["rows"]), rid
        assert all(not v for k, v in rep["totals"].items()), (rid, rep["totals"])
    day = counter_service.compute(db, date.today())
    assert day["bills"] == 0 and day["sales_total"] == "0.00" and day["received"] == "0.00"


def test_api_manual_bill_create_list_print_edit_delete(client, db):
    login(client)
    r = client.post("/api/erp/manual-bills", json={"lines": LINES, "payment_mode": "SPLIT",
                                                  "payments": [{"mode": "CASH", "amount": 186}, {"mode": "UPI", "amount": 200}]})
    assert r.status_code == 200, r.text
    bid = r.json()["manual_bill_id"]
    assert client.get(f"/manual-bills/{bid}/invoice").status_code == 200
    assert client.get(f"/manual-bills/{bid}/invoice?size=ERP").status_code == 200
    assert client.get(f"/manual-bills/{bid}/pdf").status_code == 200
    assert client.get(f"/api/erp/manual-bills/{bid}/export.csv").status_code == 200
    listed = client.get("/api/erp/manual-bills").json()
    assert listed["total"] == 1 and listed["bills"][0]["payment"].startswith("Cash")
    assert client.get("/api/erp/sales").json()["total"] == 0
    # a POS screen from before the change posting a manual bill to /api/sales still makes a manual bill
    old = client.post("/api/sales", json={"invoice_type": "MANUAL", "lines": [{"name": "Gauze", "quantity": 1, "rate": "20"}]})
    assert old.status_code == 200 and old.json()["invoice_no"].startswith("MB-") and "manual_bill_id" in old.json()
    assert client.get("/api/erp/sales").json()["total"] == 0
    payload = client.get(f"/api/erp/manual-bills/{bid}/edit").json()
    assert all(l["manual"] for l in payload["lines"])
    r = client.put(f"/api/erp/manual-bills/{bid}", json={"payment_mode": "CASH", "lines": [{"name": "Crepe bandage 10cm", "quantity": 3, "rate": "85"}]})
    assert r.status_code == 200 and r.json()["total"] == "255.00"
    assert client.post(f"/api/erp/manual-bills/{bid}/delete", json={}).status_code == 400            # reason required
    d = client.post(f"/api/erp/manual-bills/{bid}/delete", json={"reason": "typed twice"}).json()
    assert d["deleted"] and d["status"] == "CANCELLED"
    assert client.get("/api/erp/manual-bills", params={"status": "ACTIVE"}).json()["total"] == 1
    assert db.query(InventoryMovement).count() == 0


def test_manual_lines_keep_batch_expiry_and_code(db):
    dolo = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S")
    db.commit()
    bill = manual_bill_service.create(db, round_off_mode="NONE", lines=[
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "quantity": 3, "rate": "30", "batch": " DB 44 ", "expiry": "05/28"},
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "code": "PAN-X1", "quantity": 1, "rate": "12"},
        {"name": "Crepe bandage", "code": " CB 10 ", "pack": "1 roll", "quantity": 1, "rate": "85"}])
    db.commit()
    assert (bill.items[0].batch_no, bill.items[0].expiry_date) == ("DB 44", date(2028, 5, 1))
    assert [l.item_code for l in bill.items] == [dolo.article_id, "PAN-X1", "CB 10"] and bill.items[2].pack_size == "1 roll"
    with pytest.raises(sales_service.SaleError, match="MM/YY"):
        manual_bill_service.create(db, lines=[{"name": "X", "quantity": 1, "rate": "5", "expiry": "soon"}])
