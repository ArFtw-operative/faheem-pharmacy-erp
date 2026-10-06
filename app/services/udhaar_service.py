"""Udhaar — what customers owe the pharmacy (store credit, pay later). Never a card payment.

A bill paid fully or partly by ``UDHAAR`` writes one :class:`UdhaarEntry` in the same transaction
as the sale: the amount owed, the due date and the reminder date (both required). Opening balances
brought from before the ERP are entries too (``kind = OPENING``). Money received later is a
:class:`UdhaarPayment` (CASH · UPI · CARD), goods returned against an Udhaar bill a ``RETURN``
payment; ``entry.paid`` is their sum, kept in the same transaction. The sale is never changed by a
repayment and no row is edited or deleted — the ledger only grows.

Status of an entry (computed on the business date): Paid · Partially Paid · Overdue · Due Today ·
Upcoming (or Cancelled when its bill was voided). Reminders (WhatsApp, or a call noted by the
cashier) are kept in ``udhaar_reminders``; WhatsApp delivery uses the invoice queue.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import Customer, Sale, UdhaarEntry, UdhaarPayment, UdhaarReminder, User, WhatsAppMessage
from app.services import business_time
from app.utils import money, to_decimal, to_local, utcnow

DEFAULT_DAYS = 15
RECEIVE_MODES = ("CASH", "UPI", "CARD")
MODE_LABELS = {"CASH": "Cash", "UPI": "UPI", "CARD": "Card", "RETURN": "Goods returned"}
CHANNELS = {"WHATSAPP": "WhatsApp", "CALL": "Phone call", "IN_PERSON": "In person"}
DEFAULT_TEMPLATE = ("Dear {customer}, a gentle reminder from {pharmacy}: ₹{balance} is due for bill {invoice_no} "
                    "dated {bill_date} (due {due_date}). Your total Udhaar is ₹{total}. Thank you.")


class UdhaarError(Exception):
    pass


# ----------------------------------------------------------------------------- terms & dates
def default_days(db: Session) -> int:
    from app.services.settings_service import get_int

    return max(0, get_int(db, "udhaar_days", DEFAULT_DAYS))


def days_for(db: Session, customer: Customer | None) -> int:
    return customer.udhaar_days if customer is not None and customer.udhaar_days is not None else default_days(db)


def proposed_dates(db: Session, customer: Customer | None, today: date | None = None) -> tuple[date, date]:
    """Due = today + the customer's days (else the store's); reminder = the day before the due date."""
    today = today or business_time.current_business_date(db)
    due = today + timedelta(days=days_for(db, customer))
    return due, max(today, due - timedelta(days=1))


def _date(value: Any, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        raise UdhaarError(f"Enter the {label} (YYYY-MM-DD)")


def outstanding(db: Session, customer_id: int) -> Decimal:
    return money(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount - UdhaarEntry.paid), 0))
                           .where(UdhaarEntry.customer_id == customer_id, UdhaarEntry.status == "OPEN")) or 0)


def customer_summary(db: Session, customer: Customer) -> dict:
    owed = outstanding(db, customer.id)
    due, reminder = proposed_dates(db, customer)
    today = business_time.current_business_date(db)
    overdue = money(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount - UdhaarEntry.paid), 0)).where(
        UdhaarEntry.customer_id == customer.id, UdhaarEntry.status == "OPEN", UdhaarEntry.due_date < today)) or 0)
    limit = money(customer.udhaar_limit) if customer.udhaar_limit is not None else None
    return {"customer_id": customer.id, "name": customer.name, "mobile": customer.mobile or "",
            "outstanding": str(owed), "overdue": str(overdue), "limit": str(limit) if limit is not None else None,
            "available": str(money(limit - owed)) if limit is not None else None,
            "days": days_for(db, customer), "custom_days": customer.udhaar_days is not None,
            "due_date": due.isoformat(), "reminder_date": reminder.isoformat()}


def set_terms(db: Session, customer: Customer, *, limit: Any = None, days: Any = None, user: User | None = None) -> Customer:
    before = {"udhaar_limit": str(customer.udhaar_limit), "udhaar_days": customer.udhaar_days}
    if limit in ("", None):
        customer.udhaar_limit = None
    else:
        value = money(to_decimal(limit))
        if value < 0:
            raise UdhaarError("The Udhaar limit cannot be negative")
        customer.udhaar_limit = value
    if days in ("", None):
        customer.udhaar_days = None
    else:
        try:
            n = int(str(days).strip())
        except ValueError:
            raise UdhaarError("Days to pay must be a whole number")
        if not 0 <= n <= 365:
            raise UdhaarError("Days to pay must be between 0 and 365")
        customer.udhaar_days = n
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="customer", entity_id=customer.customer_id, user=user, before=before,
                 after={"udhaar_limit": str(customer.udhaar_limit), "udhaar_days": customer.udhaar_days},
                 details=f"Udhaar terms for {customer.name}: limit {customer.udhaar_limit or 'none'}, "
                         f"{customer.udhaar_days if customer.udhaar_days is not None else 'store default'} days")
    return customer


def _check_dates(db: Session, due: date, reminder: date) -> None:
    today = business_time.current_business_date(db)
    if due < today:
        raise UdhaarError("The Udhaar due date cannot be before today")
    if reminder < today:
        raise UdhaarError("The reminder date cannot be before today")
    if reminder > due:
        raise UdhaarError("The reminder date must be on or before the due date")


def _check_customer(db: Session, customer: Customer | None, amount: Decimal, *, ignore_entry: UdhaarEntry | None = None) -> Customer:
    if customer is None:
        raise UdhaarError("Udhaar needs a customer — select or add the customer before completing the bill")
    if not (customer.mobile or "").strip():
        raise UdhaarError(f"{customer.name} has no mobile number — add it before giving Udhaar")
    if customer.udhaar_limit is not None:
        owed = outstanding(db, customer.id)
        if ignore_entry is not None and ignore_entry.status == "OPEN":
            owed = money(owed - (ignore_entry.amount - ignore_entry.paid))
        if money(owed + amount) > money(customer.udhaar_limit):
            raise UdhaarError(f"Udhaar limit for {customer.name} is ₹{money(customer.udhaar_limit)}: already owes ₹{owed}, "
                              f"this bill would make it ₹{money(owed + amount)}")
    return customer


# ----------------------------------------------------------------------------- sales
def entry_for_sale(db: Session, sale_id: int) -> UdhaarEntry | None:
    return db.scalar(select(UdhaarEntry).where(UdhaarEntry.sale_id == sale_id))


def sync_sale(db: Session, sale: Sale, terms: dict | None, *, user: User | None = None) -> UdhaarEntry | None:
    """After a sale is written (new or edited): its Udhaar part becomes / updates / cancels its entry.
    ``terms`` = {due_date, reminder_date} — required whenever the bill has an Udhaar part."""
    amount = money(sum((p.amount for p in sale.payments if p.mode == "UDHAAR"), Decimal("0")))
    entry = entry_for_sale(db, sale.id)
    if amount <= 0:
        if entry is not None and entry.status == "OPEN":
            if entry.paid > 0:
                raise UdhaarError("Udhaar payments were received on this bill — it can no longer drop its Udhaar")
            entry.status, entry.updated_at = "CANCELLED", utcnow()
        return entry
    terms = terms or {}
    if not terms.get("due_date") or not terms.get("reminder_date"):
        raise UdhaarError("Udhaar bill: confirm the due date and the reminder date before completing the sale")
    due, reminder = _date(terms["due_date"], "due date"), _date(terms["reminder_date"], "reminder date")
    customer = db.get(Customer, sale.customer_id) if sale.customer_id else None
    _check_customer(db, customer, amount, ignore_entry=entry)
    if entry is not None and entry.paid > amount:
        raise UdhaarError(f"₹{money(entry.paid)} was already received on this bill's Udhaar — the Udhaar part cannot be less")
    if entry is None or (due, reminder) != (entry.due_date, entry.reminder_date):   # an edit keeping the dates is not re-judged
        _check_dates(db, due, reminder)
    local_day = to_local(sale.sale_date, business_time.timezone_name(db)).date()
    if entry is None:
        entry = UdhaarEntry(customer_id=customer.id, sale_id=sale.id, kind="SALE", business_date=local_day, paid=Decimal("0"),
                            note="", created_by=user.id if user else None)
        db.add(entry)
    entry.customer_id, entry.amount, entry.due_date, entry.reminder_date = customer.id, amount, due, reminder
    entry.status = "PAID" if entry.paid >= amount else "OPEN"
    entry.updated_at = utcnow()
    db.flush()
    return entry


def cancel_for_void(db: Session, sale: Sale) -> None:
    entry = entry_for_sale(db, sale.id)
    if entry is None or entry.status == "CANCELLED":
        return
    if entry.paid > 0:
        raise UdhaarError(f"₹{money(entry.paid)} was already received on this bill's Udhaar — return the goods instead of voiding")
    entry.status, entry.updated_at = "CANCELLED", utcnow()


def edit_problem(db: Session, sale: Sale) -> str:
    entry = entry_for_sale(db, sale.id)
    if entry is not None and entry.status != "CANCELLED" and entry.paid > 0:
        return "Udhaar payments were received on this bill — record a return instead of editing it"
    return ""


# ----------------------------------------------------------------------------- opening balance
def add_opening(db: Session, customer: Customer, *, amount: Any, due_date: Any, reminder_date: Any, note: str = "",
                user: User | None = None) -> UdhaarEntry:
    value = money(to_decimal(amount))
    if value <= 0:
        raise UdhaarError("Enter the amount the customer owes")
    _check_customer(db, customer, Decimal("0"))
    due, reminder = _date(due_date, "due date"), _date(reminder_date, "reminder date")
    _check_dates(db, due, reminder)
    entry = UdhaarEntry(customer_id=customer.id, sale_id=None, kind="OPENING", business_date=business_time.current_business_date(db),
                        amount=value, paid=Decimal("0"), due_date=due, reminder_date=reminder, status="OPEN",
                        note=" ".join(str(note or "").split())[:300], created_by=user.id if user else None)
    db.add(entry)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="udhaar", entity_id=str(entry.id), user=user,
                 details=f"Udhaar opening balance ₹{value} for {customer.name} ({customer.customer_id}), due {due}")
    return entry


def set_dates(db: Session, entry: UdhaarEntry, *, due_date: Any, reminder_date: Any, user: User | None = None) -> UdhaarEntry:
    if entry.status != "OPEN":
        raise UdhaarError("This Udhaar is already settled")
    due, reminder = _date(due_date, "due date"), _date(reminder_date, "reminder date")
    today = business_time.current_business_date(db)
    if reminder < today:
        raise UdhaarError("The reminder date cannot be before today")
    if reminder > due and due >= today:
        raise UdhaarError("The reminder date must be on or before the due date")
    before = {"due_date": entry.due_date.isoformat(), "reminder_date": entry.reminder_date.isoformat()}
    entry.due_date, entry.reminder_date, entry.updated_at = due, reminder, utcnow()
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="udhaar", entity_id=str(entry.id), user=user, before=before,
                 after={"due_date": due.isoformat(), "reminder_date": reminder.isoformat()},
                 details=f"Udhaar {label(entry)}: due {due}, reminder {reminder}")
    return entry


# ----------------------------------------------------------------------------- repayments
def _pay(db: Session, entry: UdhaarEntry, amount: Decimal, mode: str, *, reference: str = "", note: str = "",
         return_id: int | None = None, user: User | None = None) -> UdhaarPayment:
    p = UdhaarPayment(entry_id=entry.id, customer_id=entry.customer_id, amount=amount, mode=mode, reference=reference[:80],
                      note=note, business_date=business_time.current_business_date(db), received_at=utcnow(),
                      return_id=return_id, user_id=user.id if user else None)
    db.add(p)
    entry.paid = money(entry.paid + amount)
    if entry.paid >= entry.amount:
        entry.status = "PAID"
    entry.updated_at = utcnow()
    return p


def receive_payment(db: Session, customer: Customer, *, amount: Any, mode: str = "CASH", entry_id: int | None = None,
                    reference: str = "", note: str = "", user: User | None = None) -> list[UdhaarPayment]:
    """Money received. On one entry (its balance at most), or — no entry chosen — on the oldest dues first."""
    value = money(to_decimal(amount))
    mode = str(mode or "").upper()
    if mode not in RECEIVE_MODES:
        raise UdhaarError("Udhaar is received by Cash, UPI or Card")
    if value <= 0:
        raise UdhaarError("Enter the amount received")
    if entry_id:
        entry = db.get(UdhaarEntry, int(entry_id))
        if entry is None or entry.customer_id != customer.id:
            raise UdhaarError("That Udhaar entry is not this customer's")
        if entry.status != "OPEN":
            raise UdhaarError("This Udhaar is already settled")
        entries = [entry]
    else:
        entries = list(db.scalars(select(UdhaarEntry).where(UdhaarEntry.customer_id == customer.id, UdhaarEntry.status == "OPEN")
                                  .order_by(UdhaarEntry.due_date, UdhaarEntry.id)))
    owed = money(sum((e.amount - e.paid for e in entries), Decimal("0")))
    if not entries or owed <= 0:
        raise UdhaarError(f"{customer.name} owes nothing")
    if value > owed:
        raise UdhaarError(f"Only ₹{owed} is owed{' on this bill' if entry_id else ''} — ₹{value} is more than that")
    note = " ".join(str(note or "").split())[:300]
    reference = str(reference or "").strip()
    out, left = [], value
    for e in entries:
        if left <= 0:
            break
        part = min(left, money(e.amount - e.paid))
        if part > 0:
            out.append(_pay(db, e, part, mode, reference=reference, note=note, user=user))
            left = money(left - part)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="udhaar", entity_id=customer.customer_id, user=user,
                 details=f"Udhaar received ₹{value} by {MODE_LABELS[mode]} from {customer.name}: "
                         + ", ".join(f"{label(p.entry)} ₹{p.amount}" for p in out))
    return out


def apply_return(db: Session, sale: Sale, return_doc, *, user: User | None = None) -> UdhaarPayment:
    """Goods returned on an Udhaar bill, refunded by reducing what is owed (refund method UDHAAR)."""
    entry = entry_for_sale(db, sale.id)
    amount = money(return_doc.total_refund)
    if entry is None or entry.status != "OPEN":
        raise UdhaarError("Nothing is owed on this bill — refund by Cash, UPI or Card")
    balance = money(entry.amount - entry.paid)
    if amount > balance:
        raise UdhaarError(f"Only ₹{balance} is still owed on this bill — refund ₹{balance} by Udhaar and the rest by Cash / UPI / Card")
    pay = _pay(db, entry, amount, "RETURN", reference=return_doc.return_no, note="Goods returned", return_id=return_doc.id, user=user)
    db.flush()
    return pay


# ----------------------------------------------------------------------------- views
def label(entry: UdhaarEntry) -> str:
    return entry.sale.invoice_no if entry.sale_id and entry.sale else "Opening balance"


def status(entry: UdhaarEntry, today: date) -> str:
    if entry.status == "CANCELLED":
        return "Cancelled"
    if entry.paid >= entry.amount:
        return "Paid"
    if entry.paid > 0:
        return "Partially Paid"
    if entry.due_date < today:
        return "Overdue"
    if entry.due_date == today:
        return "Due Today"
    return "Upcoming"


def reminder_state(entry: UdhaarEntry, today: date, tz: str) -> dict:
    last = entry.reminders[-1] if entry.reminders else None
    sent = None
    if last is not None:
        wa = last.whatsapp
        state = (wa.status if wa else "NOTED")
        sent = {"at": last.sent_at.isoformat() + "Z", "channel": CHANNELS.get(last.channel, last.channel), "state": state,
                "error": wa.last_error if wa and wa.status == "FAILED" else ""}
    due_now = entry.status == "OPEN" and entry.reminder_date <= today and (
        last is None or to_local(last.sent_at, tz).date() < entry.reminder_date)
    if entry.status != "OPEN":
        text = "—"
    elif due_now:
        text = "Reminder due"
    elif sent:
        text = f"Reminded ({sent['channel']}{', ' + sent['state'].lower() if sent['state'] not in ('NOTED',) else ''})"
    else:
        text = "Scheduled"
    return {"text": text, "due": due_now, "last": sent, "count": len(entry.reminders)}


def row(entry: UdhaarEntry, today: date, tz: str) -> dict:
    balance = money(entry.amount - entry.paid) if entry.status != "CANCELLED" else Decimal("0.00")
    c = entry.customer
    return {"id": entry.id, "customer_id": c.id, "customer_code": c.customer_id, "customer": c.name, "mobile": c.mobile or "",
            "invoice": label(entry), "sale_id": entry.sale_id, "kind": entry.kind, "sale_date": entry.business_date.isoformat(),
            "original": str(money(entry.amount)), "paid": str(money(entry.paid)), "balance": str(balance),
            "due_date": entry.due_date.isoformat(), "reminder_date": entry.reminder_date.isoformat(),
            "days_overdue": max(0, (today - entry.due_date).days) if balance > 0 else 0,
            "status": status(entry, today), "reminder": reminder_state(entry, today, tz), "note": entry.note or ""}


def _entries_query():
    return select(UdhaarEntry).options(selectinload(UdhaarEntry.customer), selectinload(UdhaarEntry.sale),
                                       selectinload(UdhaarEntry.reminders).selectinload(UdhaarReminder.whatsapp))


def list_entries(db: Session, *, q: str = "", view: str = "open", customer_id: int | None = None, limit: int = 500) -> dict:
    """``view``: open (owed) · overdue · today (due today) · reminders (reminder due) · paid · all."""
    today = business_time.current_business_date(db)
    stmt = _entries_query().join(Customer, Customer.id == UdhaarEntry.customer_id)
    if customer_id:
        stmt = stmt.where(UdhaarEntry.customer_id == customer_id)
    if view in ("open", "overdue", "today", "reminders"):
        stmt = stmt.where(UdhaarEntry.status == "OPEN")
    if view == "overdue":
        stmt = stmt.where(UdhaarEntry.due_date < today)
    elif view == "today":
        stmt = stmt.where(UdhaarEntry.due_date == today)
    elif view == "reminders":
        stmt = stmt.where(UdhaarEntry.reminder_date <= today)
    elif view == "paid":
        stmt = stmt.where(UdhaarEntry.status == "PAID")
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(Customer.name.ilike(like), Customer.mobile.ilike(like), Customer.customer_id.ilike(like),
                              UdhaarEntry.sale_id.in_(select(Sale.id).where(Sale.invoice_no.ilike(like)))))
    entries = list(db.scalars(stmt.order_by(UdhaarEntry.due_date, UdhaarEntry.id).limit(min(max(limit, 1), 2000))))
    tz = business_time.timezone_name(db)
    rows = [row(e, today, tz) for e in entries]
    if view == "reminders":
        rows = [r for r in rows if r["reminder"]["due"]]
    owed = money(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount - UdhaarEntry.paid), 0)).where(UdhaarEntry.status == "OPEN")) or 0)
    overdue = money(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount - UdhaarEntry.paid), 0))
                              .where(UdhaarEntry.status == "OPEN", UdhaarEntry.due_date < today)) or 0)
    customers = db.scalar(select(func.count(func.distinct(UdhaarEntry.customer_id))).where(UdhaarEntry.status == "OPEN")) or 0
    return {"today": today.isoformat(), "rows": rows,
            "totals": {"outstanding": str(owed), "overdue": str(overdue), "customers": customers,
                       "shown_balance": str(money(sum((Decimal(r["balance"]) for r in rows), Decimal("0"))))}}


def ledger(db: Session, customer: Customer) -> dict:
    """The customer's Udhaar account in date order: what was given (debit), what came back (credit)."""
    today = business_time.current_business_date(db)
    entries = list(db.scalars(_entries_query().where(UdhaarEntry.customer_id == customer.id).order_by(UdhaarEntry.id)))
    events = []
    for e in entries:
        events.append((e.created_at, 0, e.id, {"date": e.business_date.isoformat(), "type": "Opening balance" if e.kind == "OPENING" else "Udhaar bill",
                                               "reference": label(e), "debit": str(money(e.amount)), "credit": "", "due_date": e.due_date.isoformat(),
                                               "note": e.note or "", "entry_id": e.id, "sale_id": e.sale_id}))
        if e.status == "CANCELLED":
            events.append((e.updated_at or e.created_at, 1, e.id, {"date": to_local(e.updated_at or e.created_at, business_time.timezone_name(db)).date().isoformat(),
                                                                   "type": "Bill voided", "reference": label(e), "debit": "", "credit": str(money(e.amount)),
                                                                   "note": "", "entry_id": e.id, "sale_id": e.sale_id}))
    for p in db.scalars(select(UdhaarPayment).where(UdhaarPayment.customer_id == customer.id).order_by(UdhaarPayment.id)):
        events.append((p.received_at, 2, p.id, {"date": p.business_date.isoformat(), "type": "Goods returned" if p.mode == "RETURN" else f"Received · {MODE_LABELS.get(p.mode, p.mode)}",
                                               "reference": (label(p.entry) + (f" · {p.reference}" if p.reference else "")), "debit": "",
                                               "credit": str(money(p.amount)), "note": p.note or "", "entry_id": p.entry_id,
                                               "by": p.user.username if p.user else ""}))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    balance, lines = Decimal("0"), []
    for _, _, _, line in events:
        balance = money(balance + to_decimal(line["debit"] or 0) - to_decimal(line["credit"] or 0))
        lines.append({**line, "balance": str(balance)})
    return {"customer": {"id": customer.id, "customer_id": customer.customer_id, "name": customer.name, "mobile": customer.mobile or ""},
            "summary": customer_summary(db, customer), "lines": lines,
            "open": [row(e, today, business_time.timezone_name(db)) for e in entries if e.status == "OPEN"],
            "reminders": [{"at": r.sent_at.isoformat() + "Z", "channel": CHANNELS.get(r.channel, r.channel), "invoice": label(r.entry),
                           "balance": str(r.balance), "state": r.whatsapp.status if r.whatsapp else "NOTED",
                           "error": r.whatsapp.last_error if r.whatsapp else "", "automatic": r.automatic,
                           "by": r.user.username if r.user else "system"}
                          for e in entries for r in e.reminders][::-1]}


