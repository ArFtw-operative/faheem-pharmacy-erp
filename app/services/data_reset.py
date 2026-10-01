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
    "whatsapp_messages", "customer_followups", "item_uoms",
    "sale_return_items", "sale_returns", "sale_payments",
    "parked_sales", "inventory_movements", "stock_adjustments", "expiry_alerts",
    "purchase_returns", "supplier_product_maps", "sale_items", "sales", 
    "customers", "purchase_items", "batches", "purchases", "items", "suppliers",
    "notifications", "audit_logs",
)
KEEP_SEQUENCES = ("employee_id",)


def counts(db: Session) -> dict[str, int]:
    return {t: db.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0 for t in WIPE_TABLES}


def wipe(db: Session, *, user: User | None = None) -> dict[str, int]:
    before = counts(db)
    for table in WIPE_TABLES:
        db.execute(text(f'DELETE FROM "{table}"'))
    keep = ", ".join(f"'{k}'" for k in KEEP_SEQUENCES)
    db.execute(text(f"DELETE FROM number_sequences WHERE key NOT IN ({keep})"))
    if db.get_bind().dialect.name == "sqlite":   # PostgreSQL enforces every reference as it deletes
        db.execute(text("INSERT INTO items_fts(items_fts) VALUES('rebuild')"))
        dangling = db.execute(text("PRAGMA foreign_key_check")).fetchall()
        if dangling:
            raise RuntimeError(f"Reset would leave broken references: {dangling[:5]}")
    audit.record(db, action=audit.A_DELETE, entity_type="system", entity_id="data-reset", user=user,
                 before=before, details="Business data reset: all test stock, products, customers and sales removed")
    db.flush()
    return before
