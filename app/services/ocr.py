"""Scanned PDFs and photos: image clean-up → OCR with word boxes → table rebuilt from positions.

Loaded only for scans; never for CSV/XLSX or a PDF that has a usable text layer. Runs the
local Tesseract binary (Apache-2.0) through ``subprocess`` — no network, no GPU. The
rebuilt grid goes through the same column mapper as spreadsheets, and every row carries
its OCR confidence and whether its arithmetic (qty × rate = amount) reconciles, so the
confidence gate can send doubtful rows to review.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.config import BASE_DIR

HEADER_WORDS = {"QTY", "QUANTITY", "MRP", "RATE", "PRICE", "TOTAL", "AMOUNT", "AMT", "BATCH", "EXP", "EXPIRY", "PACK",
                "PACKING", "FREE", "GST", "HSN", "DISC", "PRODUCT", "ITEM", "DESCRIPTION", "PARTICULARS", "NAME", "VALUE"}
NAME_WORDS = {"PRODUCT", "ITEM", "DESCRIPTION", "PARTICULARS", "NAME"}
FOOTER = re.compile(r"\b(GRAND\s+TOTAL|NET\s+(AMOUNT|AMT|TOTAL)|TOTAL\s+(ITEMS|QTY|AMOUNT)|BILL\s+AMOUNT|NO\.?\s+OF\s+ITEMS|THANK)", re.I)
TIMEOUT = 90


class OcrUnavailable(Exception):
    pass


@dataclass
class Word:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float
    key: tuple

    @property
    def center(self) -> float:
        return self.left + self.width / 2

    @property
    def right(self) -> int:
        return self.left + self.width


# --------------------------------------------------------------------------- engine
def _binary() -> tuple[str, dict] | None:
    env = dict(os.environ)
    custom = os.environ.get("PHARMACY_TESSERACT")
    bundled = BASE_DIR / "tools" / "tesseract"
    exe = None
    if custom and Path(custom).is_file():
        exe = custom
    elif (bundled / "bin" / "tesseract").is_file():
        exe = str(bundled / "bin" / "tesseract")
        env["LD_LIBRARY_PATH"] = f"{bundled / 'lib'}:{env.get('LD_LIBRARY_PATH', '')}"
    elif (bundled / "tesseract.exe").is_file():
        exe = str(bundled / "tesseract.exe")
    else:
        exe = shutil.which("tesseract")
    if not exe:
        return None
    for data in (bundled / "share" / "tessdata", bundled / "tessdata"):
        if (data / "eng.traineddata").is_file():
            env["TESSDATA_PREFIX"] = str(data)
            break
    return exe, env


def available() -> bool:
    return _binary() is not None


def clean(image):
    """Greyscale, contrast stretch and enough resolution for Tesseract (~300 dpi text)."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(image).convert("L")
    if img.width < 1600:
        factor = 1600 / img.width
        img = img.resize((int(img.width * factor), int(img.height * factor)))
    return ImageOps.autocontrast(img, cutoff=1)


def words(image, *, page: int = 1, psm: int = 6) -> list[Word]:
    found = _binary()
    if found is None:
        raise OcrUnavailable("Tesseract OCR is not installed")
    exe, env = found
    buf = io.BytesIO()
    clean(image).save(buf, format="PNG")
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # no console window on Windows
    out = subprocess.run([exe, "stdin", "stdout", "--psm", str(psm), "-l", "eng", "tsv"], input=buf.getvalue(),
                         capture_output=True, timeout=TIMEOUT, env=env, **kwargs)
    if out.returncode != 0:
        raise OcrUnavailable(out.stderr.decode("utf-8", "replace")[-300:] or "OCR failed")
    result = []
    for row in out.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        result.append(Word(cols[11].strip(), int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), conf,
                           (page, int(cols[2]), int(cols[3]), int(cols[4]))))
    return result


