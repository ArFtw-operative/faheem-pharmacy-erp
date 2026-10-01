"""Stock adjustments: numbered immutable documents, both directions, reversals."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import InventoryMovement, StockAdjustment
from app.services import adjustment_service as adj
from app.services import inventory_service as inv
from app.services import reports_service
from tests.conftest import login


def _stock(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    batch = inv.add_or_update_batch(db, item, batch_no="DB1", expiry_date=date(2028, 5, 1), quantity=2, unit="PACK",
                                    movement_type="OPENING_STOCK", mrp="30", purchase_rate="15")
    db.commit()
    return item, batch


def test_decrease_types_numbering_and_ledger_link(db):
    item, batch = _stock(db)
    a = adj.create(db, item=item, direction="OUT", category="LOOSE", quantity=3, reason="strip torn", batch=batch)
    db.commit()
    assert a.reference_no == "ADJ-000001" and batch.quantity == 27 and a.value == Decimal("3.00")
    move = db.get(InventoryMovement, a.movement_id)
    assert (move.movement_type, move.quantity, move.reference_no) == ("ADJUSTMENT_OUT", -3, "ADJ-000001")
    with pytest.raises(adj.AdjustmentError, match="not a decrease"):
        adj.create(db, item=item, direction="OUT", category="SURPLUS", quantity=1, reason="x", batch=batch)
    with pytest.raises(adj.AdjustmentError, match="reason"):
        adj.create(db, item=item, direction="OUT", category="DAMAGE", quantity=1, reason=" ", batch=batch)
    with pytest.raises(adj.AdjustmentError):
        adj.create(db, item=item, direction="OUT", category="DAMAGE", quantity=99, reason="x", batch=batch)


def test_surplus_into_existing_and_new_batch(db):
    item, batch = _stock(db)
    adj.create(db, item=item, direction="IN", category="SURPLUS", quantity=5, reason="count found extra", batch=batch)
    b = adj.create(db, item=item, direction="IN", category="OTHER", quantity=15, reason="found carton",
                   new_batch={"batch_no": "DB9", "expiry_date": date(2029, 1, 1), "mrp": "30", "cost": "15"})
    db.commit()
    assert batch.quantity == 35 and b.batch.batch_no == "DB9" and b.batch.quantity == 15
    with pytest.raises(adj.AdjustmentError, match="MRP"):
        adj.create(db, item=item, direction="IN", category="OTHER", quantity=1, reason="x", new_batch={"batch_no": "Z"})


def test_reversal_restores_stock_and_drops_the_loss(db):
    item, batch = _stock(db)
    a = adj.create(db, item=item, direction="OUT", category="DAMAGE", quantity=4, reason="wet", batch=batch)
    db.commit()
    start, end = reports_service.period_range("month")
    assert reports_service.loss_summary(db, start, end)["total"] == Decimal("4.00")
    r = adj.reverse(db, a, reason="found dry, recounted")
    db.commit()
    assert batch.quantity == 30 and r.direction == "IN" and r.reversal_of_id == a.id and a.reversed_at is not None
    assert db.get(InventoryMovement, r.movement_id).reversal_of_id == a.movement_id
    assert reports_service.loss_summary(db, start, end)["total"] == Decimal("0.00")
    with pytest.raises(adj.AdjustmentError, match="already reversed"):
        adj.reverse(db, a, reason="again")
    with pytest.raises(adj.AdjustmentError, match="itself a reversal"):
        adj.reverse(db, r, reason="undo undo")
    assert db.query(StockAdjustment).count() == 2              # nothing deleted, nothing edited


def test_api_register_create_and_reverse(client, db):
    item, batch = _stock(db)
    login(client)
    r = client.post("/api/erp/adjustments", json={"item_id": item.id, "batch_id": batch.id, "direction": "OUT",
                                                  "category": "EXPIRED", "quantity": "1s", "reason": "expired strip"})
    assert r.status_code == 200, r.text
    assert r.json()["adjustment"]["quantity"] == 15 and r.json()["stock"] == 15
    reg = client.get("/api/erp/adjustments").json()
    assert reg["total"] == 1 and reg["out"] == 15 and reg["loss_value"] == "15.00"
    aid = reg["adjustments"][0]["id"]
    assert client.post(f"/api/erp/adjustments/{aid}/reverse", json={}).status_code == 400
    r = client.post(f"/api/erp/adjustments/{aid}/reverse", json={"reason": "wrong batch"})
    assert r.status_code == 200 and r.json()["adjustment"]["reversal_of"] == reg["adjustments"][0]["reference_no"]
    reg = client.get("/api/erp/adjustments").json()
    assert reg["total"] == 2 and reg["loss_value"] == "0.00" and reg["adjustments"][1]["reversed"]
