"""Unit-of-measure (UOM) configuration: automatic detection and repack conversion.

Every product carries its UOM on the item master and every module (purchase,
inventory, POS, returns, adjustments) converts only through that configuration
and :mod:`app.services.units`:

    base / sale unit   the unit stock is counted and sold in (TABLET, BOTTLE, TUBE …)
    purchase unit      the unit suppliers invoice (STRIP, BOTTLE, BOX …)
    units_per_pack     base units in one purchase unit (conversion)
    content            what one unit contains (200 mL, 30 g) — metadata, never stock
    loose_sale         defaults from sale unit / form / pack; users can override it

Detection (:func:`resolve`) reads the printed pack together with the product's
dosage form (from the master, the generic name or the product name) and is
applied automatically — on creation, before any stock is received, and once at
startup for products that were never configured. Users only intervene to
correct an exception; such a correction is marked MANUAL and never overridden.

Changing the UOM of a product whose stock is still counted per pack repacks it
through the ledger (``REPACK_OUT`` N packs, ``REPACK_IN`` N × units); each batch
keeps its strip MRP, so one unit costs strip MRP ÷ units per strip.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.models import Batch, Item, User
from app.services import stock_ledger, units

log = logging.getLogger("pharmacy.packaging")


class PackagingError(Exception):
    pass


# dosage form from free text (name / generic); order matters (first hit wins)
_FORM_WORDS = [
    ("SUPPOSITORY", r"\b(SUPP|SUPPOSITORY|SUPPOSITORIES)\b"),
    ("TABLET", r"\b(TAB|TABS|TABLET|TABLETS|TB|LOZ|LOZENGE|LOZENGES|CHEWABLE)\b"),
    # "KNEE CAP" / "ANKLE CAP" are supports, not capsules
    ("CAPSULE", r"(?<!KNEE )(?<!ANKLE )(?<!ELBOW )(?<!NIPPLE )(?<!SHOWER )\b(CAP|CAPS|CAPSULE|CAPSULES|SOFTGEL|SOFTGELS|ROTACAP|ROTACAPS)\b"),
    ("DEVICE", r"\b(BELT|BANDAGE|CREPE|SUPPORT|KNEE|WRIST|ANKLE|ELBOW|BINDER|BRACE|COLLAR|SPLINT|VISSCO|TYNOR|GLUCOMETER|LANCET|LANCETS|SYRINGE|MASK|GLOVES|THERMOMETER|NEBULIZER|DIAPER|DIAPERS|PAD|PADS|CONDOM|CONDOMS|CARD|STRIPS|CATHETER|URINE BAG|COTTON|GAUZE|PLASTER)\b"),
    ("CREAM", r"\b(CREAM|CRM)\b"),
    ("OINTMENT", r"\b(OINT|OINTMENT)\b"),
    ("GEL", r"\bGEL\b"),
    ("SYRUP", r"\b(SYP|SYR|SYRUP|SUSP|SUSPENSION|SOLUTION|ORAL SOL|LIQUID|ELIXIR|TONIC)\b"),
    ("DROPS", r"\b(DROP|DROPS|DRP|E/D|EYE|EAR|NASAL DROP)\b"),
    ("INJECTION", r"\b(INJ|INJECTION|VIAL|VAIL|AMP|AMPOULE|IV|INFUSION)\b"),
    ("LOTION", r"\b(LOTION|SHAMPOO|WASH|FACEWASH|MOUTHWASH|GARGLE|OIL)\b"),
    ("POWDER", r"\b(POWDER|PWD|DUSTING|GRANULES)\b"),
    ("SACHET", r"\b(SACHET|SACHETS|SAC)\b"),
    ("SPRAY", r"\b(SPRAY)\b"),
    ("INHALER", r"\b(INHALER|INH|RESPULES|RESPULE|ROTAHALER|MDI)\b"),
    ("SOAP", r"\b(SOAP|BAR)\b"),
    ("KIT", r"\bKIT\b"),
]
# sale unit for products that are sold whole
_WHOLE_UNIT = {"SYRUP": "BOTTLE", "SUSPENSION": "BOTTLE", "DROPS": "BOTTLE", "LOTION": "BOTTLE", "SPRAY": "BOTTLE",
               "CREAM": "TUBE", "OINTMENT": "TUBE", "GEL": "TUBE", "INJECTION": "VIAL", "POWDER": "PACK",
               "SACHET": "SACHET", "INHALER": "PIECE", "SOAP": "PIECE", "DEVICE": "PIECE", "KIT": "KIT"}
_SOLID_DOSE = {"TABLET": "TABLET", "CAPSULE": "CAPSULE"}


def detect_form(item: Item) -> str:
    """Dosage form from the master, else the generic name, else the product name."""
    if item.dosage_form:
        return item.dosage_form
    for text in (item.generic_name or "", item.name or ""):
        # Supplier exports commonly glue the dose form to a numerical strength.
        # Split only after a number/strength, never brand suffixes (e.g. Kneecap).
        text = re.sub(r"(\d(?:\.?\d+)?\s*(?:MG|MCG|GM|ML)?)(?=TABS?\b|TABLETS?\b|CAPS?\b|CAPSULES?\b)", r"\1 ", text, flags=re.I)
        padded = f" {text.upper()} "
        for form, pattern in _FORM_WORDS:
            if re.search(pattern, padded):
                return form
    return ""


def default_loose(item: Item) -> bool:
    """Use the sale unit, pack conversion and detected dosage form; never product names."""
    return (item.units_per_pack or 1) > 1 or item.base_unit in _SOLID_DOSE.values() or detect_form(item) in _SOLID_DOSE


@dataclass
class UOM:
    base_unit: str
    pack_unit: str
    units_per_pack: int
    content_qty: Decimal | None
    content_unit: str
    dosage_form: str
    reason: str

    @property
    def loose_sale(self) -> bool:
        return self.units_per_pack > 1 or self.base_unit in _SOLID_DOSE.values()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["content_qty"] = str(self.content_qty) if self.content_qty is not None else ""
        d["loose_sale"] = self.loose_sale
        return d


def resolve(item: Item) -> UOM | None:
    """The unit of measure the printed pack and dosage form describe, or None."""
    info = units.parse_pack(item.pack_size)
    form = detect_form(item)
    raw = info.raw
    n = info.units_per_pack or 0

    # strips / packs of countable doses: 10S, 15 S, 1X10, 10X10 → N per strip, sold loose
    if info.kind in ("COUNT", "NESTED") and n > 1:
        if form in _SOLID_DOSE:
            return UOM(_SOLID_DOSE[form], "STRIP", n, None, "", form, f"Pack “{raw}”: strip of {n} {form.lower()}s")
        if info.unit_hint in ("TABLET", "CAPSULE"):
            return UOM(info.unit_hint, "STRIP", n, None, "", info.unit_hint, f"Pack “{raw}”: strip of {n}")
        if form == "SACHET":
            return UOM("SACHET", "BOX", n, None, "", form, f"Pack “{raw}”: box of {n} sachets")
        if form == "INJECTION":
            return UOM("VIAL", "BOX", n, None, "", form, f"Pack “{raw}”: box of {n} vials")
        if form == "SUPPOSITORY":
            return UOM("PIECE", "PACK", n, None, "", form, f"Pack “{raw}”: {n} suppositories")
        if info.strip and form in ("", "TABLET", "CAPSULE"):
            # "15 S" with no other clue: a strip of 15 solid doses
            return UOM("TABLET", "STRIP", n, None, "", "TABLET", f"Pack “{raw}”: strip of {n}")
        # a count with no strip marker and no dose form (device "1X3", condoms): the pack is the retail unit
        return UOM("PACK", "PACK", 1, None, "", form, f"Pack “{raw}” is sold whole")
    # a single tablet/capsule pack (“1”, “1 S”, “5X1X1”): counted per tablet
    if form in _SOLID_DOSE and info.kind in ("SINGLE", "COUNT", "NESTED", "UNKNOWN") and not info.unit_hint:
        return UOM(_SOLID_DOSE[form], "STRIP", 1, None, "", form, f"{form.title()} sold per {form.lower()}")
    # content packs: 200ML, 30GM, 5X5ML (5 ampoules of 5 mL)
    if info.kind == "CONTENT":
        cunit = info.content_unit
        if info.outer_count and info.outer_count > 1:
            unit = "AMPOULE" if form == "INJECTION" else _WHOLE_UNIT.get(form) or ("BOTTLE" if cunit in ("ML", "L") else "PACK")
            return UOM(unit, "BOX", info.outer_count, info.content_qty, cunit, form,
                       f"Pack “{raw}”: box of {info.outer_count} × {info.content_qty.normalize()} {cunit.lower()}")
        unit = info.unit_hint or _WHOLE_UNIT.get(form) or ("BOTTLE" if cunit in ("ML", "L") else "TUBE" if cunit == "G" and form in ("", "CREAM", "GEL", "OINTMENT") else "PACK")
        if unit in ("TABLET", "CAPSULE"):
            unit = "PACK"
        return UOM(unit, unit, 1, info.content_qty, cunit, form,
                   f"{info.content_qty.normalize()} {cunit.lower()} per {unit.lower()} (content, not stock)")
    if info.kind in ("KIT", "SINGLE") and info.unit_hint:
        unit = info.unit_hint
        if unit in ("TABLET", "CAPSULE"):
            return UOM(unit, "STRIP", 1, None, "", unit, f"Pack “{raw}”: one {unit.lower()}")
        return UOM(unit, unit, 1, None, "", form or ("KIT" if unit == "KIT" else ""), f"Pack “{raw}”: sold per {unit.lower()}")
    # no usable pack text: fall back to the form's natural sale unit when sold whole
    if form in _WHOLE_UNIT:
        unit = _WHOLE_UNIT[form]
        return UOM(unit, unit, 1, None, "", form, f"{form.title()} sold per {unit.lower()}")
    return None


def _current(item: Item) -> tuple:
    return (item.base_unit, item.pack_unit, item.units_per_pack or 1)


def convert(
    db: Session,
    item: Item,
    *,
    units_per_pack: int,
    base_unit: str,
    pack_unit: str,
    content_qty: Decimal | None = None,
    content_unit: str | None = None,
    dosage_form: str | None = None,
    source: str = "MANUAL",
    reason: str = "",
    user: User | None = None,
    ip_address: str = "",
) -> dict:
    """Apply a UOM; repack existing stock when it is still counted per pack."""
    upp = int(units_per_pack or 1)
    if upp < 1 or upp > 10000:
        raise PackagingError("Units per pack must be between 1 and 10000")
    base_unit = units.normalize_unit(base_unit, units.BASE_UNITS, "UNIT")
    pack_unit = units.normalize_unit(pack_unit, units.PACK_UNITS, "PACK")
    old_upp = item.units_per_pack or 1
    batches = list(db.scalars(select(Batch).where(Batch.item_id == item.id).order_by(Batch.id)))
    stock = sum(b.quantity for b in batches)
    before = {"base_unit": item.base_unit, "pack_unit": item.pack_unit, "units_per_pack": old_upp,
              "loose_sale": bool(item.loose_sale), "stock": stock}
    reason = reason.strip() or f"UOM set to {upp} {units.unit_label(base_unit, upp)} per {units.unit_label(pack_unit, 1)}"
    repacked = 0
    if upp != old_upp and stock:
        if old_upp != 1:
            raise PackagingError(
                f"{item.name} is already counted in {units.unit_label(item.base_unit)}s ({old_upp} per pack); "
                f"a different pack size must be received as a new product or after stock reaches zero")
        ref = f"REPACK {item.article_id}"[:60]
        for b in batches:
            if b.quantity < 0:
                raise PackagingError(f"Batch {b.batch_no} has negative stock; correct it first")
            q = b.quantity
            if q:
                stock_ledger.post(db, b, "REPACK_OUT", q, txn_quantity=q, txn_unit="PACK", reference_type="REPACK",
                                  reference_no=ref, reason=f"{reason} (was {q} {units.unit_label(item.pack_unit, q)})", user=user)
            b.units_per_pack = upp
            stock_ledger.sync_unit_prices(b)
            db.flush()
            if q:
                stock_ledger.post(db, b, "REPACK_IN", q * upp, txn_quantity=q, txn_unit="PACK", reference_type="REPACK",
                                  reference_no=ref, reason=f"{reason} ({q} × {upp})", user=user)
                repacked += 1
    elif upp != old_upp:
        for b in batches:  # no stock: future receipts use the new conversion
            b.units_per_pack = upp
            stock_ledger.sync_unit_prices(b)
    item.units_per_pack, item.base_unit, item.pack_unit = upp, base_unit, pack_unit
    item.loose_sale = default_loose(item)
    if content_qty is not None:
        item.content_qty = content_qty
    if content_unit is not None:
        item.content_unit = content_unit
    if dosage_form:
        item.dosage_form = units.normalize_unit(dosage_form, units.DOSAGE_FORMS, item.dosage_form or "")
    elif not item.dosage_form:
        item.dosage_form = detect_form(item) if detect_form(item) in units.DOSAGE_FORMS else ""
    item.packaging_source = source
    db.flush()
    after = {"base_unit": base_unit, "pack_unit": pack_unit, "units_per_pack": upp, "loose_sale": bool(item.loose_sale),
             "stock": sum(b.quantity for b in batches)}
    if before != after:
        audit.record(db, action=audit.A_UPDATE, entity_type="item_packaging", entity_id=item.article_id, user=user,
                     before=before, after=after, details=f"UOM for {item.name}: {reason}", ip_address=ip_address)
    return {"id": item.id, "name": item.name, "before": before, "after": after, "batches_repacked": repacked}


def auto_configure(db: Session, item: Item, *, user: User | None = None) -> dict | None:
    """Configure a product's UOM from its pack + form unless a user set it (MANUAL)."""
    if item.packaging_source == "MANUAL":
        return None
    uom = resolve(item)
    if uom is None:
        item.packaging_source = "AUTO"
        return None
    same = _current(item) == (uom.base_unit, uom.pack_unit, uom.units_per_pack) and bool(item.loose_sale) == uom.loose_sale
    if same:
        item.packaging_source = "AUTO"
        if uom.content_qty is not None and item.content_qty is None:
            item.content_qty, item.content_unit = uom.content_qty, uom.content_unit
        if uom.dosage_form and not item.dosage_form:
            item.dosage_form = uom.dosage_form
        return None
    try:
        return convert(db, item, units_per_pack=uom.units_per_pack, base_unit=uom.base_unit, pack_unit=uom.pack_unit,
                       content_qty=uom.content_qty, content_unit=uom.content_unit or None,
                       dosage_form=uom.dosage_form or None, source="AUTO",
                       reason=f"Automatic UOM — {uom.reason}", user=user)
    except PackagingError as exc:
        log.warning("UOM for %s not changed: %s", item.name, exc)
        item.packaging_source = "AUTO"
        return None


