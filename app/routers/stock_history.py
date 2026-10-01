"""Stock history: every inventory ledger movement across all products.

Read-only by design — the ledger is immutable; corrections are new movements
(reversals) that appear here like any other. Dates are business-local days.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, contains_eager

from app.database import get_db
from app.deps import require_permission
from app.models import Batch, InventoryMovement, Item, User
from app.routing import OffloadRoute
from app.services import stock_ledger, business_time

router = APIRouter(tags=["stock-history"], route_class=OffloadRoute)
INBOUND = ("OPENING_STOCK", "PURCHASE_RECEIPT", "FREE_STOCK", "SALE_RETURN", "SALE_CANCEL", "ADJUSTMENT_IN", "REPACK_IN")


def _utc_bounds(db: Session, start: str, end: str) -> tuple[datetime, datetime]:
    """Business-local day range → naive UTC instants (the ledger stores UTC)."""
    tz = ZoneInfo(business_time.timezone_name(db))
    try:
        last = date.fromisoformat(end) if end else datetime.now(tz).date()
        first = date.fromisoformat(start) if start else last - timedelta(days=6)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if first > last:
        raise HTTPException(400, "From date must be on or before To date")
    if (last - first).days > 3660:
        raise HTTPException(400, "Choose a range of up to ten years")
    to_utc = lambda d: datetime.combine(d, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(first), to_utc(last + timedelta(days=1))


def _query(db: Session, *, start: str, end: str, type: str, direction: str, q: str, item: int | None,
           batch: int | None, user: int | None):
    lo, hi = _utc_bounds(db, start, end)
    conds = [InventoryMovement.created_at >= lo, InventoryMovement.created_at < hi]
    types = [t.strip().upper() for t in type.split(",") if t.strip()]
    unknown = set(types) - set(stock_ledger.MOVEMENT_LABELS)
    if unknown:
        raise HTTPException(400, "Unknown movement type: " + ", ".join(sorted(unknown)))
    if types:
        conds.append(InventoryMovement.movement_type.in_(types))
    if direction == "in":
        conds.append(InventoryMovement.quantity > 0)
    elif direction == "out":
        conds.append(InventoryMovement.quantity < 0)
    if item:
        conds.append(InventoryMovement.item_id == item)
    if batch:
        conds.append(InventoryMovement.batch_id == batch)
    if user:
        conds.append(InventoryMovement.user_id == user)
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(Item.name.ilike(like), Item.article_id.ilike(like), Batch.batch_no.ilike(like),
                         InventoryMovement.reference_no.ilike(like), InventoryMovement.reason.ilike(like)))
    return conds


def _base(conds):
    return (select(InventoryMovement).join(Item, Item.id == InventoryMovement.item_id)
            .join(Batch, Batch.id == InventoryMovement.batch_id).where(*conds))


def _row(m: InventoryMovement) -> dict:
    it = m.item
    return {
        "id": m.id, "at": m.created_at.isoformat(), "item_id": m.item_id, "item": it.name, "code": it.article_id,
        "batch_id": m.batch_id, "batch_no": m.batch.batch_no if m.batch else "",
        "type": m.movement_type, "label": stock_ledger.MOVEMENT_LABELS.get(m.movement_type, m.movement_type),
        "in": m.quantity if m.quantity > 0 else 0, "out": -m.quantity if m.quantity < 0 else 0,
        "balance": m.balance_after, "unit": (it.base_unit or "UNIT").lower(),
        "txn": f"{m.txn_quantity} {m.txn_unit.lower()}" if m.txn_unit == "PACK" and m.txn_quantity else "",
        "reference": m.reference_no, "reference_type": m.reference_type, "reference_id": m.reference_id,
        "reason": m.reason, "reversal_of": m.reversal_of_id, "user": m.user.username if m.user else "",
    }


@router.get("/api/erp/stock-history")
def stock_history(start: str = "", end: str = "", type: str = "", direction: str = "", q: str = "",
                  item: int | None = None, batch: int | None = None, user: int | None = None,
                  limit: int = 300, offset: int = 0, db: Session = Depends(get_db),
                  who: User = Depends(require_permission("inventory.view"))):
    conds = _query(db, start=start, end=end, type=type, direction=direction, q=q, item=item, batch=batch, user=user)
    rows = db.scalars(_base(conds).options(contains_eager(InventoryMovement.item), contains_eager(InventoryMovement.batch))
                      .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
                      .limit(min(max(limit, 1), 1000)).offset(max(offset, 0))).unique().all()
    agg = db.execute(select(func.count(InventoryMovement.id),
                            func.coalesce(func.sum(case((InventoryMovement.quantity > 0, InventoryMovement.quantity), else_=0)), 0),
                            func.coalesce(func.sum(case((InventoryMovement.quantity < 0, -InventoryMovement.quantity), else_=0)), 0),
                            func.count(func.distinct(InventoryMovement.item_id)))
                     .select_from(InventoryMovement).join(Item, Item.id == InventoryMovement.item_id)
                     .join(Batch, Batch.id == InventoryMovement.batch_id).where(*conds)).one()
    out = {"total": agg[0], "in": int(agg[1]), "out": int(agg[2]), "products": agg[3],
           "movements": [_row(m) for m in rows]}
    if offset == 0:
        out["types"] = [{"code": k, "label": v} for k, v in stock_ledger.MOVEMENT_LABELS.items()]
        out["users"] = [{"id": u.id, "name": u.username} for u in db.scalars(
            select(User).where(User.id.in_(select(InventoryMovement.user_id).distinct())).order_by(User.username))]
    return out


@router.get("/api/erp/stock-history.csv")
def stock_history_csv(start: str = "", end: str = "", type: str = "", direction: str = "", q: str = "",
                      item: int | None = None, batch: int | None = None, user: int | None = None,
                      db: Session = Depends(get_db), who: User = Depends(require_permission("inventory.export"))):
    conds = _query(db, start=start, end=end, type=type, direction=direction, q=q, item=item, batch=batch, user=user)
    tz = ZoneInfo(business_time.timezone_name(db))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Time", "Product code", "Product", "Batch", "Movement", "In", "Out", "Batch balance", "Unit",
                "Packs", "Reference", "Reason", "User"])
    stmt = (_base(conds).options(contains_eager(InventoryMovement.item), contains_eager(InventoryMovement.batch))
            .order_by(InventoryMovement.created_at, InventoryMovement.id).limit(200_000))
    for m in db.scalars(stmt).unique():
        r = _row(m)
        local = m.created_at.replace(tzinfo=timezone.utc).astimezone(tz)
        w.writerow([local.strftime("%d-%m-%Y"), local.strftime("%H:%M"), r["code"], r["item"], r["batch_no"], r["label"],
                    r["in"] or "", r["out"] or "", r["balance"], r["unit"], r["txn"], r["reference"], r["reason"], r["user"]])
    name = f"stock-history_{start or 'recent'}_{end or 'today'}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})
