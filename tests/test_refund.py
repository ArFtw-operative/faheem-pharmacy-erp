"""Invoice-linked return / refund workflow (no counter: refunds are documents)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Batch
from app.services import inventory_service as inv, refund_service, sales_service
from tests.conftest import login


def _sale(db, qty=10, mode="CASH"):
    item = inv.create_item(db, name="Crocin 500", pack_size="10S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=10, dosage_form="TABLET")
    batch = inv.add_or_update_batch(db, item, batch_no="CR1", quantity=3, unit="PACK", movement_type="OPENING_STOCK",
                                    mrp="20", purchase_rate="12")
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": qty}], payment_mode=mode)
    db.commit()
    return item, batch, sale


def test_partial_cash_refund_restocks_and_updates_status(db):
    item, batch, sale = _sale(db)
    assert batch.quantity == 20
    line = sale.items[0]
    ret = refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 4}],
                                       refund_method="CASH", reason_code="CUSTOMER_RETURN")
    db.commit()
    db.expire_all()
    assert ret.total_refund == Decimal("8.00") and db.get(Batch, batch.id).quantity == 24
    assert refund_service.refund_status(db, sale) != "NONE"


def test_cannot_over_return_or_double_return(db):
    item, batch, sale = _sale(db)
    line = sale.items[0]
    with pytest.raises(refund_service.RefundError):
        refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 11}], refund_method="CASH")
    refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 10}], refund_method="CASH")
    db.commit()
    with pytest.raises(refund_service.RefundError):
        refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 1}], refund_method="CASH")


def test_cash_refund_api_needs_no_counter(client, db):
    item, batch, sale = _sale(db)
    login(client)
    r = client.post(f"/api/sales/{sale.id}/refund", json={"lines": [{"sale_item_id": sale.items[0].id, "quantity": 2}],
                                                          "reason_code": "CUSTOMER_RETURN", "refund_method": "CASH"})
    assert r.status_code == 200, r.text
