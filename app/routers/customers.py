"""Customers module API: directory, customer record, invoices, activity and
follow-ups (Inbox / Calendar read the same follow-up records). Invoice data
comes from the canonical sales rows used by Sales History."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_permission
from app.models import FOLLOWUP_REASONS, Customer, CustomerFollowUp, SaleReturn, User
from app.routing import OffloadRoute
from app.services import customer_service, followup_service

router = APIRouter(tags=["customers"], route_class=OffloadRoute)


def _customer(db: Session, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if c is None or not c.is_active:
        raise HTTPException(404, "Customer not found")
    return c


def _run(db: Session, fn, *a, **kw):
    try:
        out = fn(db, *a, **kw)
        db.commit()
        return out
    except (customer_service.CustomerError, followup_service.FollowUpError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc))


@router.get("/api/erp/customers/directory")
def directory(q: str = "", sort: str = "recent", limit: int = 100, offset: int = 0, db: Session = Depends(get_db),
              user: User = Depends(require_permission("customers.view"))):
    rows, total = customer_service.directory(db, q, sort=sort if sort in customer_service.DIRECTORY_SORTS else "recent",
                                             limit=limit, offset=offset)
    return {"total": total, "customers": rows, "followups": followup_service.counts(db)}


@router.get("/api/erp/customers/{customer_id}")
def customer_record(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("customers.view"))):
    return customer_service.record(db, _customer(db, customer_id))


@router.put("/api/erp/customers/{customer_id}")
async def customer_update(customer_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("customers.edit"))):
    c = _customer(db, customer_id)
    _run(db, customer_service.edit_customer, c, await request.json(), user=user)
    return customer_service.record(db, c)


@router.post("/api/erp/customers/{customer_id}/notes")
async def customer_note(customer_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("customers.view"))):
    c = _customer(db, customer_id)
    data = await request.json()
    _run(db, customer_service.add_note, c, data.get("text", ""), user=user)
    return customer_service.record(db, c)


@router.get("/api/erp/customers/{customer_id}/invoices")
def customer_invoices(customer_id: int, limit: int = 300, offset: int = 0, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("customers.view"))):
    from app.routers.sales_history import _row

    c = _customer(db, customer_id)
    rows, total = customer_service.invoices(db, c, limit=limit, offset=offset)
    returned = {sid for (sid,) in db.execute(select(SaleReturn.sale_id).where(SaleReturn.sale_id.in_([s.id for s in rows])))} if rows else set()
    refunds = dict(db.execute(select(SaleReturn.sale_id, SaleReturn.total_refund).where(
        SaleReturn.sale_id.in_(returned), SaleReturn.status == "COMPLETED")).all()) if returned else {}
    out = []
    for s in rows:
        r = _row(s, returned)
        gross = sum(((i.line_total or 0) + (i.discount or 0) for i in s.items), 0)
        r.update({"gross": str(round(gross, 2)), "refunded": str(refunds.get(s.id, 0))})
        out.append(r)
    return {"total": total, "invoices": out}


@router.get("/api/erp/customers/{customer_id}/activity")
def customer_activity(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("customers.view"))):
    return {"activity": customer_service.activity(db, _customer(db, customer_id))}


# --------------------------------------------------------------------------- follow-ups
@router.get("/api/erp/followups")
def followups(view: str = "open", start: str = "", end: str = "", customer: int | None = None, q: str = "",
              db: Session = Depends(get_db), user: User = Depends(require_permission("customers.view"))):
    if view not in followup_service.VIEWS:
        raise HTTPException(400, "Unknown view")
    try:
        rows = followup_service.listing(db, view=view, start=start or None, end=end or None, customer_id=customer, q=q)
    except followup_service.FollowUpError as exc:
        raise HTTPException(400, str(exc))
    return {"followups": followup_service.payloads(db, rows), "counts": followup_service.counts(db),
            "reasons": [{"code": k, "label": v} for k, v in FOLLOWUP_REASONS.items()]}


@router.post("/api/erp/followups")
async def followup_create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("followups.manage"))):
    d = await request.json()
    fu = _run(db, followup_service.create, customer_id=d.get("customer_id"), due_date=d.get("due_date"), preset=str(d.get("preset") or ""),
              due_time=d.get("due_time") or "", reason=d.get("reason") or "GENERAL", note=d.get("note") or "",
              contact_method=d.get("contact_method") or "", source_sale_id=d.get("source_sale_id") or None, user=user)
    return {"followup": followup_service.payloads(db, [fu])[0], "counts": followup_service.counts(db)}


def _fu(db: Session, fid: int) -> CustomerFollowUp:
    fu = db.get(CustomerFollowUp, fid)
    if fu is None:
        raise HTTPException(404, "Follow-up not found")
    return fu


@router.post("/api/erp/followups/{fid}/complete")
async def followup_complete(fid: int, request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("followups.manage"))):
    d = await request.json()
    fu = _run(db, followup_service.complete, _fu(db, fid), note=d.get("note") or "", user=user)
    return {"followup": followup_service.payloads(db, [fu])[0], "counts": followup_service.counts(db)}


@router.post("/api/erp/followups/{fid}/reschedule")
async def followup_reschedule(fid: int, request: Request, db: Session = Depends(get_db),
                              user: User = Depends(require_permission("followups.manage"))):
    d = await request.json()
    fu = _run(db, followup_service.reschedule, _fu(db, fid), due_date=d.get("due_date"), preset=str(d.get("preset") or ""),
              due_time=d.get("due_time"), note=d.get("note") or "", user=user)
    return {"followup": followup_service.payloads(db, [fu])[0], "counts": followup_service.counts(db)}


@router.post("/api/erp/followups/{fid}/cancel")
async def followup_cancel(fid: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("followups.manage"))):
    d = await request.json()
    fu = _run(db, followup_service.cancel, _fu(db, fid), note=d.get("note") or "", user=user)
    return {"followup": followup_service.payloads(db, [fu])[0], "counts": followup_service.counts(db)}
