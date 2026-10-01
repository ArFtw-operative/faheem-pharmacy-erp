"""Editing a completed bill: same number, stock re-posted through the ledger."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import AuditLog, Batch, InventoryMovement
from app.services import inventory_service as inv, refund_service, sales_service, stock_ledger
from tests.conftest import login


def _stock(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    batch = inv.add_or_update_batch(db, item, batch_no="DB1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    db.commit()
    return item, batch


def test_edit_keeps_number_and_reposts_stock(db):
    item, batch = _stock(db)
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 10}])
    db.commit()
    no = sale.invoice_no
    assert batch.quantity == 20
    sales_service.amend_sale(db, sale, lines=[{"item_id": item.id, "quantity": 4}], reason="customer took fewer")
    db.commit()
    db.expire_all()
    assert sale.invoice_no == no and sale.items[0].quantity == 4 and sale.total == Decimal("8.00")
    assert db.get(Batch, batch.id).quantity == 26
    types = [m.movement_type for m in db.query(InventoryMovement).filter(InventoryMovement.item_id == item.id)]
    assert types.count("SALE") == 2 and "SALE_CANCEL" in types
    assert stock_ledger.reconcile(db) == []
    assert db.query(AuditLog).filter(AuditLog.details.like(f"Invoice {no} edited%")).count() == 1


def test_manual_bill_edit_never_touches_stock(db):
    sale = sales_service.create_sale(db, lines=[{"name": "Crepe bandage", "quantity": 1, "rate": "85"}], invoice_type="MANUAL")
    db.commit()
    sales_service.amend_sale(db, sale, lines=[{"name": "Crepe bandage", "quantity": 2, "rate": "85"}])
    db.commit()
    assert sale.total == Decimal("170.00") and db.query(InventoryMovement).count() == 0


def test_returned_or_voided_bills_cannot_be_edited(db):
    item, batch = _stock(db)
    a = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}])
    b = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}])
    db.commit()
    refund_service.create_return(db, a, lines=[{"sale_item_id": a.items[0].id, "quantity": 1}], refund_method="CASH")
    sales_service.void_sale(db, b, reason="wrong")
    db.commit()
    for sale in (a, b):
        with pytest.raises(sales_service.SaleError):
            sales_service.amend_sale(db, sale, lines=[{"item_id": item.id, "quantity": 1}])


def test_edit_api_round_trip(client, db):
    item, batch = _stock(db)
    login(client)
    d = client.post("/api/sales", json={"lines": [{"item_id": item.id, "quantity": 5}], "payment_mode": "CASH", "cash_received": "20"}).json()
    payload = client.get(f"/api/erp/sales/{d['sale_id']}/edit").json()
    line = payload["lines"][0]
    assert line["qty"] == 5 and line["batches"][0]["stock"] == 30        # the bill's own units count as available
    r = client.put(f"/api/sales/{d['sale_id']}", json={"lines": [{"item_id": item.id, "batch_id": line["batch_id"], "quantity": 7}],
                                                       "payment_mode": "CASH", "cash_received": "20"})
    assert r.status_code == 200, r.text
    assert r.json()["invoice_no"] == d["invoice_no"] and r.json()["total"] == "14.00"
    db.expire_all()
    assert db.get(Batch, batch.id).quantity == 23
