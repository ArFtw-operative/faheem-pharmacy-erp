"""GST on purchases: rate resolution, CGST/SGST vs IGST, cost incl. GST, checks with fixes, the GST report."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Batch
from app.services import gst as G
from app.services import inventory_service as inv
from app.services import purchase_service, purchasing, report_generator, settings_service

HEAD = "Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,GST %,HSN,Amount\n"


def _sup(db, gstin=""):
    from app.models import Supplier
    name = f"GST Distributors {gstin[:2] or 'local'}"
    return db.query(Supplier).filter(Supplier.name == name).first() or purchasing.save_supplier(db, {"name": name, "gst_number": gstin})


def _draft(db, *rows, head=HEAD, sup=None, when=date(2026, 9, 20), no="G-1"):
    sup = sup or _sup(db)
    p = purchasing.create_from_file(db, "g.csv", (head + "\n".join(rows) + "\n").encode(), supplier_id=sup.id,
                                    invoice_no=no, invoice_date=when)
    db.flush()
    return p


def _dolo(db):
    return inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)


def _today(db):
    from datetime import datetime
    return datetime.now(report_generator.tz_for(db)).date().isoformat()     # the shop's day


def codes(line):
    return {i["code"] for i in line.issues if not i.get("accepted")}


def test_gstin_check_digit_and_supply_type():
    assert G.gstin_problem("27AAPFU0939F1ZV") == "" and G.gstin_problem("29AAGCB7383J1Z4") == ""
    assert "check digit" in G.gstin_problem("27AAPFU0939F1ZX")
    assert G.supply_type("36AAPFU0939F1ZV", "27AAPFU0939F1ZV")[0] == "INTER"
    assert G.supply_type("36AAPFU0939F1ZV", "36AAGCB7383J1Z4")[0] == "INTRA"
    mode, why = G.supply_type("", "")
    assert mode == "INTRA" and "assumed" in why


def test_rate_incl_gst_is_the_stock_cost_and_halves_add_up(db):
    item = _dolo(db)
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,3,1,24.99,33.60,5,30049069,74.97")
    line = p.items[0]
    assert line.status == "READY"
    assert (line.taxable_value, line.gst_amount) == (Decimal("74.97"), Decimal("3.75"))
    assert line.cgst_amount + line.sgst_amount == line.gst_amount and line.igst_amount == 0   # 1.88 + 1.87
    assert line.landed_total == Decimal("78.72") and line.landed_rate == Decimal("19.68")        # 78.72 / 4 strips (3 + 1 free)
    purchasing.post(db, p)
    db.flush()
    b = db.query(Batch).filter(Batch.item_id == item.id).one()
    assert b.purchase_rate == Decimal("19.68") and b.rate_basis == "INCL_GST"


def test_cost_before_gst_when_the_shop_claims_input_credit(db):
    item = _dolo(db)
    settings_service.set_setting(db, "purchase_cost_includes_gst", "false")
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,5,30049069,100")
    purchasing.post(db, p)
    db.flush()
    b = db.query(Batch).filter(Batch.item_id == item.id).one()
    assert b.purchase_rate == Decimal("25.00") and b.rate_basis == "EXCL_GST"


def test_inter_state_supplier_pays_igst(db):
    _dolo(db)
    settings_service.set_setting(db, "gst_number", "36AAPFU0939F1ZV")
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,5,30049069,100", sup=_sup(db, "27AAPFU0939F1ZV"))
    line = p.items[0]
    assert p.supply_type == "INTER" and line.igst_amount == Decimal("5.00") and line.cgst_amount == 0
    assert "IGST" in purchasing.gst_summary(db, p)["mode_reason"]


def test_bill_discount_is_taken_before_gst(db):
    _dolo(db)
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,10,,10,15,5,30049069,100")
    purchasing.update_header(db, p, {"charges": {"bill_discount": "10"}})
    line = p.items[0]
    assert (line.taxable_value, line.gst_amount, line.landed_total) == (Decimal("90.00"), Decimal("4.50"), Decimal("94.50"))
    assert purchasing.totals(p)["gst"] == "4.50"


def test_missing_gst_is_never_silently_zero(db):
    _dolo(db)
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,5,30049069,100",
               "D1,DOLO 650MG TAB,15S,DB2,May-2028,4,,25,33.6,,30049069,100")
    missing = sorted(p.items, key=lambda l: l.line_no)[1]
    assert missing.status == "NEEDS_REVIEW" and "gst_missing" in codes(missing)
    assert "How to fix" in next(i["message"] for i in missing.issues if i["code"] == "gst_missing")
    # a file without any GST column: taken as 0%, said so, not blocked
    plain = _draft(db, "D1,DOLO 650MG TAB,15S,DB3,May-2028,4,,25,33.6,100",
                   head="Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,Amount\n", no="G-2")
    line = plain.items[0]
    assert line.status == "READY" and "gst_not_shown" in codes(line) and line.gst_source == "NONE"


def test_gst_percent_is_worked_out_from_a_gst_amount_column(db):
    _dolo(db)
    head = "Product Code,Product Name,Pack,Batch,Expiry,Qty,Rate,MRP,Tax Amt,Amount\n"
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,25,33.6,5.00,100", head=head)
    line = p.items[0]
    assert line.gst_rate == Decimal("5") and line.gst_source == "DERIVED" and line.status == "READY"


def test_withdrawn_slab_after_the_rate_change_says_exactly_what_to_do(db):
    _dolo(db)
    after = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,12,30049069,100", when=date(2026, 9, 20))
    msg = next(i["message"] for i in after.items[0].issues if i["code"] == "gst_slab")
    assert "old slab" in msg and "moved to 5%" in msg and "How to fix" in msg
    before = _draft(db, "D1,DOLO 650MG TAB,15S,DB2,May-2028,4,,25,33.6,12,30049069,100", when=date(2025, 8, 1), no="G-OLD")
    assert "gst_slab" not in codes(before.items[0])
    # the slabs are settings, not code
    settings_service.set_setting(db, "gst_rates", "0,5,12,18,40")
    again = _draft(db, "D1,DOLO 650MG TAB,15S,DB3,May-2028,4,,25,33.6,12,30049069,100", no="G-3")
    assert "gst_slab" not in codes(again.items[0])


def test_gst_change_against_the_products_last_purchase_and_mixed_hsn(db):
    _dolo(db)
    first = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,5,30049069,100", no="G-1")
    purchasing.post(db, first)
    db.flush()
    second = _draft(db, "D1,DOLO 650MG TAB,15S,DB2,May-2028,4,,25,33.6,18,30049069,100",
                    "D1,DOLO 650MG TAB,15S,DB3,May-2028,4,,25,33.6,5,30049069,100", no="G-2")
    a, b = sorted(second.items, key=lambda l: l.line_no)
    assert "gst_changed" in codes(a) and "gst_hsn_mixed" in (codes(a) | codes(b))


def test_printed_invoice_gst_is_reconciled(db):
    _dolo(db)
    head = "Invoice No,Product Name,Batch,Expiry,Qty,Rate,MRP,GST %,SumGst,Amount\n"
    ok = _draft(db, "A1,DOLO 650MG TAB,DB1,May-2028,4,25,33.6,5,5.00,100", head=head, no="")
    assert purchasing.gst_summary(db, ok)["printed"] == "5.00" and not any("prints total GST" in x for x in purchasing.gst_summary(db, ok)["problems"])
    bad = _draft(db, "A2,DOLO 650MG TAB,DB2,May-2028,4,25,33.6,5,9.00,100", head=head, no="")
    problem = next(x for x in purchasing.gst_summary(db, bad)["problems"] if "prints total GST" in x)
    assert "₹9.00" in problem and "₹5.00" in problem and "How to fix" in problem


def test_purchase_gst_report_views_and_returns_reverse_gst(db):
    item = _dolo(db)
    syp = inv.create_item(db, name="COUGH SYP", pack_size="100ML", base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    p = _draft(db, "D1,DOLO 650MG TAB,15S,DB1,May-2028,4,,25,33.6,5,30049069,100",
               "C1,COUGH SYP,100ML,CS1,May-2028,2,,100,150,18,30049011,200")
    purchasing.post(db, p)
    db.flush()
    b = db.query(Batch).filter(Batch.item_id == item.id).one()
    purchase_service.create_return(db, item_id=item.id, batch_id=b.id, quantity=15, value=b.purchase_rate, purchase_id=p.id,
                                   supplier_id=p.supplier_id, reason="damaged")
    db.flush()
    rep = report_generator.generate(db, "purchase-gst", {"period": "custom", "from": "2026-01-01", "to": _today(db)},
                                    ["item", "gst_rate", "taxable", "cgst", "sgst", "igst", "gst", "landed", "rate_incl", "note"])
    t = rep["totals"]
    assert t["gst"] == Decimal("5.00") + Decimal("36.00") - Decimal("1.25")          # one strip of DOLO returned
    assert any("returned" in r["note"] for r in rep["rows"])
    by_rate = report_generator.generate(db, "purchase-gst", {"period": "custom", "from": "2026-01-01", "to": _today(db), "gst_view": "rate"},
                                        ["gst_rate", "taxable", "gst", "lines"])
    assert [r["gst_rate"] for r in by_rate["rows"]] == ["5%", "18%"]
    only18 = report_generator.generate(db, "purchase-gst", {"period": "custom", "from": "2026-01-01", "to": _today(db), "gst_rate": "18"}, ["item", "gst"])
    assert [r["item"] for r in only18["rows"]] == ["COUGH SYP"]


def test_batches_received_before_gst_tracking_are_never_recosted(db):
    item = _dolo(db)
    old = inv.add_or_update_batch(db, item, batch_no="OLD", quantity=1, unit="PACK", movement_type="OPENING_STOCK", mrp="30", purchase_rate="20")
    db.flush()
    p = _draft(db, "D1,DOLO 650MG TAB,15S,NEW1,May-2028,4,,25,33.6,5,30049069,100")
    purchasing.post(db, p)
    db.flush()
    db.refresh(old)
    assert old.purchase_rate == Decimal("20.00") and old.rate_basis == ""
