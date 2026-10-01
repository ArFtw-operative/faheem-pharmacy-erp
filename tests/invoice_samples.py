"""Synthetic supplier invoices in many layouts (for the import corpus tests).

Nothing here is copied from one supplier: each builder imitates a family of
billing software output (flat exports, letterhead bills, ERP item sheets,
ruled and borderless computer-generated PDFs).
"""
from __future__ import annotations

import io

import openpyxl
import pymupdf

LINES = [  # name, pack, mfr, batch, expiry, qty, free, rate, mrp, disc%, gst%
    ("DOLO 650MG TAB", "15S", "MICRO LABS", "DOBS4401", "05/2028", 10, 1, 24.00, 33.60, 0, 12),
    ("AZEE 500 TAB", "3S", "CIPLA", "AZ24107", "11/2027", 15, 0, 72.00, 98.50, 1, 12),
    ("PAN 40 TAB", "15S", "ALKEM", "PN5530", "02/2028", 20, 2, 10.00, 15.00, 0, 12),
    ("ALZYME SYRUP MIXED FRUIT FLAVOUR 200ML BOTTLE", "200ML", "ALKEM", "AZY882", "08/2027", 5, 0, 61.20, 85.00, 0, 12),
    ("SHELCAL 500 TAB", "15S", "TORRENT", "SH77120", "01/2029", 8, 0, 88.00, 121.00, 2, 12),
]


def amount(l) -> float:
    gross = l[5] * l[7]
    return round(gross - gross * l[9] / 100, 2)


def taxable() -> float:
    return round(sum(amount(l) for l in LINES), 2)


def net_total() -> float:
    return round(sum(round(amount(l) * l[10] / 100, 2) + amount(l) for l in LINES), 2)


# --------------------------------------------------------------------------- spreadsheets
def busy_style_csv() -> bytes:
    """Letterhead + Particulars table + totals rows (Busy / Tally-like bill export)."""
    out = ["SRI BALAJI PHARMA DISTRIBUTORS,,,,,,,,,,,",
           "GSTIN: 36AAAFB1234C1Z9  DL No: TS/HYD/2024/1188,,,,,,,,,,,",
           "Tax Invoice,,,,,,,Invoice No: SBP/1024,,Date: 12/09/2026,,",
           ",,,,,,,,,,,",
           "S.No.,Particulars,Pack,HSN/SAC,Batch No.,Exp. Dt.,Qty.,Free,M.R.P.,Rate,Disc.%,GST%,Amount"]
    for i, l in enumerate(LINES, 1):
        out.append(f"{i},{l[0]},{l[1]},30049099,{l[3]},{l[4]},{l[5]},{l[6]},{l[8]:.2f},{l[7]:.2f},{l[9]},{l[10]},{amount(l):.2f}")
    out += [f",Sub Total,,,,,,,,,,,{taxable():.2f}", f",Grand Total,,,,,,,,,,,{net_total():.2f}"]
    return ("\n".join(out) + "\n").encode()


def retailgraph_style_csv() -> bytes:
    """Item sheet where NetAmt is the *line* value (varies per row) and codes lead."""
    out = ["ItemCode,ItemName,Pack,Mfr,BatchNo,ExpDate,BillQty,FreeQty,PTR,MRP,Dis%,Tax%,NetAmt"]
    for i, l in enumerate(LINES, 1):
        out.append(f"I{i:04d},{l[0]},{l[1]},{l[2]},{l[3]},{l[4]},{l[5]},{l[6]},{l[7]:.2f},{l[8]:.2f},{l[9]},{l[10]},{amount(l):.2f}")
    return ("\n".join(out) + "\n").encode()


def easysol_style_semicolon() -> bytes:
    """Semicolon separated, dotted headers, scheme quantity column."""
    out = ["Item Code;Item Description;Pkg;Company;Batch;Expiry;Qty;Sch Qty;P.Rate;M.R.P.;CD%;Tax%;Value"]
    for i, l in enumerate(LINES, 1):
        out.append(f"{i};{l[0]};{l[1]};{l[2]};{l[3]};{l[4]};{l[5]};{l[6]};{l[7]:.2f};{l[8]:.2f};{l[9]};{l[10]};{amount(l):.2f}")
    return ("\n".join(out) + "\n").encode()


def tally_style_csv() -> bytes:
    """Accounting export without batch/expiry columns — lines must go to review, never guessed."""
    out = ["Sl No,Description of Goods,HSN/SAC,Quantity,Rate,per,Disc. %,Amount"]
    for i, l in enumerate(LINES, 1):
        out.append(f"{i},{l[0]},30049099,{l[5]} Strip,{l[7]:.2f},Strip,{l[9]},{amount(l):.2f}")
    return ("\n".join(out) + "\n").encode()


