"""Inventory valuation and profit & loss from captured purchase rates."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services import inventory_service as inv, reports_service, sales_service


def _item(db, name="PnL Med", rate="100", purchase="50", qty=20):
    item = inv.create_item(db, name=name, mrp=rate)
    batch = inv.add_or_update_batch(
        db, item, batch_no="PL1", expiry_date=date(2027, 1, 1), quantity=qty,
        purchase_rate=purchase, selling_rate=rate, mrp=rate,
    )
    db.commit()
    return item, batch


def test_sale_line_captures_cost_rate(db):
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 2, "rate": "100"}],
        payment_mode="CASH",
    )
    db.commit()
    line = sale.items[0]
    assert line.cost_rate == Decimal("50.00")


def test_profit_summary_revenue_cogs_profit(db):
    item, batch = _item(db)
    sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 2, "rate": "100"}],
        payment_mode="CASH",
    )
    db.commit()
    start, end = reports_service.period_range("today")
    pl = reports_service.profit_summary(db, start, end)
    assert pl["revenue"] == Decimal("200.00")
    assert pl["cogs"] == Decimal("100.00")
    assert pl["profit"] == Decimal("100.00")
    assert pl["margin"] == 50.0


def test_inventory_valuation_cost_and_retail(db):
    item, batch = _item(db, qty=10, rate="100", purchase="60")
    items, _ = inv.search_items(db, q=f"{item.name}", limit=5)
    assert inv.inventory_valuation(db, items) == Decimal("600.00")
    assert inv.retail_valuation(db, items) == Decimal("1000.00")


def test_incoming_stock_reactivates_item(db):
    """Receiving stock must un-hide a previously deactivated item."""
    item, _ = _item(db, name="Reactivate Me")
    inv.deactivate_item(db, item)
    db.commit()
    assert db.get(type(item), item.id).is_active is False

    inv.add_or_update_batch(
        db, item, batch_no="RE1", expiry_date=date(2027, 1, 1), quantity=5,
        purchase_rate="10", selling_rate="10", mrp="12",
    )
    db.commit()
    assert db.get(type(item), item.id).is_active is True
