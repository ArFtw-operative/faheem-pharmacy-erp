"""Item form master: how each kind of product is counted and sold.

A form says what stock is counted in (tablets, bottles, pieces), the retail pack it comes in
(strip, bottle, box) and whether that pack holds a count the operator enters (tablets per
strip) or is one whole container. The built-in list covers medicines, liquids, topicals,
injectables, IV fluids and devices; the pharmacy adds its own forms from any screen that
asks for a form ("Create new form…" at the bottom of the list). The list is data, not code.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import ItemForm, User
from app.services import units

# code, name, base (stock) unit, retail pack unit, counted, content unit
BUILTIN: tuple[tuple[str, str, str, str, bool, str], ...] = (
    ("TABLET", "Tablets", "TABLET", "STRIP", True, ""),
    ("CAPSULE", "Capsules", "CAPSULE", "STRIP", True, ""),
    ("SOFTGEL", "Softgel capsules", "CAPSULE", "STRIP", True, ""),
    ("SYRUP", "Syrup bottles", "BOTTLE", "BOTTLE", False, "ML"),
    ("SUSPENSION", "Suspension bottles", "BOTTLE", "BOTTLE", False, "ML"),
    ("DROPS", "Drop bottles (eye / ear / nasal / oral)", "BOTTLE", "BOTTLE", False, "ML"),
    ("LOTION", "Lotion / shampoo / liquid bottles", "BOTTLE", "BOTTLE", False, "ML"),
    ("SPRAY", "Sprays", "BOTTLE", "BOTTLE", False, "ML"),
    ("CREAM", "Cream tubes", "TUBE", "TUBE", False, "G"),
    ("OINTMENT", "Ointment tubes", "TUBE", "TUBE", False, "G"),
    ("GEL", "Gel tubes", "TUBE", "TUBE", False, "G"),
    ("POWDER", "Powder packs / jars", "PACK", "PACK", False, "G"),
    ("SACHET", "Sachets", "SACHET", "SACHET", False, "G"),
    ("INJECTION", "Injection vials", "VIAL", "VIAL", False, "ML"),
    ("VIAL", "Vials (box of vials)", "VIAL", "BOX", True, "ML"),
    ("AMPOULE", "Ampoules (box of ampoules)", "AMPOULE", "BOX", True, "ML"),
    ("IV_FLUID", "IV fluid bottles / bags", "BOTTLE", "BOTTLE", False, "ML"),
    ("INHALER", "Inhalers", "PIECE", "PIECE", False, ""),
    ("ROTACAP", "Rotacaps / inhalation capsules", "CAPSULE", "STRIP", True, ""),
    ("RESPULE", "Respules (pack of respules)", "PIECE", "PACK", True, "ML"),
    ("SUPPOSITORY", "Suppositories", "PIECE", "STRIP", True, ""),
    ("SOAP", "Soap bars", "PIECE", "PIECE", False, "G"),
    ("SYRINGE", "Syringes", "PIECE", "BOX", True, ""),
    ("NEEDLE", "Needles", "PIECE", "BOX", True, ""),
    ("CANNULA", "Cannulas / IV sets", "PIECE", "BOX", True, ""),
    ("DEVICE", "Pieces / devices", "PIECE", "PIECE", False, ""),
    ("TEST_STRIP", "Test strips (box of strips)", "PIECE", "BOX", True, ""),
    ("BANDAGE", "Bandages / dressings", "PIECE", "PIECE", False, ""),
    ("KIT", "Kits", "KIT", "KIT", False, ""),
    ("PAIR", "Pairs (gloves, supports)", "PAIR", "PACK", False, ""),
    ("JAR", "Jars", "JAR", "JAR", False, "G"),
    ("BOX", "Whole boxes", "BOX", "BOX", False, ""),
    ("PACK", "Whole packs", "PACK", "PACK", False, ""),
    ("BOTTLE", "Bottles", "BOTTLE", "BOTTLE", False, "ML"),
    ("TUBE", "Tubes", "TUBE", "TUBE", False, "G"),
    ("UNIT", "Other counted units", "UNIT", "PACK", True, ""),
)
_BUILTIN_CODES = {b[0] for b in BUILTIN}


class FormError(Exception):
    pass


def code_for(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper())).strip("_")[:20]


def _row(code, name, base, pack, counted, content, *, builtin=True, active=True, order=0) -> dict:
    return {"code": code, "name": name, "base_unit": base, "pack_unit": pack, "counted": bool(counted),
            "content_unit": content, "builtin": builtin, "active": active, "sort_order": order,
            "dosage_form": "" if code in ("UNIT", "BOX", "PACK", "BOTTLE", "TUBE", "JAR") else code}


def seed(db: Session) -> int:
    """Add any built-in form the database does not have yet (never changes existing rows)."""
    have = set(db.scalars(select(ItemForm.code)))
    added = 0
    for i, (code, name, base, pack, counted, content) in enumerate(BUILTIN):
        if code not in have:
            db.add(ItemForm(code=code, name=name, base_unit=base, pack_unit=pack, counted=counted,
                            content_unit=content, sort_order=(i + 1) * 10, builtin=True, is_active=True))
            added += 1
    if added:
        db.flush()
    register(db)
    return added


def listing(db: Session, *, include_inactive: bool = False) -> list[dict]:
    rows = list(db.scalars(select(ItemForm).order_by(ItemForm.sort_order, ItemForm.name)))
    if not rows:
        return [_row(*b, order=(i + 1) * 10) for i, b in enumerate(BUILTIN)]
    return [_row(r.code, r.name, r.base_unit, r.pack_unit, r.counted, r.content_unit, builtin=bool(r.builtin),
                 active=bool(r.is_active), order=r.sort_order)
            for r in rows if include_inactive or r.is_active]


def get(db: Session, code: str) -> dict | None:
    return next((f for f in listing(db, include_inactive=True) if f["code"] == code), None)


def register(db: Session) -> None:
    """Let the unit vocabulary accept the pharmacy's own form codes as dosage forms."""
    units.register_forms(r.code for r in db.scalars(select(ItemForm).where(ItemForm.builtin.is_(False))))


