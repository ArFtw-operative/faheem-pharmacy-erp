"""Snapshots on PostgreSQL: dump + fingerprint of one moment, exact restore, drill in a scratch database.
Runs when PHARMACY_TEST_PG_URL points at an empty PostgreSQL database (CI provides one)."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import snapshot

PG = os.environ.get("PHARMACY_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not PG.startswith("postgres"), reason="PHARMACY_TEST_PG_URL not set")


@pytest.fixture
def pg():
    from app.database import Base
    import app.models  # noqa: F401

    engine = create_engine(PG)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('a7c9e1f3b5d8')"))
    with Session(engine) as db:
        from app.services.category_service import DEFAULTS as DEFAULT_CATEGORIES
        from app.models import Category
        for code in DEFAULT_CATEGORIES:
            db.add(Category(code=code, name=code.title()))
        db.commit()
        from app.services import inventory_service as inv, sales_service
        item = inv.create_item(db, name="DOLO 650 TAB", pack_size="15 S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)
        inv.add_or_update_batch(db, item, batch_no="D1", quantity=4, unit="PACK", movement_type="OPENING_STOCK", mrp="30", purchase_rate="20")
        db.commit()
        sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}])
        db.commit()
    yield snapshot.PostgresDB(PG)
    engine.dispose()


def test_postgres_snapshot_restores_exactly_and_drills(pg, tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(store))
    monkeypatch.delenv("PHARMACY_SNAPSHOT_MIRROR", raising=False)
    up = tmp_path / "uploads"; up.mkdir(); (up / "logo.png").write_bytes(b"logo")
    before = snapshot.fingerprint(pg)
    assert before["count:sales"] == 1 and before["check:ledger_mismatched_batches"] == 0
    m = snapshot.create("manual", db=pg, uploads=up, store=store)
    assert m["engine"] == "postgresql" and m["fingerprint"] == before and (store / m["id"] / "database.dump").exists()
    assert m["schema"] == ["a7c9e1f3b5d8"]
    pg.execute("DELETE FROM sale_payments"); pg.execute("DELETE FROM inventory_movements WHERE movement_type = 'SALE'")
    assert snapshot.fingerprint(pg) != before
    out = snapshot.restore(m["id"], db=pg, uploads=up, store=store)
    assert snapshot.fingerprint(pg) == before and out["safety_snapshot"]
    assert snapshot.verify(out["safety_snapshot"], store)["fingerprint"] != before        # the broken state was kept
    assert snapshot.drill(m["id"], store, db=pg)["ok"]
    left = pg.query("SELECT datname FROM pg_database WHERE datname LIKE %s", (pg.name + "_drill_%",))
    assert left == []                                                                     # scratch database dropped
    with pytest.raises(snapshot.SnapshotError, match="sqlite"):
        snapshot.restore(m["id"], db=tmp_path / "x.db", uploads=up, store=store)        # engines never mixed


def test_postgres_fresh_install_upgrade_rehearsal_and_rollback(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text

    from app.services import upgrade_service as up

    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("PHARMACY_SNAPSHOT_MIRROR", raising=False)
    engine = create_engine(PG)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    db = snapshot.PostgresDB(PG)
    assert up.state(db)["state"] == "fresh"
    assert up.upgrade(db)["status"] == "installed" and up.state(db)["state"] == "current"
    # an installation one release behind: the last migration's columns missing, stamped one step back
    head, = up.code_heads()
    from alembic.script import ScriptDirectory
    prev = ScriptDirectory.from_config(up._alembic_config()).get_revision(head).down_revision
    db.execute('ALTER TABLE sale_items DROP COLUMN "item_code"')       # the newest migration's column
    db.execute("UPDATE alembic_version SET version_num = ?", (prev,))
    db.execute("INSERT INTO categories (code, name, is_active, sort_order, created_at) VALUES ('MEDICINE', 'Medicine', true, 10, now())")
    assert up.state(db)["state"] == "pending"
    rehearsal = up.check(db)
    assert rehearsal["migrations"] == [head] and rehearsal["problems"] == []          # the migration runs on PostgreSQL
    assert up.state(db)["state"] == "pending"                                          # rehearsal never touched it
    real = up.migrate

    def broken(url=None):
        real(url)
        snapshot.PostgresDB(url).execute("DELETE FROM categories")
    monkeypatch.setattr(up, "migrate", broken)
    import pytest as _pytest
    with _pytest.raises(up.UpgradeError, match="restored"):
        up.upgrade(db)
    assert up.state(db)["state"] == "pending" and db.query("SELECT count(*) FROM categories")[0][0] == 1
    monkeypatch.setattr(up, "migrate", real)
    out = up.upgrade(db)
    assert out["status"] == "upgraded" and up.state(db)["state"] == "current"
    assert [h["status"] for h in up.history(db)][:1] == ["UPGRADED"]


@pytest.mark.parametrize("fixture", ["0.9-pre-release.db", "1.3.0.db"])
def test_sqlite_database_imports_into_postgres_verified(fixture, tmp_path, monkeypatch):
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app import import_sqlite
    from app.services import upgrade_service as up

    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(tmp_path / "store"))
    engine = create_engine(PG)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    src = Path(__file__).parent / "fixtures" / "releases" / fixture
    digest = src.read_bytes()
    out = import_sqlite.run(src, PG, log=lambda *_: None)
    assert out["rows"] > 0 and src.read_bytes() == digest                      # the source file is never written
    db = snapshot.PostgresDB(PG)
    assert up.state(db)["state"] == "current"
    with pytest.raises(import_sqlite.ImportError_, match="not empty"):
        import_sqlite.run(src, PG, log=lambda *_: None)
    with Session(engine) as s:                                                # the shop carries on: new ids follow the imported ones
        from app.models import Item
        from app.services import sales_service
        item = s.query(Item).filter(Item.name.like("DOLO%")).first()
        sale = sales_service.create_sale(s, lines=[{"item_id": item.id, "quantity": 1}])
        s.commit()
        assert sale.id > 1
    hits = db.query("SELECT id FROM items WHERE to_tsvector('simple', coalesce(name,'')) @@ to_tsquery('simple', 'dol:*')")
    assert hits
    engine.dispose()


def test_production_migration_job_installs_fresh_postgres_and_smokes(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    engine = create_engine(PG)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    env = {**os.environ, "PHARMACY_DATABASE_URL": PG, "PHARMACY_UPLOAD_DIR": str(tmp_path), "PHARMACY_EXTERNAL_MIGRATIONS": ""}
    run = lambda action: subprocess.run([sys.executable, "-m", "app.production", action], env=env, cwd=Path(__file__).parents[1],
                                        capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    assert json.loads(run("schema"))["state"] == "pending"
    run("migrate")
    assert json.loads(run("schema"))["state"] == "current"
    out = json.loads(run("smoke"))
    assert out["invoice"] == "ok" and out["write_read"] == "ok"
    assert snapshot.PostgresDB(PG).query("SELECT count(*) FROM sales")[0][0] == 0     # the probe leaves no business records


def test_import_resets_two_step_secrets_sealed_with_another_installations_key(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    from pathlib import Path

    from app import import_sqlite

    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(tmp_path / "store"))
    engine = create_engine(PG)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
    src = tmp_path / "old.db"
    shutil.copy(Path(__file__).parent / "fixtures" / "releases" / "1.3.0.db", src)
    with sqlite3.connect(src) as con:                       # enrolled on the old PC (its own key)
        con.execute("UPDATE users SET mfa_enabled = 1, mfa_secret = 'gAAAAABsealed-elsewhere' WHERE id = 1")
    messages = []
    import_sqlite.run(src, PG, log=messages.append)
    row = snapshot.PostgresDB(PG).query("SELECT mfa_enabled, mfa_secret FROM users WHERE id = 1")[0]
    assert tuple(row) == (False, "") and any("set up again" in m for m in messages)
    engine.dispose()
