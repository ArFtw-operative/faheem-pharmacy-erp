"""Stock adjustments API (ERP Adjustments module): register, post, reverse.

Every adjustment is a numbered, immutable ADJ document over the inventory
ledger (see app.services.adjustment_service)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import Batch, Item, StockAdjustment, User
from app.routing import OffloadRoute
from app.services import adjustment_service, units, business_time
from app.utils import money

router = APIRouter(tags=["adjustments"], route_class=OffloadRoute)


@router.get("/adjustments")
def adjustments_page(user: User = Depends(require_permission("adjustment.create"))):
    return RedirectResponse("/app/adjustments", status_code=303)


def _bounds(db: Session, start: str, end: str):
    tz = ZoneInfo(business_time.timezone_name(db))
    to_utc = lambda d: datetime.combine(d, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    try:
        lo = to_utc(date.fromisoformat(start)) if start else None
        hi = to_utc(date.fromisoformat(end) + timedelta(days=1)) if end else None
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    return lo, hi


def _view(a: StockAdjustment, users: dict[int, str], refs: dict[int, str]) -> dict:
    return {
        "id": a.id, "reference_no": a.reference_no or f"#{a.id}", "date": a.adjustment_date.isoformat(),
        "item_id": a.item_id, "item": a.item.name if a.item else "", "code": a.item.article_id if a.item else "",
        "batch_id": a.batch_id, "batch": a.batch.batch_no if a.batch else "", "direction": a.direction,
        "category": a.category, "label": adjustment_service.LABELS.get(a.category, a.category),
        "quantity": a.quantity, "unit": (a.item.base_unit if a.item else "UNIT").lower(), "value": str(a.value),
        "reason": a.reason, "user": users.get(a.created_by, ""),
        "reversal_of": refs.get(a.reversal_of_id, "") if a.reversal_of_id else "",
        "reversed": a.reversed_at is not None, "reversed_at": a.reversed_at.isoformat() if a.reversed_at else "",
    }


@router.get("/api/erp/adjustments")
def adjustment_register(start: str = "", end: str = "", category: str = "", direction: str = "", q: str = "",
                        item: int | None = None, limit: int = 300, offset: int = 0, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("inventory.view"))):
    lo, hi = _bounds(db, start, end)
    conds = []
    if lo:
        conds.append(StockAdjustment.adjustment_date >= lo)
    if hi:
        conds.append(StockAdjustment.adjustment_date < hi)
    if category.upper() in adjustment_service.LABELS:
        conds.append(StockAdjustment.category == category.upper())
    if direction.upper() in ("IN", "OUT"):
        conds.append(StockAdjustment.direction == direction.upper())
    if item:
        conds.append(StockAdjustment.item_id == item)
    stmt = select(StockAdjustment).join(Item, Item.id == StockAdjustment.item_id).outerjoin(Batch, Batch.id == StockAdjustment.batch_id)
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Item.name.ilike(like), Item.article_id.ilike(like), Batch.batch_no.ilike(like),
                         StockAdjustment.reference_no.ilike(like), StockAdjustment.reason.ilike(like)))
    stmt = stmt.where(*conds)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    standing = (StockAdjustment.reversal_of_id.is_(None), StockAdjustment.reversed_at.is_(None))
    agg = db.execute(select(
        func.coalesce(func.sum(case((StockAdjustment.direction == "OUT", StockAdjustment.quantity), else_=0)), 0),
        func.coalesce(func.sum(case((StockAdjustment.direction == "IN", StockAdjustment.quantity), else_=0)), 0),
        func.coalesce(func.sum(case((StockAdjustment.direction == "OUT", StockAdjustment.value), else_=0)), 0))
        .select_from(StockAdjustment).join(Item, Item.id == StockAdjustment.item_id)
        .outerjoin(Batch, Batch.id == StockAdjustment.batch_id).where(*conds, *standing)).one()
    rows = list(db.scalars(stmt.options(selectinload(StockAdjustment.item), selectinload(StockAdjustment.batch))
                           .order_by(StockAdjustment.adjustment_date.desc(), StockAdjustment.id.desc())
                           .limit(min(max(limit, 1), 1000)).offset(max(offset, 0))))
    users = {u.id: u.username for u in db.scalars(select(User).where(User.id.in_({a.created_by for a in rows if a.created_by})))} if rows else {}
    refs = {a.id: a.reference_no for a in db.scalars(select(StockAdjustment).where(
        StockAdjustment.id.in_({a.reversal_of_id for a in rows if a.reversal_of_id})))} if rows else {}
    return {"total": total, "out": int(agg[0]), "in": int(agg[1]), "loss_value": str(money(agg[2])),
            "types": [{"code": k, "label": v, "in": k in adjustment_service.IN_TYPES, "out": k in adjustment_service.OUT_TYPES}
                      for k, v in adjustment_service.LABELS.items()],
            "adjustments": [_view(a, users, refs) for a in rows]}


@router.post("/api/erp/adjustments")
async def adjustment_create(request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("adjustment.create"))):
    data = await request.json()
    item = db.get(Item, int(data.get("item_id") or 0))
    if item is None or item.deleted_at is not None:
        raise HTTPException(400, "Choose the product")
    try:
        qty = units.parse_qty_expression(data.get("quantity"), item.units_per_pack or 1)
    except units.UnitError as exc:
        raise HTTPException(400, str(exc))
    batch = db.get(Batch, int(data.get("batch_id") or 0)) if data.get("batch_id") else None
    new_batch = None
    if batch is None and str(data.get("direction") or "").upper() == "IN":
        from app.routers.inventory import parse_date

        expiry_text = str(data.get("expiry") or "").strip()
        expiry = parse_date(expiry_text) if expiry_text else None
        if expiry_text and expiry is None:
            raise HTTPException(400, "Expiry must be MM/YYYY or YYYY-MM-DD")
        new_batch = {"batch_no": data.get("batch_no"), "expiry_date": expiry, "mrp": data.get("mrp"), "cost": data.get("cost"),
                     "opening": bool(data.get("opening"))}
    try:
        adj = adjustment_service.create(db, item=item, direction=str(data.get("direction") or ""),
                                        category=str(data.get("category") or ""), quantity=qty,
                                        reason=str(data.get("reason") or ""), batch=batch, new_batch=new_batch,
                                        user=user, ip_address=client_ip(request))
        db.commit()
    except adjustment_service.AdjustmentError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.refresh(adj)
    return {"adjustment": _view(adj, {user.id: user.username}, {}),
            "stock": sum(b.quantity for b in item.batches if b.quantity > 0)}


@router.post("/api/erp/adjustments/{adjustment_id}/reverse")
async def adjustment_reverse(adjustment_id: int, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_permission("adjustment.create"))):
    data = await request.json()
    adj = db.get(StockAdjustment, adjustment_id)
    if adj is None:
        raise HTTPException(404, "Adjustment not found")
    try:
        rev = adjustment_service.reverse(db, adj, reason=str(data.get("reason") or ""), user=user)
        db.commit()
    except adjustment_service.AdjustmentError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.refresh(rev)
    return {"adjustment": _view(rev, {user.id: user.username}, {adj.id: adj.reference_no})}
