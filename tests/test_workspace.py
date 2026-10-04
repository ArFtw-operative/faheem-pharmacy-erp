"""Workspace snapshots: open tabs and unfinished bills survive a crash, power cut or reboot."""
from __future__ import annotations

import json

from app.models import Role
from app.services import inventory_service as inv
from app.services import sales_service, user_service
from tests.conftest import login

DATA = {"tabs": [{"id": "t1", "module": "pos", "params": {}, "title": "POS · Bill 1"}], "active": "t1",
        "states": {"t1": {"slot": 1, "lines": [{"manual": True, "name": "Crepe bandage", "qty": 2, "rate": 85}], "requestId": "r-123"}}}


def test_snapshot_is_saved_per_counter_and_restored(client, db):
    login(client)
    assert client.get("/api/erp/workspace?terminal=counter-1").json()["data"] is None
    r = client.put("/api/erp/workspace", json={"terminal": "counter-1", "data": DATA})
    assert r.status_code == 200 and r.json()["saved_at"]
    got = client.get("/api/erp/workspace?terminal=counter-1").json()
    assert got["data"] == DATA and got["other_terminal"] is False
    other = {**DATA, "active": "t9"}
    client.put("/api/erp/workspace", json={"terminal": "counter-2", "data": other})
    assert client.get("/api/erp/workspace?terminal=counter-1").json()["data"]["active"] == "t1"        # counters apart
    busy = client.get("/api/erp/workspace?terminal=never-seen").json()        # counter-2 is in use: never copied
    assert busy["data"] is None and busy["other_terminal_active"] is True
    from datetime import timedelta
    from app.models import WorkspaceSnapshot
    from app.utils import utcnow
    for snap in db.query(WorkspaceSnapshot).all():
        snap.saved_at = utcnow() - timedelta(hours=1)
    db.commit()
    lost = client.get("/api/erp/workspace?terminal=never-seen").json()        # quiet for an hour: id lost → latest
    assert lost["data"]["active"] == "t9" and lost["other_terminal"] is True


def test_beacon_post_and_limits(client, db):
    login(client)
    r = client.post("/api/erp/workspace", content=json.dumps({"terminal": "c1", "data": DATA}), headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert client.put("/api/erp/workspace", json={"terminal": "", "data": DATA}).status_code == 400
    assert client.put("/api/erp/workspace", json={"terminal": "c1", "data": "x" * 10}).status_code == 400
    big = {"tabs": [], "states": {"t1": {"blob": "x" * 1_100_000}}}
    assert client.put("/api/erp/workspace", json={"terminal": "c1", "data": big}).status_code == 413


def test_snapshots_are_per_user(client, db):
    login(client)
    client.put("/api/erp/workspace", json={"terminal": "c1", "data": DATA})
    role = db.query(Role).filter(Role.name != "Administrator").first()
    user_service.create_user(db, username="cashier2", full_name="Cashier Two", role_id=role.id, password="Cashier@2026",
                             must_change_password=False)
    db.commit()
    client.post("/logout")
    client.cookies.clear()
    login(client, "cashier2", "Cashier@2026")
    assert client.get("/api/erp/workspace?terminal=c1").json()["data"] is None             # never another user's work


def test_bill_completed_before_a_crash_is_recognised(client, db):
    login(client)
    item = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S")
    inv.add_or_update_batch(db, item, batch_no="B1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}], client_request_id="req-abc")
    db.commit()
    found = client.get("/api/erp/sales/by-request/req-abc")
    assert found.status_code == 200 and found.json()["invoice_no"] == sale.invoice_no
    assert client.get("/api/erp/sales/by-request/req-unknown").status_code == 404


def test_test_data_reset_clears_snapshots(client, db):
    from app.models import WorkspaceSnapshot
    from app.services import data_reset

    login(client)
    client.put("/api/erp/workspace", json={"terminal": "c1", "data": DATA})
    data_reset.wipe(db, keep_customers=True)
    db.commit()
    assert db.query(WorkspaceSnapshot).count() == 0
