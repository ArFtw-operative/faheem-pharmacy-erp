"""Guarded upgrades: a new release never loses or silently changes business data.

Every start of the application goes through :func:`upgrade`:

1. The database's schema revision is compared with the code's migrations.
   A database written by a *newer* release, or one without migration history,
   is never touched: start-up stops with instructions (an older release must not
   guess at a newer schema).
2. When migrations are pending, a verified backup is taken first. If it cannot
   be taken, nothing is migrated.
3. A fingerprint of the business history (row counts, money and quantity
   totals, stock-ledger balance, foreign keys, integrity) is recorded.
4. The versioned migrations run (Alembic).
5. The fingerprint is taken again and compared. History must be identical.
6. If a migration fails, or the history changed, the pre-upgrade backup is put
   back automatically and start-up stops. The shop is back exactly where it was
   and the previous release can be reinstalled.

Each upgrade (and each first start of a new app version) is written to the
``deployment_log`` table and to ``logs/deployments.log``.

:func:`check` runs steps 3–5 on a throw-away copy of the live database: the
staging rehearsal of an upgrade (``python scripts/manage.py upgrade --check``).

Snapshots (``app.snapshot``) live in an isolated store outside the application.

A migration that must change a fingerprinted figure on purpose declares it in
its module, with the reason, e.g. ``RECONCILE_EXEMPT = {"sum:sales.total": "..."}``.
A migration that moves documents to another table declares the move instead,
``RECONCILE_MOVED = {"sum:sales.total": "sum:manual_bills.total"}``: the figure
may only drop by exactly what arrived in the other table (before = after + moved).
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from app import snapshot
from app.config import APP_BUILD, APP_VERSION, BASE_DIR, LOG_DIR
from app.snapshot import fingerprint  # one definition, shared with the standalone restore tool

logger = logging.getLogger("pharmacy.upgrade")

GROW_ONLY = {"audit_logs"}


class UpgradeError(RuntimeError):
    """The upgrade was stopped; the database is unchanged (or was restored)."""


class NewerDatabaseError(UpgradeError):
    """The database was written by a newer release than this code."""


# --------------------------------------------------------------------------- schema state
def _alembic_config(url: str | None = None):
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    if url:
        cfg.attributes["database_url"] = url
    return cfg


def code_heads() -> set[str]:
    from alembic.script import ScriptDirectory

    return set(ScriptDirectory.from_config(_alembic_config()).get_heads())


def known_revisions() -> set[str]:
    from alembic.script import ScriptDirectory

    return {s.revision for s in ScriptDirectory.from_config(_alembic_config()).walk_revisions()}


def _target(db=None):
    """The database to work on: a SQLite file path, a PostgreSQL URL, or the application's own."""
    return snapshot.database(db)


def _url(target) -> str:
    return target.url if target.engine == "postgresql" else f"sqlite:///{target.path}"


def db_revisions(db=None) -> set[str] | None:
    """Revisions recorded in the database; None when it has no migration history."""
    target = _target(db)
    try:
        return {r[0] for r in target.query("SELECT version_num FROM alembic_version")}
    except Exception:
        return None


def state(db=None) -> dict:
    """``fresh`` (no database yet) · ``current`` · ``pending`` · ``newer`` · ``unversioned``."""
    target = _target(db)
    heads = code_heads()
    if not target.exists():
        return {"state": "fresh", "database": [], "code": sorted(heads)}
    current = db_revisions(target)
    if not current:
        return {"state": "unversioned", "database": [], "code": sorted(heads)}
    if current - known_revisions():
        return {"state": "newer", "database": sorted(current), "code": sorted(heads)}
    return {"state": "current" if current == heads else "pending", "database": sorted(current), "code": sorted(heads)}


def pending_revisions(db=None) -> list[str]:
    """Revisions that an upgrade would apply, oldest first."""
    from alembic.script import ScriptDirectory

    current = db_revisions(db) or set()
    script = ScriptDirectory.from_config(_alembic_config())
    out = []
    for head in code_heads():
        for rev in script.iterate_revisions(head, tuple(current) or "base"):
            if rev.revision not in current:
                out.append(rev)
    return [r.revision for r in reversed(out)]


def _exemptions(revisions: list[str]) -> dict:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    out: dict = {}
    for rev in revisions:
        module = script.get_revision(rev).module
        out.update(getattr(module, "RECONCILE_EXEMPT", {}) or {})
        out.update({k: {"moved_to": v} for k, v in (getattr(module, "RECONCILE_MOVED", {}) or {}).items()})
    return out


