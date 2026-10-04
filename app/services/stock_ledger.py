"""The inventory engine: ledger postings, batch resolution and FEFO allocation.

This is the only code that changes ``batches.quantity``. Every stock change —
purchase, free goods, sale, return, cancellation, adjustment, opening stock —
is an :class:`InventoryMovement` posted here, and the batch projection is
moved in the same statement with a guarded ``UPDATE … WHERE quantity + d >= 0``
so two counters can never sell the same last tablet.

Invariant (checked by :func:`reconcile`)::

    batches.quantity == SUM(inventory_movements.quantity) for that batch
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app import audit
from app.models import Batch, InventoryMovement, Item, User
from app.services import units
from app.utils import money, to_decimal, utcnow

# movement type → direction
INBOUND = ("OPENING_STOCK", "PURCHASE_RECEIPT", "FREE_STOCK", "SALE_RETURN", "SALE_CANCEL", "ADJUSTMENT_IN",
           "REPACK_IN")
OUTBOUND = ("SALE", "PURCHASE_RETURN", "ADJUSTMENT_OUT", "RECEIPT_REVERSAL", "REPACK_OUT")
MOVEMENT_TYPES = INBOUND + OUTBOUND
MOVEMENT_LABELS = {
    "OPENING_STOCK": "Opening stock", "PURCHASE_RECEIPT": "Purchase", "FREE_STOCK": "Free goods",
    "SALE": "Sale", "SALE_RETURN": "Sale return", "SALE_CANCEL": "Sale cancelled",
    "PURCHASE_RETURN": "Purchase return", "ADJUSTMENT_IN": "Adjustment in",
    "ADJUSTMENT_OUT": "Adjustment out", "RECEIPT_REVERSAL": "Receipt reversed",
    "REPACK_OUT": "Repack out (old unit)", "REPACK_IN": "Repack in (new unit)",
}


class StockError(Exception):
    pass


class BatchConflict(StockError):
    """Incoming batch data contradicts an existing batch; a human must decide."""


def normalize_batch_no(value: Any) -> str:
    """Matching key only — the raw batch text is always stored untouched."""
    return re.sub(r"[\s\-_/.]", "", str(value or "")).upper()[:60]


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
def post(
    db: Session,
    batch: Batch,
    movement_type: str,
    quantity: int,
    *,
    txn_quantity: int | None = None,
    txn_unit: str = "BASE",
    reference_type: str = "",
    reference_id: int | None = None,
    reference_no: str = "",
    reason: str = "",
    user: User | None = None,
    reversal_of: InventoryMovement | None = None,
    levels: dict | None = None,
) -> InventoryMovement:
    """Post one movement of ``quantity`` base units (a positive magnitude).

    ``levels`` optionally records the document's packaging levels beside the base quantity
    (purchase_quantity / purchase_uom / retail_quantity / retail_uom / base_uom)."""
    if movement_type not in MOVEMENT_TYPES:
        raise StockError(f"Unknown movement type {movement_type!r}")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise StockError("Movement quantity must be a whole number above zero")
    delta = quantity if movement_type in INBOUND else -quantity
    db.flush()  # the batch row must exist before the guarded update
    new_balance = db.execute(
        update(Batch)
        .where(Batch.id == batch.id, Batch.quantity + delta >= 0)
        .values(quantity=Batch.quantity + delta, updated_at=utcnow())
        .returning(Batch.quantity)
        .execution_options(synchronize_session=False)
    ).scalar()
    if new_balance is None:
        current = db.scalar(select(Batch.quantity).where(Batch.id == batch.id)) or 0
        item = batch.item or db.get(Item, batch.item_id)
        raise StockError(
            f"Insufficient stock: only {current} {units.unit_label(item.base_unit, current)} of {item.name} are in "
            f"batch {batch.batch_no or '(no batch)'}; cannot remove {quantity}."
        )
    set_committed_value(batch, "quantity", new_balance)
    from app.services import financials
    if reversal_of is not None:
        cost = reversal_of.unit_cost_snapshot
        status = reversal_of.financial_status
    elif reference_type == "SALE_ITEM" and movement_type == "SALE":
        from app.models import SaleItem
        line = db.get(SaleItem, reference_id)
        cost = financials.effective_cost(line)
        status = line.financial_status
    else:
        cost, status, _ = financials.batch_cost(batch)
    movement = InventoryMovement(
        item_id=batch.item_id,
        batch_id=batch.id,
        movement_type=movement_type,
        quantity=delta,
        unit_cost_snapshot=cost, cost_amount=money(cost * delta) if cost is not None else None, financial_status=status,
        balance_after=new_balance,
        txn_quantity=txn_quantity if txn_quantity is not None else quantity,
        txn_unit=txn_unit,
        units_per_pack=batch.units_per_pack or 1,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_no=(reference_no or "")[:60],
        reason=reason,
        reversal_of_id=reversal_of.id if reversal_of is not None else None,
        user_id=user.id if user else None,
    )
    sign = 1 if delta > 0 else -1
    if levels:
        movement.purchase_quantity = sign * levels["purchase_quantity"] if levels.get("purchase_quantity") is not None else None
        movement.purchase_uom = levels.get("purchase_uom")
        movement.retail_quantity = sign * levels["retail_quantity"] if levels.get("retail_quantity") is not None else None
        movement.retail_uom = levels.get("retail_uom")
        movement.base_uom = levels.get("base_uom")
    elif reversal_of is not None and reversal_of.purchase_uom:
        share = Decimal(quantity) / abs(reversal_of.quantity)        # a reversal mirrors the levels it undoes
        movement.purchase_uom, movement.retail_uom, movement.base_uom = reversal_of.purchase_uom, reversal_of.retail_uom, reversal_of.base_uom
        if reversal_of.purchase_quantity is not None:
            movement.purchase_quantity = -Decimal(reversal_of.purchase_quantity) * share
        if reversal_of.retail_quantity is not None:
            movement.retail_quantity = -Decimal(reversal_of.retail_quantity) * share
    if reversal_of is not None and reversal_of.cost_amount is not None:
        previous = reversed_quantity(db, reversal_of)
        original = abs(reversal_of.cost_amount)
        part = money(original * (previous + quantity) / abs(reversal_of.quantity)) - money(original * previous / abs(reversal_of.quantity))
        movement.cost_amount = part if delta > 0 else -part
    db.add(movement)
    db.flush()
    return movement


def reverse(
    db: Session,
    movement: InventoryMovement,
    *,
    quantity: int | None = None,
    movement_type: str | None = None,
    reason: str = "",
    user: User | None = None,
    reference_type: str | None = None,
    reference_id: int | None = None,
    reference_no: str | None = None,
) -> InventoryMovement:
    """Undo (all or part of) a movement with a linked opposite movement."""
    remaining = abs(movement.quantity) - reversed_quantity(db, movement)
    qty = remaining if quantity is None else quantity
    if qty <= 0 or qty > remaining:
        raise StockError(f"Cannot reverse {qty}; only {remaining} of this movement remain")
    if movement_type is None:
        movement_type = {"SALE": "SALE_CANCEL", "PURCHASE_RECEIPT": "RECEIPT_REVERSAL",
                         "FREE_STOCK": "RECEIPT_REVERSAL", "OPENING_STOCK": "ADJUSTMENT_OUT",
                         "ADJUSTMENT_IN": "ADJUSTMENT_OUT", "ADJUSTMENT_OUT": "ADJUSTMENT_IN",
                         "PURCHASE_RETURN": "ADJUSTMENT_IN", "SALE_RETURN": "ADJUSTMENT_OUT",
                         "SALE_CANCEL": "SALE"}.get(movement.movement_type, "ADJUSTMENT_IN")
    return post(
        db, movement.batch, movement_type, qty,
        reference_type=movement.reference_type if reference_type is None else reference_type,
        reference_id=movement.reference_id if reference_id is None else reference_id,
        reference_no=movement.reference_no if reference_no is None else reference_no,
        reason=reason or f"Reversal of {MOVEMENT_LABELS.get(movement.movement_type, movement.movement_type)}",
        user=user, reversal_of=movement,
    )


def reversed_quantity(db: Session, movement: InventoryMovement) -> int:
    return abs(int(db.scalar(
        select(func.coalesce(func.sum(InventoryMovement.quantity), 0))
        .where(InventoryMovement.reversal_of_id == movement.id)
    ) or 0))


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------
def sync_unit_prices(batch: Batch) -> None:
    upp = max(int(batch.units_per_pack or 1), 1)
    batch.unit_mrp = units.unit_price(batch.mrp, upp)
    from app.services import financials
    batch.unit_cost = financials.unit_cost(batch.purchase_rate, upp)
    batch.cost_status = financials.RESOLVED if batch.purchase_rate > 0 else financials.ZERO if batch.cost_status == financials.ZERO else financials.MISSING


def find_batch(db: Session, item: Item, batch_no: str, expiry_date: date | None) -> Batch | None:
    norm = normalize_batch_no(batch_no)
    if norm:
        return db.scalar(select(Batch).where(Batch.item_id == item.id, Batch.batch_no_normalized == norm))
    return db.scalar(select(Batch).where(
        Batch.item_id == item.id, Batch.batch_no_normalized == "", Batch.expiry_date == expiry_date,
    ))


def resolve_batch(
    db: Session,
    item: Item,
    *,
    batch_no: str = "",
    expiry_date: date | None = None,
    mrp: Any = 0,
    purchase_rate: Any = 0,
    selling_rate: Any = 0,
    supplier_id: int | None = None,
    purchase_id: int | None = None,
    user: User | None = None,
    ip_address: str = "",
    adapt: list | None = None,
) -> Batch:
    """The batch this stock belongs to, created on first receipt.

    With ``adapt`` (a list that collects notes) a restock of an existing batch is reconciled by
    rule instead of refused: an empty batch takes the incoming expiry / MRP; a batch with stock
    keeps its expiry and takes the lower of the two MRPs, so no pack is ever sold above the
    price printed on it. A pack-size clash with stock still on hand is always refused.

    Same product + same batch number is always the same batch. Contradicting
    expiry, MRP or pack size is a :class:`BatchConflict`, never a silent merge
    or overwrite; a missing value on the existing batch may be completed.
    """
    batch_no = str(batch_no or "").strip()[:60]
    mrp_d = to_decimal(mrp)
    # Unlabelled, undated goods have no manufactured-lot identity. Keep each
    # purchase's price/provenance separate instead of merging unrelated receipts.
    batch = None if not batch_no and expiry_date is None and purchase_id else find_batch(db, item, batch_no, expiry_date)
    upp = max(int(item.units_per_pack or 1), 1)
    if batch is None:
        batch = Batch(
            item_id=item.id, batch_no=batch_no, batch_no_normalized=normalize_batch_no(batch_no),
            expiry_date=expiry_date, quantity=0, mrp=mrp_d, purchase_rate=to_decimal(purchase_rate),
            selling_rate=to_decimal(selling_rate), units_per_pack=upp,
            supplier_id=supplier_id, purchase_id=purchase_id,
        )
        sync_unit_prices(batch)
        db.add(batch)
        db.flush()
        audit.record(
            db, action=audit.A_CREATE, entity_type="batch", entity_id=batch.id, user=user,
            after=audit.snapshot(batch), details=f"New batch {batch_no or '(no batch)'} for {item.name}",
            ip_address=ip_address,
        )
        return batch

    label = batch.batch_no or "(no batch)"
    if adapt is not None:
        if expiry_date and batch.expiry_date and (expiry_date.year, expiry_date.month) != (batch.expiry_date.year, batch.expiry_date.month):
            if batch.quantity <= 0:
                adapt.append(f"Batch {label}: expiry updated {batch.expiry_date:%b-%Y} → {expiry_date:%b-%Y} (no stock was left)")
                batch.expiry_date = expiry_date
            else:
                adapt.append(f"Batch {label}: invoice expiry {expiry_date:%b-%Y} differs from the {batch.expiry_date:%b-%Y} on the "
                             "packs already in stock; the batch keeps its expiry")
            expiry_date = batch.expiry_date
        if mrp_d and batch.mrp and mrp_d != to_decimal(batch.mrp):
            if batch.quantity <= 0:
                adapt.append(f"Batch {label}: MRP updated ₹{batch.mrp} → ₹{mrp_d} (no stock was left)")
                batch.mrp = mrp_d
            else:
                low = min(mrp_d, to_decimal(batch.mrp))
                adapt.append(f"Batch {label}: invoice MRP ₹{mrp_d}, stock on hand ₹{batch.mrp}; the batch sells at ₹{low}, "
                             "the lower printed price")
                batch.mrp = low
            sync_unit_prices(batch)
            mrp_d = to_decimal(batch.mrp)
    if expiry_date and batch.expiry_date and (expiry_date.year, expiry_date.month) != (
        batch.expiry_date.year, batch.expiry_date.month
    ):
        raise BatchConflict(
            f"Batch {label} of {item.name} already exists with expiry "
            f"{batch.expiry_date:%b-%Y}; incoming stock says {expiry_date:%b-%Y}. Verify the batch."
        )
    if mrp_d and batch.mrp and mrp_d != to_decimal(batch.mrp):
        raise BatchConflict(
            f"Batch {label} of {item.name} already exists with MRP {batch.mrp}; incoming stock says "
            f"{mrp_d}. Verify the MRP before receiving."
        )
    if (batch.units_per_pack or 1) != upp and batch.quantity > 0:
        raise BatchConflict(
            f"Batch {label} of {item.name} was received as {batch.units_per_pack}/pack but the product "
            f"is now {upp}/pack. Receive it as a new batch or restore the packaging."
        )
    changed = False
    if expiry_date and batch.expiry_date is None:
        batch.expiry_date, changed = expiry_date, True
    if mrp_d and not batch.mrp:
        batch.mrp, changed = mrp_d, True
    if (batch.units_per_pack or 1) != upp:  # empty batch re-received under new packaging
        batch.units_per_pack, changed = upp, True
    if purchase_rate and to_decimal(purchase_rate) != batch.purchase_rate:
        batch.purchase_rate, changed = to_decimal(purchase_rate), True
    if selling_rate:
        batch.selling_rate = to_decimal(selling_rate)
    if supplier_id and not batch.supplier_id:
        batch.supplier_id = supplier_id
    if changed:
        sync_unit_prices(batch)
    db.flush()
    return batch


def receive(
    db: Session,
    item: Item,
    *,
    quantity: int,
    unit: str = "PACK",
    free: int = 0,
    movement_type: str = "PURCHASE_RECEIPT",
    batch_no: str = "",
    expiry_date: date | None = None,
    mrp: Any = 0,
    purchase_rate: Any = 0,
    selling_rate: Any = 0,
    supplier_id: int | None = None,
    purchase_id: int | None = None,
    reference_type: str = "",
    reference_id: int | None = None,
    reference_no: str = "",
    reason: str = "",
    user: User | None = None,
    ip_address: str = "",
    levels: tuple[dict | None, dict | None] | None = None,
    adapt: list | None = None,
) -> Batch:
    """Receive stock into a batch: paid quantity and free quantity post
    separately (``FREE_STOCK``) so free goods are never lost. ``levels`` (paid, free)
    records each movement's packaging levels."""
    if movement_type not in INBOUND:
        raise StockError(f"{movement_type} does not add stock")
    if quantity < 0 or free < 0:
        raise StockError("Quantity cannot be negative")
    batch = resolve_batch(
        db, item, batch_no=batch_no, expiry_date=expiry_date, mrp=mrp, purchase_rate=purchase_rate,
        selling_rate=selling_rate, supplier_id=supplier_id, purchase_id=purchase_id,
        user=user, ip_address=ip_address, adapt=adapt,
    )
    upp = batch.units_per_pack or 1
    refs = dict(reference_type=reference_type, reference_id=reference_id, reference_no=reference_no,
                reason=reason, user=user)
    paid_levels, free_levels = levels or (None, None)
    if quantity:
        post(db, batch, movement_type, units.to_base(quantity, upp, unit),
             txn_quantity=quantity, txn_unit=unit.upper(), levels=paid_levels, **refs)
    if free:
        post(db, batch, "FREE_STOCK", units.to_base(free, upp, unit),
             txn_quantity=free, txn_unit=unit.upper(), levels=free_levels, **refs)
    if item.mrp in (None, 0) and batch.mrp:
        item.mrp = batch.mrp
    return batch


