"""Regression corpus for the purchase processing engine (synthetic, anonymised).

The repository is public, so real supplier invoices never enter it (see
tests/test_local_supplier_corpus.py for the opt-in private files). This corpus models the
layouts the pharmacy really receives — a Marg-style CSV, an Excel export, a computer-made
PDF and a photographed receipt — with an expected-result record for every line.

Each supplier sends two invoices. The first is new to the system: a simulated person
reviews it the way the screen asks (match the product, confirm the pack) and posts it.
The second is the recurring invoice that must pass straight through. Every line of the
second invoice is checked against its expected product, batch, expiry, billed, free and
received stock: a line the gate accepts with any of these wrong is a *wrong auto-accept*.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# name, printed pack, form, base unit, retail pack unit, units per retail pack, MRP per retail pack
CATALOGUE = [
    ("PARACIP-500 TABLET", "20X10", "TABLET", "TABLET", "STRIP", 10, "25.00"),
    ("MULTIPREX CAPSULE", "10X1X10", "CAPSULE", "CAPSULE", "STRIP", 10, "175.00"),
    ("OKACET SYRUP 60ML", "60ML", "SYRUP", "BOTTLE", "BOTTLE", 1, "60.00"),
    ("OMNIGEL 20 GM", "20GM", "GEL", "TUBE", "TUBE", 1, "120.00"),
    ("DNS IV FLUID 500ML", "500ML", "IV_FLUID", "BOTTLE", "BOTTLE", 1, "45.00"),
    ("SAFECATH IV CANNULA 22G", "1X100", "CANNULA", "PIECE", "BOX", 100, "3500.00"),
    ("ROSUBEST-10 TABLET", "10S", "TABLET", "TABLET", "STRIP", 10, "180.00"),
    ("TELMAVAS-40 TABLET", "15S", "TABLET", "TABLET", "STRIP", 15, "142.50"),
    ("CALCIJOINT D3 SG CAPSULE", "10X1X4", "SOFTGEL", "CAPSULE", "STRIP", 4, "125.00"),
    ("CEFPROX 200 DT TABLET", "10X10", "TABLET", "TABLET", "STRIP", 10, "210.00"),
    ("AZITHRAL 500 TABLET", "1X5", "TABLET", "TABLET", "STRIP", 5, "119.00"),
    ("BETADINE OINTMENT 15GM", "15GM", "OINTMENT", "TUBE", "TUBE", 1, "95.00"),
    ("SINAREST DROPS 15ML", "15ML", "DROPS", "BOTTLE", "BOTTLE", 1, "88.00"),
    ("LEVOLIN INHALER", "200MD", "INHALER", "PIECE", "PIECE", 1, "210.00"),
    ("DISPOVAN SYRINGE 5ML", "1X100", "SYRINGE", "PIECE", "BOX", 100, "900.00"),
    ("ZEBRAZOL 10 MG TABLET", "10S", "TABLET", "TABLET", "STRIP", 10, "60.00"),
    ("METFORAL SR 500 TABLET", "15S", "TABLET", "TABLET", "STRIP", 15, "45.00"),
    ("DETTOL LIQUID 125ML", "125ML", "LOTION", "BOTTLE", "BOTTLE", 1, "83.00"),
    ("MOOV SPRAY 35G", "35G", "SPRAY", "BOTTLE", "BOTTLE", 1, "186.00"),
    ("IODEX BALM 20GRMS", "20GM", "OINTMENT", "TUBE", "TUBE", 1, "95.00"),
    ("COLGATE PASTE 100GRMS", "100GM", "DEVICE", "PIECE", "PIECE", 1, "73.00"),
]


# A bulk supplier: generated brands across every form, so the corpus measures more than a handful of rows.
_FORMS = [  # word on the invoice, form, base unit, retail unit, packs printed → units per retail pack
    ("TAB", "TABLET", "TABLET", "STRIP", [("10S", 10), ("15S", 15), ("1X10", 10), ("10X10", 10), ("20X10", 10), ("10X1X10", 10)]),
    ("CAP", "CAPSULE", "CAPSULE", "STRIP", [("10S", 10), ("1X10", 10), ("10X1X10", 10), ("10X1X15", 15)]),
    ("SYP", "SYRUP", "BOTTLE", "BOTTLE", [("100ML", 1), ("60ML", 1), ("200ML", 1)]),
    ("CREAM", "CREAM", "TUBE", "TUBE", [("15GM", 1), ("30GM", 1)]),
    ("DROPS", "DROPS", "BOTTLE", "BOTTLE", [("10ML", 1), ("15ML", 1)]),
    ("INJ", "INJECTION", "VIAL", "VIAL", [("VIAL", 1)]),
]
_SYLLABLES = ["CAR", "DIO", "VEN", "LIX", "MOR", "TAZ", "NEB", "ROL", "FEX", "ZAN", "QUI", "PRO", "LEV", "SAN", "TOR"]


def bulk_catalogue(n: int = 120) -> list[tuple]:
    out = []
    for i in range(n):
        word, form, base, unit, packs = _FORMS[i % len(_FORMS)]
        pack, upp = packs[(i // len(_FORMS)) % len(packs)]
        brand = _SYLLABLES[i % 15] + _SYLLABLES[(i * 7 + 3) % 15] + ("A" if i % 2 else "")
        strength = [5, 10, 20, 40, 100, 250, 500][i % 7]
        name = f"{brand}-{strength} {'TABLET' if word == 'TAB' else 'CAPSULE' if word == 'CAP' else word}"
        out.append((name, pack, form, base, unit, upp, f"{50 + (i * 13) % 400}.00", word, brand, strength))
    return out


@dataclass
class Line:
    """What the invoice prints, and what is true about it."""
    printed: str                 # supplier description
    pack: str
    batch: str
    expiry: str                  # MM/YYYY as printed
    qty: int
    free: int
    rate: Decimal
    mrp: Decimal
    product: str | None          # expected catalogue product (None = genuinely new)
    per_qty: int | None          # expected base units one invoice Qty counts (None = unknowable from the invoice)
    code: str = ""

    @property
    def amount(self) -> Decimal:
        return (self.rate * self.qty).quantize(Decimal("0.01"))

    @property
    def received(self) -> int | None:
        return (self.qty + self.free) * self.per_qty if self.per_qty else None


@dataclass
class Invoice:
    supplier: str
    route: str                   # STRUCTURED (CSV/XLSX) · PDF_TEXT · OCR
    number: str
    when: date
    lines: list[Line]
    filename: str = ""
    content: bytes = b""
    gstin: str = ""


@dataclass
class Result:
    invoice: str
    supplier: str
    route: str
    recurring: bool
    lines: int = 0
    auto: int = 0
    warning: int = 0
    review: int = 0
    blocked: int = 0
    wrong: list = field(default_factory=list)
    known: int = 0               # lines whose product is in the catalogue and was bought before
    known_auto: int = 0
    packs_resolved: int = 0
    product_right: int = 0
    ms: int = 0


def L(printed, pack, batch, expiry, qty, free, rate, mrp, product, per_qty, code=""):
    return Line(printed, pack, batch, expiry, qty, free, Decimal(rate), Decimal(mrp), product, per_qty, code)


# --------------------------------------------------------------------------- invoices
def invoices() -> list[Invoice]:
    # Supplier A — Marg-style CSV, bills nested packs per strip (retail pack)
    a1 = [L("PARACIP 500 TAB", "20X10", "PC2401", "05/2028", 10, 1, "18.00", "25.00", "PARACIP-500 TABLET", 10, "P500"),
          L("MULTIPREX CAP", "10x1x10", "MX7710", "08/2027", 5, 0, "120.00", "175.00", "MULTIPREX CAPSULE", 10, "MPX"),
          L("OKACET SYRUP", "60ML", "OK5501", "03/2028", 6, 0, "41.00", "60.00", "OKACET SYRUP 60ML", 1, "OKS"),
          L("OMNIGEL 20 GM", "20GM", "OG3301", "11/2027", 4, 0, "84.00", "120.00", "OMNIGEL 20 GM", 1, "OMG"),
          L("ROSUBEST 10 TAB", "10S", "RB1001", "01/2029", 3, 0, "130.00", "180.00", "ROSUBEST-10 TABLET", 10, "RB10"),
          L("CALCIJOINT D3 SG CAP", "10x1x4", "CJ0904", "06/2028", 10, 2, "88.00", "125.00", "CALCIJOINT D3 SG CAPSULE", 4, "CJD"),
          L("CEFPROX 200 DT TAB", "10X10", "CF2002", "09/2027", 5, 0, "150.00", "210.00", "CEFPROX 200 DT TABLET", 10, "CFX"),
          L("SINAREST DROPS", "15ML", "SN1501", "04/2028", 3, 0, "61.00", "88.00", "SINAREST DROPS 15ML", 1, "SND"),
          L("TELMAVAS-40 TAB", "15S", "TV4001", "02/2029", 6, 0, "98.00", "142.50", "TELMAVAS-40 TABLET", 15, "TV40")]
    a2 = [L("PARACIP 500 TAB", "20X10", "PC2417", "07/2028", 20, 2, "18.00", "25.00", "PARACIP-500 TABLET", 10, "P500"),
          L("MULTIPREX CAP", "10x1x10", "MX7799", "10/2027", 2, 0, "120.00", "175.00", "MULTIPREX CAPSULE", 10, "MPX"),
          L("OKACET SYRUP", "60ML", "OK5522", "05/2028", 2, 0, "41.00", "60.00", "OKACET SYRUP 60ML", 1, "OKS"),
          L("OMNIGEL 20 GM", "20GM", "OG3388", "12/2027", 3, 0, "84.00", "120.00", "OMNIGEL 20 GM", 1, "OMG"),
          L("ROSUBEST 10 TAB", "10S", "RB1077", "03/2029", 5, 1, "130.00", "180.00", "ROSUBEST-10 TABLET", 10, "RB10"),
          L("CALCIJOINT D3 SG CAP", "10x1x4", "CJ0990", "08/2028", 5, 0, "88.00", "125.00", "CALCIJOINT D3 SG CAPSULE", 4, "CJD"),
          L("CEFPROX 200 DT TAB", "10X10", "CF2050", "11/2027", 10, 1, "150.00", "210.00", "CEFPROX 200 DT TABLET", 10, "CFX"),
          L("SINAREST DROPS", "15ML", "SN1544", "06/2028", 6, 0, "61.00", "88.00", "SINAREST DROPS 15ML", 1, "SND"),
          L("TELMAVAS-40 TAB", "15S", "TV4031", "04/2029", 3, 0, "98.00", "142.50", "TELMAVAS-40 TABLET", 15, "TV40"),
          # new to this supplier, but in the catalogue under a superficially different name
          L("AZITHRAL-500 TABS", "1X5", "AZ5001", "07/2028", 4, 0, "84.00", "119.00", "AZITHRAL 500 TABLET", 5, "AZ5"),
          # traps: different strength, missing release marker, unreadable pack, a product we do not stock
          L("ZEBRAZOL 5 MG TAB", "10S", "ZB0501", "05/2028", 2, 0, "40.00", "55.00", None, None, "ZB5"),
          L("METFORAL 500 TAB", "15S", "MF5001", "06/2028", 2, 0, "30.00", "41.00", None, None, "MF5"),
          L("EXOTIC EAR DROP", "10ML57", "EX1001", "01/2028", 1, 0, "40.00", "57.00", None, None, "EXD"),
          L("NEOCARE BABY WIPES 72", "1X72", "NW7201", "12/2027", 2, 0, "110.00", "150.00", None, None, "NBW")]
    # Supplier B — Excel export, bills IV fluids and devices; bills cannulas by the box of 100
    b1 = [L("DNS 500ML IV", "500ML", "DN5001", "09/2028", 20, 0, "30.00", "45.00", "DNS IV FLUID 500ML", 1),
          L("SAFECATH CANNULA 22G", "1X100", "SC2201", "12/2029", 1, 0, "2400.00", "3500.00", "SAFECATH IV CANNULA 22G", 100),
          L("DISPOVAN 5ML SYRINGE", "1X100", "DV0501", "10/2029", 2, 0, "620.00", "900.00", "DISPOVAN SYRINGE 5ML", 100),
          L("BETADINE OINT 15GM", "15GM", "BT1501", "03/2028", 5, 0, "66.00", "95.00", "BETADINE OINTMENT 15GM", 1),
          L("LEVOLIN INHALER", "200MD", "LV2001", "02/2028", 2, 0, "150.00", "210.00", "LEVOLIN INHALER", 1)]
    b2 = [L("DNS 500ML IV", "500ML", "DN5050", "11/2028", 40, 2, "30.00", "45.00", "DNS IV FLUID 500ML", 1),
          L("SAFECATH CANNULA 22G", "1X100", "SC2260", "01/2030", 2, 0, "2400.00", "3500.00", "SAFECATH IV CANNULA 22G", 100),
          L("DISPOVAN 5ML SYRINGE", "1X100", "DV0577", "12/2029", 1, 0, "620.00", "900.00", "DISPOVAN SYRINGE 5ML", 100),
          L("BETADINE OINT 15GM", "15GM", "BT1599", "05/2028", 10, 1, "66.00", "95.00", "BETADINE OINTMENT 15GM", 1),
          L("LEVOLIN INHALER", "200MD", "LV2044", "04/2028", 3, 0, "150.00", "210.00", "LEVOLIN INHALER", 1)]
    # Supplier C — computer-made PDF
    c1 = [L("TELMAVAS 40 TABLET", "15S", "TV4101", "05/2029", 10, 0, "98.00", "142.50", "TELMAVAS-40 TABLET", 15),
          L("ROSUBEST 10 TABLET", "10S", "RB1101", "06/2029", 6, 0, "130.00", "180.00", "ROSUBEST-10 TABLET", 10),
          L("METFORAL SR 500 TAB", "15S", "MS5101", "07/2028", 8, 1, "31.00", "45.00", "METFORAL SR 500 TABLET", 15),
          L("ZEBRAZOL 10 MG TAB", "10S", "ZB1101", "08/2028", 4, 0, "43.00", "60.00", "ZEBRAZOL 10 MG TABLET", 10)]
    c2 = [L("TELMAVAS 40 TABLET", "15S", "TV4150", "07/2029", 12, 1, "98.00", "142.50", "TELMAVAS-40 TABLET", 15),
          L("ROSUBEST 10 TABLET", "10S", "RB1150", "08/2029", 4, 0, "130.00", "180.00", "ROSUBEST-10 TABLET", 10),
          L("METFORAL SR 500 TAB", "15S", "MS5150", "09/2028", 10, 0, "31.00", "45.00", "METFORAL SR 500 TABLET", 15),
          L("ZEBRAZOL 10 MG TAB", "10S", "ZB1150", "10/2028", 2, 0, "43.00", "60.00", "ZEBRAZOL 10 MG TABLET", 10)]
    # Supplier D — photographed thermal receipt (FMCG, no batch / expiry printed)
    d1 = [L("DETTOL LIQUID 125ML", "", "", "", 2, 0, "74", "83", "DETTOL LIQUID 125ML", 1),
          L("MOOV SPRAY 35G", "", "", "", 1, 0, "162", "186", "MOOV SPRAY 35G", 1),
          L("IODEX 20GRMS", "", "", "", 3, 0, "83", "95", "IODEX BALM 20GRMS", 1),
          L("COLGATE PASTE 100GRMS", "", "", "", 2, 0, "66", "73", "COLGATE PASTE 100GRMS", 1)]
    d2 = [L("DETTOL LIQUID 125ML", "", "", "", 4, 0, "74", "83", "DETTOL LIQUID 125ML", 1),
          L("MOOV SPRAY 35G", "", "", "", 2, 0, "162", "186", "MOOV SPRAY 35G", 1),
          L("IODEX 20GRMS", "", "", "", 1, 0, "83", "95", "IODEX BALM 20GRMS", 1),
          L("COLGATE PASTE 100GRMS", "", "", "", 6, 0, "66", "73", "COLGATE PASTE 100GRMS", 1)]
    bulk = bulk_catalogue()
    e1, e2 = [], []
    for i, (name, pack, form, base, unit, upp, mrp, word, brand, strength) in enumerate(bulk):
        printed = f"{brand} {strength} {word}"                      # how this supplier spells it
        respelled = f"{brand}-{strength} {word}S" if word in ("TAB", "CAP") and i % 5 == 0 else printed
        repack = pack.replace("X", " x ") if "X" in pack and i % 4 == 0 else pack
        rate = (Decimal(mrp) * Decimal("0.7")).quantize(Decimal("0.01"))
        e1.append(L(printed, pack, f"E{i:03d}A", f"{1 + i % 12:02d}/2028", 1 + i % 5, 1 if i % 7 == 0 else 0, str(rate), mrp, name, upp, f"E{i:03d}"))
        e2.append(L(respelled, repack, f"E{i:03d}B", f"{1 + i % 12:02d}/2029", 2 + i % 4, 0, str(rate), mrp, name, upp, f"E{i:03d}"))
    out = [Invoice("ANAND MEDICAL AGENCIES", "STRUCTURED", "NR03895", date(2026, 9, 10), a1),
           Invoice("ANAND MEDICAL AGENCIES", "STRUCTURED", "NR03897", date(2026, 9, 18), a2),
           Invoice("CRESCENT SURGICALS", "STRUCTURED", "CS/26/0411", date(2026, 9, 11), b1),
           Invoice("CRESCENT SURGICALS", "STRUCTURED", "CS/26/0502", date(2026, 9, 20), b2),
           Invoice("DECCAN PHARMA DISTRIBUTORS", "PDF_TEXT", "DPD-7731", date(2026, 9, 12), c1),
           Invoice("DECCAN PHARMA DISTRIBUTORS", "PDF_TEXT", "DPD-7790", date(2026, 9, 21), c2),
           Invoice("GOOD HEALTH GENERAL STORE", "OCR", "GH-0041", date(2026, 9, 8), d1),
           Invoice("GOOD HEALTH GENERAL STORE", "OCR", "GH-0042", date(2026, 9, 22), d2),
           Invoice("EVERGREEN PHARMA LLP", "STRUCTURED", "EV/1001", date(2026, 9, 5), e1),
           Invoice("EVERGREEN PHARMA LLP", "STRUCTURED", "EV/1088", date(2026, 9, 25), e2)]
    for inv in out:
        render(inv)
    return out


# --------------------------------------------------------------------------- file renderers
def _marg_csv(inv: Invoice) -> bytes:
    head = ("CustCode,CustName,FeedNo,FeedDate,ProdCode,ProdName,Packing,BatchNo,Qty,Free,Rate,Mrp,ProValue,IGstPer,"
            "Expiry,NetAmt\n")
    net = sum((l.amount * Decimal("1.05")).quantize(Decimal("0.01")) for l in inv.lines)
    rows = [f"526,FAHEEM PHARMACY,{inv.number},{inv.when:%d/%m/%Y},{l.code},{l.printed},{l.pack},{l.batch},{l.qty}.00,"
            f"{l.free}.00,{l.rate},{l.mrp},{l.amount},5.00,{l.expiry[:2]}/{l.expiry[-2:]},{net}" for l in inv.lines]
    return (head + "\n".join(rows) + "\n").encode()


def _xlsx(inv: Invoice) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([inv.supplier])
    ws.append([f"Invoice No: {inv.number}", f"Date: {inv.when:%d-%m-%Y}"])
    ws.append([])
    ws.append(["Sl", "Item Description", "Pack", "Batch No", "Exp Date", "Quantity", "Free Qty", "Purchase Rate", "M.R.P.", "GST %", "Value"])
    for i, l in enumerate(inv.lines, 1):
        ws.append([i, l.printed, l.pack, l.batch, l.expiry, l.qty, l.free, float(l.rate), float(l.mrp), 5, float(l.amount)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_text(inv: Invoice) -> bytes:
    """A small computer-made PDF with a real text layer (written by hand: no PDF library needed)."""
    def esc(t):
        return str(t).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    cols = [("S.No", 30), ("Product Description", 200), ("Pack", 50), ("Batch", 60), ("Exp", 45), ("Qty", 35),
            ("Free", 30), ("MRP", 50), ("Rate", 50), ("GST%", 35), ("Amount", 60)]
    ops = [f"BT /F1 14 Tf 40 800 Td ({esc(inv.supplier)}) Tj ET",
           f"BT /F1 8 Tf 40 785 Td (Invoice No: {esc(inv.number)}   Date: {inv.when:%d/%m/%Y}) Tj ET",
           "BT /F1 8 Tf 40 773 Td (Bill To: FAHEEM PHARMACY) Tj ET"]
    y, x = 740, 30
    for name, w in cols:
        ops.append(f"BT /F1 7 Tf {x + 2} {y} Td ({esc(name)}) Tj ET")
        x += w
    for i, l in enumerate(inv.lines, 1):
        y -= 12
        x = 30
        for (name, w), val in zip(cols, [i, l.printed, l.pack, l.batch, l.expiry, l.qty, l.free, l.mrp, l.rate, 5, l.amount]):
            ops.append(f"BT /F1 7 Tf {x + 2} {y} Td ({esc(val)}) Tj ET")
            x += w
    taxable = sum(l.amount for l in inv.lines)
    ops.append(f"BT /F1 8 Tf 400 {y - 24} Td (Taxable Value: {taxable}) Tj ET")
    ops.append(f"BT /F1 8 Tf 400 {y - 36} Td (Grand Total: {(taxable * Decimal('1.05')).quantize(Decimal('0.01'))}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 700 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _receipt_png(inv: Invoice) -> bytes:
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    font_path = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                                  "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                                  "C:/Windows/Fonts/consola.ttf") if Path(p).is_file()), None)
    font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
    img = Image.new("L", (1100, 200 + 100 * len(inv.lines)), 255)
    d = ImageDraw.Draw(img)
    d.text((40, 20), inv.supplier, font=font, fill=0)
    d.text((40, 60), f"Bill No : {inv.number}   Date : {inv.when:%d-%b-%Y}", font=font, fill=0)
    for x, h in ((80, "QTY"), (330, "MRP"), (560, "RATE"), (800, "TOTAL")):
        d.text((x, 120), h, font=font, fill=0)
    y = 180
    for l in inv.lines:
        d.text((40, y), l.printed, font=font, fill=0)
        y += 40
        for x, v in ((80, f"{l.qty}.00"), (330, str(l.mrp)), (560, str(l.rate)), (800, str(l.amount.normalize()))):
            d.text((x, y), v, font=font, fill=0)
        y += 60
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render(inv: Invoice) -> None:
    if inv.route == "OCR":
        inv.filename, inv.content = f"{inv.number}.png", _receipt_png(inv)
    elif inv.route == "PDF_TEXT":
        inv.filename, inv.content = f"{inv.number}.pdf", _pdf_text(inv)
    elif inv.supplier.startswith("CRESCENT"):
        inv.filename, inv.content = f"{inv.number.replace('/', '-')}.xlsx", _xlsx(inv)
    else:
        inv.filename, inv.content = f"{inv.number.replace('/', '-')}.csv", _marg_csv(inv)


# --------------------------------------------------------------------------- running
def seed_catalogue(db) -> dict[str, int]:
    from app.services import inventory_service as inv, settings_service

    settings_service.set_setting(db, "purchase_automation", "prepare")
    ids = {}
    for name, pack, form, base, unit, upp, mrp, *_ in CATALOGUE + bulk_catalogue():
        item = inv.create_item(db, name=name, pack_size=pack, dosage_form=form, base_unit=base, pack_unit=unit,
                               units_per_pack=upp, mrp=Decimal(mrp))
        ids[name] = item.id
    db.flush()
    return ids


def _expiry_ok(line, expected: str) -> bool:
    if not expected:
        return line.expiry_date is None
    return line.expiry_date is not None and (line.expiry_date.month, line.expiry_date.year) == (int(expected[:2]), int(expected[-4:]))


def evaluate(db, purchase, inv: Invoice, ids: dict, *, recurring: bool, seen: set, started: float) -> Result:
    from app.services import confidence_gate as gate

    res = Result(inv.number, inv.supplier, inv.route, recurring, ms=int((time.perf_counter() - started) * 1000))
    limits = gate.thresholds(db)
    lines = sorted(purchase.items, key=lambda l: l.line_no)
    for line, truth in zip(lines, inv.lines):
        res.lines += 1
        state = gate.assess(line, limits)["state"]
        res.auto += state == gate.AUTO_ACCEPT
        res.warning += state == gate.WARNING
        res.review += state == gate.REVIEW
        res.blocked += state == gate.BLOCK
        d = line.receipt_decision or {}
        res.packs_resolved += bool(d.get("resolved"))
        want_item = ids.get(truth.product) if truth.product else None
        res.product_right += (line.item_id == want_item) if want_item else (line.item_id is None)
        if want_item and truth.product in seen:
            res.known += 1
            res.known_auto += state in (gate.AUTO_ACCEPT, gate.WARNING)
        if state in (gate.AUTO_ACCEPT, gate.WARNING):
            problems = []
            if line.item_id != want_item:
                problems.append(f"product {line.item_id} ≠ {want_item}")
            if truth.batch and line.batch_no != truth.batch:
                problems.append(f"batch {line.batch_no!r} ≠ {truth.batch!r}")
            if truth.expiry and not _expiry_ok(line, truth.expiry):
                problems.append(f"expiry {line.expiry_date} ≠ {truth.expiry}")
            if truth.received is not None and d.get("received_base_units") != truth.received:
                problems.append(f"received {d.get('received_base_units')} ≠ {truth.received}")
            if truth.received is None:
                problems.append("accepted a line whose conversion cannot be known")
            if Decimal(d.get("paid") or 0) != truth.qty or Decimal(d.get("free") or 0) != truth.free:
                problems.append(f"billed/free {d.get('paid')}+{d.get('free')} ≠ {truth.qty}+{truth.free}")
            if problems:
                res.wrong.append({"line": line.line_no, "printed": truth.printed, "problems": problems})
    return res


def review_like_a_person(db, purchase, inv: Invoice, ids: dict) -> None:
    """What the review screen asks of a person on a first invoice: match products, confirm packs."""
    from app.services import packaging_correction, purchasing

    for line, truth in zip(sorted(purchase.items, key=lambda l: l.line_no), inv.lines):
        if truth.product is None:
            purchasing.correct(db, purchase, line, {"new_product": True})
            continue
        want = ids[truth.product]
        if line.item_id != want:
            purchasing.correct(db, purchase, line, {"item_id": want})
        item = line.item
        d = line.receipt_decision or {}
        if truth.per_qty and (not d.get("resolved") or d.get("units_per_invoice_unit") != truth.per_qty):
            upp = item.units_per_pack or 1
            level = "BASE" if truth.per_qty == 1 and upp > 1 else ("OUTER" if truth.per_qty > upp else "RETAIL")
            packaging_correction.apply(db, purchase, line, {
                "level": level, "units_per_retail": str(upp), "retail_per_outer": str(max(truth.per_qty // upp, 1)),
                "scope": "supplier", "reason": "Checked against the carton"})
        warns = [i["code"] for i in (line.issues or []) if i["level"] == "warn" and not i.get("accepted")]
        if warns:
            purchasing.correct(db, purchase, line, {"accept": warns})


def run(db) -> list[Result]:
    """Import every corpus invoice into ``db`` (an empty database) and measure it."""
    from app.services import purchase_automation, purchasing

    ids = seed_catalogue(db)
    suppliers, seen, results = {}, {}, []
    for inv in invoices():
        sup = suppliers.get(inv.supplier)
        recurring = sup is not None
        if sup is None:
            sup = purchasing.save_supplier(db, {"name": inv.supplier})
            suppliers[inv.supplier] = sup
        started = time.perf_counter()
        drafts = purchase_automation.import_file(db, inv.filename, inv.content, supplier_id=sup.id, invoice_no=inv.number,
                                                 invoice_date=inv.when)
        purchase = drafts[0]
        results.append(evaluate(db, purchase, inv, ids, recurring=recurring, seen=seen.setdefault(inv.supplier, set()), started=started))
        if not recurring:
            review_like_a_person(db, purchase, inv, ids)
            purchasing.post(db, purchase, accept_difference=True, line_ids=[l.id for l in purchase.items
                                                                             if l.status in purchasing.POSTABLE])
            seen[inv.supplier].update(l.product for l in inv.lines if l.product)
        db.flush()
    return results


def summarize(results: list[Result]) -> dict:
    out = {}
    for route in ("STRUCTURED", "PDF_TEXT", "OCR"):
        rec = [r for r in results if r.route == route and r.recurring]
        known = sum(r.known for r in rec)
        out[route] = {"invoices": len(rec), "lines": sum(r.lines for r in rec), "known_lines": known,
                      "known_straight_through": round(100 * sum(r.known_auto for r in rec) / known, 1) if known else None,
                      "all_lines_straight_through": round(100 * sum(r.auto + r.warning for r in rec) / max(sum(r.lines for r in rec), 1), 1),
                      "wrong_auto_accepts": sum(len(r.wrong) for r in rec)}
    out["wrong_auto_accepts_total"] = sum(len(r.wrong) for r in results)
    return out
