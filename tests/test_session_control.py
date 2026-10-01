"""Session idle / lock-out controls (administrator-only)."""
from __future__ import annotations

from sqlalchemy import select

from app.models import Role
from app.services import user_service, settings_service
from tests.conftest import login


def test_login_is_username_only(client):
    page = client.get("/login")
    assert page.status_code == 200
    assert 'name="username"' in page.text
    assert 'name="email"' not in page.text
    assert "User Login" in page.text and "Forgot password?" in page.text
    assert "sign up" not in page.text.lower() and "register" not in page.text.lower()     # accounts come from the administrator


def test_force_login_shows_form_when_authenticated(client):
    login(client)
    resp = client.get("/login?force=1")
    assert resp.status_code == 200
    assert 'name="username"' in resp.text
    # Without force, an authenticated user is redirected home.
    assert client.get("/login").status_code == 200  # TestClient follows redirect
