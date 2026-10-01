"""Idempotent baseline data seeding: settings, permissions, roles, admin user."""
from __future__ import annotations

import os

from sqlalchemy import select

from app.config import TIMEZONE
from app.database import session_scope
from app.models import Permission, Role, Setting, User
from app.permissions import DEFAULT_ROLES, PERMISSION_CATALOG
from app.security import hash_password

DEFAULT_SETTINGS: list[tuple[str, str, str, str]] = [
    # key, value, value_type, label
    ("pharmacy_name", "Faheem Pharmacy", "str", "Pharmacy name"),
    ("tagline", "Your health. Our everyday commitment.", "str", "Tagline"),
    ("contact_numbers", "7989690883 / 9381470883", "str", "Contact numbers"),
    ("pharmacy_email", "", "str", "Email address"),
    ("pharmacy_website", "", "str", "Website"),
    ("gst_number", "", "str", "GST number"),
    ("show_gst", "1", "bool", "Show GST on invoice"),
    ("drug_license_number", "", "str", "Drug License number"),
    ("show_drug_license", "1", "bool", "Show Drug License on invoice"),
    ("address", "", "text", "Address"),
    ("logo_path", "", "str", "Logo file path"),
    ("latitude", "17.360102634384617", "str", "Latitude"),
    ("longitude", "78.49319592604937", "str", "Longitude"),
    ("invoice_footer", "Thank you. Get well soon!", "text", "Invoice footer"),
    ("expiry_threshold_days", "90", "int", "Expiry alert threshold (days)"),
    ("followup_default_days", "7", "int", "Default follow-up cadence (days)"),
    ("round_off_mode", "NEAREST_RUPEE", "str", "Rounding rule"),
    ("max_discount_pct", "20", "float", "Maximum discount per item and per bill (%)"),
    ("timezone", TIMEZONE, "str", "Business timezone"),
    ("refund_manager_threshold", "1000", "float", "Refund amount above which manager approval is required"),
    ("idle_timeout_minutes", "3", "int", "Idle screen after this many minutes of inactivity"),
    ("lock_timeout_minutes", "12", "int", "Lock the workspace (require sign-in) after this many minutes"),
]


# First account on a brand-new database (never added to a database that already has users).
DEFAULT_ADMIN = {
    "username": "Syeed Faheem",
    "full_name": "Syeed Faheem",
    "password": os.environ.get("PHARMACY_ADMIN_PASSWORD", ""),
    "role": "Administrator",
}


def seed_defaults() -> None:
    with session_scope() as db:
        # Settings
        for key, value, vtype, label in DEFAULT_SETTINGS:
            if db.get(Setting, key) is None:
                db.add(Setting(key=key, value=value, value_type=vtype, label=label))

        # Product category master
        from app.models import Category
        from app.services.category_service import DEFAULT_NAMES, DEFAULTS as DEFAULT_CATEGORIES

        have = set(db.scalars(select(Category.code)))
        for i, code in enumerate(DEFAULT_CATEGORIES):
            if code not in have:
                db.add(Category(code=code, name=DEFAULT_NAMES.get(code, code.title()), sort_order=(i + 1) * 10))
        db.flush()

        # Permissions
        existing = {p.code: p for p in db.scalars(select(Permission)).all()}
        introduced = set()          # permissions new in this release: default roles receive them once
        for code, (module, desc) in PERMISSION_CATALOG.items():
            if code not in existing:
                introduced.add(code)
                perm = Permission(code=code, module=module, description=desc)
                db.add(perm)
                existing[code] = perm
        db.flush()

        # Roles
        role_objs: dict[str, Role] = {r.name: r for r in db.scalars(select(Role)).all()}
        for name, spec in DEFAULT_ROLES.items():
            role = role_objs.get(name)
            if role is None:
                role = Role(name=name, description=spec["description"], is_system=spec["is_system"])
                db.add(role)
                db.flush()
                role_objs[name] = role
            if not role.permissions:
                role.permissions = [existing[c] for c in spec["permissions"] if c in existing]
            else:
                held = {p.code for p in role.permissions}
                role.permissions.extend(existing[c] for c in spec["permissions"] if c in introduced and c not in held)
        db.flush()

        # Permissions renamed by the ERP reset keep their holders; codes of removed
        # features (CRM, dashboard, settings, user admin) are then deleted.
        renamed = {"crm.create": "customers.create"}
        for role in role_objs.values():
            held = {p.code for p in role.permissions}
            for old, new in renamed.items():
                if old in held and new not in held and new in existing:
                    role.permissions.append(existing[new])
        admin = role_objs.get("Administrator")
        if admin is not None:
            held = {p.code for p in admin.permissions}
            admin.permissions.extend(existing[c] for c in PERMISSION_CATALOG if c not in held)
        db.flush()
        for code, perm in list(existing.items()):
            if code not in PERMISSION_CATALOG:
                for role in role_objs.values():
                    if perm in role.permissions:
                        role.permissions.remove(perm)
                db.delete(perm)
        db.flush()

        # Admin user
        has_users = db.scalar(select(User.id).limit(1)) is not None
        if not has_users:
            if not DEFAULT_ADMIN["password"]:
                raise RuntimeError("Set PHARMACY_ADMIN_PASSWORD to provision the first owner account")
            from app.sequences import next_employee_id

            admin = User(
                employee_id=next_employee_id(db),
                username=DEFAULT_ADMIN["username"],
                full_name=DEFAULT_ADMIN["full_name"],
                password_hash=hash_password(DEFAULT_ADMIN["password"]),
                role_id=role_objs[DEFAULT_ADMIN["role"]].id,
                is_active=True,
                must_change_password=False,
            )
            db.add(admin)
