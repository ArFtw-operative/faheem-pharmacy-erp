"""End-to-end page rendering smoke tests (administrator)."""
from __future__ import annotations

import pytest

from app.services import inventory_service as inv
from app.services import sales_service
from tests.conftest import login

PAGES = [
    "/app", "/app/pos", "/app/inventory", "/app/purchases", "/app/sales", "/app/history", "/reports", "/app/adjustments",
]
REMOVED = ["/counter", "/expiry", "/inventory/new", "/inventory/import", "/invoices", "/review", "/purchases", "/purchase-returns", "/pos", "/crm", "/customer-insights", "/admin", "/admin/roles", "/settings", "/backups", "/api/weather"]


@pytest.mark.parametrize("path", PAGES)
def test_erp_pages_render(client, path):
    login(client)
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"


@pytest.mark.parametrize("path", REMOVED)
def test_legacy_pages_are_gone(client, path):
    """The CRM, dashboard, admin and settings product areas were removed, not hidden."""
    login(client)
    assert client.get(path, follow_redirects=False).status_code == 404


def test_root_opens_the_erp(client):
    login(client)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/app"
