"""Profit & loss widget: MRP-based revenue, restocked returns, loose/damage."""
from __future__ import annotations

from datetime import date, timedelta

from app.services import inventory_service as inv, refund_service, reports_service, sales_service
from app.utils import utcnow
from tests.conftest import login


def _end():
    return utcnow() + timedelta(days=1)


def test_profit_loss_counts_loose_writeoff(db):
    item = inv.create_item(db, name="PL Widget Med", mrp="100")
    batch = inv.add_or_update_batch(
        db, item, batch_no="PW1", expiry_date=date(2027, 1, 1), quantity=50,
        purchase_rate="60", selling_rate="100", mrp="100",
    )
    db.commit()
    sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 10, "rate": "100"}],
        payment_mode="CASH",
    )
    db.commit()
    inv.record_adjustment(db, batch=batch, quantity=2, category="LOOSE", reason="breakage")
    db.commit()

    pl = reports_service.profit_loss_widget(db, None, _end())
    assert pl["sold_packs"] == 10
    assert round(pl["revenue"]) == 1000
    assert round(pl["cogs"]) == 600
    assert round(pl["write_off"]) == 120
    assert round(pl["profit"]) == 280
    assert pl["damaged_packs"] == 2


def test_profit_loss_returns_valued_at_mrp_and_restock(db):
    item = inv.create_item(db, name="PL Return Med", mrp="100")
    batch = inv.add_or_update_batch(
        db, item, batch_no="PR1", expiry_date=date(2027, 1, 1), quantity=50,
        purchase_rate="60", selling_rate="100", mrp="100",
    )
    db.commit()
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 10, "rate": "100"}],
        payment_mode="CASH",
    )
    db.commit()
    refund_service.create_return(
        db, sale, lines=[{"sale_item_id": sale.items[0].id, "quantity": 3}], disposition="RESTOCK",
    )
    db.commit()

    pl = reports_service.profit_loss_widget(db, None, _end())
    assert pl["returned_packs"] == 3
    assert round(pl["refunds"]) == 300  # valued at MRP
    assert round(pl["recovered"]) == 180  # 3 x 60 restocked
    assert round(pl["revenue"]) == 700  # (10 - 3) x 100
    assert round(pl["cogs"]) == 420  # (10 - 3) x 60
    assert round(pl["profit"]) == 280


def test_profit_loss_non_restock_return_is_writeoff(db):
    item = inv.create_item(db, name="PL Damage Med", mrp="100")
    batch = inv.add_or_update_batch(
        db, item, batch_no="PD1", expiry_date=date(2027, 1, 1), quantity=50,
        purchase_rate="60", selling_rate="100", mrp="100",
    )
    db.commit()
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 10, "rate": "100"}],
        payment_mode="CASH",
    )
    db.commit()
    refund_service.create_return(
        db, sale, lines=[{"sale_item_id": sale.items[0].id, "quantity": 2}], disposition="DAMAGE",
    )
    db.commit()

    pl = reports_service.profit_loss_widget(db, None, _end())
    assert pl["returned_packs"] == 2
    assert pl["restocked_packs"] == 0
    assert pl["damaged_packs"] == 2
    assert round(pl["write_off"]) == 120  # 2 x 60 not recovered
    # Net sold COGS is 8 x 60; the two damaged returns are a separate 120 loss.
    assert round(pl["revenue"]) == 800
    assert round(pl["cogs"]) == 480
    assert round(pl["profit"]) == 200
