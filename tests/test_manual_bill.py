"""Manual bills: typed lines, own number series, no stock ledger, cost unknown."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import InventoryMovement, Sale
from app.services import financials, refund_service, sales_service
from app.services import inventory_service as inv
from tests.conftest import login

LINES = [{"name": "Crepe bandage 10cm", "quantity": 2, "rate": "85"},
         {"name": "Hot water bag", "quantity": 1, "rate": "240", "discount_pct": 10}]


def test_manual_bill_has_its_own_series_and_never_touches_stock(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S")
    inv.add_or_update_batch(db, item, batch_no="B1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    before = db.query(InventoryMovement).count()
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL", payment_mode="CASH", cash_received="500")
    db.commit()
    assert sale.invoice_no.startswith("MB-") and sale.invoice_type == "MANUAL"
    assert db.query(InventoryMovement).count() == before
    assert [l.item_id for l in sale.items] == [None, None]
    assert sale.subtotal == Decimal("386.00") and sale.total == Decimal("386.00")
    assert sale.change_amount == Decimal("114.00")
    stock = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}])
    assert stock.invoice_no.startswith("INV-")          # stock bills keep their own series


def test_manual_bill_rules(db):
    with pytest.raises(sales_service.SaleError, match="maximum allowed"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": 1, "rate": "100", "discount_pct": 50}], invoice_type="MANUAL")
    with pytest.raises(sales_service.SaleError, match="whole number"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": "1.5", "rate": "10"}], invoice_type="MANUAL")
    with pytest.raises(sales_service.SaleError, match="rate"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": 1, "rate": "0"}], invoice_type="MANUAL")
    item = inv.create_item(db, name="PAN 40", pack_size="15S")
    with pytest.raises(sales_service.SaleError, match="cannot take stock items"):
        sales_service.create_sale(db, lines=[{"item_id": item.id, "name": "PAN 40", "quantity": 1, "rate": "5"}], invoice_type="MANUAL")


def test_manual_bill_cost_is_unknown_not_zero(db):
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL")
    db.commit()
    assert all(financials.effective_cost(l) is None for l in sale.items)


def test_manual_bill_return_does_not_touch_inventory(db):
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL")
    db.commit()
    line = sale.items[0]
    ret = refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 1}], reason_code="DAMAGED")
    db.commit()
    assert ret.total_refund > 0 and db.query(InventoryMovement).count() == 0


def test_api_manual_bill(client, db):
    login(client)
    r = client.post("/api/sales", json={"invoice_type": "MANUAL", "lines": LINES, "payment_mode": "SPLIT",
                                        "payments": [{"mode": "CASH", "amount": 186}, {"mode": "UPI", "amount": 200}]})
    assert r.status_code == 200, r.text
    sale = db.get(Sale, r.json()["sale_id"])
    assert sale.invoice_type == "MANUAL" and len(sale.payments) == 2
    assert client.get(f"/sales/{sale.id}/invoice").status_code == 200
