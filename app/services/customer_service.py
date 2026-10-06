"""Customers as billing parties: identity, mobile lookup and creation.

This is transactional customer data (who a bill is for), not a CRM: there are
no follow-ups, scores or insight dashboards.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import audit
from app.models import CUSTOMER_TYPE_LABELS, CUSTOMER_TYPES, Customer, User
from app.sequences import next_customer_id

class CustomerError(Exception):
    pass


CUSTOMER_FIELDS = (
    "mobile", "alternate_mobile", "email", "gender", "doctor_name",
    "address", "city", "state", "pincode", "notes",
)


def normalize_customer_type(value) -> str:
    raw = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return raw if raw in CUSTOMER_TYPES else "WALK_IN"


def customer_type_label(value) -> str:
    return CUSTOMER_TYPE_LABELS.get(normalize_customer_type(value), "Walk-In")


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def customer_payload(c: Customer) -> dict:
    return {
        "id": c.id,
        "customer_id": c.customer_id,
        "name": c.name,
        "mobile": c.mobile,
        "alternate_mobile": c.alternate_mobile,
        "email": c.email,
        "gender": c.gender,
        "date_of_birth": c.date_of_birth.isoformat() if c.date_of_birth else "",
        "doctor_name": c.doctor_name,
        "address": c.address,
        "city": c.city,
        "state": c.state,
        "pincode": c.pincode,
        "notes": c.notes,
        "customer_type": c.customer_type,
        "customer_type_label": customer_type_label(c.customer_type),
        "udhaar_limit": str(c.udhaar_limit) if c.udhaar_limit is not None else None,
        "udhaar_days": c.udhaar_days,
    }


def mobile_key(mobile: str) -> str:
    """Comparable form of a phone number: digits only, last 10 (drops +91 / 0)."""
    digits = re.sub(r"\D", "", mobile or "")
    return digits[-10:] if len(digits) > 10 else digits


def find_by_mobile(db: Session, mobile: str) -> Customer | None:
    """Active customer with the same number, ignoring spaces, dashes and +91."""
    key = mobile_key(mobile)
    if not key:
        return None
    exact = db.scalar(
        select(Customer).where(Customer.mobile == (mobile or "").strip(), Customer.is_active.is_(True))
    )
    if exact is not None:
        return exact
    # Formatting differs ("98765 43210" vs "+91-9876543210"): narrow by the
    # last four digits in SQL, then compare the normalised numbers.
    candidates = db.scalars(
        select(Customer)
        .where(Customer.is_active.is_(True), Customer.mobile.like(f"%{key[-4:]}%"))
        .order_by(Customer.id)
    )
    for customer in candidates:
        if mobile_key(customer.mobile) == key:
            return customer
    return None


def search_by_mobile(db: Session, digits: str, limit: int = 8) -> list[Customer]:
    """Active customers whose number contains ``digits`` (spaces, dashes, +91 ignored)."""
    digits = re.sub(r"\D", "", digits or "")
    if len(digits) < 3:
        return []
    # Compare digits only, so "98765 43210" and "+91-9876543210" both match
    # "6543": strip the usual separators in SQL, then re-check in Python for
    # anything more exotic (brackets, dots).
    bare = Customer.mobile
    for sep in (" ", "-", "+", "(", ")", "."):
        bare = func.replace(bare, sep, "")
    candidates = db.scalars(
        select(Customer)
        .where(Customer.is_active.is_(True), bare.like(f"%{digits}%"))
        .order_by(Customer.name)
        .limit(200)
    )
    hits = [c for c in candidates if digits in re.sub(r"\D", "", c.mobile or "")]
    # numbers that start with what was typed come first
    hits.sort(key=lambda c: (not mobile_key(c.mobile).startswith(digits[-10:]), c.name.lower()))
    return hits[:limit]


def create_customer(
    db: Session,
    *,
    name: str,
    user: User | None = None,
    ip_address: str = "",
    **fields,
) -> Customer:
    name = (name or "").strip()
    if not name:
        raise CustomerError("Customer name is required")
    mobile = str(fields.get("mobile") or "").strip()
    if mobile:
        existing = find_by_mobile(db, mobile)
        if existing is not None:
            raise CustomerError(
                f"A customer with mobile {mobile} already exists: {existing.name} "
                f"({existing.customer_id})"
            )
    customer = Customer(
        customer_id=next_customer_id(db), name=name, mobile=mobile,
        customer_type=normalize_customer_type(fields.get("customer_type")),
        created_by=user.id if user else None,
    )
    dob = fields.get("date_of_birth")
    if dob:
        customer.date_of_birth = _parse_date(dob)
    for key in CUSTOMER_FIELDS:
        if key == "mobile":
            continue
        if fields.get(key) is not None:
            setattr(customer, key, str(fields[key]))
    db.add(customer)
    db.flush()
    audit.record(
        db,
        action=audit.A_CREATE,
        entity_type="customer",
        entity_id=customer.customer_id,
        user=user,
        after=customer,
        details=f"Customer created: {customer.name}",
        ip_address=ip_address,
    )
    return customer


def update_customer(
    db: Session, customer: Customer, *, user: User | None = None, ip_address: str = "", **fields
) -> Customer:
    before = audit.snapshot(customer)
    for key in ("name",) + CUSTOMER_FIELDS:
        if fields.get(key) is not None:
            setattr(customer, key, str(fields[key]))
    if fields.get("customer_type") is not None:
        customer.customer_type = normalize_customer_type(fields["customer_type"])
    if fields.get("date_of_birth") is not None:
        customer.date_of_birth = _parse_date(fields["date_of_birth"])
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="customer",
        entity_id=customer.customer_id,
        user=user,
        before=before,
        after=customer,
        ip_address=ip_address,
    )
    return customer


def delete_customer(db: Session, customer: Customer, *, user: User | None = None, ip_address: str = "") -> None:
    before = audit.snapshot(customer)
    customer.is_active = False
    db.flush()
    audit.record(
        db,
        action=audit.A_DELETE,
        entity_type="customer",
        entity_id=customer.customer_id,
        user=user,
        before=before,
        after={"is_active": False},
        details="Customer deactivated",
        ip_address=ip_address,
    )


def lookup(db: Session, q: str, limit: int = 8) -> list[Customer]:
    """Live customer search: digits search mobiles, text searches name / customer ID / doctor."""
    term = (q or "").strip().lstrip("#").strip()
    if not term:
        return []
    digits = "".join(ch for ch in term if ch.isdigit())
    if digits and len(digits) == len(term.replace(" ", "").replace("+", "").replace("-", "")):
        return search_by_mobile(db, digits, limit=limit)
    found, _ = search_customers(db, q=term, limit=limit)
    return found


def search_customers(db: Session, q: str = "", limit: int = 50, offset: int = 0) -> tuple[list[Customer], int]:
    stmt = select(Customer).where(Customer.is_active.is_(True))
    count_stmt = select(func.count(Customer.id)).where(Customer.is_active.is_(True))
    if q.strip():
        like = f"%{q.strip()}%"
        cond = or_(Customer.name.ilike(like), Customer.mobile.ilike(like), Customer.customer_id.ilike(like), Customer.doctor_name.ilike(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = db.scalar(count_stmt) or 0
    stmt = stmt.order_by(Customer.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt)), total


# --------------------------------------------------------------------------- ERP Customers module
# Every figure comes from finalized bills (voided ones excluded) less completed refunds,
# computed by the database — never in the browser.
def _money(v) -> str:
    from app.utils import money

    return str(money(v or 0))


def _sales_agg():
    from app.models import Sale

    return (select(Sale.customer_id.label("cid"), func.count(Sale.id).label("bills"),
                   func.coalesce(func.sum(Sale.total), 0).label("sales"), func.max(Sale.sale_date).label("last_visit"),
                   func.min(Sale.sale_date).label("first_visit"))
            .where(Sale.payment_status != "CANCELLED", Sale.customer_id.is_not(None)).group_by(Sale.customer_id).subquery())


def _refund_agg():
    from app.models import Sale, SaleReturn

    return (select(Sale.customer_id.label("cid"), func.coalesce(func.sum(SaleReturn.total_refund), 0).label("refunds"))
            .join(Sale, Sale.id == SaleReturn.sale_id).where(SaleReturn.status == "COMPLETED", Sale.customer_id.is_not(None))
            .group_by(Sale.customer_id).subquery())


def _followup_agg():
    from app.models import CustomerFollowUp as F

    return (select(F.customer_id.label("cid"), func.count(F.id).label("open"), func.min(F.due_date).label("next_due"))
            .where(F.status == "OPEN").group_by(F.customer_id).subquery())


DIRECTORY_SORTS = {"recent", "name", "sales", "bills"}


def directory(db: Session, q: str = "", *, sort: str = "recent", limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    """The customer directory page: identity plus bills, net sales, last visit and open follow-ups."""
    from app.models import Sale

    s, r, f = _sales_agg(), _refund_agg(), _followup_agg()
    net = (func.coalesce(s.c.sales, 0) - func.coalesce(r.c.refunds, 0)).label("net")
    stmt = (select(Customer, func.coalesce(s.c.bills, 0), net, s.c.last_visit, func.coalesce(f.c.open, 0), f.c.next_due)
            .outerjoin(s, s.c.cid == Customer.id).outerjoin(r, r.c.cid == Customer.id).outerjoin(f, f.c.cid == Customer.id)
            .where(Customer.is_active.is_(True)))
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        digits = re.sub(r"\D", "", text)
        conds = [Customer.name.ilike(like), Customer.customer_id.ilike(like), Customer.address.ilike(like), Customer.city.ilike(like)]
        if len(digits) >= 3:
            conds += [Customer.mobile.like(f"%{digits}%"), Customer.alternate_mobile.like(f"%{digits}%")]
        stmt = stmt.where(or_(*conds))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {"name": [func.lower(Customer.name)], "sales": [net.desc()], "bills": [func.coalesce(s.c.bills, 0).desc()]}.get(
        sort, [s.c.last_visit.is_(None), s.c.last_visit.desc(), func.lower(Customer.name)])
    rows = db.execute(stmt.order_by(*order).limit(min(max(limit, 1), 500)).offset(max(offset, 0))).all()
    ids = [c.id for c, *_ in rows]
    last_inv = {}
    if ids:
        latest = (select(Sale.customer_id, func.max(Sale.id)).where(Sale.customer_id.in_(ids), Sale.payment_status != "CANCELLED")
                  .group_by(Sale.customer_id).subquery())
        for cid, sid, no in db.execute(select(latest.c[0], Sale.id, Sale.invoice_no).join(Sale, Sale.id == latest.c[1])):
            last_inv[cid] = {"id": sid, "no": no}
    out = []
    for c, bills, net_v, last, open_n, next_due in rows:
        out.append({
            "id": c.id, "customer_id": c.customer_id, "name": c.name, "mobile": c.mobile, "alternate_mobile": c.alternate_mobile,
            "address": ", ".join(x for x in (c.address, c.city) if x), "area": c.city, "bills": bills, "net_sales": _money(net_v),
            "last_visit": last.isoformat() if last else "", "last_invoice": last_inv.get(c.id), "open_followups": open_n,
            "next_due": next_due.isoformat() if next_due else "",
        })
    return out, total


def record(db: Session, customer: Customer) -> dict:
    """One customer: details plus their figures (bills, gross, refunds, net, first / last visit)."""
    from app.models import Sale

    s, r, f = _sales_agg(), _refund_agg(), _followup_agg()
    row = db.execute(select(s.c.bills, s.c.sales, s.c.first_visit, s.c.last_visit).where(s.c.cid == customer.id)).first()
    refunds = db.scalar(select(r.c.refunds).where(r.c.cid == customer.id)) or 0
    opened = db.execute(select(f.c.open, f.c.next_due).where(f.c.cid == customer.id)).first()
    discount = db.scalar(select(func.coalesce(func.sum(Sale.discount), 0)).where(
        Sale.customer_id == customer.id, Sale.payment_status != "CANCELLED")) or 0
    bills, sales, first, last = row if row else (0, 0, None, None)
    return {
        **customer_payload(customer), "reference": customer.reference or "", "area": customer.city or "",
        "created_at": customer.created_at.isoformat() if customer.created_at else "",
        "updated_at": customer.updated_at.isoformat() if customer.updated_at else "",
        "stats": {"bills": bills or 0, "sales": _money(sales), "refunds": _money(refunds), "net_sales": _money((sales or 0) - (refunds or 0)),
                  "bill_discount": _money(discount), "average_bill": _money(((sales or 0) - (refunds or 0)) / bills) if bills else "0.00",
                  "first_visit": first.isoformat() if first else "", "last_visit": last.isoformat() if last else "",
                  "open_followups": opened[0] if opened else 0, "next_due": opened[1].isoformat() if opened and opened[1] else ""},
    }


EDITABLE = ("name", "mobile", "alternate_mobile", "address", "city", "reference", "notes", "doctor_name", "email")


def edit_customer(db: Session, customer: Customer, data: dict, *, user: User | None = None) -> Customer:
    from app.utils import utcnow

    changes = {k: " ".join(str(data[k]).split()) if k != "notes" and k != "address" else str(data[k]).strip()
               for k in EDITABLE if k in data and data[k] is not None}
    if "name" in changes and not changes["name"]:
        raise CustomerError("Customer name is required")
    if changes.get("mobile"):
        clash = find_by_mobile(db, changes["mobile"])
        if clash is not None and clash.id != customer.id:
            raise CustomerError(f"Mobile {changes['mobile']} already belongs to {clash.name} ({clash.customer_id})")
    before = audit.snapshot(customer)
    for k, v in changes.items():
        setattr(customer, k, v[:{"name": 150, "mobile": 20, "alternate_mobile": 20, "city": 80, "reference": 120,
                                 "doctor_name": 150, "email": 150}.get(k, 5000)])
    customer.updated_at = utcnow()
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="customer", entity_id=customer.customer_id, user=user,
                 before=before, after=customer, details=f"Customer {customer.name} updated")
    return customer


def add_note(db: Session, customer: Customer, text: str, *, user: User | None = None) -> Customer:
    """A dated line appended to the customer's notes (who and when kept)."""
    from app.services import business_time

    text = " ".join(str(text or "").split())[:500]
    if not text:
        raise CustomerError("Write the note")
    stamp = f"{business_time.now(db):%d-%b-%Y %H:%M} · {(user.username if user else 'system')}: {text}"
    customer.notes = (stamp + ("\n" + customer.notes if customer.notes else ""))[:20000]
    from app.utils import utcnow

    customer.updated_at = utcnow()
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="customer", entity_id=customer.customer_id, user=user,
                 after={"note": text}, details=f"Note added to {customer.name}")
    return customer


