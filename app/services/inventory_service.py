"""Inventory business logic: item master, packaging, batches, search, export.

Quantities are always base units (see :mod:`app.services.units`). Every stock
change is a ledger movement posted through :mod:`app.services.stock_ledger`;
the helpers here only choose the movement type and reference.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import Batch, InventoryMovement, Item, User
from app.sequences import next_article_id_for
from app.services import stock_ledger, units
from app.services.stock_ledger import StockError as InventoryError  # noqa: F401  (one error type)
from app.utils import money, to_decimal, utcnow

# the live category list is data (category_service / Masters → Categories)
from app.services.category_service import DEFAULTS as DEFAULT_CATEGORIES  # noqa: E402
CATEGORIES = DEFAULT_CATEGORIES


def categories(db: Session) -> list[str]:
    """Active category codes in display order, from the category master."""
    from app.services import category_service

    return category_service.active_codes(db)


def ensure_category(db: Session, code: str | None) -> str:
    """Normalise a category and make sure it exists in the master."""
    from app.services import category_service

    return category_service.ensure(db, code)

# unit-of-measure fields; ``loose_sale`` is never set directly — it follows units_per_pack
PACKAGING_FIELDS = ("base_unit", "pack_unit", "units_per_pack", "loose_sale")
UOM_TRIGGERS = ("name", "pack_size", "generic_name", "dosage_form")  # re-detect when these change


def clean_packaging(fields: dict[str, Any], current: Item | None = None) -> dict[str, Any]:
    """Validate/normalise unit-of-measure fields present in ``fields``.

    Loose sale is derived, not chosen: a product sells loose exactly when one
    purchase unit holds more than one sale unit (strip of 10 → loose tablets).
    """
    out: dict[str, Any] = {}
    if "units_per_pack" in fields and fields["units_per_pack"] not in (None, ""):
        try:
            upp = int(str(fields["units_per_pack"]).strip())
        except ValueError:
            raise InventoryError("Units per pack must be a whole number")
        if upp < 1 or upp > 10000:
            raise InventoryError("Units per pack must be between 1 and 10000")
        out["units_per_pack"] = upp
        out["loose_sale"] = upp > 1
    if fields.get("base_unit"):
        out["base_unit"] = units.normalize_unit(fields["base_unit"], units.BASE_UNITS, "UNIT")
    if fields.get("pack_unit"):
        out["pack_unit"] = units.normalize_unit(fields["pack_unit"], units.PACK_UNITS, "PACK")
    if "dosage_form" in fields and fields["dosage_form"] is not None:
        form = units.normalize_unit(fields["dosage_form"], units.DOSAGE_FORMS, "")
        out["dosage_form"] = form
    return out


# --------------------------------------------------------------------------- #
# Item master
# --------------------------------------------------------------------------- #
def create_item(
    db: Session,
    *,
    name: str,
    user: User | None = None,
    article_id: str | None = None,
    ip_address: str = "",
    **fields: Any,
) -> Item:
    name = (name or "").strip()
    if not name:
        raise InventoryError("Item name is required")
    if article_id is None:
        article_id = next_article_id_for(db, name)
    if db.scalar(select(Item).where(Item.article_id == article_id)):
        raise InventoryError(f"Article ID already exists: {article_id}")

    allowed = {
        "generic_name", "manufacturer", "category", "pack_size", "strength", "unit",
        "hsn_code", "gst_rate", "mrp", "barcode", "is_active", "content_qty", "content_unit",
        "reorder_level", "rack",
    }
    item = Item(article_id=article_id, name=name, base_unit="UNIT", pack_unit="PACK",
                units_per_pack=1, loose_sale=False, dosage_form="")
    for key, value in fields.items():
        if key in allowed and value is not None:
            if key in ("gst_rate", "mrp"):
                value = to_decimal(value)
            setattr(item, key, value)
    item.category = ensure_category(db, fields.get("category") or item.category)
    from app.services import packaging_service

    # unit of measure detected from the printed pack + dosage form (``10S`` + TAB →
    # tablets, 10 per strip, sold loose; ``200ML`` syrup → bottle with 200 mL content);
    # explicit unit fields always win and are recorded as MANUAL
    inferred = packaging_service.defaults_for_new(name, fields.get("pack_size") or "", fields)
    for key, value in clean_packaging({**inferred, **fields}).items():
        setattr(item, key, value)
    item.packaging_source = inferred.get("packaging_source", "AUTO")
    for key in ("content_qty", "content_unit"):
        if key in inferred and fields.get(key) in (None, ""):
            setattr(item, key, inferred[key])
    db.add(item)
    db.flush()
    audit.record(
        db,
        action=audit.A_CREATE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        after=item,
        details=f"Created item {item.name}",
        ip_address=ip_address,
    )
    return item


def update_item(
    db: Session,
    item: Item,
    *,
    user: User | None = None,
    ip_address: str = "",
    **fields: Any,
) -> Item:
    before = audit.snapshot(item)
    packaging = clean_packaging(fields, item)
    triggers = {k: getattr(item, k) for k in UOM_TRIGGERS}
    fields = {k: v for k, v in fields.items() if k not in PACKAGING_FIELDS and k != "dosage_form"}
    for key, value in fields.items():
        if value is None or not hasattr(item, key) or key in ("id", "article_id", "created_at", "packaging_source"):
            continue
        if key in ("gst_rate", "mrp"):
            value = to_decimal(value)
        if key == "category":
            value = ensure_category(db, value)
        setattr(item, key, value)
    if "dosage_form" in packaging:
        item.dosage_form = packaging.pop("dosage_form")
    packaging.pop("loose_sale", None)
    if packaging and any(getattr(item, k) != v for k, v in packaging.items()):
        _apply_packaging(db, item, packaging)   # a person corrected the unit of measure
    elif any(getattr(item, k) != v for k, v in triggers.items()) and item.packaging_source != "MANUAL":
        from app.services import packaging_service

        packaging_service.auto_configure(db, item, user=user)  # pack text / form changed: re-detect
    item.updated_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        before=before,
        after=item,
        ip_address=ip_address,
    )
    return item


def stock_on_hand(db: Session, item_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(Batch.quantity), 0)).where(Batch.item_id == item_id)) or 0)


def _apply_packaging(db: Session, item: Item, packaging: dict[str, Any]) -> None:
    """Change how stock is counted — only while the product holds no stock.

    Existing stock was counted under the old definition; reinterpreting it
    would fabricate or destroy stock. Batches keep their own pack snapshot,
    so history is never re-read with the new definition.
    """
    from app.services import packaging_service

    changes = {k: v for k, v in packaging.items() if k != "loose_sale" and getattr(item, k) != v}
    if not changes:
        return
    upp_old = item.units_per_pack or 1
    upp_new = changes.get("units_per_pack", upp_old)
    if upp_new != upp_old and stock_on_hand(db, item.id) != 0 and upp_old != 1:
        raise InventoryError(
            f"{item.name} has stock counted as {upp_old} {units.unit_label(item.base_unit)} per "
            f"{units.unit_label(item.pack_unit, 1)}. Bring its stock to zero (or receive under a new "
            f"product) before changing the pack size."
        )
    # stock still counted per pack is repacked through the ledger (1 strip → N tablets)
    try:
        packaging_service.convert(
            db, item, units_per_pack=upp_new, base_unit=changes.get("base_unit", item.base_unit),
            pack_unit=changes.get("pack_unit", item.pack_unit), source="MANUAL",
            reason="Unit of measure corrected in product editor",
        )
    except packaging_service.PackagingError as exc:
        raise InventoryError(str(exc))


def packaging_view(item: Item) -> dict:
    """Packaging facts for screens and APIs (no cost data)."""
    upp = max(int(item.units_per_pack or 1), 1)
    return {
        "base_unit": item.base_unit or "UNIT",
        "pack_unit": item.pack_unit or "PACK",
        "units_per_pack": upp,
        "loose_sale": bool(item.loose_sale),
        "sale_unit": item.base_unit or "UNIT",
        "dosage_form": item.dosage_form or "",
        "pack_raw": item.pack_size or "",
        "content": (f"{item.content_qty.normalize():f} {item.content_unit.lower()}"
                    if item.content_qty is not None and item.content_unit else ""),
        "source": item.packaging_source or "DEFAULT",
        "sell_unit_label": units.unit_label(item.base_unit, 1),
        "pack_label": (f"{upp} {units.unit_label(item.base_unit, upp)} / {units.unit_label(item.pack_unit, 1)}"
                       if upp > 1 else units.unit_label(item.base_unit, 1)),
    }


def describe_stock(item: Item, qty: int) -> str:
    return units.describe_stock(qty, item.units_per_pack or 1, item.base_unit or "UNIT", item.pack_unit or "PACK")


def deactivate_item(db: Session, item: Item, *, user: User | None = None, ip_address: str = "") -> None:
    before = audit.snapshot(item)
    item.is_active = False
    db.flush()
    audit.record(
        db,
        action=audit.A_DELETE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        before=before,
        after={"is_active": False},
        details="Item deactivated (soft delete)",
        ip_address=ip_address,
    )


def reactivate_item(db: Session, item: Item, *, user: User | None = None, ip_address: str = "") -> Item:
    """Undo a soft delete so the item is visible/sellable again."""
    if item.is_active:
        return item
    item.is_active = True
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        after={"is_active": True},
        details="Item enabled",
        ip_address=ip_address,
    )
    return item


def delete_item(db: Session, item: Item, *, user: User | None = None, ip_address: str = "") -> Item:
    """Move an item to the recycle bin (recoverable soft delete)."""
    if item.deleted_at is not None:
        return item
    before = audit.snapshot(item)
    item.deleted_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=audit.A_DELETE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        before=before,
        after={"deleted_at": item.deleted_at.isoformat()},
        details="Item moved to recycle bin",
        ip_address=ip_address,
    )
    return item


def restore_item(db: Session, item: Item, *, user: User | None = None, ip_address: str = "") -> Item:
    """Restore an item from the recycle bin."""
    if item.deleted_at is None:
        return item
    item.deleted_at = None
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="item",
        entity_id=item.article_id,
        user=user,
        after={"deleted_at": None},
        details="Item restored from recycle bin",
        ip_address=ip_address,
    )
    return item


# --------------------------------------------------------------------------- #
# Batches / stock
# --------------------------------------------------------------------------- #
def add_or_update_batch(
    db: Session,
    item: Item,
    *,
    batch_no: str = "",
    expiry_date: date | None = None,
    quantity: int = 0,
    free: int = 0,
    unit: str = "BASE",
    movement_type: str = "ADJUSTMENT_IN",
    purchase_rate: Any = 0,
    selling_rate: Any = 0,
    mrp: Any = 0,
    supplier_id: int | None = None,
    purchase_id: int | None = None,
    reference_type: str = "",
    reference_id: int | None = None,
    reference_no: str = "",
    reason: str = "",
    user: User | None = None,
    ip_address: str = "",
) -> Batch:
    """Receive stock into the matching batch (created on first receipt).

    ``quantity``/``free`` are in ``unit`` (``PACK`` for supplier documents,
    ``BASE`` for counts in tablets/bottles); the ledger stores base units.
    """
    if quantity < 0 or free < 0:
        raise InventoryError("Quantity cannot be negative")
    if item.packaging_source == "DEFAULT":
        # never configured: settle its unit of measure before any stock is counted
        from app.services import packaging_service

        packaging_service.auto_configure(db, item, user=user)
    batch = stock_ledger.receive(
        db, item, quantity=quantity, unit=unit, free=free, movement_type=movement_type,
        batch_no=batch_no, expiry_date=expiry_date, mrp=mrp, purchase_rate=purchase_rate,
        selling_rate=selling_rate, supplier_id=supplier_id, purchase_id=purchase_id,
        reference_type=reference_type, reference_id=reference_id, reference_no=reference_no,
        reason=reason, user=user, ip_address=ip_address,
    )
    # Receiving stock re-enables a *disabled* item, but it must never resurrect
    # an item the user deleted to the recycle bin. Recycle-bin items are
    # excluded from matching (see app.services.matching), so incoming stock for
    # such a product creates a fresh active item and the bin stays untouched.
    if (quantity or free) and not item.is_active and item.deleted_at is None:
        item.is_active = True
        db.flush()
        audit.record(
            db,
            action=audit.A_UPDATE,
            entity_type="item",
            entity_id=item.article_id,
            user=user,
            after={"is_active": True},
            details="Item enabled by incoming stock",
            ip_address=ip_address,
        )
    return batch


def decrement_stock(
    db: Session,
    batch: Batch,
    quantity: int,
    *,
    movement_type: str = "ADJUSTMENT_OUT",
    reference_type: str = "",
    reference_id: int | None = None,
    reference_no: str = "",
    user: User | None = None,
    reason: str = "",
    ip_address: str = "",
) -> InventoryMovement:
    """Take ``quantity`` base units out of one batch (never below zero)."""
    if quantity <= 0:
        raise InventoryError("Quantity must be positive")
    return stock_ledger.post(
        db, batch, movement_type, quantity, reference_type=reference_type, reference_id=reference_id,
        reference_no=reference_no, reason=reason, user=user,
    )


def update_batch(
    db: Session,
    batch: Batch,
    *,
    batch_no: str | None = None,
    expiry_date: date | None | str = "",
    mrp: Any = None,
    purchase_rate: Any = None,
    selling_rate: Any = None,
    supplier_id: int | None | str = "",
    user: User | None = None,
    ip_address: str = "",
) -> Batch:
    """Correct a batch's printed details (number, expiry, prices, supplier). Stock is unchanged;
    quantity changes go through :func:`set_batch_quantity` so losses are booked."""
    before = audit.snapshot(batch)
    new_no = batch.batch_no if batch_no is None else " ".join(str(batch_no).split())[:60]
    new_exp = batch.expiry_date if expiry_date == "" else expiry_date
    new_norm = stock_ledger.normalize_batch_no(new_no)
    if (new_no, new_exp) != (batch.batch_no, batch.expiry_date):
        same = (Batch.batch_no_normalized == new_norm) if new_norm else (
            (Batch.batch_no_normalized == "") & (Batch.expiry_date == new_exp))
        clash = db.scalar(select(Batch.id).where(Batch.item_id == batch.item_id, same, Batch.id != batch.id))
        if clash:
            raise InventoryError("Another batch of this product already has that batch number")
    batch.batch_no, batch.batch_no_normalized, batch.expiry_date = new_no, new_norm, new_exp
    for field, value in (("mrp", mrp), ("purchase_rate", purchase_rate), ("selling_rate", selling_rate)):
        if value is None or str(value).strip() == "":
            continue
        amount = to_decimal(value)
        if amount < 0:
            raise InventoryError("Prices cannot be negative")
        setattr(batch, field, amount)
    if batch.selling_rate and batch.mrp and batch.selling_rate > batch.mrp:
        raise InventoryError("Selling rate cannot be above MRP")
    if supplier_id != "":
        batch.supplier_id = int(supplier_id) if supplier_id else None
    stock_ledger.sync_unit_prices(batch)
    batch.updated_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="batch",
        entity_id=batch.id,
        user=user,
        before=before,
        after=audit.snapshot(batch),
        details=f"Batch details corrected for {batch.item.name} (batch {batch.batch_no or '—'})",
        ip_address=ip_address,
    )
    return batch


def list_batches_for_item(db: Session, item_id: int) -> list[Batch]:
    return list(
        db.scalars(
            select(Batch).where(Batch.item_id == item_id).order_by(Batch.expiry_date.asc().nulls_last())
        )
    )


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _fts_query(q: str) -> str:
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    return " ".join(f"{t}*" for t in tokens)


def search_items(
    db: Session,
    *,
    q: str = "",
    category: str = "",
    manufacturer: str = "",
    supplier_id: int | None = None,
    expiry_filter: str = "",
    stock_filter: str = "",
    cost_min: float | None = None,
    cost_max: float | None = None,
    mrp_min: float | None = None,
    mrp_max: float | None = None,
    sort: str = "name_asc",
    low_threshold: int = 5,
    threshold_days: int = 90,
    limit: int = 50,
    offset: int = 0,
    active_only: bool = True,
    deleted_only: bool = False,
    include_deleted: bool = False,
) -> tuple[list[Item], int]:
    """Return (items, total). Uses FTS5 for name search at 300k+ rows."""
    stmt = select(Item)
    count_stmt = select(func.count(Item.id))

    conditions = []
    if deleted_only:
        conditions.append(Item.deleted_at.is_not(None))
    elif not include_deleted:
        conditions.append(Item.deleted_at.is_(None))
    if active_only:
        conditions.append(Item.is_active.is_(True))
    if category:
        conditions.append(Item.category == category)
    if manufacturer:
        conditions.append(Item.manufacturer == manufacturer)
    if supplier_id:
        conditions.append(Item.id.in_(select(Batch.item_id).where(Batch.supplier_id == supplier_id)))
    if cost_min is not None:
        conditions.append(Item.id.in_(select(Batch.item_id).where(Batch.purchase_rate >= cost_min)))
    if cost_max is not None:
        conditions.append(Item.id.in_(select(Batch.item_id).where(Batch.purchase_rate <= cost_max)))
    if mrp_min is not None:
        conditions.append(Item.mrp >= mrp_min)
    if mrp_max is not None:
        conditions.append(Item.mrp <= mrp_max)

    ids: list[int] | None = None
    if q.strip():
        from app.services import search

        ids = search.product_ids(db, q, 2000) or None
        if ids:
            conditions.append(Item.id.in_(ids))
        else:
            # No FTS hits (or FTS unavailable): fall back to a LIKE scan of the
            # literal query so punctuation/spacing differences still match.
            like = f"%{q.strip()}%"
            conditions.append(
                or_(Item.name.ilike(like), Item.generic_name.ilike(like), Item.barcode == q.strip())
            )

    if stock_filter:
        stock_sub = (
            select(Batch.item_id, func.coalesce(func.sum(Batch.quantity), 0).label("qty"))
            .group_by(Batch.item_id)
            .subquery()
        )
        if stock_filter == "out":
            conditions.append(~Item.id.in_(select(stock_sub.c.item_id).where(stock_sub.c.qty > 0)))
        elif stock_filter == "low":
            conditions.append(
                Item.id.in_(
                    select(stock_sub.c.item_id).where(
                        stock_sub.c.qty > 0, stock_sub.c.qty <= low_threshold
                    )
                )
            )
        elif stock_filter == "in":
            conditions.append(Item.id.in_(select(stock_sub.c.item_id).where(stock_sub.c.qty > 0)))

    if expiry_filter:
        today = date.today()
        threshold = today + timedelta(days=threshold_days)
        sub = select(Batch.item_id)
        if expiry_filter == "expired":
            sub = sub.where(Batch.expiry_date.is_not(None), Batch.expiry_date < today, Batch.quantity > 0)
        elif expiry_filter == "expiring":
            sub = sub.where(
                Batch.expiry_date.is_not(None),
                Batch.expiry_date >= today,
                Batch.expiry_date <= threshold,
                Batch.quantity > 0,
            )
        elif expiry_filter == "ok":
            sub = sub.where(Batch.expiry_date.is_not(None), Batch.expiry_date > threshold)
        conditions.append(Item.id.in_(sub))

    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.scalar(count_stmt) or 0
    stmt = stmt.options(selectinload(Item.batches))
    if sort in ("stock_asc", "stock_desc"):
        stock_sub = (
            select(Batch.item_id, func.coalesce(func.sum(Batch.quantity), 0).label("qty"))
            .group_by(Batch.item_id)
            .subquery()
        )
        stmt = stmt.outerjoin(stock_sub, stock_sub.c.item_id == Item.id)
        stmt = stmt.order_by(stock_sub.c.qty.asc() if sort == "stock_asc" else stock_sub.c.qty.desc())
    elif sort == "mrp_desc":
        stmt = stmt.order_by(Item.mrp.desc())
    elif sort == "mrp_asc":
        stmt = stmt.order_by(Item.mrp.asc())
    elif sort == "name_desc":
        stmt = stmt.order_by(Item.name.desc())
    else:
        stmt = stmt.order_by(Item.name.asc())
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt)), total


def inventory_valuation(db: Session, items: list[Item]) -> Decimal:
    total = Decimal("0")
    for item in items:
        for batch in item.batches:
            total += to_decimal(batch.unit_cost) * batch.quantity
    return money(total)


def retail_valuation(db: Session, items: list[Item]) -> Decimal:
    """On-hand value at MRP (potential revenue)."""
    total = Decimal("0")
    for item in items:
        for batch in item.batches:
            total += to_decimal(batch.unit_mrp) * batch.quantity
    return money(total)


def inventory_valuation_total(db: Session) -> Decimal:
    """Whole-shelf value at purchase cost, across every active item.

    Unlike :func:`inventory_valuation` (which values a supplied list, e.g. one
    page), this aggregates in the database so the Inventory value metric always
    reflects the entire shelf.
    """
    total = db.scalar(
        select(func.coalesce(func.sum(Batch.unit_cost * Batch.quantity), 0))
        .select_from(Batch)
        .join(Item, Item.id == Batch.item_id)
        .where(Item.is_active.is_(True), Item.deleted_at.is_(None), Batch.quantity > 0)
    )
    return money(to_decimal(total or 0))


def retail_valuation_total(db: Session) -> Decimal:
    """Whole-shelf on-hand value at MRP (per base unit × base units)."""
    total = db.scalar(
        select(
            func.coalesce(
                func.sum(Batch.unit_mrp * Batch.quantity), 0
            )
        )
        .select_from(Batch)
        .join(Item, Item.id == Batch.item_id)
        .where(Item.is_active.is_(True), Item.deleted_at.is_(None), Batch.quantity > 0)
    )
    return money(to_decimal(total or 0))


def set_batch_quantity(
    db: Session, batch: Batch, new_quantity: int, *, reason: str = "",
    category: str = "", user: User | None = None, ip_address: str = "",
) -> Batch:
    """Physical count: post the difference as an adjustment (never an overwrite).

    A shortfall is booked as a stock loss (``category``, default COUNT); a
    surplus as ``ADJUSTMENT_IN``. Either way the ledger keeps the reason.
    """
    new_quantity = int(new_quantity)
    if new_quantity < 0:
        raise InventoryError("Quantity cannot be negative")
    delta = new_quantity - batch.quantity
    if delta == 0:
        return batch
    reason = (reason or "").strip() or "Physical count correction"
    if delta > 0:
        from app.services import adjustment_service

        adjustment_service.create(db, item=batch.item, direction="IN", category="SURPLUS", quantity=delta,
                                  reason=reason, batch=batch, user=user, ip_address=ip_address)
        return batch
    record_adjustment(
        db, batch=batch, quantity=-delta, category=(category or "COUNT"),
        reason=reason, user=user, ip_address=ip_address,
    )
    return batch


def stock_view_counts(db: Session, *, low_threshold: int = 5, threshold_days: int = 90) -> dict:
    """Counts for the quick stock views (all / in / low / out / expiring / expired)."""
    today = date.today()
    horizon = today + timedelta(days=threshold_days)
    rows = db.execute(
        select(Item.id, func.coalesce(func.sum(Batch.quantity), 0))
        .select_from(Item)
        .outerjoin(Batch, Batch.item_id == Item.id)
        .where(Item.is_active.is_(True), Item.deleted_at.is_(None))
        .group_by(Item.id)
    ).all()
    counts = {"all": len(rows), "in": 0, "low": 0, "out": 0}
    for _id, qty in rows:
        q = qty or 0
        if q <= 0:
            counts["out"] += 1
        elif q <= low_threshold:
            counts["low"] += 1
        else:
            counts["in"] += 1
    counts["expiring"] = db.scalar(
        select(func.count(func.distinct(Batch.item_id))).where(
            Batch.expiry_date.is_not(None), Batch.expiry_date >= today,
            Batch.expiry_date <= horizon, Batch.quantity > 0,
        )
    ) or 0
    counts["expired"] = db.scalar(
        select(func.count(func.distinct(Batch.item_id))).where(
            Batch.expiry_date.is_not(None), Batch.expiry_date < today, Batch.quantity > 0,
        )
    ) or 0
    return counts


def stock_counts(db: Session, *, low_threshold: int = 5) -> dict:
    """Active items that are out of stock or at/below the reorder level."""
    rows = db.execute(
        select(
            Item.id,
            func.coalesce(func.sum(Batch.quantity), 0),
        )
        .select_from(Item)
        .outerjoin(Batch, Batch.item_id == Item.id)
        .where(Item.is_active.is_(True), Item.deleted_at.is_(None))
        .group_by(Item.id)
    ).all()
    out = sum(1 for r in rows if (r[1] or 0) <= 0)
    low = sum(1 for r in rows if 0 < (r[1] or 0) <= low_threshold)
    return {"out_of_stock": out, "low_stock": low, "tracked": len(rows)}


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
# GST is not tracked per item any more; exports carry batch-level stock instead.
INVENTORY_COLUMNS = [
    "article_id", "name", "generic_name", "manufacturer", "category", "pack_size",
    "strength", "unit", "base_unit", "pack_unit", "units_per_pack", "loose_sale",
    "hsn_code", "barcode", "mrp", "batch_no", "expiry_date",
    "quantity", "purchase_rate", "selling_rate", "supplier", "stock_value",
]


def _inventory_rows(db: Session, items: list[Item]) -> list[dict]:
    rows = []
    for item in items:
        if not item.batches:
            rows.append(_item_row(item, None))
        for batch in item.batches:
            rows.append(_item_row(item, batch))
    return rows


def _item_row(item: Item, batch: Batch | None) -> dict:
    qty = batch.quantity if batch else 0
    rate = to_decimal(batch.purchase_rate) if batch else Decimal("0")
    unit_cost = to_decimal(batch.unit_cost) if batch else Decimal("0")
    return {
        "article_id": item.article_id,
        "name": item.name,
        "generic_name": item.generic_name,
        "manufacturer": item.manufacturer,
        "category": item.category,
        "pack_size": item.pack_size,
        "strength": item.strength,
        "unit": item.unit,
        "base_unit": item.base_unit,
        "pack_unit": item.pack_unit,
        "units_per_pack": item.units_per_pack,
        "loose_sale": "Y" if item.loose_sale else "N",
        "hsn_code": item.hsn_code,
        "barcode": item.barcode,
        "mrp": str(item.mrp),
        "batch_no": batch.batch_no if batch else "",
        "expiry_date": batch.expiry_date.isoformat() if batch and batch.expiry_date else "",
        "quantity": qty,
        "purchase_rate": str(rate),
        "selling_rate": str(batch.selling_rate) if batch else "",
        "supplier": (batch.supplier.name if batch and batch.supplier else ""),
        "stock_value": str(money(unit_cost * qty)),
    }


def export_csv(db: Session, items: list[Item]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=INVENTORY_COLUMNS)
    writer.writeheader()
    for row in _inventory_rows(db, items):
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def export_xlsx(db: Session, items: list[Item]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append(INVENTORY_COLUMNS)
    for row in _inventory_rows(db, items):
        ws.append([row[c] for c in INVENTORY_COLUMNS])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def all_items_for_export(db: Session, **filters) -> list[Item]:
    items, _ = search_items(db, limit=1_000_000, offset=0, **filters)
    return items


# LOOSE: loose strips/units lost · DAMAGE: broken/unsellable · EXPIRED: expiry
# write-off · COUNT: physical stock-count shortfall. Every one is a stock loss
# valued at purchase cost and flows into P&L, reports and the dashboard.
ADJUSTMENT_CATEGORIES = ("LOOSE", "DAMAGE", "EXPIRED", "COUNT")
ADJUSTMENT_LABELS = {"LOOSE": "Loose", "DAMAGE": "Damage", "EXPIRED": "Expired", "COUNT": "Count shortfall"}


def record_adjustment(
    db: Session,
    *,
    batch: Batch,
    quantity: int,
    category: str,
    reason: str = "",
    user: User | None = None,
    ip_address: str = "",
):
    """A stock write-off (decrease) — a numbered adjustment document (see adjustment_service)."""
    from app.services import adjustment_service

    category = (category or "LOOSE").upper()
    if category not in ADJUSTMENT_CATEGORIES:
        raise InventoryError("Category must be one of " + ", ".join(ADJUSTMENT_CATEGORIES))
    if not isinstance(quantity, int) or quantity <= 0:
        raise InventoryError("Write-off quantity must be a whole number above zero")
    try:
        return adjustment_service.create(db, item=batch.item, direction="OUT", category=category, quantity=quantity,
                                         reason=reason or ADJUSTMENT_LABELS[category], batch=batch, user=user,
                                         ip_address=ip_address)
    except adjustment_service.AdjustmentError as exc:
        raise InventoryError(str(exc))


def non_moving_stock(db: Session, *, as_of: date | None = None) -> list[dict]:
    """In-stock items with their last-sale age, for the non-moving widget.

    Inactivity is measured from the last sale; never-sold items use their first
    receipt. Stock value is the sum of batch purchase cost (matching the
    inventory valuation), and the effective rate is value / quantity.
    """
    from app.models import Sale, SaleItem

    as_of = as_of or date.today()
    sale_rows = db.execute(
        select(SaleItem.item_id, func.max(Sale.sale_date))
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(SaleItem.item_id.is_not(None), Sale.payment_status != "CANCELLED")
        .group_by(SaleItem.item_id)
    ).all()
    last_sale = {r[0]: r[1] for r in sale_rows if r[1] is not None}

    recv_rows = db.execute(
        select(Batch.item_id, func.min(Batch.created_at)).group_by(Batch.item_id)
    ).all()
    received = {r[0]: r[1] for r in recv_rows if r[1] is not None}

    stock_rows = db.execute(
        select(
            Batch.item_id,
            func.coalesce(func.sum(Batch.quantity), 0),
            func.coalesce(func.sum(Batch.unit_cost * Batch.quantity), 0),
        ).group_by(Batch.item_id)
    ).all()
    stock = {r[0]: (int(r[1] or 0), to_decimal(r[2])) for r in stock_rows}

    # plain columns, not ORM objects: this runs on every dashboard load over the whole catalogue
    items = db.execute(
        select(Item.id, Item.article_id, Item.name, Item.generic_name, Item.category, Item.unit, Item.pack_size)
        .where(Item.is_active.is_(True), Item.deleted_at.is_(None))
        .order_by(Item.name)
    ).all()

    out: list[dict] = []
    for item in items:
        qty, value = stock.get(item.id, (0, Decimal("0")))
        if qty <= 0:
            continue
        last = last_sale.get(item.id)
        recv = received.get(item.id)
        reference = last or recv
        idle = (as_of - reference.date()).days if reference else 0
        rate = money(value / qty) if qty else Decimal("0")
        out.append(
            {
                "sku": item.article_id,
                "name": item.name,
                "generic": item.generic_name or "",
                "category": item.category,
                "qty": qty,
                "unit": item.unit or "unit",
                "pack": item.pack_size or "",
                "rate": float(money(rate)),
                "value": float(money(value)),
                "last": last.date().isoformat() if last else "",
                "received": recv.date().isoformat() if recv else "",
                "days": idle,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Item import (CSV / Excel)
# --------------------------------------------------------------------------- #
IMPORT_TEMPLATE_COLUMNS = [
    "Product Name", "Generic Name", "Manufacturer", "Category", "Form", "Pack", "Strength",
    "Base Unit", "Pack Unit", "Units Per Pack",
    "HSN", "Barcode", "Batch", "Expiry", "Qty", "Loose Qty", "Purchase Rate", "MRP",
]


def _row_packaging(cells: dict[str, str], where: str) -> dict[str, Any]:
    """Unit-of-measure columns on an import row (optional).

    Leave them blank and the unit of measure is detected from Pack + Form/name
    (``15S`` tablets → 15 per strip, loose). Filled-in columns override detection."""
    out: dict[str, Any] = {}
    upp_text = cells.get("units_per_pack", "")
    if upp_text:
        out["units_per_pack"] = upp_text
    for key, cell in (("base_unit", "base_unit"), ("pack_unit", "pack_unit"), ("dosage_form", "form")):
        if cells.get(cell):
            out[key] = cells[cell]
    return clean_packaging(out) if out else {}


def import_items(
    db: Session,
    filename: str,
    content: bytes,
    *,
    default_category: str = "PHARMA",
    user: User | None = None,
    ip_address: str = "",
) -> dict:
    """Create products and post opening stock from a CSV / Excel sheet.

    A row matches an existing item by barcode, then by exact name; otherwise a
    new item is created with the row's packaging. ``Qty`` counts purchase
    packs (strips, bottles) and ``Loose Qty`` extra base units, so
    ``Qty 44, Loose Qty 6`` of a 15-tablet strip is 666 tablets. Stock posts as
    ``OPENING_STOCK`` ledger movements. Blank item fields are completed but
    existing values are never overwritten. Each row is independent: a bad row
    is reported and the rest still import.
    """
    from app.services import matching, sheet_import

    table = sheet_import.read_table(filename, content)
    default = default_category.strip().upper()
    known = categories(db)
    if default not in known:
        default = known[0]
    result = {"rows": len(table.rows), "created": 0, "updated": 0, "batches": 0, "units": 0,
              "skipped": table.skipped, "errors": []}
    seen: dict[str, Item] = {}
    reference = f"IMPORT {filename}"[:60]
    for row in table.rows:
        cells = {k: sheet_import.clean_text(v) for k, v in row.items() if k != "_row"}
        name = cells.get("name", "")[:250]
        where = f"row {row['_row']} ({name})"
        try:
            for col in ("quantity", "free", "loose_qty"):
                if sheet_import.has_fraction(row.get(col)):
                    raise InventoryError(f"{col.replace('_', ' ')} {row.get(col)!r} is fractional — stock is counted "
                                         f"in whole units (tablets, bottles, tubes)")
            quantity = sheet_import.parse_quantity(row.get("quantity"))
            packs = quantity[0] + quantity[1] + sheet_import.parse_quantity(row.get("free"))[0]
            loose_qty = sheet_import.parse_quantity(row.get("loose_qty"))[0]
            rate = sheet_import.parse_number(row.get("rate")) or Decimal("0")
            mrp = sheet_import.parse_number(row.get("mrp")) or Decimal("0")
            selling = sheet_import.parse_number(row.get("selling_rate")) or mrp
            expiry = sheet_import.parse_expiry(row.get("expiry"))
            if cells.get("expiry") and expiry is None:
                raise InventoryError(f"expiry {cells['expiry']!r} is not a date (use MM/YYYY)")
            category = cells.get("category", "").upper()
            category = category if category in known else default
            packaging = _row_packaging(cells, where)

            key = matching.normalize_name(name)
            with db.begin_nested():
                item = seen.get(key) or matching.find_exact_match(db, name, cells.get("barcode", ""))
                fields = {
                    "generic_name": cells.get("generic_name", ""),
                    "manufacturer": cells.get("manufacturer", ""),
                    "pack_size": cells.get("pack", ""),
                    "strength": cells.get("strength", ""),
                    "unit": cells.get("unit", ""),
                    "hsn_code": cells.get("hsn", ""),
                    "barcode": cells.get("barcode", ""),
                }
                created = changed = False
                if item is None:
                    item = create_item(
                        db, name=name, category=category, mrp=mrp, user=user, ip_address=ip_address,
                        **{k: v for k, v in fields.items() if v}, **packaging,
                    )
                    created = True
                else:
                    for attr, value in fields.items():
                        if value and not getattr(item, attr):
                            setattr(item, attr, value)
                            changed = True
                    if mrp and not item.mrp:
                        item.mrp = mrp
                        changed = True
                    if "dosage_form" in packaging:
                        item.dosage_form = packaging.pop("dosage_form")
                    if any(getattr(item, k) != v for k, v in packaging.items()):
                        _apply_packaging(db, item, packaging)
                        changed = True
                if loose_qty and (item.units_per_pack or 1) > 1 and loose_qty >= item.units_per_pack:
                    raise InventoryError(f"loose qty {loose_qty} is a full pack or more; put whole packs in Qty")
                if packs > 0 or loose_qty > 0:
                    batch = add_or_update_batch(
                        db, item, batch_no=cells.get("batch", "")[:60], expiry_date=expiry, quantity=packs,
                        unit="PACK", movement_type="OPENING_STOCK", reference_type="IMPORT",
                        reference_no=reference, reason="Opening stock import",
                        purchase_rate=rate, selling_rate=selling, mrp=mrp, user=user, ip_address=ip_address,
                    )
                    if loose_qty > 0:
                        stock_ledger.post(db, batch, "OPENING_STOCK", loose_qty, reference_type="IMPORT",
                                          reference_no=reference, reason="Opening stock import (loose)", user=user)
            added = units.to_base(packs, item.units_per_pack or 1, "PACK") + loose_qty
            # counted only once the row's savepoint has committed
            if created:
                result["created"] += 1
            elif changed and key not in seen:
                result["updated"] += 1
            if added > 0:
                result["batches"] += 1
                result["units"] += added
            seen[key] = item
        except (InventoryError, ValueError) as exc:
            result["errors"].append(f"{where}: {exc}")
    db.flush()
    audit.record(
        db, action=audit.A_CREATE, entity_type="item_import", entity_id=filename[:60], user=user,
        details=(f"Item import {filename}: {result['created']} created, {result['updated']} updated, "
                 f"{result['batches']} batch(es), {len(result['errors'])} error(s)"),
        ip_address=ip_address,
    )
    return result
