"""Invoice rendering: A4 PDF (PyMuPDF) and plain-text (thermal/raw) output."""
from __future__ import annotations

from io import BytesIO

from app.models import CUSTOMER_TYPE_LABELS, Sale



def _local(moment, profile):
    """Stored UTC timestamp -> pharmacy wall clock for printed documents."""
    from app.utils import to_local

    return to_local(moment, (profile or {}).get("timezone") or "Asia/Kolkata")

def _money(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _customer_type_label(value) -> str:
    return CUSTOMER_TYPE_LABELS.get((value or "WALK_IN"), "Walk-In")


def invoice_lines(sale: Sale, profile: dict) -> list[str]:
    lines = [
        profile.get("pharmacy_name", ""),
        profile.get("address", ""),
        f"Contact: {profile.get('contact_numbers', '')}",
        f"GST: {profile.get('gst_number', '')}    Drug License: {profile.get('drug_license_number', '')}",
        "-" * 64,
        f"Invoice: {sale.invoice_no}    Date: {_local(sale.sale_date, profile):%Y-%m-%d %H:%M}",
        f"Customer: {sale.customer.name if sale.customer else 'Walk-in'}    Mobile: {sale.customer.mobile if sale.customer else ''}",
        f"Customer ID: {sale.customer.customer_id if sale.customer else '-'}    Doctor: {sale.customer.doctor_name if sale.customer else ''}",
        f"Customer type: {_customer_type_label(sale.customer_type)}    Payment mode: {sale.payment_mode}",
        "-" * 64,
        f"{'#':>2} {'Product':<26} {'Qty':>4} {'MRP':>8} {'Rate':>8} {'Total':>9}",
    ]
    for idx, item in enumerate(sale.items, start=1):
        lines.append(
            f"{idx:>2} {item.product_name[:26]:<26} {item.quantity:>4} "
            f"{_money(item.mrp):>8} {_money(item.rate):>8} {_money(item.line_total):>9}"
        )
        lines.append(f"     Batch {item.batch_no}  Exp {item.expiry_date or '-'}")
    lines += [
        "-" * 64,
        f"{'Subtotal':>50} {_money(sale.subtotal):>10}",
        f"{'Discount':>50} {_money(sale.discount):>10}",
        f"{'Voucher':>50} {_money(sale.voucher):>10}",
        f"{'Round off':>50} {_money(sale.round_off):>10}",
        f"{'TOTAL':>50} {_money(sale.total):>10}",
        "-" * 64,
        profile.get("invoice_footer", ""),
    ]
    return lines


def invoice_text(sale: Sale, profile: dict) -> str:
    return "\n".join(invoice_lines(sale, profile))


def build_invoice_pdf(sale: Sale, profile: dict, qr_png: bytes | None = None) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait
    x, y = 40, 50
    page.insert_text((x, y), profile.get("pharmacy_name", "Pharmacy"), fontsize=18, fontname="helv")
    y += 20
    if profile.get("address"):
        page.insert_text((x, y), profile["address"][:90], fontsize=9)
        y += 12
    page.insert_text((x, y), f"Contact: {profile.get('contact_numbers', '')}", fontsize=9)
    y += 12
    page.insert_text(
        (x, y),
        f"GST: {profile.get('gst_number', '')}    Drug License: {profile.get('drug_license_number', '')}",
        fontsize=9,
    )
    y += 18

    page.insert_text((x, y), f"Invoice No: {sale.invoice_no}", fontsize=10)
    page.insert_text((x + 300, y), f"Date: {_local(sale.sale_date, profile):%Y-%m-%d %H:%M}", fontsize=10)
    y += 14
    customer = sale.customer
    page.insert_text(
        (x, y),
        f"Customer: {customer.name if customer else 'Walk-in'}    Mobile: {customer.mobile if customer else ''}",
        fontsize=10,
    )
    y += 14
    page.insert_text(
        (x, y),
        f"Customer ID: {customer.customer_id if customer else '-'}    Doctor: {customer.doctor_name if customer else ''}",
        fontsize=10,
    )
    y += 14
    page.insert_text(
        (x, y), f"Customer type: {_customer_type_label(sale.customer_type)}", fontsize=10
    )
    page.insert_text((x + 400, y), f"Payment: {sale.payment_mode}", fontsize=10)
    y += 20

    cols = [(x, "#"), (x + 22, "Product"), (x + 250, "Qty"), (x + 285, "MRP"), (x + 340, "Rate"), (x + 400, "Batch"), (x + 470, "Exp"), (x + 520, "Total")]
    for cx, label in cols:
        page.insert_text((cx, y), label, fontsize=8.5, fontname="hebo")
    y += 6
    page.draw_line((x, y), (555, y))
    y += 12
    for idx, item in enumerate(sale.items, start=1):
        if y > 740:
            page = doc.new_page(width=595, height=842)
            y = 50
        page.insert_text((cols[0][0], y), str(idx), fontsize=8.5)
        page.insert_text((cols[1][0], y), item.product_name[:34], fontsize=8.5)
        page.insert_text((cols[2][0], y), str(item.quantity), fontsize=8.5)
        page.insert_text((cols[3][0], y), _money(item.mrp), fontsize=8.5)
        page.insert_text((cols[4][0], y), _money(item.rate), fontsize=8.5)
        page.insert_text((cols[5][0], y), (item.batch_no or "-")[:12], fontsize=8.5)
        page.insert_text((cols[6][0], y), str(item.expiry_date or "-"), fontsize=8.5)
        page.insert_text((cols[7][0], y), _money(item.line_total), fontsize=8.5)
        y += 13

    y += 4
    page.draw_line((x, y), (555, y))
    y += 14
    totals = [
        ("Subtotal", sale.subtotal),
        ("Discount", sale.discount),
        ("Voucher", sale.voucher),
        ("Round off", sale.round_off),
        ("TOTAL", sale.total),
    ]
    for label, value in totals:
        page.insert_text((x + 380, y), label, fontsize=10, fontname="hebo")
        page.insert_text((x + 490, y), _money(value), fontsize=10)
        y += 14

    if qr_png:
        page.insert_text((x, y + 20), "Scan for location:", fontsize=9)
        page.insert_image(pymupdf.Rect(x, y + 28, x + 90, y + 118), stream=qr_png)
    if profile.get("invoice_footer"):
        page.insert_text((x + 180, y + 40), profile["invoice_footer"][:80], fontsize=9)

    out = BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def build_refund_pdf(ret, sale, profile: dict) -> bytes:
    """Refund receipt (A4 portrait) for a completed return."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    x, y = 40, 50
    page.insert_text((x, y), profile.get("pharmacy_name", "Pharmacy"), fontsize=18, fontname="hebo")
    y += 20
    page.insert_text((x, y), "REFUND RECEIPT", fontsize=12, fontname="hebo")
    y += 18
    if profile.get("address"):
        page.insert_text((x, y), profile["address"][:90], fontsize=9)
        y += 12
    page.insert_text((x, y), f"Contact: {profile.get('contact_numbers', '')}", fontsize=9)
    y += 18
    page.draw_line((x, y), (555, y))
    y += 16

    rows = [
        ("Refund No", ret.return_no),
        ("Original Invoice", sale.invoice_no if sale else ""),
        ("Customer", sale.customer.name if sale and sale.customer else "Walk-in"),
        ("Customer Type", _customer_type_label(sale.customer_type) if sale else ""),
        ("Refund Method", ret.refund_method),
        ("Processed By", sale.user.employee_id if sale and sale.user else ""),
        ("Date / Time", _local(ret.processed_at, profile).strftime("%d %b %Y %H:%M") if ret.processed_at else ""),
    ]
    for label, value in rows:
        page.insert_text((x, y), label, fontsize=10, fontname="hebo")
        page.insert_text((x + 150, y), str(value), fontsize=10)
        y += 14

    y += 6
    page.insert_text((x, y), "ITEMS RETURNED", fontsize=10, fontname="hebo")
    y += 6
    page.draw_line((x, y), (555, y))
    y += 12
    for item in ret.items:
        page.insert_text((x, y), (item.product_name or "")[:34], fontsize=9)
        page.insert_text((x + 250, y), f"Qty {item.quantity}", fontsize=9)
        page.insert_text((x + 380, y), f"Batch {item.batch_no or '-'}", fontsize=9)
        page.insert_text((x + 490, y), _money(item.refund_amount), fontsize=9)
        y += 13
        page.insert_text((x + 10, y), f"Disposition: {item.disposition.replace('_', ' ').title()}", fontsize=8)
        y += 12

    y += 6
    page.draw_line((x, y), (555, y))
    y += 16
    page.insert_text((x + 360, y), "TOTAL REFUND", fontsize=11, fontname="hebo")
    page.insert_text((x + 490, y), _money(ret.total_refund), fontsize=11, fontname="hebo")
    y += 22
    if ret.reason_code:
        page.insert_text((x, y), f"Reason: {ret.reason_code.replace('_', ' ').title()}", fontsize=9)
        y += 12
    if ret.reason_note:
        page.insert_text((x, y), ret.reason_note[:90], fontsize=9)
        y += 12
    page.insert_text((x, y), "This refund reverses part of the original invoice; the invoice remains valid.", fontsize=8)

    out = BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()
