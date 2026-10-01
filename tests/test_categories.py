"""Category master managed by the pharmacy, with SQL-enforced integrity."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, Category, Item
from app.services import category_service as cats
from app.services import inventory_service as inv
from tests.conftest import login


def test_defaults_include_general(db):
    assert cats.active_codes(db)[:9] == cats.DEFAULTS


def test_create_rename_reorder_deactivate(db):
    c = cats.create(db, "OTC medicines")
    assert (c.code, c.name) == ("OTC_MEDICINES", "OTC medicines")
    with pytest.raises(cats.CategoryError, match="already exists"):
        cats.create(db, "otc medicines")
    cats.update(db, "OTC_MEDICINES", name="Over the counter")
    assert db.get(Category, "OTC_MEDICINES").name == "Over the counter"
    cats.move(db, "OTC_MEDICINES", -100)
    assert cats.active_codes(db)[0] == "OTC_MEDICINES"
    cats.update(db, "OTC_MEDICINES", active=False)
    assert "OTC_MEDICINES" not in cats.active_codes(db)
    assert db.query(AuditLog).filter(AuditLog.entity_type == "category").count() >= 3


def test_merge_moves_products_and_removes_category(db):
    a = inv.create_item(db, name="Wipes", category="BABY")
    b = inv.create_item(db, name="Powder", category="BABY")
    db.commit()
    assert cats.merge(db, "BABY", "GENERAL") == 2
    db.commit()
    assert {db.get(Item, a.id).category, db.get(Item, b.id).category} == {"GENERAL"}
    assert db.get(Category, "BABY") is None


def test_delete_only_when_unused(db):
    inv.create_item(db, name="Juice", category="BEVERAGES")
    db.commit()
    with pytest.raises(cats.CategoryError, match="merge it"):
        cats.delete(db, "BEVERAGES")
    cats.delete(db, "AYURVEDIC")
    db.commit()
    assert db.get(Category, "AYURVEDIC") is None


def test_database_refuses_unknown_category_and_deleting_used_one(db):
    item = inv.create_item(db, name="Dolo", category="PHARMA")
    db.commit()
    with pytest.raises(IntegrityError, match="Unknown product category"):
        db.execute(text("UPDATE items SET category='NOPE' WHERE id=:i"), {"i": item.id})
    db.rollback()
    with pytest.raises(IntegrityError, match="in use"):
        db.execute(text("DELETE FROM categories WHERE code='PHARMA'"))
    db.rollback()


def test_category_api(client, db):
    inv.create_item(db, name="Soap", category="FMCG")
    db.commit()
    login(client)
    rows = {r["code"]: r for r in client.get("/api/erp/categories").json()["categories"]}
    assert rows["FMCG"]["products"] == 1 and rows["GENERAL"]["active"]
    assert client.post("/api/erp/categories", json={"name": "Personal Care"}).json()["code"] == "PERSONAL_CARE"
    assert client.put("/api/erp/categories/PERSONAL_CARE", json={"name": "Personal care"}).status_code == 200
    bad = client.delete("/api/erp/categories/FMCG")
    assert bad.status_code == 400 and "merge" in bad.json()["detail"]
    assert client.post("/api/erp/categories/FMCG/merge", json={"into": "PERSONAL_CARE"}).json()["moved"] == 1
    assert '"category_options"' in client.get("/app").text
