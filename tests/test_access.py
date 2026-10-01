"""Authentication and role boundaries in the ERP.

There are no admin screens: operator accounts are managed with scripts/manage.py.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import Role
from app.services import user_service
from tests.conftest import login


def _user(db, role_name, username):
    role = db.scalar(select(Role).where(Role.name == role_name))
    user, temp = user_service.create_user(db, username=username, full_name=username.title(), role_id=role.id,
                                          must_change_password=False)
    db.commit()
    return user, temp


def test_sales_staff_can_bill_but_not_see_financials(client, db):
    _, temp = _user(db, "Sales Staff", "cashier")
    assert login(client, "cashier", temp).status_code == 303
    assert client.get("/app/pos").status_code == 200
    boot = client.get("/app").text
    assert '"sales.create": true' in boot and '"reports.sales": false' in boot
    assert client.post("/reports/api/generate", json={"report": "profit", "parameters": {}}).status_code == 403
    assert client.get("/api/erp/customers?q=1").status_code == 200


def test_disabled_account_cannot_login(client, db):
    user, temp = _user(db, "Pharmacist", "temp1")
    user_service.set_active(db, user, False)
    db.commit()
    assert login(client, "temp1", temp).status_code == 403


def test_legacy_permissions_are_removed_and_customer_create_kept(db):
    from app.models import Permission

    codes = set(db.scalars(select(Permission.code)))
    # the old UI's settings / user-admin permissions stay gone; the ERP's Settings tab has its own (admin only)
    assert not {c for c in codes if c.split(".")[0] in ("crm", "dashboard", "settings", "users")} - {"settings.manage"}
    staff = db.scalar(select(Role).where(Role.name == "Sales Staff"))
    assert "customers.create" in staff.permission_codes()