def statement_text(db: Session, customer: Customer) -> str:
    from app.services.settings_service import get_profile

    profile = get_profile(db) or {}
    data = ledger(db, customer)
    W = 72
    out = [(profile.get("pharmacy_name") or "Faheem Pharmacy").upper().center(W), "UDHAAR STATEMENT".center(W), "",
           f"Customer: {customer.name} ({customer.customer_id})   Mobile: {customer.mobile or '-'}",
           f"Date: {datetime.now().strftime('%d-%b-%Y')}", "-" * W,
           f"{'Date':<11}{'Particulars':<31}{'Debit':>10}{'Credit':>10}{'Balance':>10}", "-" * W]
    for line in data["lines"]:
        part = f"{line['type']} {line['reference']}"[:30]
        out.append(f"{line['date']:<11}{part:<31}{line['debit'] or '':>10}{line['credit'] or '':>10}{line['balance']:>10}")
    out += ["-" * W, f"{'Balance owed':<52}{data['summary']['outstanding']:>20}"]
    open_rows = data["open"]
    if open_rows:
        out += ["", "Open bills:"]
        out += [f"  {r['invoice']:<24} balance ₹{r['balance']:>10}  due {r['due_date']}  {r['status']}" for r in open_rows]
    return "\n".join(out)


# ----------------------------------------------------------------------------- reminders
def reminder_text(db: Session, entry: UdhaarEntry) -> str:
    from app.services.settings_service import get_profile, get_setting

    profile = get_profile(db) or {}
    template = get_setting(db, "udhaar_reminder_template", "") or DEFAULT_TEMPLATE
    values = {"customer": entry.customer.name, "pharmacy": profile.get("pharmacy_name") or "Faheem Pharmacy",
              "balance": f"{money(entry.amount - entry.paid)}", "invoice_no": label(entry),
              "bill_date": entry.business_date.strftime("%d %b %Y"), "due_date": entry.due_date.strftime("%d %b %Y"),
              "total": f"{outstanding(db, entry.customer_id)}"}
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_TEMPLATE.format(**values)


