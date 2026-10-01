"""Move a Faheem Pharmacy SQLite database into an empty PostgreSQL database — verified.

    python -m app.import_sqlite /path/to/pharmacy.db [--target postgresql+psycopg://…]
    (on the appliance:  sudo faheem-erp import-sqlite /path/to/pharmacy.db)

1. The source is never written: a copy is brought to this release's schema first.
2. The target must be empty; it is created at this release's schema.
3. Every table is copied (types converted by the models), identity sequences reset.
4. Verified before it counts:
   - every table has the same number of rows;
   - the business-history fingerprint (counts, money and quantity totals, statuses,
     stock-ledger balance) is identical;
   - no row points at a missing parent (every foreign key is checked).
   If anything differs the target is emptied again and the import fails.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select, text

from app import snapshot


class ImportError_(RuntimeError):
    pass


def _ordered_tables(metadata):
    tables = list(metadata.sorted_tables)
    first = [t for t in tables if t.name == "categories"]          # items' category is checked by a trigger, not a FK
    return first + [t for t in tables if t.name != "categories"]


def orphans(engine, metadata) -> list[str]:
    """Rows whose foreign key points at a missing parent (empty = every relationship intact)."""
    problems = []
    with engine.connect() as con:
        for table in metadata.sorted_tables:
            for fk in table.foreign_keys:
                col, parent = fk.parent, fk.column
                sql = (f'SELECT COUNT(*) FROM "{table.name}" c WHERE c."{col.name}" IS NOT NULL AND NOT EXISTS '
                       f'(SELECT 1 FROM "{parent.table.name}" p WHERE p."{parent.name}" = c."{col.name}")')
                n = con.execute(text(sql)).scalar()
                if n:
                    problems.append(f"{table.name}.{col.name} → {parent.table.name}: {n} row(s) without a parent")
    return problems


def _reset_unreadable_mfa(engine) -> list[str]:
    """Authenticator secrets are sealed with the installation's key; the new installation has its own.
    A user whose secret cannot be opened enrols again at the next sign-in (password still required)."""
    from app.services import mfa

    users = []
    with engine.begin() as con:
        for uid, name, secret in con.execute(text("SELECT id, username, mfa_secret FROM users WHERE coalesce(mfa_secret, '') <> ''")):
            if not mfa.unseal(secret):
                con.execute(text("UPDATE users SET mfa_enabled = false, mfa_secret = '', mfa_recovery = NULL, "
                                 "mfa_last_step = NULL, mfa_enrolled_at = NULL WHERE id = :id"), {"id": uid})
                users.append(name)
    return users


def run(source: Path, target_url: str, *, log=print) -> dict:
    import app.models  # noqa: F401
    from app.database import PG_CONSTRAINT_DDL, PG_SEARCH_DDL, PERF_INDEXES, Base
    from app.services import upgrade_service as up

    source = Path(source)
    if not source.is_file():
        raise ImportError_(f"No SQLite database at {source}")
    target = snapshot.PostgresDB(target_url)
    if target.exists():
        raise ImportError_(f"The target database {target} is not empty — import only into a new, empty database")
    work = Path(tempfile.mkdtemp(prefix="faheem-import-"))
    tgt_engine = None
    try:
        copy = work / "source.db"
        import sqlite3

        s, d = sqlite3.connect(f"file:{source}?mode=ro", uri=True), sqlite3.connect(str(copy))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        st = up.state(copy)
        if st["state"] == "newer":
            raise ImportError_("The SQLite database is from a newer release than this one — install the newer release first")
        if st["state"] in ("unversioned", "fresh"):
            raise ImportError_("The SQLite file has no Faheem Pharmacy migration history — it cannot be imported safely")
        if st["state"] == "pending":
            log(f"Bringing a copy of the source to this release's schema ({len(up.pending_revisions(copy))} migration(s))…")
            up.upgrade(copy, backups=False)
        before = snapshot.fingerprint(copy)

        log(f"Creating the schema in {target}…")
        up.migrate(target.url)
        src_engine, tgt_engine = create_engine(f"sqlite:///{copy}"), create_engine(target.url)
        src_cols = {t: {c["name"] for c in inspect(src_engine).get_columns(t)} for t in inspect(src_engine).get_table_names()}
        counts = {}
        with tgt_engine.begin() as tcon, src_engine.connect() as scon:
            tcon.execute(text("SET session_replication_role = replica"))     # parents and children in any order; checked below
            for table in _ordered_tables(Base.metadata):
                if table.name not in src_cols:
                    counts[table.name] = (0, 0)
                    continue
                cols = [c for c in table.columns if c.name in src_cols[table.name]]
                n = 0
                result = scon.execute(select(*cols).order_by(*(table.primary_key.columns or cols[:1])))
                while True:
                    rows = result.fetchmany(2000)
                    if not rows:
                        break
                    tcon.execute(table.insert(), [dict(r._mapping) for r in rows])
                    n += len(rows)
                counts[table.name] = (scon.execute(select(func.count()).select_from(table)).scalar(), n)
            tcon.execute(text("SET session_replication_role = DEFAULT"))
            for table in Base.metadata.sorted_tables:                          # new rows continue after the imported ids
                pk = list(table.primary_key.columns)
                if len(pk) == 1 and pk[0].name == "id" and pk[0].type.python_type is int:
                    tcon.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), "
                                      f"COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 1), "
                                      f"(SELECT MAX(id) FROM \"{table.name}\") IS NOT NULL)"))
            for ddl in PG_SEARCH_DDL + PG_CONSTRAINT_DDL:
                tcon.execute(text(ddl))
            for name, (tbl, col) in PERF_INDEXES.items():
                tcon.execute(text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{tbl}" ("{col}")'))

        log("Verifying…")
        problems = [f"{t}: {src} rows in SQLite, {dst} copied" for t, (src, dst) in counts.items() if src != dst]
        problems += up.compare(before, snapshot.fingerprint(target))
        problems += orphans(tgt_engine, Base.metadata)
        if problems:
            raise ImportError_("Import not accepted:\n  " + "\n  ".join(problems[:20]))
        reenrol = _reset_unreadable_mfa(tgt_engine)
        if reenrol:
            log(f"Two-step sign-in will be set up again at next login for: {', '.join(reenrol)} "
                "(their authenticator secrets were sealed with the old installation's key)")
        return {"tables": len(counts), "rows": sum(n for _, n in counts.values()), "figures": len(before),
                "schema": snapshot.schema_revision(target)}
    except BaseException:
        if tgt_engine is not None:
            tgt_engine.dispose()
            with snapshot.PostgresDB(target_url).connect(autocommit=True) as con:    # leave nothing half-imported
                con.execute("DROP SCHEMA public CASCADE")
                con.execute("CREATE SCHEMA public")
        raise
    finally:
        if tgt_engine is not None:
            tgt_engine.dispose()
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="the SQLite pharmacy.db to import")
    p.add_argument("--target", default=os.environ.get("PHARMACY_DATABASE_URL", ""), help="PostgreSQL URL (default: the app's)")
    a = p.parse_args(argv)
    if not a.target.startswith("postgres"):
        print("ERROR: the target must be a PostgreSQL database (set --target or PHARMACY_DATABASE_URL)", file=sys.stderr)
        return 2
    try:
        out = run(Path(a.source), a.target)
    except ImportError_ as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {out['rows']} rows in {out['tables']} tables · {out['figures']} history figures identical · "
          f"every relationship intact · schema {','.join(out['schema'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
