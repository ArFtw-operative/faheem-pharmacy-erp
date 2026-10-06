"""Upgrade safety: every release upgrades every earlier release's database with its
whole business history intact, failed or data-changing upgrades put the database
back, and snapshots of the whole ERP restore exactly (from the shell tool too)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import snapshot
from app.services import upgrade_service as up

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "releases").glob("*.db"))
# Migrations written before the non-destructive policy (docs/UPGRADES.md). Every newer
# migration is checked for destructive operations.
BASELINE_REVISION = "c3e5a7b9d1f3"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = tmp_path / "snapshots"
    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(s))
    monkeypatch.delenv("PHARMACY_SNAPSHOT_MIRROR", raising=False)
    return s


@pytest.fixture
def shop(tmp_path, store, monkeypatch):
    """A copy of the pre-1.0 release database with uploads, as a live install."""
    data = tmp_path / "data"
    uploads = tmp_path / "uploads"
    data.mkdir()
    (uploads / "suppliers").mkdir(parents=True)
    (uploads / "logo.png").write_bytes(b"\x89PNG logo")
    (uploads / "suppliers" / "bill-1.pdf").write_bytes(b"%PDF supplier bill")
    db = data / "pharmacy.db"
    shutil.copy(FIXTURES[0], db)
    monkeypatch.setenv("PHARMACY_UPLOAD_DIR", str(uploads))
    return db


def _all_revisions() -> list[str]:
    from alembic.script import ScriptDirectory

    return [r.revision for r in ScriptDirectory.from_config(up._alembic_config()).walk_revisions()]


# --------------------------------------------------------------------------- acceptance
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_every_release_upgrades_to_this_one_with_history_intact(fixture, tmp_path, store):
    db = tmp_path / "pharmacy.db"
    shutil.copy(fixture, db)
    before = up.fingerprint(db)
    out = up.upgrade(db)
    assert out["status"] in ("upgraded", "current")
    assert up.state(db)["state"] == "current"
    after = up.fingerprint(db)
    # only figures a migration declares (RECONCILE_EXEMPT) may change — and those must still reconcile
    assert up.compare(before, after, up._exemptions(_all_revisions())) == []
    if "count:manual_bills" in after:                               # 1.10.0: manual bills left sales, nothing lost
        assert before.get("count:sales", 0) == after["count:sales"] + after["count:manual_bills"]
        assert round(before.get("sum:sales.total", 0), 2) == round(after["sum:sales.total"] + after["sum:manual_bills.total"], 2)
        con = sqlite3.connect(db)
        assert con.execute("SELECT COUNT(*) FROM sales WHERE invoice_type = 'MANUAL'").fetchone()[0] == 0
        con.close()
    if out["status"] == "upgraded":
        shot = snapshot.verify(out["backup"])                       # the pre-upgrade snapshot is complete and valid
        assert shot["reason"] == "pre-upgrade" and shot["fingerprint"] == before
        con = sqlite3.connect(db)
        assert con.execute("SELECT status FROM deployment_log ORDER BY id DESC").fetchone()[0] == "UPGRADED"
        con.close()
    assert up.upgrade(db)["status"] == "current"                    # idempotent


def test_fresh_install_builds_the_exact_model_schema(tmp_path, store):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from app.database import Base

    db = tmp_path / "new.db"
    assert up.upgrade(db)["status"] == "installed"
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as con:
        diffs = compare_metadata(MigrationContext.configure(con), Base.metadata)
    engine.dispose()
    def name(d):
        return d[1].name if d[0].endswith("_table") else d[2]

    # the FTS5 search index is created by migrations as raw SQL, outside the models
    tables = [(d[0], name(d)) for d in diffs if d[0] in ("add_table", "remove_table", "add_column", "remove_column")
              and not str(name(d)).startswith("items_fts")]
    assert tables == [], tables


# --------------------------------------------------------------------------- failure handling
def test_failed_migration_puts_the_database_back(shop, monkeypatch):
    before, rev = up.fingerprint(shop), up.db_revisions(shop)
    real = up.migrate

    def broken(url=None):
        real(url)
        raise RuntimeError("simulated failure half-way")

    monkeypatch.setattr(up, "migrate", broken)
    with pytest.raises(up.UpgradeError, match="restored"):
        up.upgrade(shop)
    assert up.db_revisions(shop) == rev and up.fingerprint(shop) == before


def test_migration_that_changes_history_is_refused_and_rolled_back(shop, monkeypatch):
    before = up.fingerprint(shop)
    real = up.migrate

    def rewrites_history(url=None):
        real(url)
        con = sqlite3.connect(shop)
        con.execute("UPDATE sales SET total = total * 1.18")          # e.g. "apply the new GST rule to old bills"
        con.commit()
        con.close()

    monkeypatch.setattr(up, "migrate", rewrites_history)
    with pytest.raises(up.UpgradeError, match="sum:sales.total"):
        up.upgrade(shop)
    assert up.fingerprint(shop) == before


def test_intentional_change_must_be_declared_by_the_migration():
    before = {"sum:sales.total": 100.0, "count:sales": 2}
    after = {"sum:sales.total": 118.0, "count:sales": 2}
    assert up.compare(before, after) == ["sum:sales.total: 100.0 → 118.0"]
    assert up.compare(before, after, {"sum:sales.total": "documented correction"}) == []


def test_newer_or_unversioned_database_is_never_touched(tmp_path, store):
    newer = tmp_path / "newer.db"
    shutil.copy(FIXTURES[-1], newer)
    con = sqlite3.connect(newer)
    con.execute("UPDATE alembic_version SET version_num = 'ffffffffffff'")
    con.commit()
    con.close()
    digest = _sha(newer)
    with pytest.raises(up.NewerDatabaseError, match="newer release"):
        up.upgrade(newer)
    assert _sha(newer) == digest
    con = sqlite3.connect(newer)
    con.execute("DROP TABLE alembic_version")
    con.commit()
    con.close()
    with pytest.raises(up.UpgradeError, match="no migration history"):
        up.upgrade(newer)


def test_rehearsal_on_a_copy_never_writes_the_live_database(shop):
    digest = _sha(shop)
    out = up.check(shop)
    assert out["state"] == "pending" and out["migrations"] and out["problems"] == []
    assert _sha(shop) == digest and up.state(shop)["state"] == "pending"


def test_startup_path_has_no_silent_create_all_fallback():
    src = (ROOT / "app" / "database.py").read_text()
    body = src[src.index("def init_db"):]
    assert "except Exception:\n            Base.metadata.create_all" not in body
    assert "upgrade_service.upgrade(" in body


# --------------------------------------------------------------------------- migration policy
def _revisions_after_baseline() -> list:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(up._alembic_config())
    return [r for r in script.walk_revisions() if r.revision != BASELINE_REVISION
            and BASELINE_REVISION in {a.revision for a in script.iterate_revisions(r.revision, "base")}]


def test_single_migration_head():
    assert len(up.code_heads()) == 1


def test_new_migrations_are_not_destructive():
    """drop_table / drop_column / DELETE / UPDATE on history need an explicit, reviewed exemption."""
    risky = re.compile(r"op\.drop_table|op\.drop_column|batch_op\.drop_column|DELETE\s+FROM|\bUPDATE\s+\w+\s+SET|op\.rename_table", re.I)
    for rev in _revisions_after_baseline():
        text = Path(rev.path).read_text()
        body = text[text.index("def upgrade"):text.index("def downgrade")] if "def downgrade" in text else text[text.index("def upgrade"):]
        if risky.search(body):
            assert "DESTRUCTIVE_APPROVED" in text and "RECONCILE_EXEMPT" in text, \
                f"{Path(rev.path).name}: destructive operation without DESTRUCTIVE_APPROVED + RECONCILE_EXEMPT (docs/UPGRADES.md)"


# --------------------------------------------------------------------------- snapshots
def test_snapshot_covers_the_whole_erp_and_restores_exactly(shop, store, tmp_path):
    uploads = Path(snapshot.upload_dir())
    m = snapshot.create("manual", "test", db=shop)
    assert {"pharmacy.db", "uploads.tar.gz", "manifest.json"} <= {p.name for p in (store / m["id"]).iterdir()}
    assert snapshot.verify(m["id"])["fingerprint"] == up.fingerprint(shop)
    assert (store / "restore-tool" / "faheem_snapshot.py").read_bytes() == (ROOT / "app" / "snapshot.py").read_bytes()
    # the shop keeps working, then something goes wrong
    con = sqlite3.connect(shop)
    con.execute("DELETE FROM sale_return_items")
    con.execute("UPDATE batches SET quantity = 0")
    con.commit()
    con.close()
    (uploads / "logo.png").unlink()
    out = snapshot.restore(m["id"], db=shop)
    assert up.fingerprint(shop) == m["fingerprint"]
    assert (uploads / "logo.png").read_bytes() == b"\x89PNG logo"
    assert (uploads / "suppliers" / "bill-1.pdf").exists()
    safety = snapshot.verify(out["safety_snapshot"])          # the broken state was kept too
    assert safety["reason"] == "pre-restore" and safety["fingerprint"]["count:sale_return_items"] == 0
    assert snapshot.drill(m["id"])["ok"] and snapshot.last_drill()["id"] == m["id"]


def test_tampered_snapshot_is_detected(shop, store):
    m = snapshot.create("manual", db=shop)
    db = store / m["id"] / "pharmacy.db"
    db.chmod(0o644)
    with open(db, "r+b") as fh:
        fh.seek(200)
        fh.write(b"tampered")
    with pytest.raises(snapshot.SnapshotError, match="checksum"):
        snapshot.verify(m["id"])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore(m["id"], db=shop)


def test_snapshot_files_are_read_only_and_never_pruned(shop, store):
    ids = [snapshot.create("scheduled", db=shop)["id"] for _ in range(3)]
    assert [m["id"] for m in snapshot.list_snapshots()][:3] == sorted(ids, reverse=True)
    f = store / ids[0] / "pharmacy.db"
    assert not (f.stat().st_mode & 0o222)


def test_store_must_be_isolated_from_app_and_data(monkeypatch):
    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(ROOT / "backups-inside-app"))
    with pytest.raises(snapshot.SnapshotError, match="outside"):
        snapshot.store_dir()
    monkeypatch.setenv("PHARMACY_SNAPSHOT_DIR", str(snapshot.data_dir() / "snaps"))
    with pytest.raises(snapshot.SnapshotError, match="outside"):
        snapshot.store_dir()


def test_mirror_keeps_a_verified_copy_that_restores_when_the_primary_is_lost(shop, store, tmp_path, monkeypatch):
    mirror = tmp_path / "windows-drive" / "FaheemPharmacy-Snapshots"
    monkeypatch.setenv("PHARMACY_SNAPSHOT_MIRROR", str(mirror))
    m = snapshot.create("manual", db=shop)
    assert (mirror / m["id"] / "manifest.json").exists() and (mirror / "restore-tool" / "faheem_snapshot.py").exists()
    for p in (store / m["id"]).rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    (store / m["id"]).chmod(0o755)
    shutil.rmtree(store / m["id"])                                 # the WSL disk image was lost
    assert snapshot.verify(m["id"])["id"] == m["id"]
    assert any(x.get("mirror_only") for x in snapshot.list_snapshots())
    snapshot.restore(m["id"], db=shop, safety=False)
    assert up.fingerprint(shop) == m["fingerprint"]


def test_standalone_restore_tool_works_without_the_application(shop, store):
    import subprocess
    import sys

    m = snapshot.create("manual", db=shop)
    tool = store / "restore-tool" / "faheem_snapshot.py"
    env = {"PATH": "/usr/bin:/bin", "PHARMACY_SNAPSHOT_DIR": str(store), "PHARMACY_DATABASE_URL": f"sqlite:///{shop}",
           "PHARMACY_UPLOAD_DIR": str(snapshot.upload_dir()), "HOME": str(store.parent)}
    listed = subprocess.run([sys.executable, str(tool), "list"], env=env, cwd="/", capture_output=True, text=True)
    assert listed.returncode == 0 and m["id"] in listed.stdout, listed.stderr
    verified = subprocess.run([sys.executable, str(tool), "verify", m["id"]], env=env, cwd="/", capture_output=True, text=True)
    assert verified.returncode == 0 and "OK" in verified.stdout, verified.stderr
    manifest = json.loads((store / m["id"] / "manifest.json").read_text())
    assert manifest["schema"] == up.db_revisions(shop) or manifest["schema"] == sorted(up.db_revisions(shop))
