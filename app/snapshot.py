"""Faheem Pharmacy snapshots — the whole ERP, kept apart from the application.

A snapshot is everything needed to bring the shop back: the database (a
consistent copy taken with SQLite's online backup, safe while billing), every
uploaded file (logo, supplier bills) and the local configuration (faheem.env,
VERSION). Each snapshot is one folder in the snapshot store:

    <store>/20261001-093000-pre-upgrade/
        pharmacy.db          the database
        uploads.tar.gz       uploaded files
        config/              faheem.env, VERSION (when present)
        manifest.json        SHA-256 of every file, app version, schema revision,
                             and a fingerprint of the business history

Isolation — the store is never inside the application, data or upload folders
(this module refuses such a location), so installing, updating or migrating the
application can never touch it. Nothing in the application deletes, rewrites or
prunes a snapshot: files are written once, made read-only, and a snapshot only
counts once its folder has been verified and atomically renamed into place.

The store also holds ``restore-tool/faheem_snapshot.py``, a copy of this file:
it uses only the Python standard library, so a snapshot can be listed, verified
and restored even if an installed release is broken.

    python -m app.snapshot create [--reason manual] [--note "..."]
    python -m app.snapshot list
    python -m app.snapshot verify <id>
    python -m app.snapshot restore <id> [--with-config]     (stop the app first)
    python -m app.snapshot drill [<id>]     restore into a scratch folder and prove it
    python -m app.snapshot where            print the store location

Location: ``PHARMACY_SNAPSHOT_DIR`` (the installer sets it to a folder outside the
installation), else ``~/FaheemPharmacy-Snapshots``. Under WSL every Linux file
lives in one virtual disk image, so ``PHARMACY_SNAPSHOT_MIRROR`` (set by the
installer to ``/mnt/c/FaheemPharmacy-Snapshots``) keeps a verified second copy
of every snapshot on the Windows drive. Only finished snapshots are copied there;
the live database always stays on the Linux filesystem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

FORMAT = 1
REASONS = ("manual", "scheduled", "pre-upgrade", "pre-restore", "pre-install", "pre-reset", "shutdown")
# Run as the standalone copy in <store>/restore-tool/, this file is not inside an application.
STANDALONE = Path(__file__).resolve().parent.name == "restore-tool"


def _load_env_file() -> None:
    """Standalone: pick up the installation's settings (data folders, store, mirror) from its faheem.env."""
    candidates = [os.environ.get("PHARMACY_ENV_FILE", ""), str(Path.home() / "faheem-pharmacy" / "shared" / "faheem.env")]
    for c in candidates:
        if c and Path(c).is_file():
            for line in Path(c).read_text(encoding="utf-8").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key.startswith("PHARMACY_") and not key.startswith("#"):
                    os.environ.setdefault(key, value)
            return


if STANDALONE:
    _load_env_file()
    APP_DIR = Path(os.environ.get("FAHEEM_APP_DIR") or Path.home() / "faheem-pharmacy" / "current")
else:
    APP_DIR = Path(__file__).resolve().parent.parent
_lock = threading.Lock()


class SnapshotError(Exception):
    pass


# --------------------------------------------------------------------------- locations
def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ[name]) if os.environ.get(name) else default


def data_dir() -> Path:
    return _env_path("PHARMACY_DATA_DIR", APP_DIR / "data")


def upload_dir() -> Path:
    return _env_path("PHARMACY_UPLOAD_DIR", APP_DIR / "uploads")


