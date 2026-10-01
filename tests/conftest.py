"""Pytest fixtures.

Environment is configured BEFORE importing the app so the module-level engine
points at an isolated temp database and the locally extracted Tesseract build.
"""
from __future__ import annotations

import os
import tempfile
import secrets
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pharmacy_tests_"))
os.environ.setdefault("PHARMACY_ADMIN_PASSWORD", secrets.token_urlsafe(24))
# PHARMACY_TEST_DATABASE_URL=postgresql+psycopg://… runs the whole suite on PostgreSQL (CI does both)
os.environ["PHARMACY_DATABASE_URL"] = os.environ.get("PHARMACY_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'test.db'}"
os.environ["PHARMACY_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["PHARMACY_DATA_DIR"] = str(_TMP / "data")
os.environ["PHARMACY_LOG_DIR"] = str(_TMP / "logs")
os.environ["PHARMACY_SKIP_MIGRATIONS"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, reset_db_for_tests  # noqa: E402
from app.seed import DEFAULT_ADMIN
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db_for_tests()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(client: TestClient, username: str = DEFAULT_ADMIN["username"], password: str = DEFAULT_ADMIN["password"]):
    """Log in like a person: on a first login, scan (read) the key, type the current code, continue."""
    import re

    from app.services import mfa

    r = client.post("/login", data={"username": username, "password": password, "next": "/"}, follow_redirects=False)
    if r.status_code == 303 and r.headers.get("location") == "/login/2fa/setup":
        page = client.get("/login/2fa/setup").text
        secret = re.search(r'class="secret">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")
        client.post("/login/2fa/setup", data={"code": mfa.code_at(secret, mfa.current_step())})
        r = client.post("/login/2fa/done", follow_redirects=False)
    return r
