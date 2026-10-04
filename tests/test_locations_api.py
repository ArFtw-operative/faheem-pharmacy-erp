"""Rack / box locations through the application: inventory, POS, purchases, import, export,
reports (reconciliation), permissions and context-aware bulk status."""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.models import Batch, Rack
from app.services import inventory_service as inv
from app.services import location_service as loc
from app.services import purchasing
from tests.conftest import login
from tests.test_access import _user
from tests.test_locations import boxes_on, product, rack
from tests.test_purchasing import draft


def test_inventory_lists_filters_searches_and_sorts_by_location(client, db):
    login(client)
    boxes_on(db)
    a, b = rack(db, code="R-A03", name="Fever & Pain"), rack(db, code="R-B01", name="Syrups")
    bx = loc.create_box(db, a, code="B02", name="Middle")
    dolo, calpol, loose = product(db, "DOLO 650 TAB"), product(db, "CALPOL TAB"), product(db, "ZINC TAB")
    loc.assign(db, [dolo.id], a.id, bx.id)
    loc.assign(db, [calpol.id], b.id)
    db.commit()
    rows = {r["name"]: r for r in client.get("/api/erp/inventory").json()["rows"]}
    assert (rows["DOLO 650 TAB"]["rack"], rows["DOLO 650 TAB"]["rack_name"], rows["DOLO 650 TAB"]["box"]) == ("R-A03", "Fever & Pain", "B02")
    assert rows["ZINC TAB"]["rack"] == "" and rows["ZINC TAB"]["location"] == ""
    names = lambda url: [r["name"] for r in client.get(url).json()["rows"]]
    assert names("/api/erp/inventory?location=unassigned") == ["ZINC TAB"]
    assert names(f"/api/erp/inventory?location=rack:{a.id}") == ["DOLO 650 TAB"]
    assert names(f"/api/erp/inventory?location=box:{bx.id}") == ["DOLO 650 TAB"]
    assert names("/api/erp/inventory?location=nobox") == ["CALPOL TAB"]
    assert names("/api/erp/inventory?q=R-A03") == ["DOLO 650 TAB"]            # a rack code finds what is there
    assert names("/api/erp/inventory?sort=rack") == ["DOLO 650 TAB", "CALPOL TAB", "ZINC TAB"]
    assert client.get("/api/erp/inventory?location=bogus").status_code == 400
    assert client.get(f"/api/erp/inventory/{dolo.id}").json()["location"] == "R-A03 / B02"


def test_pos_search_shows_rack_and_box_and_finds_by_code(client, db):
    login(client)
    boxes_on(db)
    a = rack(db, code="R-A03", name="Fever & Pain")
    bx = loc.create_box(db, a, code="B02")
    dolo = product(db, "DOLO 650 TAB", stock=30)
    other = product(db, "AZITHRAL 500 TAB", stock=6)
    loc.assign(db, [dolo.id, other.id], a.id, bx.id)
    db.commit()
    hit = client.get("/api/erp/pos/search?q=dolo").json()["items"][0]
    assert hit["location"] == {"rack": "R-A03", "rack_name": "Fever & Pain", "box": "B02", "box_name": ""}
    assert hit["rack"] == "R-A03 / B02"
    by_code = {i["name"] for i in client.get("/api/erp/pos/search?q=R-A03").json()["items"]}
    assert by_code == {"DOLO 650 TAB", "AZITHRAL 500 TAB"}
    assert {i["name"] for i in client.get("/api/erp/pos/search?q=R-A03/B02").json()["items"]} == by_code
    loc.set_config(db, {"pos_show_location": False, "pos_search_by_location": False})
    db.commit()
    assert client.get("/api/erp/pos/search?q=dolo").json()["items"][0]["location"] is None
    assert client.get("/api/erp/pos/search?q=R-A03").json()["items"] == []


def test_out_of_stock_product_still_shows_its_rack_in_pos(client, db):
    login(client)
    a = rack(db, code="R-A03")
    item = product(db, "EMPTY SHELF TAB")
    loc.assign(db, [item.id], a.id)
    db.commit()
    hit = client.get("/api/erp/pos/search?q=EMPTY").json()["items"][0]
    assert hit["stock"] == 0 and hit["location"]["rack"] == "R-A03"


