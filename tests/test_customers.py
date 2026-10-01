"""Customers module: directory, record, invoices, activity, follow-ups (one record, many views)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import CustomerFollowUp
from app.services import business_time, customer_service, followup_service, inventory_service as inv, refund_service, sales_service
from tests.conftest import login


def _setup(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    inv.add_or_update_batch(db, item, batch_no="DB1", quantity=10, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    ali = customer_service.create_customer(db, name="Mohammed Ali", mobile="9876543210", address="12 Main Rd", city="Yakutpura")
    khan = customer_service.create_customer(db, name="Ahmed Khan", mobile="9123456780")
    s1 = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}], customer_id=ali.id)       # 30
    s2 = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 30}], customer_id=ali.id)       # 60
    void = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}], customer_id=ali.id)
    sales_service.void_sale(db, void, reason="wrong")
    db.commit()
    refund_service.create_return(db, s2, lines=[{"sale_item_id": s2.items[0].id, "quantity": 5}], refund_method="CASH")
    db.commit()
    return item, ali, khan, s1, s2


def test_directory_figures_exclude_voids_and_subtract_refunds(db):
    item, ali, khan, s1, s2 = _setup(db)
    rows, total = customer_service.directory(db)
    assert total == 2
    a = next(r for r in rows if r["id"] == ali.id)
    assert (a["bills"], a["net_sales"]) == (2, "80.00")          # 30 + 60 − 10 refund; void excluded
    assert a["last_invoice"]["no"] == s2.invoice_no
    assert [r["name"] for r in customer_service.directory(db, "43210")[0]] == ["Mohammed Ali"]
    assert [r["name"] for r in customer_service.directory(db, "yakut")[0]] == ["Mohammed Ali"]
    rec = customer_service.record(db, ali)
    assert rec["stats"]["refunds"] == "10.00" and rec["stats"]["net_sales"] == "80.00"


def test_followup_one_record_many_views(db):
    item, ali, khan, s1, s2 = _setup(db)
    today = business_time.current_business_date(db)
    fu = followup_service.create(db, customer_id=ali.id, preset="7", reason="REFILL", note="monthly", source_sale_id=s2.id)
    followup_service.create(db, customer_id=khan.id, due_date=today.isoformat())
    db.commit()
    assert fu.due_date == today + timedelta(days=7)
    assert [f.id for f in followup_service.listing(db, view="upcoming")] == [fu.id]
    assert followup_service.counts(db) == {"today": 1, "overdue": 0, "upcoming": 1}
    assert any(e["kind"] == "FOLLOWUP" for e in customer_service.activity(db, ali))
    followup_service.reschedule(db, fu, preset="15", note="asked to call later")
    followup_service.complete(db, fu, note="refilled")
    db.commit()
    assert db.query(CustomerFollowUp).count() == 2                       # never duplicated
    assert fu.status == "COMPLETED" and fu.reschedule_count == 1
    assert [f.id for f in followup_service.listing(db, view="completed")] == [fu.id]
    assert any(e["kind"] == "FOLLOWUP_DONE" for e in customer_service.activity(db, ali))
    with pytest.raises(followup_service.FollowUpError):
        followup_service.complete(db, fu)
    with pytest.raises(followup_service.FollowUpError):
        followup_service.create(db, customer_id=ali.id, due_date=(today - timedelta(days=1)).isoformat())
    with pytest.raises(followup_service.FollowUpError):
        followup_service.create(db, customer_id=khan.id, preset="7", source_sale_id=s1.id)   # not Khan's bill


def test_customer_api(client, db):
    item, ali, khan, s1, s2 = _setup(db)
    login(client)
    d = client.get("/api/erp/customers/directory", params={"q": "ali"}).json()
    assert d["total"] == 1 and d["customers"][0]["net_sales"] == "80.00"
    rec = client.get(f"/api/erp/customers/{ali.id}").json()
    assert rec["stats"]["bills"] == 2
    inv_rows = client.get(f"/api/erp/customers/{ali.id}/invoices").json()["invoices"]
    assert {r["invoice_no"] for r in inv_rows} >= {s1.invoice_no, s2.invoice_no} and len(inv_rows) == 3   # void listed, marked
    r = client.put(f"/api/erp/customers/{ali.id}", json={"alternate_mobile": "9000000001", "reference": "Aadhaar ****1234"})
    assert r.status_code == 200 and r.json()["reference"] == "Aadhaar ****1234"
    assert client.put(f"/api/erp/customers/{ali.id}", json={"mobile": "9123456780"}).status_code == 400    # Khan's number
    assert "prefers evening" in client.post(f"/api/erp/customers/{ali.id}/notes", json={"text": "prefers evening"}).json()["notes"]
    fu = client.post("/api/erp/followups", json={"customer_id": ali.id, "preset": "30", "reason": "REPEAT", "source_sale_id": s1.id}).json()
    assert fu["followup"]["source_invoice"] == s1.invoice_no
    listing = client.get("/api/erp/followups", params={"view": "upcoming"}).json()
    assert listing["followups"][0]["customer"] == "Mohammed Ali" and listing["followups"][0]["last_visit"]
    done = client.post(f"/api/erp/followups/{fu['followup']['id']}/complete", json={"note": "called"}).json()
    assert done["followup"]["status"] == "COMPLETED"
    acts = client.get(f"/api/erp/customers/{ali.id}/activity").json()["activity"]
    assert {"INVOICE", "REFUND", "FOLLOWUP", "FOLLOWUP_DONE", "VOID"} <= {a["kind"] for a in acts}
