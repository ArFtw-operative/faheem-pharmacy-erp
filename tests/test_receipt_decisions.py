from decimal import Decimal

import pytest
from app.models import Item, Batch, InventoryMovement, SupplierProductMap
from app.services import inventory_service as inv, purchasing, receipt_decision as rd, units
from app.services.medicine_reference import Catalog
from tests.test_purchasing import draft, supplier, stock, csv_bytes
from tests.conftest import certain, login


def product(db, name="EXAMPLE 40MG TAB", pack="15S", upp=15):
    return inv.create_item(db, name=name, pack_size=pack, base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=upp, dosage_form="TABLET")


@pytest.mark.parametrize("bad", ["-1", "1e3", "NaN", "Infinity", "2.5+0.5+1", "2x10", "take 2", "2 tablets maybe", "1,2", "0.1234567"])
def test_malformed_quantities_cannot_become_receipts(bad):
    with pytest.raises(ValueError):
        rd.quantity(bad)


@pytest.mark.parametrize("text,paid,free", [("2.5+0.5", "2.5", "0.5"), ("2.7", "2.7", "0"), ("1,00,000", "100000", "0"), ("5 Nos", "5", "0")])
def test_exact_decimal_quantities(text, paid, free):
    assert rd.quantity(text) == (Decimal(paid), Decimal(free))


def test_fractional_allocations_on_odd_strip_receive_whole_tablets(db):
    item = product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2.5,0.5,54.29,71,135.725")
    line = p.items[0]
    assert line.status == "READY"
    assert line.receipt_decision["received_base_units"] == 45
    assert line.receipt_decision["paid_base_equivalent"] == "37.5"
    assert line.receipt_decision["free_base_equivalent"] == "7.5"
    assert line.receipt_decision["financial_split_only"]
    purchasing.post(db, p)
    db.commit()
    assert stock(db, item) == 45
    assert line.receipt_decision["received_base_units"] == 45


def test_duplicate_free_quantity_and_negative_return_are_blocked(db):
    product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2.5+0.5,0.5,10,20,25",
              ",EXAMPLE 40MG TAB,15S,B2,May-2028,-1,,10,20,-10")
    assert all(not l.receipt_decision["resolved"] for l in p.items)
    with pytest.raises(purchasing.PurchaseError):
        purchasing.post(db, p)
    assert db.query(InventoryMovement).count() == 0


def test_free_only_receipt_is_valid(db):
    item = product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,0,2,0,20,0")
    purchasing.post(db, p)
    db.commit()
    assert stock(db, item) == 30


def test_nested_box_confirmation_scales_stock_cost_and_mrp(db):
    item = product(db, pack="10S", upp=10)
    p = draft(db, ",EXAMPLE 40MG TAB,20X10S,B1,May-2028,2,,1000,1400,2000")
    line = p.items[0]
    assert not certain(line.receipt_decision)
    with pytest.raises(purchasing.PurchaseError):
        purchasing.post(db, p)
    rd.confirm(db, p, line, factor=200, mrp_basis="INVOICE_UNIT", reason="Supplier confirmed 20 strips in each box")
    assert line.receipt_decision["received_base_units"] == 400
    assert line.receipt_decision["master_pack_equivalent"] == "40"
    purchasing.post(db, p)
    db.commit()
    b = db.query(Batch).filter_by(item_id=item.id).one()
    assert b.quantity == 400
    assert b.purchase_rate == Decimal("50.00")
    assert b.mrp == Decimal("70.00")
    assert db.query(SupplierProductMap).one().receipt_conventions


def test_supplier_memory_is_scoped_to_pack_and_schema(db):
    product(db, pack="10S", upp=10)
    sup = supplier(db)
    row = ",EXAMPLE 40MG TAB,20X10S,B1,May-2028,2,,10,20,20"
    p = draft(db, row, sup=sup)
    rd.confirm(db, p, p.items[0], factor=10, mrp_basis="MASTER_PACK", reason="Supplier quantity counts strips")
    purchasing.post(db, p)
    db.commit()
    p2 = purchasing.create_from_file(db, "next.csv", csv_bytes(row.replace("B1", "B2")), supplier_id=sup.id, invoice_no="NEXT")
    assert p2.items[0].receipt_decision["source"] == "SUPPLIER_MEMORY"
    assert p2.items[0].receipt_decision["received_base_units"] == 20
    purchasing.correct(db, p2, p2.items[0], {"pack": "30X10S"})
    assert not certain(p2.items[0].receipt_decision)


