"""Two counters on one ERP (the shop PC and a browser on another PC): change counters that let open
screens refresh, workspaces that do not collide, and races decided by the database."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import WorkspaceSnapshot
from app.services import location_service as loc
from app.services import parking_service, sales_service, sync_service
from app.utils import utcnow
from tests.conftest import login
from tests.test_locations import product, rack
from tests.test_purchasing import draft


def v(db):
    db.expire_all()
    return sync_service.versions(db)


def test_changes_bump_their_area_counters(db):
    before = v(db)
    item = product(db, stock=20)                                   # products + opening stock
    after = v(db)
    assert after["inventory"] > before["inventory"] and after["sales"] == before["sales"]
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 5}]); db.commit()
    nxt = v(db)
    assert nxt["sales"] > after["sales"] and nxt["inventory"] > after["inventory"]   # a sale moves stock too
    r = rack(db)
    loc.assign(db, [item.id], r.id); db.commit()                   # (bulk update statements included)
    assert v(db)["locations"] > nxt["locations"]
    p = draft(db, ",DOLO 650 TAB,15S,D1,Aug-2028,2,,30,32,60"); db.commit()
    assert v(db)["purchases"] > nxt["purchases"]


def test_rolled_back_work_bumps_nothing(db):
    before = v(db)
    product(db, name="ROLLED BACK TAB")                            # commits; then a change that is rolled back
    mid = v(db)
    s = SessionLocal()
    try:
        from app.models import Item
        it = s.query(Item).first(); it.name = "CHANGED"; s.flush(); s.rollback()
    finally:
        s.close()
    assert v(db) == mid and mid["inventory"] > before["inventory"]


def test_sync_endpoint(client, db):
    login(client)
    out = client.get("/api/erp/sync").json()["versions"]
    assert set(out) == set(sync_service.AREAS)


def test_the_page_starts_live_refresh_from_its_own_versions(client, db):
    """A change made before the first refresh round (seconds after opening) must still be noticed."""
    import json
    import re

    login(client)
    page = client.get("/app/inventory").text
    boot = json.loads(re.search(r"window\.ERP_BOOT = (\{.*?\});</script>", page).group(1))
    assert boot["sync"] == client.get("/api/erp/sync").json()["versions"]
    assert client.post("/api/erp/categories", json={"name": "Sync Probe"}).status_code == 200
    now = client.get("/api/erp/sync").json()["versions"]
    assert now["masters"] > boot["sync"]["masters"] and now["inventory"] > boot["sync"]["inventory"]


def test_a_counter_in_use_keeps_its_tabs_a_quiet_one_hands_them_over(client, db):
    login(client)
    client.put("/api/erp/workspace", json={"terminal": "pc-shop", "data": {"tabs": [{"module": "pos"}], "states": {}}})
    other = client.get("/api/erp/workspace?terminal=browser-2").json()
    assert other["data"] is None and other["other_terminal_active"] is True       # the shop PC is in use: start clean
    snap = db.query(WorkspaceSnapshot).filter_by(terminal="pc-shop").one()
    snap.saved_at = utcnow() - timedelta(hours=2); db.commit()
    other = client.get("/api/erp/workspace?terminal=browser-2").json()
    assert other["data"]["tabs"][0]["module"] == "pos" and other["other_terminal"] is True   # replaced PC: work handed over


def test_two_counters_resume_the_same_parked_bill_one_wins(db):
    item = product(db, stock=10)
    from app.services import customer_service
    cust = customer_service.create_customer(db, name="Asha", mobile="9876543210")
    parked = parking_service.park_sale(db, payload={"cart": [{"item_id": item.id, "quantity": 1}]}, customer_id=cust.id)
    db.commit()
    a, b = SessionLocal(), SessionLocal()
    try:
        pa, pb = a.get(type(parked), parked.id), b.get(type(parked), parked.id)     # both screens loaded it as PARKED
        parking_service.resume_parked(a, pa); a.commit()
        with pytest.raises(parking_service.ParkedClaimConflict):
            parking_service.resume_parked(b, pb)
        b.rollback()
    finally:
        a.close(); b.close()


def test_the_same_user_works_on_two_pcs_at_once(db):
    product(db, name="SHARED TAB", stock=10)
    with TestClient(app) as pc1, TestClient(app) as pc2:
        login(pc1); login(pc2)
        assert pc1.get("/api/erp/inventory?q=SHARED").json()["total"] == 1
        assert pc2.get("/api/erp/inventory?q=SHARED").json()["total"] == 1
        v1 = pc1.get("/api/erp/sync").json()["versions"]
        r = pc2.post("/api/erp/racks", json={"code": "R-SYNC", "name": "From PC 2"})
        assert r.status_code == 200
        v2 = pc1.get("/api/erp/sync").json()["versions"]                          # PC 1 learns something changed
        assert v2["locations"] > v1["locations"]
        assert pc1.get("/api/erp/racks").json()["racks"][0]["code"] == "R-SYNC"
