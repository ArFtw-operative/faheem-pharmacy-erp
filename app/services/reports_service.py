"""Reporting: sales, purchases, returns, adjustments across time cycles."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.models import (
    SalePayment,
    CUSTOMER_TYPE_LABELS,
    Item,
    Purchase,
    PurchaseReturn,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    StockAdjustment,
    Supplier,
)
from app.services import settings_service
from app.utils import money, to_decimal, utcnow


def _to_utc_naive(local_dt: datetime) -> datetime:
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def period_range(period: str, tz_name: str | None = None) -> tuple[datetime | None, datetime]:
    """UTC-naive [start, end) bounds for a business period.

    Rows store naive UTC timestamps, but a business day is defined in the
    pharmacy's timezone. Boundaries are therefore built in local time and
    converted back to UTC, so "today" is the local day, not the UTC day.
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name or TIMEZONE)
    except Exception:
        return _period_range_utc(period)

    now_local = datetime.now(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start_local, end_local = day_start, day_start + timedelta(days=1)
    elif period == "week":
        start_local, end_local = day_start - timedelta(days=6), day_start + timedelta(days=1)
    elif period == "month":
        start_local, end_local = day_start - timedelta(days=29), day_start + timedelta(days=1)
    elif period == "year":
        start_local, end_local = day_start - timedelta(days=364), day_start + timedelta(days=1)
    else:
        # "all" / unknown: no lower bound and an end far in the future so every
        # stored row is included regardless of clock skew.
        return None, datetime(9999, 12, 31, 23, 59, 59)
    return _to_utc_naive(start_local), _to_utc_naive(end_local)


def _period_range_utc(period: str) -> tuple[datetime | None, datetime]:
    now = utcnow()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if period == "week":
        return now - timedelta(days=7), now + timedelta(days=1)
    if period == "month":
        return now - timedelta(days=30), now + timedelta(days=1)
    if period == "year":
        return now - timedelta(days=365), now + timedelta(days=1)
    return None, datetime(9999, 12, 31, 23, 59, 59)


def sales_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    conds = [Sale.payment_status != "CANCELLED", Sale.sale_date < end]
    if start:
        conds.append(Sale.sale_date >= start)
    row = db.execute(
        select(
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total), 0),
            func.coalesce(func.sum(Sale.discount), 0),
            func.coalesce(func.sum(Sale.voucher), 0),
            func.coalesce(func.sum(Sale.round_off), 0),
        ).where(*conds)
    ).one()
    # Money by method comes from the payment rows, so a split bill counts its
    # cash part as cash and its UPI part as UPI.
    by_mode = db.execute(
        select(SalePayment.mode, func.count(func.distinct(Sale.id)), func.coalesce(func.sum(SalePayment.amount), 0))
        .join(Sale, Sale.id == SalePayment.sale_id)
        .where(*conds)
        .group_by(SalePayment.mode)
    ).all()
    refunds = sale_refunds_summary(db, start, end)
    return {
        "count": row[0],
        "total": money(row[1]),
        # refunds are booked on the day they are paid out, like a billing ledger
        "refunds": refunds["refunded"],
        "returns": refunds["count"],
        "net": money(to_decimal(row[1]) - refunds["refunded"]),
        "refunds_by_mode": refunds["by_mode"],
        "discount": money(row[2]),
        "voucher": money(row[3]),
        "round_off": money(row[4]),
        "by_payment_mode": [
            {"mode": m or "UNKNOWN", "count": c, "total": money(t)} for m, c, t in by_mode
        ],
    }


def sale_refunds_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    """Completed customer returns paid out in the period: amount, count, method, restocked cost."""
    conds = [SaleReturn.status == "COMPLETED", SaleReturn.processed_at < end]
    if start:
        conds.append(SaleReturn.processed_at >= start)
    count, refunded = db.execute(
        select(func.count(SaleReturn.id), func.coalesce(func.sum(SaleReturn.total_refund), 0)).where(*conds)
    ).one()
    by_mode = {
        mode: money(total)
        for mode, total in db.execute(
            select(SaleReturn.refund_method, func.coalesce(func.sum(SaleReturn.total_refund), 0))
            .where(*conds)
            .group_by(SaleReturn.refund_method)
        )
    }
    from app.services import financials
    restocked=[r for r in financials.facts(db,start,end) if r.get('disposition')=='RESTOCK']
    restocked_cost=None if any(r['cost'] is None for r in restocked) else money(-sum((r['cost'] for r in restocked),Decimal(0)))
    return {
        "count": int(count or 0),
        "refunded": money(refunded),
        "by_mode": by_mode,
        "restocked_cost": restocked_cost,
    }


