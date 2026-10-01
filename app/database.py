"""Database engine, session management, search index and initialisation.

PostgreSQL in production (Docker appliance); SQLite for development and the
fast test suite. Dialect-specific pieces (search index, integrity triggers,
connection settings) live here; everything else is portable SQLAlchemy."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_POSTGRES = DATABASE_URL.startswith("postgresql")

if IS_SQLITE:
    connect_args: dict = {"check_same_thread": False}
    _pool = {"pool_size": 20, "max_overflow": 20, "pool_timeout": 20} if ":memory:" not in DATABASE_URL else {}
elif IS_POSTGRES:
    # every timestamp is stored as UTC (naive), exactly as on SQLite; statements may not hang a till
    connect_args = {"options": "-c timezone=UTC -c statement_timeout=60000 -c idle_in_transaction_session_timeout=300000"}
    _pool = {"pool_size": 10, "max_overflow": 10, "pool_timeout": 20, "pool_pre_ping": True, "pool_recycle": 1800}
else:
    connect_args, _pool = {}, {}
engine: Engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True, **_pool)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    """FK enforcement, WAL and crash-safe durability on SQLite."""
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    # FULL: a committed sale is on disk even if power is lost a moment later.
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute("PRAGMA busy_timeout=15000")
    # Speed: a 32 MB page cache per connection, temp tables in memory, memory-mapped reads.
    cursor.execute("PRAGMA cache_size=-32000")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


FTS_DDL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        name, generic_name, manufacturer, barcode,
        content='items', content_rowid='id', tokenize='unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, name, generic_name, manufacturer, barcode)
        VALUES (new.id, new.name, new.generic_name, new.manufacturer, new.barcode);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, name, generic_name, manufacturer, barcode)
        VALUES ('delete', old.id, old.name, old.generic_name, old.manufacturer, old.barcode);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, name, generic_name, manufacturer, barcode)
        VALUES ('delete', old.id, old.name, old.generic_name, old.manufacturer, old.barcode);
        INSERT INTO items_fts(rowid, name, generic_name, manufacturer, barcode)
        VALUES (new.id, new.name, new.generic_name, new.manufacturer, new.barcode);
    END
    """,
]


# PostgreSQL: the same prefix search as FTS5 ("dol* 650*"), on an expression index.
# The expression must stay identical to app/services/search.py so the index is used.
PG_SEARCH_EXPR = ("to_tsvector('simple', coalesce(name,'') || ' ' || coalesce(generic_name,'') || ' ' || "
                  "coalesce(manufacturer,'') || ' ' || coalesce(barcode,''))")
PG_SEARCH_DDL = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    f"CREATE INDEX IF NOT EXISTS ix_items_search ON items USING gin ({PG_SEARCH_EXPR})",
    "CREATE INDEX IF NOT EXISTS ix_items_name_trgm ON items USING gin (name gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_items_generic_trgm ON items USING gin (generic_name gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_customers_mobile_trgm ON customers USING gin (mobile gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_customers_name_trgm ON customers USING gin (name gin_trgm_ops)",
]

# Lookup indexes for tables that grow with every sale/purchase. Created with IF NOT EXISTS at
# start-up: idempotent, instant on small tables, and they never change any data.
PERF_INDEXES = {
    "ix_sale_items_sale_id": ("sale_items", "sale_id"),
    "ix_sale_items_item_id": ("sale_items", "item_id"),
    "ix_sale_items_batch_id": ("sale_items", "batch_id"),
    "ix_sales_customer_id": ("sales", "customer_id"),
    "ix_sales_user_id": ("sales", "user_id"),
    "ix_purchase_items_purchase_id": ("purchase_items", "purchase_id"),
    "ix_purchase_items_item_id": ("purchase_items", "item_id"),
    "ix_purchases_supplier_id": ("purchases", "supplier_id"),
    "ix_batches_supplier_id": ("batches", "supplier_id"),
    "ix_batches_purchase_id": ("batches", "purchase_id"),
    "ix_stock_adjustments_item_id": ("stock_adjustments", "item_id"),
    "ix_stock_adjustments_batch_id": ("stock_adjustments", "batch_id"),
    "ix_sale_return_items_return_id": ("sale_return_items", "return_id"),
    "ix_sale_return_items_sale_item_id": ("sale_return_items", "sale_item_id"),
    "ix_sale_return_items_item_id": ("sale_return_items", "item_id"),
    "ix_sale_returns_customer_id": ("sale_returns", "customer_id"),
    "ix_audit_logs_user_id": ("audit_logs", "user_id"),
    "ix_login_sessions_user_id": ("login_sessions", "user_id"),
    "ix_expiry_alerts_batch_id": ("expiry_alerts", "batch_id"),
    "ix_purchase_returns_batch_id": ("purchase_returns", "batch_id"),
}


