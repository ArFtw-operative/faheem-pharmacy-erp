"""Factory-reset business data (test/demo data) while keeping the setup.

Removed: products, batches, stock ledger, suppliers, purchases and review
queue, customers and follow-ups, sales/returns/payments, parked bills and
drafts, expiry alerts, adjustments,
notifications, audit history, and the document/ID number sequences.

Kept: users, roles and permissions, settings,
login sessions and the employee-ID sequence.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import audit
from app.models import User

# children before parents
WIPE_TABLES = (
    "workspace_snapshots", "udhaar_reminders", "whatsapp_messages", "udhaar_payments", "udhaar_entries",
    "manual_bill_items", "manual_bills", "counter_day_closes", "customer_followups", "item_uoms",
    "mapping_history", "import_metrics", "supplier_packaging_aliases", "supplier_invoice_profiles", "product_packagings",
    "sale_return_items", "sale_returns", "sale_payments",
    "parked_sales", "inventory_movements", "stock_adjustments", "expiry_alerts",
    "purchase_returns", "supplier_product_maps", "sale_items", "sales", 
    "customers", "purchase_items", "batches", "purchases", "items", "suppliers",
    "notifications", "audit_logs",
)
KEEP_SEQUENCES = ("employee_id",)


def delete_order() -> list[str]:
    """Children before parents, from the schema's own foreign keys — PostgreSQL checks every
    reference immediately, so a hand-kept order breaks as soon as a new reference appears."""
    import app.models  # noqa: F401
    from app.database import Base

    ordered = [t.name for t in reversed(Base.metadata.sorted_tables) if t.name in WIPE_TABLES]
    return ordered + [t for t in WIPE_TABLES if t not in ordered]


def counts(db: Session) -> dict[str, int]:
    return {t: db.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0 for t in WIPE_TABLES}


CUSTOMER_TABLES = ("customers", "customer_followups")


def wipe(db: Session, *, user: User | None = None, keep_customers: bool = False) -> dict[str, int]:
    """Remove the business data. ``keep_customers`` (testing reset): customers and their follow-ups stay
    (a follow-up's link to a removed bill is cleared); WhatsApp settings are settings and always stay."""
    before = counts(db)
    if keep_customers:
        db.execute(text("UPDATE customer_followups SET source_sale_id = NULL"))
    for table in delete_order():
        if keep_customers and table in CUSTOMER_TABLES:
            continue
        db.execute(text(f'DELETE FROM "{table}"'))
    keep = ", ".join(f"'{k}'" for k in KEEP_SEQUENCES + (("customer_id",) if keep_customers else ()))
    db.execute(text(f"DELETE FROM number_sequences WHERE key NOT IN ({keep})"))
    if db.get_bind().dialect.name == "sqlite":   # PostgreSQL enforces every reference as it deletes
        db.execute(text("INSERT INTO items_fts(items_fts) VALUES('rebuild')"))
        dangling = db.execute(text("PRAGMA foreign_key_check")).fetchall()
        if dangling:
            raise RuntimeError(f"Reset would leave broken references: {dangling[:5]}")
    audit.record(db, action=audit.A_DELETE, entity_type="system", entity_id="data-reset", user=user, before=before,
                 details="Test data reset: stock, products, purchases and sales removed" + ("; customers kept" if keep_customers else "; customers removed"))
    db.flush()
    return before
