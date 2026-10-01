"""Park / resume / discard unfinished POS sales."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.models import ParkedSale, Sale
from app.services import inventory_service as inv, parking_service
from tests.conftest import login


def _item(db, rate="100", qty=20):
    item = inv.create_item(db, name="Park Test Med")
    batch = inv.add_or_update_batch(
        db, item, batch_no="P1", expiry_date=date(2027, 1, 1), quantity=qty,
        purchase_rate="5", selling_rate=rate, mrp=rate,
    )
    db.commit()
    return item, batch


def _bill_payload(item, batch, qty=2):
    return {
        "customer": {"id": 1, "name": "Qasim", "mobile": "7771717171"},
        "cart": [{"item_id": item.id, "name": item.name, "batch_id": batch.id, "batch_no": "P1",
                  "quantity": qty, "rate": 100, "mrp": 100, "discount": 0, "stock": 20}],
        "discount": {"mode": "AMOUNT", "value": 0, "reason": ""},
        "voucher": 0, "notes": "", "reference": "", "doctor": "",
    }


def test_park_creates_reference_without_invoice_or_ledger(client, db):
    item, batch = _item(db)
    login(client)
    resp = client.post("/api/pos/park", json={"payload": _bill_payload(item, batch), "reason_code": "CUSTOMER_WILL_RETURN", "note": "back soon"})
    assert resp.status_code == 200, resp.text
    ref = resp.json()["park_reference"]
    assert ref.startswith("PARK-")

    # No sale / invoice / counter ledger movement is created.
    assert db.query(Sale).count() == 0
    parked = db.scalar(select(ParkedSale).where(ParkedSale.park_reference == ref))
    assert parked is not None and parked.status == "PARKED"

    assert client.get("/api/pos/parked/count").json()["count"] == 1


def test_resume_claims_once_then_conflicts(client, db):
    item, batch = _item(db)
    login(client)
    pid = client.post("/api/pos/park", json={"payload": _bill_payload(item, batch), "reason_code": "PAYMENT_ISSUE"}).json()["id"]

    first = client.post(f"/api/pos/parked/{pid}/resume", json={})
    assert first.status_code == 200
    assert first.json()["parked"]["status"] == "CLAIMED"
    assert first.json()["parked"]["payload"]["cart"][0]["quantity"] == 2

    second = client.post(f"/api/pos/parked/{pid}/resume", json={})
    assert second.status_code == 409


def test_completing_resumed_sale_marks_parked_completed(client, db):
    item, batch = _item(db)
    login(client)
    parked = client.post("/api/pos/park", json={"payload": _bill_payload(item, batch), "reason_code": "CUSTOMER_WILL_RETURN"}).json()
    pid = parked["id"]
    client.post(f"/api/pos/parked/{pid}/resume", json={})

    sale = client.post("/api/sales", json={
        "payment_mode": "CASH", "cash_received": 200, "parked_id": pid,
        "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 2, "rate": "100"}],
    })
    assert sale.status_code == 200, sale.text
    row = db.get(ParkedSale, pid)
    assert row.status == "COMPLETED"
    assert row.completed_sale_id == sale.json()["sale_id"]
    assert client.get("/api/pos/parked/count").json()["count"] == 0


def test_discard_parked_sale(client, db):
    item, batch = _item(db)
    login(client)
    pid = client.post("/api/pos/park", json={"payload": _bill_payload(item, batch), "reason_code": "OTHER", "note": "asked to keep"}).json()["id"]
    resp = client.post(f"/api/pos/parked/{pid}/discard", json={"reason_code": "CUSTOMER_CANCELLED"})
    assert resp.status_code == 200
    assert db.get(ParkedSale, pid).status == "DISCARDED"
    assert client.get("/api/pos/parked/count").json()["count"] == 0


def test_park_reasons_and_search(client, db):
    from app.services import customer_service

    item, batch = _item(db)
    customer = customer_service.create_customer(db, name="Qasim", mobile="7771717171")
    db.commit()
    payload = _bill_payload(item, batch)
    payload["customer"] = {"id": customer.id, "name": "Qasim", "mobile": "7771717171"}
    login(client)
    client.post("/api/pos/park", json={"payload": payload, "reason_code": "WAITING_PRESCRIPTION"})
    found = client.get("/api/pos/parked?q=Qasim").json()["parked"]
    assert len(found) == 1
    assert parking_service.count_parked(db) == 1


def test_claimed_sale_is_visible_and_releasable(client, db):
    """A resumed-but-unfinished sale must be visible and recoverable, otherwise
    it silently blocks counter closing with no way to resolve it."""
    item, batch = _item(db)
    login(client)
    pid = client.post("/api/pos/park", json={"payload": _bill_payload(item, batch), "reason_code": "PAYMENT_ISSUE"}).json()["id"]
    client.post(f"/api/pos/parked/{pid}/resume", json={})

    listing = client.get("/api/pos/parked?scope=all").json()
    assert [p["status"] for p in listing["parked"]] == ["CLAIMED"]
    assert listing["count"] == 1

    released = client.post(f"/api/pos/parked/{pid}/release", json={})
    assert released.status_code == 200
    db.expire_all()
    assert db.get(ParkedSale, pid).status == "PARKED"

    # it can be resumed again after release
    again = client.post(f"/api/pos/parked/{pid}/resume", json={})
    assert again.status_code == 200