def xlsx_with_letterhead() -> bytes:
    """Excel: letterhead rows, real Excel dates for expiry, numeric batch numbers."""
    from datetime import datetime

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["MEDPLUS HEALTH WHOLESALE"])
    ws.append(["GSTIN 36AAACM9999K1Z2"])
    ws.append(["Bill No.", "MHW-77", None, "Bill Date", "10/09/2026"])
    ws.append([])
    ws.append(["Product", "Packing", "Batch", "Expiry", "Quantity", "Free", "Purchase Rate", "MRP", "GST %", "Line Total"])
    for i, l in enumerate(LINES, 1):
        m, y = l[4].split("/")
        ws.append([l[0], l[1], 240300 + i, datetime(int(y), int(m), 1), l[5], l[6], l[7], l[8], l[10], amount(l)])
    ws.append([None, None, None, None, None, None, None, None, "Net Amount", net_total()])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- computer-generated PDFs
def pdf_invoice(*, ruled: bool, two_line_header: bool = False, wrap: bool = False, per_page: int = 40,
                repeat_header: bool = True, seller: str = "SRI BALAJI PHARMA DISTRIBUTORS",
                gstin: str = "36AAAFB1234C1Z9", inv: str = "SBP/2231", date: str = "14/09/2026") -> bytes:
    cols = [("S.No", 30, "r"), ("Product Description", 190, "l"), ("Pack", 45, "l"), ("Batch", 60, "l"),
            ("Exp", 45, "l"), ("Qty", 35, "r"), ("Free", 30, "r"), ("MRP", 50, "r"), ("Rate", 50, "r"),
            ("GST%", 35, "r"), ("Amount", 60, "r")]
    header2 = {"Batch": ("Batch", "No."), "Exp": ("Exp.", "Date")} if two_line_header else {}
    doc = pymupdf.open()
    fs = 7.5
    rows = []
    for i, l in enumerate(LINES * (3 if per_page < 10 else 1), 1):
        name = l[0]
        rows.append([str(i), name, l[1], l[3], l[4], str(l[5]), str(l[6]), f"{l[8]:.2f}", f"{l[7]:.2f}", str(l[10]),
                     f"{amount(l):.2f}"])
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)]
    for pno, chunk in enumerate(pages):
        page = doc.new_page(width=700, height=900)
        y = 40
        if pno == 0:
            page.insert_text((40, y), seller, fontsize=16)
            page.insert_text((40, y + 18), f"GSTIN: {gstin}   D.L.No: 20B/21B-1188", fontsize=8)
            page.insert_text((40, y + 32), "Bill To: FAHEEM PHARMACY, Yakutpura, Hyderabad  GSTIN: 36BBBPF4321D1Z7", fontsize=8)
            page.insert_text((470, y), "TAX INVOICE", fontsize=11)
            page.insert_text((470, y + 18), f"Invoice No: {inv}", fontsize=8)
            page.insert_text((470, y + 32), f"Date: {date}", fontsize=8)
            y += 60
        if pno == 0 or repeat_header:
            x = 30
            for name, w, align in cols:
                top, bottom = header2.get(name, (name, None))
                page.insert_text((x + 2, y), top, fontsize=fs)
                if bottom:
                    page.insert_text((x + 2, y + 9), bottom, fontsize=fs)
                x += w
            if ruled:
                page.draw_line((30, y - 9), (30 + sum(c[1] for c in cols), y - 9))
            y += 20 if two_line_header else 12
        top_of_table = y - 9
        for r in chunk:
            name_lines = [r[1]]
            if wrap and len(r[1]) > 30:
                name_lines = [r[1][:30].rsplit(" ", 1)[0], r[1][len(r[1][:30].rsplit(" ", 1)[0]):].strip()]
            x = 30
            for (cname, w, align), val in zip(cols, r):
                text = name_lines[0] if cname == "Product Description" else val
                tx = x + w - 3 - pymupdf.get_text_length(text, fontsize=fs) if align == "r" else x + 2
                page.insert_text((tx, y), text, fontsize=fs)
                x += w
            if ruled:
                page.draw_line((30, y + 3), (30 + sum(c[1] for c in cols), y + 3))
            y += 11
            if len(name_lines) > 1:
                page.insert_text((30 + cols[0][1] + 2, y), name_lines[1], fontsize=fs)
                y += 11
        if ruled:
            x = 30
            for _, w, _ in cols + [("", 0, "")]:
                page.draw_line((x, top_of_table), (x, y - 8))
                x += w
        if pno == len(pages) - 1:
            page.insert_text((400, y + 20), f"Taxable Value: {taxable():.2f}", fontsize=8)
            page.insert_text((400, y + 34), f"Grand Total: {net_total():.2f}", fontsize=9)
        else:
            page.insert_text((500, y + 20), "Continued...", fontsize=8)
    return doc.tobytes()


