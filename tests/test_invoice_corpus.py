"""One import engine, many supplier layouts.

Every file below comes from a different family of billing software (see
tests/invoice_samples.py). None of them is special-cased anywhere: the same
column understanding reads them all, and each must reconcile to its printed
total where it has one.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services import purchasing
from tests import invoice_samples as S

EXPECTED = {  # line 4 of every sample: the long-named syrup
    "name": "ALZYME SYRUP MIXED FRUIT FLAVOUR 200ML BOTTLE", "batch": "AZY882", "expiry": date(2027, 8, 1),
    "quantity": 5, "rate": Decimal("61.20"), "mrp": Decimal("85.00"), "line_total": Decimal("306.00"),
}
NET = Decimal(f"{S.net_total():.2f}")


def _supplier(db, name="Corpus Agency", gstin=""):
    return purchasing.save_supplier(db, {"name": name, "gst_number": gstin})


def _check_line(line, *, name=True, batch=None, mrp=True):
    if name:
        assert line.product_name == EXPECTED["name"]
    assert (line.batch_no, line.expiry_date) == (batch or EXPECTED["batch"], EXPECTED["expiry"])   # numeric batches stay text
    if mrp:
        assert line.mrp == EXPECTED["mrp"]
    assert (line.quantity, line.rate, line.line_total) == (EXPECTED["quantity"], EXPECTED["rate"], EXPECTED["line_total"])


LAYOUTS = [
    ("busy.csv", S.busy_style_csv, {"invoice_no": "SBP/1024", "total": True}),
    ("retailgraph.csv", S.retailgraph_style_csv, {}),
    ("easysol.csv", S.easysol_style_semicolon, {}),
    ("letterhead.xlsx", S.xlsx_with_letterhead, {"invoice_no": "MHW-77", "total": True, "batch": "240304"}),
    ("ruled.pdf", lambda: S.pdf_invoice(ruled=True, wrap=True), {"invoice_no": "SBP/2231", "total": True}),
    ("plain-multipage.pdf", lambda: S.pdf_invoice(ruled=False, wrap=True, per_page=6), {"invoice_no": "SBP/2231"}),
    ("two-line-header.pdf", lambda: S.pdf_invoice(ruled=False, wrap=True, two_line_header=True), {"invoice_no": "SBP/2231", "total": True}),
]


@pytest.mark.parametrize("filename,build,expect", LAYOUTS, ids=[l[0] for l in LAYOUTS])
def test_every_layout_is_understood(db, filename, build, expect):
    p = purchasing.create_from_file(db, filename, build(), supplier_id=_supplier(db).id, invoice_no="" if expect.get("invoice_no") else "X-1")
    lines = p.items
    assert len(lines) in (5, 15)
    _check_line(lines[3], batch=expect.get("batch"))
    assert all(l.batch_no and l.expiry_date and l.quantity and l.rate and l.mrp for l in lines)
    if expect.get("invoice_no"):
        assert p.invoice_no == expect["invoice_no"]
    if expect.get("total"):
        s = purchasing.summary(p)
        assert p.supplier_total == NET
        assert s["difference"] == "0.00", s["totals"]


def test_accounting_export_without_batches_goes_to_review_not_guessing(db):
    p = purchasing.create_from_file(db, "tally.csv", S.tally_style_csv(), supplier_id=_supplier(db).id, invoice_no="T-1")
    line = p.items[3]
    assert (line.product_name, line.quantity, line.rate) == (EXPECTED["name"], 5, EXPECTED["rate"])
    codes = {i["code"] for i in line.issues}
    assert {"batch_missing", "expiry_missing", "mrp_missing"} <= codes
    assert line.status not in ("READY", "CORRECTED")


def test_supplier_is_recognised_from_the_gstin_printed_on_the_invoice(db):
    sup = _supplier(db, "Sri Balaji Pharma", gstin="36AAAFB1234C1Z9")
    other = _supplier(db, "Someone Else", gstin="29ABCDE1234F1Z5")
    p = purchasing.create_from_file(db, "bill.pdf", S.pdf_invoice(ruled=False, wrap=True))
    assert p.supplier_id == sup.id and p.supplier_id != other.id
    q = purchasing.create_from_file(db, "busy.csv", S.busy_style_csv())
    assert q.supplier_id == sup.id


def test_unknown_supplier_is_reported_not_guessed(db):
    p = purchasing.create_from_file(db, "bill.pdf", S.pdf_invoice(ruled=False, wrap=True, gstin="36ZZZZZ9999Z1Z9"))
    assert p.supplier_id is None
    assert "not in the supplier master" in p.extraction_meta and "36ZZZZZ9999Z1Z9" in p.extraction_meta


def test_each_supplier_keeps_its_own_learned_layout(db):
    """A correction for one supplier never changes how another supplier's file is read."""
    a, b = _supplier(db, "Agency A"), _supplier(db, "Agency B")
    body = S.retailgraph_style_csv()
    pa = purchasing.create_from_file(db, "a.csv", body, supplier_id=a.id, invoice_no="A1")
    purchasing.remap(db, pa, {"Mfr": "ignore"})
    assert a.column_profile == {"mfr": "ignore"} and not b.column_profile
    pb = purchasing.create_from_file(db, "b.csv", body.replace(b"I0001", b"J0001"), supplier_id=b.id, invoice_no="B1")
    assert pa.items[0].manufacturer == "" and pb.items[0].manufacturer == "MICRO LABS"