def test_assign_api_bulk_and_permissions(client, db):
    a = rack(db)
    items = [product(db, f"BULK {k} TAB") for k in range(4)]
    _, temp = _user(db, "Sales Staff", "cashier")
    assert login(client, "cashier", temp).status_code == 303
    r = client.post("/api/erp/locations/assign", json={"item_ids": [items[0].id], "rack_id": a.id})
    assert r.status_code == 403                                      # sales staff see racks, never move them
    assert client.get("/api/erp/racks").status_code == 200
    client.post("/logout")
    login(client)
    out = client.post("/api/erp/locations/assign", json={"item_ids": [i.id for i in items], "rack_id": a.id}).json()
    assert (out["requested"], out["processed"], out["skipped"], out["failed"]) == (4, 4, [], [])
    again = client.post("/api/erp/locations/assign", json={"item_ids": [items[0].id], "rack_id": a.id}).json()
    assert again["processed"] == 0 and again["skipped"][0]["reason"].startswith("already")
    bad = client.post("/api/erp/locations/assign", json={"item_ids": [items[0].id], "rack_id": 99999})
    assert bad.status_code == 400 and bad.json()["detail"] == "Rack not found"
    hist = client.get(f"/api/erp/inventory/{items[0].id}/location").json()
    assert hist["current"]["rack"] == "R-A01" and hist["history"][0]["type"] == "ASSIGNED"


def test_rack_disable_with_contents_needs_a_destination(client, db):
    login(client)
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db, stock=9)
    loc.assign(db, [item.id], a.id)
    db.commit()
    r = client.post(f"/api/erp/racks/{a.id}/status", json={"active": False})
    assert r.status_code == 409 and r.json()["detail"]["products"] == 1 and r.json()["detail"]["units"] == 9
    r = client.post(f"/api/erp/racks/{a.id}/status", json={"active": False, "move_to_rack": b.id})
    assert r.status_code == 200 and r.json()["moved"]["processed"] == 1 and r.json()["rack"]["active"] is False


def test_purchase_receiving_applies_the_confirmed_rack_and_suggests(db):
    a = rack(db, code="R-A03")
    item = product(db, "DOLO 650 TAB")
    p = draft(db, ",DOLO 650 TAB,15S,D239,Aug-2028,2,,30,32,60")
    line = p.items[0]
    hint = purchasing.location_suggestions(db, p)[line.id]
    assert hint["state"] == "NONE"
    loc.set_category_defaults(db, a, ["PHARMA"])
    assert purchasing.location_suggestions(db, p)[line.id]["state"] == "SUGGESTED"
    assert purchasing.set_location(db, p, [line.id], None, suggested=True)["changed"] == 1
    assert purchasing.location_suggestions(db, p)[line.id]["state"] == "CONFIRMED"
    assert not loc.current(db, [item.id])                           # confirmed on the line, applied only on posting
    purchasing.post(db, p, accept_warnings=True)
    here = loc.current(db, [item.id])[item.id]
    assert here.rack_code == "R-A03"
    ev = loc.item_history(db, item.id)[0]
    assert ev["source"] == "PURCHASE" and ev["reference"] == p.reference_no


def test_rack_can_be_required_before_posting(db):
    loc.set_config(db, {"location_require_rack_on_receipt": True})
    a = rack(db)
    product(db, "DOLO 650 TAB")
    p = draft(db, ",DOLO 650 TAB,15S,D239,Aug-2028,2,,30,32,60")
    db.commit()
    try:
        purchasing.post(db, p, accept_warnings=True)
        raise AssertionError("posted without a rack")
    except purchasing.PurchaseError as exc:
        assert exc.code == "LOCATION_REQUIRED"
    db.rollback()
    p = db.get(type(p), p.id)
    purchasing.set_location(db, p, [p.items[0].id], a.id)
    purchasing.post(db, p, accept_warnings=True)
    assert p.status == "POSTED"


def test_a_disabled_rack_chosen_on_a_line_stops_posting(db):
    a = rack(db)
    product(db, "DOLO 650 TAB")
    p = draft(db, ",DOLO 650 TAB,15S,D239,Aug-2028,2,,30,32,60")
    purchasing.set_location(db, p, [p.items[0].id], a.id)
    loc.set_rack_status(db, a, False)
    try:
        purchasing.post(db, p, accept_warnings=True)
        raise AssertionError("posted into a disabled rack")
    except purchasing.PurchaseError as exc:
        assert exc.code == "LOCATION" and "disabled" in str(exc)


