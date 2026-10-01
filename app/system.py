"""Readiness and administrator-only appliance information. No Docker socket."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import APP_VERSION, BUILD_DATE, GIT_COMMIT, DATA_DIR
from app.database import engine
from app.deps import require_permission

router = APIRouter()
STARTED = time.monotonic()
_CORS = {"Access-Control-Allow-Origin": "*"}


def version() -> dict:
    from app.production import schema_state
    schema = schema_state()
    return {"APP_VERSION": APP_VERSION, "GIT_COMMIT": GIT_COMMIT,
            "BUILD_DATE": BUILD_DATE, "DATABASE_SCHEMA_VERSION": schema["current"], "schema": schema}


@router.get("/health/live")
def live():
    return {"status": "alive"}


@router.get("/health/ready")
def ready(response: Response = None):
    # the kiosk's local "Starting services…" page polls this from a data: URL (origin "null");
    # the answer carries no data, so any origin may read it
    if response is not None:
        response.headers["Access-Control-Allow-Origin"] = "*"
    try:
        with engine.connect() as con:
            con.execute(text("SELECT 1 FROM settings LIMIT 1"))
        if os.environ.get("PHARMACY_EXTERNAL_MIGRATIONS") == "1":
            from app.production import schema_state
            if schema_state()["state"] != "current":
                return JSONResponse({"status": "not ready", "database": "schema mismatch"}, status_code=503, headers=_CORS)
        return {"status": "ready", "database": "ok", "commit": GIT_COMMIT}
    except Exception:
        return JSONResponse({"status": "not ready", "database": "unavailable"}, status_code=503, headers=_CORS)


@router.get("/api/v1/system/version", dependencies=[Depends(require_permission("settings.manage"))])
def system_version():
    return version()


@router.get("/api/erp/settings/system", dependencies=[Depends(require_permission("settings.manage"))])
def information():
    from app.services.whatsapp.service import provider
    state_path = Path(os.environ.get("PHARMACY_APPLIANCE_STATE", "/run/faheem-erp/status.json"))
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    usage = shutil.disk_usage(DATA_DIR)
    heartbeat = DATA_DIR / "worker-heartbeat"
    worker_ok = heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 60
    return {**version(), "database": ready(), "worker": "healthy" if worker_ok else "unavailable",
            "whatsapp": provider().connection().state, "disk_percent": round(usage.used / usage.total * 100),
            "uptime_seconds": int(time.monotonic() - STARTED), "timezone": os.environ.get("PHARMACY_TIMEZONE", "Asia/Kolkata"),
            "next_maintenance": "05:00 Asia/Kolkata", "appliance": state}
