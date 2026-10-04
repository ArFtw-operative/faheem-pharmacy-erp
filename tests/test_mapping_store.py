"""Mapping store: aliases with trust, overrules, packaging aliases, bootstrap from history."""
from __future__ import annotations

from app.models import MappingHistory, ProductPackaging, SupplierPackagingAlias, SupplierProductMap
from app.services import inventory_service as inv
from app.services import mapping_store, purchasing, receipt_decision
from tests.test_purchase_automation import enable
from tests.test_purchasing import draft, supplier
from tests.conftest import certain


def cap(db, name="EXAMPLE CAP"):
    return inv.create_item(db, name=name, pack_size="10x1x10", base_unit="CAPSULE", pack_unit="STRIP",
                           units_per_pack=10, dosage_form="CAPSULE")


def test_confirmed_invoice_unit_becomes_a_packaging_alias_and_resolves_next_invoice(db):
    enable(db)
    sup = supplier(db)
    item = cap(db)
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,,10,20,20", sup=sup)
    line = p.items[0]
    assert not certain(line.receipt_decision)           # 10x1x10 alone does not say strips or boxes
    receipt_decision.confirm(db, p, line, factor=10, mrp_basis="MASTER_PACK", reason="Supplier bills strips of ten")
    purchasing.post(db, p)
    alias = db.query(SupplierPackagingAlias).one()
    assert (alias.units_per_invoice_unit, alias.retail_units, alias.status) == (10, 10, "ACTIVE")
    assert db.query(ProductPackaging).filter_by(item_id=item.id, source="USER_CORRECTION").count() == 1
    # the next invoice with the same printed pack resolves with no review — even in a new column layout
    nxt = purchasing.create_from_file(db, "next.csv", "Item,Pack,Batch,Expiry,Qty,Rate,MRP\nEXAMPLE CAP,10X1X10,B2,Jun-2028,3,10,20\n".encode(),
                                      supplier_id=sup.id, invoice_no="INV-2")
    d = nxt.items[0].receipt_decision
    assert d["resolved"] and d["source"] == "SUPPLIER_PACKING_ALIAS" and d["received_base_units"] == 30


def test_packaging_alias_is_ignored_once_the_product_pack_changes(db):
    enable(db)
    sup = supplier(db)
    item = cap(db)
    mapping_store.save_packaging_alias(db, supplier_id=sup.id, pack="10x1x10", item=item, units_per_invoice_unit=10,
                                       mrp_basis="MASTER_PACK", source="USER_CORRECTION")
    item.units_per_pack = 15
    db.flush()
    assert mapping_store.packaging_alias(db, sup.id, "10X1X10", item) is None


def test_overruled_product_alias_is_suggested_but_not_applied(db):
    sup = supplier(db)
    wrong = cap(db, "WRONG CAP")
    right = cap(db, "RIGHT CAP")
    db.add(SupplierProductMap(supplier_id=sup.id, supplier_code="", description_key=purchasing.description_key("SHORT NAME CAP"),
                              description_raw="SHORT NAME CAP", item_id=wrong.id, status="ACTIVE"))
    db.flush()
    p = draft(db, ",SHORT NAME CAP,10x1x10,B1,May-2028,2,,10,20,20", sup=sup)
    line = p.items[0]
    assert line.item_id == wrong.id and line.match_method == "SUPPLIER_MAP"
    purchasing.correct(db, p, line, {"item_id": right.id})
    receipt_decision.confirm(db, p, line, factor=10, mrp_basis="MASTER_PACK", reason="Checked the carton")
    purchasing.post(db, p)
    m = db.query(SupplierProductMap).one()
    assert (m.item_id, m.status, m.corrections) == (right.id, "AMBIGUOUS", 1)
    assert db.query(MappingHistory).filter_by(kind="PRODUCT_ALIAS", action="OVERRULE").count() == 1
    # next invoice: the overruled alias is not applied automatically
    nxt = purchasing.create_from_file(db, "n.csv", "Item,Pack,Batch,Expiry,Qty,Rate,MRP\nSHORT NAME CAP,10x1x10,B9,Jun-2028,1,10,20\n".encode(),
                                      supplier_id=sup.id, invoice_no="INV-3")
    assert nxt.items[0].item_id is None and nxt.items[0].status == "PRODUCT_MATCH_REQUIRED"
    # confirming the same product again restores it
    purchasing.correct(db, nxt, nxt.items[0], {"item_id": right.id})
    purchasing.post(db, nxt)
    db.refresh(m)
    assert m.status == "ACTIVE"


def test_bootstrap_creates_aliases_only_for_unambiguous_history(db):
    sup = supplier(db)
    a, b = cap(db, "ALPHA CAP"), cap(db, "BETA CAP")
    p = draft(db, ",ALPHA CAP,10x1x10,B1,May-2028,1,,10,20,10", ",GAMMA CAP,10x1x10,B2,May-2028,1,,10,20,10", sup=sup)
    for line, item in zip(p.items, (a, b)):
        purchasing.correct(db, p, line, {"item_id": item.id})
        receipt_decision.confirm(db, p, line, factor=10, mrp_basis="MASTER_PACK", reason="Verified on the carton")
    purchasing.post(db, p)
    db.query(SupplierProductMap).delete()
    db.query(SupplierPackagingAlias).delete()
    db.flush()
    out = mapping_store.bootstrap(db)
    assert out["created"] == 2 and not out["ambiguous"]
    assert {m.item_id for m in db.query(SupplierProductMap)} == {a.id, b.id}
    assert out["packaging_aliases"] == 2
    assert mapping_store.bootstrap(db)["created"] == 0          # idempotent


def test_bootstrap_lists_conflicting_history(db):
    sup = supplier(db)
    a, b = cap(db, "ALPHA CAP"), cap(db, "BETA CAP")
    for n, item in enumerate((a, b)):
        p = purchasing.create_from_file(db, f"{n}.csv", f"Item,Pack,Batch,Expiry,Qty,Rate,MRP\nODD NAME CAP,10x1x10,B{n},May-2028,1,10,20\n".encode(),
                                        supplier_id=sup.id, invoice_no=f"INV-{n}")
        purchasing.correct(db, p, p.items[0], {"item_id": item.id})
        receipt_decision.confirm(db, p, p.items[0], factor=10, mrp_basis="MASTER_PACK", reason="Verified on the carton")
        purchasing.post(db, p)
    for m in db.query(SupplierProductMap):
        m.status = None
    db.flush()
    out = mapping_store.bootstrap(db)
    assert len(out["ambiguous"]) == 1 and set(out["ambiguous"][0]["items"]) == {a.id, b.id}
    assert db.query(SupplierProductMap).one().status == "AMBIGUOUS"


def test_column_role_correction_is_recorded(db, tmp_path):
    sup = supplier(db)
    content = "Item,Pack,Lot,Expiry,Qty,Rate,MRP\nDOLO 650MG TAB,15S,B1,May-2028,1,24,33.6\n".encode()
    p = purchasing.create_from_file(db, "c.csv", content, supplier_id=sup.id, invoice_no="C-1")
    purchasing.remap(db, p, {"Lot": "batch"})
    assert db.query(MappingHistory).filter_by(kind="COLUMN_ROLE").count() == 1
