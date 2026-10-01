"""Customer follow-ups: one authoritative record behind POS, Inbox, Calendar
and the customer's activity. Completing, rescheduling or cancelling changes
that one record, so every view agrees immediately; every change is audited.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import FOLLOWUP_REASONS, Customer, CustomerFollowUp, Sale, User
from app.services import business_time
from app.utils import utcnow

PRESETS = {"7": 7, "15": 15, "30": 30}
VIEWS = ("today", "overdue", "upcoming", "completed", "open", "all")


class FollowUpError(Exception):
    pass


def _date(value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise FollowUpError("Choose the follow-up date")


def _time(value: Any) -> str:
    text = str(value or "").strip()
    if text and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise FollowUpError("Time must be HH:MM")
    return text


def create(db: Session, *, customer_id: int, due_date: Any = None, preset: str = "", due_time: str = "", reason: str = "GENERAL",
           note: str = "", contact_method: str = "", source_sale_id: int | None = None, user: User | None = None) -> CustomerFollowUp:
    customer = db.get(Customer, int(customer_id or 0))
    if customer is None:
        raise FollowUpError("Choose the customer")
    today = business_time.current_business_date(db)
    due = today + timedelta(days=PRESETS[preset]) if preset in PRESETS else _date(due_date)
    if due < today:
        raise FollowUpError("A follow-up cannot be due in the past")
    reason = (reason or "GENERAL").upper()
    if reason not in FOLLOWUP_REASONS:
        raise FollowUpError("Choose a valid reason")
    if reason == "CUSTOM" and not str(note or "").strip():
        raise FollowUpError("Write the reason in the note")
    sale = db.get(Sale, int(source_sale_id)) if source_sale_id else None
    if source_sale_id and (sale is None or sale.customer_id != customer.id):
        raise FollowUpError("That invoice does not belong to this customer")
    fu = CustomerFollowUp(customer_id=customer.id, source_sale_id=sale.id if sale else None, due_date=due, due_time=_time(due_time),
                          reason=reason, note=str(note or "").strip()[:1000], contact_method=str(contact_method or "").upper()[:20],
                          status="OPEN", created_by=user.id if user else None)
    db.add(fu)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="followup", entity_id=fu.id, user=user,
                 after={"customer": customer.customer_id, "due": due.isoformat(), "reason": reason},
                 details=f"Follow-up for {customer.name} due {due:%d-%b-%Y}")
    return fu


def _open(fu: CustomerFollowUp) -> None:
    if fu.status != "OPEN":
        raise FollowUpError(f"This follow-up is already {fu.status.lower()}")


def complete(db: Session, fu: CustomerFollowUp, *, note: str = "", user: User | None = None) -> CustomerFollowUp:
    _open(fu)
    fu.status, fu.completed_at, fu.completed_by = "COMPLETED", utcnow(), user.id if user else None
    fu.completion_note = str(note or "").strip()[:1000]
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="followup", entity_id=fu.id, user=user,
                 after={"status": "COMPLETED", "note": fu.completion_note}, details="Follow-up completed")
    return fu


def reschedule(db: Session, fu: CustomerFollowUp, *, due_date: Any = None, preset: str = "", due_time: str | None = None,
               note: str = "", user: User | None = None) -> CustomerFollowUp:
    _open(fu)
    today = business_time.current_business_date(db)
    due = today + timedelta(days=PRESETS[preset]) if preset in PRESETS else _date(due_date)
    if due < today:
        raise FollowUpError("A follow-up cannot be due in the past")
    before = {"due": fu.due_date.isoformat(), "time": fu.due_time}
    fu.due_date = due
    if due_time is not None:
        fu.due_time = _time(due_time)
    fu.reschedule_count = (fu.reschedule_count or 0) + 1
    if note:
        fu.note = (fu.note + ("\n" if fu.note else "") + f"Rescheduled: {str(note).strip()}")[:1000]
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="followup", entity_id=fu.id, user=user, before=before,
                 after={"due": due.isoformat(), "time": fu.due_time}, details=f"Follow-up rescheduled to {due:%d-%b-%Y}")
    return fu


def cancel(db: Session, fu: CustomerFollowUp, *, note: str = "", user: User | None = None) -> CustomerFollowUp:
    _open(fu)
    fu.status, fu.completed_at, fu.completed_by = "CANCELLED", utcnow(), user.id if user else None
    fu.completion_note = str(note or "").strip()[:1000]
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="followup", entity_id=fu.id, user=user,
                 after={"status": "CANCELLED"}, details="Follow-up cancelled")
    return fu


def listing(db: Session, *, view: str = "open", start: Any = None, end: Any = None, customer_id: int | None = None,
            q: str = "", created_by: int | None = None, limit: int = 500) -> list[CustomerFollowUp]:
    today = business_time.current_business_date(db)
    stmt = select(CustomerFollowUp).options(selectinload(CustomerFollowUp.customer), selectinload(CustomerFollowUp.source_sale))
    if view == "today":
        stmt = stmt.where(CustomerFollowUp.status == "OPEN", CustomerFollowUp.due_date == today)
    elif view == "overdue":
        stmt = stmt.where(CustomerFollowUp.status == "OPEN", CustomerFollowUp.due_date < today)
    elif view == "upcoming":
        stmt = stmt.where(CustomerFollowUp.status == "OPEN", CustomerFollowUp.due_date > today)
    elif view == "completed":
        stmt = stmt.where(CustomerFollowUp.status.in_(("COMPLETED", "CANCELLED")))
    elif view == "open":
        stmt = stmt.where(CustomerFollowUp.status == "OPEN")
    if start:
        stmt = stmt.where(CustomerFollowUp.due_date >= _date(start))
    if end:
        stmt = stmt.where(CustomerFollowUp.due_date <= _date(end))
    if customer_id:
        stmt = stmt.where(CustomerFollowUp.customer_id == customer_id)
    if created_by:
        stmt = stmt.where(CustomerFollowUp.created_by == created_by)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.join(Customer, Customer.id == CustomerFollowUp.customer_id).where(
            or_(Customer.name.ilike(like), Customer.mobile.ilike(like), CustomerFollowUp.note.ilike(like)))
    order = [CustomerFollowUp.completed_at.desc()] if view == "completed" else [CustomerFollowUp.due_date, CustomerFollowUp.due_time]
    return list(db.scalars(stmt.order_by(*order, CustomerFollowUp.id).limit(min(max(limit, 1), 2000))))


def payload(db: Session, fu: CustomerFollowUp, users: dict[int, str] | None = None, last_visits: dict[int, str] | None = None) -> dict:
    today = business_time.current_business_date(db)
    c = fu.customer
    state = fu.status if fu.status != "OPEN" else ("OVERDUE" if fu.due_date < today else "TODAY" if fu.due_date == today else "UPCOMING")
    return {
        "id": fu.id, "customer_id": fu.customer_id, "customer": c.name if c else "", "mobile": c.mobile if c else "",
        "customer_code": c.customer_id if c else "", "due_date": fu.due_date.isoformat(), "due_time": fu.due_time,
        "reason": fu.reason, "reason_label": FOLLOWUP_REASONS.get(fu.reason, fu.reason), "note": fu.note,
        "contact_method": fu.contact_method, "status": fu.status, "state": state, "reschedules": fu.reschedule_count,
        "source_sale_id": fu.source_sale_id, "source_invoice": fu.source_sale.invoice_no if fu.source_sale else "",
        "created_by": (users or {}).get(fu.created_by, ""), "created_at": fu.created_at.isoformat() if fu.created_at else "",
        "completed_at": fu.completed_at.isoformat() if fu.completed_at else "", "completion_note": fu.completion_note,
        "last_visit": (last_visits or {}).get(fu.customer_id, ""),
    }


def payloads(db: Session, rows: list[CustomerFollowUp]) -> list[dict]:
    from sqlalchemy import func

    ids = {r.created_by for r in rows if r.created_by}
    users = {u.id: u.username for u in db.scalars(select(User).where(User.id.in_(ids)))} if ids else {}
    cids = {r.customer_id for r in rows}
    visits = {cid: last.isoformat() for cid, last in db.execute(
        select(Sale.customer_id, func.max(Sale.sale_date)).where(Sale.customer_id.in_(cids), Sale.payment_status != "CANCELLED")
        .group_by(Sale.customer_id))} if cids else {}
    return [payload(db, r, users, visits) for r in rows]


def counts(db: Session) -> dict:
    from sqlalchemy import case, func

    today = business_time.current_business_date(db)
    row = db.execute(select(
        func.sum(case((CustomerFollowUp.due_date == today, 1), else_=0)),
        func.sum(case((CustomerFollowUp.due_date < today, 1), else_=0)),
        func.sum(case((CustomerFollowUp.due_date > today, 1), else_=0))).where(CustomerFollowUp.status == "OPEN")).one()
    return {"today": int(row[0] or 0), "overdue": int(row[1] or 0), "upcoming": int(row[2] or 0)}
