"""PostgreSQL deployment gates. No schema writes happen in the API process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.config import BASE_DIR, DATABASE_URL
from app.database import engine


def config() -> Config:
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.attributes["database_url"] = DATABASE_URL
    return cfg


def schema_state() -> dict:
    heads = sorted(ScriptDirectory.from_config(config()).get_heads())
    with engine.connect() as con:
        current = sorted(con.execute(text("SELECT version_num FROM alembic_version")).scalars()) if inspect(con).has_table("alembic_version") else []
    return {"state": "current" if current == heads else "pending", "current": current, "target": heads}


def migrate() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("The production migration job requires PostgreSQL")
    if os.environ.get("PHARMACY_PRODUCTION") == "1":
        from app.config import SECRET_KEY
        if len(SECRET_KEY) < 32 or "dev-secret" in SECRET_KEY:
            raise RuntimeError("A unique installation session secret is required")
    # PostgreSQL transactional DDL makes a failed Alembic batch atomic. The host
    # updater owns the mandatory full backup and the stopped-writer boundary.
    with engine.connect() as con:
        con.execute(text("SELECT pg_advisory_lock(73429618)"))
        try:
            # a brand-new database is built from the models and stamped (the historical chain is SQLite-only)
            from app.services import upgrade_service
            upgrade_service.migrate(DATABASE_URL)
            os.environ["PHARMACY_EXTERNAL_MIGRATIONS"] = "1"       # this job *is* the migration step
            # first install only: the owner account typed into the installer (passed for this one run,
            # never stored); seeding creates it only while the database has no users at all
            from app import seed
            owner = {"username": os.environ.get("FAHEEM_OWNER_USERNAME"), "full_name": os.environ.get("FAHEEM_OWNER_FULL_NAME"),
                     "password": os.environ.get("FAHEEM_OWNER_PASSWORD")}
            seed.DEFAULT_ADMIN.update({k: v for k, v in owner.items() if v})
            from app.database import init_db
            init_db()
        finally:
            con.execute(text("SELECT pg_advisory_unlock(73429618)"))
    if schema_state()["state"] != "current":
        raise RuntimeError("Migration head verification failed")


def smoke() -> dict:
    """Probe actual reads/writes/search/PDF rendering; leave no business records."""
    from decimal import Decimal
    from app.services.invoice_render import build_invoice_pdf
    from app.models import Item
    from sqlalchemy import select
    with engine.connect() as con:
        transaction = con.begin()
        try:
            con.execute(text("CREATE TEMP TABLE faheem_deployment_probe (value integer) ON COMMIT DROP"))
            con.execute(text("INSERT INTO faheem_deployment_probe VALUES (42)"))
            assert con.execute(text("SELECT value FROM faheem_deployment_probe")).scalar_one() == 42
            con.execute(select(Item.id).limit(1)).all()
        finally:
            transaction.rollback()
    # Render the production PDF engine with a transient sale that is never added to a session.
    from datetime import datetime
    from app.models import Sale
    sale = Sale(invoice_no="DEPLOYMENT-CHECK", sale_date=datetime.now(), customer_type="WALK_IN",
                subtotal=Decimal(0), discount=Decimal(0), round_off=Decimal(0), total=Decimal(0), payment_mode="CASH")
    pdf = build_invoice_pdf(sale, {"name": "Faheem Pharmacy"})
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("Invoice rendering failed")
    state = schema_state()
    if state["state"] != "current":
        raise RuntimeError("Schema is not at this image's migration head")
    path = Path(os.environ.get("PHARMACY_UPLOAD_DIR", "/var/lib/faheem-erp/uploads")) / ".deployment-probe"
    path.write_text("persistence check")
    path.unlink()
    return {"database": "ok", "write_read": "ok", "products": "ok", "invoice": "ok", "schema": state}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["migrate", "schema", "smoke"])
    action = parser.parse_args().action
    print(json.dumps({"migrate": migrate, "schema": schema_state, "smoke": smoke}[action]()))
