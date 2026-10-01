"""Sales / billing business logic.

Every sale is atomic: either all line items debit stock and the sale row is
written, or nothing is. Quantities are base units (tablets for a loose strip
product, bottles for a syrup). Stock is allocated FEFO across unexpired
batches unless the cashier picked a batch; one cashier line that spans
batches becomes one sale row per batch so each is charged its own MRP.
Prices come from the batch MRP only — never from the client, never from
purchase rates, and no GST is added. Cancellations post reversing ledger
movements; nothing is deleted from the stock ledger.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.models import Batch, InventoryMovement, Item, Sale, SaleItem, SalePayment, User
from app.sequences import next_invoice_no
from app.services import business_time, customer_service, stock_ledger, units, financials, uom_service
from app.utils import money, to_decimal, to_local, utcnow


class SaleError(Exception):
    pass


def round_off(total: Decimal, mode: str = "NEAREST_RUPEE") -> tuple[Decimal, Decimal]:
    total = money(total)
    if mode == "NONE":
        return total, Decimal("0.00")
    if mode == "NEAREST_HALF":
        nearest = (total * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2
    else:  # NEAREST_RUPEE
        nearest = total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return money(nearest), money(nearest - total)


PAYMENT_MODES = ("CASH", "UPI", "CARD")


DEFAULT_MAX_DISCOUNT_PCT = Decimal("20")


def max_discount_pct(db: Session) -> Decimal:
    """Store policy: the most any line, or the whole bill, may be discounted (default 20%)."""
    from app.services.settings_service import get_setting

    try:
        pct = Decimal(str(get_setting(db, "max_discount_pct", str(DEFAULT_MAX_DISCOUNT_PCT)) or DEFAULT_MAX_DISCOUNT_PCT))
    except Exception:
        pct = DEFAULT_MAX_DISCOUNT_PCT
    return min(max(pct, Decimal("0")), Decimal("100"))


def _pct(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _line_discount(line: dict[str, Any], gross: Decimal, name: str, cap: Decimal = Decimal("100")) -> Decimal:
    """Per-line discount: ``discount_pct`` wins over a rupee ``discount``; neither may exceed ``cap``%."""
    pct_raw = line.get("discount_pct")
    if pct_raw not in (None, ""):
        pct = to_decimal(pct_raw)
        if pct < 0:
            raise SaleError(f"Discount for {name} cannot be negative")
        if pct > cap:
            raise SaleError(f"Discount on {name} is {_pct(pct)}% — the maximum allowed is {_pct(cap)}%")
        return money(gross * pct / 100)
    amount = to_decimal(line.get("discount") or 0)
    if amount < 0:
        raise SaleError(f"Discount for {name} cannot be negative")
    if amount > money(gross * cap / 100):
        raise SaleError(f"Discount on {name} (₹{money(amount)}) is more than {_pct(cap)}% of ₹{money(gross)} "
                        f"— the maximum is ₹{money(gross * cap / 100)}")
    return money(amount)


def _line_quantity(line: dict[str, Any], item: Item, db: Session) -> int:
    raw = line.get("quantity")
    try:
        if line.get("uom"):
            return uom_service.to_base(db,item,raw,line["uom"])[0]
        if isinstance(raw, bool):
            raise units.UnitError("bad")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        return units.parse_qty_expression(raw, item.units_per_pack or 1)
    except (units.UnitError, TypeError, ValueError, ArithmeticError):
        raise SaleError(f"Invalid quantity for {item.name}")


def _split(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split ``amount`` over ``weights`` in paise; the last part takes the remainder."""
    total = sum(weights, Decimal("0"))
    if not total or not amount:
        return [Decimal("0.00")] * len(weights)
    parts = [money(amount * w / total) for w in weights[:-1]]
    return parts + [money(amount - sum(parts, Decimal("0")))]