def test_import_assigns_known_racks_and_queues_typos(client, db):
    login(client)
    rack(db, code="R-A01")
    sheet = ("Product Name,Pack,Batch,Expiry,Qty,MRP,Rack Code\n"
             "ALPHA TAB,10S,A1,05/2029,2,20,R-A01\nBETA TAB,10S,B1,05/2029,1,20,R-AO1\n").encode()
    out = client.post("/api/erp/inventory/import", files={"file": ("stock.csv", io.BytesIO(sheet), "text/csv")}).json()
    assert out["located"] == 1 and out["created"] == 2
    review = out["location_review"]
    assert len(review) == 1 and review[0]["problem"] == "Unknown rack R-AO1" and review[0]["suggestion"] == "R-A01"
    assert db.scalar(select(func.count()).select_from(Rack)) == 1


def test_export_includes_location(client, db):
    login(client)
    a = rack(db, code="R-A01", name="Antibiotics")
    item = product(db, "AUGMENTIN 625 TAB", stock=10)
    loc.assign(db, [item.id], a.id)
    db.commit()
    body = client.get("/inventory/export/csv").content.decode("utf-8-sig")
    header, row = body.splitlines()[0].split(","), [l for l in body.splitlines() if "AUGMENTIN" in l][0].split(",")
    assert "rack_code" in header and row[header.index("rack_code")] == "R-A01" and row[header.index("rack_name")] == "Antibiotics"


def test_rack_report_reconciles_with_current_stock(client, db):
    login(client)
    a, b = rack(db), rack(db, code="R-B01")
    items = [product(db, f"REC {k} TAB", stock=5 * (k + 1)) for k in range(4)]
    loc.assign(db, [items[0].id], a.id)
    loc.assign(db, [items[1].id, items[2].id], b.id)
    db.commit()
    gen = lambda rid, **p: client.post("/reports/api/generate", json={"report": rid, "parameters": p}).json()
    racked = gen("rack-inventory")
    stock = gen("current-stock")
    assert int(racked["totals"]["quantity"]) == int(stock["totals"]["quantity"]) == 50
    by = {}
    for r in racked["rows"]:
        by[r["rack"]] = by.get(r["rack"], 0) + r["quantity"]
    assert by == {"R-A01": 5, "R-B01": 25, "Unassigned": 20}
    assert "Unassigned 20" in racked["note"]
    only_b = gen("rack-inventory", racks=str(b.id))
    assert int(only_b["totals"]["quantity"]) == 25
    hist = gen("rack-history", rack=str(b.id), period="last_7")
    assert hist["rows"][0]["units"] == 25 and len(hist["rows"]) == 7


def test_bulk_status_is_context_aware_and_writes_only_changes(client, db):
    login(client)
    on, off = product(db, "ON TAB"), product(db, "OFF TAB")
    inv.deactivate_item(db, off)
    db.commit()
    out = client.post("/api/erp/inventory/bulk/status", json={"action": "enable", "ids": [on.id, off.id]}).json()
    assert out["done"] == [off.id] and [u["id"] for u in out["unchanged"]] == [on.id] and out["requested"] == 2


def test_inventory_starts_with_filter(client, db):
    login(client)
    for name in ("AMOX TAB", "azee 500 TAB", "BETADINE", "3M TAPE"):
        product(db, name)
    names = lambda u: sorted(r["name"] for r in client.get(u).json()["rows"])
    assert names("/api/erp/inventory?letter=A") == ["AMOX TAB", "azee 500 TAB"]
    assert names("/api/erp/inventory?letter=b") == ["BETADINE"]
    assert names("/api/erp/inventory?letter=%23") == ["3M TAPE"]
    assert client.get("/api/erp/inventory?letter=AB").status_code == 400


def test_a_new_product_created_on_posting_lands_in_the_rack_chosen_on_its_line(db):
    from app.models import Item
    a = rack(db, code="R-NEW")
    p = draft(db, ",BRAND NEW SOAP 75G,75G,S1,Aug-2028,2,,30,40,60")
    line = p.items[0]
    purchasing.correct(db, p, line, {"new_product": True, "category": "FMCG"}) if not line.new_product else None
    purchasing.set_location(db, p, [line.id], a.id)
    purchasing.post(db, p, accept_warnings=True)
    item = db.get(Item, line.item_id)
    assert item is not None and loc.current(db, [item.id])[item.id].rack_code == "R-NEW"
