"""Inventory movement and customer reports from finalized transactions."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.services import (business_time, customer_service, followup_service, inventory_service as inv, refund_service,
                          report_generator, sales_service)
from tests.conftest import login


def _item(db, name, upp=10, packs=3):
    it = inv.create_item(db, name=name, pack_size=f"{upp}S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=upp, dosage_form="TABLET")
    inv.add_or_update_batch(db, it, batch_no=name[:4] + "1", quantity=packs, unit="PACK", movement_type="OPENING_STOCK",
                            mrp="20", purchase_rate="12")
    return it


def _today(db):
    today = business_time.current_business_date(db).isoformat()
    return {"period": "custom", "from": today, "to": today}


def test_non_moving_and_top_moving(db):
    busy, returned, idle = _item(db, "Busy Tab"), _item(db, "Returned Tab"), _item(db, "Idle Tab")
    s1 = sales_service.create_sale(db, lines=[{"item_id": busy.id, "quantity": 12}])       # loose: 1 strip + 2 tablets
    sales_service.create_sale(db, lines=[{"item_id": busy.id, "quantity": 5}])
    s3 = sales_service.create_sale(db, lines=[{"item_id": returned.id, "quantity": 4}])
    db.commit()
    refund_service.create_return(db, s3, lines=[{"sale_item_id": s3.items[0].id, "quantity": 4}], refund_method="CASH")
    db.commit()
    nm = report_generator.generate(db, "non-moving", {**_today(db), "inactive_days": "30"})
    names = {r["item"]: r for r in nm["rows"]}
    assert "Busy Tab" not in names and {"Returned Tab", "Idle Tab"} <= set(names)          # a fully returned sale is not movement
    assert names["Idle Tab"]["last_sold"] == "Never" and "NON-MOVING ITEMS" in nm.get("title", "").upper()
    top = report_generator.generate(db, "top-moving", {**_today(db), "rank_by": "quantity", "top": "10"})
    assert [(r["item"], r["quantity"], r["bills"]) for r in top["rows"]] == [("Busy Tab", 17, 2)]   # base units, net of returns


def test_customer_reports(db):
    it = _item(db, "Dolo Tab", packs=20)
    ali = customer_service.create_customer(db, name="Mohammed Ali", mobile="9876543210")
    khan = customer_service.create_customer(db, name="Ahmed Khan", mobile="")
    a1 = sales_service.create_sale(db, lines=[{"item_id": it.id, "quantity": 10}], customer_id=ali.id)       # 20
    sales_service.create_sale(db, lines=[{"item_id": it.id, "quantity": 20}], customer_id=ali.id)            # 40
    old = sales_service.create_sale(db, lines=[{"item_id": it.id, "quantity": 10}], customer_id=khan.id)
    old.sale_date = datetime.utcnow() - timedelta(days=45)
    db.commit()
    refund_service.create_return(db, a1, lines=[{"sale_item_id": a1.items[0].id, "quantity": 5}], refund_method="CASH")
    followup_service.create(db, customer_id=ali.id, preset="7", reason="REFILL")
    db.commit()
    p = _today(db)
    freq = report_generator.generate(db, "frequent-customers", {**p, "min_bills": "2"})
    assert [(r["customer"], r["bills"], str(r["value"])) for r in freq["rows"]] == [("Mohammed Ali", 2, "50.00")]
    top = report_generator.generate(db, "top-customers", {**p, "top": "10"})
    assert top["rows"][0]["customer"] == "Mohammed Ali" and str(top["rows"][0]["value"]) == "50.00"
    rec = report_generator.generate(db, "customer-recovery", {**p, "inactive_days": "30"})
    assert [r["customer"] for r in rec["rows"]] == ["Ahmed Khan"] and rec["rows"][0]["days_away"] >= 44
    assert report_generator.generate(db, "customer-recovery", {**p, "inactive_days": "30", "exclude_no_mobile": "1"})["rows"] == []
    new = report_generator.generate(db, "new-customers", p)
    assert [r["customer"] for r in new["rows"]] == ["Mohammed Ali"]
    hist = report_generator.generate(db, "customer-history", {**p, "customer": "98765"})
    assert len(hist["rows"]) == 2 and str(hist["totals"]["retained"]) == "50.00"
    rets = report_generator.generate(db, "customer-returns", p)
    assert rets["rows"][0]["customer"] == "Mohammed Ali" and str(rets["rows"][0]["value"]) == "10.00"
    due = report_generator.generate(db, "followups-due", {**p, "followup_scope": "open"})
    assert due["rows"][0]["reason"] == "Medicine refill"
    perf = report_generator.generate(db, "followup-performance", p)
    assert perf["rows"][0]["created"] == 1 and perf["rows"][0]["open"] == 1


def test_reports_listed_and_rendered_as_erp_documents(client, db):
    _item(db, "Idle Tab")
    db.commit()
    login(client)
    catalog = client.get("/reports/api/catalog").json()
    ids = {r["id"] for r in (catalog if isinstance(catalog, list) else catalog.get("reports", []))}
    assert {"non-moving", "top-moving", "frequent-customers", "customer-recovery", "followups-due"} <= ids
    doc = client.post("/reports/api/generate", json={"report": "non-moving", "parameters": {"period": "today", "inactive_days": "90"}}).json()
    assert "NON-MOVING ITEMS" in doc["text"] and "Idle Tab" in doc["text"]
    assert client.post("/reports/api/generate", json={"report": "top-moving", "parameters": {"period": "today", "top": "7"}}).status_code == 400
