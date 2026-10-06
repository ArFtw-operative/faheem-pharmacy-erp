"""Test-data reset on a database holding every kind of record (run on SQLite and PostgreSQL)."""
from __future__ import annotations

from datetime import date

from app.models import Customer, Item, StockAdjustment, WorkspaceSnapshot
from app.services import business_time, adjustment_service, customer_service, data_reset, followup_service
from app.services import inventory_service as inv
from app.services import parking_service, refund_service, sales_service


def _everything(db):
    item = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S")
    batch = inv.add_or_update_batch(db, item, batch_no="B1", quantity=5, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    cust = customer_service.create_customer(db, name="Asha", mobile="9876543210")
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}], customer_id=cust.id)
    db.flush()
    refund_service.create_return(db, sale, lines=[{"sale_item_id": sale.items[0].id, "quantity": 1}], reason_code="DAMAGED")
    adj = adjustment_service.create(db, item=item, direction="OUT", category="LOOSE", quantity=1, reason="broken", batch=batch)
    adjustment_service.reverse(db, adj, reason="mistake")
    from app.services import manual_bill_service

    manual_bill_service.create(db, lines=[{"item_id": item.id, "name": "DOLO", "quantity": 1, "rate": "30"}])
    parking_service.park_sale(db, payload={"cart": [{"item_id": item.id, "quantity": 1}]}, customer_id=cust.id)
    followup_service.create(db, customer_id=cust.id, due_date=business_time.current_business_date(db).isoformat(), source_sale_id=sale.id)
    from app.models import User
    db.add(WorkspaceSnapshot(user_id=db.query(User).first().id, terminal="c1", data="{}"))
    db.commit()
    return cust


def test_reset_keeps_customers_and_removes_everything_else(db):
    cust = _everything(db)
    assert db.query(StockAdjustment).count() == 2
    data_reset.wipe(db, keep_customers=True)
    db.commit()
    left = {k: v for k, v in data_reset.counts(db).items() if v}
    assert set(left) <= {"customers", "customer_followups", "audit_logs"}                # audit keeps the reset record
    assert db.query(Customer).count() == 1 and db.query(Item).count() == 0
    assert db.get(Customer, cust.id).name == "Asha"


def test_full_reset_removes_customers_too(db):
    _everything(db)
    data_reset.wipe(db)
    db.commit()
    assert not {k: v for k, v in data_reset.counts(db).items() if v and k != "audit_logs"}
