"""Counter Report — one business day at the counter, by payment mode.

A business day is the store's calendar day (``timezone`` setting): it ends at 11:59 PM and the next
one starts at midnight. Figures are read from the documents of that day only — sales (voided bills
counted apart, never in money), payment rows, returns / refunds, and Udhaar repayments. Manual bills
are not sales and never appear here.

Once a day is over its figures are frozen in ``counter_day_closes`` (with a SHA-256 digest) — the
first time after midnight that anyone looks, or by the background job. A closed day always shows the
frozen figures; if a bill of that day was edited or voided later, the live figures are shown next
to them as a difference, never silently instead of them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import CounterDayClose, Sale, SaleItem, SalePayment, SaleReturn, UdhaarEntry, UdhaarPayment
from app.services import business_time
from app.utils import money, utcnow

MODES = ("CASH", "UPI", "CARD", "UDHAAR")
LABELS = {"CASH": "Cash", "UPI": "UPI / Online", "CARD": "Card", "UDHAAR": "Udhaar"}
VERSION = 1
_checked: date | None = None


def bounds(db: Session, day: date) -> tuple[datetime, datetime]:
    """The day's start and end as stored (UTC, naive): local midnight to the next midnight."""
    tz = ZoneInfo(business_time.timezone_name(db))
    to_utc = lambda d: datetime.combine(d, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(day), to_utc(day + timedelta(days=1))


def _d(v) -> Decimal:
    return money(v or 0)


def compute(db: Session, day: date) -> dict:
    start, end = bounds(db, day)
    in_day = (Sale.sale_date >= start, Sale.sale_date < end)
    live = (*in_day, Sale.payment_status != "CANCELLED")
    bills, sales_total, bill_disc, round_total = db.execute(
        select(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0), func.coalesce(func.sum(Sale.discount), 0),
               func.coalesce(func.sum(Sale.round_off), 0)).where(*live)).one()
    item_disc, items_value = db.execute(
        select(func.coalesce(func.sum(SaleItem.discount), 0), func.coalesce(func.sum(SaleItem.line_total), 0))
        .join(Sale, Sale.id == SaleItem.sale_id).where(*live)).one()
    void_count, void_total = db.execute(select(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0))
                                        .where(*in_day, Sale.payment_status == "CANCELLED")).one()
    by_mode = {m: (n, a) for m, n, a in db.execute(
        select(SalePayment.mode, func.count(func.distinct(SalePayment.sale_id)), func.coalesce(func.sum(SalePayment.amount), 0))
        .join(Sale, Sale.id == SalePayment.sale_id).where(*live).group_by(SalePayment.mode))}
    refunds = {m: (n, a) for m, n, a in db.execute(
        select(SaleReturn.refund_method, func.count(SaleReturn.id), func.coalesce(func.sum(SaleReturn.total_refund), 0))
        .where(SaleReturn.business_date == day, SaleReturn.status == "COMPLETED").group_by(SaleReturn.refund_method))}
    collected = {m: (n, a) for m, n, a in db.execute(
        select(UdhaarPayment.mode, func.count(UdhaarPayment.id), func.coalesce(func.sum(UdhaarPayment.amount), 0))
        .where(UdhaarPayment.business_date == day).group_by(UdhaarPayment.mode))}
    opening_given = _d(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount), 0))
                                 .where(UdhaarEntry.kind == "OPENING", UdhaarEntry.business_date == day)))
    # owed at the end of the day: everything given up to then (bills not voided) less everything received up to then
    given_to_date = _d(db.scalar(select(func.coalesce(func.sum(UdhaarEntry.amount), 0))
                                 .where(UdhaarEntry.business_date <= day, UdhaarEntry.status != "CANCELLED")))
    back_to_date = _d(db.scalar(select(func.coalesce(func.sum(UdhaarPayment.amount), 0))
                                .join(UdhaarEntry, UdhaarEntry.id == UdhaarPayment.entry_id)
                                .where(UdhaarPayment.business_date <= day, UdhaarEntry.status != "CANCELLED")))

    modes = []
    for m in MODES:
        n, sold = by_mode.get(m, (0, 0))
        rn, refunded = refunds.get(m, (0, 0))
        if m == "UDHAAR":
            cn, back = 0, 0
            received = Decimal("0.00")                    # Udhaar is money not yet received
        else:
            cn, back = collected.get(m, (0, 0))
            received = money(_d(sold) + _d(back) - _d(refunded))
        modes.append({"mode": m, "label": LABELS[m], "bills": n, "sales": str(_d(sold)), "refund_count": rn, "refunds": str(_d(refunded)),
                      "collections": cn, "collected": str(_d(back)), "received": str(received)})
    refund_total = money(sum((_d(a) for _, a in refunds.values()), Decimal("0")))
    refund_paid = money(sum((_d(a) for m, (_, a) in refunds.items() if m != "UDHAAR"), Decimal("0")))
    udhaar_sold = _d(by_mode.get("UDHAAR", (0, 0))[1])
    figures = {
        "version": VERSION, "date": day.isoformat(), "bills": bills,
        "gross": str(money(_d(items_value) + _d(item_disc))),          # MRP value of everything billed
        "item_discounts": str(_d(item_disc)), "bill_discounts": str(_d(bill_disc)),
        "discounts": str(money(_d(item_disc) + _d(bill_disc))), "round_off": str(_d(round_total)),
        "sales_total": str(_d(sales_total)),
        "returns": sum(n for n, _ in refunds.values()), "refunds": str(refund_total),
        "net": str(money(_d(sales_total) - refund_total)),
        "received": str(money(sum((Decimal(r["received"]) for r in modes), Decimal("0")))),
        "received_on_bills": str(money(_d(sales_total) - udhaar_sold)),
        "refunds_paid_out": str(refund_paid),
        "udhaar_given": str(udhaar_sold), "udhaar_opening_added": str(opening_given),
        "udhaar_collected": str(_d(sum((_d(a) for m, (_, a) in collected.items() if m != "RETURN"), Decimal("0")))),
        "udhaar_returned": str(_d(collected.get("RETURN", (0, 0))[1])),
        "udhaar_outstanding": str(money(given_to_date - back_to_date)),
        "void": {"bills": void_count, "total": str(_d(void_total))},
        "cash_in_hand": next(r["received"] for r in modes if r["mode"] == "CASH"),
        "modes": modes,
    }
    return figures


