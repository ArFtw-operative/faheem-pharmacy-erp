"""Purchase returns (debit notes to the supplier), numbered PR-000001."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app import audit
from app.models import Batch, PurchaseReturn, User
from app.utils import money, utcnow


class PurchaseError(Exception):
    pass


def create_return(
    db: Session,
    *,
    item_id: int,
    batch_id: int | None,
    quantity: int,
    value: Any,
    purchase_id: int | None = None,
    supplier_id: int | None = None,
    reason: str = "",
    return_date: datetime | None = None,
    user: User | None = None,
    ip_address: str = "",
) -> PurchaseReturn:
    if quantity <= 0:
        raise PurchaseError("Return quantity must be positive")
    from app.models import Item

    item = db.get(Item, item_id)
    if item is None:
        raise PurchaseError("Item not found")
    batch = db.get(Batch, batch_id) if batch_id else None
    if batch is not None:
        if batch.item_id != item.id:
            raise PurchaseError("Batch does not belong to this product")
        if batch.quantity < quantity:
            raise PurchaseError(
                f"Cannot return {quantity} of {item.name}; only {batch.quantity} in stock"
            )
    if batch is not None:
        supplier_id = supplier_id or batch.supplier_id
        purchase_id = purchase_id or batch.purchase_id
    from app.sequences import next_number

    ret = PurchaseReturn(
        reference_no=f"PR-{next_number(db, 'purchase_return_ref'):06d}",
        purchase_id=purchase_id,
        supplier_id=supplier_id,
        item_id=item_id,
        batch_id=batch_id,
        product_name=item.name,
        quantity=quantity,
        value=money(value),
        return_date=return_date or utcnow(),
        reason=reason,
        status="SETTLED",
        created_by=user.id if user else None,
    )
    db.add(ret)
    db.flush()
    if batch is not None:
        from app.services import inventory_service

        inventory_service.decrement_stock(
            db, batch, quantity, movement_type="PURCHASE_RETURN", reference_type="PURCHASE_RETURN",
            reference_id=ret.id, reference_no=ret.reference_no,
            reason=f"Purchase return {ret.reference_no}: {reason}".rstrip(": "), user=user,
            ip_address=ip_address,
        )
    audit.record(
        db,
        action=audit.A_CREATE,
        entity_type="purchase_return",
        entity_id=ret.id,
        user=user,
        after=audit.snapshot(ret),
        details=f"Purchase return {ret.reference_no}: {quantity} x {item.name}",
        ip_address=ip_address,
    )
    return ret
