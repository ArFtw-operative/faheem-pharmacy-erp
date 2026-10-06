"""Manual bills — typed reference documents, kept completely apart from sales.

A manual bill lists any items (picked from inventory as a reference, or typed) with a whole
quantity, a rate and an optional discount. It has its own number series (MB-…) and its own table:
it is never a sale, never in Sales History, sales / counter / profit / GST reports, cash, Udhaar or
customer balances, and it never checks or changes stock (no stock ledger row is ever written).
Creating, editing and deleting it are audited. Deleting keeps the record, marked Deleted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import Customer, Item, ManualBill, ManualBillItem, User
from app.sequences import next_manual_bill_no
from app.services import business_time, customer_service
from app.services.sales_service import SaleError, _line_discount, _pct, max_discount_pct, round_off
from app.utils import money, to_decimal, to_local, utcnow

MODES = ("CASH", "UPI", "CARD")


def _lines(db: Session, bill: ManualBill, lines: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    from app.services.sheet_import import parse_expiry

    if not lines:
        raise SaleError("A manual bill must contain at least one line")
    cap = max_discount_pct(db)
    subtotal = gross_total = Decimal("0")
    for line_no, line in enumerate(lines, start=1):
        if line.get("batch_id"):
            raise SaleError("A manual bill does not take a batch — it never changes stock")
        item = None
        if line.get("item_id"):
            item = db.get(Item, int(line["item_id"]))
            if item is None:
                raise SaleError(f"Line {line_no}: that product no longer exists")
        name = " ".join(str(line.get("name") or (item.name if item else "")).split())[:250]
        if not name:
            raise SaleError(f"Line {line_no}: enter the item name")
        try:
            qty = int(str(line.get("quantity") or "0").strip())
        except ValueError:
            raise SaleError(f"{name}: quantity must be a whole number")
        if qty <= 0:
            raise SaleError(f"{name}: quantity must be at least 1")
        rate = to_decimal(line.get("rate") or 0)
        if rate <= 0:
            raise SaleError(f"{name}: enter the rate")
        expiry = parse_expiry(line.get("expiry"))
        if str(line.get("expiry") or "").strip() and expiry is None:
            raise SaleError(f"{name}: expiry “{line.get('expiry')}” — type it as MM/YY, e.g. 05/28")
        gross = money(rate * qty)
        disc = _line_discount(line, gross, name, cap)
        gross_total += gross
        code = line.get("code") if line.get("code") is not None else (item.article_id if item else "")
        row = ManualBillItem(
            bill_id=bill.id, line_no=line_no, item_id=item.id if item else None, product_name=name,
            item_code=" ".join(str(code or "").split())[:40],
            pack_size=str(line.get("pack") or (item.pack_size if item else "") or "")[:60],
            batch_no=" ".join(str(line.get("batch") or "").split())[:60], expiry_date=expiry,
            quantity=qty, rate=money(rate), discount=disc, line_total=money(gross - disc))
        db.add(row)
        subtotal += row.line_total
    db.flush()
    db.refresh(bill, ["items"])
    return money(subtotal), money(gross_total)


def _totals(db: Session, bill: ManualBill, subtotal: Decimal, gross: Decimal, *, discount_pct: Any, round_off_mode: str,
            payment_mode: str, payments: list[dict] | None, cash_received: Any) -> None:
    cap = max_discount_pct(db)
    pct = to_decimal(discount_pct or 0)
    if pct < 0 or pct > cap:
        raise SaleError(f"Bill discount must be 0–{_pct(cap)}%")
    discount = money(subtotal * pct / 100)
    if gross > 0 and money(gross - subtotal + discount) > money(gross * cap / 100) + Decimal("0.01") * len(bill.items):
        raise SaleError(f"Total discount is more than {_pct(cap)}% of ₹{money(gross)}")
    total, delta = round_off(money(subtotal - discount), round_off_mode)
    bill.subtotal, bill.discount, bill.round_off, bill.total = subtotal, discount, delta, total
    # the payment as written on the bill — a record, never a collection
    if payments:
        parts = []
        for p in payments:
            mode = str(p.get("mode") or "").upper()
            if mode not in MODES:
                raise SaleError("A manual bill is paid by Cash, UPI or Card (no Udhaar: it is not a sale)")
            amount = money(to_decimal(p.get("amount") or 0))
            if amount > 0:
                parts.append({"mode": mode, "amount": str(amount), "reference": str(p.get("reference") or "")[:80]})
        if money(sum((Decimal(p["amount"]) for p in parts), Decimal("0"))) != total:
            raise SaleError(f"Payments must add up to ₹{total}")
    else:
        mode = (payment_mode or "CASH").upper()
        if mode not in MODES:
            raise SaleError("A manual bill is paid by Cash, UPI or Card (no Udhaar: it is not a sale)")
        parts = [{"mode": mode, "amount": str(total), "reference": ""}]
    bill.payment_parts = parts
    bill.payment_mode = parts[0]["mode"] if len(parts) == 1 else "SPLIT"
    cash = money(sum((Decimal(p["amount"]) for p in parts if p["mode"] == "CASH"), Decimal("0")))
    if cash > 0:
        tendered = money(cash_received) if cash_received not in (None, "") else cash
        if tendered < cash:
            raise SaleError(f"Insufficient cash received. {money(cash - tendered)} more is required")
        bill.tendered_amount, bill.change_amount = tendered, money(tendered - cash)
    else:
        bill.tendered_amount = bill.change_amount = None


def _snapshot(bill: ManualBill) -> dict:
    return {"invoice_no": bill.invoice_no, "total": str(bill.total), "customer_id": bill.customer_id,
            "lines": [{"name": i.product_name, "qty": i.quantity, "rate": str(i.rate), "discount": str(i.discount)} for i in bill.items]}


def create(db: Session, *, lines: list[dict], user: User | None = None, customer_id: int | None = None, customer_type: str = "WALK_IN",
           payment_mode: str = "CASH", payments: list[dict] | None = None, discount_pct: Any = None, notes: str = "",
           round_off_mode: str = "NEAREST_RUPEE", cash_received: Any = None, client_request_id: str | None = None,
           ip_address: str = "") -> ManualBill:
    if client_request_id:
        existing = db.scalar(select(ManualBill).where(ManualBill.client_request_id == client_request_id))
        if existing is not None:
            return existing
    now = utcnow()
    bill = ManualBill(invoice_no=next_manual_bill_no(db, to_local(now, business_time.timezone_name(db))), customer_id=customer_id,
                      customer_type=customer_service.normalize_customer_type(customer_type), user_id=user.id if user else None,
                      sale_date=now, notes=notes or "", status="ACTIVE", delete_reason="", client_request_id=client_request_id)
    db.add(bill)
    db.flush()
    subtotal, gross = _lines(db, bill, lines)
    _totals(db, bill, subtotal, gross, discount_pct=discount_pct, round_off_mode=round_off_mode, payment_mode=payment_mode,
            payments=payments, cash_received=cash_received)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="manual_bill", entity_id=bill.invoice_no, user=user, after=_snapshot(bill),
                 details=f"Manual bill {bill.invoice_no} ₹{bill.total} (record only — no stock, not a sale)", ip_address=ip_address)
    return bill


def amend(db: Session, bill: ManualBill, *, lines: list[dict], user: User | None = None, customer_id: int | None = None,
          customer_type: str = "WALK_IN", payment_mode: str = "CASH", payments: list[dict] | None = None, discount_pct: Any = None,
          notes: str | None = None, round_off_mode: str = "NEAREST_RUPEE", cash_received: Any = None, ip_address: str = "") -> ManualBill:
    if bill.status == "DELETED":
        raise SaleError("This manual bill was deleted")
    before = _snapshot(bill)
    for line in list(bill.items):
        db.delete(line)
    db.flush()
    db.expire(bill, ["items"])
    bill.customer_id = customer_id
    bill.customer_type = customer_service.normalize_customer_type(customer_type)
    if notes is not None:
        bill.notes = notes
    subtotal, gross = _lines(db, bill, lines)
    _totals(db, bill, subtotal, gross, discount_pct=discount_pct, round_off_mode=round_off_mode, payment_mode=payment_mode,
            payments=payments, cash_received=cash_received)
    bill.updated_at = utcnow()
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="manual_bill", entity_id=bill.invoice_no, user=user, before=before,
                 after=_snapshot(bill), details=f"Manual bill {bill.invoice_no} edited: ₹{before['total']} → ₹{bill.total}",
                 ip_address=ip_address)
    return bill


def delete(db: Session, bill: ManualBill, *, reason: str, user: User | None = None, ip_address: str = "") -> ManualBill:
    reason = " ".join(str(reason or "").split())[:200]
    if not reason:
        raise SaleError("Enter the reason for deleting the manual bill")
    if bill.status == "DELETED":
        raise SaleError("This manual bill was already deleted")
    bill.status, bill.deleted_at, bill.deleted_by, bill.delete_reason = "DELETED", utcnow(), user.id if user else None, reason
    db.flush()
    audit.record(db, action=audit.A_DELETE, entity_type="manual_bill", entity_id=bill.invoice_no, user=user,
                 details=f"Manual bill deleted: {reason}", ip_address=ip_address)
    return bill


def get(db: Session, bill_id: int) -> ManualBill | None:
    return db.scalar(select(ManualBill).where(ManualBill.id == bill_id)
                     .options(selectinload(ManualBill.items), selectinload(ManualBill.customer), selectinload(ManualBill.user)))


def payment_label(bill: ManualBill) -> str:
    names = {"CASH": "Cash", "UPI": "UPI", "CARD": "Card"}
    parts = bill.payments
    if len(parts) <= 1:
        mode = parts[0].mode if parts else bill.payment_mode
        return names.get(mode, mode.title())
    return " + ".join(f"{names.get(p.mode, p.mode)} ₹{money(p.amount)}" for p in parts)


def row(bill: ManualBill) -> dict:
    return {"id": bill.id, "kind": "manual", "invoice_no": bill.invoice_no, "date": bill.sale_date.isoformat(), "type": "MANUAL",
            "customer": bill.customer.name if bill.customer else "Walk-in", "mobile": bill.customer.mobile if bill.customer else "",
            "customer_type": bill.customer_type or "WALK_IN", "items": len(bill.items), "units": sum(i.quantity for i in bill.items),
            "total": str(bill.total), "payment": payment_label(bill), "mode": bill.payment_mode,
            "status": "CANCELLED" if bill.status == "DELETED" else "ACTIVE", "user": bill.user.username if bill.user else ""}


def detail(bill: ManualBill) -> dict:
    return {**row(bill), "subtotal": str(bill.subtotal), "bill_discount": str(bill.discount), "round_off": str(bill.round_off),
            "tendered": str(bill.tendered_amount) if bill.tendered_amount is not None else "",
            "change": str(bill.change_amount) if bill.change_amount is not None else "", "notes": bill.notes or "",
            "customer_id": bill.customer_id, "customer_code": bill.customer.customer_id if bill.customer else "",
            "deleted": bill.status == "DELETED", "delete_reason": bill.delete_reason or "",
            "lines": [{"id": i.id, "name": i.product_name, "code": i.item_code, "batch": i.batch_no,
                       "expiry": i.expiry_date.isoformat() if i.expiry_date else "", "pack": i.pack_size, "qty": i.quantity,
                       "rate": str(i.rate), "discount": str(i.discount), "total": str(i.line_total), "item_id": i.item_id}
                      for i in sorted(bill.items, key=lambda i: (i.line_no, i.id))]}


def search(db: Session, *, q: str = "", start=None, end=None, status: str = "", limit: int = 200, offset: int = 0) -> tuple[list[ManualBill], int]:
    conds = []
    if start:
        conds.append(ManualBill.sale_date >= start)
    if end:
        conds.append(ManualBill.sale_date < end)
    if status == "ACTIVE":
        conds.append(ManualBill.status == "ACTIVE")
    elif status == "DELETED":
        conds.append(ManualBill.status == "DELETED")
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        conds.append(or_(ManualBill.invoice_no.ilike(like),
                         ManualBill.customer_id.in_(select(Customer.id).where(or_(Customer.name.ilike(like), Customer.mobile.ilike(like)))),
                         ManualBill.id.in_(select(ManualBillItem.bill_id).where(ManualBillItem.product_name.ilike(like)))))
    total = db.scalar(select(func.count(ManualBill.id)).where(*conds)) or 0
    rows = list(db.scalars(select(ManualBill).where(*conds)
                           .options(selectinload(ManualBill.items), selectinload(ManualBill.customer), selectinload(ManualBill.user))
                           .order_by(ManualBill.sale_date.desc(), ManualBill.id.desc()).limit(min(max(limit, 1), 500)).offset(max(offset, 0))))
    return rows, total


def edit_payload(db: Session, bill: ManualBill) -> dict:
    """The bill reopened in a POS tab (manual mode)."""
    lines = []
    for i in sorted(bill.items, key=lambda i: (i.line_no, i.id)):
        gross = (i.line_total or 0) + (i.discount or 0)
        lines.append({"manual": True, "name": i.product_name, "qty": i.quantity, "rate": float(i.rate),
                      "disc": float(round(i.discount / gross * 100, 2)) if gross else 0.0, "item_id": i.item_id,
                      "code": i.item_code or "", "pack": i.pack_size or "", "batch": i.batch_no or "",
                      "expiry": i.expiry_date.strftime("%m/%y") if i.expiry_date else ""})
    items_total = sum((i.line_total or 0 for i in bill.items), 0)
    return {"manual_bill_id": bill.id, "invoice_no": bill.invoice_no, "invoice_type": "MANUAL", "lines": lines,
            "customer": customer_service.customer_payload(bill.customer) if bill.customer else None,
            "customer_type": bill.customer_type or "WALK_IN",
            "discount_pct": float(round(bill.discount / items_total * 100, 2)) if items_total and bill.discount else 0.0,
            "notes": bill.notes or "", "payment_mode": bill.payment_mode,
            "payments": [{"mode": p.mode, "amount": str(p.amount), "reference": p.reference} for p in bill.payments],
            "tendered": str(bill.tendered_amount) if bill.tendered_amount is not None else None, "total": str(bill.total)}
