"""ERP workspace: the single-page shell (``/app``) and its JSON APIs.

The shell hosts keyboard-first modules (POS, Inventory, ledger …) in
workspace tabs. Everything here reads and writes through the same services
as the rest of the app: stock only ever changes via the inventory ledger.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import client_ip, require_login, require_permission
from app.models import Batch, Item, User
from app.permissions import has_permission
from app.routing import OffloadRoute
from app.services import search, category_service, keymap_service, parking_service, settings_service, units
from app.services import inventory_service as inv, inventory_pricing
from app.services.settings_service import get_int
from app.web import render

router = APIRouter(route_class=OffloadRoute)

MODULE_PERMS = {
    "pos": "sales.create", "inventory": "inventory.view", "history": "inventory.view", "adjustments": "inventory.view",
    "purchases": "purchase.view", "customers": "customers.view",
    "sales": "sales.view_own", "reports": "reports.sales", "masters": "inventory.view",
    "settings": "settings.manage",
}
CAPABILITIES = (
    "sales.create", "sales.discount", "sales.void", "sales.refund", "sales.view_history", "inventory.view", "inventory.create",
    "inventory.edit", "inventory.export", "adjustment.create", "purchase.view", "purchase.create", "purchase.post",
    "purchase.return", "supplier.manage", "reports.sales", "reports.export",
    "customers.create", "customers.view", "customers.edit", "followups.manage",
    "whatsapp.send", "settings.manage",
)


# --------------------------------------------------------------------------- shell
def _max_discount(db: Session):
    from app.services.sales_service import max_discount_pct

    return max_discount_pct(db)


def _boot(request: Request, db: Session, user: User) -> dict:
    profile = settings_service.get_profile(db)
    return {
        "user": {"name": user.full_name or user.username, "username": user.username,
                 "employee_id": getattr(user, "employee_id", "") or ""},
        "can": {code: has_permission(user, code) for code in CAPABILITIES},
        "modules": [m for m, perm in MODULE_PERMS.items() if has_permission(user, perm)],
        "pharmacy": profile.get("name") if isinstance(profile, dict) else getattr(profile, "name", ""),
        "settings": {
            "round_off_mode": settings_service.get_setting(db, "round_off_mode", "NEAREST_RUPEE"),
            "expiry_threshold_days": get_int(db, "expiry_threshold_days", 90),
            "low_stock_threshold": get_int(db, "low_stock_threshold", 5),
            "max_discount_pct": float(_max_discount(db)),
        },
        "units": {"base": units.BASE_UNITS, "pack": units.PACK_UNITS, "forms": units.DOSAGE_FORMS},
        "keymap": keymap_service.payload(db, user),
        "categories": inv.categories(db),
        "category_options": category_service.options(db),
    }


@router.get("/app")
@router.get("/app/{rest:path}")
def erp_shell(request: Request, rest: str = "", db: Session = Depends(get_db), user: User = Depends(require_login)):
    return render(request, "erp.html", db, {"boot": _boot(request, db, user)}, user=user)


# --------------------------------------------------------------------------- supplier list (filters)
# --------------------------------------------------------------------------- category master
def _cat_call(db: Session, fn, *args, **kwargs):
    try:
        out = fn(db, *args, **kwargs)
        db.commit()
        return out
    except category_service.CategoryError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    except IntegrityError as exc:  # a database rule refused it (e.g. category still in use)
        db.rollback()
        raise HTTPException(400, str(exc.orig).split("\n")[0])


@router.get("/api/erp/categories")
def erp_categories(db: Session = Depends(get_db), user: User = Depends(require_permission("inventory.view"))):
    return {"categories": category_service.listing(db)}


@router.post("/api/erp/categories")
async def erp_category_create(request: Request, db: Session = Depends(get_db),
                              user: User = Depends(require_permission("inventory.edit"))):
    data = await request.json()
    cat = _cat_call(db, category_service.create, data.get("name", ""), user=user)
    return {"code": cat.code, "categories": category_service.listing(db)}


@router.put("/api/erp/categories/{code}")
async def erp_category_update(code: str, request: Request, db: Session = Depends(get_db),
                              user: User = Depends(require_permission("inventory.edit"))):
    data = await request.json()
    if "move" in data:
        _cat_call(db, category_service.move, code, int(data["move"]), user=user)
    else:
        active = data.get("active")
        _cat_call(db, category_service.update, code, name=data.get("name"),
                  active=None if active is None else bool(active), user=user)
    return {"categories": category_service.listing(db)}


@router.post("/api/erp/categories/{code}/merge")
async def erp_category_merge(code: str, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_permission("inventory.edit"))):
    data = await request.json()
    moved = _cat_call(db, category_service.merge, code, str(data.get("into") or ""), user=user)
    return {"moved": moved, "categories": category_service.listing(db)}


@router.delete("/api/erp/categories/{code}")
def erp_category_delete(code: str, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("inventory.edit"))):
    _cat_call(db, category_service.delete, code, user=user)
    return {"categories": category_service.listing(db)}


# --------------------------------------------------------------------------- keyboard shortcuts
@router.get("/api/erp/keymap")
def erp_keymap(db: Session = Depends(get_db), user: User = Depends(require_login)):
    return keymap_service.payload(db, user)


@router.put("/api/erp/keymap")
async def erp_keymap_save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    """Save this user's shortcut overrides (``{"overrides": {action_id: key or ""}}``).

    Every key is re-validated here: browser-reserved keys, typing keys and
    clashes with another action are refused with a reason per action."""
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected a JSON object")
    if not isinstance(data, dict) or "overrides" not in data:
        raise HTTPException(400, "Expected an overrides object")
    overrides = data["overrides"]
    if not isinstance(overrides, dict):
        raise HTTPException(400, "overrides must be an object")
    if any(not isinstance(v, str) for v in overrides.values()):
        raise HTTPException(400, "Shortcut values must be strings")
    try:
        clean = keymap_service.save(db, user, overrides)
        db.commit()
    except ValueError as exc:
        db.rollback()
        errors = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {"": str(exc)}
        raise HTTPException(400, {"message": next(iter(errors.values())), "errors": errors})
    return {"overrides": clean}


# --------------------------------------------------------------------------- inventory grid
def _month_start(today: date) -> date:
    return today.replace(day=1)


def _stock_view(db: Session, today: date):
    """Per-product stock aggregates over batches that hold stock."""
    return (
        select(
            Batch.item_id.label("item_id"),
            func.sum(Batch.quantity).label("stock"),
            func.min(Batch.expiry_date).label("first_expiry"),
            func.sum(case((Batch.expiry_date < _month_start(today), Batch.quantity), else_=0)).label("expired"),
        )
        .where(Batch.quantity > 0)
        .group_by(Batch.item_id)
        .subquery()
    )


def _status(stock: int, expired: int, first_expiry: date | None, reorder: int, today: date, threshold_days: int) -> str:
    if stock <= 0:
        return "OUT"
    if expired:
        return "EXPIRED"
    if reorder and stock <= reorder:
        return "LOW"
    if first_expiry and first_expiry <= today + timedelta(days=threshold_days):
        return "EXPIRING"
    return "OK"


def _row(item: Item, stock: int, expired: int, first_expiry: date | None, low: int, today: date, days: int) -> dict:
    reorder = item.reorder_level or 0
    return {
        "id": item.id, "code": item.article_id, "name": item.name, "generic": item.generic_name,
        "manufacturer": item.manufacturer, "form": item.dosage_form or "", "strength": item.strength,
        "category": item.category,
        "pack_raw": item.pack_size, "upp": item.units_per_pack or 1, "base_unit": item.base_unit,
        "pack_unit": item.pack_unit, "loose": bool(item.loose_sale), "stock": stock,
        "equivalent": inv.describe_stock(item, stock), "reorder": reorder, "rack": item.rack or "",
        "first_expiry": first_expiry.isoformat() if first_expiry else "",
        "status": _status(stock, expired, first_expiry, reorder or low, today, days),
        "active": bool(item.is_active),
    }


@router.get("/api/erp/inventory")
def erp_inventory(
    q: str = "", form: str = "", loose: str = "", stock: str = "", expiry: str = "",
    category: str = "", supplier: str = "",
    offset: int = 0, limit: int = 200,
    db: Session = Depends(get_db), user: User = Depends(require_permission("inventory.view")),
):
    today = date.today()
    days = get_int(db, "expiry_threshold_days", 90)
    low = get_int(db, "low_stock_threshold", 5)
    sv = _stock_view(db, today)
    qty = func.coalesce(sv.c.stock, 0)
    reorder = func.coalesce(func.nullif(Item.reorder_level, 0), low)
    conds = [Item.deleted_at.is_(None)]
    if q.strip():
        term = q.strip()
        ids = search.product_ids(db, term, 5000)
        like = f"%{term}%"
        conds.append(or_(Item.id.in_(ids), Item.name.ilike(like), Item.article_id.ilike(like),
                         Item.barcode == term, Item.generic_name.ilike(like)))
    if form:
        conds.append(Item.dosage_form == form.upper())
    if loose in ("yes", "no"):
        conds.append(Item.loose_sale.is_(loose == "yes"))
    if category:
        conds.append(Item.category == category)
    if supplier.strip().isdigit():
        conds.append(Item.id.in_(select(Batch.item_id).where(Batch.supplier_id == int(supplier))))
    if stock == "in":
        conds.append(qty > 0)
    elif stock == "out":
        conds.append(qty <= 0)
    elif stock == "low":
        conds.append(and_(qty > 0, qty <= reorder))
    if expiry == "expired":
        conds.append(func.coalesce(sv.c.expired, 0) > 0)
    elif expiry == "expiring":
        conds.append(and_(sv.c.first_expiry >= _month_start(today), sv.c.first_expiry <= today + timedelta(days=days)))
    base = select(Item, qty, func.coalesce(sv.c.expired, 0), sv.c.first_expiry).outerjoin(sv, sv.c.item_id == Item.id).where(*conds)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.options(selectinload(Item.batches)).order_by(Item.name.asc(), Item.id.asc()).offset(max(offset, 0)).limit(min(max(limit, 1), 500))).all()
    cat_names = category_service.names(db)
    prices = inventory_pricing.batch_prices(db, [b for it, *_ in rows for b in it.batches]) if has_permission(user, "purchase.view") else {}
    output = []
    for it, s, e, fe in rows:
        row = _row(it, int(s or 0), int(e or 0), fe, low, today, days)
        row["category_name"] = cat_names.get(it.category, it.category)
        active = [b for b in it.batches if b.quantity > 0] or list(it.batches)
        mrps = {b.mrp for b in active}
        row["pack_mrp"] = next(iter(mrps)) if len(mrps) == 1 else None if mrps else it.mrp
        row["pricing_varies"] = ["pack_mrp"] if len(mrps) > 1 else []
        if has_permission(user, "purchase.view"):
            row.update(_price_json(inventory_pricing.product_prices(it.batches, prices)))
        if not it.batches:
            row["pack_mrp"] = str(it.mrp)
        output.append(row)
    return {"total": total, "offset": offset, "rows": output}


def _batch_status(b: Batch, today: date, days: int) -> str:
    if b.quantity <= 0:
        return "Empty"
    if units.is_expired(b.expiry_date, today):
        return "Expired"
    if b.expiry_date and b.expiry_date <= today + timedelta(days=days):
        return "Near expiry"
    return "Active"


def _price_json(prices):
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in prices.items()}


def _detail(db: Session, item: Item, user: User | None = None) -> dict:
    today = date.today()
    days = get_int(db, "expiry_threshold_days", 90)
    low = get_int(db, "low_stock_threshold", 5)
    batches = sorted(item.batches, key=lambda b: (b.quantity <= 0, b.expiry_date is None, b.expiry_date or date.max, b.id))
    stock = sum(b.quantity for b in batches if b.quantity > 0)
    expired = sum(b.quantity for b in batches if b.quantity > 0 and units.is_expired(b.expiry_date, today))
    first = min((b.expiry_date for b in batches if b.quantity > 0 and b.expiry_date), default=None)
    row = _row(item, stock, expired, first, low, today, days)
    row.update({
        "category": item.category, "category_name": category_service.names(db).get(item.category, item.category),
        "hsn": item.hsn_code, "barcode": item.barcode, "mrp": str(item.mrp),
        "content_qty": str(item.content_qty) if item.content_qty is not None else "",
        "content_unit": item.content_unit or "", "sellable": stock - expired,
        "packaging": inv.packaging_view(item),
        # stock counted per pack can still be repacked into units; otherwise packaging is fixed while stock exists
        "packaging_locked": stock != 0 and (item.units_per_pack or 1) > 1,
        "packaging_convertible": stock != 0 and (item.units_per_pack or 1) == 1,
        "batches": [
            {
                "id": b.id, "batch_no": b.batch_no, "expiry": b.expiry_date.isoformat() if b.expiry_date else "",
                "pack_mrp": str(b.mrp), "unit_mrp": str(units.display_unit_price(b.mrp, b.units_per_pack or 1)),
                "upp": b.units_per_pack or 1, "stock": b.quantity,
                "equivalent": units.describe_stock(b.quantity, b.units_per_pack or 1, item.base_unit, item.pack_unit),
                "status": _batch_status(b, today, days),
            }
            for b in batches
        ],
    })
    _provenance(db, batches, row["batches"])
    if user is not None and has_permission(user, "purchase.view"):
        prices = inventory_pricing.batch_prices(db, batches)
        row.update(_price_json(inventory_pricing.product_prices(batches, prices)))
        if not batches:
            row["pack_mrp"] = str(item.mrp)
        for batch in row["batches"]:
            batch.update(_price_json(prices[batch["id"]]))
            original = next(b for b in batches if b.id == batch["id"])
            batch["acquisition_rate"] = str(original.purchase_rate)
            batch["cost_status"] = original.cost_status
    return row


def _provenance(db: Session, batches: list[Batch], rows: list[dict]) -> None:
    """Where each batch came from: supplier, supplier invoice / PUR number, first receipt."""
    from app.models import InventoryMovement, Purchase

    ids = [b.id for b in batches]
    if not ids:
        return
    received = dict(db.execute(select(InventoryMovement.batch_id, func.min(InventoryMovement.created_at))
                               .where(InventoryMovement.batch_id.in_(ids), InventoryMovement.quantity > 0)
                               .group_by(InventoryMovement.batch_id)).all())
    pids = {b.purchase_id for b in batches if b.purchase_id}
    docs = {p.id: p for p in db.scalars(select(Purchase).where(Purchase.id.in_(pids)))} if pids else {}
    by_id = {b.id: b for b in batches}
    for row in rows:
        b = by_id[row["id"]]
        doc = docs.get(b.purchase_id)
        row.update({
            "supplier": b.supplier.name if b.supplier else "",
            "invoice": doc.invoice_no if doc else "", "purchase_ref": (doc.reference_no or "") if doc else "",
            "purchase_id": b.purchase_id, "received": received[b.id].isoformat() if b.id in received else "",
        })


def _item_or_404(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None or item.deleted_at is not None:
        raise HTTPException(404, "Product not found")
    return item


@router.get("/api/erp/inventory/{item_id}")
def erp_inventory_detail(item_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("inventory.view"))):
    return _detail(db, _item_or_404(db, item_id), user)


# --------------------------------------------------------------------------- adjustments
def _qty(value: Any, item: Item) -> int:
    try:
        qty = units.parse_qty_expression(value, item.units_per_pack or 1)
    except units.UnitError as exc:
        raise HTTPException(400, str(exc))
    if qty <= 0:
        raise HTTPException(400, "Quantity must be above zero")
    return qty


def _money(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value or "0").strip() or "0")
    except InvalidOperation:
        raise HTTPException(400, f"{label} must be a number")
    if amount < 0:
        raise HTTPException(400, f"{label} cannot be negative")
    return amount


@router.post("/api/erp/inventory/{item_id}/adjust")
async def erp_adjust(item_id: int, request: Request, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("adjustment.create"))):
    """Stock adjustment as a ledger transaction (never a direct overwrite).

    ``direction`` IN/OUT, ``quantity`` in base units (``2s+3`` accepted), a
    mandatory ``reason``; OUT also takes a loss ``category``. IN without a
    ``batch_id`` receives into a new or matching batch (``batch_no``,
    ``expiry``, ``mrp``) — e.g. opening stock for a product.
    """
    item = _item_or_404(db, item_id)
    data = await request.json()
    direction = str(data.get("direction") or "").upper()
    reason = " ".join(str(data.get("reason") or "").split())[:300]
    if direction not in ("IN", "OUT"):
        raise HTTPException(400, "Choose Increase or Decrease")
    if not reason:
        raise HTTPException(400, "A reason is required for every stock adjustment")
    qty = _qty(data.get("quantity"), item)
    batch_id = int(data.get("batch_id") or 0) or None
    from app.services import adjustment_service

    batch = db.get(Batch, batch_id) if batch_id else None
    if batch_id and (batch is None or batch.item_id != item.id):
        raise HTTPException(400, "Batch not found for this product")
    new_batch = None
    if direction == "IN" and batch is None:
        from app.routers.inventory import parse_date

        expiry_text = str(data.get("expiry") or "").strip()
        expiry = parse_date(expiry_text) if expiry_text else None
        if expiry_text and expiry is None:
            raise HTTPException(400, "Expiry must be MM/YYYY or YYYY-MM-DD")
        new_batch = {"batch_no": data.get("batch_no"), "expiry_date": expiry, "mrp": _money(data.get("mrp"), "MRP"),
                     "cost": _money(data.get("cost"), "Cost"), "opening": bool(data.get("opening"))}
    category = str(data.get("category") or ("COUNT" if direction == "OUT" else "SURPLUS" if batch else "OTHER")).upper()
    try:
        adjustment_service.create(db, item=item, direction=direction, category=category, quantity=qty, reason=reason,
                                  batch=batch, new_batch=new_batch, user=user, ip_address=client_ip(request))
        db.commit()
    except adjustment_service.AdjustmentError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.refresh(item)
    return _detail(db, item, user)


# --------------------------------------------------------------------------- product master
TEXT_FIELDS = {"name": 250, "generic_name": 250, "manufacturer": 150, "strength": 60, "pack_size": 60,
               "hsn_code": 20, "barcode": 60, "rack": 20, "content_unit": 10}
PACK_FIELDS = ("base_unit", "pack_unit", "units_per_pack", "dosage_form")  # loose sale is derived


def _fields(data: dict) -> dict:
    out: dict[str, Any] = {k: " ".join(str(data[k]).split())[:n] for k, n in TEXT_FIELDS.items()
                           if k in data and data[k] is not None}
    if "category" in data and data["category"]:
        out["category"] = str(data["category"]).upper()
    for k in PACK_FIELDS:
        if k in data and data[k] is not None and data[k] != "":
            out[k] = data[k]
    if "reorder_level" in data and str(data["reorder_level"]).strip() != "":
        try:
            out["reorder_level"] = max(int(str(data["reorder_level"]).strip()), 0)
        except ValueError:
            raise HTTPException(400, "Reorder level must be a whole number")
    if "mrp" in data and str(data["mrp"]).strip() != "":
        out["mrp"] = _money(data["mrp"], "MRP")
    return out


@router.post("/api/erp/inventory")
async def erp_create_item(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("inventory.create"))):
    fields = _fields(await request.json())
    name = fields.pop("name", "")
    if not name:
        raise HTTPException(400, "Product name is required")
    try:
        item = inv.create_item(db, name=name, user=user, ip_address=client_ip(request), **fields)
        db.commit()
    except inv.InventoryError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return _detail(db, item, user)


@router.put("/api/erp/inventory/{item_id}")
async def erp_update_item(item_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("inventory.edit"))):
    item = _item_or_404(db, item_id)
    fields = _fields(await request.json())
    if "name" in fields and not fields["name"]:
        raise HTTPException(400, "Product name is required")
    if fields.get("barcode"):
        clash = db.scalar(select(Item).where(Item.barcode == fields["barcode"], Item.id != item.id,
                                             Item.deleted_at.is_(None)))
        if clash:
            raise HTTPException(400, f"Barcode is already used by {clash.name}")
    try:
        inv.update_item(db, item, user=user, ip_address=client_ip(request), **fields)
        db.commit()
    except inv.InventoryError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.refresh(item)
    return _detail(db, item, user)


# --------------------------------------------------------------------------- POS helpers
@router.get("/api/erp/pos/parked")
def erp_parked(db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    rows = parking_service.list_parked(db, status=("PARKED",))
    return {"parked": [parking_service.parked_payload(db, p) | {"payload": p.payload} for p in rows]}


def _search_ids(db: Session, term: str, limit: int) -> list[int]:
    """Candidate product ids: exact code/barcode, FTS prefix match, then LIKE."""
    exact = list(db.scalars(select(Item.id).where(
        Item.deleted_at.is_(None), Item.is_active.is_(True),
        or_(Item.barcode == term, func.upper(Item.article_id) == term.upper()))))
    if exact:
        return exact
    ids = search.product_ids(db, term, 300, ranked=True)
    if not ids:
        like = f"%{term}%"
        ids = list(db.scalars(select(Item.id).where(
            or_(Item.name.ilike(like), Item.generic_name.ilike(like))).limit(300)))
    return ids


@router.get("/api/erp/pos/search")
def erp_pos_search(q: str = "", limit: int = 15, db: Session = Depends(get_db),
                   user: User = Depends(require_permission("sales.create"))):
    """POS search: products with sellable batches (FEFO), MRPs, never cost.

    Two indexed queries whatever the catalogue size: candidate products, then
    all their in-stock batches in one round trip.
    """
    term = q.strip()
    if not term:
        return {"items": []}
    today = date.today()
    ids = _search_ids(db, term, limit)
    if not ids:
        return {"items": []}
    items = list(db.scalars(select(Item).where(
        Item.id.in_(ids), Item.deleted_at.is_(None), Item.is_active.is_(True))))
    by_item: dict[int, list[Batch]] = {}
    for b in db.scalars(select(Batch).where(Batch.item_id.in_([i.id for i in items]), Batch.quantity > 0)
                        .order_by(Batch.expiry_date.asc().nulls_last(), Batch.id.asc())):
        if not units.is_expired(b.expiry_date, today):
            by_item.setdefault(b.item_id, []).append(b)
    low = term.lower()

    def rank(it: Item) -> tuple:
        name = it.name.lower()
        tier = 0 if name.startswith(low) else 1 if any(w.startswith(low) for w in name.replace("-", " ").split()) else 2
        return (not by_item.get(it.id), tier, name)

    ranked = sorted(items, key=rank)[:min(max(limit, 1), 40)]
    return {"items": [_pos_item(it, by_item.get(it.id, [])) for it in ranked]}


def _pos_item(it: Item, batches: list[Batch]) -> dict:
    """What the POS needs about one product: unit of measure, sellable batches, MRPs. Never cost."""
    stock = sum(b.quantity for b in batches)
    pv = inv.packaging_view(it)
    return {
        "id": it.id, "code": it.article_id, "name": it.name, "generic": it.generic_name,
        "manufacturer": it.manufacturer, "pack_raw": it.pack_size, "upp": it.units_per_pack or 1,
        "base_unit": it.base_unit, "pack_unit": it.pack_unit, "loose": bool(it.loose_sale),
        "form": it.dosage_form, "rack": it.rack or "", "stock": stock,
        "stock_label": inv.describe_stock(it, stock), "content": pv["content"],
        "batches": [{"id": b.id, "batch_no": b.batch_no, "expiry": b.expiry_date.isoformat() if b.expiry_date else "",
                     "stock": b.quantity, "pack_mrp": str(b.mrp), "upp": b.units_per_pack or 1,
                     "unit_mrp": str(units.display_unit_price(b.mrp, b.units_per_pack or 1))} for b in batches],
    }


@router.get("/api/erp/products")
def erp_products(q: str = "", limit: int = 20, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("inventory.view"))):
    """Product lookup for back-office pickers (every product, stocked or not)."""
    term = q.strip()
    if not term:
        return {"items": []}
    ids = _search_ids(db, term, min(max(limit, 1), 40))
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids), Item.deleted_at.is_(None)))}
    return {"items": [{"id": i.id, "code": i.article_id, "name": i.name, "pack": i.pack_size, "manufacturer": i.manufacturer,
                       "upp": i.units_per_pack or 1, "base_unit": i.base_unit, "pack_unit": i.pack_unit, "active": bool(i.is_active)}
                      for i in (items[x] for x in ids if x in items)]}


# --------------------------------------------------------------------------- UOM register (masters)
@router.get("/api/erp/uom")
def erp_uom_register(q: str = "", scope: str = "all", offset: int = 0, limit: int = 1000,
                     db: Session = Depends(get_db), user: User = Depends(require_permission("inventory.view"))):
    """Every product's unit of measure: sale/base unit, purchase unit, conversion, content,
    derived loose sale, and whether it was detected (AUTO) or corrected by a user (MANUAL).

    ``scope``: all · loose · whole · manual · undetected (no usable pack text: sold per unit).
    """
    from app.services import packaging_service

    stmt = select(Item).where(Item.deleted_at.is_(None))
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Item.name.ilike(like), Item.article_id.ilike(like), Item.pack_size.ilike(like)))
    if scope == "loose":
        stmt = stmt.where(Item.loose_sale.is_(True))
    elif scope == "whole":
        stmt = stmt.where(Item.loose_sale.is_(False))
    elif scope == "manual":
        stmt = stmt.where(Item.packaging_source == "MANUAL")
    stock = dict(db.execute(select(Batch.item_id, func.sum(Batch.quantity)).group_by(Batch.item_id)).all())
    rows = []
    for it in db.scalars(stmt.order_by(Item.name)):
        detected = packaging_service.resolve(it)
        if scope == "undetected" and (detected is not None or it.packaging_source == "MANUAL"):
            continue
        pv = inv.packaging_view(it)
        qty = int(stock.get(it.id) or 0)
        rows.append({
            "id": it.id, "code": it.article_id, "name": it.name, "pack_raw": it.pack_size, "form": it.dosage_form,
            "base_unit": it.base_unit, "pack_unit": it.pack_unit, "upp": it.units_per_pack or 1,
            "loose": bool(it.loose_sale), "content": pv["content"], "source": it.packaging_source,
            "stock": qty, "stock_label": inv.describe_stock(it, qty),
            "detected": detected.as_dict() if detected else None,
        })
    return {"total": len(rows), "rows": rows[max(offset, 0): max(offset, 0) + min(max(limit, 1), 5000)]}


@router.get("/api/erp/customers")
def erp_customers(q: str = "", db: Session = Depends(get_db),
                  user: User = Depends(require_permission("sales.create"))):
    """POS customer lookup: digits search mobiles, text searches name / ID / doctor."""
    from app.services import customer_service

    found = customer_service.lookup(db, q)
    return {"customers": [{"id": c.id, "customer_id": c.customer_id, "name": c.name, "mobile": c.mobile,
                           "type": c.customer_type, "doctor": c.doctor_name} for c in found]}
