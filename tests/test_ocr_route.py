"""Scanned PDFs and photos: OCR with word boxes → rebuilt table → reconciliation → review."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.services import ocr, purchase_import, purchasing
from tests.test_purchasing import supplier

FONT = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                         "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                         "C:/Windows/Fonts/consola.ttf") if Path(p).is_file()), None)
pytestmark = pytest.mark.skipif(not ocr.available() or FONT is None, reason="Tesseract or a monospace font is not available")

ROWS = [("DETTOL LIQUID 125ML", "2.00", "83", "74", "148"),
        ("MOOV SPRAY 35G", "1.00", "186", "162", "162"),
        ("IODEX 20GRMS", "3.00", "95", "83", "249"),
        ("COLGATE PASTE 100GRMS", "2.00", "73", "66", "130")]      # 2 × 66 = 132: does not reconcile


def receipt(two_line: bool = True) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(FONT, 30)
    img = Image.new("L", (1100, 160 + 100 * len(ROWS)), 255)
    d = ImageDraw.Draw(img)
    d.text((40, 20), "GOOD HEALTH TRADERS", font=font, fill=0)
    d.text((40, 60), "Bill No : GH-0042   Date : 08-Sep-2026", font=font, fill=0)
    d.text((80, 120), "QTY", font=font, fill=0)
    for x, h in ((330, "MRP"), (560, "RATE"), (800, "TOTAL")):
        d.text((x, 120), h, font=font, fill=0)
    y = 180
    for name, qty, mrp, rate, total in ROWS:
        d.text((40, y), name, font=font, fill=0)
        y += 40
        for x, v in ((80, qty), (330, mrp), (560, rate), (800, total)):
            d.text((x, y), v, font=font, fill=0)
        y += 60
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def as_pdf(png: bytes) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def test_photo_is_read_and_rows_are_checked_by_arithmetic():
    doc = purchase_import.parse("receipt.png", receipt())
    assert doc.method == "ocr" and len(doc.lines) == 4
    lines = {l.raw["name"]: l.raw for l in doc.lines}
    assert lines["DETTOL LIQUID 125ML"]["quantity"] == "2.00" and lines["DETTOL LIQUID 125ML"]["_ocr"]["reconciled"]
    assert not lines["COLGATE PASTE 100GRMS"]["_ocr"]["reconciled"]
    assert all(0 < l.raw["_ocr"]["confidence"] <= 1 for l in doc.lines)
    assert doc.invoice_no == "GH-0042" and doc.invoice_date == "08-Sep-2026"


def test_scanned_pdf_uses_ocr_and_a_text_pdf_never_does():
    from tests.invoice_samples import rough_estimate_pdf

    assert purchase_import._is_scan(as_pdf(receipt()))
    text_pdf, _ = rough_estimate_pdf()
    assert not purchase_import._is_scan(text_pdf)
    doc = purchase_import.parse("scan.pdf", as_pdf(receipt()))
    assert doc.method == "ocr" and len(doc.lines) == 4


def test_unreconciled_ocr_line_goes_to_review_and_reconciled_lines_do_not(db):
    from app.services import settings_service
    settings_service.set_setting(db, "purchase_scan_import", "on")
    from app.services import confidence_gate

    sup = supplier(db)
    p = purchasing.create_from_file(db, "receipt.png", receipt(), supplier_id=sup.id, invoice_no="GH-0042")
    by_name = {l.product_name: l for l in p.items}
    bad = by_name["COLGATE PASTE 100GRMS"]
    assert any(i["code"] == "ocr_unverified" for i in bad.issues)
    assert confidence_gate.assess(bad)["fields"]["quantity"] <= 0.6
    good = by_name["DETTOL LIQUID 125ML"]
    assert not any(i["code"] == "ocr_unverified" for i in good.issues)
    # a person checking the paper and correcting the amount clears the OCR doubt
    purchasing.correct(db, p, bad, {"amount": "132"})
    assert not any(i["code"] == "ocr_unverified" for i in bad.issues)


def test_reconciliation_tries_alternatives_and_uses_only_a_unique_one():
    raw = {"quantity": "2", "rate": "95", "mrp": "83", "amount": "166"}       # rate and MRP read in swapped columns
    ok, how = ocr.reconcile_row(raw)
    assert ok and how == "swap rate and MRP" and (raw["rate"], raw["mrp"]) == ("83", "95")
    raw = {"quantity": "", "rate": "45", "mrp": "50", "amount": "180"}
    ok, how = ocr.reconcile_row(raw)
    assert ok and raw["quantity"] == "4"
    raw = {"quantity": "7", "rate": "45", "mrp": "50", "amount": "180"}
    assert ocr.reconcile_row(raw) == (False, "")


def test_ocr_number_fixes():
    assert ocr.fix_number("1,00") == "1.00" and ocr.fix_number("1O0") == "100" and ocr.fix_number("1,000") == "1,000"
