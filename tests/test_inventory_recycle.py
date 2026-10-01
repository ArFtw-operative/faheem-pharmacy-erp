"""Inventory enable/disable and recycle-bin (soft delete + restore)."""
from __future__ import annotations

from datetime import date

from app.models import Item
from app.seed import DEFAULT_ADMIN
from app.services import inventory_service as inv
from tests.conftest import login


def _item(db, name="Recycle Me"):
    item = inv.create_item(db, name=name, mrp="10")
    inv.add_or_update_batch(
        db, item, batch_no="R1", expiry_date=date(2027, 1, 1), quantity=5,
        purchase_rate="6", selling_rate="8", mrp="10",
    )
    db.commit()
    return item


def test_delete_moves_to_bin_and_restore(db):
    item = _item(db, name="Bin Flow Item")
    inv.delete_item(db, item)
    db.commit()

    assert db.get(Item, item.id).deleted_at is not None
    # hidden from the normal inventory
    items, _ = inv.search_items(db, q="Bin Flow Item")
    assert items == []
    # visible only in the recycle bin
    deleted, _ = inv.search_items(db, q="Bin Flow Item", deleted_only=True)
    assert [i.id for i in deleted] == [item.id]

    inv.restore_item(db, item)
    db.commit()
    assert db.get(Item, item.id).deleted_at is None
    items, _ = inv.search_items(db, q="Bin Flow Item")
    assert [i.id for i in items] == [item.id]


def test_deleted_items_excluded_from_valuation(db):
    item = _item(db, name="Valued Item")
    before = inv.inventory_valuation_total(db)
    inv.delete_item(db, item)
    db.commit()
    after = inv.inventory_valuation_total(db)
    assert after < before


def test_incoming_stock_does_not_restore_recycle_bin(db):
    item = _item(db, name="Bin Keep Item")
    inv.delete_item(db, item)
    db.commit()

    inv.add_or_update_batch(db, item, batch_no="R2", expiry_date=date(2027, 1, 1),
                            quantity=3, purchase_rate="6", selling_rate="8", mrp="10")
    db.commit()

    kept = db.get(Item, item.id)
    assert kept.deleted_at is not None  # still in the recycle bin
    items, _ = inv.search_items(db, q="Bin Keep Item")
    assert items == []  # and still hidden
