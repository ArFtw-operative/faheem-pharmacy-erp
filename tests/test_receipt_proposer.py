"""Every line counted: weighed proposals, plausibility, restock reconciliation, variants, learning."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Batch, SupplierPackagingAlias
from app.services import confidence_gate as gate
from app.services import inventory_service as inv
from app.services import purchase_automation as auto
from app.services import purchasing, receipt_decision as rd, receipt_proposer, settings_service, stock_ledger
from tests.conftest import certain
from tests.test_purchase_automation import enable
from tests.test_purchasing import draft, supplier


def tab(db, name="EXAMPLE 10MG TAB", upp=10, mrp="50"):
    return inv.create_item(db, name=name, pack_size=f"{upp}S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=upp,
                           dosage_form="TABLET", mrp=Decimal(mrp))


def history(db, sup, n=3, factor_of=lambda upp: upp):
    """Posted, person-confirmed nested-pack lines of this supplier."""
    for k in range(n):
        item = tab(db, name=f"HIST {k}MG TAB")
        p = draft(db, f",HIST {k}MG TAB,20X10,H{k},May-2028,2,,10,50,20", sup=sup, name=f"h{k}.csv")
        p.invoice_no = f"H-{k}"
        rd.confirm(db, p, p.items[0], factor=factor_of(item.units_per_pack), mrp_basis="MASTER_PACK", reason="Checked the carton")
        purchasing.post(db, p)


def test_supplier_history_decides_retail_versus_box(db):
    enable(db)
    sup = supplier(db)
    history(db, sup, n=2)                    # three reviewed products would make it a certain convention
    tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,30X10,B1,May-2028,3,,30,50,90", sup=sup, name="n.csv")
    d = p.items[0].receipt_decision
    assert d["resolved"] and d["source"] == "PROPOSED" and d["units_per_invoice_unit"] == 10 and d["received_base_units"] == 30
    assert any("billed the retail pack" in e for e in d["evidence"])
    assert p.items[0].status == "READY" and gate.assess(p.items[0])["state"] == gate.WARNING


def test_box_billing_supplier_is_learned_too(db):
    enable(db)
    sup = supplier(db)
    history(db, sup, n=2, factor_of=lambda upp: upp * 20)                # this supplier bills whole boxes
    tab(db, mrp="50")
    p = draft(db, ",EXAMPLE 10MG TAB,20X10,B1,May-2028,1,,600,1000,600", sup=sup, name="n.csv")
    d = p.items[0].receipt_decision
    assert d["units_per_invoice_unit"] == 200 and d["received_base_units"] == 200


def test_product_mrp_is_evidence_without_history(db):
    enable(db)
    tab(db, mrp="50")
    p = draft(db, ",EXAMPLE 10MG TAB,20X10,B1,May-2028,2,,30,50,60")      # invoice MRP = the strip's MRP
    d = p.items[0].receipt_decision
    assert d["units_per_invoice_unit"] == 10
    assert any("matches the product's MRP" in e for e in d["evidence"])


def test_catalogue_pack_counts_in_its_own_unit(db):
    enable(db)
    inv.create_item(db, name="GENERIC BRAND", pack_size="12", base_unit="PACK", pack_unit="PACK", units_per_pack=1)
    p = draft(db, ",GENERIC BRAND,12,B,May-2028,2,,10,20,20")
    d = p.items[0].receipt_decision
    assert d["received_base_units"] == 2 and d["source"] == "PROPOSED"
    assert "whole packs" in d["evidence"][0]


def test_proposals_are_never_posted_unattended_but_a_person_posts_them(db):
    enable(db)
    tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,20X10,B1,May-2028,2,,30,50,60")
    a = auto.assessment(db, p)
    assert not a["ready_for_unattended_post"] and "count_proposed" in a["exceptions"][0]["codes"]
    purchasing.post(db, p)
    assert sum(b.quantity for b in db.query(Batch)) == 20


def test_a_posted_proposal_is_learned_and_a_second_posting_trusts_it(db):
    enable(db)
    sup = supplier(db)
    item = tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,20X10,B1,May-2028,2,,30,50,60", sup=sup, name="a.csv")
    purchasing.post(db, p)
    alias = db.query(SupplierPackagingAlias).one()
    assert alias.units_per_invoice_unit == 10 and alias.trust == Decimal("0.900") and alias.source == "POSTED_PROPOSAL"
    p2 = draft(db, ",EXAMPLE 10MG TAB,20X10,B2,Jun-2028,1,,30,50,30", sup=sup, name="b.csv")
    p2.invoice_no = "INV-2"
    d = p2.items[0].receipt_decision
    assert d["source"] == "SUPPLIER_PACKING_ALIAS" and certain(d)
    assert gate.assess(p2.items[0])["fields"]["packaging"] == 0.95                 # learned once: still shown
    purchasing.post(db, p2)
    db.refresh(alias)
    assert alias.trust == Decimal("1.000")
    p3 = draft(db, ",EXAMPLE 10MG TAB,20X10,B3,Jul-2028,1,,30,50,30", sup=sup, name="c.csv")
    assert gate.assess(p3.items[0])["fields"]["packaging"] > 0.97 and item.id == p3.items[0].item_id


def test_implausibly_priced_count_needs_a_look(db):
    enable(db)
    for k in range(30):                                  # the pharmacy's tablets cost about ₹5 each
        b = inv.add_or_update_batch(db, tab(db, name=f"PRIOR {k} TAB", mrp="50"), batch_no=f"P{k}", quantity=10,
                                    unit="BASE", mrp=Decimal("50"))
    receipt_proposer._CACHE.clear()
    inv.create_item(db, name="ODD PACK TAB", pack_size="20X10", base_unit="TABLET", pack_unit="STRIP", units_per_pack=200,
                    dosage_form="TABLET")
    p = draft(db, ",ODD PACK TAB,20X10,B1,May-2028,1,,30,50,30")     # ₹50 for 200 tablets: ₹0.25 each
    line = p.items[0]
    issue = next(i for i in line.issues if i["code"] == "unit_price_unusual")
    assert "10 per pack would make it" in issue["message"]
    assert gate.assess(line)["state"] == gate.REVIEW
    with pytest.raises(purchasing.PurchaseError):
        purchasing.post(db, p, accept_warnings=True)     # posting does not wave it through in bulk


def test_strength_printed_as_pack_counts_strips(db):
    enable(db)
    p = draft(db, ",NEW TABLET,500MG,B,May-2028,2,,10,20,20")
    line = p.items[0]
    assert (line.base_unit, line.units_per_pack) == ("STRIP", 1) and line.receipt_decision["received_base_units"] == 2


def test_restock_of_a_batch_in_stock_keeps_the_lower_mrp_and_its_expiry(db):
    item = tab(db)
    inv.add_or_update_batch(db, item, batch_no="B1", expiry_date=date(2028, 5, 1), quantity=1, unit="PACK", mrp=Decimal("50"))
    p = draft(db, ",EXAMPLE 10MG TAB,10S,B1,Jun-2028,2,,30,55,60")      # same batch, new MRP and expiry
    line = p.items[0]
    note = next(i for i in line.issues if i["code"] == "restock_reconcile")
    assert "lower ₹50" in note["message"] and "keeps its expiry" in note["message"]
    purchasing.post(db, p)                                              # no error
    batch = db.query(Batch).one()
    assert batch.mrp == Decimal("50") and batch.expiry_date == date(2028, 5, 1) and batch.quantity == 30
    assert line.corrections["_batch_reconciled"]
    assert not stock_ledger.reconcile(db)


def test_restock_of_an_empty_batch_takes_the_new_mrp(db):
    item = tab(db)
    b = inv.add_or_update_batch(db, item, batch_no="B1", expiry_date=date(2028, 5, 1), quantity=1, unit="PACK", mrp=Decimal("50"))
    stock_ledger.post(db, b, "SALE", 10, reason="sold out")
    p = draft(db, ",EXAMPLE 10MG TAB,10S,B1,Jun-2028,2,,30,55,60")
    purchasing.post(db, p)
    db.refresh(b)
    assert b.mrp == Decimal("55") and b.expiry_date == date(2028, 6, 1) and b.quantity == 20


def test_posting_accepts_routine_warnings(db):
    tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,10S,B1,Nov-2026,2,,30,50,60")      # expires soon
    line = p.items[0]
    assert line.status == "NEEDS_REVIEW" and purchasing.warnings_only(line)
    assert purchasing.summary(p)["postable"]
    with pytest.raises(purchasing.PurchaseError):
        purchasing.post(db, p)
    purchasing.post(db, p, accept_warnings=True)
    assert line.status == "POSTED" and "expiry_soon" in line.corrections["_accepted"]


def test_a_different_size_becomes_its_own_product_and_is_reused(db):
    enable(db)
    sup = supplier(db)
    inv.create_item(db, name="EXAMPLE CREAM", pack_size="10GM", base_unit="TUBE", pack_unit="TUBE", units_per_pack=1,
                    dosage_form="CREAM", category="OTC")
    p = draft(db, ",EXAMPLE CREAM,15GM,B1,May-2028,2,,60,90,120", sup=sup, name="v1.csv")
    line = p.items[0]
    assert line.new_product and line.product_name == "EXAMPLE CREAM 15GM" and line.category == "OTC"
    assert any(i["code"] == "variant_proposed" for i in line.issues) and line.receipt_decision["received_base_units"] == 2
    purchasing.post(db, p)
    p2 = draft(db, ",EXAMPLE CREAM,15GM,B2,Jun-2028,1,,60,90,60", sup=sup, name="v2.csv")
    assert p2.items[0].item_id == line.item_id                            # the variant is reused, not re-created


def test_a_person_matching_the_original_product_wins(db):
    enable(db)
    item = inv.create_item(db, name="EXAMPLE CREAM", pack_size="10GM", base_unit="TUBE", pack_unit="TUBE", units_per_pack=1,
                           dosage_form="CREAM")
    p = draft(db, ",EXAMPLE CREAM,15GM,B1,May-2028,2,,60,90,120")
    purchasing.correct(db, p, p.items[0], {"item_id": item.id})
    assert p.items[0].item_id == item.id and "_variant" not in p.items[0].corrections


def test_scans_are_off_by_default_and_can_be_switched_on(db):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (40, 40), 255).save(buf, format="PNG")
    with pytest.raises(purchasing.PurchaseError, match="switched off"):
        purchasing.create_from_file(db, "bill.png", buf.getvalue(), supplier_id=supplier(db).id)
    settings_service.set_setting(db, "purchase_scan_import", "on")
    with pytest.raises(purchasing.PurchaseError) as exc:
        purchasing.create_from_file(db, "bill2.png", buf.getvalue())
    assert "switched off" not in str(exc.value)