def _digest(figures: dict) -> str:
    return hashlib.sha256(json.dumps(figures, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def close_day(db: Session, day: date) -> CounterDayClose:
    """Freeze a finished day (idempotent: a closed day is never recomputed)."""
    if day >= business_time.current_business_date(db):
        raise ValueError("Only a finished business day can be closed")
    row = db.get(CounterDayClose, day)
    if row is None:
        figures = compute(db, day)
        row = CounterDayClose(business_date=day, figures=figures, digest=_digest(figures), closed_at=utcnow())
        db.add(row)
        db.flush()
    return row


def close_finished_days(db: Session, back: int = 45) -> int:
    """Background job: close every finished day (up to ``back`` days ago) that had any activity."""
    global _checked
    today = business_time.current_business_date(db)
    if _checked == today:                   # once per process per business day (the job runs every few minutes)
        return 0
    done = set(db.scalars(select(CounterDayClose.business_date).where(CounterDayClose.business_date >= today - timedelta(days=back))))
    start, _ = bounds(db, today - timedelta(days=back))
    days: set[date] = set()
    tz = ZoneInfo(business_time.timezone_name(db))
    for (when,) in db.execute(select(Sale.sale_date).where(Sale.sale_date >= start)):
        days.add(when.replace(tzinfo=timezone.utc).astimezone(tz).date())
    days |= set(db.scalars(select(UdhaarPayment.business_date).where(UdhaarPayment.business_date >= today - timedelta(days=back))))
    days |= set(db.scalars(select(SaleReturn.business_date).where(SaleReturn.business_date >= today - timedelta(days=back))))
    closed = 0
    for day in sorted(d for d in days if d < today and d not in done):
        close_day(db, day)
        closed += 1
    _checked = today
    return closed


def _changes(frozen: dict, live: dict, prefix: str = "") -> list[dict]:
    out = []
    for key, old in frozen.items():
        new = live.get(key)
        if key == "modes":
            for a, b in zip(old, new or []):
                out += _changes(a, b, f"{a['label']} · ")
        elif isinstance(old, dict):
            out += _changes(old, new or {}, f"{prefix}{key} · ")
        elif old != new and key not in ("version", "label", "mode"):
            out.append({"field": prefix + key, "closed": old, "now": new})
    return out


def report(db: Session, day: date) -> dict:
    today = business_time.current_business_date(db)
    if day > today:
        raise ValueError("That day has not started yet")
    live = compute(db, day)
    if day == today:
        return {"date": day.isoformat(), "state": "OPEN", "figures": live, "closed_at": None, "changes": [],
                "note": "Today is still open — figures update with every bill until midnight."}
    row = close_day(db, day)
    db.commit()
    intact = _digest(row.figures) == row.digest
    return {"date": day.isoformat(), "state": "CLOSED", "figures": row.figures, "closed_at": row.closed_at.isoformat() + "Z",
            "digest": row.digest, "intact": intact, "changes": _changes(row.figures, live),
            "note": "Closed at midnight — these are the figures as the day ended."}


def documents(db: Session, day: date, mode: str = "") -> dict:
    """Drill-down: the bills behind a figure (``mode``: CASH · UPI · CARD · UDHAAR · VOID · RETURNS · COLLECTIONS · ALL)."""
    from app.services.sales_service import payment_label

    start, end = bounds(db, day)
    mode = (mode or "ALL").upper()
    if mode == "RETURNS":
        rets = db.scalars(select(SaleReturn).where(SaleReturn.business_date == day).options(selectinload(SaleReturn.sale))
                          .order_by(SaleReturn.id))
        return {"kind": "returns", "rows": [{"id": r.sale_id, "return_no": r.return_no, "invoice_no": r.sale.invoice_no if r.sale else "",
                                             "date": r.processed_at.isoformat(), "amount": str(r.total_refund), "method": r.refund_method}
                                            for r in rets]}
    if mode == "COLLECTIONS":
        pays = db.scalars(select(UdhaarPayment).where(UdhaarPayment.business_date == day)
                          .options(selectinload(UdhaarPayment.entry).selectinload(UdhaarEntry.customer),
                                   selectinload(UdhaarPayment.entry).selectinload(UdhaarEntry.sale)).order_by(UdhaarPayment.id))
        return {"kind": "collections", "rows": [{"id": p.entry.sale_id, "customer": p.entry.customer.name,
                                                 "invoice_no": p.entry.sale.invoice_no if p.entry.sale else "Opening balance",
                                                 "date": p.received_at.isoformat(), "amount": str(p.amount), "method": p.mode}
                                                for p in pays]}
    stmt = select(Sale).where(Sale.sale_date >= start, Sale.sale_date < end)
    if mode == "VOID":
        stmt = stmt.where(Sale.payment_status == "CANCELLED")
    else:
        stmt = stmt.where(Sale.payment_status != "CANCELLED")
        if mode in MODES:
            stmt = stmt.where(Sale.payments.any(SalePayment.mode == mode))
    sales = db.scalars(stmt.options(selectinload(Sale.payments), selectinload(Sale.customer)).order_by(Sale.sale_date, Sale.id))
    return {"kind": "bills", "rows": [{"id": s.id, "invoice_no": s.invoice_no, "date": s.sale_date.isoformat(),
                                       "customer": s.customer.name if s.customer else "Walk-in", "total": str(s.total),
                                       "amount": str(money(sum((p.amount for p in s.payments if p.mode == mode), Decimal("0"))) if mode in MODES else s.total),
                                       "payment": payment_label(s), "status": s.payment_status} for s in sales]}