def compare(before: dict, after: dict, exempt: dict | None = None) -> list[str]:
    """Differences that an upgrade is not allowed to make (empty = history intact)."""
    exempt = exempt or {}
    problems = []
    for key, old in before.items():
        rule = exempt.get(key)
        if isinstance(rule, dict) and rule.get("moved_to"):
            # moved, not lost: what left this figure must be exactly what arrived in the other table
            moved = float(after.get(rule["moved_to"], 0) or 0) - float(before.get(rule["moved_to"], 0) or 0)
            if abs(float(after.get(key, 0) or 0) + float(moved) - float(old)) > 0.005:
                problems.append(f"{key}: {old} → {after.get(key)} (+ {moved} in {rule['moved_to']}) does not reconcile")
            continue
        if key in exempt:
            continue
        if key.startswith("check:"):
            new = after.get(key)
            worse = new != "ok" if key == "check:integrity" else (new or 0) > old
            if worse:
                problems.append(f"{key}: {old} → {new}")
            continue
        if key not in after:
            problems.append(f"{key}: {old} → missing after upgrade")
            continue
        new = after[key]
        table = key.split(":", 1)[1].split(".")[0].split("=")[0]
        if key.startswith("count:") and table in GROW_ONLY:
            if new < old:
                problems.append(f"{key}: {old} → {new}")
        elif isinstance(old, float) or isinstance(new, float):
            if abs(float(new) - float(old)) > 0.005:
                problems.append(f"{key}: {old} → {new}")
        elif new != old:
            problems.append(f"{key}: {old} → {new}")
    for key, new in after.items():   # a status that did not exist before appeared
        if key.startswith("group:") and key not in before and key not in exempt and new:
            problems.append(f"{key}: 0 → {new}")
    return problems


# --------------------------------------------------------------------------- migrate
def migrate(url: str | None = None) -> None:
    """Versioned migrations to head. A brand-new PostgreSQL database is created from the current
    models in one step and stamped at head: the historical migration chain was written for SQLite
    databases that already exist in the field; every migration from now on runs on both."""
    from alembic import command

    from app.config import DATABASE_URL

    url = url or DATABASE_URL
    if url.startswith("postgres") and not snapshot.database(url).exists():
        from sqlalchemy import create_engine

        import app.models  # noqa: F401
        from app.database import Base

        engine = create_engine(url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        command.stamp(_alembic_config(url), "head")
        return
    command.upgrade(_alembic_config(url), "head")


def _log(entry: dict) -> None:
    """Deployment history: a line in logs/deployments.log, and the deployment_log table when present."""
    target = entry.pop("_db", None)
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "app_version": APP_VERSION, "build": APP_BUILD, **entry}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "deployments.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.exception("Could not write the deployment log file")
    try:
        db = _target(target)
        if "deployment_log" in db.tables():
            db.execute("INSERT INTO deployment_log (deployed_at, app_version, build, status, from_revision, to_revision, "
                       "backup_name, detail) VALUES (?,?,?,?,?,?,?,?)",
                       (entry["at"], APP_VERSION, APP_BUILD, entry.get("status", ""), entry.get("from_revision", ""),
                        entry.get("to_revision", ""), entry.get("backup", "") or "", json.dumps(entry, default=str)))
    except Exception:
        logger.exception("Could not record the deployment in the database")


def _last_logged_version(db=None) -> str | None:
    try:
        rows = _target(db).query("SELECT build FROM deployment_log ORDER BY id DESC LIMIT 1")
        return rows[0][0] if rows else None
    except Exception:
        return None


def _refuse(st: dict) -> None:
    if st["state"] == "newer":
        raise NewerDatabaseError(
            f"This database was upgraded by a newer release (schema {', '.join(st['database'])}); this release "
            f"({APP_VERSION}) only knows up to {', '.join(st['code'])}. Install the newer release again, or roll back "
            "with its pre-upgrade backup: faheem-erp rollback (or faheem-erp restore).")
    if st["state"] == "unversioned":
        raise UpgradeError("The database has tables but no migration history, so its schema version is unknown. "
                           "It was not changed. Restore a backup made by this application or contact support.")