def send_reminder(db: Session, entry: UdhaarEntry, *, channel: str = "WHATSAPP", note: str = "", user: User | None = None,
                  automatic: bool = False) -> UdhaarReminder:
    """WhatsApp: the reminder is queued (the worker delivers it, retries, records the outcome).
    CALL / IN_PERSON: the cashier reminded the customer another way — noted in the same history."""
    channel = str(channel or "WHATSAPP").upper()
    if channel not in CHANNELS:
        raise UdhaarError("Unknown reminder channel")
    if entry.status != "OPEN":
        raise UdhaarError("Nothing is owed on this Udhaar")
    balance = money(entry.amount - entry.paid)
    message = reminder_text(db, entry)
    wa_msg = None
    if channel == "WHATSAPP":
        from app.services.whatsapp import service as wa

        phone = wa.normalize_phone(entry.customer.mobile)
        wa_msg = WhatsAppMessage(sale_id=entry.sale_id, kind="UDHAAR_REMINDER", udhaar_entry_id=entry.id, invoice_no=label(entry),
                                 customer_phone=phone, status="QUEUED", message_text=message, pdf_path="", provider=wa.provider().name,
                                 queued_at=utcnow(), next_attempt_at=utcnow(), created_by=user.id if user else None)
        db.add(wa_msg)
        db.flush()
        wa.wake()
    else:
        message = " ".join(str(note or "").split())[:300] or f"{CHANNELS[channel]} reminder"
    rem = UdhaarReminder(entry_id=entry.id, customer_id=entry.customer_id, channel=channel, whatsapp_message_id=wa_msg.id if wa_msg else None,
                         message=message, balance=balance, automatic=automatic, sent_at=utcnow(), user_id=user.id if user else None)
    db.add(rem)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="udhaar", entity_id=str(entry.id), user=user,
                 details=f"Udhaar reminder ({CHANNELS[channel]}{', automatic' if automatic else ''}) to {entry.customer.name} "
                         f"for {label(entry)}: ₹{balance}")
    return rem