# ---------------------------------------------------------------------------
# FEFO allocation
# ---------------------------------------------------------------------------
@dataclass
class Allocation:
    batch: Batch
    quantity: int


def sellable_batches(db: Session, item_id: int, today: date | None = None) -> list[Batch]:
    """In-stock, unexpired batches in FEFO order (no-expiry batches last)."""
    today = today or date.today()
    rows = db.scalars(
        select(Batch)
        .where(Batch.item_id == item_id, Batch.quantity > 0)
        .order_by(Batch.expiry_date.asc().nulls_last(), Batch.id.asc())
    )
    return [b for b in rows if not units.is_expired(b.expiry_date, today)]


def available(db: Session, item_id: int, today: date | None = None) -> int:
    return sum(b.quantity for b in sellable_batches(db, item_id, today))


def allocate(
    db: Session, item: Item, quantity: int, *, batch_id: int | None = None, today: date | None = None,
) -> list[Allocation]:
    """Which batches ``quantity`` base units come from (FEFO, or one chosen batch).

    The read here may be stale under concurrency; :func:`post` re-checks each
    batch atomically, so an oversell fails the whole transaction.
    """
    today = today or date.today()
    label = units.unit_label(item.base_unit, quantity)
    if batch_id:
        batch = db.get(Batch, batch_id)
        if batch is None or batch.item_id != item.id:
            raise StockError(f"Batch {batch_id} does not belong to {item.name}")
        if units.is_expired(batch.expiry_date, today):
            raise StockError(
                f"Batch {batch.batch_no} expired in {batch.expiry_date:%b-%Y} and cannot be sold."
            )
        if batch.quantity < quantity:
            raise StockError(
                f"Insufficient stock: only {batch.quantity} {units.unit_label(item.base_unit, batch.quantity)} of {item.name} "
                f"are in batch {batch.batch_no or '(no batch)'}."
            )
        return [Allocation(batch, quantity)]
    batches = sellable_batches(db, item.id, today)
    have = sum(b.quantity for b in batches)
    if have < quantity:
        raise StockError(
            f"Insufficient stock: only {have} {units.unit_label(item.base_unit, have)} of {item.name} are available "
            f"across non-expired batches; {quantity} {label} requested."
        )
    plan: list[Allocation] = []
    need = quantity
    for batch in batches:
        take = min(batch.quantity, need)
        plan.append(Allocation(batch, take))
        need -= take
        if not need:
            break
    return plan


