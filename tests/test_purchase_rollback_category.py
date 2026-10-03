"""Roll back posted purchase lines to draft; change only the category of selected lines."""
from __future__ import annotations

import pytest

from app.models import AuditLog, Batch, InventoryMovement, Item
from app.services import inventory_service as inv
from app.services import purchasing, sales_service, stock_ledger
from tests.conftest import login
from tests.test_purchasing import dolo, draft


def _stock(db, item):
    db.expire_all()
    return sum(b.quantity for b in db.query(Batch).filter_by(item_id=item.id))


def test_rollback_removes_stock_and_returns_lines_to_review(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    purchasing.post(db, p)
    assert _stock(db, item) == 180 and p.status == "POSTED"
    purchasing.rollback(db, p, reason="Posted the wrong invoice by mistake")
    assert _stock(db, item) == 0
    assert p.status == "DRAFT" and p.posted_at is None and p.reference_no
    line = p.items[0]
    assert line.status in ("READY", "CORRECTED") and line.batch_id is None
    assert line.corrections["_rolled_back"]["reason"].startswith("Posted the wrong")
    reversals = db.query(InventoryMovement).filter_by(movement_type="RECEIPT_REVERSAL").all()
    assert sum(m.quantity for m in reversals) == -180 and all(m.reversal_of_id for m in reversals)
    assert db.query(InventoryMovement).filter(InventoryMovement.movement_type.in_(("PURCHASE_RECEIPT", "FREE_STOCK"))).count() == 2
    assert not stock_ledger.reconcile(db)
    # it can be corrected and posted again under the same reference
    purchasing.correct(db, p, line, {"quantity": "8", "amount": "192.00"})
    ref = p.reference_no
    purchasing.post(db, p)
    assert _stock(db, item) == 150 and p.reference_no == ref and p.posted_at is not None
    assert not stock_ledger.reconcile(db)


def test_partial_rollback_keeps_other_lines_received(db):
    item = dolo(db)
    other = inv.create_item(db, name="PAN 40 TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                            units_per_pack=15, dosage_form="TABLET")
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,2,,24.00,33.60,48.00",
              ",PAN 40 TAB,15S,PB1,May-2028,1,,50,90,50")
    purchasing.post(db, p)
    first = next(l for l in p.items if l.item_id == item.id)
    purchasing.rollback(db, p, reason="Wrong batch number", line_ids=[first.id])
    assert p.status == "PARTIAL"
    assert _stock(db, item) == 0 and _stock(db, other) == 15
    assert first.status != "POSTED"


def test_rollback_refused_when_stock_was_already_sold(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00")
    purchasing.post(db, p)
    batch = db.query(Batch).filter_by(item_id=item.id).one()
    stock_ledger.post(db, batch, "SALE", 5, reason="sold")
    with pytest.raises(purchasing.PurchaseError) as exc:
        purchasing.rollback(db, p, reason="Wrong product entirely")
    assert exc.value.code == "STOCK_USED" and "purchase return" in str(exc.value)
    assert p.status == "POSTED" and p.items[0].status == "POSTED"
    assert _stock(db, item) == 10
    assert db.query(InventoryMovement).filter_by(movement_type="RECEIPT_REVERSAL").count() == 0


def test_rollback_needs_a_reason_and_a_posted_purchase(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00")
    with pytest.raises(purchasing.PurchaseError, match="posted"):
        purchasing.rollback(db, p, reason="not yet posted")
    purchasing.post(db, p)
    with pytest.raises(purchasing.PurchaseError, match="reason"):
        purchasing.rollback(db, p, reason="")


def test_rolled_back_new_product_keeps_the_created_product(db):
    p = draft(db, ",BRAND NEW SYRUP,100ML,S1,May-2028,2,,40,60,80")
    purchasing.correct(db, p, p.items[0], {"new_product": True})
    purchasing.post(db, p)
    created = p.items[0].item
    assert created is not None
    purchasing.rollback(db, p, reason="Quantity was wrong")
    assert p.items[0].item_id == created.id and not p.items[0].new_product
    assert db.query(Item).filter_by(name="BRAND NEW SYRUP").count() == 1


def test_rollback_reopens_lines_closed_as_not_received(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00", ",UNKNOWN,10ML57,X,May-2028,1,,1,2,1")
    ready = next(l for l in p.items if l.item_id == item.id)
    purchasing.post(db, p, line_ids=[ready.id])
    purchasing.close_remaining(db, p, reason="short supplied")
    assert p.status == "POSTED"
    purchasing.rollback(db, p, reason="Whole invoice entered wrongly")
    assert p.status == "DRAFT" and all(l.status not in ("POSTED", "CLOSED") for l in p.items)


def _otc(db):
    from app.services import category_service
    category_service.create(db, "OTC")


def test_change_category_on_selected_lines(db):
    _otc(db)
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00", ",NEW SOAP,75GM,S1,May-2028,1,,30,45,30")
    new = next(l for l in p.items if l.item_id is None)
    purchasing.correct(db, p, new, {"new_product": True})
    out = purchasing.set_category(db, p, [l.id for l in p.items], "FMCG")
    assert out["lines"] == 2 and out["products"] == 1
    assert item.category == "FMCG" and new.category == "FMCG"
    purchasing.post(db, p)
    assert new.item.category == "FMCG"
    # works on received lines too, without touching stock
    before = db.query(InventoryMovement).count()
    purchasing.set_category(db, p, [new.id], "OTC")
    assert new.item.category == "OTC" and db.query(InventoryMovement).count() == before


def test_change_category_rejects_unknown_category(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00")
    with pytest.raises(purchasing.PurchaseError, match="category"):
        purchasing.set_category(db, p, [p.items[0].id], "NOT A CATEGORY")


def test_rollback_and_category_api(client, db):
    login(client)
    _otc(db)
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,1,,24.00,33.60,24.00")
    db.commit()
    lid = p.items[0].id
    r = client.post(f"/api/erp/purchases/{p.id}/lines/category", json={"line_ids": [lid], "category": "OTC"})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["product_category"] == "OTC"
    assert client.post(f"/api/erp/purchases/{p.id}/post", json={}).status_code == 200
    r = client.post(f"/api/erp/purchases/{p.id}/rollback", json={"reason": "Entered against the wrong supplier"})
    assert r.status_code == 200, r.text
    assert r.json()["purchase"]["status"] == "DRAFT"
    db.expire_all()
    assert _stock(db, item) == 0
    assert db.query(AuditLog).filter(AuditLog.details.like("%returned to review%")).count() == 1


def test_receipt_movements_keep_purchase_retail_and_base_levels(db):
    from decimal import Decimal
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    purchasing.post(db, p)
    moves = {m.movement_type: m for m in db.query(InventoryMovement)}
    paid, free = moves["PURCHASE_RECEIPT"], moves["FREE_STOCK"]
    assert (paid.quantity, paid.purchase_quantity, paid.purchase_uom, paid.retail_quantity, paid.retail_uom, paid.base_uom) == (
        150, Decimal("10"), "STRIP", Decimal("10"), "STRIP", "TABLET")
    assert (free.quantity, free.purchase_quantity, free.retail_quantity) == (30, Decimal("2"), Decimal("2"))
    purchasing.rollback(db, p, reason="Entered twice by mistake")
    back = db.query(InventoryMovement).filter_by(movement_type="RECEIPT_REVERSAL").all()
    assert sorted(m.purchase_quantity for m in back) == [Decimal("-10"), Decimal("-2")]
    assert all(m.base_uom == "TABLET" for m in back)