def share_statement(db: Session, customer: Customer, *, user: User | None = None) -> WhatsAppMessage:
    """The customer's Udhaar statement as a WhatsApp text (queued like every message)."""
    from app.services.whatsapp import service as wa

    if not (customer.mobile or "").strip():
        raise UdhaarError(f"{customer.name} has no mobile number")
    msg = WhatsAppMessage(sale_id=None, kind="UDHAAR_STATEMENT", invoice_no="Udhaar statement", customer_phone=wa.normalize_phone(customer.mobile),
                          status="QUEUED", message_text="```\n" + statement_text(db, customer) + "\n```", pdf_path="",
                          provider=wa.provider().name, queued_at=utcnow(), next_attempt_at=utcnow(), created_by=user.id if user else None)
    db.add(msg)
    db.flush()
    wa.wake()
    audit.record(db, action=audit.A_CREATE, entity_type="udhaar", entity_id=customer.customer_id, user=user,
                 details=f"Udhaar statement sent on WhatsApp to {customer.name}")
    return msg


def settings(db: Session) -> dict:
    from app.services.settings_service import get_setting

    return {"days": default_days(db), "auto_reminders": get_setting(db, "udhaar_auto_reminders", "false").lower() in ("true", "1", "yes"),
            "template": get_setting(db, "udhaar_reminder_template", "") or DEFAULT_TEMPLATE, "default_template": DEFAULT_TEMPLATE}


