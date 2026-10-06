"""RBAC permission catalogue and default role templates.

Permissions are granular (module.action). Roles are seeded from the templates
below; there is no permission-management screen (authorization will be designed
separately). ``manage.py`` assigns users to roles.
"""
from __future__ import annotations

from app.models import Role, User

# code -> (module, description)
PERMISSION_CATALOG: dict[str, tuple[str, str]] = {
    # Inventory
    "inventory.view": ("inventory", "View item master and stock"),
    "inventory.create": ("inventory", "Add items to the master"),
    "inventory.edit": ("inventory", "Edit item master / batches"),
    "inventory.delete": ("inventory", "Deactivate or delete items"),
    "inventory.export": ("inventory", "Export inventory to Excel/CSV"),
    # Sales / billing
    "sales.create": ("sales", "Create a sale / bill"),
    "sales.view_own": ("sales", "View own sales"),
    "sales.view_history": ("sales", "View all sales history"),
    "sales.view_profit": ("sales", "View profit/loss figures"),
    "sales.discount": ("sales", "Apply discounts and vouchers"),
    "sales.void": ("sales", "Void or cancel a sale"),
    "sales.refund": ("sales", "Process a return / refund against an invoice"),
    "sales.refund_override": ("sales", "Approve refunds above threshold or to a different method"),
    "sales.export": ("sales", "Export sales data"),
    "billing.print": ("billing", "Print or reprint invoices"),
    "whatsapp.send": ("billing", "Send / resend a sale's invoice on WhatsApp (customer-requested)"),
    "settings.manage": ("settings", "Settings: pair or log out WhatsApp, invoice message and image"),
    # Purchases
    "purchase.view": ("purchase", "View purchases"),
    "purchase.create": ("purchase", "Import, enter and correct purchase invoices (drafts)"),
    "purchase.post": ("purchase", "Post a reviewed purchase invoice into stock"),
    "supplier.manage": ("purchase", "Add and edit suppliers and their product mappings"),
    "purchase.return": ("purchase", "Create purchase returns"),
    # Customers (billing parties)
    "customers.create": ("customers", "Add a customer while billing"),
    "customers.view": ("customers", "Customer directory, invoices, activity and customer reports"),
    "customers.edit": ("customers", "Edit customer details and notes"),
    "followups.manage": ("customers", "Create, complete and reschedule customer follow-ups"),
    # Reports
    "reports.sales": ("reports", "View sales reports"),
    "reports.purchase": ("reports", "View purchase reports"),
    "reports.expiry": ("reports", "View expiry reports"),
    "reports.financials": ("reports", "View financial / profit reports"),
    "reports.export": ("reports", "Export reports"),
    "reports.counter": ("reports", "Counter Report: the day's sales and collections by payment mode"),

    "udhaar.view": ("udhaar", "Udhaar Ledger: what customers owe, ledgers and statements"),
    "udhaar.receive": ("udhaar", "Receive Udhaar payments and send Udhaar reminders"),
    "udhaar.manage": ("udhaar", "Set customers' Udhaar limit and days, and opening balances"),
    # Expiry
    "expiry.view": ("expiry", "View expiry alerts"),
    "expiry.snooze": ("expiry", "Snooze an expiry reminder"),
    "expiry.settle": ("expiry", "Settle an expiry (debits stock)"),
    # Stock adjustments
    "adjustment.create": ("adjustment", "Record loose/damage write-offs"),
    # Rack / box locations
    "rack.view": ("rack", "See racks, boxes and product locations"),
    "rack.create": ("rack", "Create racks"),
    "rack.edit": ("rack", "Edit racks and location settings"),
    "rack.disable": ("rack", "Disable and enable racks"),
    "rack.assign": ("rack", "Assign or move a product's location"),
    "rack.bulk_move": ("rack", "Move many products to a rack at once"),
    "rack.history.view": ("rack", "See location history"),
    "rack.report.view": ("rack", "Rack inventory reports"),
    "rack.snapshot.view": ("rack", "Historical (as-of) rack inventory"),
    "box.manage": ("rack", "Create, rename and disable boxes"),
}
RACK_PHARMACIST = {"rack.view", "rack.assign", "rack.bulk_move", "rack.history.view", "rack.report.view", "rack.snapshot.view"}

ALL_PERMISSIONS = set(PERMISSION_CATALOG)

# Default role templates. Admin can change these at runtime.
DEFAULT_ROLES: dict[str, dict] = {
    "Administrator": {
        "description": "Full system access",
        "is_system": True,
        "permissions": ALL_PERMISSIONS,
    },
    "Manager": {
        "description": "Operational management without account administration",
        "is_system": True,
        "permissions": ALL_PERMISSIONS - {"settings.manage"},      # only Administrators pair / change WhatsApp
    },
    "Pharmacist": {
        "description": "Dispensing, inventory and expiry management",
        "is_system": False,
        "permissions": {
            "inventory.view", "inventory.create", "inventory.edit",
            "inventory.export",
            "sales.create", "sales.view_own", "sales.view_history", "sales.discount",
            "sales.refund",
            "billing.print", "whatsapp.send",
            "purchase.view", "purchase.create", "purchase.post", "purchase.return", "supplier.manage",
            "customers.create", "customers.view", "customers.edit", "followups.manage",
            "reports.sales", "reports.expiry", "reports.export",
            "expiry.view", "expiry.snooze", "expiry.settle",
            "adjustment.create",
            "udhaar.view", "udhaar.receive", "udhaar.manage", "reports.counter",
            } | RACK_PHARMACIST,
    },
    "Sales Staff": {
        "description": "Baseline sales role: no sales history or profit/loss",
        "is_system": True,
        "permissions": {
            "inventory.view",
            "sales.create", "sales.view_own", "sales.discount", "sales.refund",
            "billing.print", "whatsapp.send",
            "customers.create", "customers.view", "followups.manage", "rack.view",
            "udhaar.view", "udhaar.receive",
            },
    },
    "Counter Manager": {
        "description": "Billing supervisor: sales, discounts, refunds and history",
        "is_system": True,
        "permissions": {
            "inventory.view",
            "sales.create", "sales.view_own", "sales.view_history", "sales.discount", "sales.refund",
            "billing.print", "whatsapp.send",
            "customers.create", "customers.view", "followups.manage", "rack.view",
            "udhaar.view", "udhaar.receive", "reports.counter",
            },
    },
    "Accountant": {
        "description": "Financial reporting and purchasing",
        "is_system": False,
        "permissions": {
            "purchase.view", "purchase.return", "supplier.manage",
            "customers.view", "reports.sales", "reports.purchase", "reports.expiry",
            "reports.financials", "reports.export",
            "sales.view_history", "sales.view_profit", "sales.export",
            "inventory.view", "inventory.export",
            "rack.view", "rack.history.view", "rack.report.view", "rack.snapshot.view",
            "udhaar.view", "reports.counter",
            },
    },
}


def has_permission(user: User | None, code: str) -> bool:
    if user is None or not user.is_active:
        return False
    role: Role | None = user.role
    if role is None:
        return False
    return code in role.permission_codes()


def has_any(user: User | None, codes: set[str]) -> bool:
    if user is None:
        return False
    role = user.role
    if role is None:
        return False
    return bool(role.permission_codes() & codes)
