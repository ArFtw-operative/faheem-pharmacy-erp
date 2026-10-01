"""Geometry-based native PDF table reconstruction (a three-page rough estimate).

The estimate is generated (tests/invoice_samples.rough_estimate_pdf) in the layout of a common billing
package: 132 items across 50 / 49 / 33 rows, carry-over totals, a negative round-off printed without its
sign, free/offer quantities, duplicate names and a product whose name contains TOTAL. These tests pin
that every row, page sum and total is recovered exactly and no summary/footer line becomes an item.
"""
from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from app.services import pdf_tables
from tests.invoice_samples import rough_estimate_pdf

_PDF, EXPECT = rough_estimate_pdf()
FIXTURE = Path(tempfile.mkdtemp(prefix="estimate-")) / "rough-estimate-E004512.pdf"
FIXTURE.write_bytes(_PDF)


def _doc():
    doc = pdf_tables.reconstruct(FIXTURE)
    assert doc is not None, "native geometry reconstruction returned None"
    return doc


def _by_serial(doc, serial):
    return next(r for r in doc.rows if r.serial == serial)


def test_reconstructs_exactly_132_rows():
    doc = _doc()
    assert doc.item_count == 132
    assert doc.page_counts == [50, 49, 33]
    serials = [r.serial for r in doc.rows]
    assert serials == list(range(1, 133))


def test_page_sums_subtotal_roundoff_and_grand_total():
    doc = _doc()
    assert doc.page_sums == EXPECT["page_sums"]
    assert doc.subtotal == EXPECT["subtotal"] and str(doc.subtotal).endswith(".49")
    assert doc.roundoff == Decimal("-0.49")  # signed, inferred from grand total
    assert doc.grand_total == EXPECT["grand_total"]


def test_every_line_reconciles_paid_quantity_times_rate():
    doc = _doc()
    failures = [r.serial for r in doc.rows if not r.arithmetic_pass]
    assert failures == []


def test_quantity_overflow_and_offers_are_preserved():
    doc = _doc()
    row54 = _by_serial(doc, 54)
    assert row54.quantity_raw == "100+20"
    assert row54.quantity_paid == 100
    assert row54.quantity_free == 20
    assert row54.rate == Decimal("8.75")
    assert row54.line_amount == Decimal("875.00")  # not 120 x 8.75

    offers = {128: (5, 1), 129: (6, 1), 130: (11, 2), 131: (10, 2), 132: (6, 2)}
    for serial, (paid, free) in offers.items():
        row = _by_serial(doc, serial)
        assert (row.quantity_paid, row.quantity_free) == (paid, free)


def test_identical_descriptions_are_not_deduplicated():
    doc = _doc()
    assert _by_serial(doc, 123).description_raw == "COTTON PADS XL"
    assert _by_serial(doc, 124).description_raw == "COTTON PADS XL"
    assert _by_serial(doc, 123).mrp != _by_serial(doc, 124).mrp


def test_descriptions_retain_embedded_mrp_and_units():
    doc = _doc()
    assert "RS.180" in _by_serial(doc, 61).description_raw
    assert _by_serial(doc, 61).mrp == Decimal("160.00")  # numeric column wins
    assert "400g" in _by_serial(doc, 1).description_raw


def test_summary_and_footer_rows_are_not_items():
    doc = _doc()
    import re

    summary = re.compile(r"^(total|sub\s*total|grand\s*total|c/?f|b/?f|continued|for\s+more|contact)\b", re.I)
    for row in doc.rows:
        # Every item carries a real serial anchor, so summary/footer lines
        # (which have none) can never appear here.
        assert row.serial is not None
        assert not summary.match(row.description_raw), row.description_raw
    # "INFANT FORMULA TOTAL COMFORT" is a genuine product, not the TOTAL summary line.
    assert any("TOTAL COMFORT" in r.description_raw.upper() for r in doc.rows)


def test_validation_report_is_valid():
    report = pdf_tables.validate(_doc())
    assert report["status"] == "valid"
    assert report["issues"] == []
    assert report["item_count"] == 132
    assert report["free_units"] == 28


def test_document_header_fields():
    doc = _doc()
    assert doc.document_type == "ESTIMATE"
    assert doc.document_number == EXPECT["number"]
    assert doc.party_name.startswith(EXPECT["party"])
