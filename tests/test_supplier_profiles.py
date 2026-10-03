"""Supplier recognition from learned invoice profiles (layout + invoice-number shape)."""
from __future__ import annotations

from datetime import date

from app.models import SupplierInvoiceProfile
from app.services import purchasing, supplier_profiles
from tests.test_purchasing import dolo, supplier

MARG = "ProdCode,ProdName,Packing,BatchNo,Expiry,Qty,Free,Rate,Mrp,ProValue\n"


def marg(*rows):
    return (MARG + "\n".join(rows) + "\n").encode()


def post_one(db, sup, invoice_no, batch):
    p = purchasing.create_from_file(db, f"{invoice_no}.csv", marg(f"D650,DOLO 650MG TAB,15S,{batch},May-2028,1,,24,33.6,24"),
                                    supplier_id=sup.id, invoice_no=invoice_no, invoice_date=date(2026, 9, 1))
    purchasing.post(db, p)
    return p


def test_invoice_shape():
    assert supplier_profiles.invoice_shape("NR03897") == "NR#####"
    assert supplier_profiles.invoice_shape("0000000028_C1") == "##########_C#"
    assert supplier_profiles.invoice_shape("") == ""


def test_posting_learns_layout_and_invoice_shape(db):
    dolo(db)
    sup = supplier(db)
    post_one(db, sup, "NR03895", "B1")
    prof = db.query(SupplierInvoiceProfile).one()
    assert prof.supplier_id == sup.id and prof.invoice_patterns == ["NR#####"] and prof.invoices == 1


def test_layout_and_shape_together_assign_the_supplier(db):
    dolo(db)
    sup = supplier(db)
    post_one(db, sup, "NR03895", "B1")
    p = purchasing.create_from_file(db, "next.csv", marg("D650,DOLO 650MG TAB,15S,B2,May-2028,2,,24,33.6,48"), invoice_no="NR03897")
    rec = p.charges["_supplier_recognition"]
    assert p.supplier_id == sup.id and rec["assigned"] and rec["confidence"] < 0.97     # shown with a warning
    assert p.items[0].status == "READY"


def test_layout_alone_only_suggests(db):
    dolo(db)
    sup = supplier(db)
    post_one(db, sup, "NR03895", "B1")
    p = purchasing.create_from_file(db, "other.csv", marg("D650,DOLO 650MG TAB,15S,B3,May-2028,2,,24,33.6,48"), invoice_no="IC20256")
    rec = p.charges["_supplier_recognition"]
    assert p.supplier_id is None and not rec["assigned"]
    assert rec["suggestions"][0]["supplier_id"] == sup.id and rec["suggestions"][0]["signals"] == ["same file layout"]


def test_shape_shared_by_two_suppliers_is_not_assigned(db):
    dolo(db)
    a, b = supplier(db, "Alpha Distributors"), supplier(db, "Beta Distributors")
    post_one(db, a, "INV1001", "B1")
    post_one(db, b, "INV2002", "B2")
    p = purchasing.create_from_file(db, "x.csv", marg("D650,DOLO 650MG TAB,15S,B4,May-2028,2,,24,33.6,48"), invoice_no="INV3003")
    assert p.supplier_id is None
    assert {s["supplier_id"] for s in p.charges["_supplier_recognition"]["suggestions"]} == {a.id, b.id}
