"""Customer categories: Walk-In vs Home Delivery across POS, invoice and reports."""
from __future__ import annotations

from datetime import date

from app.services import business_time as cs
from app.services import customer_service, invoice_render, inventory_service as inv, reports_service, sales_service
from tests.conftest import login


def _item(db, rate="100", qty=10):
    item = inv.create_item(db, name="Channel Test Med")
    batch = inv.add_or_update_batch(
        db, item, batch_no="CT1", expiry_date=date(2027, 1, 1), quantity=qty,
        purchase_rate="5", selling_rate=rate, mrp=rate,
    )
    db.commit()
    return item, batch


def test_normalize_customer_type():
    assert customer_service.normalize_customer_type("home delivery") == "HOME_DELIVERY"
    assert customer_service.normalize_customer_type("home-delivery") == "HOME_DELIVERY"
    assert customer_service.normalize_customer_type("WALK_IN") == "WALK_IN"
    assert customer_service.normalize_customer_type("") == "WALK_IN"
    assert customer_service.normalize_customer_type("nonsense") == "WALK_IN"
    assert customer_service.customer_type_label("HOME_DELIVERY") == "Home Delivery"


def test_create_sale_records_customer_type_and_invoice_shows_it(db):
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "100"}],
        customer_type="HOME_DELIVERY",
    )
    db.commit()
    assert sale.customer_type == "HOME_DELIVERY"
    text = invoice_render.invoice_text(sale, {"pharmacy_name": "Test Pharmacy"})
    assert "Customer type: Home Delivery" in text


def test_create_sale_defaults_to_walk_in(db):
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db, lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "100"}]
    )
    db.commit()
    assert sale.customer_type == "WALK_IN"


def test_pos_customer_creation_records_category(client, db):
    login(client)
    resp = client.post(
        "/api/customers",
        json={"name": "Nadia Home", "mobile": "9876501234", "customer_type": "HOME_DELIVERY"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()["customer"]
    assert payload["customer_type"] == "HOME_DELIVERY"
    assert payload["customer_type_label"] == "Home Delivery"

    found = client.get("/api/customers/search", params={"q": "Nadia"}).json()["customers"]
    assert found and found[0]["customer_type"] == "HOME_DELIVERY"

    # duplicate mobile returns the existing customer so POS can select it
    dup = client.post(
        "/api/customers",
        json={"name": "Nadia Home", "mobile": "9876501234", "customer_type": "WALK_IN"},
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["customer"]["customer_type"] == "HOME_DELIVERY"


def test_sale_api_persists_customer_type(client, db):
    item, batch = _item(db)
    login(client)
    resp = client.post(
        "/api/sales",
        json={
            "payment_mode": "CASH",
            "cash_received": 500,
            "customer_type": "HOME_DELIVERY",
            "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "100"}],
        },
    )
    assert resp.status_code == 200, resp.text

    from app.models import Sale

    sale = db.query(Sale).order_by(Sale.id.desc()).first()
    assert sale.customer_type == "HOME_DELIVERY"


def test_report_groups_by_customer_type(client, db):
    item, batch = _item(db)
    login(client)
    for ctype in ("WALK_IN", "HOME_DELIVERY", "HOME_DELIVERY"):
        client.post(
            "/api/sales",
            json={
                "payment_mode": "CASH",
                "cash_received": 500,
                "customer_type": ctype,
                "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "100"}],
            },
        )
    code = cs.timezone_name(db)
    start, end = reports_service.period_range("today", code)
    rows = {r["type"]: r for r in reports_service.sales_by_customer_type(db, start, end)}
    assert rows["WALK_IN"]["count"] == 1
    assert rows["HOME_DELIVERY"]["count"] == 2
    assert rows["HOME_DELIVERY"]["label"] == "Home Delivery"


def test_pos_api_changes_customer_type_in_place(client, db):
    from app.models import Customer

    login(client)
    created = client.post(
        "/api/customers",
        json={"name": "Switch Me", "mobile": "9800011122", "customer_type": "WALK_IN"},
    ).json()["customer"]
    resp = client.post(
        f"/api/customers/{created['id']}/type", json={"customer_type": "HOME_DELIVERY"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["customer"]["customer_type"] == "HOME_DELIVERY"
    assert db.get(Customer, created["id"]).customer_type == "HOME_DELIVERY"