def _tables(conn) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(conn).get_table_names())


def ensure_indexes() -> None:
    if not (IS_SQLITE or IS_POSTGRES):
        return
    with engine.begin() as conn:
        tables = _tables(conn)
        for name, (table, column) in PERF_INDEXES.items():
            if table in tables:
                conn.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")')


# Referential rules SQLite cannot add to an existing table without rebuilding it
# (the migration f2a4b6c8d0e2 installs the same triggers on live databases).
CONSTRAINT_DDL = [
    """CREATE TRIGGER IF NOT EXISTS trg_items_category_insert BEFORE INSERT ON items
       WHEN NOT EXISTS (SELECT 1 FROM categories WHERE code = NEW.category)
       BEGIN SELECT RAISE(ABORT, 'Unknown product category'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_items_category_update BEFORE UPDATE OF category ON items
       WHEN NOT EXISTS (SELECT 1 FROM categories WHERE code = NEW.category)
       BEGIN SELECT RAISE(ABORT, 'Unknown product category'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_categories_delete_in_use BEFORE DELETE ON categories
       WHEN EXISTS (SELECT 1 FROM items WHERE category = OLD.code)
       BEGIN SELECT RAISE(ABORT, 'Category is in use by products; merge it into another category first'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_categories_code_update BEFORE UPDATE OF code ON categories
       WHEN EXISTS (SELECT 1 FROM items WHERE category = OLD.code)
       BEGIN SELECT RAISE(ABORT, 'Category code is referenced by products and cannot change'); END""",
]


# The same rules on PostgreSQL (functions + triggers, idempotent).
PG_CONSTRAINT_DDL = [
    """CREATE OR REPLACE FUNCTION faheem_items_category_check() RETURNS trigger AS $$
       BEGIN
         IF NOT EXISTS (SELECT 1 FROM categories WHERE code = NEW.category) THEN
           RAISE EXCEPTION 'Unknown product category' USING ERRCODE = 'check_violation';
         END IF;
         RETURN NEW;
       END $$ LANGUAGE plpgsql""",
    """CREATE OR REPLACE FUNCTION faheem_categories_in_use_check() RETURNS trigger AS $$
       BEGIN
         IF EXISTS (SELECT 1 FROM items WHERE category = OLD.code) THEN
           IF TG_OP = 'DELETE' THEN
             RAISE EXCEPTION 'Category is in use by products; merge it into another category first' USING ERRCODE = 'check_violation';
           END IF;
           RAISE EXCEPTION 'Category code is referenced by products and cannot change' USING ERRCODE = 'check_violation';
         END IF;
         RETURN COALESCE(NEW, OLD);
       END $$ LANGUAGE plpgsql""",
    "CREATE OR REPLACE TRIGGER trg_items_category_insert BEFORE INSERT ON items FOR EACH ROW EXECUTE FUNCTION faheem_items_category_check()",
    "CREATE OR REPLACE TRIGGER trg_items_category_update BEFORE UPDATE OF category ON items FOR EACH ROW EXECUTE FUNCTION faheem_items_category_check()",
    "CREATE OR REPLACE TRIGGER trg_categories_delete_in_use BEFORE DELETE ON categories FOR EACH ROW EXECUTE FUNCTION faheem_categories_in_use_check()",
    """CREATE OR REPLACE TRIGGER trg_categories_code_update BEFORE UPDATE OF code ON categories FOR EACH ROW
       WHEN (OLD.code IS DISTINCT FROM NEW.code) EXECUTE FUNCTION faheem_categories_in_use_check()""",
]