def db_path() -> Path:
    url = os.environ.get("PHARMACY_DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    return _env_path("PHARMACY_DB", data_dir() / "pharmacy.db")


def config_files() -> list[Path]:
    """Local configuration: faheem.env (``PHARMACY_ENV_FILE`` on a server install) and VERSION."""
    env = _env_path("PHARMACY_ENV_FILE", APP_DIR / "faheem.env")
    return [p for p in (env, APP_DIR / "VERSION") if p.is_file()]


def _isolated(store: Path, what: str) -> Path:
    store = store.resolve()
    for inside in (APP_DIR, data_dir(), upload_dir()):
        inside = inside.resolve()
        if store == inside or inside in store.parents:
            raise SnapshotError(f"The {what} {store} is inside {inside}; it must live outside the application and its data.")
    return store


def mirror_dir() -> Path | None:
    """Second copy of every snapshot outside WSL's virtual disk (e.g. /mnt/c/FaheemPharmacy-Snapshots):
    if the Linux distribution is reset or its disk image is lost, the snapshots survive on Windows."""
    raw = os.environ.get("PHARMACY_SNAPSHOT_MIRROR", "").strip()
    return _isolated(Path(raw), "snapshot mirror") if raw else None


def store_dir() -> Path:
    if os.environ.get("PHARMACY_SNAPSHOT_DIR"):
        store = Path(os.environ["PHARMACY_SNAPSHOT_DIR"])
    else:
        store = Path.home() / "FaheemPharmacy-Snapshots"
    return _isolated(store, "snapshot store (PHARMACY_SNAPSHOT_DIR)")


# --------------------------------------------------------------------------- helpers
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if path.is_dir() else 0))
    except OSError:
        pass


