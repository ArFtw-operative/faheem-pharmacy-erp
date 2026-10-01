"""Stock adjustments: numbered, immutable documents over the inventory ledger.

Every manual stock correction is an ADJ-000001 document tied to exactly one
ledger movement:

    OUT  LOOSE (loose / missing) · DAMAGE · EXPIRED · COUNT (count shortfall) · OTHER
    IN   SURPLUS (count surplus) · OTHER

A document is never edited or deleted. A mistake is corrected by *reversing*
it: a new ADJ document posts the opposite movement (linked in the ledger) and
the original is marked reversed. Loss value uses the batch's unit cost.
"""
from __future__ import annotations


from sqlalchemy.orm import Session

from app import audit
from app.models import Batch, InventoryMovement, Item, StockAdjustment, User
from app.sequences import next_number
from app.services import stock_ledger
from app.utils import money, to_decimal, utcnow

OUT_TYPES = ("LOOSE", "DAMAGE", "EXPIRED", "COUNT", "OTHER")
IN_TYPES = ("SURPLUS", "OTHER")
LABELS = {"LOOSE": "Loose / missing", "DAMAGE": "Damage", "EXPIRED": "Expired", "COUNT": "Count shortfall",
          "SURPLUS": "Count surplus", "OTHER": "Other"}


class AdjustmentError(Exception):
    pass


def _ref(db: Session) -> str:
    return f"ADJ-{next_number(db, 'adjustment_ref'):06d}"


def create(db: Session, *, item: Item, direction: str, category: str, quantity: int, reason: str,
           batch: Batch | None = None, new_batch: dict | None = None, user: User | None = None,
           ip_address: str = "") -> StockAdjustment:
    """Post one adjustment. OUT takes stock from ``batch``; IN adds to ``batch``
    or, with ``new_batch`` (batch_no, expiry_date, mrp, cost), to a new/matching batch."""
    direction = (direction or "").upper()
    category = (category or "").upper()
    reason = " ".join(str(reason or "").split())[:300]
    allowed = OUT_TYPES if direction == "OUT" else IN_TYPES if direction == "IN" else ()
    if not allowed:
        raise AdjustmentError("Choose Increase or Decrease")
    if category not in allowed:
        raise AdjustmentError(f"{LABELS.get(category, category)} is not a {'decrease' if direction == 'OUT' else 'increase'} type"
                              f" — use one of: {', '.join(LABELS[c] for c in allowed)}")
    if not reason:
        raise AdjustmentError("A reason is required for every stock adjustment")
    if not isinstance(quantity, int) or quantity <= 0:
        raise AdjustmentError("Quantity must be a whole number above zero")
    if batch is not None and batch.item_id != item.id:
        raise AdjustmentError("That batch belongs to another product")
    if direction == "OUT" and batch is None:
        raise AdjustmentError("Choose the batch the stock is taken from")
    if direction == "IN" and batch is None and not new_batch:
        raise AdjustmentError("Choose the batch, or enter the new batch's details")
    ref = _ref(db)
    adj = StockAdjustment(item_id=item.id, batch_id=batch.id if batch else None, reference_no=ref, direction=direction,
                          category=category, quantity=quantity, reason=reason, created_by=user.id if user else None)
    db.add(adj)
    db.flush()
    note = f"{ref} · {LABELS[category]}: {reason}"
    try:
        if direction == "OUT":
            move = stock_ledger.post(db, batch, "ADJUSTMENT_OUT", quantity, reference_type="ADJUSTMENT",
                                     reference_id=adj.id, reference_no=ref, reason=note, user=user)
        elif batch is not None:
            move = stock_ledger.post(db, batch, "ADJUSTMENT_IN", quantity, reference_type="ADJUSTMENT",
                                     reference_id=adj.id, reference_no=ref, reason=note, user=user)
        else:
            from app.services import inventory_service

            mrp = to_decimal(new_batch.get("mrp") or 0)
            if mrp <= 0:
                raise AdjustmentError("Enter the pack MRP for the new batch")
            batch = inventory_service.add_or_update_batch(
                db, item, batch_no=str(new_batch.get("batch_no") or "").strip()[:60], expiry_date=new_batch.get("expiry_date"),
                quantity=quantity, unit="BASE", movement_type="OPENING_STOCK" if new_batch.get("opening") else "ADJUSTMENT_IN",
                mrp=mrp, purchase_rate=to_decimal(new_batch.get("cost") or 0), reference_type="ADJUSTMENT",
                reference_id=adj.id, reference_no=ref, reason=note, user=user, ip_address=ip_address)
            adj.batch_id = batch.id
            move = db.query(InventoryMovement).filter(InventoryMovement.reference_type == "ADJUSTMENT",
                                                      InventoryMovement.reference_id == adj.id).order_by(InventoryMovement.id.desc()).first()
    except stock_ledger.StockError as exc:
        raise AdjustmentError(str(exc))
    adj.movement_id = move.id if move is not None else None
    adj.value = money(to_decimal(batch.unit_cost) * quantity)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="stock_adjustment", entity_id=ref, user=user,
                 after={"direction": direction, "category": category, "quantity": quantity, "batch": batch.batch_no,
                        "value": str(adj.value)},
                 details=f"{ref}: {LABELS[category]} {'+' if direction == 'IN' else '−'}{quantity} {item.name} — {reason}",
                 ip_address=ip_address)
    return adj


def reverse(db: Session, adj: StockAdjustment, *, reason: str, user: User | None = None) -> StockAdjustment:
    """Correct a mistaken adjustment with an opposite, linked document."""
    reason = " ".join(str(reason or "").split())[:300]
    if not reason:
        raise AdjustmentError("A reason is required to reverse an adjustment")
    if adj.reversal_of_id:
        raise AdjustmentError(f"{adj.reference_no} is itself a reversal — post a new adjustment instead")
    if adj.reversed_at is not None:
        raise AdjustmentError(f"{adj.reference_no} was already reversed")
    move = db.get(InventoryMovement, adj.movement_id) if adj.movement_id else None
    if move is None:
        raise AdjustmentError(f"{adj.reference_no} has no ledger movement to reverse")
    ref = _ref(db)
    rev = StockAdjustment(item_id=adj.item_id, batch_id=adj.batch_id, reference_no=ref,
                          direction="IN" if adj.direction == "OUT" else "OUT", category=adj.category,
                          quantity=adj.quantity, value=adj.value, reason=f"Reversal of {adj.reference_no}: {reason}",
                          reversal_of_id=adj.id, created_by=user.id if user else None)
    db.add(rev)
    db.flush()
    try:
        back = stock_ledger.reverse(db, move, reason=f"{ref} · reversal of {adj.reference_no}: {reason}", user=user,
                                    reference_type="ADJUSTMENT", reference_id=rev.id, reference_no=ref)
    except stock_ledger.StockError as exc:
        raise AdjustmentError(str(exc))
    rev.movement_id = back.id
    adj.reversed_at, adj.reversed_by = utcnow(), user.id if user else None
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="stock_adjustment", entity_id=ref, user=user,
                 after={"reverses": adj.reference_no, "quantity": adj.quantity},
                 details=f"{ref} reverses {adj.reference_no}: {reason}")
    return rev
