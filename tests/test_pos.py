"""Point of Sale workflow: search, customers, batch handling and billing."""
from __future__ import annotations

from datetime import date

from app.services import inventory_service as inv
from tests.conftest import login


def _item_with_batches(db, name="POS Test Med", batches=(("B1", date(2027, 1, 1), 10),)):
    item = inv.create_item(db, name=name)
    for no, expiry, qty in batches:
        inv.add_or_update_batch(
            db, item, batch_no=no, expiry_date=expiry, quantity=qty,
            purchase_rate="5", selling_rate="9", mrp="10",
        )
    db.commit()
    return item


def test_item_search_excludes_expired_and_out_of_stock(client, db):
    item = _item_with_batches(
        db,
        batches=(
            ("OK1", date(2027, 1, 1), 5),
            ("OLD", date(2020, 1, 1), 5),
            ("ZERO", date(2027, 6, 1), 0),
        ),
    )
    login(client)
    data = client.get(f"/api/items/search?q={item.name}").json()
    assert data["items"], "expected a search hit"
    hit = data["items"][0]
    nos = [b["batch_no"] for b in hit["batches"]]
    assert nos == ["OK1"]
    assert hit["stock"] == 5


def test_item_search_barcode_is_exact(client, db):
    item = _item_with_batches(db)
    item.barcode = "1234567890123"
    db.commit()
    login(client)
    data = client.get("/api/items/search?q=1234567890123").json()
    assert data.get("exact") is True
    assert data["items"][0]["id"] == item.id


def test_item_batches_endpoint(client, db):
    item = _item_with_batches(db, batches=(("A1", date(2027, 1, 1), 3), ("A2", date(2028, 1, 1), 4)))
    login(client)
    data = client.get(f"/api/items/{item.id}/batches").json()
    assert data["stock"] == 7
    assert [b["batch_no"] for b in data["batches"]] == ["A1", "A2"]


def test_customer_search_and_inline_create(client):
    login(client)
    created = client.post(
        "/api/customers", json={"name": "Abdur Rahman", "mobile": "9876543210"}
    )
    assert created.status_code == 200
    customer = created.json()["customer"]
    assert customer["customer_id"].startswith("CUST")

    found = client.get("/api/customers/search?q=9876543210").json()["customers"]
    assert [c["id"] for c in found] == [customer["id"]]


def test_customer_create_requires_name_and_mobile(client):
    login(client)
    assert client.post("/api/customers", json={"name": "No Mobile"}).status_code == 400
    assert client.post("/api/customers", json={"mobile": "9000000000"}).status_code == 400


def test_customer_duplicate_mobile_is_rejected(client):
    login(client)
    first = client.post("/api/customers", json={"name": "Abdur Rahman", "mobile": "9000000001"})
    assert first.status_code == 200
    dup = client.post("/api/customers", json={"name": "Someone Else", "mobile": "9000000001"})
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["customer"]["name"] == "Abdur Rahman"


def test_customer_profile_fields_persist_and_search(client):
    login(client)
    resp = client.post(
        "/api/customers",
        json={
            "name": "Full Profile", "mobile": "9000000002", "gender": "Female",
            "date_of_birth": "1990-01-02", "email": "f@example.com",
            "city": "Hyderabad", "state": "Telangana", "pincode": "500001",
        },
    )
    assert resp.status_code == 200
    customer = resp.json()["customer"]
    assert customer["gender"] == "Female"
    assert customer["date_of_birth"] == "1990-01-02"
    found = client.get("/api/customers/search?q=9000000002").json()["customers"][0]
    assert found["city"] == "Hyderabad" and found["pincode"] == "500001"


def test_recent_sales_lists_newest_first(client, db):
    item = _item_with_batches(db)
    login(client)
    for _ in range(2):
        client.post(
            "/api/sales",
            json={
                "payment_mode": "CASH",
                "lines": [{"item_id": item.id, "quantity": 1, "rate": "9"}],
            },
        )
    data = client.get("/api/sales/recent?limit=5").json()
    assert len(data["sales"]) == 2
    assert data["sales"][0]["invoice_no"] > data["sales"][1]["invoice_no"]
