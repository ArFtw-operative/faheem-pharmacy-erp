"""Expiry, stock adjustments, admin and settings: integration with stock, reports and P&L."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models import Supplier
from app.services import inventory_service as inv, settings_service
from tests.conftest import login


def _batch(db, name="Expiring Med", days=-10, qty=20, cost="5", supplier=True):
    item = inv.create_item(db, name=name)
    sup = None
    if supplier:
        sup = Supplier(name="Gupta Dist")
        db.add(sup)
        db.flush()
    batch = inv.add_or_update_batch(
        db, item, batch_no="EX1", expiry_date=date.today() + timedelta(days=days), quantity=qty,
        purchase_rate=cost, selling_rate="9", mrp="10", supplier_id=sup.id if sup else None,
    )
    db.commit()
    return item, batch


def test_adjustment_api_requires_reason_and_valid_qty(client, db):
    item, batch = _batch(db, days=200)
    login(client)
    post = lambda **kw: client.post("/api/erp/adjustments", json={"item_id": item.id, "batch_id": batch.id, "direction": "OUT",
                                                                  "category": "DAMAGE", **kw})
    assert post(quantity=2, reason="").status_code == 400
    assert post(quantity=0, reason="x").status_code == 400
    assert post(quantity=99, reason="x").status_code == 400
    ok = post(quantity=3, reason="crushed")
    assert ok.status_code == 200 and ok.json()["stock"] == 17
    assert ok.json()["adjustment"]["reference_no"].startswith("ADJ-")
    listing = client.get("/api/erp/adjustments", params={"category": "DAMAGE"}).json()
    assert "crushed" in listing["adjustments"][0]["reason"]


def test_logo_paths_are_portable_and_contained(db, tmp_path):
    from app.config import UPLOAD_DIR

    settings_service.save_logo(db, "brand.png", b"\x89PNG fake")
    assert settings_service.get_setting(db, "logo_path") == "branding/logo.png"
    assert settings_service.logo_file(db) == (UPLOAD_DIR / "branding" / "logo.png").resolve()
    # an absolute path from another install still resolves by name inside this install
    settings_service.set_setting(db, "logo_path", "/home/someone/else/uploads/branding/logo.png")
    assert settings_service.logo_file(db) is not None
    settings_service.set_setting(db, "logo_path", "../../etc/passwd")
    assert settings_service.logo_file(db) is None


def test_invoice_list_search_finds_products_inside_bills(client, db):
    from app.models import Purchase, PurchaseItem

    p = Purchase(invoice_no="INV-778", total=Decimal("10"), status="POSTED")
    db.add(p)
    db.flush()
    db.add(PurchaseItem(purchase_id=p.id, product_name="CODVEL-SG CAP", batch_no="LAL26120", quantity=1, rate=Decimal("10"), line_total=Decimal("10")))
    db.commit()
    login(client)
    find = lambda q: [r["invoice_no"] for r in client.get("/api/erp/purchases", params={"q": q}).json()["purchases"]]
    assert find("codvel") == ["INV-778"] and find("LAL261") == ["INV-778"] and find("nothing-like-this") == []
