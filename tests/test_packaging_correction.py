"""Correction dialog: invoice-only, product packaging and supplier alias scopes."""
from __future__ import annotations

from app.models import ProductPackaging, SupplierPackagingAlias
from app.routers.purchases import _line_view
from app.services import inventory_service as inv
from app.services import packaging_correction as pcx
from app.services import purchasing
from tests.conftest import login
from tests.test_purchase_automation import enable
from tests.test_purchasing import draft, supplier


def cap(db):
    return inv.create_item(db, name="EXAMPLE CAP", pack_size="10x1x10", base_unit="CAPSULE", pack_unit="STRIP",
                           units_per_pack=10, dosage_form="CAPSULE")


def test_proposal_reads_the_pack_and_the_product(db):
    enable(db)
    cap(db)
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,,10,20,20")
    prop = pcx.proposal(p.items[0])
    assert (prop["units_per_retail"], prop["retail_per_outer"], prop["base_unit"]) == (10, 10, "CAPSULE")
    assert prop["reading"]["confidence"] == "HIGH"


def test_save_for_this_invoice_does_not_teach_the_supplier(db):
    enable(db)
    sup = supplier(db)
    cap(db)
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,,10,20,20", sup=sup)
    line = p.items[0]
    pcx.apply(db, p, line, {"form": "CAPSULE", "units_per_retail": "10", "retail_per_outer": "10", "level": "RETAIL", "scope": "invoice"})
    assert line.receipt_decision["resolved"] and line.receipt_decision["received_base_units"] == 20
    purchasing.post(db, p)
    assert db.query(SupplierPackagingAlias).count() == 0


def test_save_supplier_alias_resolves_the_next_invoice_immediately(db):
    enable(db)
    sup = supplier(db)
    item = cap(db)
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,1,10,20,20", sup=sup)
    line = p.items[0]
    pcx.apply(db, p, line, {"form": "CAPSULE", "units_per_retail": "10", "retail_per_outer": "10", "level": "OUTER",
                            "scope": "supplier", "reason": "Carton holds 10 strips"})
    view = _line_view(line)["stock"]
    assert (view["stock"], view["equivalent"]) == ("3 boxes", "30 strips, 300 capsules")
    alias = db.query(SupplierPackagingAlias).one()
    assert alias.units_per_invoice_unit == 100 and alias.item_id == item.id
    assert db.query(ProductPackaging).filter_by(item_id=item.id, purchase_to_retail=10, verified_by_user=True).count() == 1


def test_choosing_a_form_for_a_new_product_defines_it(db):
    enable(db)
    p = draft(db, ",NEW SYRINGE 5ML,1X100,B1,May-2028,1,,500,900,500")
    line = p.items[0]
    pcx.apply(db, p, line, {"form": "SYRINGE", "units_per_retail": "100", "retail_per_outer": "1", "level": "RETAIL", "scope": "product"})
    assert line.new_product and line.base_unit == "PIECE" and line.units_per_pack == 100
    assert line.receipt_decision["received_base_units"] == 100


def test_packaging_api(client, db):
    login(client)
    enable(db)
    cap(db)
    p = draft(db, ",EXAMPLE CAP,10x1x10,B1,May-2028,2,,10,20,20")
    db.commit()
    lid = p.items[0].id
    assert client.get(f"/api/erp/purchases/{p.id}/lines/{lid}/packaging").json()["units_per_retail"] == 10
    r = client.put(f"/api/erp/purchases/{p.id}/lines/{lid}/packaging",
                   json={"form": "CAPSULE", "units_per_retail": 10, "retail_per_outer": 10, "level": "RETAIL", "scope": "invoice"})
    assert r.status_code == 200, r.text
    line = r.json()["lines"][0]
    assert line["stock"]["equivalent"] == "20 capsules" and "1 box = 10 strips" in line["stock"]["hierarchy"]
