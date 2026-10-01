"""Park / resume unfinished POS sales.

A parked sale is an unfinished draft — never an invoice or payment. It is
created from the bill in progress and claimed back
when the customer is ready.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit
from app.models import ParkedSale, User
from app.sequences import next_number
from app.services import business_time


class ParkingError(Exception):
    pass


class ParkedClaimConflict(ParkingError):
    def __init__(self, parked: ParkedSale | None):
        super().__init__("This parked sale is no longer available")
        self.parked = parked


def next_park_reference(db: Session, when=None) -> str:
    day = (when or business_time.now(db)).strftime("%Y%m%d")
    seq = next_number(db, f"park:{day}")
    return f"PARK-{day}-{seq:04d}"


def park_sale(
    db: Session,
    *,
    payload: dict[str, Any],
    customer_id: int | None,
    reason_code: str = "",
    note: str = "",
    business_date=None,
    user: User | None = None,
    ip_address: str = "",
) -> ParkedSale:
    if not payload or not payload.get("cart"):
        raise ParkingError("There is nothing to park")
    if reason_code == "OTHER" and not (note or "").strip():
        raise ParkingError("A note is required when the reason is Other")
    parked = ParkedSale(
        park_reference=next_park_reference(db),
        status="PARKED",
        customer_id=customer_id,
        business_date=business_date or business_time.current_business_date(db),
        parked_by_user_id=user.id if user else None,
        parked_at=business_time.now(db),
        reason_code=reason_code or "",
        note=note or "",
        payload=payload,
        version=1,
    )
    db.add(parked)
    db.flush()
    audit.record(
        db, action=audit.A_CREATE, entity_type="parked_sale", entity_id=parked.park_reference,
        user=user, after=audit.snapshot(parked),
        details=f"Sale parked: {parked.park_reference}", ip_address=ip_address,
    )
    return parked


def get_parked(db: Session, parked_id: int) -> ParkedSale | None:
    return db.get(ParkedSale, parked_id)


def list_parked(
    db: Session,
    *,
    q: str = "",
    status: str | tuple[str, ...] = "PARKED",
    user_id: int | None = None,
    business_date=None,
    limit: int = 100,
) -> list[ParkedSale]:
    stmt = select(ParkedSale)
    if status and status != "ALL":
        statuses = (status,) if isinstance(status, str) else tuple(status)
        stmt = stmt.where(ParkedSale.status.in_(statuses))
    if user_id:
        stmt = stmt.where(ParkedSale.parked_by_user_id == user_id)
    if business_date:
        stmt = stmt.where(ParkedSale.business_date == business_date)
    if q.strip():
        from app.models import Customer

        like = f"%{q.strip()}%"
        stmt = stmt.outerjoin(Customer, ParkedSale.customer_id == Customer.id).where(
            or_(
                ParkedSale.park_reference.ilike(like),
                ParkedSale.note.ilike(like),
                Customer.name.ilike(like),
                Customer.mobile.ilike(like),
            )
        )
    return list(db.scalars(stmt.order_by(ParkedSale.parked_at.desc()).limit(limit)))


def count_parked(db: Session, *, status: str | tuple[str, ...] = ("PARKED", "CLAIMED")) -> int:
    stmt = select(func.count(ParkedSale.id))
    if isinstance(status, str):
        stmt = stmt.where(ParkedSale.status == status)
    else:
        stmt = stmt.where(ParkedSale.status.in_(tuple(status)))
    return db.scalar(stmt) or 0


def unresolved_for_date(db: Session, business_date) -> list[ParkedSale]:
    return list(
        db.scalars(
            select(ParkedSale)
            .where(ParkedSale.status.in_(("PARKED", "CLAIMED")), ParkedSale.business_date == business_date)
            .order_by(ParkedSale.parked_at.asc())
        )
    )


def resume_parked(
    db: Session,
    parked: ParkedSale,
    *,
    user: User | None = None,
    ip_address: str = "",
) -> ParkedSale:
    if parked.status != "PARKED":
        raise ParkedClaimConflict(parked)
    parked.status = "CLAIMED"
    parked.resumed_by_user_id = user.id if user else None
    parked.resumed_at = business_time.now(db)
    parked.version += 1
    db.flush()
    audit.record(
        db, action=audit.A_UPDATE, entity_type="parked_sale", entity_id=parked.park_reference,
        user=user, after=audit.snapshot(parked),
        details=f"Parked sale resumed: {parked.park_reference}", ip_address=ip_address,
    )
    return parked


def complete_parked(db: Session, parked: ParkedSale, sale_id: int, *, user: User | None = None) -> ParkedSale:
    parked.status = "COMPLETED"
    parked.completed_sale_id = sale_id
    db.flush()
    audit.record(
        db, action=audit.A_UPDATE, entity_type="parked_sale", entity_id=parked.park_reference,
        user=user, after={"completed_sale_id": sale_id},
        details=f"Parked sale completed: {parked.park_reference}",
    )
    return parked


def release_parked(
    db: Session,
    parked: ParkedSale,
    *,
    user: User | None = None,
    ip_address: str = "",
) -> ParkedSale:
    """Return a resumed (CLAIMED) sale to the parked list so it can be picked up again."""
    if parked.status != "CLAIMED":
        raise ParkingError("Only a resumed sale can be released")
    parked.status = "PARKED"
    parked.resumed_by_user_id = None
    parked.resumed_at = None
    parked.version += 1
    db.flush()
    audit.record(
        db, action=audit.A_UPDATE, entity_type="parked_sale", entity_id=parked.park_reference,
        user=user, after=audit.snapshot(parked),
        details=f"Parked sale released: {parked.park_reference}", ip_address=ip_address,
    )
    return parked


def discard_parked(
    db: Session,
    parked: ParkedSale,
    *,
    reason: str = "",
    note: str = "",
    user: User | None = None,
    ip_address: str = "",
) -> ParkedSale:
    if parked.status in ("COMPLETED", "DISCARDED"):
        raise ParkingError("This parked sale is already closed")
    if reason == "OTHER" and not (note or "").strip():
        raise ParkingError("A note is required when the reason is Other")
    parked.status = "DISCARDED"
    parked.discarded_by_user_id = user.id if user else None
    parked.discarded_at = business_time.now(db)
    parked.discard_reason = reason or ""
    if note:
        parked.note = (parked.note + " | " + note).strip(" |")
    db.flush()
    audit.record(
        db, action=audit.A_DELETE, entity_type="parked_sale", entity_id=parked.park_reference,
        user=user, after=audit.snapshot(parked),
        details=f"Parked sale discarded: {reason}", ip_address=ip_address,
    )
    return parked


def parked_payload(db: Session, parked: ParkedSale) -> dict[str, Any]:
    from app.models import User as _User

    customer = None
    if parked.customer_id:
        from app.models import Customer

        c = db.get(Customer, parked.customer_id)
        if c is not None:
            customer = {"id": c.id, "customer_id": c.customer_id, "name": c.name, "mobile": c.mobile}
    parked_by = db.get(_User, parked.parked_by_user_id) if parked.parked_by_user_id else None
    resumed_by = db.get(_User, parked.resumed_by_user_id) if parked.resumed_by_user_id else None
    payload = parked.payload or {}
    cart = payload.get("cart") or []
    return {
        "id": parked.id,
        "park_reference": parked.park_reference,
        "status": parked.status,
        "business_date": parked.business_date.isoformat(),
        "customer": customer,
        "items": len(cart),
        "total": _cart_total(cart),
        "reason_code": parked.reason_code,
        "note": parked.note,
        "parked_at": parked.parked_at.isoformat(sep=" ", timespec="seconds") if parked.parked_at else None,
        "parked_by": parked_by.employee_id if parked_by else "",
        "resumed_by": resumed_by.employee_id if resumed_by else "",
        "resumed_at": parked.resumed_at.isoformat(sep=" ", timespec="seconds") if parked.resumed_at else None,
        "completed_sale_id": parked.completed_sale_id,
        "payload": payload,
    }


def _cart_total(cart: list[dict]) -> str:
    total = 0
    for line in cart:
        try:
            total += float(line.get("rate", 0)) * int(line.get("quantity", 0)) - float(line.get("discount", 0) or 0)
        except (TypeError, ValueError):
            continue
    return f"{total:.2f}"