# --------------------------------------------------------------------------- database targets
class SqliteDB:
    engine = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)

    def __str__(self) -> str:
        return str(self.path)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        con = sqlite3.connect(str(self.path), timeout=30)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def tables(self) -> dict[str, set]:
        names = [r[0] for r in self.query("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: {r[1] for r in self.query(f'PRAGMA table_info("{t}")')} for t in names}


class PostgresDB:
    """PostgreSQL through psycopg (the app image ships it) plus pg_dump / pg_restore."""
    engine = "postgresql"

    def __init__(self, url: str):
        from urllib.parse import unquote, urlparse

        self.scheme = url.split("://", 1)[0] if "://" in url else "postgresql+psycopg"
        u = urlparse(url.replace(self.scheme + "://", "postgresql://", 1))
        self.host, self.port = u.hostname or "localhost", str(u.port or 5432)
        self.user, self.password = unquote(u.username or ""), unquote(u.password or "")
        self.name = (u.path or "/").lstrip("/")

    @property
    def url(self) -> str:
        """Always built from the current name, so a scratch copy (with_name) can never point at the live database."""
        from urllib.parse import quote

        auth = quote(self.user, safe="") + (":" + quote(self.password, safe="") if self.password else "")
        return f"{self.scheme}://{auth}@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:
        return f"postgresql://{self.user}@{self.host}:{self.port}/{self.name}"   # never the password

    def with_name(self, name: str) -> "PostgresDB":
        other = PostgresDB(self.url)
        other.name = name
        assert other.url != self.url or name == self.name
        return other

    def conninfo(self, name: str | None = None) -> str:
        return f"host={self.host} port={self.port} user={self.user} dbname={name or self.name}" + (
            f" password={self.password}" if self.password else "")

    def env(self) -> dict:
        env = dict(os.environ)
        if self.password:
            env["PGPASSWORD"] = self.password
        return env

    def cli(self, name: str | None = None) -> list[str]:
        return ["-h", self.host, "-p", self.port, "-U", self.user, "-d", name or self.name]

    def connect(self, name: str | None = None, autocommit: bool = False):
        import psycopg

        return psycopg.connect(self.conninfo(name), autocommit=autocommit)

    def exists(self) -> bool:
        try:
            with self.connect() as con:
                return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0] > 0
        except Exception:
            return False

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.connect() as con:
            return con.execute(sql.replace("?", "%s"), params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as con:
            con.execute(sql.replace("?", "%s"), params)
            con.commit()

    def tables(self) -> dict[str, set]:
        out: dict[str, set] = {}
        for t, c in self.query("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'"):
            out.setdefault(t, set()).add(c)
        return out

    def run(self, args: list[str], **kw):
        import subprocess

        r = subprocess.run(args, env=self.env(), capture_output=True, text=True, **kw)
        if r.returncode != 0:
            raise SnapshotError(f"{args[0]} failed: {(r.stderr or r.stdout).strip()[:400]}")
        return r


def database(db=None):
    """The database a snapshot is taken from / restored into: a SQLite file or a PostgreSQL URL."""
    if isinstance(db, (SqliteDB, PostgresDB)):
        return db
    if db is None:
        url = os.environ.get("PHARMACY_DATABASE_URL", "")
        return PostgresDB(url) if url.startswith("postgres") else SqliteDB(db_path())
    text = str(db)
    return PostgresDB(text) if text.startswith("postgres") else SqliteDB(Path(text))


def _copy_db(src: Path, dst: Path) -> None:
    """Consistent online copy, turned into one self-contained file (rollback journal, no -wal / -shm)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
    finally:
        d.close()
        s.close()


def schema_revision(db) -> list[str]:
    try:
        return sorted(r[0] for r in database(db).query("SELECT version_num FROM alembic_version"))
    except Exception:
        return []


# Business history that must survive every upgrade and every restore unchanged.
HISTORY_TABLES = (
    "sales", "sale_items", "sale_payments", "sale_returns", "sale_return_items",
    "purchases", "purchase_items", "purchase_returns", "suppliers", "supplier_product_maps",
    "items", "batches", "inventory_movements", "stock_adjustments",
    "customers", "customer_followups", "users", "categories", "audit_logs",
)
HISTORY_SUMS = (
    ("sales", "total"), ("sales", "subtotal"), ("sales", "discount"), ("sales", "round_off"),
    ("sale_items", "quantity"), ("sale_items", "line_total"), ("sale_items", "discount"),
    ("sale_items", "cost_rate"), ("sale_items", "line_cost"), ("sale_items", "net_sale_value"),
    ("sale_payments", "amount"), ("sale_returns", "total_refund"), ("sale_return_items", "quantity"),
    ("sale_return_items", "refund_amount"),
    ("purchases", "total"), ("purchase_items", "quantity"), ("purchase_items", "line_total"),
    ("batches", "quantity"), ("batches", "mrp"), ("batches", "purchase_rate"),
    ("inventory_movements", "quantity"), ("inventory_movements", "cost_amount"),
    ("stock_adjustments", "quantity"),
)
HISTORY_GROUPS = (("sales", "payment_status"), ("purchases", "status"), ("sale_returns", "status"))


def _fingerprint(q, cols: dict[str, set], engine: str) -> dict:
    """The fingerprint, given a query function — the same figures whichever database holds them."""
    num = "REAL" if engine == "sqlite" else "NUMERIC"
    out: dict = {}
    for t in HISTORY_TABLES:
        if t in cols:
            out[f"count:{t}"] = q(f'SELECT COUNT(*) FROM "{t}"')[0][0]
    for t, c in HISTORY_SUMS:
        if t in cols and c in cols[t]:
            v = q(f'SELECT COALESCE(SUM(ROUND(CAST("{c}" AS {num}), 4)), 0) FROM "{t}"')[0][0]
            out[f"sum:{t}.{c}"] = round(float(v), 2)
    for t, c in HISTORY_GROUPS:
        if t in cols and c in cols[t]:
            for key, n in q(f'SELECT COALESCE("{c}", \'\'), COUNT(*) FROM "{t}" GROUP BY 1'):
                out[f"group:{t}.{c}={key}"] = n
    if "batches" in cols and {"batch_id", "quantity"} <= cols.get("inventory_movements", set()):
        out["check:ledger_mismatched_batches"] = q(
            "SELECT COUNT(*) FROM batches b LEFT JOIN (SELECT batch_id, SUM(quantity) q FROM inventory_movements "
            "GROUP BY batch_id) m ON m.batch_id = b.id WHERE b.quantity != COALESCE(m.q, 0)")[0][0]
    if engine == "sqlite":
        out["check:foreign_key_violations"] = len(q("PRAGMA foreign_key_check"))
        out["check:integrity"] = q("PRAGMA integrity_check")[0][0]
    else:                                          # PostgreSQL enforces every reference and checksums its pages
        out["check:foreign_key_violations"] = 0
        out["check:integrity"] = "ok" if q("SELECT 1")[0][0] == 1 else "error"
    return out


def fingerprint(db) -> dict:
    """Read-only summary of the business history: counts, money / quantity totals,
    documents by status, stock-ledger balance, foreign keys and integrity."""
    target = database(db)
    if target.engine == "sqlite":
        con = sqlite3.connect(f"file:{target.path}?mode=ro", uri=True)
        try:
            q = lambda sql: con.execute(sql).fetchall()
            return _fingerprint(q, target.tables(), "sqlite")
        finally:
            con.close()
    with target.connect() as con:
        con.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        q = lambda sql: con.execute(sql).fetchall()
        return _fingerprint(q, target.tables(), "postgresql")


def _pg_dump_with_fingerprint(target: "PostgresDB", dest: Path) -> dict:
    """pg_dump and the fingerprint of the very same moment: an exported snapshot held open
    while pg_dump reads it, so billing during the backup cannot make them disagree."""
    with target.connect() as con:
        con.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snap = con.execute("SELECT pg_export_snapshot()").fetchone()[0]
        cols = target.tables()
        fp = _fingerprint(lambda sql: con.execute(sql).fetchall(), cols, "postgresql")
        target.run(["pg_dump", "-Fc", "--no-owner", "--no-privileges", f"--snapshot={snap}", "-f", str(dest), *target.cli()])
        con.rollback()
    return fp


class _StoreLock:
    """One writer at a time across processes (app, shell, installer)."""

    def __init__(self, store: Path):
        self.path = store / ".lock"

    def __enter__(self):
        deadline = time.time() + 120
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 3600:   # left behind by a crashed process
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise SnapshotError(f"Another snapshot is in progress ({self.path})")
                time.sleep(0.5)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _install_tool(store: Path) -> None:
    """Keep a standalone copy of this tool in the store (restores work even if the app is broken)."""
    tool = store / "restore-tool"
    tool.mkdir(exist_ok=True)
    target = tool / "faheem_snapshot.py"
    body = Path(__file__).read_bytes()
    if not target.exists() or target.read_bytes() != body:
        tmp = tool / ".faheem_snapshot.py.part"
        tmp.write_bytes(body)
        os.replace(tmp, target)
    readme = store / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Faheem Pharmacy snapshots. Never edit or delete files here by hand.\n\n"
            "List:     python3 restore-tool/faheem_snapshot.py list\n"
            "Verify:   python3 restore-tool/faheem_snapshot.py verify <id>\n"
            "Restore:  stop the app, then\n"
            "          PHARMACY_DATA_DIR=<app>/data PHARMACY_UPLOAD_DIR=<app>/uploads \\\n"
            "          python3 restore-tool/faheem_snapshot.py restore <id>\n", encoding="utf-8")


# --------------------------------------------------------------------------- create
def create(reason: str = "manual", note: str = "", *, db: Path | None = None, uploads: Path | None = None,
           store: Path | None = None) -> dict:
    """Take, verify and store one snapshot of the whole ERP. Raises SnapshotError when it could not be verified."""
    target = database(db)
    if not target.exists():
        raise SnapshotError(f"There is no database at {target}")
    src = target
    reason = reason if reason in REASONS else "manual"
    up = Path(uploads or upload_dir())
    store = Path(store or store_dir())
    store.mkdir(parents=True, exist_ok=True)
    with _lock, _StoreLock(store):
        stamp = datetime.now().replace(microsecond=0)
        sid = f"{stamp:%Y%m%d-%H%M%S}-{reason}"
        n = 1
        while (store / sid).exists():
            n += 1
            sid = f"{stamp:%Y%m%d-%H%M%S}-{reason}-{n}"
        work = store / f".incoming-{sid}"
        work.mkdir()
        try:
            if target.engine == "sqlite":
                _copy_db(target.path, work / "pharmacy.db")
                con = sqlite3.connect(str(work / "pharmacy.db"))
                try:
                    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    con.close()
                if integrity != "ok":
                    raise SnapshotError(f"The database copy failed its integrity check: {integrity}")
                fp, schema = fingerprint(work / "pharmacy.db"), schema_revision(work / "pharmacy.db")
            else:
                fp = _pg_dump_with_fingerprint(target, work / "database.dump")
                schema = schema_revision(target)
                target.run(["pg_restore", "--list", str(work / "database.dump")])      # the archive is readable
            with tarfile.open(work / "uploads.tar.gz", "w:gz") as tar:
                if up.is_dir():
                    for f in sorted(up.rglob("*")):
                        if f.is_file():
                            tar.add(f, arcname=str(f.relative_to(up)))
            (work / "config").mkdir()
            config_sources = {}
            for f in config_files():
                shutil.copy2(f, work / "config" / f.name)
                config_sources[f.name] = str(f.resolve())
            files = {}
            for f in sorted(work.rglob("*")):
                if f.is_file():
                    _fsync(f)
                    files[str(f.relative_to(work)).replace(os.sep, "/")] = {"sha256": _sha256(f), "size": f.stat().st_size}
            manifest = {
                "format": FORMAT, "id": sid, "created": stamp.isoformat(), "reason": reason, "note": note,
                "engine": target.engine, "app_version": _app_version(), "schema": schema,
                "source": {"database": str(src), "uploads": str(up), "config": config_sources}, "files": files,
                "fingerprint": fp,
            }
            (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            _fsync(work / "manifest.json")
            _verify_folder(work)                       # prove it before it counts
            for f in work.rglob("*"):
                _readonly(f)
            os.replace(work, store / sid)
            _readonly(store / sid)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True, onerror=_force_remove)
            raise
        _install_tool(store)
    mirror(sid, store)
    return manifest


def mirror(sid: str, store: Path | None = None) -> bool:
    """Copy one finished snapshot to the mirror and verify the copy. Never fails the snapshot itself."""
    target = mirror_dir()
    store = Path(store or store_dir())
    if target is None or target == store.resolve():
        return False
    try:
        target.mkdir(parents=True, exist_ok=True)
        if (target / sid / "manifest.json").is_file():
            return True
        work = target / f".incoming-{sid}"
        shutil.rmtree(work, ignore_errors=True, onerror=_force_remove)
        shutil.copytree(store / sid, work, copy_function=shutil.copy2)
        _verify_folder(work)
        for f in work.rglob("*"):
            _readonly(f)
        os.replace(work, target / sid)
        _readonly(target / sid)
        _install_tool(target)
        return True
    except (OSError, SnapshotError) as exc:
        try:
            with open(store / "mirror.log", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "id": sid, "error": str(exc)}) + "\n")
        except OSError:
            pass
        return False


def mirror_sync(store: Path | None = None) -> dict:
    """Copy every snapshot the mirror does not have yet."""
    target = mirror_dir()
    if target is None:
        raise SnapshotError("No mirror configured (PHARMACY_SNAPSHOT_MIRROR)")
    done = failed = 0
    for m in list_snapshots(store):
        if mirror(m["id"], store):
            done += 1
        else:
            failed += 1
    return {"mirror": str(target), "copied_or_present": done, "failed": failed}


def _force_remove(func, path, _exc):  # only for our own half-written .incoming folder
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
    func(path)


def _app_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        from app.config import APP_VERSION
        return APP_VERSION
    except Exception:
        return ""


# --------------------------------------------------------------------------- read / verify
def snapshot_dir(sid: str, store: Path | None = None) -> Path:
    store = Path(store or store_dir())
    if not sid or any(c in sid for c in "/\\") or sid.startswith("."):
        raise SnapshotError(f"Unknown snapshot {sid!r}")
    path = store / sid
    if not (path / "manifest.json").is_file():
        alt = mirror_dir()
        if alt is not None and (alt / sid / "manifest.json").is_file():
            return alt / sid          # the primary copy is gone; the mirror still has it
        raise SnapshotError(f"Unknown snapshot {sid!r} in {store}")
    return path


def list_snapshots(store: Path | None = None) -> list[dict]:
    store = Path(store or store_dir())
    out = []
    if store.is_dir():
        for d in store.iterdir():
            if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").is_file():
                try:
                    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                m["size"] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                out.append(m)
    alt = mirror_dir() if store == store_dir() else None
    if alt is not None and alt.is_dir():
        have = {m["id"] for m in out}
        for d in alt.iterdir():
            if d.is_dir() and not d.name.startswith(".") and d.name not in have and (d / "manifest.json").is_file():
                try:
                    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                m["size"] = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                m["mirror_only"] = True
                out.append(m)
    out.sort(key=lambda m: m["id"], reverse=True)
    return out


def latest(store: Path | None = None, reason: str | None = None) -> dict | None:
    return next((m for m in list_snapshots(store) if reason is None or m["reason"] == reason), None)


def _verify_folder(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for name, meta in manifest["files"].items():
        f = folder / name
        if not f.is_file():
            raise SnapshotError(f"{folder.name}: {name} is missing")
        if f.stat().st_size != meta["size"] or _sha256(f) != meta["sha256"]:
            raise SnapshotError(f"{folder.name}: {name} has changed since the snapshot was taken (checksum mismatch)")
    extra = {str(f.relative_to(folder)).replace(os.sep, "/") for f in folder.rglob("*") if f.is_file()} - set(manifest["files"]) - {"manifest.json"}
    if extra:
        raise SnapshotError(f"{folder.name}: unexpected files {sorted(extra)}")
    with tarfile.open(folder / "uploads.tar.gz") as tar:
        tar.getmembers()
    if manifest.get("engine") == "postgresql":
        # checksums prove the dump is the one written and the archive lists cleanly; the full
        # restore-and-compare is the drill (a scratch database), which runs daily
        import subprocess

        r = subprocess.run(["pg_restore", "--list", str(folder / "database.dump")], capture_output=True, text=True)
        if r.returncode != 0:
            raise SnapshotError(f"{folder.name}: the database dump cannot be read ({r.stderr.strip()[:200]})")
        return manifest
    work = Path(tempfile.mkdtemp(prefix="faheem-verify-"))
    try:
        shutil.copy2(folder / "pharmacy.db", work / "pharmacy.db")    # checked on a copy: the snapshot is never opened for writing
        os.chmod(work / "pharmacy.db", stat.S_IRUSR | stat.S_IWUSR)
        now = fingerprint(work / "pharmacy.db")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if now.get("check:integrity") != "ok":
        raise SnapshotError(f"{folder.name}: the database failed its integrity check")
    if now != manifest["fingerprint"]:
        raise SnapshotError(f"{folder.name}: the database does not match its recorded fingerprint")
    return manifest


def verify(sid: str, store: Path | None = None) -> dict:
    return _verify_folder(snapshot_dir(sid, store))


# --------------------------------------------------------------------------- restore
def _replace_db(snapshot_db: Path, live: Path) -> None:
    """Write the snapshot into the live database through SQLite (also safe if a connection is open)."""
    live.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(live), timeout=60)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        dst.close()
        src.close()


def _restore_uploads(archive: Path, target: Path, *, aside_dir: Path) -> None:
    """Put the snapshot's uploads in place. Current uploads are moved aside (never deleted)."""
    staging = Path(tempfile.mkdtemp(prefix="faheem-uploads-", dir=str(target.parent) if target.parent.exists() else None))
    try:
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                    raise SnapshotError(f"Unsafe path in uploads archive: {m.name}")
            tar.extractall(staging)
        if target.exists():
            aside_dir.mkdir(parents=True, exist_ok=True)
            os.replace(target, aside_dir / target.name)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore(sid: str, *, with_config: bool = False, db: Path | None = None, uploads: Path | None = None,
            store: Path | None = None, safety: bool = True) -> dict:
    """Restore database and uploads (and config with ``with_config``) from a verified snapshot.

    The current state is snapshotted first ("pre-restore"), so a restore can itself be undone.
    """
    store = Path(store or store_dir())
    folder = snapshot_dir(sid, store)
    manifest = _verify_folder(folder)
    target, up = database(db), Path(uploads or upload_dir())
    engine = manifest.get("engine", "sqlite")
    if engine != target.engine:
        raise SnapshotError(f"Snapshot {sid} is a {engine} snapshot and the database is {target.engine} — "
                            "move data between them with: python -m app.import_sqlite (see docs/BACKUP-RESTORE.md)")
    before = create("pre-restore", f"state before restoring {sid}", db=target, uploads=up, store=store) \
        if safety and target.exists() else None
    if engine == "sqlite":
        live = target.path
        work = Path(tempfile.mkdtemp(prefix="faheem-restore-"))
        try:
            shutil.copy2(folder / "pharmacy.db", work / "pharmacy.db")
            os.chmod(work / "pharmacy.db", stat.S_IRUSR | stat.S_IWUSR)
            _replace_db(work / "pharmacy.db", live)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        aside_root = live.parent
    else:
        _pg_restore(target, folder / "database.dump")
        aside_root = up.parent
    aside = aside_root / f"uploads-before-restore-{datetime.now():%Y%m%d-%H%M%S}"
    _restore_uploads(folder / "uploads.tar.gz", up, aside_dir=aside)
    if with_config:   # faheem.env goes back where it came from; VERSION belongs to the release, not restored
        sources = manifest.get("source", {}).get("config", {})
        for f in (folder / "config").glob("*"):
            if f.name == "VERSION":
                continue
            target = Path(sources.get(f.name) or _env_path("PHARMACY_ENV_FILE", APP_DIR / f.name))
            shutil.copy2(f, target)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    after = fingerprint(target)
    if after != manifest["fingerprint"]:
        raise SnapshotError("The restored database does not match the snapshot's fingerprint. The state before the "
                            f"restore is in snapshot {before and before['id']}.")
    return {"restored": sid, "schema": manifest["schema"], "safety_snapshot": before and before["id"],
            "uploads_moved_aside": str(aside) if aside.exists() else None}


def _pg_restore(target: "PostgresDB", dump: Path) -> None:
    """Replace the database's contents with the dump, all or nothing (one transaction)."""
    with target.connect(autocommit=True) as con:     # an empty public schema: no leftovers from newer migrations
        con.execute("DROP SCHEMA IF EXISTS public CASCADE")
        con.execute("CREATE SCHEMA public")
    target.run(["pg_restore", "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error", *target.cli(), str(dump)])


def drill(sid: str | None = None, store: Path | None = None, db=None) -> dict:
    """Restore rehearsal: restore a snapshot into a scratch folder and prove it opens and matches. Live data untouched."""
    store = Path(store or store_dir())
    m = verify(sid, store) if sid else latest(store)
    if not m:
        raise SnapshotError("There are no snapshots yet")
    work = Path(tempfile.mkdtemp(prefix="faheem-drill-"))
    scratch = None
    try:
        if m.get("engine") == "postgresql":           # a throw-away database next to the live one
            live = database(db)
            scratch = live.with_name(f"{live.name}_drill_{datetime.now():%Y%m%d%H%M%S}")
            with live.connect(autocommit=True) as con:
                con.execute(f'CREATE DATABASE "{scratch.name}"')
            target = scratch
        else:
            target = SqliteDB(work / "data" / "pharmacy.db")
        result = restore(m["id"], db=target, uploads=work / "uploads", store=store, safety=False)
        files = sum(1 for f in (work / "uploads").rglob("*") if f.is_file())
        record = {"id": m["id"], "at": datetime.now().isoformat(timespec="seconds"), "ok": True, "uploads": files,
                  "schema": result["schema"]}
    except Exception as exc:
        record = {"id": m["id"], "at": datetime.now().isoformat(timespec="seconds"), "ok": False, "error": str(exc)}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if scratch is not None:
            try:
                with database(db).connect(autocommit=True) as con:
                    con.execute(f'DROP DATABASE IF EXISTS "{scratch.name}" WITH (FORCE)')
            except Exception:
                pass
    try:
        with open(store / "drills.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass
    if not record["ok"]:
        raise SnapshotError(record["error"])
    return record


def last_drill(store: Path | None = None) -> dict | None:
    try:
        lines = (Path(store or store_dir()) / "drills.log").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- shell
def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="faheem-snapshot", description="Snapshots of the whole Faheem Pharmacy ERP.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="take a verified snapshot now")
    c.add_argument("--reason", default="manual", choices=REASONS)
    c.add_argument("--note", default="")
    sub.add_parser("list", help="list snapshots, newest first")
    sub.add_parser("where", help="print the snapshot store (and mirror) location")
    sub.add_parser("mirror-sync", help="copy every snapshot the mirror does not have yet")
    v = sub.add_parser("verify", help="check a snapshot's checksums, integrity and fingerprint")
    v.add_argument("id")
    r = sub.add_parser("restore", help="restore a snapshot (stop the app first)")
    r.add_argument("id")
    r.add_argument("--with-config", action="store_true", help="also restore faheem.env / VERSION")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    d = sub.add_parser("drill", help="restore a snapshot into a scratch folder and prove it (live data untouched)")
    d.add_argument("id", nargs="?")
    a = p.parse_args(argv)
    try:
        if a.cmd == "where":
            print(store_dir())
            if mirror_dir():
                print(f"mirror: {mirror_dir()}")
        elif a.cmd == "mirror-sync":
            print(mirror_sync())
        elif a.cmd == "create":
            m = create(a.reason, a.note)
            print(f"Snapshot {m['id']} written and verified · schema {','.join(m['schema'])} · "
                  f"{m['fingerprint'].get('count:sales', 0)} bills, {m['fingerprint'].get('count:items', 0)} products")
        elif a.cmd == "list":
            rows = list_snapshots()
            print(f"Store: {store_dir()}")
            for m in rows:
                f = m["fingerprint"]
                print(f"{m['id']:40} v{m.get('app_version', ''):10} {_human(m['size']):>8}  bills {f.get('count:sales', 0):>6}  "
                      f"products {f.get('count:items', 0):>6}  {'(mirror only) ' if m.get('mirror_only') else ''}{m.get('note', '')}")
            if not rows:
                print("No snapshots yet.")
            last = last_drill()
            if last:
                print(f"Last restore drill: {last['at']} {last['id']} {'OK' if last['ok'] else 'FAILED: ' + last.get('error', '')}")
        elif a.cmd == "verify":
            m = verify(a.id)
            print(f"{m['id']}: OK — checksums, integrity and fingerprint match ({len(m['files'])} files)")
        elif a.cmd == "restore":
            m = verify(a.id)
            if not a.yes:
                print(f"Restore {m['id']} (taken {m['created']}, app {m.get('app_version', '?')}) over {db_path()}?")
                print("The app must be stopped. The current state is snapshotted first.")
                if input("Type RESTORE to continue: ").strip() != "RESTORE":
                    print("Cancelled.")
                    return 1
            out = restore(a.id, with_config=a.with_config)
            print(f"Restored {out['restored']} (schema {','.join(out['schema'])}). Previous state kept as snapshot "
                  f"{out['safety_snapshot']}. Start the release whose version matches the snapshot, or a newer one.")
        elif a.cmd == "drill":
            r = drill(a.id)
            print(f"Restore drill OK: {r['id']} restored to a scratch folder, fingerprint matches, {r['uploads']} uploaded files")
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
