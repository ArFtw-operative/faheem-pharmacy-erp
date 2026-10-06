"""Phase 1 foundation: schema, seed data, sequences, audit."""
from __future__ import annotations

from sqlalchemy import inspect, select

from app import audit
from app.database import engine
from app.models import AuditLog, Permission, Role, Setting, User
from app.permissions import PERMISSION_CATALOG
from app.sequences import (
    article_prefix,
    next_article_id_for,
    next_customer_id,
    next_employee_id,
    next_invoice_no,
)


def test_all_core_tables_exist():
    tables = set(inspect(engine).get_table_names())
    expected = {
        "items", "batches", "customers", "sales", "sale_items",
        "purchases", "purchase_items", "purchase_returns", "suppliers",
        "users", "roles", "permissions", "role_permissions", "audit_logs",
        "settings", "expiry_alerts", "stock_adjustments", "supplier_product_maps",
        "notifications", "login_sessions", "number_sequences",
    }
    assert expected.issubset(tables), expected - tables
    if engine.dialect.name == "sqlite":
        assert "items_fts" in tables                     # FTS5 search table
    else:
        from sqlalchemy import text
        with engine.connect() as c:                       # PostgreSQL: GIN search index on items
            assert c.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_items_search'")).scalar() == 1


def test_seed_defaults(db):
    from app.security import find_user, verify_password
    from app.seed import DEFAULT_ADMIN

    admin = db.scalar(select(User).where(User.username == "Owner"))
    assert admin is not None and admin.full_name == "Owner"
    assert verify_password(DEFAULT_ADMIN["password"], admin.password_hash) and not admin.must_change_password
    assert find_user(db, "  OWNER ") is admin  # case and spacing do not matter at sign-in
    assert admin.employee_id == "EMP0001"
    assert admin.role.name == "Administrator"
    assert len(admin.role.permissions) == len(PERMISSION_CATALOG)
    assert db.scalar(select(Role).where(Role.name == "Sales Staff")) is not None
    assert db.query(Setting).count() >= 13
    assert db.query(Permission).count() == len(PERMISSION_CATALOG)


def test_default_account_is_only_created_on_an_empty_database(db):
    from app.seed import seed_defaults

    before = db.query(User).count()
    seed_defaults()
    assert db.query(User).count() == before == 1


def test_sales_staff_role_template_excludes_history_and_profit(db):
    role = db.scalar(select(Role).where(Role.name == "Sales Staff"))
    codes = role.permission_codes()
    assert "sales.create" in codes
    assert "customers.create" in codes
    assert "inventory.view" in codes
    assert "sales.view_history" not in codes
    assert "sales.view_profit" not in codes


def test_sequences_are_unique_and_formatted(db):
    assert article_prefix("Paracetamol 500mg") == "PAR"
    assert article_prefix("Avil 25") == "AVI"
    assert article_prefix("ab") == "ABX"
    a1 = next_article_id_for(db, "Paracetamol 500mg Tablet")
    a2 = next_article_id_for(db, "Paracetamol 650mg Tablet")
    assert a1 == "PAR0001" and a2 == "PAR0002"
    assert next_article_id_for(db, "Azithral 500") == "AZI0001"
    assert next_customer_id(db) == "CUST000001"
    # seed consumes EMP0001 for the default admin, so the next is EMP0002
    assert next_employee_id(db) == "EMP0002"
    inv = next_invoice_no(db)
    assert inv.startswith("INV-") and inv.endswith("0001")
    db.commit()


def test_audit_entry_records_before_after(db):
    setting = db.get(Setting, "pharmacy_name")
    audit.record(
        db, action=audit.A_UPDATE, entity_type="setting", entity_id="pharmacy_name",
        username="tester", before={"value": "old"}, after={"value": "new"},
    )
    db.commit()
    log = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()))
    assert log.action == "UPDATE"
    assert log.before == {"value": "old"}
    assert log.after == {"value": "new"}
    assert log.username == "tester"


def test_category_master_includes_general_and_registers_new_codes(db):
    from app.models import Category
    from app.services import inventory_service as inv

    codes = inv.categories(db)
    assert "GENERAL" in codes and codes[0] == "PHARMA"
    item = inv.create_item(db, name="Hand Wash", category="personal care")
    db.commit()
    assert item.category == "PERSONAL_CARE" and db.get(Category, "PERSONAL_CARE") is not None
    assert "PERSONAL_CARE" in inv.categories(db)
