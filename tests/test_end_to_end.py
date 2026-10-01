"""The whole ERP in one flow, through the same HTTP APIs the screens use.

purchase import → review → post (with free goods) → loose sale (FEFO) →
customer return to stock → write-off → reversal → purchase return →
manual bill — and at the end the ledger, batches, stock history, sales
register and reports must all agree.
"""
from __future__ import annotations

from decimal import Decimal

from app.models import Batch, InventoryMovement
from app.services import inventory_service as inv
from app.services import stock_ledger
from tests.conftest import login

INVOICE = (b"Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,GST %,Amount\n"
           b"D650,DOLO 650MG TAB,15S,DA1,Jan-2028,4,1,24.00,33.00,5,96.00\n"
           b"D650,DOLO 650MG TAB,15S,DB2,Dec-2029,2,0,25.50,33.00,5,51.00\n"
           b"Grand Total,,,,,,,,,,154.35\n")


def test_full_business_flow_reconciles(client, db):
    login(client)
    dolo = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=15, dosage_form="TABLET")
    db.commit()
    sup = client.post("/api/erp/suppliers", json={"name": "Micro Distributors", "gst_number": "36AAAFB1234C1Z9"}).json()["supplier"]

    # 1. purchase: import → every line matched → reconciles to the printed total → post
    doc = client.post("/api/erp/purchases/import", files={"file": ("inv.csv", INVOICE, "text/csv")},
                      data={"supplier_id": str(sup["id"]), "invoice_no": "MD-900"}).json()
    assert doc["summary"]["postable"] and doc["summary"]["difference"] == "0.00", doc["summary"]
    pid = doc["purchase"]["id"]
    posted = client.post(f"/api/erp/purchases/{pid}/post", json={}).json()
    assert posted["purchase"]["status"] == "POSTED"
    ref = posted["purchase"]["reference_no"]
    stock = lambda: sum(b.quantity for b in db.query(Batch).filter(Batch.item_id == dolo.id))
    db.expire_all()
    assert stock() == (4 + 1 + 2) * 15 == 105

    # 2. loose sale: 20 tablets, FEFO takes the earlier-expiring batch
    sale = client.post("/api/sales", json={"lines": [{"item_id": dolo.id, "quantity": 20}], "payment_mode": "CASH",
                                           "cash_received": "100"}).json()
    assert sale["invoice_no"].startswith("INV-")
    db.expire_all()
    da1 = db.query(Batch).filter(Batch.batch_no == "DA1").one()
    assert da1.quantity == 55 and stock() == 85          # 5 strips (4 + 1 free) − 20 tablets
    assert Decimal(str(sale["total"])) == Decimal("44.00")        # 20 × 33.00 / 15

    # 3. customer returns 5 tablets to stock (UPI refund, no counter needed)
    info = client.get(f"/api/sales/{sale['sale_id']}/refundable").json()
    r = client.post(f"/api/sales/{sale['sale_id']}/refund", json={
        "lines": [{"sale_item_id": info["lines"][0]["sale_item_id"], "quantity": 5}], "reason_code": "CUSTOMER_RETURN",
        "disposition": "RESTOCK", "refund_method": "UPI"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert stock() == 90

    # 4. damage write-off of one strip, then reversed (a mistake)
    w = client.post("/api/erp/adjustments", json={"item_id": dolo.id, "batch_id": da1.id, "direction": "OUT",
                                                  "category": "DAMAGE", "quantity": "1s", "reason": "wet carton"}).json()
    assert w["stock"] == 75
    rv = client.post(f"/api/erp/adjustments/{w['adjustment']['id']}/reverse", json={"reason": "carton was dry"})
    assert rv.status_code == 200 and rv.json()["adjustment"]["direction"] == "IN"
    db.expire_all()
    assert stock() == 90

    # 5. return one strip of the later batch to the supplier
    db2 = db.query(Batch).filter(Batch.batch_no == "DB2").one()
    pr = client.post("/api/erp/purchase-returns", json={"batch_id": db2.id, "quantity": "1s", "reason": "short expiry offer"})
    assert pr.status_code == 200 and pr.json()["reference_no"] == "PR-000001"
    assert pr.json()["returns"][0]["value"] == "26.78"             # default value = cost of the strip incl. 5% GST (25.50 + 1.28)

    # 6. a manual bill changes no stock
    mb = client.post("/api/sales", json={"invoice_type": "MANUAL", "lines": [{"name": "Crepe bandage", "quantity": 1, "rate": "85"}]}).json()
    assert mb["invoice_no"].startswith("MB-")
    db.expire_all()
    assert stock() == 75

    # ---- everything agrees
    assert stock_ledger.reconcile(db) == []                        # batches == ledger, to the tablet
    moves = db.query(InventoryMovement).filter(InventoryMovement.item_id == dolo.id).all()
    assert sum(m.quantity for m in moves) == stock()
    hist = client.get("/api/erp/stock-history", params={"item": dolo.id}).json()
    assert hist["in"] - hist["out"] == stock() and hist["total"] == len(moves)
    assert {m["reference"] for m in hist["movements"]} >= {ref, "PR-000001", sale["invoice_no"]}

    reg = client.get("/api/erp/sales").json()
    assert reg["total"] == 2 and {s["type"] for s in reg["sales"]} == {"INVENTORY", "MANUAL"}

    batches = client.get(f"/api/erp/inventory/{dolo.id}").json()["batches"]
    assert {b["supplier"] for b in batches} == {"Micro Distributors"} and {b["purchase_ref"] for b in batches} == {ref}

    from datetime import datetime
    from app.services import report_generator
    today = datetime.now(report_generator.tz_for(db)).date().isoformat()   # the shop's day, not the machine's UTC date
    report = client.post("/reports/api/generate", json={"report": "item-sales", "parameters": {"period": "custom", "from": today, "to": today},
                                                      "columns": ["item", "quantity", "value", "cost", "profit"]}).json()
    rows = {r["item"]: r for r in report["rows"]}
    assert rows["DOLO 650MG TAB"]["cost"] is not None                # cost comes from the posted purchase
    assert rows["Crepe bandage"]["cost"] is None                     # manual bill: cost unknown, never zero
    assert report["totals"]["cost"] is None and "—" in report["text"]
