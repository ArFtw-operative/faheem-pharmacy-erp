"""ERP workspace APIs: shell, inventory grid/detail, adjustments, product master, POS search, customers."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Customer, InventoryMovement
from app.services import inventory_service as inv
from app.services import stock_ledger
from tests.conftest import login


def dolo(db, name="DOLO 650MG TAB 15'S"):
    item = inv.create_item(db, name=name, pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, loose_sale=True, dosage_form="TABLET", reorder_level=30)
    inv.add_or_update_batch(db, item, batch_no="DOBS4401", expiry_date=date(2099, 2, 1), quantity=44,
                            unit="PACK", movement_type="OPENING_STOCK", mrp="32.10", purchase_rate="20")
    db.commit()
    return item


def test_shell_renders_with_boot_data(client, db):
    login(client)
    page = client.get("/app/pos")
    assert page.status_code == 200
    assert "ERP_BOOT" in page.text and '"pos"' in page.text


def test_inventory_grid_shows_units_and_strip_equivalent(client, db):
    item = dolo(db)
    syrup = inv.create_item(db, name="CALCIJOINT SYRUP 200ML", base_unit="BOTTLE", pack_unit="BOTTLE")
    db.commit()
    login(client)
    data = client.get("/api/erp/inventory").json()
    rows = {r["name"]: r for r in data["rows"]}
    d = rows[item.name]
    assert (d["stock"], d["equivalent"], d["upp"], d["base_unit"], d["loose"], d["status"]) == (
        660, "44 strips", 15, "TABLET", True, "OK")
    assert rows[syrup.name]["status"] == "OUT"
    assert client.get("/api/erp/inventory", params={"stock": "out"}).json()["total"] == 1
    assert client.get("/api/erp/inventory", params={"loose": "yes"}).json()["rows"][0]["id"] == item.id
    assert client.get("/api/erp/inventory", params={"q": "dolo"}).json()["total"] == 1


def test_inventory_detail_lists_batches_with_pack_and_unit_mrp(client, db):
    item = dolo(db)
    login(client)
    d = client.get(f"/api/erp/inventory/{item.id}").json()
    b = d["batches"][0]
    assert (b["pack_mrp"], b["unit_mrp"], b["stock"], b["equivalent"], b["status"]) == (
        "32.10", "2.14", 660, "44 strips", "Active")
    assert d["packaging_locked"] is True
    assert Decimal(str(b["purchase_rate"])) == Decimal("20")
    assert b["rate_source"] == "Recorded batch rate"
    assert b["cost_status"] == "COST_RESOLVED"
    assert "unit_cost" not in str(d)


def test_adjustments_post_to_the_ledger_and_need_reasons(client, db):
    item = dolo(db)
    batch_id = item.batches[0].id
    login(client)
    url = f"/api/erp/inventory/{item.id}/adjust"
    assert client.post(url, json={"direction": "OUT", "quantity": 3, "batch_id": batch_id}).status_code == 400
    d = client.post(url, json={"direction": "OUT", "quantity": 3, "batch_id": batch_id, "reason": "count",
                               "category": "COUNT"}).json()
    assert d["stock"] == 657
    d = client.post(url, json={"direction": "IN", "quantity": "1s", "batch_id": batch_id, "reason": "found"}).json()
    assert d["stock"] == 672
    d = client.post(url, json={"direction": "IN", "quantity": "2s", "batch_no": "NEW1", "expiry": "06/2099",
                               "mrp": "33.00", "reason": "opening"}).json()
    assert d["stock"] == 702 and len(d["batches"]) == 2
    kinds = [m.movement_type for m in db.query(InventoryMovement).order_by(InventoryMovement.id)]
    assert kinds == ["OPENING_STOCK", "ADJUSTMENT_OUT", "ADJUSTMENT_IN", "ADJUSTMENT_IN"]
    assert stock_ledger.reconcile(db) == []


def test_product_create_and_guarded_packaging_edit(client, db):
    login(client)
    d = client.post("/api/erp/inventory", json={
        "name": "PANTOCID DSR CAP 10'S", "dosage_form": "CAPSULE", "base_unit": "CAPSULE", "pack_unit": "STRIP",
        "units_per_pack": 10, "loose_sale": "yes", "rack": "A2", "reorder_level": 100}).json()
    assert (d["upp"], d["loose"], d["rack"], d["reorder"]) == (10, True, "A2", 100)
    ok = client.put(f"/api/erp/inventory/{d['id']}", json={"units_per_pack": 15})
    assert ok.status_code == 200 and ok.json()["upp"] == 15  # no stock yet: allowed
    client.post(f"/api/erp/inventory/{d['id']}/adjust", json={"direction": "IN", "quantity": 5, "batch_no": "P1",
                                                              "mrp": "48", "reason": "opening"})
    bad = client.put(f"/api/erp/inventory/{d['id']}", json={"units_per_pack": 10})
    assert bad.status_code == 400 and "stock to zero" in bad.json()["detail"]


def test_pos_search_is_fefo_and_hides_expired_and_cost(client, db):
    item = dolo(db)
    inv.add_or_update_batch(db, item, batch_no="OLD", expiry_date=date(2020, 1, 1), quantity=1, unit="PACK",
                            movement_type="OPENING_STOCK", mrp="30")
    inv.add_or_update_batch(db, item, batch_no="SOON", expiry_date=date(2098, 1, 1), quantity=1, unit="PACK",
                            movement_type="OPENING_STOCK", mrp="31.50")
    db.commit()
    login(client)
    res = client.get("/api/erp/pos/search", params={"q": "dol"}).json()["items"][0]
    assert [b["batch_no"] for b in res["batches"]] == ["SOON", "DOBS4401"]
    assert res["stock"] == 675 and res["batches"][0]["unit_mrp"] == "2.10"
    assert "cost" not in str(res) and "purchase_rate" not in str(res)
    by_code = client.get("/api/erp/pos/search", params={"q": item.article_id}).json()["items"]
    assert len(by_code) == 1


def test_customer_lookup_by_mobile_or_name(client, db):
    db.add_all([Customer(customer_id="C-1", name="Ravi Kumar", mobile="9876543210"),
                Customer(customer_id="C-2", name="Asha", mobile="9123456780")])
    db.commit()
    login(client)
    assert [c["name"] for c in client.get("/api/erp/customers", params={"q": "#98765"}).json()["customers"]] == ["Ravi Kumar"]
    assert [c["name"] for c in client.get("/api/erp/customers", params={"q": "asha"}).json()["customers"]] == ["Asha"]
    assert client.get("/api/erp/customers", params={"q": "#zzz"}).json()["customers"] == []


def test_inventory_filters_accept_blank_and_category_supplier(client, db):
    from app.models import Supplier

    sup = Supplier(name="Micro Distributors")
    db.add(sup)
    db.flush()
    item = inv.create_item(db, name="Filter Tab", category="GENERIC")
    inv.add_or_update_batch(db, item, batch_no="F1", expiry_date=date(2099, 1, 1), quantity=5, unit="PACK",
                            movement_type="PURCHASE_RECEIPT", mrp="10", supplier_id=sup.id)
    inv.create_item(db, name="Other Tab", category="PHARMA")
    db.commit()
    login(client)
    blank = client.get("/api/erp/inventory", params={"category": "", "supplier": ""})
    assert blank.status_code == 200 and blank.json()["total"] == 2
    assert [r["name"] for r in client.get("/api/erp/inventory", params={"category": "GENERIC"}).json()["rows"]] == ["Filter Tab"]
    rows = client.get("/api/erp/inventory", params={"supplier": str(sup.id)}).json()["rows"]
    assert [r["name"] for r in rows] == ["Filter Tab"] and rows[0]["category_name"] == "Generic"