def create(db: Session, *, name: str, base_unit: str, pack_unit: str = "", counted: bool = False,
           content_unit: str = "", user: User | None = None) -> dict:
    name = " ".join(str(name or "").split())[:60]
    code = code_for(name)
    if len(code) < 2:
        raise FormError("Give the form a name, e.g. Mouthwash bottles")
    if db.get(ItemForm, code) is not None or code in _BUILTIN_CODES:
        raise FormError(f"A form called {name} already exists")
    if db.scalar(select(ItemForm.code).where(func.lower(ItemForm.name) == name.lower())):
        raise FormError(f"A form called {name} already exists")
    base = units.normalize_unit(base_unit, units.BASE_UNITS, "")
    if not base:
        raise FormError("Choose what stock is counted in (tablet, bottle, piece …)")
    pack = units.normalize_unit(pack_unit or base, units.PACK_UNITS, "") or ("PACK" if counted else base)
    if not counted and pack not in units.PACK_UNITS:
        pack = "PACK"
    content = str(content_unit or "").strip().upper()
    if content not in ("", "ML", "G", "MG", "L", "KG"):
        raise FormError("Content is measured in ML, G, MG, L or KG")
    order = (db.scalar(select(func.max(ItemForm.sort_order))) or 0) + 10
    form = ItemForm(code=code, name=name, base_unit=base, pack_unit=pack, counted=bool(counted), content_unit=content,
                    sort_order=order, builtin=False, is_active=True, created_by=user.id if user else None)
    db.add(form)
    db.flush()
    register(db)
    audit.record(db, action=audit.A_CREATE, entity_type="item_form", entity_id=code, user=user,
                 after={"name": name, "base_unit": base, "pack_unit": pack, "counted": bool(counted), "content_unit": content},
                 details=f"Item form {name} added")
    return get(db, code)


def set_active(db: Session, code: str, active: bool, *, user: User | None = None) -> dict:
    form = db.get(ItemForm, code)
    if form is None:
        raise FormError("Form not found")
    form.is_active = bool(active)
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="item_form", entity_id=code, user=user,
                 after={"is_active": bool(active)}, details=f"Item form {form.name} {'enabled' if active else 'hidden'}")
    return get(db, code)


def definition(db: Session, code: str) -> tuple[str, str, str]:
    """(base unit, pack unit, dosage form) a product of this form is stocked with."""
    f = get(db, code)
    if f is None:
        raise FormError("Choose the item form")
    return f["base_unit"], f["pack_unit"], f["dosage_form"]
