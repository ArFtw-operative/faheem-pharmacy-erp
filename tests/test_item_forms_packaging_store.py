"""Item form master (built-in + the pharmacy's own) and the ProductPackaging store."""
from __future__ import annotations

import pytest

from app.models import ItemForm, MappingHistory, ProductPackaging
from app.services import form_service, packaging_store, purchase_adjustment as physical, purchasing, units
from app.services import inventory_service as inv
from tests.conftest import login
from tests.test_purchase_automation import enable
from tests.test_purchasing import draft


def test_builtin_forms_cover_medicines_liquids_injectables_and_devices(db):
    form_service.seed(db)
    codes = {f["code"] for f in form_service.listing(db)}
    for code in ("TABLET", "CAPSULE", "SOFTGEL", "SYRUP", "DROPS", "CREAM", "GEL", "OINTMENT", "INJECTION", "VIAL",
                 "AMPOULE", "IV_FLUID", "SACHET", "POWDER", "SYRINGE", "NEEDLE", "CANNULA", "DEVICE", "INHALER"):
        assert code in codes
    assert form_service.seed(db) == 0          # idempotent


def test_new_form_is_created_and_usable_as_a_dosage_form(db):
    form_service.seed(db)
    made = form_service.create(db, name="Mouthwash bottles", base_unit="BOTTLE", pack_unit="BOTTLE", content_unit="ML")
    assert made["code"] == "MOUTHWASH_BOTTLES" and not made["builtin"] and made["dosage_form"] == "MOUTHWASH_BOTTLES"
    assert "MOUTHWASH_BOTTLES" in units.known_forms()
    item = inv.create_item(db, name="LISTERINE 250ML", pack_size="250ML", dosage_form="MOUTHWASH_BOTTLES",
                           base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    assert item.dosage_form == "MOUTHWASH_BOTTLES"
    with pytest.raises(form_service.FormError, match="already exists"):
        form_service.create(db, name="mouthwash bottles", base_unit="BOTTLE")


def test_counted_custom_form_drives_the_package_editor(db):
    enable(db)
    form_service.seed(db)
    form_service.create(db, name="Insulin pen needles", base_unit="PIECE", pack_unit="BOX", counted=True)
    p = draft(db, ",NOVOFINE NEEDLE,1X100,B1,May-2028,1,,500,900,500")
    physical.adjust(db, p, p.items[0], dict(form="INSULIN_PEN_NEEDLES", units_per_pack="100", quantity="1", free="0"))
    d = p.items[0].receipt_decision
    assert d["resolved"] and d["received_base_units"] == 100 and d["base_unit"] == "PIECE"


def test_whole_container_form_refuses_a_count(db):
    enable(db)
    form_service.seed(db)
    p = draft(db, ",X SYRUP,60ML,B1,May-2028,1,,50,90,50")
    with pytest.raises(purchasing.PurchaseError, match="one unit per container"):
        physical.adjust(db, p, p.items[0], dict(form="SYRUP", units_per_pack="10", quantity="1", free="0"))


def test_hidden_form_is_not_offered(db):
    form_service.seed(db)
    form_service.set_active(db, "PAIR", False)
    assert "PAIR" not in {f["code"] for f in form_service.listing(db)}
    assert "PAIR" in {f["code"] for f in form_service.listing(db, include_inactive=True)}


def test_item_forms_api(client, db):
    login(client)
    r = client.get("/api/erp/item-forms")
    assert r.status_code == 200 and any(f["code"] == "IV_FLUID" for f in r.json()["forms"])
    r = client.post("/api/erp/item-forms", json={"name": "Nasal sprays", "base_unit": "BOTTLE", "pack_unit": "BOTTLE", "content_unit": "ML"})
    assert r.status_code == 200, r.text
    assert r.json()["form"]["code"] == "NASAL_SPRAYS"
    assert client.post("/api/erp/item-forms", json={"name": "", "base_unit": "BOTTLE"}).status_code == 400
    assert client.put("/api/erp/item-forms/NASAL_SPRAYS", json={"active": False}).status_code == 200
    page = client.get("/app").text
    assert "item_forms" in page


def test_bootstrap_creates_packaging_only_without_guessing(db):
    good = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    syrup = inv.create_item(db, name="OKACET SYRUP", pack_size="60ML", base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    vague = inv.create_item(db, name="C-PILL 72 TAB", pack_size="1", base_unit="TABLET", pack_unit="STRIP", units_per_pack=1)
    clash = inv.create_item(db, name="EXAMPLE TAB", pack_size="10S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)
    out = packaging_store.bootstrap(db)
    made = {r.item_id: r for r in db.query(ProductPackaging)}
    assert good.id in made and made[good.id].retail_to_base == 15 and made[good.id].is_preferred
    assert syrup.id in made and made[syrup.id].container_size == 60 and not made[syrup.id].allow_loose_sale
    assert vague.id not in made and clash.id not in made
    reasons = {s["item_id"]: s["reason"] for s in out["skipped"]}
    assert "bare '1'" in reasons[vague.id] and "product says 15" in reasons[clash.id]
    assert packaging_store.bootstrap(db)["created"] == 0          # idempotent
    assert db.query(MappingHistory).filter_by(kind="PACKAGING").count() == 2


def test_a_product_keeps_several_packs_with_one_preferred(db):
    from app.services import packaging_parser as pp
    item = inv.create_item(db, name="X TAB", pack_size="10S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=10, dosage_form="TABLET")
    packaging_store.bootstrap(db)
    packaging_store.upsert(db, item, pp.parse("10X10", master_form="TABLET"), source="USER_CORRECTION", verified=True,
                           purchase_to_retail=10, retail_to_base=10, retail_unit="STRIP", base_unit="TABLET", purchase_unit="BOX",
                           preferred=True)
    rows = packaging_store.for_item(db, item.id)
    assert len(rows) == 2 and sum(r.is_preferred for r in rows) == 1 and rows[0].normalized_packing == "10X10"
