"""Manual Bills API — reference documents only, kept apart from sales (see manual_bill_service)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import ManualBill, User
from app.routing import OffloadRoute
from app.routers.sales import _create_manual_bill, _resolve_bill_customer, export_document, invoice_page, pdf_document
from app.routers.sales_history import _bounds
from app.services import customer_service, manual_bill_service, settings_service
from app.services.sales_service import SaleError

router = APIRouter(tags=["manual-bills"], route_class=OffloadRoute)


def _bill(db: Session, bill_id: int) -> ManualBill:
    bill = manual_bill_service.get(db, bill_id)
    if bill is None:
        raise HTTPException(404, "Manual bill not found")
    return bill


@router.get("/api/erp/manual-bills")
def manual_bills(q: str = "", start: str = "", end: str = "", status: str = "", limit: int = 200, offset: int = 0,
                 db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    lo, hi = _bounds(db, start, end)
    rows, total = manual_bill_service.search(db, q=q, start=lo, end=hi, status=status, limit=limit, offset=offset)
    return {"total": total, "bills": [manual_bill_service.row(b) for b in rows]}


@router.post("/api/erp/manual-bills")
async def manual_bill_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    return _create_manual_bill(db, await request.json(), user, client_ip(request))


@router.get("/api/erp/manual-bills/{bill_id}")
def manual_bill_detail(bill_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    return manual_bill_service.detail(_bill(db, bill_id))


@router.get("/api/erp/manual-bills/{bill_id}/edit")
def manual_bill_edit_payload(bill_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.void"))):
    bill = _bill(db, bill_id)
    if bill.status == "DELETED":
        raise HTTPException(400, "This manual bill was deleted")
    return manual_bill_service.edit_payload(db, bill)


@router.put("/api/erp/manual-bills/{bill_id}")
async def manual_bill_amend(bill_id: int, request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("sales.void"))):
    bill = _bill(db, bill_id)
    payload = await request.json()
    try:
        customer_id = _resolve_bill_customer(db, payload, user, client_ip(request))
        manual_bill_service.amend(
            db, bill, lines=payload.get("lines") or [], user=user, customer_id=customer_id,
            customer_type=payload.get("customer_type", bill.customer_type or "WALK_IN"), payment_mode=payload.get("payment_mode", "CASH"),
            payments=payload.get("payments") or None, discount_pct=payload.get("discount_pct"), notes=payload.get("notes"),
            round_off_mode=settings_service.get_setting(db, "round_off_mode", "NEAREST_RUPEE"),
            cash_received=payload.get("cash_received"), ip_address=client_ip(request))
        db.commit()
    except (SaleError, customer_service.CustomerError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    bill = _bill(db, bill_id)
    return {"ok": True, "manual_bill_id": bill.id, "invoice_no": bill.invoice_no, "total": str(bill.total),
            "tendered": str(bill.tendered_amount) if bill.tendered_amount is not None else None,
            "change": str(bill.change_amount) if bill.change_amount is not None else None,
            "payment_mode": bill.payment_mode, "paid": str(bill.total), "udhaar": None}


@router.post("/api/erp/manual-bills/{bill_id}/delete")
async def manual_bill_delete(bill_id: int, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_permission("sales.void"))):
    data = await request.json()
    try:
        manual_bill_service.delete(db, _bill(db, bill_id), reason=data.get("reason", ""), user=user, ip_address=client_ip(request))
        db.commit()
    except SaleError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return manual_bill_service.detail(_bill(db, bill_id))


@router.get("/manual-bills/{bill_id}/invoice")
def manual_bill_invoice(bill_id: int, request: Request, size: str = "A4", mono: int = 0, expiry: int = 1, autoprint: int = 0,
                        db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    return invoice_page(request, db, _bill(db, bill_id), user, size=size, mono=mono, expiry=expiry, autoprint=autoprint)


@router.get("/manual-bills/{bill_id}/pdf")
def manual_bill_pdf(bill_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    return pdf_document(db, _bill(db, bill_id), user)


@router.get("/api/erp/manual-bills/{bill_id}/export.{fmt}")
def manual_bill_export(bill_id: int, fmt: str, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    return export_document(db, _bill(db, bill_id), fmt, user)