def test_pack_size_conflict_with_matched_product_blocks(db):
    product(db, pack="10S", upp=10)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2,,10,20,20")
    assert "pack_conflict" in {i["code"] for i in p.items[0].issues}
    with pytest.raises(purchasing.PurchaseError):
        purchasing.post(db, p)


def test_content_size_variant_cannot_be_overridden_by_conversion(db):
    inv.create_item(db, name="EXAMPLE SYRUP", pack_size="100ML", base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    p = draft(db, ",EXAMPLE SYRUP,200ML,B1,May-2028,2,,10,20,20")
    line = p.items[0]
    rd.confirm(db, p, line, factor=1, mrp_basis="MASTER_PACK", reason="Bottles are counted individually")
    assert not certain(line.receipt_decision)
    assert "content_conflict" in {i["code"] for i in line.issues}


def test_correcting_source_invalidates_accepted_warnings_and_match(db):
    item = product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2,,10,20,999")
    line = p.items[0]
    purchasing.correct(db, p, line, {"accept": ["amount_mismatch"]})
    purchasing.correct(db, p, line, {"name": "DIFFERENT TABLET", "amount": "1000"})
    assert not (line.corrections or {}).get("_accepted")
    assert line.item is None


def test_ambiguous_same_name_does_not_choose_first_product(db):
    product(db, pack="10S", upp=10)
    product(db, pack="15S", upp=15)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2,,10,20,20")
    assert p.items[0].item is None


def test_hsn_and_numeric_corruption_are_not_silently_coerced(db):
    product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2,,1e3,20,20")
    purchasing.correct(db, p, p.items[0], {"hsn": "205.00"})
    assert {"rate_invalid", "hsn_invalid"} <= {i["code"] for i in p.items[0].issues}


def test_reference_catalog_idempotence_and_variant_preservation(tmp_path):
    path = tmp_path / "refs.csv"
    path.write_text("name,manufacturer_name,pack_size_label,is_discontinued\nExample 40 Tablet,Maker,strip of 15 tablets,FALSE\nExample 40 Tablet,Maker,strip of 10 tablets,FALSE\n", encoding="utf-8")
    c = Catalog(tmp_path / "refs.sqlite")
    c.ingest(path)
    c.ingest(path)
    assert len(c.search("EXAMPLE 40 TAB")) == 2
    with c.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM medicines").fetchone()[0] == 2


@pytest.mark.parametrize("pack,upp,content", [("strip of 15 tablets",15,None), ("strip of 10 capsule sr",10,None), ("bottle of 100 ml Syrup",1,Decimal(100)), ("tube of 30 gm Cream",1,Decimal(30))])
def test_catalog_pack_labels(pack, upp, content):
    info = units.parse_pack(pack)
    assert info.confident and info.units_per_pack == upp and info.content_qty == content


def test_invoice_unit_api_and_posted_snapshot(client, db):
    login(client)
    product(db, pack="10S", upp=10)
    p = draft(db, ",EXAMPLE 40MG TAB,20X10S,B1,May-2028,2,,10,20,20")
    db.commit()
    url = f"/api/erp/purchases/{p.id}/lines/{p.items[0].id}/invoice-unit"
    assert client.put(url, json={"units_per_invoice_unit":0,"mrp_basis":"MASTER_PACK","reason":"checked"}).status_code == 400
    assert client.put(url, json={"units_per_invoice_unit":10,"mrp_basis":[],"reason":"checked"}).status_code == 400
    r = client.put(url, json={"units_per_invoice_unit":10,"mrp_basis":"MASTER_PACK","reason":"Counted the tablets in one billed strip"})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["receipt"]["received_base_units"] == 20
    assert client.post(f"/api/erp/purchases/{p.id}/post",json={}).status_code == 200
    assert client.post(f"/api/erp/purchases/{p.id}/post",json={}).status_code == 400
    assert client.put(url,json={"units_per_invoice_unit":200,"mrp_basis":"MASTER_PACK","reason":"second attempt"}).status_code == 400
    assert client.get(f"/api/erp/purchases/{p.id}/decisions").json()["lines"][0]["receipt"]["received_base_units"] == 20


def test_reference_api_requires_purchase_permission(client):
    response = client.get("/api/erp/medicine-reference?q=test", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_catalog_resolves_missing_tablet_pack_without_creating_reference_products(db, tmp_path, monkeypatch):
    import app.config
    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    source = tmp_path / "reference.csv"
    source.write_text("name,manufacturer_name,pack_size_label\nExample 40 Tablet,Maker,strip of 15 tablets\n", encoding="utf-8")
    Catalog(tmp_path / "medicine-reference.sqlite").ingest(source)
    p = draft(db, ",Example 40 Tablet,,B1,May-2028,2,,10,20,20")
    assert db.query(Item).count() == 0
    purchasing.correct(db, p, p.items[0], {"new_product": True})
    assert p.items[0].units_per_pack == 15
    assert p.items[0].receipt_decision["received_base_units"] == 30
    assert p.items[0].receipt_decision["reference_evidence"]["source"] == "reference.csv"


def test_missing_tablet_pack_without_reference_requires_definition(db):
    p = draft(db, ",Unknown Brand Tablet,,B1,May-2028,2,,10,20,20")
    purchasing.correct(db, p, p.items[0], {"new_product": True})
    assert not certain(p.items[0].receipt_decision)


def test_pdf_incomplete_extraction_cannot_be_acknowledged_as_money_difference(db):
    product(db)
    p = draft(db, ",EXAMPLE 40MG TAB,15S,B1,May-2028,2,,10,20,20")
    p.charges = {"_extraction_issues": ["Printed 20 rows, extracted 19"]}
    with pytest.raises(purchasing.PurchaseError, match="extraction"):
        purchasing.post(db, p, accept_difference=True)


def test_pack_of_and_nested_words_are_parsed_without_choosing_invoice_level():
    assert units.parse_pack("pack of 10 tablets").units_per_pack == 10
    box = units.parse_pack("box of 20 strips of 15 tablets")
    assert (box.units_per_pack, box.outer_count, box.confident) == (15,20,False)


def test_latest_release_migrates_without_rewriting_history(tmp_path):
    import shutil
    from pathlib import Path
    from app.services import upgrade_service as up
    source = Path(__file__).parent / "fixtures" / "releases" / "1.7.0.db"
    target = tmp_path / "migrate.db"
    shutil.copyfile(source,target)
    before = up.fingerprint(target)
    # Snapshot durability is covered by the existing platform-specific backup tests.
    result = up.upgrade(target, backups=False)
    assert result["status"] == "upgraded"
    assert up.compare(before,up.fingerprint(target)) == []


def test_same_invoice_number_from_two_suppliers_stays_separate():
    from app.services.purchase_import import parse
    raw = b"Supplier,Invoice_No,Product_Name,Qty,Rate,MRP\nAlpha,INV1,AAA,1,10,20\nBeta,INV1,BBB,2,30,40\n"
    d = parse("mixed.csv",raw)
    assert len(d.parts) == 2
    assert {p.supplier_name for p in d.parts} == {"Alpha","Beta"}


def test_duplicate_reference_rows_cannot_hide_a_conflicting_pack(tmp_path, monkeypatch):
    import app.config
    from app.services.medicine_reference import unique_pack_evidence
    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    source = tmp_path / "reference.csv"
    source.write_text("name,manufacturer_name,pack_size_label\n" +
                      "Example 40 Tablet,Maker,strip of 15 tablets\n" * 101 +
                      "Example 40 Tablet,Maker,strip of 10 tablets\n", encoding="utf-8")
    Catalog(tmp_path / "medicine-reference.sqlite").ingest(source)
    assert unique_pack_evidence("Example 40 Tablet", "Maker") is None