def upgrade(db=None, *, backups: bool = True) -> dict:
    """Bring the live database to this release's schema without losing history. See module docstring."""
    target = _target(db)
    st = state(target)
    _refuse(st)
    if st["state"] == "fresh":
        migrate(_url(target))
        _log({"status": "INSTALLED", "to_revision": ",".join(sorted(code_heads())), "_db": target})
        return {"status": "installed"}
    if st["state"] == "current":
        if _last_logged_version(target) != APP_BUILD:
            _log({"status": "STARTED", "from_revision": ",".join(st["database"]), "to_revision": ",".join(st["database"]),
                  "note": "first start of this app version; no schema change", "_db": target})
        return {"status": "current"}

    revisions = pending_revisions(target)
    backup = None
    if backups:
        try:
            backup = snapshot.create("pre-upgrade", f"before upgrading to {APP_VERSION}", db=target)
            backup["name"] = backup["id"]
        except Exception as exc:
            _log({"status": "ABORTED", "from_revision": ",".join(st["database"]), "reason": f"pre-upgrade snapshot failed: {exc}", "_db": target})
            raise UpgradeError(f"Upgrade not started: the pre-upgrade snapshot failed ({exc}). The database is unchanged.") from exc
    before = fingerprint(target)
    logger.info("Upgrading database %s → %s (%d migration(s)); backup %s",
                ",".join(st["database"]), ",".join(st["code"]), len(revisions), backup and backup["name"])
    try:
        migrate(_url(target))
        problems = compare(before, fingerprint(target), _exemptions(revisions))
    except Exception as exc:  # the migration itself failed
        problems = [f"migration failed: {exc}"]
        logger.exception("Migration failed")
    if problems:
        restored = None
        if backup:
            from app.database import engine
            engine.dispose()
            snapshot.restore(backup["id"], db=target, safety=True)
            engine.dispose()
            restored = backup["id"]
        _log({"status": "ROLLED_BACK" if restored else "FAILED", "from_revision": ",".join(st["database"]),
              "to_revision": ",".join(st["code"]), "backup": restored, "problems": problems, "migrations": revisions, "_db": target})
        raise UpgradeError("The upgrade was stopped and " + (f"the database restored from {restored}" if restored else "NOT restored")
                           + ". Reinstall the previous release. Problems: " + "; ".join(problems[:8]))
    _log({"status": "UPGRADED", "from_revision": ",".join(st["database"]), "to_revision": ",".join(sorted(code_heads())),
          "backup": backup and backup["name"], "migrations": revisions, "reconciled": len(before), "_db": target})
    return {"status": "upgraded", "migrations": revisions, "backup": backup and backup["name"], "reconciled": len(before)}


def check(db=None) -> dict:
    """Staging rehearsal: upgrade a copy of the database and reconcile it. The live database is never written."""
    target = _target(db)
    st = state(target)
    _refuse(st)
    if st["state"] in ("fresh", "current"):
        return {"state": st["state"], "migrations": [], "problems": []}
    revisions = pending_revisions(target)
    work = Path(tempfile.mkdtemp(prefix="faheem-staging-"))
    scratch = None
    try:
        if target.engine == "sqlite":
            import sqlite3

            copy = snapshot.SqliteDB(work / "pharmacy.db")
            src, dst = sqlite3.connect(f"file:{target.path}?mode=ro", uri=True), sqlite3.connect(str(copy.path))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        else:                                     # a scratch database restored from a fresh dump
            dump = work / "rehearsal.dump"
            target.run(["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f", str(dump), *target.cli()])
            scratch = copy = target.with_name(f"{target.name}_rehearsal_{datetime.now():%Y%m%d%H%M%S}")
            with target.connect(autocommit=True) as con:
                con.execute(f'CREATE DATABASE "{scratch.name}"')
            target.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", *scratch.cli(), str(dump)])
        before = fingerprint(copy)
        try:
            migrate(_url(copy))
            problems = compare(before, fingerprint(copy), _exemptions(revisions))
        except Exception as exc:
            problems = [f"migration failed: {exc}"]
        return {"state": st["state"], "migrations": revisions, "problems": problems, "checked": len(before)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if scratch is not None:
            try:
                with target.connect(autocommit=True) as con:
                    con.execute(f'DROP DATABASE IF EXISTS "{scratch.name}" WITH (FORCE)')
            except Exception:
                logger.exception("Could not drop the rehearsal database %s", scratch.name)


def history(db=None, limit: int = 50) -> list[dict]:
    try:
        rows = _target(db).query("SELECT deployed_at, app_version, build, status, from_revision, to_revision, backup_name "
                                 "FROM deployment_log ORDER BY id DESC LIMIT ?", (limit,))
    except Exception:
        return []
    keys = ("at", "version", "build", "status", "from", "to", "backup")
    return [dict(zip(keys, r)) for r in rows]
