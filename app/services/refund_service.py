"""Invoice-linked return / refund workflow.

Refunds are controlled reversals against a completed invoice. The original
sale, its lines and its ledger entries are never edited or deleted.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import (
    Batch,
    Sale,
    SaleReturn,
    SaleReturnItem,
    User,
)
from app.services import business_time
from app.utils import money, to_decimal, utcnow


class RefundError(Exception):
    pass


def next_return_no(db: Session, when: datetime | None = None) -> str:
    from app.sequences import next_number

    day = (when or business_time.now(db)).strftime("%Y%m%d")
    return f"RFND-{day}-{next_number(db, f'refund:{day}'):04d}"


def _sale_adjust(sale: Sale) -> Decimal:
    return to_decimal(sale.discount) + to_decimal(sale.voucher) - to_decimal(sale.round_off)


def _returned_qty_by_line(db: Session, sale_id: int) -> dict[int, int]:
    rows = db.execute(
        select(SaleReturnItem.sale_item_id, func.coalesce(func.sum(SaleReturnItem.quantity), 0))
        .join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id)
        .where(SaleReturn.sale_id == sale_id, SaleReturn.status == "COMPLETED")
        .group_by(SaleReturnItem.sale_item_id)
    ).all()
    return {r[0]: int(r[1] or 0) for r in rows if r[0] is not None}


def refundable_lines(db: Session, sale: Sale) -> list[dict[str, Any]]:
    """Per-line sold / already-returned / remaining and the effective paid value."""
    returned = _returned_qty_by_line(db, sale.id)
    line_totals = sum((to_decimal(i.line_total) for i in sale.items), Decimal("0"))
    adjust = _sale_adjust(sale)
    out = []
    for line in sale.items:
        lt = to_decimal(line.line_total)
        share = (adjust * lt / line_totals) if line_totals else Decimal("0")
        effective = money(lt - share)
        sold = line.quantity
        already = returned.get(line.id, 0)
        remaining = max(0, sold - already)
        unit = (effective / sold) if sold else Decimal("0")
        out.append({
            "sale_item_id": line.id,
            "item_id": line.item_id,
            "batch_id": line.batch_id,
            "name": line.product_name,
            "batch_no": line.batch_no,
            "expiry": line.expiry_date.isoformat() if line.expiry_date else "",
            "sold": sold,
            "returned": already,
            "remaining": remaining,
            "unit_refund": str(money(unit)),
            "effective": str(effective),
            "rate": str(to_decimal(line.rate)),
        })
    return out


def refunded_total(db: Session, sale: Sale) -> Decimal:
    row = db.scalar(
        select(func.coalesce(func.sum(SaleReturn.total_refund), 0)).where(
            SaleReturn.sale_id == sale.id, SaleReturn.status == "COMPLETED"
        )
    )
    return money(to_decimal(row))


def refund_status(db: Session, sale: Sale) -> str:
    refunded = refunded_total(db, sale)
    if refunded <= 0:
        return "NONE"
    if refunded >= money(sale.total):
        return "FULL"
    return "PARTIAL"


def create_return(
    db: Session,
    sale: Sale,
    *,
    lines: list[dict[str, Any]],
    refund_method: str = "CASH",
    reason_code: str = "",
    reason_note: str = "",
    disposition: str = "RESTOCK",
    manager_threshold: Any = 0,
    allow_override: bool = False,
    user: User | None = None,
    ip_address: str = "",
) -> SaleReturn:
    if sale.payment_status == "CANCELLED":
        raise RefundError("A cancelled (voided) sale cannot be refunded")
    if not lines:
        raise RefundError("Select at least one item to return")
    if reason_code == "OTHER" and not (reason_note or "").strip():
        raise RefundError("A reason note is required when the reason is Other")
    if refund_method not in ("CASH", "UPI", "CARD"):
        raise RefundError("Unsupported refund method")

    refundable = {r["sale_item_id"]: r for r in refundable_lines(db, sale)}
    items_by_id = {i.id: i for i in sale.items}

    return_doc = SaleReturn(
        return_no=next_return_no(db),
        sale_id=sale.id,
        customer_id=sale.customer_id,
        business_date=business_time.current_business_date(db),
        refund_method=refund_method,
        reason_code=reason_code or "",
        reason_note=reason_note or "",
        status="COMPLETED",
        processed_by_user_id=user.id if user else None,
        processed_at=utcnow(),   # stored in UTC like every other timestamp
    )
    db.add(return_doc)
    db.flush()

    total = Decimal("0")
    for raw in lines:
        sale_item_id = int(raw.get("sale_item_id") or 0)
        qty = int(raw.get("quantity") or 0)
        if qty <= 0:
            continue
        info = refundable.get(sale_item_id)
        if info is None:
            raise RefundError("Invalid sale line in refund")
        if qty > info["remaining"]:
            raise RefundError(
                f"Cannot return {qty} of {info['name']}; only {info['remaining']} refundable"
            )
        line = items_by_id[sale_item_id]
        line_disposition = str(raw.get("disposition") or disposition or "RESTOCK").upper()
        if line_disposition not in ("RESTOCK", "DAMAGE", "EXPIRED", "NO_RESTOCK"):
            raise RefundError("Invalid stock disposition")
        # Refund from the original effective paid value, not today's price.
        refund_amount = money(
            to_decimal(info["effective"]) * qty / info["sold"]
        )
        db.add(
            SaleReturnItem(
                return_id=return_doc.id,
                sale_item_id=sale_item_id,
                item_id=line.item_id,
                batch_id=line.batch_id,
                product_name=line.product_name,
                batch_no=line.batch_no,
                expiry_date=line.expiry_date,
                quantity=qty,
                refund_amount=refund_amount,
                disposition=line_disposition,
            )
        )
        total += refund_amount

        if line_disposition == "RESTOCK" and line.batch_id:
            _restock_line(db, line, qty, return_doc, user=user)

    total = money(total)
    if not allow_override:
        if manager_threshold and total > to_decimal(manager_threshold):
            raise RefundError(
                f"Refunds above {money(manager_threshold)} require manager approval"
            )
        paid_with = {p.mode for p in sale.payments} or {sale.payment_mode}
        if refund_method not in paid_with and paid_with & {"CASH", "UPI", "CARD"}:
            raise RefundError(
                "The refund method must be one the bill was paid with unless a manager overrides it"
            )
    return_doc.total_refund = total
    db.flush()

    audit.record(
        db, action=audit.A_CREATE, entity_type="sale_return", entity_id=return_doc.return_no,
        user=user, after=audit.snapshot(return_doc),
        details=f"Refund {return_doc.return_no} against {sale.invoice_no}: {total} via {refund_method}",
        ip_address=ip_address,
    )
    return return_doc


def get_return(db: Session, return_id: int) -> SaleReturn | None:
    return db.scalar(
        select(SaleReturn)
        .where(SaleReturn.id == return_id)
        .options(selectinload(SaleReturn.items), selectinload(SaleReturn.sale))
    )


def list_returns(
    db: Session,
    *,
    sale_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
) -> list[SaleReturn]:
    stmt = select(SaleReturn)
    if sale_id:
        stmt = stmt.where(SaleReturn.sale_id == sale_id)
    if date_from:
        stmt = stmt.where(SaleReturn.business_date >= date_from)
    if date_to:
        stmt = stmt.where(SaleReturn.business_date <= date_to)
    return list(
        db.scalars(
            stmt.options(selectinload(SaleReturn.items)).order_by(SaleReturn.processed_at.desc()).limit(limit)
        )
    )


def return_payload(db: Session, ret: SaleReturn) -> dict[str, Any]:
    return {
        "id": ret.id,
        "return_no": ret.return_no,
        "sale_id": ret.sale_id,
        "invoice_no": ret.sale.invoice_no if ret.sale else "",
        "business_date": ret.business_date.isoformat(),
        "total_refund": str(money(ret.total_refund)),
        "refund_method": ret.refund_method,
        "reason_code": ret.reason_code,
        "reason_note": ret.reason_note,
        "status": ret.status,
        "processed_at": ret.processed_at.isoformat(sep=" ", timespec="seconds") if ret.processed_at else "",
        "processed_by": ret.sale.user.employee_id if ret.sale and ret.sale.user else "",
        "items": [
            {
                "product_name": i.product_name,
                "batch_no": i.batch_no,
                "quantity": i.quantity,
                "refund_amount": str(money(i.refund_amount)),
                "disposition": i.disposition,
            }
            for i in ret.items
        ],
    }


def _restock_line(db: Session, line, qty: int, return_doc, *, user) -> None:
    """Returned goods go back into the exact batch they were sold from,
    as ``SALE_RETURN`` movements linked to the original sale movements."""
    from app.models import InventoryMovement
    from app.services import stock_ledger

    refs = dict(movement_type="SALE_RETURN", reason=f"Return {return_doc.return_no}", user=user,
                reference_type="SALE_RETURN", reference_id=return_doc.id, reference_no=return_doc.return_no)
    moves = db.scalars(
        select(InventoryMovement).where(
            InventoryMovement.reference_type == "SALE_ITEM", InventoryMovement.reference_id == line.id,
            InventoryMovement.movement_type == "SALE",
        ).order_by(InventoryMovement.id)
    )
    need = qty
    for move in moves:
        left = abs(move.quantity) - stock_ledger.reversed_quantity(db, move)
        take = min(left, need)
        if take > 0:
            stock_ledger.reverse(db, move, quantity=take, **refs)
            need -= take
        if not need:
            return
    batch = db.get(Batch, line.batch_id)
    if batch is not None and need > 0:  # bill from before the ledger existed
        refs.pop("movement_type")
        stock_ledger.post(db, batch, "SALE_RETURN", need, **refs)
