"""Purchases: import → staging → review → correction → atomic posting."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app.models import AuditLog, Batch, InventoryMovement, Item, Purchase, PurchaseItem, SupplierProductMap
from app.services import inventory_service as inv
from app.services import purchasing
from app.services.purchasing import PurchaseError
from tests.conftest import login

HEAD = "Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,Amount\n"
from tests.invoice_samples import rough_estimate_pdf  # noqa: E402

ESTIMATE_PDF, _ = rough_estimate_pdf()


def supplier(db, name="Micro Distributors"):
    return purchasing.save_supplier(db, {"name": name, "gst_number": "29ABCDE1234F1Z5", "credit_days": "30"})


def dolo(db):
    return inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")


def csv_bytes(*rows: str) -> bytes:
    return (HEAD + "\n".join(rows) + "\n").encode()


def draft(db, *rows, sup=None, total=None, name="inv.csv"):
    sup = sup or supplier(db)
    p = purchasing.create_from_file(db, name, csv_bytes(*rows), supplier_id=sup.id, invoice_no="INV-1",
                                    invoice_date=date(2026, 9, 1), supplier_total=total)
    db.flush()
    return p


def stock(db, item):
    db.expire_all()
    return sum(b.quantity for b in db.query(Batch).filter(Batch.item_id == item.id))


def test_import_stages_without_touching_stock(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    line = p.items[0]
    assert p.status == "DRAFT" and line.status == "READY" and line.match_method == "EXACT_NAME"
    assert (line.quantity, line.quantity_free, line.expiry_date) == (10, 2, date(2028, 5, 1))
    assert line.raw["batch"] == "DB1" and line.raw["expiry"] == "May-2028"
    assert stock(db, item) == 0 and db.query(InventoryMovement).count() == 0


def test_post_receives_paid_and_free_in_base_units_atomically(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00", total="240.00")
    purchasing.post(db, p)
    db.commit()
    assert p.status == "POSTED" and p.reference_no == "PUR-000001"
    assert stock(db, item) == 12 * 15
    types = sorted(m.movement_type for m in db.query(InventoryMovement))
    assert "PURCHASE_RECEIPT" in types
    assert all(m.reference_no == "PUR-000001" for m in db.query(InventoryMovement))
    assert p.items[0].status == "POSTED" and p.items[0].batch_id


def test_scientific_batch_bad_expiry_and_fraction_need_review(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,2.61E+09,####,1.5,,24,33.6,36")
    codes = {i["code"] for i in p.items[0].issues}
    assert {"batch_corrupt", "expiry_invalid", "qty_fraction"} <= codes
    assert p.items[0].status == "NEEDS_REVIEW"
    with pytest.raises(PurchaseError) as exc:
        purchasing.post(db, p)
    assert exc.value.code == "LINES_NOT_READY"


def test_correction_keeps_raw_and_records_who_and_when(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,2.61E+09,May-2028,10,,24,33.6,240")
    line = p.items[0]
    purchasing.correct(db, p, line, {"batch": "2610000001"})
    assert line.raw["batch"] == "2.61E+09" and line.batch_no == "2610000001"
    c = line.corrections["batch"]
    assert c["raw"] == "2.61E+09" and c["by"] == "system" and c["at"]
    assert line.status == "CORRECTED"
    purchasing.correct(db, p, line, {"batch": "2.61E+09"})   # back to the supplier value
    assert "batch" not in line.corrections and line.status == "NEEDS_REVIEW"


def test_unknown_product_needs_match_and_suggests_without_applying(db):
    dolo(db)
    inv.create_item(db, name="DOLO 500MG TAB", pack_size="15S")
    p = draft(db, "X1,DOLO-650 TABLETS,15S,DB1,May-2028,10,,24,33.6,240")
    line = p.items[0]
    assert line.status == "PRODUCT_MATCH_REQUIRED" and line.item_id is None
    names = [s["name"] for s in purchasing.suggestions(db, line)]
    assert names[0] == "DOLO 650MG TAB"
    assert line.item_id is None                     # suggestions are never applied


def test_confirmed_match_is_remembered_for_the_supplier(db):
    item = dolo(db)
    sup = supplier(db)
    p = draft(db, "X1,DOLO-650 TABLETS,15S,DB1,May-2028,10,,24,33.6,240", sup=sup)
    purchasing.correct(db, p, p.items[0], {"item_id": item.id})
    purchasing.post(db, p)
    db.commit()
    m = db.query(SupplierProductMap).one()
    assert (m.supplier_code, m.item_id) == ("X1", item.id)
    p2 = purchasing.create_from_file(db, "inv2.csv", csv_bytes("X1,DOLO 650 (NEW PACK),15S,DB2,Jun-2028,5,,24,33.6,120"),
                                     supplier_id=sup.id, invoice_no="INV-2")
    assert p2.items[0].match_method == "SUPPLIER_MAP" and p2.items[0].item_id == item.id


def test_new_product_created_only_on_post_with_confirmed_units(db):
    p = draft(db, "N1,AZEE 500 TAB,3S,AZ1,Jan-2029,4,,60,85,240")
    line = p.items[0]
    purchasing.correct(db, p, line, {"new_product": True})
    assert line.units_per_pack == 3 and line.base_unit == "TABLET"
    assert line.status == "READY"                        # category is optional
    purchasing.correct(db, p, line, {"category": "PHARMA"})
    assert line.status == "CORRECTED"
    assert db.query(Item).filter(Item.name == "AZEE 500 TAB").count() == 0
    purchasing.post(db, p)
    db.commit()
    item = db.query(Item).filter(Item.name == "AZEE 500 TAB").one()
    assert (item.units_per_pack, item.category, item.packaging_source) == (3, "PHARMA", "MANUAL")
    assert stock(db, item) == 12


def test_duplicate_file_and_duplicate_posted_invoice_blocked(db):
    dolo(db)
    sup = supplier(db)
    row = "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240"
    p = draft(db, row, sup=sup)
    with pytest.raises(PurchaseError) as exc:
        draft(db, row, sup=sup)
    assert exc.value.code == "DUPLICATE_FILE"
    purchasing.post(db, p)
    db.commit()
    p2 = purchasing.create_from_file(db, "other.csv", csv_bytes(row.replace("DB1", "DB9")), supplier_id=sup.id,
                                     invoice_no="inv-1")
    with pytest.raises(PurchaseError) as exc:
        purchasing.post(db, p2)
    assert exc.value.code == "DUPLICATE_INVOICE"


def test_total_difference_needs_acknowledgement(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240", total="250")
    assert purchasing.summary(p)["difference"] == "10.00"
    with pytest.raises(PurchaseError) as exc:
        purchasing.post(db, p)
    assert exc.value.code == "TOTAL_DIFFERENCE" and stock(db, item) == 0
    purchasing.post(db, p, accept_difference=True)
    db.commit()
    assert p.status == "POSTED"
    assert db.query(AuditLog).filter(AuditLog.details.like("%difference%acknowledged%")).count() == 1


def test_failed_post_leaves_nothing_behind(db):
    item = dolo(db)
    inv.create_item(db, name="CROCIN 500 TAB", pack_size="15S")
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240",
              "C500,CROCIN 500 TAB,15S,CR1,May-2028,5,,20,30,100")
    db.commit()
    # stock an existing batch with a conflicting expiry to force a failure on line 2
    crocin = db.query(Item).filter(Item.name == "CROCIN 500 TAB").one()
    inv.add_or_update_batch(db, crocin, batch_no="CR1", expiry_date=date(2027, 1, 1), quantity=1, unit="PACK",
                            movement_type="OPENING_STOCK", mrp="30")
    db.commit()
    before = db.query(InventoryMovement).count()
    try:
        purchasing.post(db, p)
    except PurchaseError:
        db.rollback()
        db.expire_all()
        assert db.get(Purchase, p.id).status == "DRAFT"
        assert db.query(InventoryMovement).count() == before and stock(db, item) == 0
    else:  # same batch number with a new expiry is accepted as a separate batch
        db.commit()
        assert stock(db, item) == 150


def test_expired_and_mrp_below_rate_warnings(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,Jan-2020,10,,40,33.6,400")
    codes = {i["code"]: i for i in p.items[0].issues}
    assert "expired" in codes and "mrp_below_rate" in codes
    purchasing.correct(db, p, p.items[0], {"expiry": "Jan-2029", "accept": ["mrp_below_rate"]})
    assert p.items[0].status == "CORRECTED"


def test_unreadable_images_and_blank_scans_are_refused(db):
    # scans and photos go through OCR (tests/test_ocr_route.py); what cannot be read is still refused
    sup = supplier(db)
    with pytest.raises(PurchaseError, match="scan|Photos|OCR"):
        purchasing.create_from_file(db, "bill.jpg", b"\xff\xd8\xff", supplier_id=sup.id)
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    with pytest.raises(PurchaseError, match="scan"):
        purchasing.create_from_file(db, "scan.pdf", doc.tobytes(), supplier_id=sup.id)


def test_xlsx_identifiers_stay_text(db):
    dolo(db)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Product Code", "Product Name", "Batch", "Expiry", "Qty", "Rate", "MRP"])
    ws.append([1001, "DOLO 650MG TAB", 240315, "05/2028", 10, 24, 33.6])
    buf = io.BytesIO()
    wb.save(buf)
    p = purchasing.create_from_file(db, "inv.xlsx", buf.getvalue(), supplier_id=supplier(db).id, invoice_no="X")
    line = p.items[0]
    assert line.raw["batch"] == "240315" and line.supplier_code == "1001" and line.status == "READY"


def test_digital_pdf_imports_lines(db):
    p = purchasing.create_from_file(db, "rough-estimate-E004512.pdf", ESTIMATE_PDF, supplier_id=supplier(db).id)
    assert len(p.items) == 132 and p.source_format == "PDF"
    assert db.query(InventoryMovement).count() == 0


def test_supplier_master_validation(db):
    s = supplier(db)
    assert s.code == f"SUP{s.id:04d}" and s.credit_days == 30
    with pytest.raises(PurchaseError, match="already exists"):
        supplier(db)
    recorded = purchasing.save_supplier(db, {"name": "Partial GST record", "gst_number": "123"})
    assert recorded.gst_number == "123"


# --------------------------------------------------------------------------- API
def test_api_import_review_post_and_return(client, db):
    login(client)
    item = dolo(db)
    db.commit()
    r = client.post("/api/erp/suppliers", json={"name": "Api Pharma", "phone": "9999", "credit_days": 15})
    assert r.status_code == 200, r.text
    sid = r.json()["supplier"]["id"]
    body = csv_bytes("D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240")
    r = client.post("/api/erp/purchases/import", files={"file": ("inv.csv", body, "text/csv")},
                    data={"supplier_id": str(sid), "invoice_no": "A-1", "invoice_date": "01/09/2026"})
    assert r.status_code == 200, r.text
    doc = r.json()
    pid, lid = doc["purchase"]["id"], doc["lines"][0]["id"]
    assert doc["summary"]["postable"] and doc["purchase"]["invoice_date"] == "2026-09-01"
    r = client.post("/api/erp/purchases/import", files={"file": ("inv.csv", body, "text/csv")}, data={"supplier_id": str(sid)})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_FILE"
    r = client.put(f"/api/erp/purchases/{pid}/lines/{lid}", json={"quantity": "12", "amount": "288"})
    assert r.json()["lines"][0]["status"] == "CORRECTED"
    r = client.post(f"/api/erp/purchases/{pid}/post", json={})
    assert r.status_code == 200, r.text
    assert r.json()["purchase"]["reference_no"].startswith("PUR-")
    reg = client.get("/api/erp/purchases?status=POSTED").json()
    assert reg["total"] == 1 and reg["purchases"][0]["lines"] == 1
    assert stock(db, item) == 180
    batch = db.query(Batch).filter(Batch.item_id == item.id).one()
    r = client.post("/api/erp/purchase-returns", json={"batch_id": batch.id, "quantity": "1s", "reason": "damaged"})
    assert r.status_code == 200, r.text
    assert r.json()["reference_no"] == "PR-000001" and stock(db, item) == 165
    assert r.json()["returns"][0]["supplier"] == "Api Pharma"


def test_api_manual_purchase_and_permissions(client, db):
    login(client)
    item = dolo(db)
    sup = supplier(db)
    db.commit()
    r = client.post("/api/erp/purchases", json={"supplier_id": sup.id, "invoice_no": "M-1"})
    pid = r.json()["purchase"]["id"]
    r = client.post(f"/api/erp/purchases/{pid}/lines", json={"item_id": item.id, "batch": "M1", "expiry": "Dec-2028",
                                                            "quantity": "3", "rate": "24", "mrp": "33.60"})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["status"] == "READY"
    assert client.post(f"/api/erp/purchases/{pid}/cancel", json={}).status_code == 400
    r = client.post(f"/api/erp/purchases/{pid}/cancel", json={"reason": "wrong supplier"})
    assert r.json()["purchase"]["status"] == "CANCELLED"
    assert client.get("/api/erp/purchases/template.csv").status_code == 200
    assert client.get("/api/erp/purchase-products?q=dolo").json()["items"][0]["id"] == item.id


def test_api_manual_line_without_product(client, db):
    """A line typed in full by hand (no product chosen) waits for a match, or posts as a new product."""
    login(client)
    sup = supplier(db)
    db.commit()
    pid = client.post("/api/erp/purchases", json={"supplier_id": sup.id, "invoice_no": "M-2"}).json()["purchase"]["id"]
    r = client.post(f"/api/erp/purchases/{pid}/lines", json={"name": "ZYXOMAB 500 TAB", "pack": "10S", "batch": "Z1",
                                                            "expiry": "Dec-2028", "quantity": "2", "rate": "40", "mrp": "56"})
    assert r.status_code == 200, r.text
    line = next(l for l in r.json()["lines"] if l["id"] == r.json()["line_id"])
    assert line["name"] == "ZYXOMAB 500 TAB" and line["item"] is None and line["status"] == "PRODUCT_MATCH_REQUIRED"
    r = client.put(f"/api/erp/purchases/{pid}/lines/{line['id']}", json={"new_product": True, "units_per_pack": "10",
                                                                        "base_unit": "TABLET", "pack_unit": "STRIP"})
    assert r.status_code == 200, r.text
    line = r.json()["lines"][0]
    if line["status"] == "NEEDS_REVIEW":
        codes = [i["code"] for i in line["issues"] if i["level"] == "warn" and not i["accepted"]]
        line = client.put(f"/api/erp/purchases/{pid}/lines/{line['id']}", json={"accept": codes}).json()["lines"][0]
    assert line["status"] in ("READY", "CORRECTED"), line
    r = client.post(f"/api/erp/purchases/{pid}/post", json={})
    assert r.status_code == 200, r.text
    assert db.query(Item).filter(Item.name == "ZYXOMAB 500 TAB").count() == 1


def test_suggestions_respect_strength_and_ignore_form_words(db):
    inv.create_item(db, name="DAPANORM 10 TABS", pack_size="10s")
    inv.create_item(db, name="DAPANORM 5 TABS", pack_size="10S")
    inv.create_item(db, name="CLOPITAB TAB", pack_size="15S")
    p = draft(db, "DP05,DAPANORM 5 TABLETS,10S,DB1,May-2028,3,,90,130,270",
              "ZZ9,ZINCOVIT NEW TAB,15S,ZN1,May-2028,2,,80,110,160")
    first, zinc = p.items
    s = purchasing.suggestions(db, first)
    assert s[0]["name"] == "DAPANORM 5 TABS"
    ten = next((x for x in s if x["name"] == "DAPANORM 10 TABS"), None)
    assert ten is None or ("different strength" in ten["note"] and ten["score"] < s[0]["score"])
    assert purchasing.suggestions(db, zinc) == []      # "TAB" alone is no reason to suggest CLOPITAB


def test_grand_total_label_in_any_column_is_read(db):
    dolo(db)
    body = csv_bytes("D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240") + b"Grand Total,,,,,,,,,250.00\n"
    p = purchasing.create_from_file(db, "t.csv", body, supplier_id=supplier(db).id, invoice_no="G-1")
    assert p.supplier_total == Decimal("250.00") and purchasing.summary(p)["difference"] == "10.00"


# --------------------------------------------------------------------------- understanding any supplier layout
MARG = Path(__file__).parent / "fixtures" / "marg-flat-invoice.csv"


def test_distributor_flat_export_is_understood_and_reconciles(db):
    """ProdName/BatchNo/IDisPer/IGstPer/FeedNo/NetAmt… — no fixed column names."""
    p = purchasing.create_from_file(db, MARG.name, MARG.read_bytes(), supplier_id=supplier(db).id)
    assert (p.invoice_no, p.invoice_date, p.supplier_total) == ("SV/25-26/04521", date(2026, 9, 14), Decimal("1478.00"))
    assert p.charges == {"round_off": "0.50", "printed_gst": "158.30"}        # SumGst: reconciled, never added to the total
    g = purchasing.gst_summary(db, p)
    assert g["total"] == "158.30" and g["printed"] == "158.30" and not any("prints total GST" in x for x in g["problems"])
    azee = p.items[1]
    assert (azee.product_name, azee.batch_no, azee.expiry_date, azee.quantity) == ("AZEE 500 TAB", "AZ24107", date(2027, 11, 1), 15)
    assert (azee.gst_rate, azee.discount, azee.line_total, azee.manufacturer) == (Decimal("12"), Decimal("10.80"), Decimal("1069.20"), "CIPLA LTD")
    t = purchasing.summary(p)
    assert (t["totals"]["taxable"], t["totals"]["gst"], t["calculated_total"], t["difference"]) == ("1319.20", "158.30", "1478.00", "0.00")
    fields = {c["field"]: c["column"] for c in p.column_map if c["level"] != "ignored"}
    assert fields["discount"] == "IDisPer" and fields["bill_discount"] == "DisPer" and fields["amount"] == "ProValue"
    assert "Mrp_Old" not in fields.values() and "RetPrice" not in fields.values()


def test_file_with_two_invoices_becomes_two_drafts(db):
    text = MARG.read_text()
    second = text.splitlines()[1].replace("SV/25-26/04521", "SV/25-26/04522").replace("DOBS4401", "DOBS4402")
    drafts = purchasing.import_file(db, "two.csv", (text + second + "\n").encode(), supplier_id=supplier(db).id)
    assert [d.invoice_no for d in drafts] == ["SV/25-26/04521", "SV/25-26/04522"]
    assert [len(d.items) for d in drafts] == [3, 1]


def test_column_correction_is_learned_for_the_supplier(db):
    sup = supplier(db)
    p = purchasing.create_from_file(db, MARG.name, MARG.read_bytes(), supplier_id=sup.id)
    assert p.items[0].manufacturer == "MICRO LABS"                     # from ComName
    purchasing.remap(db, p, {"MfgName": "manufacturer"})
    assert p.items[0].manufacturer == "MICRO LABS LTD"
    assert sup.column_profile["mfgname"] == "manufacturer"
    other = MARG.read_text().replace("SV/25-26/04521", "SV/25-26/09999")
    p2 = purchasing.create_from_file(db, "next.csv", other.encode(), supplier_id=sup.id)
    assert p2.items[0].manufacturer == "MICRO LABS LTD"                # remembered next time
    assert any("learned" in c["reason"] for c in p2.column_map)


def test_pharmacy_can_extend_the_vocabulary_without_code(db):
    from app.services import settings_service

    body = b"Itm Desc,Lot,Exp,Qtty,PurRt,Mrp\nDOLO 650MG TAB,DB1,05/28,10,24,33.6\n"
    first = purchasing.create_from_file(db, "a.csv", body, supplier_id=supplier(db).id, invoice_no="V0")
    mapped = {c["field"] for c in first.column_map}
    assert {"name", "batch", "expiry", "mrp", "rate"} <= mapped and "quantity" not in mapped
    assert "qty_missing" in {i["code"] for i in first.items[0].issues}   # never guessed
    purchasing.cancel(db, first, reason="re-import with vocabulary")
    settings_service.set_setting(db, "invoice_vocabulary", '{"qtty": "qty", "purrt": "rate", "rt": "rate"}')
    p = purchasing.create_from_file(db, "a.csv", body, supplier_id=supplier(db, "Other Agency").id, invoice_no="V1")
    line = p.items[0]
    assert (line.product_name, line.batch_no, line.quantity, line.rate) == ("DOLO 650MG TAB", "DB1", 10, Decimal("24.00"))


def test_unnamed_expiry_column_is_inferred_from_its_values(db):
    body = b"Product Name,Batch,Col7,Qty,Rate,MRP\nDOLO 650MG TAB,DB1,05/28,10,24,33.6\nCROCIN TAB,CR1,11/27,5,20,30\n"
    p = purchasing.create_from_file(db, "c.csv", body, supplier_id=supplier(db).id, invoice_no="C1")
    assert p.items[0].expiry_date == date(2028, 5, 1)
    assert "contents" in p.extraction_meta


def test_ai_reader_is_off_by_default(db):
    from app.services import invoice_agent

    assert invoice_agent.advisor(db) is None


def test_legacy_advisor_proposals_are_checked_without_purchase_network_calls(db, monkeypatch):
    from app.services import invoice_agent, settings_service

    settings_service.set_setting(db, "invoice_ai_enabled", "1")
    settings_service.set_setting(db, "invoice_ai_key", "test-key")
    sent = {}

    def fake(headers, rows, **kw):
        sent["headers"], sent["rows"] = headers, rows
        # K3 holds dates: proposing it as the rate must be refused by the content check
        return {"K1": "name", "K2": "batch", "K3": "rate", "K4": "quantity", "K5": "mrp", "K6": "expiry"}

    monkeypatch.setattr(invoice_agent, "propose", fake)
    body = b"K1,K2,K3,K4,K5,K6\nDOLO 650MG TAB,DB1,05/28,10,33.6,05/28\nCROCIN TAB,CR1,11/27,5,30,11/27\n"
    from app.services import purchase_import
    p = purchase_import.parse("k.csv", body, advisor=fake)
    fields = {c["field"]: c["column"] for c in p.column_map}
    assert fields["name"] == "K1" and fields["quantity"] == "K4" and fields["expiry"] == "K6"
    assert "rate" not in fields                                   # contradicted by the values → not used
    assert len(sent["rows"]) <= invoice_agent.SAMPLE_ROWS
    sent.clear()
    with pytest.raises(purchasing.PurchaseError):
        purchasing.create_from_file(db, "k.csv", body, supplier_id=supplier(db).id, invoice_no="K-1")
    assert not sent  # ERP importing stays local even when legacy AI settings are enabled


def test_api_columns_window_remaps_and_learns(client, db):
    login(client)
    sup = supplier(db)
    db.commit()
    r = client.post("/api/erp/purchases/import", files={"file": (MARG.name, MARG.read_bytes(), "text/csv")},
                    data={"supplier_id": str(sup.id)})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["drafts"] == [doc["purchase"]["id"]] and doc["summary"]["difference"] == "0.00"
    cols = {c["column"]: c["field"] for c in doc["purchase"]["column_map"]}
    assert cols["ProdName"] == "name" and cols["Mrp_Old"] == ""
    assert client.get("/api/erp/purchases/fields").status_code == 200
    r = client.put(f"/api/erp/purchases/{doc['purchase']['id']}/columns", json={"changes": {"MfgName": "manufacturer"}})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["manufacturer"] == "MICRO LABS LTD"
    r = client.put(f"/api/erp/purchases/{doc['purchase']['id']}", json={"charges": {"freight": "20"}})
    assert r.json()["summary"]["totals"]["total"] == "1498.00"


# --------------------------------------------------------------------------- partial receipt (post selected lines)
def _three_lines(db, sup=None):
    dolo(db)
    inv.create_item(db, name="CROCIN 500 TAB", pack_size="15S")
    return draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240",
                 "C500,CROCIN 500 TAB,15S,CR1,May-2028,5,,20,30,100",
                 "X9,UNKNOWN NEW THING,10S,U1,May-2028,2,,10,15,20", sup=sup)


def test_post_selected_ready_lines_leaves_the_rest_open(db):
    p = _three_lines(db)
    d, c, u = p.items
    assert u.status == "PRODUCT_MATCH_REQUIRED"
    with pytest.raises(PurchaseError) as exc:
        purchasing.post(db, p, line_ids=[d.id, u.id])
    assert exc.value.code == "LINES_NOT_READY"
    purchasing.post(db, p, line_ids=[d.id])
    db.commit()
    assert (p.status, p.reference_no, d.status, c.status) == ("PARTIAL", "PUR-000001", "POSTED", "READY")
    assert stock(db, d.item) == 150 and stock(db, c.item) == 0
    purchasing.correct(db, p, c, {"batch": "CR2"})               # open lines stay editable
    with pytest.raises(PurchaseError, match="can no longer change"):
        purchasing.correct(db, p, d, {"batch": "X"})             # received lines do not
    with pytest.raises(PurchaseError, match="only drafts"):
        purchasing.update_header(db, p, {"invoice_no": "OTHER"})
    purchasing.post(db, p, line_ids=[c.id])
    assert p.status == "PARTIAL" and p.reference_no == "PUR-000001"   # same document number
    purchasing.correct(db, p, u, {"new_product": True, "category": "PHARMA"})
    purchasing.post(db, p)
    db.commit()
    assert p.status == "POSTED" and all(l.status == "POSTED" for l in p.items)
    assert {m.reference_no for m in db.query(InventoryMovement)} == {"PUR-000001"}


def test_partly_posted_invoice_blocks_duplicates_and_can_close_remaining(db):
    sup = supplier(db)
    p = _three_lines(db, sup=sup)
    purchasing.post(db, p, line_ids=[p.items[0].id])
    db.commit()
    again = purchasing.create_from_file(db, "again.csv", csv_bytes("D650,DOLO 650MG TAB,15S,DB7,May-2028,1,,24,33.6,24"),
                                        supplier_id=sup.id, invoice_no="INV-1")
    with pytest.raises(PurchaseError) as exc:
        purchasing.post(db, again)
    assert exc.value.code == "DUPLICATE_INVOICE"
    with pytest.raises(PurchaseError, match="reason"):
        purchasing.close_remaining(db, p, reason="")
    purchasing.close_remaining(db, p, reason="Short supplied")
    assert p.status == "POSTED" and [l.status for l in p.items] == ["POSTED", "CLOSED", "CLOSED"]
    assert p.total == Decimal("240.00")                           # value actually received


def test_api_post_selected_lines(client, db):
    login(client)
    p = _three_lines(db)
    db.commit()
    ids = [p.items[0].id, p.items[1].id]
    r = client.post(f"/api/erp/purchases/{p.id}/post", json={"line_ids": ids})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["purchase"]["status"] == "PARTIAL" and doc["summary"]["open"] == 1 and not doc["summary"]["postable"]
    assert client.get("/api/erp/purchases", params={"status": "PARTIAL"}).json()["total"] == 1
    r = client.post(f"/api/erp/purchases/{p.id}/close", json={"reason": "not supplied"})
    assert r.json()["purchase"]["status"] == "POSTED"
    assert client.post(f"/api/erp/purchases/{p.id}/post", json={"line_ids": ["x"]}).status_code == 400


def test_bulk_new_products_and_confirmed_matches(client, db):
    login(client)
    item = dolo(db)
    p = draft(db, "N1,AZEE 500 TAB,3S,AZ1,Jan-2029,4,,60,85,240",
              "N2,ALZYME SYRUP,200ML,AL1,Jan-2029,2,,50,70,100",
              "X1,DOLO-650 TABLETS,15S,DB1,May-2028,10,,24,33.6,240")
    db.commit()
    a, b, c = p.items
    r = client.post(f"/api/erp/purchases/{p.id}/lines/bulk",
                    json={"line_ids": [a.id, b.id], "changes": {"new_product": True, "category": "PHARMA"}})
    assert r.status_code == 200, r.text
    lines = {l["id"]: l for l in r.json()["lines"]}
    assert lines[a.id]["new_product"] and lines[a.id]["units_per_pack"] == 3 and lines[a.id]["status"] == "CORRECTED"
    assert lines[b.id]["base_unit"] in ("BOTTLE", "ML", "UNIT") and lines[b.id]["category"] == "PHARMA"
    sug = client.post(f"/api/erp/purchases/{p.id}/lines/suggest", json={"line_ids": [c.id]}).json()["suggestions"]
    assert sug[str(c.id)][0]["item_id"] == item.id
    r = client.post(f"/api/erp/purchases/{p.id}/lines/bulk", json={"matches": {str(c.id): item.id}})
    assert {l["id"]: l for l in r.json()["lines"]}[c.id]["match"] == "MANUAL"
    assert client.post(f"/api/erp/purchases/{p.id}/lines/bulk", json={"line_ids": [a.id]}).status_code == 400


def test_scheme_spread_over_quantity_is_received_exactly(db):
    """Distributors bill a 5+1 scheme as 2.5 + 0.5: three whole bottles arrive, 2.5 are paid for."""
    syp = inv.create_item(db, name="AMYRON SYRUP", pack_size="200ML", base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    lanol = inv.create_item(db, name="LANOL-ER TAB", pack_size="10S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=10, loose_sale=True)
    half = inv.create_item(db, name="HALF SYP", pack_size="100ML", base_unit="BOTTLE", pack_unit="BOTTLE", units_per_pack=1)
    p = draft(db, "A1,AMYRON SYRUP,200ML,AM1,May-2028,2.5+0.5,,120.00,160.00,300.00",
              "L1,LANOL-ER TAB,10S,LN1,May-2028,2.5+1,,80.00,105.00,200.00",
              "H1,HALF SYP,100ML,HS1,May-2028,1.5,,50.00,70.00,75.00")
    a, l, h = sorted(p.items, key=lambda x: x.line_no)
    assert a.status == "READY" and (a.quantity, a.quantity_free) == (3, 0)
    assert a.line_total == Decimal("300.00") and a.rate == Decimal("120.00")        # the supplier's printed rate stays
    assert a.landed_rate == Decimal("100.00")                                       # 300 billed for 3 bottles (no GST on this file)
    assert any(i["code"] == "scheme_split" and i["level"] == "info" for i in a.issues)
    assert l.status == "READY" and l.line_total == Decimal("200.00"), l.issues      # 3.5 strips = 35 tablets
    assert h.status == "NEEDS_REVIEW" and any(i["code"] == "qty_fraction" for i in h.issues)   # 1.5 bottles cannot arrive
    purchasing.post(db, p, line_ids=[a.id, l.id])
    db.flush()
    assert stock(db, syp) == 3 and stock(db, lanol) == 35 and stock(db, half) == 0
    b = db.query(Batch).filter(Batch.item_id == lanol.id).one()
    assert Decimal(b.purchase_rate) == Decimal("57.14")                             # 200 / 3.5 strips


def test_real_batch_with_an_E_is_not_mistaken_for_spreadsheet_damage(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,634E2602,May-2028,10,,24,33.6,240",
              "D650,DOLO 650MG TAB,15S,6.34E+05,May-2028,10,,24,33.6,240")
    real, damaged = sorted(p.items, key=lambda x: x.line_no)
    assert not any(i["code"] == "batch_corrupt" for i in real.issues)
    assert any(i["code"] == "batch_corrupt" for i in damaged.issues)


def test_delete_draft_is_audited_and_allows_reimport(db):
    from app.models import Purchase, PurchaseItem
    dolo(db)
    row = "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240"
    sup = supplier(db)
    p = draft(db, row, sup=sup)
    pid = p.id
    purchasing.delete_draft(db, p)
    db.commit()
    assert db.get(Purchase, pid) is None
    assert db.query(PurchaseItem).count() == 0
    assert db.query(InventoryMovement).count() == 0
    assert db.query(AuditLog).filter(AuditLog.details == "Unreceived purchase draft deleted").count() == 1
    assert draft(db, row, sup=sup).status == "DRAFT"


def test_received_purchase_cannot_be_deleted(db):
    item = dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,,24,33.6,240")
    purchasing.post(db, p)
    db.commit()
    with pytest.raises(PurchaseError, match="cannot be deleted"):
        purchasing.delete_draft(db, p)
    assert stock(db, item) == 150


def test_general_goods_can_be_posted_without_optional_metadata(db):
    item = inv.create_item(db, name="General goods", article_id="GEN1", pack_size="1PCS")
    p = draft(db, "GEN1,General goods,,,,3,,,,")
    line = p.items[0]
    assert line.status == "READY", line.issues
    purchasing.post(db, p)
    db.commit()
    assert stock(db, item) == 3
    assert line.batch.batch_no == "" and line.batch.expiry_date is None
    assert line.batch.cost_status == "COST_MISSING"
    assert __import__('app.services.stock_ledger', fromlist=['reconcile']).reconcile(db) == []


def test_unlabelled_receipts_keep_their_purchase_cost_and_price(db):
    item = inv.create_item(db, name="General goods", article_id="GEN1", pack_size="1PCS")
    p = draft(db, "GEN1,General goods,,,,3,,10,20,30")
    purchasing.post(db, p)
    db.commit()
    p2 = purchasing.create_from_file(db, "second.csv", csv_bytes("GEN1,General goods,,,,2,,15,25,30"),
                                     supplier_id=p.supplier_id, invoice_no="GEN-2")
    purchasing.post(db, p2)
    db.commit()
    assert p.items[0].batch_id != p2.items[0].batch_id
    assert p.items[0].batch.mrp == Decimal("20")
    assert p2.items[0].batch.mrp == Decimal("25")
    assert stock(db, item) == 5


def test_delete_draft_api(client, db):
    from app.models import Purchase
    login(client)
    p = purchasing.create_manual(db, supplier_id=supplier(db).id, invoice_no="DELETE-1")
    db.commit()
    pid = p.id
    r = client.delete(f"/api/erp/purchases/{pid}")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Purchase, pid) is None
    assert client.delete(f"/api/erp/purchases/{pid}").status_code == 404



def test_new_general_goods_with_no_metadata_can_be_received(db):
    p = draft(db, ",Unlabelled general product,,,,2,,,,")
    purchasing.correct(db, p, p.items[0], {"new_product": True})
    assert p.items[0].status == "READY", p.items[0].issues
    purchasing.post(db, p)
    db.commit()
    line = p.items[0]
    assert line.item.category == "GENERAL" and line.item.units_per_pack == 1
    assert line.batch.batch_no == "" and line.batch.expiry_date is None and line.batch.quantity == 2



def test_optional_prices_reject_supplied_invalid_values(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,,,2,,-3,bad,")
    codes = {i["code"] for i in p.items[0].issues}
    assert {"rate_invalid", "mrp_invalid"} <= codes
    assert p.items[0].status == "NEEDS_REVIEW"
