"""Unit-of-measure model: automatic detection, derived loose sale, repack conversion, pricing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Batch, InventoryMovement, Item
from app.services import inventory_service as inv
from app.services import packaging_service, sales_service, stock_ledger
from tests.conftest import login


def legacy_item(db, name="ADVASTAT 10 TAB", pack="10S", strips=3, mrp="200.00"):
    """A product as an old import left it: never configured, stock counted in whole packs."""
    item = Item(article_id=f"L{db.query(Item).count():05d}", name=name, pack_size=pack, base_unit="UNIT",
                pack_unit="PACK", units_per_pack=1, loose_sale=False, packaging_source="DEFAULT", dosage_form="")
    db.add(item)
    db.flush()
    batch = stock_ledger.resolve_batch(db, item, batch_no="FGF6079", expiry_date=date(2099, 5, 1), mrp=mrp,
                                       purchase_rate="150")
    if strips:
        stock_ledger.post(db, batch, "OPENING_STOCK", strips, reference_type="IMPORT")
    db.commit()
    return item


def uom(name, pack, generic=""):
    return packaging_service.resolve(Item(name=name, pack_size=pack, generic_name=generic, dosage_form="",
                                          base_unit="UNIT", pack_unit="PACK", units_per_pack=1, loose_sale=False))


@pytest.mark.parametrize("name,pack,expect", [
    ("ADVASTAT 10 TAB", "10S", ("TABLET", "STRIP", 10, True)),
    ("DOLO 650MG TABLETS", "15 S", ("TABLET", "STRIP", 15, True)),
    ("PANTOCID DSR CAP", "1X10", ("CAPSULE", "STRIP", 10, True)),
    ("ATARAX 10MG", "15 S", ("TABLET", "STRIP", 15, True)),          # "S" strip count, no form word
    ("CILAMET XL 25MG", "20 S", ("TABLET", "STRIP", 20, True)),
    ("AZITHRAL 500 TAB", "10X10", ("TABLET", "STRIP", 10, True)),      # box of 10 strips of 10
    ("CALCIJOINT SYRUP", "200ML", ("BOTTLE", "BOTTLE", 1, False)),
    ("8X SHAMPOO", "100ML", ("BOTTLE", "BOTTLE", 1, False)),
    ("BETADINE OINTMENT", "20GM", ("TUBE", "TUBE", 1, False)),
    ("CIPLOX EYE DROPS", "5ML", ("BOTTLE", "BOTTLE", 1, False)),
    ("CEFTRIAXONE INJ", "VAIL", ("VIAL", "VIAL", 1, False)),
    ("KNEE BELT", "1PCS", ("PIECE", "PIECE", 1, False)),
    ("PREGNANCY KIT", "KIT", ("KIT", "KIT", 1, False)),
    ("ENJOY CONDOMS", "1X3", ("PACK", "PACK", 1, False)),             # a count alone is not loose
    ("KS 3'S DOT", "1X3", ("PACK", "PACK", 1, False)),
    ("C-PILL 72 TAB", "1", ("TABLET", "STRIP", 1, False)),
    ("AF 400MG TAB", "1 S", ("TABLET", "STRIP", 1, False)),
    ("MOXIKIND-CV 625MG TAB", "lOS", ("TABLET", "STRIP", 10, True)),  # look-alike letters in a strip count
    ("FORACORT-200 INH", "120MD", ("PIECE", "PIECE", 1, False)),
    ("DEKSEL NANO SYP", "5X5ML", ("BOTTLE", "BOX", 5, True)),         # box of 5 × 5 mL shots
    ("KNEE CAP (VISSCO) ALL SIZE", "1", ("PIECE", "PIECE", 1, False)),
    ("LS BELT (XL) VISSCO", "1", ("PIECE", "PIECE", 1, False)),
    ("IMMUNE SUPPORT TAB", "10S", ("TABLET", "STRIP", 10, True)),
    ("EPISOFT AC SPF50+50G", "EACH", ("PIECE", "PIECE", 1, False)),
])
def test_detection(name, pack, expect):
    u = uom(name, pack)
    assert (u.base_unit, u.pack_unit, u.units_per_pack, u.loose_sale) == expect


def test_content_is_metadata_never_stock():
    u = uom("CALCIJOINT SYRUP", "200ML")
    assert (u.content_qty, u.content_unit, u.units_per_pack) == (Decimal("200"), "ML", 1)


def test_new_products_are_configured_automatically(db):
    tab = inv.create_item(db, name="CROCIN 500 TAB", pack_size="15S")
    syrup = inv.create_item(db, name="BENADRYL SYRUP", pack_size="100ML")
    corrected = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S", units_per_pack=10, base_unit="TABLET")
    assert (tab.base_unit, tab.units_per_pack, tab.loose_sale, tab.packaging_source) == ("TABLET", 15, True, "AUTO")
    assert (syrup.base_unit, syrup.units_per_pack, syrup.loose_sale, syrup.content_qty) == ("BOTTLE", 1, False, Decimal("100"))
    assert (corrected.units_per_pack, corrected.packaging_source) == (10, "MANUAL")


def test_loose_sale_is_derived_never_chosen(db):
    item = inv.create_item(db, name="Test", units_per_pack=1, loose_sale=True)
    assert item.loose_sale is False
    item = inv.create_item(db, name="Test 2", units_per_pack=10, loose_sale=False)
    assert item.loose_sale is True


def test_startup_configures_legacy_products_and_repacks_their_stock(db):
    item = legacy_item(db, strips=3)
    assert packaging_service.auto_configure_pending(db) == 1
    db.commit()
    db.refresh(item)
    assert (item.base_unit, item.units_per_pack, item.loose_sale, item.packaging_source) == ("TABLET", 10, True, "AUTO")
    batch = db.query(Batch).one()
    assert (batch.quantity, batch.units_per_pack, batch.mrp, batch.unit_mrp) == (30, 10, Decimal("200.00"), Decimal("20.0000"))
    moves = [(m.movement_type, m.quantity, m.units_per_pack) for m in db.query(InventoryMovement).order_by(InventoryMovement.id)]
    assert moves == [("OPENING_STOCK", 3, 1), ("REPACK_OUT", -3, 1), ("REPACK_IN", 30, 10)]
    assert stock_ledger.reconcile(db) == []
    assert packaging_service.auto_configure_pending(db) == 0  # idempotent


def test_receiving_stock_configures_an_unconfigured_product_first(db):
    item = legacy_item(db, strips=0)
    inv.add_or_update_batch(db, item, batch_no="NEW1", expiry_date=date(2099, 1, 1), quantity=5, unit="PACK",
                            movement_type="PURCHASE_RECEIPT", mrp="200")
    db.commit()
    assert db.get(Item, item.id).total_stock == 50  # 5 strips inward = +50 tablets


def test_loose_pricing_is_strip_mrp_divided_by_tablets(db):
    """₹200 strip of 10: 1 tablet ₹20, 3 tablets ₹60, 10 tablets ₹200."""
    item = legacy_item(db, strips=3, mrp="200.00")
    packaging_service.auto_configure_pending(db)
    db.commit()
    for qty, amount in ((1, "20.00"), (3, "60.00"), (10, "200.00")):
        sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": qty}], payment_mode="UPI")
        db.commit()
        assert sale.items[0].line_total == Decimal(amount)
        assert sale.items[0].mrp == Decimal("20.00") and sale.items[0].pack_mrp == Decimal("200.00")
    assert db.get(Item, item.id).total_stock == 30 - 14


def test_manual_correction_is_never_overridden(db):
    item = legacy_item(db, strips=2)
    inv.update_item(db, item, base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)  # label said 10, strip has 15
    db.commit()
    assert (item.units_per_pack, item.packaging_source, item.total_stock) == (15, "MANUAL", 30)
    inv.update_item(db, item, pack_size="10S")        # re-detection does not touch a MANUAL setup
    db.commit()
    assert item.units_per_pack == 15
    with pytest.raises(inv.InventoryError, match="stock to zero"):
        inv.update_item(db, item, units_per_pack=10)  # already counted in tablets: never reinterpreted


def test_pack_text_change_redetects_automatic_products(db):
    item = inv.create_item(db, name="ZINCOVIT TAB", pack_size="")
    assert item.units_per_pack == 1
    inv.update_item(db, item, pack_size="15S")
    db.commit()
    assert (item.base_unit, item.units_per_pack, item.loose_sale) == ("TABLET", 15, True)


def test_sale_history_keeps_its_old_pack_snapshot(db):
    item = legacy_item(db, strips=3)
    item.packaging_source = "MANUAL"  # sold before configuration
    db.commit()
    old = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}], payment_mode="UPI")
    db.commit()
    item.packaging_source = "DEFAULT"
    db.commit()
    packaging_service.auto_configure_pending(db)
    db.commit()
    db.refresh(old)
    assert (old.items[0].quantity, old.items[0].units_per_pack, old.items[0].line_total) == (1, 1, Decimal("200.00"))
    assert db.get(Item, item.id).total_stock == 20


def test_fractional_quantities_are_rejected_not_truncated(db):
    sheet = "Product Name,Pack,Batch,Expiry,Qty,MRP\nCIPLOX EYE DROPS,5ML,C1,01/2099,3.7,20\n"
    result = inv.import_items(db, "fraction.csv", sheet.encode())
    assert result["units"] == 0 and "fractional" in result["errors"][0]


def test_uom_register_api(client, db):
    legacy_item(db)
    inv.create_item(db, name="BENADRYL SYRUP", pack_size="100ML")
    packaging_service.auto_configure_pending(db)
    db.commit()
    login(client)
    rows = {r["name"]: r for r in client.get("/api/erp/uom").json()["rows"]}
    assert rows["ADVASTAT 10 TAB"]["loose"] is True and rows["ADVASTAT 10 TAB"]["stock"] == 30
    assert rows["BENADRYL SYRUP"]["content"] == "100 ml" and rows["BENADRYL SYRUP"]["loose"] is False
    assert [r["name"] for r in client.get("/api/erp/uom", params={"scope": "loose"}).json()["rows"]] == ["ADVASTAT 10 TAB"]
    hit = client.get("/api/erp/pos/search", params={"q": "advastat"}).json()["items"][0]
    assert (hit["loose"], hit["upp"], hit["batches"][0]["unit_mrp"]) == (True, 10, "20.00")
    sale = client.post("/api/sales", json={"lines": [{"item_id": hit["id"], "quantity": 3}], "payment_mode": "CASH",
                                           "cash_received": 100})
    assert sale.status_code == 200 and sale.json()["total"] == "60.00"
