"""Customer-invoice kit: mapping a saved sale to the paise view model."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.config import STATIC_DIR
from app.services import inventory_service as inv, invoice_kit, sales_service
from tests.conftest import login


def _item(db, rate="8.00", qty=50):
    item = inv.create_item(db, name="Invoice Kit Med")
    batch = inv.add_or_update_batch(
        db, item, batch_no="IK1", expiry_date=date(2027, 6, 30), quantity=qty,
        purchase_rate="4.00", selling_rate=rate, mrp="10.00",
    )
    db.commit()
    return item, batch


def _paise(value) -> int:
    return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _assert_kit_consistent(d: dict) -> None:
    t = d["totals"]
    assert sum(i["lineTotalPaise"] for i in d["items"]) == t["subtotalPaise"] - t["discountPaise"]
    assert sum(i["discountPaise"] for i in d["items"]) == t["discountPaise"]
    assert t["subtotalPaise"] - t["discountPaise"] + t["taxPaise"] + t["roundingPaise"] == t["totalPaise"]
    assert t["totalPaise"] - t["paidPaise"] == t["duePaise"]
    assert sum(p["amountPaise"] for p in d["payments"]) == t["paidPaise"]
    for i in d["items"]:
        assert i["unitPricePaise"] * i["quantity"] - i["discountPaise"] == i["lineTotalPaise"]
        assert i["lineTotalPaise"] >= 0


def test_item_carries_article_id_mrp_and_terms(db):
    from app.services import settings_service

    settings_service.set_settings(db, {"invoice_footer": "Goods once sold are not returnable."})
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 2, "rate": "8.00"}],
        payment_mode="CASH",
        cash_received="500",
    )
    db.commit()

    data = invoice_kit.build_invoice_view(db, sale)
    line = data["items"][0]
    assert line["articleId"] == item.article_id
    assert line["mrpPaise"] == 1000  # batch MRP 10.00
    assert line["batch"] == item.article_id  # no real batch; the column carries the article id
    assert data["terms"] == "Goods once sold are not returnable."
    assert data["totals"]["taxPaise"] == 0
    assert data["totals"]["taxLines"] == []


def test_maps_discounts_voucher_and_rounding(db):
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db,
        lines=[
            {"item_id": item.id, "batch_id": batch.id, "quantity": 3, "rate": "8.00"},
            {"item_id": item.id, "batch_id": batch.id, "quantity": 2, "rate": "8.00", "discount": "1.50"},
        ],
        discount="5.00",
        voucher="2.00",
        payment_mode="CASH",
        cash_received="100",
    )
    db.commit()

    data = invoice_kit.build_invoice_view(db, sale)
    _assert_kit_consistent(data)
    totals = data["totals"]
    assert totals["totalPaise"] == _paise(sale.total)
    assert totals["roundingPaise"] == _paise(sale.round_off)
    assert totals["paidPaise"] == totals["totalPaise"]
    assert totals["duePaise"] == 0
    assert totals["changePaise"] == 10000 - totals["totalPaise"]
    assert totals["duePaise"] == 0
    assert data["sample"] is False
    assert data["business"]["logo"] and data["business"]["logo"].endswith(".svg")
    assert len(data["items"]) == 2


def test_partly_paid_has_amount_due(db):
    item, batch = _item(db)
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "100.00"}],
        payment_mode="CASH",
        cash_received="100",
    )
    db.commit()
    data = invoice_kit.build_invoice_view(db, sale)
    _assert_kit_consistent(data)
    assert data["totals"]["duePaise"] == 0


def test_invoice_view_endpoint(client, db):
    item, batch = _item(db)
    login(client)
    created = client.post(
        "/api/sales",
        json={
            "payment_mode": "CASH",
            "cash_received": 500,
            "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "8.00"}],
        },
    )
    assert created.status_code == 200, created.text
    sale_id = created.json()["sale_id"]
    resp = client.get(f"/api/sales/{sale_id}/invoice-view")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    _assert_kit_consistent(data)
    assert data["number"] == created.json()["invoice_no"]
    assert data["items"][0]["name"] == "Invoice Kit Med"


def test_invoice_view_404_and_cancelled(client, db):
    from app.models import Sale

    item, batch = _item(db)
    login(client)
    assert client.get("/api/sales/999999/invoice-view").status_code == 404
    created = client.post(
        "/api/sales",
        json={
            "payment_mode": "CASH",
            "cash_received": 500,
            "lines": [{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "8.00"}],
        },
    ).json()
    sale = db.get(Sale, created["sale_id"])
    sale.payment_status = "CANCELLED"
    db.commit()
    assert client.get(f"/api/sales/{sale.id}/invoice-view").status_code == 400


def test_header_fields_and_registration_checkboxes(db):
    from app.services import settings_service

    item, batch = _item(db)
    settings_service.set_settings(
        db,
        {
            "tagline": "Care first",
            "pharmacy_email": "care@example.test",
            "pharmacy_website": "example.test",
            "gst_number": "36ABCDE1234F1Z5",
            "show_gst": "0",
            "drug_license_number": "20B/21B/TS/2019-1234",
            "show_drug_license": "1",
        },
    )
    sale = sales_service.create_sale(
        db,
        lines=[{"item_id": item.id, "batch_id": batch.id, "quantity": 1, "rate": "8.00"}],
        payment_mode="CASH",
        cash_received="500",
    )
    db.commit()

    data = invoice_kit.build_invoice_view(db, sale)
    labels = [r["label"] for r in data["business"]["registrations"]]
    assert "GSTIN" not in labels  # checkbox off
    assert "Drug License" in labels  # checkbox on
    assert data["business"]["tagline"] == "Care first"
    assert "care@example.test" in data["business"]["contact"]
    assert "example.test" in data["business"]["contact"]

    settings_service.set_settings(db, {"show_gst": "1"})
    data = invoice_kit.build_invoice_view(db, sale)
    assert "GSTIN" in [r["label"] for r in data["business"]["registrations"]]


def test_amount_words():
    assert invoice_kit.amount_words(0) == "Rupees Zero Only"
    assert invoice_kit.amount_words(6700) == "Rupees Sixty Seven Only"
    assert invoice_kit.amount_words(105) == "Rupees One and Five Paise Only"


def test_v2_engine_bundled_with_logo_and_a2():
    base = STATIC_DIR / "invoice-kit-v2"
    js = (base / "invoice-engine.js").read_text(encoding="utf-8")
    assert "A2:[420,594]" in js
    assert "horizontal-color.svg" in js
    for name in ("horizontal-color.svg", "horizontal-mono.svg", "symbol-color.svg", "symbol-mono.svg"):
        assert (base / "assets" / name).exists()
    studio = (base / "invoice-settings.js").read_text(encoding="utf-8")
    assert "Article ID" in studio
    assert "showTerms: false" in studio
