"""Inventory control: disable / enable / recycle bin / restore, packaging filter, POS status."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Item
from app.services import inventory_service as inv
from app.services import sales_service
from tests.conftest import login


def product(db, name="ALPHA 10MG TAB", stock=0, base="TABLET", upp=10):
    item = inv.create_item(db, name=name, pack_size=f"{upp}S", base_unit=base, pack_unit="STRIP", units_per_pack=upp,
                           dosage_form="TABLET" if base == "TABLET" else "")
    if stock:
        inv.add_or_update_batch(db, item, batch_no="B1", quantity=stock, unit="BASE", mrp=Decimal("50"))
    db.commit()
    return item


def status(client, item, action):
    return client.post(f"/api/erp/inventory/{item.id}/status", json={"action": action})


def test_disable_enable_and_recycle_bin(client, db):
    login(client)
    item = product(db)
    assert status(client, item, "disable").json() == {"id": item.id, "active": False, "deleted": False}
    rows = client.get("/api/erp/inventory?state=disabled").json()["rows"]
    assert [r["id"] for r in rows] == [item.id] and rows[0]["active"] is False
    assert status(client, item, "enable").json()["active"] is True
    assert status(client, item, "delete").json()["deleted"] is True
    assert item.id not in [r["id"] for r in client.get("/api/erp/inventory").json()["rows"]]
    bin_rows = client.get("/api/erp/inventory?state=deleted").json()["rows"]
    assert [r["id"] for r in bin_rows] == [item.id] and bin_rows[0]["deleted"]
    assert client.get(f"/api/erp/inventory/{item.id}").status_code == 200          # still viewable
    assert status(client, item, "restore").json()["deleted"] is False
    assert status(client, item, "bogus").status_code == 400


def test_a_product_with_stock_cannot_be_deleted_only_disabled(client, db):
    login(client)
    item = product(db, stock=20)
    r = status(client, item, "delete")
    assert r.status_code == 400 and "Disable it instead" in r.json()["detail"]
    assert status(client, item, "disable").status_code == 200


def test_packaging_filter_lists_products_by_setup(client, db):
    login(client)
    strip = product(db, name="STRIP PRODUCT TAB")
    plain = inv.create_item(db, name="PLAIN PACK THING", pack_size="", base_unit="PACK", pack_unit="PACK", units_per_pack=1)
    db.commit()
    ids = [r["id"] for r in client.get("/api/erp/inventory?packaging=generic").json()["rows"]]
    assert plain.id in ids and strip.id not in ids
    row = next(r for r in client.get("/api/erp/inventory").json()["rows"] if r["id"] == plain.id)
    assert row["generic_pack"] is True


def test_pos_shows_a_disabled_product_with_its_status_and_refuses_to_sell_it(client, db):
    login(client)
    item = product(db, name="BETA 5MG TAB", stock=20)
    status(client, item, "disable")
    found = client.get("/api/erp/pos/search?q=BETA").json()["items"]
    assert found[0]["id"] == item.id and found[0]["active"] is False and found[0]["status"] == "DISABLED"
    db.expire_all()
    with pytest.raises(sales_service.SaleError, match="disabled"):
        sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 10}])


def test_an_old_bill_with_a_since_disabled_product_can_still_be_edited(db):
    item = product(db, name="GAMMA 5MG TAB", stock=30)
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 10}])
    inv.deactivate_item(db, item)
    sales_service.amend_sale(db, sale, lines=[{"item_id": item.id, "quantity": 20}], reason="customer took more")
    assert sum(i.quantity for i in sale.items) == 20


def test_bulk_status_changes_many_and_skips_those_with_stock(client, db):
    login(client)
    a, b = product(db, name="BULK A TAB"), product(db, name="BULK B TAB")
    c = product(db, name="BULK C TAB", stock=10)
    out = client.post("/api/erp/inventory/bulk/status", json={"action": "delete", "ids": [a.id, b.id, c.id]}).json()
    assert sorted(out["done"]) == sorted([a.id, b.id]) and [r["id"] for r in out["refused"]] == [c.id]
    out = client.post("/api/erp/inventory/bulk/status", json={"action": "disable", "ids": [a.id, c.id]}).json()
    assert out["done"] == [c.id] and "recycle bin" in out["refused"][0]["reason"]
    assert client.post("/api/erp/inventory/bulk/status", json={"action": "disable", "ids": []}).status_code == 400


def test_bulk_category_changes_only_the_category(client, db):
    login(client)
    a, b = product(db, name="CAT A TAB"), product(db, name="CAT B TAB")
    out = client.post("/api/erp/inventory/bulk/category", json={"ids": [a.id, b.id], "category": "FMCG"}).json()
    assert out == {"changed": 2, "category": "FMCG"}
    db.expire_all()
    assert {db.get(Item, i).category for i in (a.id, b.id)} == {"FMCG"} and db.get(Item, a.id).units_per_pack == 10
    assert client.post("/api/erp/inventory/bulk/category", json={"ids": [a.id], "category": "NOPE"}).status_code == 400