def profit_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    from app.services import financials
    facts=financials.facts(db,start,end)
    result=financials.aggregate(facts)
    result.update(refunds=money(-sum((r['net_sales'] for r in facts if r['quantity']<0),Decimal(0))),
                  gross=money(sum((r['gross_sales'] for r in facts),Decimal(0))),
                  units=sum(r['quantity'] for r in facts),bills=len({r['sale_id'] for r in facts if r['quantity']>0}))
    return result


def _merge_named(rows, key: str, default: str) -> list[dict]:
    """(name, total, qty) rows → dicts, NULL and empty names merged under ``default``, biggest first."""
    acc: dict = {}
    for name, total, qty in rows:
        k = name or default
        a = acc.setdefault(k, [0, 0])
        a[0] += total or 0
        a[1] += qty or 0
    return [{key: k, "total": money(v[0]), "quantity": v[1]} for k, v in sorted(acc.items(), key=lambda kv: -kv[1][0])]


def sales_by_category(db: Session, start: datetime | None, end: datetime) -> list[dict]:
    conds = [Sale.payment_status != "CANCELLED", Sale.sale_date < end]
    if start:
        conds.append(Sale.sale_date >= start)
    rows = db.execute(
        select(
            Item.category,
            func.coalesce(func.sum(SaleItem.line_total), 0),
            func.coalesce(func.sum(SaleItem.quantity), 0),
        )
        .select_from(Sale)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .join(Item, Item.id == SaleItem.item_id, isouter=True)
        .where(*conds)
        .group_by(Item.category)          # plain column: identical GROUP BY on SQLite and PostgreSQL
    ).all()
    return _merge_named(rows, "category", "UNCATEGORISED")


def sales_by_customer_type(db: Session, start: datetime | None, end: datetime) -> list[dict]:
    """Walk-In vs Home Delivery split of sales for a period."""
    conds = [Sale.payment_status != "CANCELLED", Sale.sale_date < end]
    if start:
        conds.append(Sale.sale_date >= start)
    rows = db.execute(
        select(
            Sale.customer_type,
            func.count(Sale.id),
            func.coalesce(func.sum(Sale.total), 0),
        )
        .where(*conds)
        .group_by(Sale.customer_type)
        .order_by(func.coalesce(func.sum(Sale.total), 0).desc())
    ).all()
    return [
        {
            "type": t or "WALK_IN",
            "label": CUSTOMER_TYPE_LABELS.get(t or "WALK_IN", "Walk-In"),
            "count": c,
            "total": money(v),
        }
        for t, c, v in rows
    ]


def sales_by_brand(db: Session, start: datetime | None, end: datetime, limit: int = 100) -> list[dict]:
    conds = [Sale.payment_status != "CANCELLED", Sale.sale_date < end]
    if start:
        conds.append(Sale.sale_date >= start)
    rows = db.execute(
        select(
            Item.manufacturer,
            func.coalesce(func.sum(SaleItem.line_total), 0),
            func.coalesce(func.sum(SaleItem.quantity), 0),
        )
        .select_from(Sale)
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .join(Item, Item.id == SaleItem.item_id, isouter=True)
        .where(*conds)
        .group_by(Item.manufacturer)
    ).all()
    return _merge_named(rows, "brand", "Unknown")[:limit]


def sales_by_item(db: Session, start: datetime | None, end: datetime, limit: int = 100) -> list[dict]:
    conds = [Sale.payment_status != "CANCELLED", Sale.sale_date < end]
    if start:
        conds.append(Sale.sale_date >= start)
    rows = db.execute(
        select(
            SaleItem.product_name,
            func.coalesce(func.sum(SaleItem.quantity), 0),
            func.coalesce(func.sum(SaleItem.line_total), 0),
        )
        .select_from(SaleItem)
        .join(Sale, Sale.id == SaleItem.sale_id)
        .where(*conds)
        .group_by(SaleItem.product_name)
        .order_by(func.coalesce(func.sum(SaleItem.line_total), 0).desc())
        .limit(limit)
    ).all()
    return [{"item": r[0], "quantity": r[1], "total": money(r[2])} for r in rows]


def purchase_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    conds = [Purchase.purchase_date <= end]
    if start:
        conds.append(Purchase.purchase_date >= start)
    row = db.execute(
        select(func.count(Purchase.id), func.coalesce(func.sum(Purchase.total), 0)).where(*conds)
    ).one()
    return {"count": row[0], "total": money(row[1])}


