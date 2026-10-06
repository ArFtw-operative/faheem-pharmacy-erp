"""Sales History API for the ERP Sales module: bill register, bill detail,
void. Returns / refunds use the existing ``/api/sales/{id}/refundable`` and
``/api/sales/{id}/refund`` endpoints (same service, same rules)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import Sale, SaleItem, SaleReturn, User
from app.permissions import has_permission
from app.routing import OffloadRoute
from app.services import refund_service, sales_service, business_time
from app.utils import money

router = APIRouter(tags=["sales-history"], route_class=OffloadRoute)


def _scope(user: User) -> int | None:
    return None if has_permission(user, "sales.view_history") else user.id


def _bounds(db: Session, start: str, end: str) -> tuple[datetime | None, datetime | None]:
    tz = ZoneInfo(business_time.timezone_name(db))
    to_utc = lambda d: datetime.combine(d, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    try:
        lo = to_utc(date.fromisoformat(start)) if start else None
        hi = to_utc(date.fromisoformat(end) + timedelta(days=1)) if end else None
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if lo and hi and lo >= hi:
        raise HTTPException(400, "From date must be on or before To date")
    return lo, hi


def _row(s: Sale, returned: set[int]) -> dict:
    return {
        "id": s.id, "invoice_no": s.invoice_no, "date": s.sale_date.isoformat(), "type": s.invoice_type or "INVENTORY",
        "customer": s.customer.name if s.customer else "Walk-in", "mobile": s.customer.mobile if s.customer else "",
        "customer_type": s.customer_type or "WALK_IN", "items": len(s.items), "units": sum(i.quantity for i in s.items),
        "total": str(s.total), "discount": str(money((s.discount or 0) + sum((i.discount or 0) for i in s.items))),
        "payment": sales_service.payment_label(s), "mode": s.payment_mode, "status": s.payment_status,
        "returned": s.id in returned, "user": s.user.username if s.user else "",
    }


@router.get("/api/erp/sales")
def sales_register(q: str = "", start: str = "", end: str = "", payment: str = "", status: str = "", type: str = "",
                   customer_type: str = "", limit: int = 200, offset: int = 0, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("sales.view_own"))):
    lo, hi = _bounds(db, start, end)
    conds = sales_service._sales_conditions(db, q="", start=lo, end=hi, user_id=_scope(user),
                                            payment=payment, status=status, customer_type=customer_type)
    text = q.strip()
    if text:
        # invoice / customer (as the classic screen) — or a product or batch inside the bill
        base = sales_service._sales_conditions(db, q=text)
        like = f"%{text}%"
        conds.append(or_(*base, exists().where(SaleItem.sale_id == Sale.id,
                                                or_(SaleItem.product_name.ilike(like), SaleItem.batch_no.ilike(like)))))
    total = db.scalar(select(func.count(Sale.id)).where(*conds)) or 0
    agg = db.execute(select(func.coalesce(func.sum(Sale.total), 0), func.count(Sale.id))
                     .where(*conds, Sale.payment_status != "CANCELLED")).one()
    rows = list(db.scalars(select(Sale).where(*conds)
                           .options(selectinload(Sale.items), selectinload(Sale.payments), selectinload(Sale.customer),
                                    selectinload(Sale.user))
                           .order_by(Sale.sale_date.desc(), Sale.id.desc())
                           .limit(min(max(limit, 1), 500)).offset(max(offset, 0))))
    returned = {sid for (sid,) in db.execute(select(SaleReturn.sale_id).where(
        SaleReturn.sale_id.in_([s.id for s in rows])))} if rows else set()
    return {"total": total, "value": str(money(agg[0])), "bills": agg[1], "sales": [_row(s, returned) for s in rows]}


def _udhaar(db: Session, s: Sale) -> dict | None:
    from app.services import udhaar_service

    e = udhaar_service.entry_for_sale(db, s.id)
    if e is None:
        return None
    today = business_time.current_business_date(db)
    return {"entry_id": e.id, "amount": str(e.amount), "paid": str(e.paid), "balance": str(money(e.amount - e.paid)) if e.status == "OPEN" else "0.00",
            "due_date": e.due_date.isoformat(), "reminder_date": e.reminder_date.isoformat(), "status": udhaar_service.status(e, today)}


@router.get("/api/erp/sales/{sale_id}")
def sale_detail(sale_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.view_own"))):
    s = sales_service.get_sale(db, sale_id)
    if s is None or (_scope(user) and s.user_id != user.id):
        raise HTTPException(404, "Bill not found")
    returned: dict[int, int] = refund_service._returned_qty_by_line(db, s.id)
    returns = [refund_service.return_payload(db, r) for r in refund_service.list_returns(db, sale_id=s.id)]
    return {
        **_row(s, {s.id} if returns else set()),
        "subtotal": str(s.subtotal), "bill_discount": str(s.discount), "round_off": str(s.round_off),
        "tendered": str(s.tendered_amount) if s.tendered_amount is not None else "",
        "change": str(s.change_amount) if s.change_amount is not None else "", "notes": s.notes or "",
        "customer_id": s.customer_id, "customer_code": s.customer.customer_id if s.customer else "",
        "lines": [{
            "id": i.id, "name": i.product_name, "batch": i.batch_no, "expiry": i.expiry_date.isoformat() if i.expiry_date else "",
            "qty": i.quantity, "unit": (i.base_unit or "UNIT").lower(), "rate": str(i.rate), "pack_mrp": str(i.pack_mrp),
            "upp": i.units_per_pack or 1, "discount": str(i.discount), "total": str(i.line_total),
            "returned": returned.get(i.id, 0), "manual": i.item_id is None, "item_id": i.item_id,
        } for i in sorted(s.items, key=lambda i: (i.line_no or 0, i.id))],
        "payments": [{"mode": p.mode, "amount": str(p.amount), "reference": p.reference} for p in s.payments],
        "returns": returns, "refunded": str(refund_service.refunded_total(db, s)),
        "udhaar": _udhaar(db, s),
        "refund_status": refund_service.refund_status(db, s),
        "locked": sales_service.editable_problem(db, s),
    }


@router.post("/api/erp/sales/{sale_id}/void")
async def sale_void(sale_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("sales.void"))):
    data = await request.json()
    reason = " ".join(str(data.get("reason") or "").split())[:200]
    if not reason:
        raise HTTPException(400, "Enter the reason for voiding the bill")
    s = sales_service.get_sale(db, sale_id)
    if s is None:
        raise HTTPException(404, "Bill not found")
    try:
        sales_service.void_sale(db, s, user=user, reason=reason, ip_address=client_ip(request))
        db.commit()
    except sales_service.SaleError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return sale_detail(sale_id, db, user)
