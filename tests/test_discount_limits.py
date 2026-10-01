"""Discount policy: per-item and per-bill discounts never exceed the store limit (default 20%)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Sale
from app.services import inventory_service as inv
from app.services import sales_service, settings_service
from tests.conftest import login


def product(db, name="DOLO 650 TAB", mrp="100.00", strips=10):
    item = inv.create_item(db, name=name, pack_size="10S")  # 10 tablets per strip, ₹10 a tablet
    inv.add_or_update_batch(db, item, batch_no="B1", expiry_date=date(2099, 1, 1), quantity=strips, unit="PACK",
                            movement_type="OPENING_STOCK", mrp=mrp)
    db.commit()
    return item


def sell(db, lines, **kw):
    sale = sales_service.create_sale(db, lines=lines, payment_mode="UPI", **kw)
    db.commit()
    return sale


def test_item_discount_up_to_the_limit(db):
    item = product(db)
    sale = sell(db, [{"item_id": item.id, "quantity": 10, "discount_pct": 20}])
    assert (sale.items[0].discount, sale.items[0].line_total, sale.total) == (Decimal("20.00"), Decimal("80.00"), Decimal("80.00"))


def test_item_discount_above_the_limit_is_refused(db):
    item = product(db)
    with pytest.raises(sales_service.SaleError, match="maximum allowed is 20%"):
        sell(db, [{"item_id": item.id, "quantity": 10, "discount_pct": "20.5"}])
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="more than 20%"):
        sell(db, [{"item_id": item.id, "quantity": 10, "discount": "25"}])  # ₹25 on ₹100
    db.rollback()
    assert db.query(Sale).count() == 0 and db.get(type(item), item.id).total_stock == 100


def test_bill_discount_is_capped_too(db):
    item = product(db)
    sale = sell(db, [{"item_id": item.id, "quantity": 10}], discount="20")
    assert sale.total == Decimal("80.00")
    with pytest.raises(sales_service.SaleError, match="Bill discount"):
        sell(db, [{"item_id": item.id, "quantity": 10}], discount="20.01")


def test_item_and_bill_discounts_cannot_stack_past_the_limit(db):
    item = product(db)
    sale = sell(db, [{"item_id": item.id, "quantity": 10, "discount_pct": 10}], discount="10")  # 10 + 10 = 20%
    assert sale.total == Decimal("80.00")
    with pytest.raises(sales_service.SaleError, match="Total discount"):
        sell(db, [{"item_id": item.id, "quantity": 10, "discount_pct": 20}], discount="10")  # 30% in all


def test_split_batch_line_discount_follows_each_batch(db):
    item = product(db, strips=1)
    inv.add_or_update_batch(db, item, batch_no="B2", expiry_date=date(2099, 6, 1), quantity=1, unit="PACK",
                            movement_type="OPENING_STOCK", mrp="110.00")
    db.commit()
    sale = sell(db, [{"item_id": item.id, "quantity": 15, "discount_pct": 20}])  # 10 @ ₹10 + 5 @ ₹11
    assert sum(r.discount for r in sale.items) == Decimal("31.00")
    assert sale.subtotal == Decimal("124.00")


def test_limit_is_a_setting(db):
    item = product(db)
    settings_service.set_setting(db, "max_discount_pct", "10")
    db.commit()
    with pytest.raises(sales_service.SaleError, match="maximum allowed is 10%"):
        sell(db, [{"item_id": item.id, "quantity": 10, "discount_pct": 15}])


def test_bill_discount_as_percent_0_to_limit(db):
    item = product(db)
    sale = sell(db, [{"item_id": item.id, "quantity": 10}], discount_pct="20")
    assert (sale.discount, sale.total) == (Decimal("20.00"), Decimal("80.00"))
    with pytest.raises(sales_service.SaleError, match="Bill discount is 21% — the maximum allowed is 20%"):
        sell(db, [{"item_id": item.id, "quantity": 10}], discount_pct="21")
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="cannot be negative"):
        sell(db, [{"item_id": item.id, "quantity": 10}], discount_pct="-1")


def test_pos_api_bill_discount_percent(client, db):
    item = product(db)
    login(client)
    ok = client.post("/api/sales", json={"lines": [{"item_id": item.id, "quantity": 10, "discount_pct": 10}],
                                         "discount_pct": 5, "payment_mode": "CASH", "cash_received": 100})
    assert ok.status_code == 200 and ok.json()["total"] == "86.00"  # 100 − 10% = 90, − 5% = 85.50 → 86
    bad = client.post("/api/sales", json={"lines": [{"item_id": item.id, "quantity": 10}],
                                          "discount_pct": 25, "payment_mode": "CASH", "cash_received": 100})
    assert bad.status_code == 400 and "maximum allowed is 20%" in bad.json()["detail"]