def _add_manual_lines(db: Session, sale: Sale, lines: list[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """Typed lines for a manual bill: name, whole quantity, unit rate, optional discount %."""

    cap = max_discount_pct(db)
    subtotal = gross_total = Decimal("0")
    for line_no, line in enumerate(lines, start=1):
        if line.get("item_id") or line.get("batch_id"):
            raise SaleError("A manual bill cannot take stock items — bill them from inventory")
        name = " ".join(str(line.get("name") or "").split())[:250]
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
        gross = money(rate * qty)
        disc = _line_discount(line, gross, name, cap)
        gross_total += gross
        sale_item = SaleItem(
            sale_id=sale.id, item_id=None, batch_id=None, product_name=name,
            batch_no=str(line.get("batch") or "")[:60], quantity=qty, mrp=money(rate), rate=money(rate),
            pack_mrp=money(rate), units_per_pack=1, pack_size=str(line.get("pack") or "")[:60], base_unit="UNIT",
            cost_rate=Decimal("0"), discount=disc, line_total=money(gross - disc), line_no=line_no,
            financial_status=financials.MISSING, financial_cost_source="MANUAL_BILL",
            sale_uom="UNIT", sale_uom_factor=1, sale_quantity=Decimal(qty),
        )
        db.add(sale_item)
        subtotal += sale_item.line_total
    db.flush()
    db.refresh(sale, ["items"])
    return money(subtotal), money(gross_total)


def _add_lines(db: Session, sale: Sale, lines: list[dict[str, Any]], *, user, ip_address: str) -> tuple[Decimal, Decimal]:
    """Allocate stock, post SALE movements and write the sale rows.

    Returns ``(subtotal after line discounts, gross MRP value)``.
    """
    cap = max_discount_pct(db)
    gross_total = Decimal("0")
    if not lines:
        raise SaleError("A sale must contain at least one line item")
    subtotal = Decimal("0")
    for line_no, line in enumerate(lines, start=1):
        item = db.get(Item, line.get("item_id"))
        if item is None:
            raise SaleError(f"Item {line.get('item_id')} not found")
        quantity = _line_quantity(line, item, db)
        try:
            units.check_sale_quantity(
                quantity, units_per_pack=item.units_per_pack or 1, loose_sale=bool(item.loose_sale),
                pack_unit=item.pack_unit, base_unit=item.base_unit, name=item.name,
            )
            chosen = int(line.get("batch_id") or 0) or None
            plan = stock_ledger.allocate(db, item, quantity, batch_id=chosen)
        except (units.UnitError, stock_ledger.StockError) as exc:
            raise SaleError(str(exc))
        if chosen:
            fefo = stock_ledger.sellable_batches(db, item.id)
            if fefo and fefo[0].id != chosen:
                audit.record(
                    db, action=audit.A_UPDATE, entity_type="sale", entity_id=sale.invoice_no, user=user,
                    after={"item": item.article_id, "batch": plan[0].batch.batch_no,
                           "fefo_batch": fefo[0].batch_no},
                    details=f"Manual batch override for {item.name}: {plan[0].batch.batch_no} "
                            f"instead of FEFO {fefo[0].batch_no}",
                    ip_address=ip_address,
                )

        grosses = []
        for part in plan:
            if not part.batch.mrp or part.batch.mrp <= 0:
                raise SaleError(
                    f"Batch {part.batch.batch_no or '(no batch)'} of {item.name} has no MRP. "
                    f"Set its MRP in Inventory before selling."
                )
            grosses.append(units.line_amount(part.batch.mrp, part.batch.units_per_pack or 1, part.quantity))
        line_gross = money(sum(grosses, Decimal("0")))
        line_discount = _line_discount(line, line_gross, item.name, cap)
        gross_total += line_gross
        if line_discount > line_gross:
            raise SaleError(f"Discount exceeds line value for {item.name}")
        discounts = _split(line_discount, grosses)

        for part, gross, disc in zip(plan, grosses, discounts):
            batch = part.batch
            upp = batch.units_per_pack or 1
            sale_item = SaleItem(
                sale_id=sale.id,
                item_id=item.id,
                batch_id=batch.id,
                product_name=item.name,
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                quantity=part.quantity,
                mrp=units.display_unit_price(batch.mrp, upp),
                rate=units.display_unit_price(batch.mrp, upp),
                cost_rate=to_decimal(batch.unit_cost),
                discount=disc,
                line_total=money(gross - disc),
                pack_mrp=to_decimal(batch.mrp),
                units_per_pack=upp,
                pack_size=item.pack_size or "",
                base_unit=item.base_unit or "UNIT",
                line_no=line_no,
            )
            sale_item.sale_uom=str(line.get("uom") or item.base_unit or "BASE").upper()
            sale_item.sale_uom_factor=uom_service.factor(item,line["uom"],uom_service.definitions(db,item)) if line.get("uom") else 1
            sale_item.sale_quantity=Decimal(part.quantity)/sale_item.sale_uom_factor
            financials.snapshot_allocation(sale_item, batch)
            if sale_item.financial_status != financials.RESOLVED:
                from app.services.settings_service import get_setting
                if get_setting(db,"require_sale_cost","false").lower() in ("true","1","yes"):
                    raise SaleError(f"Purchase cost missing for {item.name}, batch {batch.batch_no}; verify its cost before selling")
            db.add(sale_item)
            db.flush()
            try:
                stock_ledger.post(
                    db, batch, "SALE", part.quantity, reference_type="SALE_ITEM",
                    reference_id=sale_item.id, reference_no=sale.invoice_no,
                    reason=f"Sale {sale.invoice_no}", user=user,
                )
            except stock_ledger.StockError as exc:
                raise SaleError(str(exc))
            subtotal += sale_item.line_total
    return money(subtotal), money(gross_total)


def _normalize_payments(payments: list[dict[str, Any]] | None, payment_mode: str, total: Decimal) -> list[dict]:
    """Validated ``[{mode, amount, reference}]`` that adds up to the bill exactly."""
    if not payments:
        mode = (payment_mode or "CASH").upper()
        if mode not in PAYMENT_MODES:
            raise SaleError(f"Unknown payment method {payment_mode!r}")
        return [{"mode": mode, "amount": total, "reference": ""}]
    merged: dict[str, dict] = {}
    for part in payments:
        mode = str(part.get("mode") or "").upper()
        if mode not in PAYMENT_MODES:
            raise SaleError(f"Unknown payment method {part.get('mode')!r}")
        amount = money(to_decimal(part.get("amount") or 0))
        if amount < 0:
            raise SaleError("A payment amount cannot be negative")
        if amount == 0:
            continue
        entry = merged.setdefault(mode, {"mode": mode, "amount": Decimal("0.00"), "reference": ""})
        entry["amount"] = money(entry["amount"] + amount)
        ref = str(part.get("reference") or "").strip()[:80]
        if ref:
            entry["reference"] = ref
    rows = list(merged.values())
    paid = money(sum((r["amount"] for r in rows), Decimal("0")))
    if paid != money(total):
        raise SaleError(f"Payments add up to {paid} but the bill is {money(total)}")
    if not rows:  # a zero-value bill
        rows = [{"mode": "CASH", "amount": Decimal("0.00"), "reference": ""}]
    return rows


def _apply_totals_and_payments(
    db: Session,
    sale: Sale,
    subtotal: Decimal,
    *,
    gross: Decimal | None = None,
    discount: Any,
    discount_pct: Any = None,
    voucher: Any,
    round_off_mode: str,
    payment_mode: str,
    payments: list[dict] | None,
    cash_received: Any,
) -> Decimal:
    """Set totals, payment rows, tendered/change. Returns the cash part of the bill."""
    cap = max_discount_pct(db)
    if discount_pct not in (None, ""):
        # the bill discount is always a percentage of the bill after item discounts
        pct = to_decimal(discount_pct)
        if pct < 0:
            raise SaleError("Bill discount cannot be negative")
        if pct > cap:
            raise SaleError(f"Bill discount is {_pct(pct)}% — the maximum allowed is {_pct(cap)}%")
        discount = money(subtotal * pct / 100)
    discount = to_decimal(discount)
    voucher = to_decimal(voucher)
    if discount < 0 or voucher < 0:
        raise SaleError("Discount cannot be negative")
    bill_cap = money(subtotal * cap / 100)
    if discount > bill_cap:
        raise SaleError(f"Bill discount ₹{money(discount)} is more than {_pct(cap)}% of ₹{money(subtotal)} "
                        f"— the maximum is ₹{bill_cap}")
    if gross is not None and gross > 0:
        # line + bill discounts together stay within the same limit of the MRP value
        # (a paisa of rounding per line is tolerated)
        total_off = money(gross - subtotal + discount)
        total_cap = money(gross * cap / 100) + Decimal("0.01") * len(sale.items or [])
        if total_off > total_cap:
            raise SaleError(f"Total discount ₹{total_off} is more than {_pct(cap)}% of the MRP value ₹{money(gross)} "
                            f"— the maximum is ₹{money(gross * cap / 100)}")
    net = money(subtotal - discount - voucher)
    if net < 0:
        raise SaleError("Discount/voucher exceeds the sale value")
    rounded_total, delta = round_off(net, round_off_mode)
    sale.subtotal = subtotal
    sale.discount = money(discount)
    sale.voucher = money(voucher)
    sale.round_off = delta
    sale.total = rounded_total

    rows = _normalize_payments(payments, payment_mode, rounded_total)
    sale.payment_mode = rows[0]["mode"] if len(rows) == 1 else "SPLIT"
    for row in rows:
        db.add(SalePayment(sale_id=sale.id, mode=row["mode"], amount=row["amount"], reference=row["reference"]))

    # Cash tender / change apply to the cash part only (a split bill may be
    # ₹300 cash + ₹200 UPI: the customer hands over ₹500 for the ₹300).
    cash_part = money(sum((r["amount"] for r in rows if r["mode"] == "CASH"), Decimal("0")))
    if cash_part > 0 or sale.payment_mode == "CASH":
        tendered = money(cash_received) if cash_received not in (None, "") else cash_part
        if tendered < cash_part:
            shortfall = money(cash_part - tendered)
            raise SaleError(f"Insufficient cash received. {shortfall} more is required")
        sale.tendered_amount = tendered
        sale.change_amount = money(tendered - cash_part)
    else:
        sale.tendered_amount = None
        sale.change_amount = None
    return cash_part


def create_sale(
    db: Session,
    *,
    lines: list[dict[str, Any]],
    user: User | None = None,
    customer_id: int | None = None,
    customer_type: str = "WALK_IN",
    payment_mode: str = "CASH",
    payments: list[dict[str, Any]] | None = None,
    discount: Any = 0,
    discount_pct: Any = None,
    voucher: Any = 0,
    notes: str = "",
    round_off_mode: str = "NEAREST_RUPEE",
    ip_address: str = "",
    sale_date=None,
    cash_received: Any = None,
    client_request_id: str | None = None,
    invoice_type: str = "INVENTORY",
) -> Sale:
    """Record a bill. ``payments`` splits it across methods (cash/UPI/card).

    ``invoice_type="MANUAL"``: a manual bill of typed lines (items not kept in
    inventory). It never touches the stock ledger and its cost is unknown, so
    reports show no cost or profit for it rather than inventing one."""
    invoice_type = (invoice_type or "INVENTORY").upper()
    if invoice_type not in ("INVENTORY", "MANUAL"):
        raise SaleError("Invoice type must be INVENTORY or MANUAL")
    if not lines:
        raise SaleError("A sale must contain at least one line item")

    # Idempotency: a retried submission returns the already-created sale.
    if client_request_id:
        existing = db.scalar(select(Sale).where(Sale.client_request_id == client_request_id))
        if existing is not None:
            return existing

    # the invoice number carries the pharmacy's local date, not the UTC date
    local_now = to_local(sale_date or utcnow(), business_time.timezone_name(db))
    from app.sequences import next_manual_bill_no

    sale = Sale(
        invoice_no=next_manual_bill_no(db, local_now) if invoice_type == "MANUAL" else next_invoice_no(db, local_now),
        invoice_type=invoice_type,
        customer_id=customer_id,
        customer_type=customer_service.normalize_customer_type(customer_type),
        invoice_format="STUDIO",
        user_id=user.id if user else None,
        sale_date=sale_date or utcnow(),
        payment_mode=(payment_mode or "CASH").upper(),
        notes=notes,
        client_request_id=client_request_id,
    )
    db.add(sale)
    db.flush()

    if invoice_type == "MANUAL":
        subtotal, gross = _add_manual_lines(db, sale, lines)
    else:
        subtotal, gross = _add_lines(db, sale, lines, user=user, ip_address=ip_address)
    _apply_totals_and_payments(
        db, sale, subtotal, gross=gross, discount=discount, discount_pct=discount_pct, voucher=voucher, round_off_mode=round_off_mode,
        payment_mode=payment_mode, payments=payments, cash_received=cash_received,
    )
    db.flush()

    financials.finalize(sale)
    db.flush()

    audit.record(
        db,
        action=audit.A_CREATE,
        entity_type="sale",
        entity_id=sale.invoice_no,
        user=user,
        after=audit.snapshot(sale),
        details=f"Sale {sale.invoice_no} total {sale.total} via {payment_label(sale)}",
        ip_address=ip_address,
    )
    return sale


def payment_label(sale: Sale) -> str:
    """``Cash`` / ``UPI`` / ``Cash ₹300.00 + UPI ₹200.00``."""
    names = {"CASH": "Cash", "UPI": "UPI", "CARD": "Card"}
    parts = list(sale.payments or [])
    if len(parts) <= 1:
        mode = parts[0].mode if parts else (sale.payment_mode or "CASH")
        return names.get(mode, mode.title())
    return " + ".join(f"{names.get(p.mode, p.mode)} ₹{money(p.amount)}" for p in parts)


def _sale_movements(db: Session, line: SaleItem) -> list[InventoryMovement]:
    return list(db.scalars(
        select(InventoryMovement).where(
            InventoryMovement.reference_type == "SALE_ITEM",
            InventoryMovement.reference_id == line.id,
            InventoryMovement.movement_type == "SALE",
        ).order_by(InventoryMovement.id)
    ))


def _restore_sale_stock(db: Session, sale: Sale, *, user, ip_address: str, reason: str = "") -> None:
    """Put a bill's stock back with reversing ``SALE_CANCEL`` movements."""
    note = reason or f"Cancel {sale.invoice_no}"
    for line in sale.items:
        if not line.batch_id:
            continue
        moves = _sale_movements(db, line)
        if moves:
            for move in moves:
                if stock_ledger.reversed_quantity(db, move) < abs(move.quantity):
                    stock_ledger.reverse(db, move, movement_type="SALE_CANCEL", reason=note, user=user)
            continue
        # bill from before the ledger existed: post the return without a link
        batch = db.get(Batch, line.batch_id)
        if batch is not None and line.quantity > 0:
            stock_ledger.post(db, batch, "SALE_CANCEL", line.quantity, reference_type="SALE_ITEM",
                              reference_id=line.id, reference_no=sale.invoice_no, reason=note, user=user)


def editable_problem(db: Session, sale: Sale) -> str:
    """Why this invoice cannot be changed, or "" when it can."""
    if sale.payment_status == "CANCELLED":
        return "This invoice is void"
    from app.services import refund_service

    if refund_service.refunded_total(db, sale) > 0:
        return "Items on this invoice were returned; record another return instead of editing"
    return ""




def get_sale(db: Session, sale_id: int) -> Sale | None:
    return db.scalar(
        select(Sale).where(Sale.id == sale_id).options(selectinload(Sale.items), selectinload(Sale.customer))
    )


def _sales_conditions(
    db: Session,
    *,
    q: str = "",
    start: Any = None,
    end: Any = None,
    user_id: int | None = None,
    payment: str = "",
    status: str = "",
    customer_type: str = "",
) -> list:
    from sqlalchemy import exists, or_

    from app.models import Customer, SaleReturn

    conds = []
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        digits = "".join(ch for ch in text if ch.isdigit())
        matches = [Sale.invoice_no.ilike(like), Customer.name.ilike(like), Customer.customer_id.ilike(like)]
        if len(digits) >= 3:
            bare = Customer.mobile
            for sep in (" ", "-", "+"):
                bare = func.replace(bare, sep, "")
            matches.append(bare.like(f"%{digits}%"))
        conds.append(
            or_(Sale.invoice_no.ilike(like),
                exists().where(Customer.id == Sale.customer_id, or_(*matches[1:])))
        )
    if start:
        conds.append(Sale.sale_date >= start)
    if end:
        conds.append(Sale.sale_date < end)
    if user_id:
        conds.append(Sale.user_id == user_id)
    mode = (payment or "").upper()
    if mode == "SPLIT":
        conds.append(Sale.payment_mode == "SPLIT")
    elif mode in PAYMENT_MODES:
        # "paid by cash" includes split bills with a cash part. Aliased so it
        # stays a proper subquery inside queries that also select payments.
        from sqlalchemy.orm import aliased

        part = aliased(SalePayment)
        conds.append(select(part.id).where(part.sale_id == Sale.id, part.mode == mode).exists())
    from sqlalchemy.orm import aliased as _aliased

    ret = _aliased(SaleReturn)
    has_return = select(ret.id).where(ret.sale_id == Sale.id).exists()
    if status == "void":
        conds.append(Sale.payment_status == "CANCELLED")
    elif status == "returned":
        conds.append(has_return)
    elif status == "paid":
        conds.append(Sale.payment_status != "CANCELLED")
    if customer_type in ("WALK_IN", "HOME_DELIVERY"):
        conds.append(Sale.customer_type == customer_type)
    return conds


def search_sales(
    db: Session,
    *,
    q: str = "",
    start: Any = None,
    end: Any = None,
    user_id: int | None = None,
    payment: str = "",
    status: str = "",
    customer_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Sale], int]:
    """Invoices matching invoice no., customer name / ID / mobile and filters (newest first)."""
    conds = _sales_conditions(
        db, q=q, start=start, end=end, user_id=user_id,
        payment=payment, status=status, customer_type=customer_type,
    )
    total = db.scalar(select(func.count(Sale.id)).where(*conds)) or 0
    stmt = (
        select(Sale)
        .where(*conds)
        .options(selectinload(Sale.items), selectinload(Sale.payments), selectinload(Sale.customer), selectinload(Sale.user))
        .order_by(Sale.sale_date.desc(), Sale.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt)), total


def sales_summary(db: Session, **filters: Any) -> dict:
    """Totals for everything matching the filters (not just one page).

    Void bills are counted separately and never included in money totals.
    """
    conds = _sales_conditions(db, **filters)
    live = conds + [Sale.payment_status != "CANCELLED"]
    row = db.execute(
        select(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0), func.coalesce(func.sum(Sale.discount), 0))
        .where(*live)
    ).one()
    void = db.scalar(select(func.count(Sale.id)).where(*conds, Sale.payment_status == "CANCELLED")) or 0
    by_mode = {
        mode: money(amount)
        for mode, amount in db.execute(
            select(SalePayment.mode, func.coalesce(func.sum(SalePayment.amount), 0))
            .join(Sale, Sale.id == SalePayment.sale_id)
            .where(*live)
            .group_by(SalePayment.mode)
        )
    }
    from app.models import SaleReturn

    refunded = db.scalar(
        select(func.coalesce(func.sum(SaleReturn.total_refund), 0))
        .join(Sale, Sale.id == SaleReturn.sale_id)
        .where(*live)
    ) or 0
    count, total, discount = row
    return {
        "bills": count,
        "gross": money(total),
        "net": money(Decimal(total) - Decimal(refunded)),  # after refunds paid out
        "discount": money(discount),
        "void": void,
        "refunded": money(refunded),
        "average": money(Decimal(total) / count) if count else Decimal("0.00"),
        "by_mode": {m: by_mode.get(m, Decimal("0.00")) for m in PAYMENT_MODES},
    }


