"""Regression: received purchase quantity is independent of pack interpretation.

After editable package quantity was added (e894f7b) lines with an unresolved pack
showed "Quantity not confirmed" and "Equivalent: —" although billed + free were known.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.models import Batch, SupplierProductMap
from app.routers.purchases import _line_view
from app.services import inventory_service as inv
from app.services import packaging_conversion as pc
from app.services import purchase_adjustment as physical
from app.services import purchasing
from tests.test_purchase_automation import enable
from tests.test_purchasing import csv_bytes, draft, supplier


def test_billed_quantity_stays_known_when_packaging_is_unknown(db):
    p = draft(db, ",MYSTERY BRAND,10ML57,B1,May-2028,2,1,10,20,20")
    view = _line_view(p.items[0])
    assert not p.items[0].receipt_decision["resolved"]
    assert view["stock"]["stock"] == "3 purchase packs"
    assert view["stock"]["equivalent"] == "Pack conversion unresolved"
    assert "not confirmed" not in str(view["stock"]).lower()


def test_unknown_pack_with_billed_quantity_never_says_not_confirmed(db):
    p = draft(db, ",ALPHA,10X1,B1,May-2028,2,,10,20,20", ",BETA,5ml+,B2,May-2028,4,1,10,20,40",
              ",GAMMA,,B3,May-2028,1,,10,20,10")
    for line in p.items:
        view = _line_view(line)["stock"]
        assert view["stock"].endswith(("purchase pack", "purchase packs")) or view["resolved"]
        assert view["quantity"] != ""


def test_changing_package_quantity_does_not_invalidate_the_product_mapping(db):
    enable(db)
    sup = supplier(db)
    item = inv.create_item(db, name="EXAMPLE CAP", pack_size="10x1x10", base_unit="CAPSULE", pack_unit="STRIP",
                           units_per_pack=10, dosage_form="CAPSULE")
    db.add(SupplierProductMap(supplier_id=sup.id, supplier_code="", description_key=purchasing.description_key("EXAMPLE CAP"),
                              description_raw="EXAMPLE CAP", item_id=item.id))
    db.flush()
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,,10,20,20", sup=sup)
    line = p.items[0]
    assert line.item_id == item.id and line.match_method == "SUPPLIER_MAP"
    physical.adjust(db, p, line, dict(form="CAPSULE", units_per_pack="10", quantity="2", free="0"))
    assert line.item_id == item.id
    assert db.query(SupplierProductMap).filter_by(item_id=item.id).count() == 1


def test_editing_packaging_recalculates_only_the_equivalent(db):
    enable(db)
    p = draft(db, ",NEW BRAND,10x1x15,B,May-2028,2,1,10,20,20")
    line = p.items[0]
    before = _line_view(line)["stock"]
    assert before["stock"] == "3 purchase packs"
    physical.adjust(db, p, line, dict(form="TABLET", units_per_pack="15", quantity="2", free="1"))
    after = _line_view(line)["stock"]
    assert after["quantity"] == before["quantity"] == "3"
    assert after["stock"] == "3 strips" and after["equivalent"] == "45 tablets"


def test_correcting_one_row_does_not_push_unrelated_rows_into_review(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00",
              ",UNKNOWN THING,10ML57,X1,May-2028,1,,10,20,10")
    good, odd = p.items
    assert good.status == "READY" and good.item_id == item.id
    purchasing.correct(db, p, odd, {"pack": "10ML"})
    assert good.status == "READY"
    assert _line_view(good)["stock"]["equivalent"] == "180 tablets"


def test_saved_supplier_aliases_survive_packaging_edits(db):
    enable(db)
    sup = supplier(db)
    item = inv.create_item(db, name="EXAMPLE BRAND", pack_size="15", base_unit="PACK", pack_unit="PACK", units_per_pack=1)
    alias = SupplierProductMap(supplier_id=sup.id, supplier_code="", description_key=purchasing.description_key("EXAMPLE BRAND"),
                               description_raw="EXAMPLE BRAND", item_id=item.id)
    db.add(alias)
    db.flush()
    p = draft(db, ",EXAMPLE BRAND,15,B,May-2028,2,,10,20,20", sup=sup)
    physical.adjust(db, p, p.items[0], dict(form="TABLET", units_per_pack="15", quantity="2", free="0"))
    purchasing.post(db, p)
    db.refresh(alias)
    assert alias.item_id == item.id and alias.uses == 1


def test_reimporting_the_same_file_does_not_double_stock(db):
    from app.services import purchase_automation

    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    sup = supplier(db)
    content = csv_bytes("D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    p = purchasing.create_from_file(db, "inv.csv", content, supplier_id=sup.id, invoice_no="INV-9",
                                    invoice_date=date(2026, 9, 1))
    purchasing.post(db, p)
    assert sum(b.quantity for b in db.query(Batch).filter_by(item_id=item.id)) == 180
    with pytest.raises(purchasing.PurchaseError) as exc:
        purchasing.create_from_file(db, "inv.csv", content, supplier_id=sup.id)
    assert exc.value.code == "DUPLICATE_FILE"
    enable(db, "post")
    again = purchase_automation.import_file(db, "copy.csv", content, supplier_id=sup.id)
    assert [d.id for d in again] == [p.id]
    assert sum(b.quantity for b in db.query(Batch).filter_by(item_id=item.id)) == 180


@pytest.mark.parametrize("decision,definition,pack,expected", [
    # MULTIPREX CAP 10X1X10 billed 2 as outer packs: 2 outer packs → 20 strips, 200 capsules
    (dict(resolved=True, paid="2", free="0", units_per_invoice_unit=100, received_base_units=200),
     dict(base_unit="CAPSULE", pack_unit="STRIP", units_per_pack=10), "10X1X10", ("2 boxes", "20 strips, 200 capsules")),
    # OKACET SYRUP 60ML billed 2
    (dict(resolved=True, paid="2", free="0", units_per_invoice_unit=1, received_base_units=2),
     dict(base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1), "60ML", ("2 bottles", "2 × 60 mL")),
    # OMNIGEL 10 GMS billed 3
    (dict(resolved=True, paid="3", free="0", units_per_invoice_unit=1, received_base_units=3),
     dict(base_unit="TUBE", pack_unit="TUBE", units_per_pack=1), "10GMS", ("3 tubes", "3 × 10 g")),
    # IV fluid 500ML billed 4
    (dict(resolved=True, paid="4", free="0", units_per_invoice_unit=1, received_base_units=4),
     dict(base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1), "500ML", ("4 bottles", "4 × 500 mL")),
    # Cannula 1X100 billed 1
    (dict(resolved=True, paid="1", free="0", units_per_invoice_unit=100, received_base_units=100),
     dict(base_unit="PIECE", pack_unit="BOX", units_per_pack=100), "1X100", ("1 box", "100 pieces")),
    # Device, packing 1, billed 3
    (dict(resolved=True, paid="3", free="0", units_per_invoice_unit=1, received_base_units=3),
     dict(base_unit="PIECE", pack_unit="PIECE", units_per_pack=1), "1", ("3 pieces", "3 pieces")),
    # PARACIP-500 20X10 billed 5 + free 1 as boxes
    (dict(resolved=True, paid="5", free="1", units_per_invoice_unit=200, received_base_units=1200),
     dict(base_unit="TABLET", pack_unit="STRIP", units_per_pack=10), "20X10", ("6 boxes", "120 strips, 1,200 tablets")),
    # billed 2 + free 1, packing unresolved
    (dict(resolved=False, paid="2", free="1"),
     dict(base_unit="UNIT", pack_unit="PACK", units_per_pack=None), "10ML57", ("3 purchase packs", "Pack conversion unresolved")),
])
def test_acceptance_examples_received_stock_and_equivalent(decision, definition, pack, expected):
    view = pc.receipt_view(decision, definition, pack_text=pack)
    assert (view["stock"], view["equivalent"]) == expected