def purchases_by_agency(db: Session, start: datetime | None, end: datetime) -> list[dict]:
    conds = [Purchase.purchase_date <= end]
    if start:
        conds.append(Purchase.purchase_date >= start)
    rows = db.execute(
        select(
            Supplier.name,
            func.count(Purchase.id),
            func.coalesce(func.sum(Purchase.total), 0),
        )
        .select_from(Purchase)
        .join(Supplier, Supplier.id == Purchase.supplier_id, isouter=True)
        .where(*conds)
        .group_by(Supplier.name)
    ).all()
    rows = sorted(((r[0] or "Unassigned", r[1], r[2]) for r in rows), key=lambda r: -r[2])
    return [{"agency": r[0], "count": r[1], "total": money(r[2])} for r in rows]


def returns_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    conds = [PurchaseReturn.return_date <= end]
    if start:
        conds.append(PurchaseReturn.return_date >= start)
    row = db.execute(
        select(func.count(PurchaseReturn.id), func.coalesce(func.sum(PurchaseReturn.value), 0)).where(*conds)
    ).one()
    items = db.execute(
        select(PurchaseReturn.product_name, func.sum(PurchaseReturn.quantity), func.sum(PurchaseReturn.value))
        .select_from(PurchaseReturn)
        .where(*conds)
        .group_by(PurchaseReturn.product_name)
    ).all()
    return {
        "count": row[0],
        "total": money(row[1]),
        "items": [{"item": r[0], "quantity": r[1], "value": money(r[2])} for r in items],
    }


def _is_loss():
    """A standing write-off: a decrease that was not reversed (and is not itself a reversal)."""
    return (StockAdjustment.direction == "OUT", StockAdjustment.reversal_of_id.is_(None), StockAdjustment.reversed_at.is_(None))


def adjustments_summary(db: Session, start: datetime | None, end: datetime) -> dict:
    conds = [StockAdjustment.adjustment_date < end, *_is_loss()]
    if start:
        conds.append(StockAdjustment.adjustment_date >= start)
    rows = db.execute(
        select(
            StockAdjustment.category,
            func.count(StockAdjustment.id),
            func.coalesce(func.sum(StockAdjustment.quantity), 0),
            func.coalesce(func.sum(StockAdjustment.value), 0),
        )
        .select_from(StockAdjustment)
        .where(*conds)
        .group_by(StockAdjustment.category)
    ).all()
    return {
        r[0]: {"count": r[1], "quantity": r[2], "value": money(r[3])} for r in rows
    }


# Customer-return dispositions that never go back on the shelf, mapped onto the
# write-off categories so every stock loss is counted in one place.
RETURN_LOSS_CATEGORY = {"DAMAGE": "DAMAGE", "EXPIRED": "EXPIRED", "NO_RESTOCK": "DAMAGE"}


