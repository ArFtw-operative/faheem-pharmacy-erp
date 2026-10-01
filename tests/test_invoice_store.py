"""Settings → Invoice Store: premium WhatsApp invoice templates (paging, optional fields, stamp)."""
from __future__ import annotations

import pymupdf
import pytest

from app.services import invoice_premium as P
from app.services import inventory_service as inv
from app.services import sales_service, settings_service
from tests.conftest import login


def _pages(pdf: bytes):
    return [p.get_text() for p in pymupdf.open("pdf", pdf)]


@pytest.mark.parametrize("items,pages", [(1, 1), (5, 1), (6, 2), (10, 2), (11, 3)])
def test_five_lines_per_page_never_split_and_summary_only_on_the_last_page(db, items, pages):
    texts = _pages(P.render(P.demo_data(db, items), "a4-compact-stamp"))
    assert len(texts) == pages
    for n, t in enumerate(texts, start=1):
        import re
        assert len(re.findall(r"\bDB4\d{3}\b", t)) <= 5                       # demo batches: one per line
        assert "ITEM DESCRIPTION" in t                                     # the table header repeats
        assert ("NET AMOUNT" in t) == (n == pages)                         # summary on the last page only
        assert ("Continued on page" in t) == (n < pages)
        if pages > 1:
            assert f"Page {n} of {pages}" in t


def test_items_per_page_setting_and_optional_fields(db):
    settings_service.set_settings(db, {"invoice_store_items_per_page": "3", "gst_number": "36ABCDE1234F1Z5", "show_gst": "0",
                                       "drug_license_number": "20B/21B-1234", "show_drug_license": "0"})
    pdf = P.render(P.demo_data(db, 7), "a4-compact")
    texts = _pages(pdf)
    assert len(texts) == 3 and "GSTIN" not in texts[0] and "20B/21B" not in texts[0]     # off: not printed
    settings_service.set_settings(db, {"show_gst": "1", "show_drug_license": "1"})
    first = _pages(P.render(P.demo_data(db, 2), "a4-compact"))[0]
    assert "GSTIN 36ABCDE1234F1Z5" in first and "DL 20B/21B-1234" in first


def test_stamp_only_on_the_stamp_template(db):
    with_stamp = pymupdf.open("pdf", P.render(P.demo_data(db, 2), "a4-compact-stamp"))
    without = pymupdf.open("pdf", P.render(P.demo_data(db, 2), "a4-compact"))
    assert len(with_stamp[0].get_xobjects()) > len(without[0].get_xobjects())           # the stamp drawing


def test_real_bill_and_whatsapp_uses_the_chosen_template(db, tmp_path, monkeypatch):
    from app.services.whatsapp import service as wa

    monkeypatch.setattr(wa, "UPLOAD_DIR", tmp_path)
    item = inv.create_item(db, name="DOLO 650 TAB", pack_size="15 S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15, loose_sale=True)
    inv.add_or_update_batch(db, item, batch_no="D1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="32.28")
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 10}], round_off_mode="NEAREST_RUPEE")
    db.commit()
    data = P.invoice_data(db, sale)
    from decimal import Decimal
    assert data["lines"][0]["amount"] == Decimal("21.52") and data["total"] == 22       # strip MRP × 10 ÷ 15
    text = pymupdf.open(wa.invoice_pdf(db, sale))[0].get_text()
    assert sale.invoice_no in text and "NET AMOUNT" in text and "₹32.28/15" in text      # premium by default
    settings_service.set_settings(db, {"invoice_store_template": "classic"})
    classic = pymupdf.open(wa.invoice_pdf(db, sale))[0].get_text()
    assert "NET AMOUNT" not in classic                                                    # back to the classic file


def test_invoice_store_api(client, db):
    login(client)
    v = client.get("/api/erp/settings/invoice-store").json()
    assert v["template"] == "a4-compact-stamp" and {t["id"] for t in v["templates"]} == {"a4-compact-stamp", "a4-compact", "classic"}
    r = client.put("/api/erp/settings/invoice-store", json={"template": "a4-compact", "items_per_page": 4,
                   "fields": {"address": "Shop 4, Yakutpura", "pharmacy_email": "a@b.in"}, "switches": {"show_gst": True}})
    assert r.status_code == 200 and r.json()["template"] == "a4-compact" and r.json()["items_per_page"] == 4
    assert r.json()["fields"]["address"] == "Shop 4, Yakutpura" and r.json()["switches"]["show_gst"] is True
    assert client.put("/api/erp/settings/invoice-store", json={"items_per_page": 12}).status_code == 400
    assert client.put("/api/erp/settings/invoice-store", json={"template": "nope"}).status_code == 400
    pdf = client.get("/api/erp/settings/invoice-store/preview.pdf?template=a4-compact&items=9")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pymupdf.open("pdf", pdf.content)) == 3
    png = client.get("/api/erp/settings/invoice-store/preview.png?items=9&page=2")
    assert png.status_code == 200 and png.headers["x-pages"] == "3" and png.content[:4] == b"\x89PNG"   # 4 per page
    bad = client.post("/api/erp/settings/invoice-store/stamp", files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    stamp_png = pymupdf.open(); stamp_png.new_page(width=50, height=50)
    pix = stamp_png[0].get_pixmap().tobytes("png")
    ok = client.post("/api/erp/settings/invoice-store/stamp", files={"file": ("stamp.png", pix, "image/png")})
    assert ok.status_code == 200 and ok.json()["custom_stamp"] is True
    assert client.get("/api/erp/settings/invoice-store/stamp.png").status_code == 200
    assert client.delete("/api/erp/settings/invoice-store/stamp").json()["custom_stamp"] is False