def ensure_constraints() -> None:
    if not (IS_SQLITE or IS_POSTGRES):
        return
    with engine.begin() as conn:
        if {"items", "categories"} <= _tables(conn):
            for ddl in (CONSTRAINT_DDL if IS_SQLITE else PG_CONSTRAINT_DDL):
                conn.execute(text(ddl))


def ensure_fts() -> None:
    if IS_POSTGRES:
        with engine.begin() as conn:
            if {"items", "customers"} <= _tables(conn):
                for ddl in PG_SEARCH_DDL:
                    conn.execute(text(ddl))
        return
    if not IS_SQLITE:
        return
    with engine.begin() as conn:
        for ddl in FTS_DDL:
            conn.execute(text(ddl))


def rebuild_fts() -> None:
    if not IS_SQLITE:          # the PostgreSQL index follows the table by itself
        return
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO items_fts(items_fts) VALUES('rebuild')"))


def _run_migrations() -> None:
    """Apply Alembic migrations to head (creates the schema on a fresh DB)."""
    from alembic import command
    from alembic.config import Config

    from app.config import BASE_DIR

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(cfg, "head")


def create_all_for_tests() -> None:
    """Fast schema creation for the test-suite (no migration bookkeeping)."""
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_fts()
    ensure_constraints()


def reset_db_for_tests() -> None:
    """Drop and recreate the whole schema (used between tests)."""
    import app.models  # noqa: F401

    if IS_SQLITE:
        with engine.begin() as conn:
            for trig in ("items_fts_au", "items_fts_ad", "items_fts_ai", "trg_items_category_insert",
                         "trg_items_category_update", "trg_categories_delete_in_use", "trg_categories_code_update"):
                conn.execute(text(f"DROP TRIGGER IF EXISTS {trig}"))
            conn.execute(text("DROP TABLE IF EXISTS items_fts"))
        Base.metadata.drop_all(bind=engine)
    else:
        with engine.begin() as conn:          # triggers and indexes go with their tables
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    create_all_for_tests()
    from app.seed import seed_defaults

    seed_defaults()


def init_db(use_migrations: bool = True) -> None:
    """Create all tables, FTS structures and seed baseline data.

    Idempotent. Schema changes only through versioned Alembic migrations, run by
    the upgrade guard (app/services/upgrade_service.py). A failed or data-changing
    upgrade stops start-up. PHARMACY_SKIP_MIGRATIONS=1 (tests) uses create_all.
    """
    import os

    import app.models  # noqa: F401  (register models on Base.metadata)

    if os.environ.get("PHARMACY_SKIP_MIGRATIONS") == "1":
        use_migrations = False
    if use_migrations:
        # Guarded: snapshot → migrate → reconcile, restored automatically on failure.
        # Never fall back to create_all: starting on a half-migrated database hides data loss.
        from app.services import upgrade_service

        if os.environ.get("PHARMACY_EXTERNAL_MIGRATIONS") == "1":
            from app.production import schema_state
            if schema_state()["state"] != "current":
                raise RuntimeError("Database migrations must complete before application startup")
        elif IS_POSTGRES:
            raise RuntimeError("PostgreSQL requires the production migration job (PHARMACY_EXTERNAL_MIGRATIONS=1)")
        else:
            upgrade_service.upgrade(backups=os.environ.get("PHARMACY_BACKUPS", "1") != "0")
    else:
        Base.metadata.create_all(bind=engine)
    ensure_fts()
    from app.seed import seed_defaults

    seed_defaults()
    ensure_constraints()
    ensure_indexes()
    if IS_SQLITE:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA optimize")
