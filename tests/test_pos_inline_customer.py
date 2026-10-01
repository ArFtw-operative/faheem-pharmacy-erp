"""POS: customers typed directly on the bill (name + mobile), no pre-created record."""
from __future__ import annotations

from datetime import date

from app.models import Customer, Sale
from app.services import customer_service, inventory_service as inv
from tests.conftest import login


def _stock(db):
    item = inv.create_item(db, name="Inline Customer Med")
    batch = inv.add_or_update_batch(
        db, item, batch_no="IC1", expiry_date=date(2030, 1, 1), quantity=20,
        purchase_rate="5", selling_rate="50", mrp="50",
    )
    db.commit()
    return item, batch


def _sell(client, item, batch, **extra):
    body = {
        "payment_mode": "CASH", "cash_received": 500,
        "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "50"}],
    }
    body.update(extra)
    return client.post("/api/sales", json=body)


def test_mobile_key_ignores_formatting():
    assert customer_service.mobile_key("+91 98765-43210") == "9876543210"
    assert customer_service.mobile_key("098765 43210") == "9876543210"
    assert customer_service.mobile_key("98765") == "98765"


def test_typed_new_customer_is_created_with_the_bill(client, db):
    item, batch = _stock(db)
    login(client)
    resp = _sell(client, item, batch, customer_type="HOME_DELIVERY",
                 new_customer={"name": "  Rahul   Sharma ", "mobile": "98765 43210"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer"]["name"] == "Rahul Sharma"
    customer = db.query(Customer).one()
    assert customer.mobile == "98765 43210" and customer.customer_type == "HOME_DELIVERY"
    assert db.query(Sale).one().customer_id == customer.id


def test_existing_mobile_links_existing_customer_never_duplicates(client, db):
    item, batch = _stock(db)
    existing = customer_service.create_customer(db, name="Asha Rao", mobile="9876543210")
    db.commit()
    login(client)
    found = client.get("/api/customers/lookup", params={"mobile": "+91-98765 43210"}).json()["customer"]
    assert found and found["id"] == existing.id
    resp = _sell(client, item, batch, new_customer={"name": "Someone Else", "mobile": "+91 98765-43210"})
    assert resp.status_code == 200
    assert db.query(Customer).count() == 1
    assert db.query(Sale).one().customer_id == existing.id


def test_lookup_needs_a_full_number(client, db):
    customer_service.create_customer(db, name="Asha Rao", mobile="9876543210")
    db.commit()
    login(client)
    assert client.get("/api/customers/lookup", params={"mobile": "3210"}).json()["customer"] is None


def test_bad_or_incomplete_typed_customer_is_rejected_and_nothing_is_saved(client, db):
    item, batch = _stock(db)
    login(client)
    short = _sell(client, item, batch, new_customer={"name": "Ravi", "mobile": "12345"})
    assert short.status_code == 400 and "10-digit" in short.json()["detail"]
    nameless = _sell(client, item, batch, new_customer={"name": "", "mobile": "9123456780"})
    assert nameless.status_code == 400 and "name" in nameless.json()["detail"]
    assert db.query(Customer).count() == 0 and db.query(Sale).count() == 0


def test_name_only_customer_and_blank_walk_in(client, db):
    item, batch = _stock(db)
    login(client)
    assert _sell(client, item, batch, new_customer={"name": "Walk-in Uncle", "mobile": ""}).status_code == 200
    assert _sell(client, item, batch, new_customer={"name": "", "mobile": ""}).status_code == 200
    sales = db.query(Sale).order_by(Sale.id).all()
    assert sales[0].customer.name == "Walk-in Uncle" and sales[1].customer_id is None


def test_failed_sale_does_not_leave_a_customer_behind(client, db):
    item, batch = _stock(db)
    login(client)
    resp = client.post("/api/sales", json={
        "payment_mode": "CASH", "cash_received": 5000,
        "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 999, "rate": "50"}],
        "new_customer": {"name": "Ghost", "mobile": "9000000001"},
    })
    assert resp.status_code == 400
    assert db.query(Customer).count() == 0


def test_mobile_search_matches_partial_digits_across_formats(client, db):
    customer_service.create_customer(db, name="Asha Rao", mobile="98765 43210")
    customer_service.create_customer(db, name="Bilal Khan", mobile="+91-9876512345")
    customer_service.create_customer(db, name="Chitra", mobile="9123400000")
    db.commit()
    login(client)
    names = lambda q: [c["name"] for c in client.get("/api/customers/mobile-search", params={"q": q}).json()["customers"]]
    assert names("98765") == ["Asha Rao", "Bilal Khan"]
    assert names("6543") == ["Asha Rao"]  # digits across the space
    assert names("9876512345") == ["Bilal Khan"]
    assert names("98") == []  # too short to search
    assert names("5555555555") == []
