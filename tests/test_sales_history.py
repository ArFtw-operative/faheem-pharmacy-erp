"""Sales module API: register, detail, void, returns. Manual bills are never in it."""
from __future__ import annotations

from app.services import inventory_service as inv
from app.services import manual_bill_service, sales_service
from tests.conftest import login


def _bills(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    inv.add_or_update_batch(db, item, batch_no="DB1", quantity=4, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    stock = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 10}])
    manual = manual_bill_service.create(db, lines=[{"name": "Crepe bandage", "quantity": 2, "rate": "85"}])
    db.commit()
    return item, stock, manual


def test_register_search_filters_and_detail(client, db):
    item, stock, manual = _bills(db)
    login(client)
    d = client.get("/api/erp/sales").json()
    assert d["total"] == 1 and {s["type"] for s in d["sales"]} == {"INVENTORY"}          # the manual bill is not a sale
    assert [s["invoice_no"] for s in client.get("/api/erp/sales", params={"q": "dolo"}).json()["sales"]] == [stock.invoice_no]
    assert [s["invoice_no"] for s in client.get("/api/erp/sales", params={"q": "DB1"}).json()["sales"]] == [stock.invoice_no]
    assert client.get("/api/erp/sales", params={"q": "crepe"}).json()["sales"] == []
    assert client.get("/api/erp/sales", params={"q": manual.invoice_no}).json()["sales"] == []
    det = client.get(f"/api/erp/sales/{stock.id}").json()
    assert det["lines"][0]["batch"] == "DB1" and det["lines"][0]["qty"] == 10 and det["locked"] == ""
    assert client.get("/api/erp/sales", params={"start": "2026-09-10", "end": "2026-09-01"}).status_code == 400


def test_void_and_return_through_the_module(client, db):
    item, stock, manual = _bills(db)
    login(client)
    assert client.post(f"/api/erp/sales/{stock.id}/void", json={}).status_code == 400      # reason required
    r = client.post(f"/api/erp/sales/{stock.id}/void", json={"reason": "wrong customer"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    other = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 5}], payment_mode="UPI")
    db.commit()
    info = client.get(f"/api/sales/{other.id}/refundable").json()
    r = client.post(f"/api/sales/{other.id}/refund", json={"lines": [{"sale_item_id": info["lines"][0]["sale_item_id"], "quantity": 1}],
                                                          "reason_code": "CUSTOMER_RETURN", "refund_method": "UPI"})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/erp/sales/{other.id}").json()
    assert det["lines"][0]["returned"] == 1 and det["returns"][0]["refund_method"] == "UPI"
    assert client.get("/api/erp/sales").json()["sales"][0]["returned"] in (True, False)
