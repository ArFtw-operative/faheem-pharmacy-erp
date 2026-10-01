"""Phase 3 inventory core."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Batch, Item
from app.services import inventory_service as inv
from tests.conftest import login


def make_item(db, name="Paracetamol 500mg Tablet", **kw):
    item = inv.create_item(db, name=name, **kw)
    db.commit()
    return item


def test_create_item_generates_article_id(db):
    item = make_item(db)
    assert item.article_id == "PAR0001"
    assert item.is_active is True
    item2 = make_item(db, name="Amoxicillin 250mg Capsule")
    assert item2.article_id == "AMO0001"
    # second item sharing the same 3-char prefix gets the next serial
    item3 = make_item(db, name="Paracetamol 650mg Tablet")
    assert item3.article_id == "PAR0002"


def test_duplicate_article_id_is_rejected(db):
    inv.create_item(db, name="A", article_id="ART000100")
    db.commit()
    with pytest.raises(inv.InventoryError, match="already exists"):
        inv.create_item(db, name="B", article_id="ART000100")


def test_batch_stock_increment_and_insufficient_decrement(db):
    item = make_item(db)
    batch = inv.add_or_update_batch(
        db, item, batch_no="B1", expiry_date=date(2027, 12, 31), quantity=10,
        purchase_rate="5.00", selling_rate="8.00", mrp="10.00",
    )
    db.commit()
    assert batch.quantity == 10

    inv.add_or_update_batch(db, item, batch_no="B1", expiry_date=date(2027, 12, 31), quantity=5)
    db.commit()
    assert db.get(Batch, batch.id).quantity == 15

    with pytest.raises(inv.InventoryError, match="Insufficient stock"):
        inv.decrement_stock(db, batch, 100)
    db.rollback()


def test_fts_search_finds_item(db):
    make_item(db, name="Azithromycin 500mg Tablet", manufacturer="Cipla")
    make_item(db, name="Cetirizine 10mg Tablet", manufacturer="Dr Reddy")
    results, total = inv.search_items(db, q="azithro")
    assert total == 1
    assert results[0].name.startswith("Azithromycin")


def test_inventory_export_csv_and_xlsx(db):
    item = make_item(db, name="Pantoprazole 40mg")
    inv.add_or_update_batch(
        db, item, batch_no="PX1", expiry_date=date(2026, 6, 30), quantity=7,
        purchase_rate="3.50", selling_rate="6.00", mrp="7.00",
    )
    db.commit()
    items = inv.all_items_for_export(db)
    csv_bytes = inv.export_csv(db, items)
    assert b"article_id" in csv_bytes
    assert b"PX1" in csv_bytes
    xlsx_bytes = inv.export_xlsx(db, items)
    assert xlsx_bytes[:2] == b"PK"


def test_expiry_filter(db):
    soon = make_item(db, name="ExpiringSoon 5mg")
    inv.add_or_update_batch(db, soon, batch_no="E1", expiry_date=date.today() + timedelta(days=10), quantity=3)
    far = make_item(db, name="LongLife 5mg")
    inv.add_or_update_batch(db, far, batch_no="E2", expiry_date=date.today() + timedelta(days=900), quantity=3)
    db.commit()
    rows, total = inv.search_items(db, expiry_filter="expiring", threshold_days=90)
    names = [r.name for r in rows]
    assert "ExpiringSoon 5mg" in names
    assert "LongLife 5mg" not in names
