"""Udhaar Ledger API: what customers owe, repayments, reminders, statements — and the Counter Report."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_permission
from app.models import Customer, UdhaarEntry, User
from app.routing import OffloadRoute
from app.services import business_time, counter_service, udhaar_service
from app.utils import money

router = APIRouter(tags=["udhaar"], route_class=OffloadRoute)


def _customer(db: Session, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if c is None:
        raise HTTPException(404, "Customer not found")
    return c


def _entry(db: Session, eid: int) -> UdhaarEntry:
    e = db.get(UdhaarEntry, eid)
    if e is None:
        raise HTTPException(404, "Udhaar entry not found")
    return e


def _run(db: Session, fn, *a, **kw):
    try:
        out = fn(db, *a, **kw)
        db.commit()
        return out
    except udhaar_service.UdhaarError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))


@router.get("/api/erp/udhaar")
def udhaar_list(q: str = "", view: str = "open", customer_id: int | None = None, db: Session = Depends(get_db),
                user: User = Depends(require_permission("udhaar.view"))):
    if view not in ("open", "overdue", "today", "reminders", "paid", "all"):
        raise HTTPException(400, "Unknown view")
    return udhaar_service.list_entries(db, q=q, view=view, customer_id=customer_id)


@router.get("/api/erp/udhaar/customers/{customer_id}")
def udhaar_customer(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("sales.create"))):
    """The customer's Udhaar position and proposed dates — what the POS shows when Udhaar is chosen."""
    return udhaar_service.customer_summary(db, _customer(db, customer_id))


@router.get("/api/erp/udhaar/customers/{customer_id}/ledger")
def udhaar_ledger(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("udhaar.view"))):
    return udhaar_service.ledger(db, _customer(db, customer_id))


@router.get("/api/erp/udhaar/customers/{customer_id}/statement")
def udhaar_statement(customer_id: int, print: int = 0, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("udhaar.view"))):
    from html import escape

    text = udhaar_service.statement_text(db, _customer(db, customer_id))
    if not print:
        return Response(text, media_type="text/plain; charset=utf-8")
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><title>Udhaar statement</title>"
                        f"<style>body{{margin:12mm}}pre{{font:11px/1.4 'Courier New',monospace}}@page{{margin:10mm}}</style></head>"
                        f"<body><pre>{escape(text)}</pre><script>window.onload=()=>window.print()</script></body></html>")


@router.put("/api/erp/udhaar/customers/{customer_id}/terms")
async def udhaar_terms(customer_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("udhaar.manage"))):
    data = await request.json()
    c = _run(db, udhaar_service.set_terms, _customer(db, customer_id), limit=data.get("limit"), days=data.get("days"), user=user)
    return udhaar_service.customer_summary(db, c)


@router.post("/api/erp/udhaar/customers/{customer_id}/opening")
async def udhaar_opening(customer_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("udhaar.manage"))):
    data = await request.json()
    e = _run(db, udhaar_service.add_opening, _customer(db, customer_id), amount=data.get("amount"), due_date=data.get("due_date"),
             reminder_date=data.get("reminder_date"), note=data.get("note", ""), user=user)
    return {"ok": True, "entry_id": e.id}


@router.post("/api/erp/udhaar/customers/{customer_id}/receive")
async def udhaar_receive(customer_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("udhaar.receive"))):
    data = await request.json()
    c = _customer(db, customer_id)
    pays = _run(db, udhaar_service.receive_payment, c, amount=data.get("amount"), mode=data.get("mode", "CASH"),
                entry_id=data.get("entry_id"), reference=data.get("reference", ""), note=data.get("note", ""), user=user)
    total = money(sum((p.amount for p in pays), 0))
    return {"ok": True, "received": str(total), "outstanding": str(udhaar_service.outstanding(db, c.id)),
            "applied": [{"invoice": udhaar_service.label(p.entry), "amount": str(p.amount)} for p in pays]}


@router.post("/api/erp/udhaar/{entry_id}/dates")
async def udhaar_dates(entry_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("udhaar.receive"))):
    data = await request.json()
    _run(db, udhaar_service.set_dates, _entry(db, entry_id), due_date=data.get("due_date"), reminder_date=data.get("reminder_date"), user=user)
    return {"ok": True}


@router.post("/api/erp/udhaar/{entry_id}/remind")
async def udhaar_remind(entry_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("udhaar.receive"))):
    from app.services.whatsapp import service as wa

    data = await request.json()
    channel = str(data.get("channel") or "WHATSAPP").upper()
    entry = _entry(db, entry_id)
    if channel == "WHATSAPP":
        st = wa.public_status(fresh=True)
        if not st["connected"]:
            raise HTTPException(400, st["message"] + " — or note a phone-call reminder instead")
    try:
        rem = udhaar_service.send_reminder(db, entry, channel=channel, note=data.get("note", ""), user=user)
        db.commit()
    except (udhaar_service.UdhaarError, wa.WhatsAppError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return {"ok": True, "reminder_id": rem.id, "message": rem.message}


@router.post("/api/erp/udhaar/customers/{customer_id}/statement/whatsapp")
def udhaar_statement_whatsapp(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("udhaar.receive"))):
    from app.services.whatsapp import service as wa

    st = wa.public_status(fresh=True)
    if not st["connected"]:
        raise HTTPException(400, st["message"])
    try:
        msg = udhaar_service.share_statement(db, _customer(db, customer_id), user=user)
        db.commit()
    except (udhaar_service.UdhaarError, wa.WhatsAppError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    return {"ok": True, "message_id": msg.id}


@router.get("/api/erp/udhaar/settings")
def udhaar_settings(db: Session = Depends(get_db), user: User = Depends(require_permission("udhaar.view"))):
    return udhaar_service.settings(db)


@router.put("/api/erp/udhaar/settings")
async def udhaar_settings_save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("udhaar.manage"))):
    return _run(db, udhaar_service.save_settings, await request.json(), user=user)


@router.get("/api/erp/udhaar/{entry_id}/reminder-text")
def udhaar_reminder_text(entry_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("udhaar.view"))):
    return {"text": udhaar_service.reminder_text(db, _entry(db, entry_id))}


# ----------------------------------------------------------------------------- Counter Report
def _day(value: str, db: Session) -> date:
    if not value:
        return business_time.current_business_date(db)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")


@router.get("/api/erp/counter")
def counter_report(day: str = "", db: Session = Depends(get_db), user: User = Depends(require_permission("reports.counter"))):
    try:
        return counter_service.report(db, _day(day, db))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/erp/counter/documents")
def counter_documents(day: str = "", mode: str = "", db: Session = Depends(get_db),
                      user: User = Depends(require_permission("reports.counter"))):
    return counter_service.documents(db, _day(day, db), mode)
