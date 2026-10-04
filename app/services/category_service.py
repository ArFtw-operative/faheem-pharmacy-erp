"""Product category master, managed by the pharmacy (Categories & Forms).

Categories are rows in ``categories``; products refer to them by ``code``. The
database enforces the relationship (triggers: a product's category must exist;
a category in use cannot be deleted or re-coded). Users change the display
name, order and active flag freely; to retire a category that still has
products they merge it into another one, which moves those products in the
same transaction. Every change is audited.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from app import audit
from app.models import Category, Item, User

DEFAULTS = ["PHARMA", "GENERIC", "FMCG", "SURGICAL", "BABY", "BEVERAGES", "AYURVEDIC", "GENERAL", "OTHER"]
DEFAULT_NAMES = {"PHARMA": "Pharma", "GENERIC": "Generic", "FMCG": "FMCG", "SURGICAL": "Surgical", "BABY": "Baby care",
                 "BEVERAGES": "Beverages", "AYURVEDIC": "Ayurvedic", "GENERAL": "General", "OTHER": "Other"}


class CategoryError(Exception):
    pass


def code_for(name: str) -> str:
    """``OTC medicines`` → ``OTC_MEDICINES`` (the permanent key products store)."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper())).strip("_")[:30]


def active_codes(db: Session) -> list[str]:
    codes = list(db.scalars(select(Category.code).where(Category.is_active.is_(True))
                            .order_by(Category.sort_order, Category.code)))
    return codes or list(DEFAULTS)


def names(db: Session) -> dict[str, str]:
    """code → display name, for reports and screens (includes inactive categories)."""
    return {c.code: c.name or c.code.title() for c in db.scalars(select(Category))}


def options(db: Session) -> list[dict]:
    return [{"code": c.code, "name": c.name or c.code.title()} for c in db.scalars(
        select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order, Category.code))]


def listing(db: Session) -> list[dict]:
    counts = dict(db.execute(select(Item.category, func.count(Item.id))
                             .where(Item.deleted_at.is_(None)).group_by(Item.category)).all())
    rows = db.scalars(select(Category).order_by(Category.sort_order, Category.code))
    return [{"code": c.code, "name": c.name or c.code.title(), "sort_order": c.sort_order,
             "active": bool(c.is_active), "products": int(counts.get(c.code, 0))} for c in rows]


def ensure(db: Session, code_or_name: str | None, *, user: User | None = None) -> str:
    """The code for a category, registering it when a document introduces a new one."""
    code = code_for(code_or_name or "")
    if not code:
        return active_codes(db)[0]
    if db.get(Category, code) is None:
        db.add(Category(code=code, name=str(code_or_name).strip().title()[:60] or code.title(), sort_order=_next_order(db)))
        db.flush()
        audit.record(db, action=audit.A_CREATE, entity_type="category", entity_id=code, user=user,
                     after={"code": code}, details=f"Category {code} added from a document")
    return code


def _next_order(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.max(Category.sort_order), 0))) or 0) + 10


def _get(db: Session, code: str) -> Category:
    cat = db.get(Category, code)
    if cat is None:
        raise CategoryError(f"No category {code}")
    return cat


def create(db: Session, name: str, *, user: User | None = None) -> Category:
    name = " ".join(str(name or "").split())[:60]
    code = code_for(name)
    if not code:
        raise CategoryError("Enter a category name")
    if db.get(Category, code) is not None:
        raise CategoryError(f"A category {code} already exists")
    if db.scalar(select(Category).where(func.lower(Category.name) == name.lower())) is not None:
        raise CategoryError(f"A category named {name} already exists")
    cat = Category(code=code, name=name, sort_order=_next_order(db), is_active=True)
    db.add(cat)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="category", entity_id=code, user=user,
                 after={"code": code, "name": name}, details=f"Category {name} created")
    return cat


def update(db: Session, code: str, *, name: str | None = None, active: bool | None = None,
           user: User | None = None) -> Category:
    cat = _get(db, code)
    before = {"name": cat.name, "active": cat.is_active}
    if name is not None:
        name = " ".join(str(name).split())[:60]
        if not name:
            raise CategoryError("Enter a category name")
        clash = db.scalar(select(Category).where(func.lower(Category.name) == name.lower(), Category.code != code))
        if clash is not None:
            raise CategoryError(f"A category named {name} already exists")
        cat.name = name
    if active is not None:
        if not active and cat.is_active and len(active_codes(db)) <= 1:
            raise CategoryError("At least one category must stay active")
        cat.is_active = active
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="category", entity_id=code, user=user, before=before,
                 after={"name": cat.name, "active": cat.is_active}, details=f"Category {code} updated")
    return cat


def move(db: Session, code: str, step: int, *, user: User | None = None) -> None:
    """Move a category up (-1) or down (+1) in every list and report."""
    rows = list(db.scalars(select(Category).order_by(Category.sort_order, Category.code)))
    i = next((k for k, c in enumerate(rows) if c.code == code), None)
    if i is None:
        raise CategoryError(f"No category {code}")
    j = max(0, min(len(rows) - 1, i + step))
    rows.insert(j, rows.pop(i))
    for k, c in enumerate(rows):
        c.sort_order = (k + 1) * 10
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="category", entity_id=code, user=user,
                 after={"position": j + 1}, details=f"Category {code} moved to position {j + 1}")


def merge(db: Session, source: str, target: str, *, user: User | None = None) -> int:
    """Move every product of ``source`` to ``target`` and remove ``source`` (one transaction)."""
    if source == target:
        raise CategoryError("Choose a different category to merge into")
    src, dst = _get(db, source), _get(db, target)
    moved = db.execute(sql_update(Item).where(Item.category == source).values(category=target)
                       .execution_options(synchronize_session=False)).rowcount or 0
    db.delete(src)
    db.flush()
    db.expire_all()
    audit.record(db, action=audit.A_UPDATE, entity_type="category", entity_id=source, user=user,
                 before={"code": source, "name": src.name}, after={"merged_into": target, "products_moved": moved},
                 details=f"Category {src.name} merged into {dst.name}: {moved} product(s) moved")
    return moved


def delete(db: Session, code: str, *, user: User | None = None) -> None:
    cat = _get(db, code)
    used = db.scalar(select(func.count(Item.id)).where(Item.category == code)) or 0
    if used:
        raise CategoryError(f"{cat.name} is used by {used} product(s) — merge it into another category instead")
    db.delete(cat)
    db.flush()
    audit.record(db, action=audit.A_DELETE, entity_type="category", entity_id=code, user=user,
                 before={"code": code, "name": cat.name}, details=f"Category {cat.name} deleted")