def pages(content: bytes, *, is_pdf: bool):
    """Page images: rendered PDF pages, or the photo itself."""
    from PIL import Image

    if not is_pdf:
        yield Image.open(io.BytesIO(content))
        return
    import pypdfium2

    pdf = pypdfium2.PdfDocument(content)
    try:
        for page in pdf:
            yield page.render(scale=3).to_pil()
    finally:
        pdf.close()


def text_chars_per_page(content: bytes) -> float:
    """How much text a PDF's own text layer holds (to decide whether it is a scan)."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(content)
    try:
        n = len(pdf)
        total = sum(len(p.get_textpage().get_text_range().strip()) for p in pdf)
    finally:
        pdf.close()
    return total / n if n else 0.0


# --------------------------------------------------------------------------- table rebuild
_NUM = re.compile(r"^[₹]?\d+(?:[.,]\d+)*$")


def fix_number(text: str) -> str:
    """OCR look-alikes inside numbers only: O→0, l/I→1; a decimal comma 1,00 → 1.00."""
    t = text.strip().strip("|[](){}")
    if re.fullmatch(r"[\dOoIl.,]+", t) and re.search(r"\d", t):
        t = t.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    if re.fullmatch(r"\d+,\d{2}", t):
        t = t.replace(",", ".")
    return t


def is_number(text: str) -> bool:
    return bool(_NUM.match(fix_number(text)))


def lines_of(all_words: list[Word]) -> list[list[Word]]:
    groups: dict[tuple, list[Word]] = {}
    for w in all_words:
        groups.setdefault(w.key, []).append(w)
    ordered = sorted(groups.values(), key=lambda ws: (ws[0].key[0], min(x.top for x in ws)))
    return [sorted(ws, key=lambda x: x.left) for ws in ordered]


def _header(lines: list[list[Word]]):
    for i, line in enumerate(lines):
        hits = [w for w in line if re.sub(r"[^A-Z]", "", w.text.upper()) in HEADER_WORDS]
        if len(hits) >= 2:
            return i, hits
    return None, []


def grid(all_words: list[Word]) -> tuple[list[list[str]], dict[int, float], str]:
    """(grid rows with a header row first, OCR confidence per grid row number, full text)."""
    lines = lines_of(all_words)
    text = "\n".join(" ".join(w.text for w in line) for line in lines)
    start, header = _header(lines)
    if start is None:
        return [], {}, text
    cols = [(re.sub(r"[^A-Z%]", "", w.text.upper()), w.center) for w in header]
    name_col = next((c for c, _ in cols if c in NAME_WORDS), None)
    value_cols = [(c, x) for c, x in cols if c not in NAME_WORDS]
    first_value_x = min((x for _, x in value_cols), default=0)
    # a photographed page is shifted against the header page: learn each page's horizontal offset
    # from lines that hold exactly one number per column, and use it where a number is missing
    shifts: dict[int, list[float]] = {}
    for line in lines[start + 1:]:
        nums = [w for w in line if is_number(w.text)]
        if len(nums) == len(value_cols) and value_cols:
            nums = nums[-len(value_cols):]
            shifts.setdefault(line[0].key[0], []).extend(w.center - x for (_, x), w in zip(value_cols, nums))
    offset = {page: sorted(v)[len(v) // 2] for page, v in shifts.items()}
    out = [["DESCRIPTION", *[c for c, _ in value_cols]]]
    conf: dict[int, float] = {}
    pending: list[Word] = []
    for line in lines[start + 1:]:
        joined = " ".join(w.text for w in line)
        if FOOTER.search(joined) or re.fullmatch(r"[-=_*. ]{6,}", joined):
            if out[1:] and FOOTER.search(joined):
                break
            pending = []
            continue
        numeric = [w for w in line if is_number(w.text)]
        if len(numeric) < 2:
            if len(re.sub(r"[^A-Za-z]", "", joined)) >= 3:
                pending = line                               # a name line of a two-line receipt row
            continue
        if name_col:
            every = cols
            name_words = [w for w in line if min(every, key=lambda cx: abs(cx[1] - w.center))[0] == name_col]
        else:
            centers = sorted(x for _, x in value_cols)
            spacing = min((b - a for a, b in zip(centers, centers[1:])), default=200)
            name_words = [w for w in line if not is_number(w.text) and w.right < first_value_x - spacing * 0.4]
        cells = {c: [] for c, _ in value_cols}
        rest = [w for w in line if w not in name_words]
        nums = [w for w in rest if is_number(w.text)]
        if len(nums) == len(value_cols):
            for (c, _), w in zip(value_cols, nums):      # one number per column: order is safer than x on a skewed photo
                cells[c].append(fix_number(w.text))
        else:
            shift = offset.get(line[0].key[0], 0)
            for w in rest:
                col = min(value_cols, key=lambda cx: abs(cx[1] + shift - w.center))[0]
                cells[col].append(fix_number(w.text) if is_number(w.text) else w.text)
        name_src = name_words or pending
        name = re.sub(r"^[\[({]+[^\])}]*[\])}]\s*", "", " ".join(w.text for w in name_src)).strip(" |")
        if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
            pending = []
            continue
        out.append([name, *[" ".join(cells[c]) for c, _ in value_cols]])
        used = name_src + [w for w in line if w not in name_words]
        conf[len(out)] = round(sum(w.conf for w in used) / len(used) / 100, 3) if used else 0.0
        pending = []
    return out, conf, text


# --------------------------------------------------------------------------- header facts
def header_facts(text: str) -> dict:
    from app.services import gst

    facts = {}
    m = re.search(r"(?:BILL|INVOICE|INV)\s*(?:NO|NUMBER|#)\.?\s*[:;.\-]?\s*([A-Z0-9][A-Z0-9/_\-]{2,})", text, re.I)
    if m:
        facts["invoice_no"] = m.group(1)
    for g in re.findall(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b", text.upper()):
        if not gst.gstin_problem(g):                       # a misread GSTIN fails its checksum and is dropped
            facts["gstin"] = g
            break
    d = re.search(r"\b(\d{1,2}[-/ ](?:[A-Z]{3}|\d{1,2})[-/ ]\d{2,4})\b", text, re.I)
    if d:
        facts["date"] = d.group(1)
    first = next((l.strip() for l in text.splitlines() if len(re.sub(r"[^A-Za-z]", "", l)) >= 5), "")
    if first and not re.search(r"\b(BILL|INVOICE|GSTIN|DATE)\b", first, re.I):
        facts["supplier_name"] = re.sub(r"[^A-Za-z0-9&.\- ]", "", first).strip()[:150]
    return facts


def reconcile_row(raw: dict) -> tuple[bool, str]:
    """qty × rate ≈ amount; on failure try the systematic alternatives (rate↔MRP swap, amount ÷ rate)."""
    def d(v):
        try:
            return Decimal(str(v).replace(",", "")) if str(v).strip() else None
        except InvalidOperation:
            return None
    q, r, m, a = d(raw.get("quantity")), d(raw.get("rate")), d(raw.get("mrp")), d(raw.get("amount"))
    near = lambda x, y: x is not None and y is not None and abs(x - y) <= max(Decimal("0.5"), y * Decimal("0.01"))
    if q and r and near(q * r, a):
        return True, ""
    alternatives = []
    if q and m and near(q * m, a) and r and r > m:
        alternatives.append(("swap rate and MRP", {"rate": raw.get("mrp"), "mrp": raw.get("rate")}))
    if r and a and (a / r) == (a / r).to_integral_value() and 0 < a / r <= 10000 and q is None:
        alternatives.append(("quantity from amount ÷ rate", {"quantity": str((a / r).normalize())}))
    if len(alternatives) == 1:
        how, change = alternatives[0]
        raw.update(change)
        return True, how
    return False, ""