def loss_entries(
    db: Session,
    start: datetime | None,
    end: datetime,
    *,
    item_id: int | None = None,
    category: str = "",
    q: str = "",
    limit: int = 300,
) -> list[dict]:
    """Every inventory loss, newest first: manual write-offs (loose, damage, expired,
    count shortfall) plus customer returns that were not restocked, valued at cost."""
    from app.models import Batch, Item

    text = q.strip().lower()
    adj_conds = [StockAdjustment.adjustment_date < end, *_is_loss()]
    if start:
        adj_conds.append(StockAdjustment.adjustment_date >= start)
    if item_id:
        adj_conds.append(StockAdjustment.item_id == item_id)
    if category:
        adj_conds.append(StockAdjustment.category == category)
    adj_rows = db.execute(
        select(StockAdjustment, Item.name, Batch.batch_no)
        .join(Item, Item.id == StockAdjustment.item_id)
        .outerjoin(Batch, Batch.id == StockAdjustment.batch_id)
        .where(*adj_conds)
        .order_by(StockAdjustment.adjustment_date.desc())
        .limit(limit)
    ).all()
    entries = [
        {
            "date": a.adjustment_date, "item_id": a.item_id, "item_name": name or "", "batch_no": batch_no or "",
            "category": a.category, "quantity": a.quantity, "value": money(a.value), "reason": a.reason or "",
            "user_id": a.created_by, "source": "WRITE_OFF", "ref": "", "sale_id": None,
        }
        for a, name, batch_no in adj_rows
    ]

    cats = [k for k, v in RETURN_LOSS_CATEGORY.items() if not category or v == category]
    if cats:
        ret_conds = [SaleReturn.status == "COMPLETED", SaleReturn.processed_at < end, SaleReturnItem.disposition.in_(cats)]
        if start:
            ret_conds.append(SaleReturn.processed_at >= start)
        if item_id:
            ret_conds.append(SaleReturnItem.item_id == item_id)
        ret_rows = db.execute(
            select(SaleReturnItem, SaleReturn, SaleItem.cost_rate)
            .join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id)
            .outerjoin(SaleItem, SaleItem.id == SaleReturnItem.sale_item_id)
            .where(*ret_conds)
            .order_by(SaleReturn.processed_at.desc())
            .limit(limit)
        ).all()
        for ri, ret, cost in ret_rows:
            entries.append({
                "date": ret.processed_at, "item_id": ri.item_id, "item_name": ri.product_name, "batch_no": ri.batch_no or "",
                "category": RETURN_LOSS_CATEGORY[ri.disposition], "quantity": ri.quantity,
                "value": money(to_decimal(cost or 0) * ri.quantity),
                "reason": " — ".join(x for x in (f"Customer return {ret.return_no}", (ret.reason_note or ret.reason_code or "").strip()) if x),
                "user_id": ret.processed_by_user_id, "source": "RETURN", "ref": ret.return_no, "sale_id": ret.sale_id,
            })
    if text:
        entries = [e for e in entries if text in f"{e['item_name']} {e['batch_no']} {e['reason']}".lower()]
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:limit]


def loss_summary(db: Session, start: datetime | None, end: datetime, *, item_id: int | None = None) -> dict:
    """Per-category loss totals (write-offs + unsellable customer returns) with a grand total."""
    by_cat: dict[str, dict] = {}
    for e in loss_entries(db, start, end, item_id=item_id, limit=100_000):
        v = by_cat.setdefault(e["category"], {"count": 0, "quantity": 0, "value": money(0), "returns": money(0)})
        v["count"] += 1
        v["quantity"] += e["quantity"]
        v["value"] = money(v["value"] + e["value"])
        if e["source"] == "RETURN":
            v["returns"] = money(v["returns"] + e["value"])
    return {
        "categories": by_cat,
        "total": money(sum((v["value"] for v in by_cat.values()), start=0)),
        "quantity": sum(v["quantity"] for v in by_cat.values()),
        "count": sum(v["count"] for v in by_cat.values()),
    }


def profit_loss_widget(db: Session, start: datetime | None, end: datetime) -> dict:
    from app.services import financials
    facts=financials.facts(db,start,end)
    basis=settings_service.get_setting(db,'profit_basis','realized')
    result=financials.aggregate(facts,basis)
    conds=[StockAdjustment.adjustment_date<end]
    if start: conds.append(StockAdjustment.adjustment_date>=start)
    qty,value=db.execute(select(func.coalesce(func.sum(StockAdjustment.quantity),0),func.coalesce(func.sum(StockAdjustment.value),0)).where(*conds)).one()
    loss=money(result['stock_loss']+to_decimal(value)) if result['stock_loss'] is not None else None
    profit=money(result['profit']-loss) if result['profit'] is not None else None
    recovered=[r for r in facts if r.get('disposition')=='RESTOCK']
    recovered_cost=None if any(r['cost'] is None for r in recovered) else money(-sum((r['cost'] for r in recovered),Decimal(0)))
    sold=sum(r['quantity'] for r in facts if r['quantity']>0);returned=-sum(r['quantity'] for r in facts if r['quantity']<0)
    restocked=-sum(r['quantity'] for r in recovered)
    def numeric(value): return float(value) if value is not None else None
    return dict(sold_packs=sold,returned_packs=returned,restocked_packs=restocked,damaged_packs=returned-restocked+int(qty),net_packs=sold-returned,
        revenue=numeric(result['revenue']),cogs=numeric(result['cogs']),profit=numeric(profit),
        margin=numeric(money(profit*100/result['revenue'])) if profit is not None and result['revenue'] else None,
        roi=numeric(money(profit*100/result['cogs'])) if profit is not None and result['cogs'] else None,
        refunds=numeric(money(-sum((r['net_sales'] for r in facts if r['quantity']<0),Decimal(0)))),
        recovered=numeric(recovered_cost),write_off=numeric(loss),missing_cost_lines=result['missing_cost_lines'],financial_status=result['financial_status'])
