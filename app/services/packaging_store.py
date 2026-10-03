"""ProductPackaging: the valid packs of each product (1X10, 10X10, 10X1X10 …), one preferred.

Rows come from three places, each recorded in ``source``:

* PRODUCT_MASTER — bootstrapped from existing products, only where the printed pack and the
  product's own definition agree (nothing is guessed; disagreements are listed in a report);
* USER_CORRECTION — a person confirmed a pack while reviewing a purchase;
* PURCHASE — a posted receipt whose conversion was proven by reviewed evidence.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, MappingHistory, ProductPackaging, User
from app.services import packaging_parser as pp
from app.services import packaging_service

_WHOLE_SALE = {"SYRUP", "SUSPENSION", "DROPS", "LOTION", "SPRAY", "CREAM", "GEL", "OINTMENT", "INJECTION", "IV_FLUID"}


def default_loose_sale(form: str, retail_to_base: int) -> bool:
    """Loose sale defaults from the form (sealed liquids, creams, injections and IV: off) and
    the pack (a strip of several doses: on). The product can always override it."""
    if form in _WHOLE_SALE:
        return False
    return retail_to_base > 1


def _history(db, item_id, action, after, user=None):
    db.add(MappingHistory(kind="PACKAGING", action=action, key=str(item_id), after=after, user_id=user.id if user else None))
    db.flush()


def upsert(db: Session, item: Item, parsed: pp.Packaging, *, source: str, verified: bool, purchase_to_retail: int,
           retail_to_base: int, retail_unit: str, base_unit: str, purchase_unit: str, preferred: bool = False,
           user: User | None = None) -> ProductPackaging:
    key = parsed.normalized or pp.normalize(parsed.raw)
    row = db.scalar(select(ProductPackaging).where(ProductPackaging.item_id == item.id,
                                                   ProductPackaging.normalized_packing == key,
                                                   ProductPackaging.purchase_to_retail == purchase_to_retail))
    content = parsed.content
    if row is None:
        row = ProductPackaging(item_id=item.id, normalized_packing=key, purchase_to_retail=purchase_to_retail)
        db.add(row)
        action = "CREATE"
    else:
        action = "CONFIRM"
    row.raw_supplier_packing = (parsed.raw or "")[:60]
    row.dosage_form = item.dosage_form or parsed.dosage_form or ""
    row.purchase_unit, row.retail_unit, row.base_unit = purchase_unit, retail_unit, base_unit
    row.retail_to_base = retail_to_base
    row.container_size = content[0] if content else (item.content_qty if item.content_qty else None)
    row.container_size_unit = (content[1] if content else (item.content_unit or "")).replace("GRAM", "G")
    row.allow_loose_sale = bool(item.loose_sale) if item.packaging_source == "MANUAL" else default_loose_sale(row.dosage_form, retail_to_base)
    row.source = source if action == "CREATE" or verified else (row.source or source)
    row.confidence = parsed.confidence
    row.verified_by_user = bool(row.verified_by_user or verified)
    if preferred or not db.scalar(select(ProductPackaging.id).where(ProductPackaging.item_id == item.id,
                                                                    ProductPackaging.is_preferred.is_(True),
                                                                    ProductPackaging.id != (row.id or 0))):
        for other in db.scalars(select(ProductPackaging).where(ProductPackaging.item_id == item.id, ProductPackaging.is_preferred.is_(True))):
            other.is_preferred = False
        row.is_preferred = True
    db.flush()
    _history(db, item.id, action, {"packing": key, "retail_to_base": retail_to_base, "purchase_to_retail": purchase_to_retail,
                                   "source": source, "verified": bool(verified)}, user)
    return row


def for_item(db: Session, item_id: int) -> list[ProductPackaging]:
    return list(db.scalars(select(ProductPackaging).where(ProductPackaging.item_id == item_id)
                           .order_by(ProductPackaging.is_preferred.desc(), ProductPackaging.id)))


def bootstrap(db: Session) -> dict:
    """Seed ProductPackaging from existing products where it needs no guess. Idempotent."""
    created, skipped = 0, []
    have = {(r.item_id, r.normalized_packing) for r in db.scalars(select(ProductPackaging))}
    for item in db.scalars(select(Item).where(Item.deleted_at.is_(None)).order_by(Item.id)):
        if not item.pack_size:
            skipped.append({"item_id": item.id, "name": item.name, "pack": "", "reason": "no printed pack on the product"})
            continue
        parsed = pp.parse(item.pack_size, master_form=item.dosage_form or packaging_service.detect_form(item), product_name=item.name)
        if (item.id, parsed.normalized) in have:
            continue
        upp = item.units_per_pack or 1
        reason = ""
        if parsed.confidence not in (pp.HIGH, pp.MEDIUM) or parsed.base is None:
            reason = "pack is ambiguous: " + ("; ".join(parsed.issues) or parsed.confidence)
        elif parsed.retail_to_base != upp and not (parsed.content and upp == 1):
            reason = f"printed pack gives {parsed.retail_to_base} per retail pack, product says {upp}"
        elif parsed.base.unit and item.base_unit not in (parsed.base.unit, "UNIT") and not (parsed.content and upp == 1):
            reason = f"printed pack counts {parsed.base.unit.lower()}s, product counts {item.base_unit.lower()}s"
        if reason:
            skipped.append({"item_id": item.id, "name": item.name, "pack": item.pack_size, "reason": reason})
            continue
        upsert(db, item, parsed, source="PRODUCT_MASTER", verified=item.packaging_source == "MANUAL",
               purchase_to_retail=parsed.retail.quantity if parsed.retail else 1, retail_to_base=upp,
               retail_unit=item.pack_unit, base_unit=item.base_unit, purchase_unit=(parsed.purchase.unit if parsed.purchase else item.pack_unit) or item.pack_unit)
        have.add((item.id, parsed.normalized))
        created += 1
    return {"created": created, "skipped": skipped}


def as_dict(row: ProductPackaging) -> dict:
    return {"id": row.id, "raw": row.raw_supplier_packing, "packing": row.normalized_packing, "dosage_form": row.dosage_form,
            "purchase_unit": row.purchase_unit, "retail_unit": row.retail_unit, "base_unit": row.base_unit,
            "purchase_to_retail": row.purchase_to_retail, "retail_to_base": row.retail_to_base,
            "container": f"{Decimal(row.container_size).normalize():f} {row.container_size_unit.lower()}" if row.container_size else "",
            "allow_loose_sale": bool(row.allow_loose_sale), "preferred": bool(row.is_preferred), "source": row.source,
            "confidence": row.confidence, "verified": bool(row.verified_by_user)}
