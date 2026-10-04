"""Inventory API used by the ERP workspace: opening-stock sheet import, stock
export, and per-product ledger / batch lookups. Product and batch screens are
in the ERP Inventory module (app/routers/erp.py)."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.database import get_db
from app.deps import client_ip, require_login, require_permission
from app.models import Batch, Item, User
from app.routing import OffloadRoute
from app.services import inventory_service as inv
from app.services import stock_ledger, units
from app.services.settings_service import get_int

router = APIRouter(tags=["inventory"], route_class=OffloadRoute)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%Y", "%Y-%m"):
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return dt.date()
        except ValueError:
            continue
    return None


@router.get("/api/erp/inventory/import/template.csv")
def item_import_template(user: User = Depends(require_permission("inventory.create"))):
    sample = ["DOLO 650MG TAB 15'S", "Paracetamol 650mg", "Micro Labs", "PHARMA", "Tablet", "15S", "650mg",
              "Tablet", "Strip", "15", "Y", "30049099", "8901234567890", "DOBS4401", "02/2030", "44", "6",
              "24.50", "32.10"]
    body = ",".join(inv.IMPORT_TEMPLATE_COLUMNS) + "\r\n" + ",".join(sample) + "\r\n"
    return Response(
        body.encode("utf-8-sig"), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory-import-template.csv"},
    )


@router.post("/api/erp/inventory/import")
async def item_import(request: Request, file: UploadFile = File(...), default_category: str = Form("PHARMA"),
                      db: Session = Depends(get_db), user: User = Depends(require_permission("inventory.create"))):
    """Create products and post opening stock from a CSV / Excel sheet (one result per row)."""
    from app.services import sheet_import

    content = await file.read()
    if not content:
        raise HTTPException(400, "Choose a CSV or Excel file to import")
    try:
        result = inv.import_items(db, file.filename or "stock.csv", content, default_category=default_category,
                                  user=user, ip_address=client_ip(request))
        db.commit()
    except (sheet_import.SheetError, inv.InventoryError) as exc:
        db.rollback()
        raise HTTPException(400, f"{file.filename}: {exc}")
    return result


@router.get("/inventory/export/{fmt}")
def export_inventory(
    fmt: str,
    request: Request,
    q: str = "",
    category: str = "",
    manufacturer: str = "",
    supplier_id: str = "",
    expiry: str = "",
    stock: str = "",
    cost_min: str = "",
    cost_max: str = "",
    mrp_min: str = "",
    mrp_max: str = "",
    sort: str = "name_asc",
    active: str = "",
    bin: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("inventory.export")),
):
    def _num(v):
        try:
            return float(v) if str(v).strip() else None
        except (TypeError, ValueError):
            return None

    threshold = get_int(db, "expiry_threshold_days", 90)
    # the same filters as the list on screen, so the file matches what you see
    items = inv.all_items_for_export(
        db,
        q=q,
        category=category,
        manufacturer=manufacturer,
        supplier_id=int(supplier_id) if supplier_id.strip().isdigit() else None,
        expiry_filter=expiry,
        stock_filter=stock,
        cost_min=_num(cost_min),
        cost_max=_num(cost_max),
        mrp_min=_num(mrp_min),
        mrp_max=_num(mrp_max),
        sort=sort,
        low_threshold=get_int(db, "low_stock_threshold", 5),
        threshold_days=threshold,
        active_only=(active != "all") and bin != "1",
        deleted_only=bin == "1",
    )
    audit.record(
        db,
        action=audit.A_EXPORT,
        entity_type="inventory",
        entity_id=fmt,
        user=user,
        details=f"Exported {len(items)} items",
        ip_address=client_ip(request),
        commit=True,
    )
    if fmt == "csv":
        return Response(
            inv.export_csv(db, items),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=inventory.csv"},
        )
    if fmt == "xlsx":
        return Response(
            inv.export_xlsx(db, items),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=inventory.xlsx"},
        )
    raise HTTPException(400, "Unsupported format")


def _usable_batches(item: Item, today: date) -> list[Batch]:
    """In-stock, non-expired batches ordered FEFO (soonest expiry first)."""
    batches = [b for b in item.batches if b.quantity > 0 and not units.is_expired(b.expiry_date, today)]
    return sorted(batches, key=lambda b: (b.expiry_date is None, b.expiry_date or date.max, b.id))


def _item_payload(item: Item, today: date, rack: str = "") -> dict:
    """What the counter needs: packaging, sellable batches, MRPs, where it is kept. Never cost."""
    batches = _usable_batches(item, today)
    stock = sum(b.quantity for b in batches)
    return {
        "rack": rack,
        "active": bool(item.is_active),
        "id": item.id,
        "article_id": item.article_id,
        "name": item.name,
        "generic_name": item.generic_name,
        "manufacturer": item.manufacturer,
        "category": item.category,
        "pack_size": item.pack_size,
        "unit": item.unit,
        "barcode": item.barcode,
        "mrp": str(item.mrp),
        **inv.packaging_view(item),
        "stock": stock,
        "stock_label": inv.describe_stock(item, stock),
        "batches": [
            {
                "id": b.id,
                "batch_no": b.batch_no,
                "expiry": b.expiry_date.isoformat() if b.expiry_date else "",
                "quantity": b.quantity,
                "mrp": str(b.mrp),
                "units_per_pack": b.units_per_pack or 1,
                "unit_mrp": str(units.display_unit_price(b.mrp, b.units_per_pack or 1)),
            }
            for b in batches
        ],
    }


@router.get("/api/items/search")
def api_item_search(
    q: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    q = q.strip()
    if not q:
        return {"items": []}
    today = date.today()
    # Exact barcode / article-id scan resolves to a single product immediately.
    exact = db.scalar(
        select(Item).where(
            Item.is_active.is_(True),
            (Item.barcode == q) | (Item.article_id == q),
        )
    )
    if exact is not None:
        return {"items": [_item_payload(exact, today)], "exact": True}
    items, _ = inv.search_items(db, q=q, limit=150)
    payloads = [_item_payload(it, today) for it in items]
    return {"items": sorted(payloads, key=lambda p: _rank(p, q))[:12]}


def _rank(payload: dict, q: str) -> tuple:
    """Best match first: name starts with the text, then a word starts with it, then contains it;
    in-stock before out-of-stock; then alphabetical."""
    name, text = payload["name"].lower(), q.lower()
    compact = name.replace(" ", "").replace("-", "")
    if name.startswith(text) or compact.startswith(text.replace(" ", "")):
        tier = 0
    elif any(w.startswith(text) for w in name.replace("-", " ").split()):
        tier = 1
    elif text in name:
        tier = 2
    else:
        tier = 3
    return (tier, payload["stock"] <= 0, name)


@router.get("/api/items/{item_id}/ledger")
def api_item_ledger(
    item_id: int,
    batch_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("inventory.view")),
):
    """Stock movement history (newest first) for a product or one batch."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    rows = stock_ledger.movements(db, item_id=item.id, batch_id=batch_id, limit=min(max(limit, 1), 1000))
    return {
        "item_id": item.id,
        "base_unit": item.base_unit,
        "movements": [
            {
                "id": m.id, "at": m.created_at.isoformat(), "type": m.movement_type,
                "label": stock_ledger.MOVEMENT_LABELS.get(m.movement_type, m.movement_type),
                "batch_id": m.batch_id, "batch_no": m.batch.batch_no if m.batch else "",
                "in": m.quantity if m.quantity > 0 else 0, "out": -m.quantity if m.quantity < 0 else 0,
                "balance": m.balance_after, "reference": m.reference_no, "reference_type": m.reference_type,
                "txn": f"{m.txn_quantity} {m.txn_unit}" if m.txn_unit == "PACK" else "",
                "reason": m.reason, "reversal_of": m.reversal_of_id,
                "user": m.user.username if m.user else "",
            }
            for m in rows
        ],
    }


@router.get("/api/items/{item_id}/batches")
def api_item_batches(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    from app.services import location_service

    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    here = location_service.current(db, [item.id]).get(item.id)
    return _item_payload(item, date.today(), here.short if here else "")


