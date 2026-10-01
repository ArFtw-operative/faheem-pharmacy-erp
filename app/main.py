"""FastAPI application entrypoint for Faheem Pharmacy Management System."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import APP_BUILD, APP_NAME, APP_VERSION, STATIC_DIR
from app.database import SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pharmacy")


def _snapshot_interval_hours() -> float:
    from app.services.settings_service import get_int

    try:
        db = SessionLocal()
        try:
            return max(1, get_int(db, "snapshot_interval_hours", 6))
        finally:
            db.close()
    except Exception:  # pragma: no cover - defensive
        return 6


async def _snapshot_loop() -> None:
    """Whole-ERP snapshot every few hours (6 by default) and a daily restore drill of the newest one."""
    from app import snapshot

    while True:
        try:
            hours = await asyncio.to_thread(_snapshot_interval_hours)
            last = await asyncio.to_thread(snapshot.latest)
            age = None if not last else (datetime.now() - datetime.fromisoformat(last["created"])).total_seconds()
            if age is None or age >= hours * 3600:
                await asyncio.to_thread(snapshot.create, "scheduled")
            drill = await asyncio.to_thread(snapshot.last_drill)
            if not drill or (datetime.now() - datetime.fromisoformat(drill["at"])).total_seconds() >= 86400:
                await asyncio.to_thread(snapshot.drill)
        except Exception:  # pragma: no cover - logged, retried next tick
            logger.exception("Scheduled snapshot / restore drill failed; retrying in 5 minutes")
        await asyncio.sleep(300)


async def _whatsapp_worker() -> None:
    """Delivers queued WhatsApp invoices. Never part of a sale: billing does not wait for it."""
    from app.services.whatsapp import service as wa

    def tick(first: bool) -> None:
        db = SessionLocal()
        try:
            if first:
                wa.recover(db)
            wa.process_due(db)
        finally:
            db.close()

    def reconnect() -> None:
        db = SessionLocal()
        try:
            wa.keep_connected(db)
        finally:
            db.close()

    first, last_check = True, 0.0
    while True:
        try:
            await asyncio.to_thread(tick, first)
            first = False
            now = asyncio.get_running_loop().time()
            if now - last_check > 120:                 # a paired session that dropped (reboot) is started again
                last_check = now
                await asyncio.to_thread(reconnect)
        except Exception:  # pragma: no cover - logged, retried
            logger.exception("WhatsApp worker error; retrying")
        await asyncio.to_thread(wa.wait_for_work, 3.0)


def _startup_integrity() -> None:
    """Refuse to open a damaged database. Nothing is changed; the fix is a snapshot restore from the shell."""
    import sqlite3

    from app import snapshot

    from app.database import IS_SQLITE

    if not IS_SQLITE:            # PostgreSQL recovers its own WAL after a power cut; nothing to check here
        return
    path = snapshot.db_path()
    if not path.exists():
        return
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if check != "ok":
        raise RuntimeError(f"The database failed its start-up check ({check}). It was not changed. Restore the newest "
                           "good snapshot: scripts/snapshot.sh list, then scripts/snapshot.sh restore <id>.")


def _configure_units_of_measure() -> None:
    """Give every never-configured product its unit of measure (strips → tablets, bottles …)."""
    from app.services import packaging_service

    db = SessionLocal()
    try:
        changed = packaging_service.auto_configure_pending(db)
        db.commit()
        if changed:
            logger.info("Unit of measure configured automatically for %d product(s)", changed)
    except Exception:  # pragma: no cover - never block start-up
        db.rollback()
        logger.exception("Automatic unit-of-measure setup failed")
    finally:
        db.close()


def _snapshots_enabled() -> bool:
    return os.environ.get("PHARMACY_SKIP_MIGRATIONS") != "1" and os.environ.get("PHARMACY_BACKUPS", "1") != "0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    if _snapshots_enabled():
        _startup_integrity()
    logger.info("Initialising database…")
    try:
        init_db()        # guarded upgrade: snapshot → migrate → reconcile (restored if anything is off)
    except Exception:
        logger.critical("Start-up stopped — see the message above and docs/UPGRADES.md")
        raise
    _configure_units_of_measure()
    if _snapshots_enabled():
        tasks.append(asyncio.create_task(_snapshot_loop()))
    if os.environ.get("PHARMACY_SKIP_MIGRATIONS") != "1" and os.environ.get("PHARMACY_EXTERNAL_WORKER") != "1":
        tasks.append(asyncio.create_task(_whatsapp_worker()))
    logger.info("Startup complete")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if _snapshots_enabled():
            from app import snapshot

            try:
                last = snapshot.latest()
                if not last or (datetime.now() - datetime.fromisoformat(last["created"])).total_seconds() > 600:
                    snapshot.create("shutdown")
            except Exception:  # pragma: no cover
                logger.exception("Shut-down snapshot failed")


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
    from app.system import router as system_router
    app.include_router(system_router)

    @app.middleware("http")
    async def no_store_api(request: Request, call_next):
        """ERP data is live: no browser or proxy may serve an API answer from cache."""
        response = await call_next(request)
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        """Unauthenticated liveness probe for faheemctl.sh and monitoring."""
        from sqlalchemy import text

        try:
            db = SessionLocal()
            try:
                db.execute(text("SELECT 1"))
            finally:
                db.close()
            return JSONResponse({"app": "faheem-pharmacy", "version": APP_BUILD, "db": "ok"})
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"app": "faheem-pharmacy", "version": APP_VERSION, "db": str(exc)}, status_code=503)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.routers import (
        adjustments,
        assets,
        auth,
        erp,
        financials,
        inventory,
        purchases,
        reports,
        sales,
        customers,
        sales_history,
        stock_history,
        whatsapp,
    )

    for router in (
        auth.router,
        erp.router,
        assets.router,
        inventory.router,
        sales.router,
        purchases.router,
        stock_history.router,
        sales_history.router,
        customers.router,
        reports.router,
        financials.router,
        adjustments.router,
        whatsapp.router,
    ):
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        """The ERP workspace is the application."""
        return RedirectResponse("/app", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
        if exc.status_code == 403:
            from app.web import render

            db = SessionLocal()
            try:
                user = getattr(request.state, "user", None)
                return render(
                    request,
                    "error.html",
                    db,
                    {"code": 403, "message": exc.detail},
                    user=user,
                    status_code=403,
                )
            finally:
                db.close()
        if exc.status_code == 404:
            from app.web import render

            db = SessionLocal()
            try:
                return render(
                    request,
                    "error.html",
                    db,
                    {"code": 404, "message": "Page not found"},
                    user=getattr(request.state, "user", None),
                    status_code=404,
                )
            finally:
                db.close()
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    return app


app = create_app()