def save_settings(db: Session, data: dict, *, user: User | None = None) -> dict:
    from app.services.settings_service import set_setting

    try:
        days = int(str(data.get("days", DEFAULT_DAYS)).strip())
    except ValueError:
        raise UdhaarError("Days to pay must be a whole number")
    if not 0 <= days <= 365:
        raise UdhaarError("Days to pay must be between 0 and 365")
    template = str(data.get("template") or "").strip()[:600]
    try:
        template.format(customer="", pharmacy="", balance="", invoice_no="", bill_date="", due_date="", total="")
    except (KeyError, IndexError, ValueError):
        raise UdhaarError("The message may use only {customer} {pharmacy} {balance} {invoice_no} {bill_date} {due_date} {total}")
    set_setting(db, "udhaar_days", str(days))
    set_setting(db, "udhaar_auto_reminders", "true" if data.get("auto_reminders") else "false")
    set_setting(db, "udhaar_reminder_template", "" if template == DEFAULT_TEMPLATE else template)
    audit.record(db, action=audit.A_UPDATE, entity_type="settings", entity_id="udhaar", user=user,
                 details=f"Udhaar settings: {days} days, automatic reminders {'on' if data.get('auto_reminders') else 'off'}")
    return settings(db)


def send_due_reminders(db: Session) -> int:
    """Automatic WhatsApp reminders (setting ``udhaar_auto_reminders``): each open Udhaar whose reminder
    date has come and that was not reminded since that date gets one reminder."""
    from app.services.settings_service import get_setting
    from app.services.whatsapp import service as wa

    if get_setting(db, "udhaar_auto_reminders", "false").lower() not in ("true", "1", "yes"):
        return 0
    if not wa.public_status().get("connected"):
        return 0
    today, tz = business_time.current_business_date(db), business_time.timezone_name(db)
    sent = 0
    for entry in db.scalars(_entries_query().where(UdhaarEntry.status == "OPEN", UdhaarEntry.reminder_date <= today).limit(50)):
        if not reminder_state(entry, today, tz)["due"] or not (entry.customer.mobile or "").strip():
            continue
        try:
            send_reminder(db, entry, automatic=True)
            sent += 1
        except Exception:                               # one bad number never stops the others
            db.rollback()
            continue
        db.commit()
    return sent