def invoices(db: Session, customer: Customer, *, limit: int = 300, offset: int = 0) -> tuple[list, int]:
    """The customer's bills through the canonical sales search (same rows as Sales History)."""
    from app.models import Sale

    base = select(Sale).where(Sale.customer_id == customer.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    from sqlalchemy.orm import selectinload

    rows = list(db.scalars(base.options(selectinload(Sale.items), selectinload(Sale.payments), selectinload(Sale.user))
                           .order_by(Sale.sale_date.desc(), Sale.id.desc()).limit(min(max(limit, 1), 1000)).offset(max(offset, 0))))
    return rows, total


def activity(db: Session, customer: Customer, limit: int = 200) -> list[dict]:
    """A timeline derived from the ERP's own records: bills, refunds, follow-ups (nothing stored twice)."""
    from app.models import CustomerFollowUp, Sale, SaleReturn

    events = []
    for s in db.scalars(select(Sale).where(Sale.customer_id == customer.id).order_by(Sale.sale_date.desc()).limit(limit)):
        events.append({"at": s.sale_date.isoformat(), "kind": "VOID" if s.payment_status == "CANCELLED" else "INVOICE",
                       "ref": s.invoice_no, "sale_id": s.id, "amount": _money(s.total),
                       "text": ("Voided bill " if s.payment_status == "CANCELLED" else "Bill ") + s.invoice_no})
    for ret in db.scalars(select(SaleReturn).join(Sale, Sale.id == SaleReturn.sale_id).where(Sale.customer_id == customer.id)
                          .order_by(SaleReturn.processed_at.desc()).limit(limit)):
        events.append({"at": ret.processed_at.isoformat(), "kind": "REFUND", "ref": ret.return_no, "sale_id": ret.sale_id,
                       "amount": _money(ret.total_refund), "text": f"Refund {ret.return_no} ({ret.refund_method.title()})"})
    for fu in db.scalars(select(CustomerFollowUp).where(CustomerFollowUp.customer_id == customer.id)
                         .order_by(CustomerFollowUp.created_at.desc()).limit(limit)):
        events.append({"at": fu.created_at.isoformat(), "kind": "FOLLOWUP", "ref": f"FU-{fu.id}", "followup_id": fu.id,
                       "text": f"Follow-up due {fu.due_date:%d-%b-%Y}", "status": fu.status})
        if fu.completed_at:
            events.append({"at": fu.completed_at.isoformat(), "kind": "FOLLOWUP_DONE", "ref": f"FU-{fu.id}", "followup_id": fu.id,
                           "text": ("Follow-up completed" if fu.status == "COMPLETED" else "Follow-up cancelled")
                                   + (f": {fu.completion_note}" if fu.completion_note else "")})
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]