# --------------------------------------------------------------------------- rough estimate
def _words(n: int) -> str:
    ones = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen "
            "Sixteen Seventeen Eighteen Nineteen").split()
    tens = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()

    def two(x):
        return ones[x] if x < 20 else tens[x // 10] + ("" if x % 10 == 0 else " " + ones[x % 10])

    def three(x):
        h, r = divmod(x, 100)
        return ((ones[h] + " Hundred" + (" and " if r else "")) if h else "") + (two(r) if r else "")

    parts = []
    lakh, rest = divmod(n, 100000)
    thousand, rest = divmod(rest, 1000)
    if lakh:
        parts.append(two(lakh) + " Lakh")
    if thousand:
        parts.append(two(thousand) + " Thousand")
    if rest:
        parts.append(three(rest))
    return " ".join(parts) or "Zero"


def rough_estimate_pdf() -> tuple[bytes, dict]:
    """A three-page 'rough estimate' in the layout of a common billing package's native PDF output:
    ruled columns, serial anchors slightly above their row, TOTAL C/F / B/F carry-overs, page markers,
    SUB TOTAL / Roundoff / GRAND TOTAL, amount in words, an order-contact block and a software footer.
    Everything — party, products, prices, phone numbers — is invented. Returns (pdf bytes, expected figures)."""
    import random
    from decimal import Decimal, ROUND_HALF_UP

    rnd = random.Random(8436)
    kinds = ["BABY FORMULA STAGE {n} 400G", "COUGH SYRUP {n} 100ML", "VITAMIN C {n} TAB", "ANTISEPTIC LIQUID {n} 250ML",
             "FACE WASH {n} 100G", "BODY LOTION {n} 200ML", "TOOTH PASTE {n} 150G", "HAND WASH {n} 200ML",
             "DIAPER PANTS M {n}", "PAIN BALM {n} 25G", "GLUCOSE POWDER {n} 500G", "HAIR SHAMPOO {n} 180ML"]
    mrps = [45, 55, 60, 75, 85, 99, 110, 125, 140, 160, 199, 225, 250, 320, 460, 530, 720]
    rows = []
    for i in range(1, 133):
        name = kinds[i % len(kinds)].format(n=i)
        mrp = Decimal(rnd.choice(mrps))
        rate = (mrp * Decimal("0.88")).quantize(Decimal("0.01"), ROUND_HALF_UP)
        qty, free = rnd.randint(1, 6), 0
        rows.append({"name": name, "qty": qty, "free": free, "mrp": mrp, "rate": rate})
    rows[0]["name"] = "BABY FORMULA STAGE 1 400g"
    rows[20]["name"] = "INFANT FORMULA TOTAL COMFORT 350G"          # a product, not the TOTAL line
    rows[53].update(qty=100, free=20, mrp=Decimal("10.00"), rate=Decimal("8.75"), name="ORS SACHET ORANGE 21G")
    rows[60].update(name="HERBAL HAIR OIL RS.180", mrp=Decimal("160.00"), rate=Decimal("140.80"))
    rows[122].update(name="COTTON PADS XL", mrp=Decimal("120.00"), rate=Decimal("105.60"))
    rows[123].update(name="COTTON PADS XL", mrp=Decimal("145.00"), rate=Decimal("127.60"))
    for serial, (paid, free) in {128: (5, 1), 129: (6, 1), 130: (11, 2), 131: (10, 2), 132: (6, 2)}.items():
        rows[serial - 1].update(qty=paid, free=free)
    for r in rows:
        r["amount"] = (r["rate"] * r["qty"]).quantize(Decimal("0.01"))
    # paise of the subtotal = .49, so the bill rounds DOWN (a negative round-off printed without its sign)
    delta = (Decimal("0.49") - sum(r["amount"] for r in rows) % 1) % 1
    adj = next(r for r in rows[1:50] if r["qty"] == 1 and not r["free"] and r is not rows[20])
    adj["rate"] += delta
    adj["amount"] = adj["rate"]
    pages = [rows[:50], rows[50:99], rows[99:]]
    page_sums = [sum((r["amount"] for r in p), Decimal("0")) for p in pages]
    subtotal = sum(page_sums, Decimal("0"))
    grand = subtotal.quantize(Decimal("1"), ROUND_HALF_UP)
    roundoff = grand - subtotal
    words = f"Rs. {_words(int(grand))} only"

    doc = pymupdf.open()
    W = 594.75

    def text(page, x, y, s, font="helv", size=8.0, right=None):
        if right is not None:
            x = right - pymupdf.get_text_length(s, fontname=font, fontsize=size)
        page.insert_text((x, y), s, fontname=font, fontsize=size)

    carried = Decimal("0")
    for pi, chunk in enumerate(pages):
        last = pi == len(pages) - 1
        page = doc.new_page(width=W, height=841.5)
        text(page, 24, 38, "M/s SAMPLE MEDICALS HYDERABAD", "hebo", 10)
        text(page, 257, 38, "ROUGH ESTIMATE", "hebo", 8)
        text(page, 377, 38, "Estimate No.:", "helv", 10)
        text(page, 442, 38, "E004512", "hebo", 10)
        text(page, 490, 38, "Date :", "hebo", 10)
        text(page, 377, 49, "Order No. :", "helv", 10)
        text(page, 490, 50, "16-09-2026", "hebo", 10)
        if pi:
            text(page, 505, 80, f"Page No...{pi + 1}", "hebo", 12)
        for x, s in ((108, "S.No."), (157, "Product"), (362, "Qty."), (420, "M.R.P."), (489, "Rate")):
            text(page, x, 98, s, "hebo", 9)
        for x in (98, 135, 345, 384, 452, 522):
            page.draw_line((x, 86), (x, 632), width=0.5)
        page.draw_line((19, 632), (582, 632), width=0.5)
        y = 117
        if pi:
            text(page, 422, 115, "TOTAL B/F", "hebo", 9)
            text(page, 0, 117, f"{carried:.2f}", "hebo", 9, right=571)
            y = 126
        for r in chunk:
            serial = rows.index(r) + 1
            text(page, 120, y - 2, str(serial), "hebo", 8)
            text(page, 152, y, r["name"], "hebo", 8)
            q = f"{r['qty']}+{r['free']}" if r["free"] else str(r["qty"])
            text(page, 0, y, q, "helv", 9, right=373)
            text(page, 0, y, f"{r['mrp']:.2f}", "helv", 8, right=443)
            text(page, 0, y, f"{r['rate']:.2f}", "helv", 8, right=505)
            text(page, 0, y, f"{r['amount']:.2f}", "helv", 9, right=571)
            y += 10.4
        carried += page_sums[pi]
        if not last:
            text(page, 422, 642, "TOTAL C/F", "hebo", 9)
            text(page, 0, 643, f"{carried:.2f}", "hebo", 9, right=571)
            text(page, 422, 676, f"Continued ...{pi + 2}", "hebo", 12)
            page.draw_line((19, 698), (582, 698), width=0.5)
            text(page, 24, 707, words, "helv", 8)
            text(page, 422, 707, "GRAND TOTAL", "hebo", 10)
            text(page, 0, 707, f"{grand:.2f}", "hebo", 10, right=571)
        else:
            text(page, 422, 642, "SUB TOTAL", "hebo", 9)
            text(page, 0, 643, f"{subtotal:.2f}", "hebo", 9, right=571)
            text(page, 422, 652, "Roundoff", "helv", 9)
            text(page, 0, 654, f"{abs(roundoff):.2f}", "helv", 9, right=572)
            page.draw_line((19, 656), (582, 656), width=0.5)
            text(page, 24, 665, words, "helv", 8)
            text(page, 422, 665, "GRAND TOTAL", "hebo", 10)
            text(page, 0, 665, f"{grand:.2f}", "hebo", 10, right=571)
            page.draw_line((19, 669), (579, 669), width=0.5)
            text(page, 40, 678, "For More Orders Please Contact:-", "hebo", 10)
            for k in range(5):
                text(page, 24, 689 + 10.5 * k, f"{k + 1}:-  90000000{k + 10}", "hebo", 10)
        text(page, 161, 776, "DEMO BILLING SOFTWARE | Stock, Accounts, GST | Call 9000000099", "heit", 7)
    data = doc.tobytes()
    doc.close()
    return data, {"rows": rows, "page_counts": [len(p) for p in pages], "page_sums": page_sums,
                  "subtotal": subtotal, "roundoff": roundoff, "grand_total": grand,
                  "free_units": sum(r["free"] for r in rows), "number": "E004512", "party": "M/s SAMPLE MEDICALS"}
