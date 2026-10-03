"""Invoice Store — premium A4 invoice templates for WhatsApp (printing keeps the classic invoice).

    invoice_data(db, sale)  →  plain data for one bill (also built by hand for previews)
    render(data, template)  →  PDF bytes

Layout rules: every page carries the brand bar, logo, invoice number and the item table header; a page
holds at most ``items_per_page`` lines (default 5) and a line is never split across pages; the bill
summary, amount in words, thank-you block and (template "a4-compact-stamp") the stamp come on the last
page only. Shop details come from the Invoice Store settings; GSTIN and drug licence appear only when
switched on there.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pymupdf

from app.config import BASE_DIR, UPLOAD_DIR
from app.services import units

ASSETS = BASE_DIR / "app" / "static" / "invoice-store"
LOGO_SVG = BASE_DIR / "app" / "static" / "brand" / "svg" / "horizontal-color.svg"

TEMPLATES = {
    "a4-compact-stamp": {"name": "A4 Compact — with stamp", "stamp": True,
                         "description": "Brand bar and wave, item table, net-amount panel, round stamp at the bottom right"},
    "a4-compact": {"name": "A4 Compact — without stamp", "stamp": False,
                   "description": "The same layout without the stamp"},
}
DEFAULT_TEMPLATE = "a4-compact-stamp"

W, H = 595.5, 842.25
X0, X1 = 34.0, 561.2
TEAL = (0 / 255, 86 / 255, 101 / 255)
INK = (0x12 / 255, 0x34 / 255, 0x3D / 255)
MUTED = (0x60 / 255, 0x77 / 255, 0x7C / 255)
RULE = (0.8627, 0.9137, 0.9176)
ROW = (0.949, 0.9765, 0.9765)
PILL = (0.9098, 0.9569, 0.9412)
WHITE = (1, 1, 1)
FONTS = {"r": "Inter-Regular.ttf", "m": "Inter-Medium.ttf", "s": "Inter-SemiBold.ttf", "b": "Inter-Bold.ttf"}
# item table columns: (key, header, x, align, width)
COLS = [("no", "#", 39.0, "l", 20), ("name", "ITEM DESCRIPTION", 62.0, "l", 134), ("pack", "PACK", 202.0, "l", 30),
        ("mfr", "MFR.", 234.0, "l", 44), ("batch", "BATCH", 280.0, "l", 55), ("expiry", "EXPIRY", 337.0, "l", 44),
        ("qty", "QTY", 415.0, "r", 34), ("mrp", "MRP", 476.0, "r", 56), ("amount", "AMOUNT", 554.0, "r", 70)]


@lru_cache(maxsize=None)
def _font(key: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=str(ASSETS / "fonts" / FONTS[key]))


@lru_cache(maxsize=None)
def _svg_pdf(path: str) -> bytes:
    return pymupdf.open(path).convert_to_pdf()


def inr(value) -> str:
    """₹1,23,456.78 — Indian digit grouping."""
    v = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    sign = "-" if v < 0 else ""
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:]); head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def _qty(q) -> str:
    q = Decimal(str(q or 0))
    return f"{q.normalize():f}" if q != q.to_integral() else str(int(q))


# ------------------------------------------------------------------------------------------------ data
def store_options(db) -> dict:
    from app.services import settings_service as S
    g = lambda k, d="": (S.get_setting(db, k, d) or d)
    on = lambda k: str(g(k, "0")).lower() in ("1", "true", "yes", "on")
    return {
        "template": g("invoice_store_template", DEFAULT_TEMPLATE),
        "items_per_page": max(3, min(8, int(g("invoice_store_items_per_page", "5") or 5))),
        "pharmacy_name": g("pharmacy_name", "Faheem Pharmacy"),
        "address": g("address", ""), "phones": g("contact_numbers", ""), "email": g("pharmacy_email", ""),
        "gstin": g("gst_number", "") if on("show_gst") else "",
        "dl": g("drug_license_number", "") if on("show_drug_license") else "",
        "thanks": g("invoice_store_thanks", "Thank you for choosing"),
        "note": g("invoice_store_note", "Keep this invoice for your purchase records."),
        "closing": g("invoice_store_closing", "Clear details. Thoughtful care."),
        "stamp_path": str(UPLOAD_DIR / g("invoice_store_stamp")) if g("invoice_store_stamp") else "",
        "timezone": g("timezone", "Asia/Kolkata"),
    }


def invoice_data(db, sale) -> dict:
    """Everything the template prints for one bill."""
    from app.services import financials, invoice_kit
    from app.utils import to_local

    opts = store_options(db)
    lines = []
    for line in sorted(sale.items, key=lambda l: (l.line_no or 0, l.id or 0)):
        it = line.item
        upp = int(line.units_per_pack or 1)
        pack_mrp = Decimal(str(line.pack_mrp or line.mrp or 0))
        lines.append({
            "name": line.product_name, "pack": line.pack_size or (it.pack_size if it else "") or "",
            "mfr": ((it.manufacturer if it else "") or "").split()[0][:10] if it and it.manufacturer else "",
            "batch": line.batch_no or "", "expiry": line.expiry_date.strftime("%m-%Y") if line.expiry_date else "",
            "qty": line.quantity, "mrp": pack_mrp, "upp": upp, "amount": financials.gross_value(line),
        })
    gross = sum((l["amount"] for l in lines), Decimal("0"))
    total = Decimal(str(sale.total or 0))
    discount = gross - total + Decimal(str(sale.round_off or 0))          # item + bill discount + voucher
    paid = sum((Decimal(str(p.amount)) for p in sale.payments), Decimal("0")) if sale.payments else total
    if sale.payment_status == "CANCELLED":
        paid = Decimal("0")
    cust = sale.customer
    modes = sorted({p.mode.title() for p in sale.payments}) if sale.payments else [str(sale.payment_mode or "Cash").title()]
    local = to_local(sale.sale_date, opts["timezone"]) or datetime.now()
    return {
        "invoice_no": sale.invoice_no, "date": local, "manual": (sale.invoice_type or "") == "MANUAL",
        "customer": {"name": cust.name if cust else "Walk-in customer", "phone": (cust.mobile if cust else "") or "",
                     "address": ", ".join(x for x in ((cust.address if cust else ""), (cust.city if cust else "")) if x)},
        "served_by": (sale.user.full_name or sale.user.username) if sale.user else "",
        "payment": " + ".join(modes), "lines": lines,
        "gross": gross, "discount": max(discount, Decimal("0")), "round_off": Decimal(str(sale.round_off or 0)),
        "total": total, "paid": min(paid, total), "balance": max(total - paid, Decimal("0")),
        "cancelled": sale.payment_status == "CANCELLED",
        "words": invoice_kit.amount_words(int((total * 100).to_integral())), "options": opts,
    }


def demo_data(db, items: int = 7) -> dict:
    """A made-up bill for previews in Settings (nothing is stored)."""
    from app.services import invoice_kit

    names = ["DOLO 650 MG TAB", "AZEE 500 MG TAB", "PAN 40 MG TAB", "SHELCAL 500 TAB", "ALLEGRA 120 MG TAB",
             "BECOSULES CAP", "LIMCEE 500 MG TAB", "CROCIN ADVANCE TAB", "VICKS ACTION 500 TAB", "ORS POWDER ORANGE"]
    lines = []
    for i in range(items):
        upp, mrp, qty = (15, Decimal("32.28"), 10) if i % 3 == 0 else (1, Decimal("98.50") + i, 1 + i % 2)
        lines.append({"name": names[i % len(names)], "pack": "15 S" if upp > 1 else "10 S", "mfr": "MICRO",
                      "batch": f"DB{4400 + i}", "expiry": f"0{1 + i % 9}-2028", "qty": qty, "mrp": mrp, "upp": upp,
                      "amount": units.line_amount(mrp, upp, qty)})
    gross = sum((l["amount"] for l in lines), Decimal("0"))
    total = gross.quantize(Decimal("1"))
    return {"invoice_no": "INV-PREVIEW-0001", "date": datetime.now(), "manual": False,
            "customer": {"name": "Sample Customer", "phone": "9000000001", "address": ""}, "served_by": "Counter",
            "payment": "Cash", "lines": lines, "gross": gross, "discount": Decimal("0"), "round_off": total - gross,
            "total": total, "paid": total, "balance": Decimal("0"), "cancelled": False,
            "words": invoice_kit.amount_words(int(total * 100)), "options": store_options(db)}


# ------------------------------------------------------------------------------------------------ drawing
class _Page:
    def __init__(self, doc):
        self.p = doc.new_page(width=W, height=H)
        for key, file in FONTS.items():
            self.p.insert_font(fontname=f"in{key}", fontfile=str(ASSETS / "fonts" / file))

    def text(self, x, y, s, size=9.0, font="r", color=INK, align="l", width=None):
        s = str(s or "")
        f = _font(font)
        if width:                                         # never overflow its cell: shrink a little, then ellipsis
            while f.text_length(s, size) > width and size > 6.6:
                size -= 0.2
            if f.text_length(s, size) > width:
                while s and f.text_length(s + "…", size) > width:
                    s = s[:-1]
                s += "…"
        w = f.text_length(s, size)
        if align == "r":
            x -= w
        elif align == "c":
            x -= w / 2
        self.p.insert_text((x, y), s, fontname=f"in{font}", fontsize=size, color=color)
        return w

    def wrap(self, x, y, s, width, size=9.0, font="r", color=INK, leading=None, max_lines=3):
        words, line, n = str(s or "").split(), "", 0
        leading = leading or size * 1.45
        for word in words:
            trial = (line + " " + word).strip()
            if _font(font).text_length(trial, size) > width and line:
                self.text(x, y, line, size, font, color); y += leading; n += 1; line = word
                if n == max_lines - 1:
                    break
            else:
                line = trial
        if line:
            self.text(x, y, line, size, font, color, width=width); y += leading
        return y

    def rule(self, x0, y, x1, color=RULE, width=0.6):
        self.p.draw_line((x0, y), (x1, y), color=color, width=width)

    def box(self, rect, fill, radius=None):
        r = pymupdf.Rect(rect)
        self.p.draw_rect(r, color=None, fill=fill, radius=radius / min(r.width, r.height) if radius else None)

    def image(self, rect, path):
        self.p.insert_image(pymupdf.Rect(rect), filename=str(path), keep_proportion=False)

    def svg(self, rect, path):
        src = pymupdf.open("pdf", _svg_pdf(str(path)))
        self.p.show_pdf_page(pymupdf.Rect(rect), src, 0)


def _chrome(pg: _Page, data: dict, page_no: int, pages: int, first: bool):
    o = data["options"]
    pg.image((-5, -5, 600, 10), ASSETS / "top-bar.png")
    pg.image((-5, 785, 600, 847.5), ASSETS / "footer-wave.png")
    if LOGO_SVG.exists():
        pg.svg((X0, 50, X0 + 165, 50 + 165 * 164 / 484), LOGO_SVG)
    pg.text(X1, 70, "INVOICE", 25, "r", TEAL, "r")
    pg.text(X1, 89, data["invoice_no"], 10, "b", TEAL, "r")
    pg.text(X1, 103, data["date"].strftime("%d %b %Y · %I:%M %p").upper(), 8.5, "r", MUTED, "r")
    if pages > 1:
        pg.text(X1, 116, f"Page {page_no} of {pages}", 8.5, "m", MUTED, "r")
    if not first:
        c = data["customer"]
        pg.text(X0, 116, f"Billed to {c['name']}" + (f" · {c['phone']}" if c["phone"] else ""), 8.5, "m", MUTED, width=330)
        return 128.0
    y = 120.0
    y = pg.wrap(X0, y, o["address"], 270, 9.5, "r", INK, max_lines=2) if o["address"] else y
    if o["phones"]:
        pg.text(X0, y, o["phones"], 9.5, "r", INK, width=270); y += 14
    if o["email"]:
        pg.text(X0, y, o["email"], 9, "r", MUTED, width=270); y += 13
    extra = " · ".join(x for x in ((f"GSTIN {o['gstin']}" if o["gstin"] else ""), (f"DL {o['dl']}" if o["dl"] else "")) if x)
    if extra:
        pg.text(X0, y, extra, 8.5, "m", MUTED, width=300); y += 13
    status, fill, ink = (("CANCELLED", (0.99, 0.93, 0.92), (0.7, 0.15, 0.12)) if data["cancelled"]
                         else ("PAID", PILL, TEAL) if not data["balance"] else ("BALANCE DUE", (1, 0.95, 0.85), (0.6, 0.38, 0)))
    pw = _font("b").text_length(status, 9.5) + 34
    pg.box((X1 - pw, 128, X1, 151), fill, radius=11.5)
    pg.text(X1 - pw / 2, 143.5, status, 9.5, "b", ink, "c")
    top = max(y + 6, 165.0)
    pg.rule(100, top, 495.3)
    # billed to · payment details
    y = top + 18
    pg.text(X0, y, "BILLED TO", 9.5, "b", MUTED)
    pg.text(X0, y + 17, data["customer"]["name"], 11, "b", INK, width=255)
    yy = y + 34
    if data["customer"]["phone"]:
        pg.text(X0, yy, f"Phone {data['customer']['phone']}", 9.5, "r", INK); yy += 14
    if data["customer"]["address"]:
        pg.text(X0, yy, data["customer"]["address"], 9, "r", MUTED, width=255)
    pg.p.draw_line((309, y - 8), (309, y + 52), color=RULE, width=0.5)
    pg.text(333.1, y, "PAYMENT DETAILS", 9, "b", MUTED)
    pg.text(333.1, y + 17, data["payment"], 9.5, "s", INK, width=120)
    pg.text(X1, y + 20, inr(data["total"]), 20, "b", TEAL, "r")
    if data["served_by"]:
        pg.text(333.1, y + 36, f"Served by {data['served_by']}", 9, "r", INK, width=220)
    note = ("Bill cancelled" if data["cancelled"] else "Payment received in full" if not data["balance"]
            else f"Balance due {inr(data['balance'])}")
    pg.text(333.1, y + 50, note, 9, "r", MUTED)
    return y + 66


def _table(pg: _Page, y: float, rows: list[dict], start_no: int) -> float:
    pg.box((X0, y, X1, y + 24), TEAL, radius=5)
    for key, head, x, align, width in COLS:
        pg.text(x, y + 15.2, head, 7, "b", WHITE, align)
    y += 26
    for i, l in enumerate(rows):
        if i % 2 == 0:
            pg.box((X0, y, X1, y + 25), ROW, radius=3)
        base = y + 15.8
        mrp = inr(l["mrp"]) + (f"/{l['upp']}" if l["upp"] > 1 else "")
        cells = {"no": f"{start_no + i:02d}", "name": l["name"], "pack": l["pack"], "mfr": l["mfr"], "batch": l["batch"],
                 "expiry": l["expiry"], "qty": _qty(l["qty"]), "mrp": mrp, "amount": inr(l["amount"])}
        for key, head, x, align, width in COLS:
            bold = key in ("name", "amount")
            pg.text(x, base, cells[key], 8.5 if bold else 8, "s" if bold else "r", INK, align, width - 4)
        y += 26
    pg.rule(X0, y + 1, X1, width=0.7)
    return y + 2


def _summary(pg: _Page, y: float, data: dict, stamp: bool):
    o = data["options"]
    units = sum((Decimal(str(l["qty"])) for l in data["lines"]), Decimal("0"))
    pg.text(39, y + 13, f"{len(data['lines'])} line item{'s' if len(data['lines']) != 1 else ''}  ·  {_qty(units)} units", 8.5, "r", MUTED)
    s = y + 40
    for label, value, k in (("Subtotal", inr(data["gross"]), 0), ("Discount", ("− " + inr(data["discount"])) if data["discount"] else inr(0), 1),
                            ("Round off", ("+ " if data["round_off"] >= 0 else "− ") + inr(abs(data["round_off"])), 2)):
        pg.text(333.1, s + 21 * k, label, 9.5, "r", MUTED)
        pg.text(550.9, s + 21 * k, value, 10, "r", INK, "r")
    nb = s + 56
    pg.box((318.1, nb, X1, nb + 42), TEAL, radius=7)
    pg.text(333.1, nb + 25, "NET AMOUNT", 9, "b", WHITE)
    pg.text(546.2, nb + 28, inr(data["total"]), 20, "b", WHITE, "r")
    pg.text(333.1, nb + 62, "Received", 9, "r", MUTED)
    pg.text(550.9, nb + 62, inr(data["paid"]), 10, "b", INK, "r")
    pg.text(333.1, nb + 82, "Balance due", 9, "r", MUTED)
    pg.text(550.9, nb + 82, inr(data["balance"]), 10, "b", TEAL, "r")
    # left column: amount in words, thank you
    pg.text(X0, s + 4, "AMOUNT IN WORDS", 8, "b", MUTED)
    yy = pg.wrap(X0, s + 19, data["words"], 255, 11, "b", INK, max_lines=3)
    yy = max(yy + 18, nb + 18)
    pg.text(X0, yy, o["thanks"], 15, "r", TEAL, width=260)
    pg.text(X0, yy + 23, (o["pharmacy_name"] or "").rstrip(".") + ".", 19, "b", TEAL, width=260)
    if o["note"]:
        pg.text(X0, yy + 41, o["note"], 9, "r", MUTED, width=260)
    bottom = max(yy + 52, nb + 92)
    pg.rule(100, bottom + 8, 495.3)
    if o["closing"]:
        pg.text(X0, 752, o["closing"], 9, "r", MUTED, width=330)
    if stamp:
        size = min(118.0, 772 - (bottom + 18))
        path = stamp_file(o)
        rect = (X1 - 6 - size, bottom + 16, X1 - 6, bottom + 16 + size)
        pg.svg(rect, path) if str(path).lower().endswith(".svg") else pg.image(rect, path)


def stamp_file(options: dict) -> Path:
    """The uploaded stamp (Settings → Invoice Store) or the bundled one."""
    custom = Path(options["stamp_path"]) if options.get("stamp_path") else None
    return custom if custom and custom.is_file() else ASSETS / "stamp.svg"


def render(data: dict, template: str | None = None) -> bytes:
    template = template if template in TEMPLATES else data["options"].get("template", DEFAULT_TEMPLATE)
    template = template if template in TEMPLATES else DEFAULT_TEMPLATE
    per = data["options"]["items_per_page"]
    chunks = [data["lines"][i:i + per] for i in range(0, len(data["lines"]), per)] or [[]]
    doc = pymupdf.open()
    for n, rows in enumerate(chunks, start=1):
        pg = _Page(doc)
        y = _chrome(pg, data, n, len(chunks), first=n == 1)
        y = _table(pg, y, rows, (n - 1) * per + 1)
        if n < len(chunks):
            pg.text(X1, y + 16, f"Continued on page {n + 1}  →", 8.5, "m", MUTED, "r")
        else:
            _summary(pg, y, data, TEMPLATES[template]["stamp"])
    doc.set_metadata({"title": f"Invoice {data['invoice_no']}", "author": data["options"]["pharmacy_name"],
                      "creator": "Faheem Pharmacy ERP — Invoice Store", "producer": "Faheem Pharmacy ERP"})
    try:
        doc.subset_fonts()                       # only the glyphs used: a WhatsApp-sized file
    except Exception:  # pragma: no cover - older MuPDF: full fonts are still correct
        pass
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


def render_sale(db, sale, template: str | None = None) -> bytes:
    return render(invoice_data(db, sale), template)