# ---------------------------------------------------------------------------
# Reading & reconciliation
# ---------------------------------------------------------------------------
def movements(db: Session, *, item_id: int | None = None, batch_id: int | None = None,
              limit: int = 500) -> list[InventoryMovement]:
    stmt = select(InventoryMovement)
    if item_id:
        stmt = stmt.where(InventoryMovement.item_id == item_id)
    if batch_id:
        stmt = stmt.where(InventoryMovement.batch_id == batch_id)
    return list(db.scalars(stmt.order_by(InventoryMovement.id.desc()).limit(limit)))


def reconcile(db: Session) -> list[dict]:
    """Batches whose projection differs from their ledger (should be empty)."""
    ledger = (
        select(InventoryMovement.batch_id, func.sum(InventoryMovement.quantity).label("qty"))
        .group_by(InventoryMovement.batch_id).subquery()
    )
    rows = db.execute(
        select(Batch.id, Batch.item_id, Batch.quantity, func.coalesce(ledger.c.qty, 0))
        .outerjoin(ledger, ledger.c.batch_id == Batch.id)
        .where(Batch.quantity != func.coalesce(ledger.c.qty, 0))
    ).all()
    return [{"batch_id": r[0], "item_id": r[1], "projection": r[2], "ledger": int(r[3])} for r in rows]


def rebuild_projection(db: Session) -> int:
    """Reset every batch quantity to its ledger sum. Returns batches fixed."""
    fixed = reconcile(db)
    for row in fixed:
        db.execute(update(Batch).where(Batch.id == row["batch_id"]).values(quantity=row["ledger"])
                   .execution_options(synchronize_session=False))
    db.flush()
    return len(fixed)