def _line_audit(sale: Sale) -> list[dict]:
    return [{"product": i.product_name, "batch": i.batch_no, "qty": i.quantity, "rate": str(i.rate), "discount": str(i.discount),
             "cost_rate": str(i.cost_rate), "units_per_pack": i.units_per_pack} for i in sale.items]


def amend_sale(
    db: Session,
    sale: Sale,
    *,
    lines: list[dict[str, Any]],
    user: User | None = None,
    customer_id: int | None = None,
    customer_type: str = "WALK_IN",
    payment_mode: str = "CASH",
    payments: list[dict[str, Any]] | None = None,
    discount: Any = 0,
    discount_pct: Any = None,
    voucher: Any = 0,
    notes: str | None = None,
    round_off_mode: str = "NEAREST_RUPEE",
    cash_received: Any = None,
    reason: str = "",
    ip_address: str = "",
) -> Sale:
    """Edit a completed bill in place, keeping its number — linked to the ledger.

    Every old line's stock goes back to its exact batch with ``SALE_CANCEL``
    movements, then the new lines are sold with the same rules as a new bill
    (FEFO, discount cap, payments) in the same transaction; the full
    before / after is audited. Manual-bill lines never touch stock. A bill
    with returns or a voided bill cannot be edited.
    """
    problem = editable_problem(db, sale)
    if problem:
        raise SaleError(problem)
    old_total = money(sale.total)
    before = {**audit.snapshot(sale), "lines": _line_audit(sale),
              "payments": [{"mode": p.mode, "amount": str(p.amount)} for p in sale.payments]}

    _restore_sale_stock(db, sale, user=user, ip_address=ip_address, reason=f"Edit of {sale.invoice_no}")
    for line in list(sale.items):
        db.delete(line)
    for part in list(sale.payments):
        db.delete(part)
    db.flush()
    db.expire(sale, ["items", "payments"])

    sale.customer_id = customer_id
    sale.customer_type = customer_service.normalize_customer_type(customer_type)
    if notes is not None:
        sale.notes = notes
    if (sale.invoice_type or "INVENTORY") == "MANUAL":
        subtotal, gross = _add_manual_lines(db, sale, lines)
    else:
        subtotal, gross = _add_lines(db, sale, lines, user=user, ip_address=ip_address)
    _apply_totals_and_payments(
        db, sale, subtotal, gross=gross, discount=discount, discount_pct=discount_pct, voucher=voucher,
        round_off_mode=round_off_mode, payment_mode=payment_mode, payments=payments, cash_received=cash_received,
    )
    db.flush()
    financials.finalize(sale)
    db.flush()
    db.refresh(sale)
    after = {**audit.snapshot(sale), "lines": _line_audit(sale),
             "payments": [{"mode": p.mode, "amount": str(p.amount)} for p in sale.payments]}
    audit.record(
        db, action=audit.A_UPDATE, entity_type="sale", entity_id=sale.invoice_no, user=user, before=before, after=after,
        details=f"Invoice {sale.invoice_no} edited: ₹{old_total} → ₹{money(sale.total)}" + (f" ({reason})" if reason else ""),
        ip_address=ip_address,
    )
    return sale


def void_sale(db: Session, sale: Sale, *, user: User | None = None, reason: str = "", ip_address: str = "") -> None:
    """Cancel a sale and return stock to the affected batches (audited)."""
    if sale.payment_status == "CANCELLED":
        raise SaleError("Sale is already cancelled")
    from app.services import refund_service

    if refund_service.refunded_total(db, sale) > 0:
        raise SaleError("Items on this invoice were already returned; return the remaining items instead of voiding")
    before = audit.snapshot(sale)
    _restore_sale_stock(db, sale, user=user, ip_address=ip_address, reason=f"Void of {sale.invoice_no}: {reason}".rstrip(": "))
    sale.payment_status = "CANCELLED"
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="sale",
        entity_id=sale.invoice_no,
        user=user,
        before=before,
        after=audit.snapshot(sale),
        details=f"Sale voided: {reason}",
        ip_address=ip_address,
    )
