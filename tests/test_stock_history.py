"""Stock history across all products, and batch provenance in Inventory."""
from __future__ import annotations

from datetime import date

from app.services import inventory_service as inv
from app.services import purchasing
from tests.conftest import login

CSV = (b"Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,Amount\n"
       b"D650,DOLO 650MG TAB,15S,DB1,May-2028,10,1,24.00,33.60,240.00\n")


def _posted(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    sup = purchasing.save_supplier(db, {"name": "Micro Distributors"})
    p = purchasing.create_from_file(db, "h.csv", CSV, supplier_id=sup.id, invoice_no="MD-77", invoice_date=date(2026, 9, 1))
    purchasing.post(db, p)
    db.commit()
    return item, p


def test_history_lists_filters_and_totals(client, db):
    item, p = _posted(db)
    login(client)
    d = client.get("/api/erp/stock-history").json()
    assert d["total"] >= 1 and d["in"] == 165 and d["out"] == 0 and d["products"] == 1
    row = d["movements"][0]
    assert (row["item"], row["reference"], row["reference_type"], row["unit"]) == ("DOLO 650MG TAB", p.reference_no, "PURCHASE", "tablet")
    assert {t["code"] for t in d["types"]} >= {"SALE", "PURCHASE_RECEIPT"}
    assert client.get("/api/erp/stock-history", params={"direction": "out"}).json()["total"] == 0
    assert client.get("/api/erp/stock-history", params={"q": "MD-77"}).json()["total"] >= 1          # supplier invoice no. finds it
    assert client.get("/api/erp/stock-history", params={"q": p.reference_no}).json()["total"] >= 1
    assert client.get("/api/erp/stock-history", params={"type": "SALE"}).json()["total"] == 0
    assert client.get("/api/erp/stock-history", params={"type": "BOGUS"}).status_code == 400
    assert client.get("/api/erp/stock-history", params={"start": "2026-09-10", "end": "2026-09-01"}).status_code == 400
    assert client.get("/api/erp/stock-history", params={"item": item.id}).json()["total"] >= 1
    csv = client.get("/api/erp/stock-history.csv")
    assert csv.status_code == 200 and "DOLO 650MG TAB" in csv.text and p.reference_no in csv.text
    assert csv.headers["cache-control"] == "no-store"


def test_inventory_batch_shows_supplier_invoice_and_received_date(client, db):
    item, p = _posted(db)
    login(client)
    b = client.get(f"/api/erp/inventory/{item.id}").json()["batches"][0]
    assert (b["supplier"], b["invoice"], b["purchase_ref"]) == ("Micro Distributors", "MD-77", p.reference_no)
    assert b["received"][:4] == str(date.today().year)
