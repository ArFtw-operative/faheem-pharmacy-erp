"""Rack / box location API (screens: Racks, Inventory, Purchases, POS).

Thin layer over app/services/location_service.py: permissions, request parsing, commit.
Bulk operations take id lists and run server-side in one request.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import client_ip, require_login, require_permission
from app.models import Batch, Category, Item, ItemLocation, User
from app.permissions import has_permission
from app.routing import OffloadRoute
from app.services import business_time, location_service as loc, units

router = APIRouter(route_class=OffloadRoute)


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected a JSON object")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    return data


def _run(db: Session, fn, *args, **kwargs):
    """Call a service function; its refusal becomes a clear 400 / 409 and nothing is half-written."""
    try:
        out = fn(*args, **kwargs)
        db.commit()
        return out
    except loc.NotEmpty as exc:
        db.rollback()
        raise HTTPException(409, {"message": str(exc), "products": exc.products, "units": exc.units})
    except loc.LocationError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))


def _need(user: User, code: str) -> None:
    if not has_permission(user, code):
        raise HTTPException(403, "You do not have permission for this")


# --------------------------------------------------------------------------- racks
@router.get("/api/erp/racks")
def racks(q: str = "", status: str = "", db: Session = Depends(get_db), user: User = Depends(require_permission("rack.view"))):
    return {"racks": loc.list_racks(db, q=q, status=status), "metrics": loc.metrics(db), "config": loc.config(db)}


@router.get("/api/erp/locations/options")
def location_options(db: Session = Depends(get_db), user: User = Depends(require_permission("rack.view"))):
    """Active racks and their active boxes, for the location pickers."""
    from app.models import Rack

    out = []
    for r in db.scalars(select(Rack).where(Rack.is_active.is_(True)).order_by(Rack.sort_order, Rack.code)):
        out.append({"id": r.id, "code": r.code, "name": r.name,
                    "boxes": [{"id": b.id, "code": b.code, "name": b.name} for b in r.boxes if b.is_active]})
    return {"racks": out, "config": loc.config(db)}


@router.post("/api/erp/racks")
async def rack_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.create"))):
    data = await _json(request)
    rack = _run(db, loc.create_rack, db, code=data.get("code", ""), name=data.get("name", ""),
                description=data.get("description", ""), sort_order=data.get("sort_order"), user=user, ip_address=client_ip(request))
    return loc.rack_row(db, rack)


@router.get("/api/erp/racks/{rack_id}")
def rack_detail(rack_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.view"))):
    rack = _run(db, loc.get_rack, db, rack_id)
    return {**loc.rack_row(db, rack), "box_list": loc.list_boxes(db, rack), "config": loc.config(db)}


@router.put("/api/erp/racks/{rack_id}")
async def rack_update(rack_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.edit"))):
    data = await _json(request)
    rack = _run(db, loc.get_rack, db, rack_id)
    fields = {k: data[k] for k in ("code", "name", "description", "sort_order", "zone") if k in data}

    def apply():
        loc.update_rack(db, rack, user=user, ip_address=client_ip(request), **fields)
        if "categories" in data:
            loc.set_category_defaults(db, rack, data["categories"] or [], user=user, ip_address=client_ip(request))

    _run(db, apply)
    return loc.rack_row(db, rack)


@router.post("/api/erp/racks/{rack_id}/status")
async def rack_status(rack_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.disable"))):
    data = await _json(request)
    rack = _run(db, loc.get_rack, db, rack_id)
    if data.get("move_to_rack"):
        _need(user, "rack.bulk_move")
    return _run(db, loc.set_rack_status, db, rack, bool(data.get("active")), move_to_rack=data.get("move_to_rack"),
                move_to_box=data.get("move_to_box"), user=user, ip_address=client_ip(request))


@router.get("/api/erp/racks/{rack_id}/products")
def rack_products(rack_id: int, box: str = "", q: str = "", category: str = "", db: Session = Depends(get_db),
                  user: User = Depends(require_permission("rack.view"))):
    """Products in a rack (``box``: a box id, or ``none`` for those without a box)."""
    rack = _run(db, loc.get_rack, db, rack_id)
    cond = [ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None), ItemLocation.rack_id == rack.id, Item.deleted_at.is_(None)]
    if box == "none":
        cond.append(ItemLocation.box_id.is_(None))
    elif box.isdigit():
        cond.append(ItemLocation.box_id == int(box))
    if category:
        cond.append(Item.category == category)
    if q.strip():
        like = f"%{q.strip()}%"
        cond.append(Item.name.ilike(like) | Item.article_id.ilike(like) | Item.generic_name.ilike(like))
    rows = db.execute(select(Item, ItemLocation).join(ItemLocation, ItemLocation.item_id == Item.id).where(*cond).order_by(Item.name)).all()
    ids = [i.id for i, _ in rows]
    stock, first_exp, batches = {}, {}, {}
    if ids:
        for item_id, qty, expiry in db.execute(select(Batch.item_id, Batch.quantity, Batch.expiry_date)
                                               .where(Batch.item_id.in_(ids), Batch.quantity > 0)):
            stock[item_id] = stock.get(item_id, 0) + qty
            batches[item_id] = batches.get(item_id, 0) + 1
            if expiry and (item_id not in first_exp or expiry < first_exp[item_id]):
                first_exp[item_id] = expiry
    here = loc.current(db, ids)
    return {"rows": [{
        "id": i.id, "code": i.article_id, "name": i.name, "category": i.category, "form": i.dosage_form, "active": bool(i.is_active),
        "box_id": l.box_id, "box": here[i.id].box_code if i.id in here else "", "stock": stock.get(i.id, 0), "batches": batches.get(i.id, 0),
        "stock_label": units.describe_stock(stock.get(i.id, 0), i.units_per_pack or 1, i.base_unit, i.pack_unit),
        "first_expiry": first_exp[i.id].isoformat() if i.id in first_exp else "", "since": l.valid_from.isoformat()}
        for i, l in rows]}


@router.get("/api/erp/racks/{rack_id}/history")
def rack_history(rack_id: int, offset: int = 0, limit: int = 200, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("rack.history.view"))):
    _run(db, loc.get_rack, db, rack_id)
    return loc.rack_history(db, rack_id, offset=max(offset, 0), limit=min(max(limit, 1), 500))


@router.get("/api/erp/racks/{rack_id}/timeline")
def rack_timeline(rack_id: int, days: int = 30, box: str = "", db: Session = Depends(get_db),
                  user: User = Depends(require_permission("rack.snapshot.view"))):
    _run(db, loc.get_rack, db, rack_id)
    return {"days": loc.timeline(db, rack_id, days=days, box_id=int(box) if box.isdigit() else None),
            "timezone": business_time.timezone_name(db)}


@router.get("/api/erp/racks/{rack_id}/snapshot")
def rack_snapshot(rack_id: int, day: str = "", box: str = "", db: Session = Depends(get_db),
                  user: User = Depends(require_permission("rack.snapshot.view"))):
    """The rack's contents at the close of ``day`` (local time), from history — not today's state."""
    _run(db, loc.get_rack, db, rack_id)
    try:
        d = date.fromisoformat(day) if day else business_time.current_business_date(db)
    except ValueError:
        raise HTTPException(400, "Choose a valid date")
    if d > business_time.current_business_date(db):
        raise HTTPException(400, "The date cannot be in the future")
    when = loc.end_of_day(db, d)
    rows = loc.inventory_rows(db, when, rack_ids=[rack_id], box_id=int(box) if box.isdigit() else None, stock="all")
    return {"date": d.isoformat(), "as_of": when.isoformat(), "timezone": business_time.timezone_name(db), "rows": rows,
            "products": len({r["item_id"] for r in rows}), "units": sum(r["quantity"] for r in rows)}


# --------------------------------------------------------------------------- boxes
@router.post("/api/erp/racks/{rack_id}/boxes")
async def box_create(rack_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("box.manage"))):
    data = await _json(request)
    rack = _run(db, loc.get_rack, db, rack_id)
    box = _run(db, loc.create_box, db, rack, code=data.get("code", ""), name=data.get("name", ""),
               description=data.get("description", ""), user=user, ip_address=client_ip(request))
    return {"id": box.id, "boxes": loc.list_boxes(db, rack)}


@router.put("/api/erp/boxes/{box_id}")
async def box_update(box_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("box.manage"))):
    data = await _json(request)
    box = _run(db, loc.get_box, db, box_id)
    _run(db, loc.update_box, db, box, user=user, ip_address=client_ip(request),
         **{k: data[k] for k in ("code", "name", "description") if k in data})
    return {"boxes": loc.list_boxes(db, box.rack)}


@router.post("/api/erp/boxes/{box_id}/status")
async def box_status(box_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("box.manage"))):
    data = await _json(request)
    box = _run(db, loc.get_box, db, box_id)
    out = _run(db, loc.set_box_status, db, box, bool(data.get("active")), then=str(data.get("then") or ""),
               move_to_box=data.get("move_to_box"), user=user, ip_address=client_ip(request))
    return {**out, "boxes": loc.list_boxes(db, box.rack)}


# --------------------------------------------------------------------------- assignment
@router.post("/api/erp/locations/assign")
async def assign(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.assign"))):
    """Put products in a rack / box (``rack_id`` null = unassign). One request whatever the count:
    requested · processed · skipped (already there) · failed (with the reason for each)."""
    data = await _json(request)
    ids = data.get("item_ids")
    if isinstance(ids, list) and len(ids) > 1:
        _need(user, "rack.bulk_move")
    source = "BULK" if isinstance(ids, list) and len(ids) > 1 else "MANUAL"
    return _run(db, loc.assign, db, ids, data.get("rack_id"), data.get("box_id"), reason=str(data.get("reason") or ""),
                source=source, expected=data.get("expected"), atomic=bool(data.get("atomic")), user=user, ip_address=client_ip(request))


@router.get("/api/erp/inventory/{item_id}/location")
def item_location(item_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.view"))):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Product not found")
    here = loc.current(db, [item.id]).get(item.id)
    return {"current": here.as_dict() if here else None,
            "suggested": None if here else loc.suggest(db, [item]).get(item.id),
            "history": loc.item_history(db, item.id) if has_permission(user, "rack.history.view") else []}


# --------------------------------------------------------------------------- settings
@router.get("/api/erp/locations/settings")
def location_settings(db: Session = Depends(get_db), user: User = Depends(require_permission("rack.view"))):
    return {"config": loc.config(db), "labels": {k: v[1] for k, v in loc.SETTINGS.items()},
            "categories": [{"code": c.code, "name": c.name or c.code.title(), "default_rack_id": c.default_rack_id}
                           for c in db.scalars(select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order, Category.code))]}


@router.put("/api/erp/locations/settings")
async def location_settings_save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("rack.edit"))):
    data = await _json(request)
    return {"config": _run(db, loc.set_config, db, data, user=user, ip_address=client_ip(request))}
