"""Application configuration and OS-agnostic path handling.

All paths derive from the project root using pathlib so the exact same code
runs on Linux (dev) and Windows (production). Nothing here is hardcoded to a
machine; deployment-specific values live in the Settings table (DB) or, for a
few bootstrap values, in environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path

# Project root = parent of the `app` package.
BASE_DIR: Path = Path(__file__).resolve().parent.parent

DATA_DIR: Path = Path(os.environ.get("PHARMACY_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR: Path = Path(os.environ.get("PHARMACY_UPLOAD_DIR", BASE_DIR / "uploads"))
LOG_DIR: Path = Path(os.environ.get("PHARMACY_LOG_DIR", BASE_DIR / "logs"))
STATIC_DIR: Path = BASE_DIR / "app" / "static"
TEMPLATE_DIR: Path = BASE_DIR / "app" / "templates"

for _d in (DATA_DIR, UPLOAD_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = Path(os.environ.get("PHARMACY_DB", DATA_DIR / "pharmacy.db"))
DATABASE_URL: str = os.environ.get("PHARMACY_DATABASE_URL", f"sqlite:///{DB_PATH}")

# Business timezone used to derive the counter's business date server-side.
TIMEZONE: str = os.environ.get("PHARMACY_TIMEZONE", "Asia/Kolkata")

HOST: str = os.environ.get("PHARMACY_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("PHARMACY_PORT", "8000"))
SECRET_KEY: str = os.environ.get(
    "PHARMACY_SECRET_KEY", "faheem-pharmacy-dev-secret-change-in-production"
)
SESSION_COOKIE: str = "pharmacy_session"

APP_NAME: str = "Faheem Pharmacy"
APP_VERSION: str = "1.8.0"
GIT_COMMIT: str = os.environ.get("GIT_COMMIT", "development")
BUILD_DATE: str = os.environ.get("BUILD_DATE", "development")
# Full build version (e.g. 0.1.0.7) written by the Windows build; falls back to APP_VERSION.
try:
    APP_BUILD: str = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or APP_VERSION
except OSError:
    APP_BUILD = APP_VERSION

# Default expiry alert threshold in days (configurable in Settings).
DEFAULT_EXPIRY_THRESHOLD_DAYS: int = 90