def auto_configure_pending(db: Session) -> int:
    """Configure every product that has never been configured. Returns products changed."""
    changed = 0
    for item in list(db.scalars(select(Item).where(Item.packaging_source == "DEFAULT", Item.deleted_at.is_(None)))):
        with db.begin_nested():
            if auto_configure(db, item):
                changed += 1
    db.flush()
    return changed


def defaults_for_new(name: str, pack: str, fields: dict) -> dict:
    """UOM for a product being created. Explicit unit fields always win (MANUAL)."""
    if any(fields.get(k) not in (None, "") for k in ("units_per_pack", "base_unit", "pack_unit")):
        return {"packaging_source": "MANUAL"}
    probe = Item(name=name or "", pack_size=pack or "", dosage_form=fields.get("dosage_form") or "",
                 generic_name=fields.get("generic_name") or "", base_unit="UNIT", pack_unit="PACK",
                 units_per_pack=1, loose_sale=False)
    uom = resolve(probe)
    if uom is None:
        return {"packaging_source": "AUTO"}
    out = {"base_unit": uom.base_unit, "pack_unit": uom.pack_unit, "units_per_pack": uom.units_per_pack,
           "packaging_source": "AUTO"}
    if uom.content_qty is not None:
        out["content_qty"], out["content_unit"] = uom.content_qty, uom.content_unit
    if uom.dosage_form and not fields.get("dosage_form") and uom.dosage_form in units.DOSAGE_FORMS:
        out["dosage_form"] = uom.dosage_form
    return out
