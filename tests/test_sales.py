"""Phase 6 sales & billing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Sale, SaleItem
from app.services import inventory_service as inv
from app.services import sales_service


def seed_item(db, name="Paracetamol 500mg", qty=20, selling="8.00", mrp="10.00", expiry=date(2027, 12, 31)):
    item = inv.create_item(db, name=name)
    batch = inv.add_or_update_batch(
        db, item, batch_no="B1", expiry_date=expiry, quantity=qty,
        purchase_rate="5.00", selling_rate=selling, mrp=mrp,
    )
    db.commit()
    return item, batch


def test_round_off_rules():
    assert sales_service.round_off(Decimal("99.40"), "NEAREST_RUPEE") == (Decimal("99.00"), Decimal("-0.40"))
    assert sales_service.round_off(Decimal("99.60"), "NEAREST_RUPEE") == (Decimal("100.00"), Decimal("0.40"))
    assert sales_service.round_off(Decimal("99.50"), "NEAREST_HALF") == (Decimal("99.50"), Decimal("0.00"))
    assert sales_service.round_off(Decimal("99.49"), "NONE") == (Decimal("99.49"), Decimal("0.00"))


def test_create_sale_happy_path_decrements_stock(db):
    item, batch = seed_item(db, qty=20)
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 3, "rate": "8.00"}],
        payment_mode="UPI",
    )
    db.commit()
    assert sale.invoice_no.startswith("INV-")
    # the client "rate" is ignored: the counter charges the batch MRP
    assert sale.subtotal == Decimal("30.00")
    assert sale.total == Decimal("30.00")
    assert sale.payment_mode == "UPI"
    assert len(sale.items) == 1
    assert db.get(type(batch), batch.id).quantity == 17
    # per-employee attribution column exists (null for anonymous here)
    assert sale.user_id is None


def test_insufficient_stock_fails_and_rolls_back(db):
    item, batch = seed_item(db, qty=2)
    with pytest.raises(sales_service.SaleError, match="Insufficient stock"):
        sales_service.create_sale(
            db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 5, "rate": "8.00"}]
        )
    db.rollback()
    assert db.get(type(batch), batch.id).quantity == 2
    assert db.query(Sale).count() == 0


def test_discount_voucher_and_round_off_flow(db):
    item, batch = seed_item(db, qty=50, selling="8.00", mrp="9.94")
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 10, "rate": "9.94"}],
        discount="10.00",
        voucher="5.00",
        payment_mode="CARD",
    )
    db.commit()
    assert sale.subtotal == Decimal("99.40")
    assert sale.discount == Decimal("10.00")
    assert sale.voucher == Decimal("5.00")
    # 99.40 - 10 - 5 = 84.40 -> rounds to 84
    assert sale.total == Decimal("84.00")
    assert sale.round_off == Decimal("-0.40")


def test_void_sale_returns_stock(db):
    item, batch = seed_item(db, qty=10)
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 4, "rate": "8.00"}]
    )
    db.commit()
    assert db.get(type(batch), batch.id).quantity == 6
    sales_service.void_sale(db, sale, reason="customer changed mind")
    db.commit()
    assert db.get(type(batch), batch.id).quantity == 10
    assert sale.payment_status == "CANCELLED"


def test_fefo_batch_selection(db):
    item = inv.create_item(db, name="FEFO Test")
    early = inv.add_or_update_batch(db, item, batch_no="EARLY", expiry_date=date(2099, 1, 31), quantity=5, mrp="10")
    late = inv.add_or_update_batch(db, item, batch_no="LATE", expiry_date=date(2099, 6, 30), quantity=5, mrp="10")
    db.commit()
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "quantity": 2, "rate": "1.00"}]
    )
    db.commit()
    line = sale.items[0]
    assert line.batch_id == early.id
    assert db.get(type(early), early.id).quantity == 3
    assert db.get(type(late), late.id).quantity == 5
