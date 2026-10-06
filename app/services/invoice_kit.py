"""Map a saved Sale to the customer-invoice kit's `InvoiceData` contract (INR paise).

The kit is presentation-only: it never recalculates tax or totals and validates that
its own numbers balance. This module is the single place that translates the app's
saved invoice (rupee Decimals) into the kit's integer-paise model, allocating the
invoice-wide discount/voucher across lines so the kit's consistency checks hold.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CUSTOMER_TYPE_LABELS, Item, Sale
from app.services import settings_service
from app.utils import to_local

PAYMENT_LABELS = {"CASH": "Cash", "UPI": "UPI", "CARD": "Card", "SPLIT": "Split", "UDHAAR": "Udhaar"}
BRAND_LOGO = "/static/brand/svg/symbol-color.svg"
BRAND_LOGO_MONO = "/static/brand/svg/symbol-dark.svg"


def _paise(value) -> int:
    if value is None:
        return 0
    return int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
    "Eighteen", "Nineteen",
]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return _ONES[n // 100] + " Hundred" + (" " + _words(n % 100) if n % 100 else "")
    if n < 100000:
        return _words(n // 1000) + " Thousand" + (" " + _words(n % 1000) if n % 1000 else "")
    if n < 10000000:
        return _words(n // 100000) + " Lakh" + (" " + _words(n % 100000) if n % 100000 else "")
    return _words(n // 10000000) + " Crore" + (" " + _words(n % 10000000) if n % 10000000 else "")


def amount_words(paise: int) -> str:
    rupees, paise = divmod(int(paise), 100)
    text = "Rupees " + (_words(rupees) or "Zero")
    if paise:
        text += " and " + _words(paise) + " Paise"
    return text + " Only"


def _allocate(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` across ``weights`` proportionally, never exceeding a weight."""
    shares = [0] * len(weights)
    capacity = sum(weights)
    if total <= 0 or capacity <= 0:
        return shares
    total = min(total, capacity)
    remaining = total
    for i, w in enumerate(weights):
        if remaining <= 0:
            break
        if w <= 0:
            continue
        share = min(total * w // capacity, remaining, w)
        shares[i] = share
        remaining -= share
    i = 0
    while remaining > 0 and i < len(weights):
        gap = weights[i] - shares[i]
        if gap > 0:
            add = min(gap, remaining)
            shares[i] += add
            remaining -= add
        i += 1
    return shares


def build_invoice_view(db: Session, sale: Sale) -> dict:
    profile = settings_service.get_profile(db) or {}
    # printed date/time are the pharmacy's wall clock (sale_date is stored in UTC)
    local_date = to_local(sale.sale_date, settings_service.get_setting(db, "timezone", "Asia/Kolkata"))

    item_ids = [it.item_id for it in sale.items if it.item_id]
    items_by_id = {}
    if item_ids:
        items_by_id = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(item_ids)))}

    rows: list[dict] = []
    gross_total = 0
    for it in sale.items:
        unit_price = _paise(it.rate)
        qty = int(it.quantity or 0)
        # the value actually charged: loose units are priced from the pack MRP, so
        # the rounded per-unit rate x qty can be a paisa or two off the bill
        gross = _paise((it.line_total or 0) + (it.discount or 0)) if it.line_total is not None else unit_price * qty
        gross_total += gross
        meta = items_by_id.get(it.item_id)
        rows.append({
            "unit_price": unit_price,
            "qty": qty,
            "gross": gross,
            # the discount actually given on this line (per-item discount %)
            "line_discount": min(gross, max(0, _paise(it.discount))),
            "batch_no": (it.batch_no or "").strip(),
            "item": {
                "name": it.product_name or (meta.name if meta else "Item"),
                "detail": (meta.generic_name.strip() if meta and meta.generic_name else None),
                "hsn": (meta.hsn_code.strip() if meta and meta.hsn_code else None),
                "pack": (meta.pack_size.strip() if meta and meta.pack_size else None),
                "mfr": (meta.manufacturer.strip() if meta and meta.manufacturer else None),
                "articleId": (meta.article_id if meta else None),
                "quantity": qty,
                "unit": (meta.unit if meta and meta.unit else "units"),
                "expiry": it.expiry_date.strftime("%m-%Y") if it.expiry_date else None,
                "mrpPaise": _paise(it.mrp),
            },
        })

    total = _paise(sale.total)
    rounding = _paise(sale.round_off)
    subtotal = gross_total
    discount_total = max(0, min(subtotal, subtotal - total + rounding))
    # Each line keeps its own discount; only the bill-level remainder is spread
    # across lines in proportion to what is left of their value.
    line_discounts = [r["line_discount"] for r in rows]
    bill_share = _allocate(
        max(0, discount_total - sum(line_discounts)),
        [r["gross"] - d for r, d in zip(rows, line_discounts)],
    )
    shares = [d + b for d, b in zip(line_discounts, bill_share)]
    items = []
    for row, share in zip(rows, shares):
        item = row["item"]
        item["unitPricePaise"] = row["unit_price"]
        item["discountPaise"] = share
        item["lineTotalPaise"] = row["gross"] - share
        # v2 engine item keys (paise for money; article id rides the batch column).
        item["manufacturer"] = item.get("mfr")
        # The bill format prints this column as "Article ID" (see invoice-settings.js).
        item["batch"] = item.get("articleId")
        item["batchNo"] = row["batch_no"]
        item["free"] = 0
        item["mrp"] = item.get("mrpPaise", 0)
        item["rate"] = row["unit_price"]
        item["discountPercent"] = round(share / row["gross"] * 100, 2) if row["gross"] else 0
        item["gstPercent"] = 0
        item["taxablePaise"] = row["gross"] - share
        item["taxPaise"] = 0
        item["amount"] = row["gross"] - share
        # printed per line: qty x rate before discount; the discount appears
        # once in the totals so the customer sees it as a single figure
        item["displayAmount"] = row["gross"]
        items.append(item)

    # v2 contract: paid is the amount applied to the bill (cash change is handed
    # back, not owed), so total - paid == due. The v1 "change" value is kept for
    # the legacy renderer only.
    change = _paise(sale.change_amount) if sale.change_amount is not None else 0
    parts = list(getattr(sale, "payments", None) or [])
    # an Udhaar part is owed, not paid: it is the invoice's balance due, with its due date
    owed = sum(_paise(p.amount) for p in parts if p.mode == "UDHAAR")
    paid = total - owed
    due = max(0, total - paid)
    due_date = local_date
    udhaar_note = ""
    if owed and (sale.invoice_type or "INVENTORY") != "MANUAL":
        from app.services import udhaar_service

        entry = udhaar_service.entry_for_sale(db, sale.id)
        if entry is not None:
            due_date = entry.due_date
            udhaar_note = f"Udhaar: ₹{owed / 100:.2f} to be paid by {entry.due_date:%d %b %Y}."
    if len(parts) > 1:
        method = " + ".join(PAYMENT_LABELS.get(p.mode, p.mode) for p in parts)
    else:
        method = PAYMENT_LABELS.get(parts[0].mode if parts else sale.payment_mode, sale.payment_mode or "Cash")

    registrations = []
    if profile.get("show_gst", "1") == "1" and profile.get("gst_number"):
        registrations.append({"label": "GSTIN", "value": profile["gst_number"]})
    if profile.get("show_drug_license", "1") == "1" and profile.get("drug_license_number"):
        registrations.append({"label": "Drug License", "value": profile["drug_license_number"]})

    contact = " · ".join(
        part for part in (
            profile.get("contact_numbers"),
            profile.get("pharmacy_email"),
            profile.get("pharmacy_website"),
        ) if part
    )

    customer = sale.customer
    address = ""
    if customer is not None:
        address = ", ".join(
            part for part in (customer.address, customer.city, customer.pincode) if part
        )

    notes = []
    if udhaar_note:
        notes.append(udhaar_note)
    if sale.notes:
        notes.append(sale.notes)
    if (sale.customer_type or "WALK_IN") == "HOME_DELIVERY":
        notes.append("Home Delivery order.")
    notes.append(
        "This is a computer-generated invoice. "
        f"Category: {CUSTOMER_TYPE_LABELS.get(sale.customer_type or 'WALK_IN', 'Walk-In')}."
    )

    if settings_service.logo_file(db) is not None:
        logo, logo_mono = "/assets/logo.png", "/assets/logo.png"
    else:
        logo, logo_mono = BRAND_LOGO, BRAND_LOGO_MONO

    if paid and due == 0:
        status = "Paid in full"
    elif paid > 0:
        status = "Part paid"
    else:
        status = "Unpaid"
    payment_text = f"{method} · {status}"

    return {
        "sample": False,
        "number": sale.invoice_no,
        "date": local_date.strftime("%d/%m/%Y"),
        "dueDate": due_date.strftime("%d/%m/%Y"),
        "reference": "",
        "issuedDate": local_date.strftime("%d %b %Y"),
        "issuedTime": local_date.strftime("%H:%M"),
        "currency": "INR",
        "paymentText": payment_text,
        "amountWords": amount_words(total),
        "cashier": (
            (sale.user.full_name or sale.user.username)
            if getattr(sale, "user", None) is not None
            else ""
        ),
        "business": {
            "name": profile.get("pharmacy_name") or "Pharmacy",
            "tagline": profile.get("tagline") or "",
            "address": profile.get("address") or "",
            "contact": contact,
            "logo": logo,
            "logoMono": logo_mono,
            "registrations": registrations,
        },
        "buyer": {
            "name": customer.name if customer is not None else "Walk-in customer",
            "address": address,
            "phone": customer.mobile if customer is not None else "",
            "gstin": "",
            "dl": "",
        },
        "customer": {
            "name": customer.name if customer is not None else "Walk-in customer",
            "phone": customer.mobile if customer is not None else "",
            "address": address,
        },
        "items": items,
        "taxSummary": [],
        "totals": {
            "subtotalPaise": subtotal,
            "discountPaise": discount_total,
            "taxPaise": 0,
            "taxLines": [],
            "roundingPaise": rounding,
            "totalPaise": total,
            "paidPaise": paid,
            "duePaise": due,
            "changePaise": change,
        },
        "payments": (
            [{"method": PAYMENT_LABELS.get(p.mode, p.mode), "amountPaise": _paise(p.amount), "reference": p.reference or ""}
             for p in parts if p.mode != "UDHAAR"]
            if parts else ([{"method": method, "amountPaise": paid, "reference": ""}] if paid else [])
        ),
        "tenderedPaise": _paise(sale.tendered_amount) if sale.tendered_amount is not None else None,
        # stamped across every printed page (engine renders data.stamp)
        "stamp": _stamp(db, sale),
        "stampNote": _stamp_note(db, sale, local_date),
        "notes": notes,
        "terms": profile.get("invoice_footer") or "",
        "footer": profile.get("invoice_footer") or "Thank you for shopping with us.",
    }


def _stamp(db: Session, sale: Sale) -> str:
    if sale.payment_status == "CANCELLED":
        return "VOID"
    if (sale.invoice_type or "") == "MANUAL":       # a manual bill has no returns: it is not a sale
        return ""
    from app.services import refund_service

    return "RETURNED" if refund_service.refund_status(db, sale) == "FULL" else ""


def _stamp_note(db: Session, sale: Sale, local_date) -> str:
    if sale.payment_status == "CANCELLED" and (sale.invoice_type or "") == "MANUAL":
        return "Deleted manual bill · record only"
    if sale.payment_status == "CANCELLED":
        from app.models import AuditLog

        when = db.scalar(
            select(AuditLog.timestamp)
            .where(AuditLog.entity_type == "sale", AuditLog.entity_id == sale.invoice_no, AuditLog.details.like("Sale voided%"))
            .order_by(AuditLog.timestamp.desc())
            .limit(1)
        )
        tz = settings_service.get_setting(db, "timezone", "Asia/Kolkata")
        return "Cancelled" + (f" on {to_local(when, tz):%d %b %Y}" if when else "") + " · not payable"
    return ""
