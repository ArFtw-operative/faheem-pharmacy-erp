"""Geometry-preserving native PDF table reconstruction.

The default ``pdfplumber.extract_tables`` strategy fails on invoices whose body
rows have no horizontal separators: every product collapses into one tall cell
and is later serialized as a single item. This module reconstructs logical rows
from **word coordinates** instead:

1. Column boundaries come from the drawn vertical rules (falling back to the
   header row's word positions).
2. A serial-number column (small increasing integers) anchors one logical row
   per item. Serial anchors are a strong clue but never mandatory for the
   caller: when no serial column exists the function returns ``None`` and the
   legacy positional parser is used.
3. Every word is assigned to its nearest row anchor and to a column by its
   horizontal centre. Using the centre (rather than a hard crop) recovers
   quantities that overflow their drawn cell, e.g. ``100+20``.
4. Page summaries (B/F, C/F, SUB TOTAL, GRAND TOTAL, headers, footers,
   advertisements) are excluded from items by construction because they carry
   no serial anchor.

Only native text is used here. No OCR and no language model is involved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.services.sheet_import import field_for_header

try:  # pdfplumber is an optional dependency; callers handle absence.
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None


class TableExtractionError(Exception):
    pass


# Header terms used when a page has no drawn column rules.
_HEADER_LABELS = (
    ("serial", ("s.no", "s.no.", "sn", "sr", "sr.no", "sno", "sl", "sl.no")),
    ("product", ("product", "particulars", "description", "item", "medicine")),
    ("quantity", ("qty", "qty.", "quantity", "nos", "nos.")),
    ("mrp", ("m.r.p", "m.r.p.", "mrp", "mrp.")),
    ("rate", ("rate", "p.rate", "ptr", "price")),
    ("amount", ("amount", "amt", "value", "total")),
)

_SUMMARY_RE = re.compile(
    r"\b(total|sub\s*total|grand\s*total|c/?f|b/?f|continued|carried|round\s*off|"
    r"roundoff|page\s*no|for\s+more|contact|gst|gstin|thank|visit|www|call)\b",
    re.I,
)


@dataclass
class NativeRow:
    serial: int | None
    description_raw: str
    quantity_raw: str
    quantity_paid: int
    quantity_free: int
    mrp: Decimal
    rate: Decimal
    line_amount: Decimal
    page: int
    row_id: str
    arithmetic_pass: bool
    method: str = "native_geometry"
    evidence: list[dict] = field(default_factory=list)
    batch: str = ""
    expiry_raw: str = ""
    hsn: str = ""
    pack: str = ""
    manufacturer: str = ""

    def as_dict(self) -> dict:
        return {
            "serial": self.serial,
            "description_raw": self.description_raw,
            "quantity_raw": self.quantity_raw,
            "quantity_paid": self.quantity_paid,
            "quantity_free": self.quantity_free,
            "mrp": str(self.mrp),
            "rate": str(self.rate),
            "line_amount": str(self.line_amount),
            "page": self.page,
            "row_id": self.row_id,
            "arithmetic_pass": self.arithmetic_pass,
            "method": self.method,
        }


@dataclass
class NativeDocument:
    rows: list[NativeRow]
    page_counts: list[int]
    page_sums: list[Decimal]
    printed: dict[str, Decimal | None]
    subtotal: Decimal
    roundoff: Decimal | None
    grand_total: Decimal | None
    document_type: str
    document_number: str
    document_date: str
    party_name: str
    warnings: list[str] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------- #
# low level helpers
# --------------------------------------------------------------------------- #
def _dec(value: str | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    text = re.sub(r"[^\d.\-]", "", str(value))
    if not text:
        return Decimal("0.00")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _int(value: str | None) -> int:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else 0


def _center_x(word: dict) -> float:
    return (word["x0"] + word["x1"]) / 2.0


def _center_y(word: dict) -> float:
    return (word["top"] + word["bottom"]) / 2.0


def _clean_description(words: list[dict]) -> str:
    text = " ".join(w["text"] for w in words)
    text = re.sub(r"\s{2,}", " ", text).strip(" .-|:;,")
    return text[:250]


def _parse_quantity(raw: str) -> tuple[int, int]:
    """Return (paid, free) from a quantity cell such as ``100+20``."""
    text = (raw or "").strip()
    m = re.match(r"^(\d+)\s*\+\s*(\d+)$", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)", text)
    if m:
        return int(m.group(1)), 0
    return 0, 0


def _vertical_rules(page) -> list[float]:
    rules = [
        round(line["x0"], 1)
        for line in page.lines
        if abs(line["x0"] - line["x1"]) < 0.6
    ]
    # de-duplicate and drop the page border if it sits at the margins
    rules = sorted(set(rules))
    return rules


def _cluster_lines(words: list[dict], tolerance: float) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: _center_y(w)):
        if rows and abs(_center_y(word) - _center_y(rows[-1][0])) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    return rows


def _header_label(token: str) -> str | None:
    token = token.strip().lower().rstrip(".")
    for name, aliases in _HEADER_LABELS:
        if token in aliases:
            return name
    field_name = field_for_header(token)
    return "product" if field_name == "name" else field_name


def _header_columns(words: list[dict]) -> list[float] | None:
    """Derive column boundaries from a header row when no rules are drawn."""
    for cluster in _cluster_lines(words, 3.0):
        labels: list[tuple[str, float]] = []
        for word in sorted(cluster, key=lambda w: w["x0"]):
            name = _header_label(word["text"])
            # "Batch No" / "Product Name": the second word belongs to the first
            if name and not (labels and labels[-1][0] == name):
                labels.append((name, _center_x(word)))
        if len({name for name, _ in labels}) >= 3 and any(n == "product" for n, _ in labels):
            labels.sort(key=lambda item: item[1])
            centres = [c for _, c in labels]
            bounds = [0.0]
            for a, b in zip(centres, centres[1:]):
                bounds.append((a + b) / 2.0)
            bounds.append(max(_center_x(w) for w in words) + 40.0)
            return bounds
    return None


def _detect_serial_column(
    words: list[dict], bounds: list[float]
) -> tuple[int, list[dict]] | None:
    """Find the leftmost column dominated by small increasing integers."""
    best: tuple[int, list[dict]] | None = None
    for index in range(len(bounds) - 1):
        left, right = bounds[index], bounds[index + 1]
        column = [
            w for w in words
            if left <= _center_x(w) < right and re.fullmatch(r"\d{1,4}", w["text"].strip())
        ]
        column.sort(key=lambda w: _center_y(w))
        values = [int(w["text"]) for w in column]
        if len(values) < 3:
            continue
        increasing = sum(1 for a, b in zip(values, values[1:]) if b > a)
        if increasing >= max(2, int(len(values) * 0.8)):
            if best is None or len(column) > len(best[1]):
                best = (index, column)
            # serial columns are almost always the first; stop at the first match
            if index == 0:
                break
    return best


# --------------------------------------------------------------------------- #
# row reconstruction
# --------------------------------------------------------------------------- #
def _column_map(
    words: list[dict], bounds: list[float], first_anchor: float, serial_index: int
) -> dict[str, int] | None:
    """Map fields to column indexes from the header text above the first row.

    ``None`` when the header is not recognisable; callers then use the legacy
    fixed order (serial, product, qty, mrp, rate, amount).
    """
    band = [w for w in words if first_anchor - 60.0 <= _center_y(w) < first_anchor - 4.0]
    columns: dict[str, int] = {}
    for index in range(len(bounds) - 1):
        cell = sorted(
            (w for w in band if bounds[index] <= _center_x(w) < bounds[index + 1]),
            key=lambda w: (round(w["top"]), w["x0"]),
        )
        if not cell:
            continue
        name = field_for_header(" ".join(w["text"] for w in cell))
        if name is None:  # e.g. "Product" on one line and "Name" on the next
            for word in cell:
                name = field_for_header(word["text"])
                if name:
                    break
        if name and name not in columns:
            columns[name] = index
    columns.setdefault("serial", serial_index)
    if "name" not in columns or not ({"quantity", "rate", "amount"} & columns.keys()):
        return None
    legacy = {"serial": 0, "name": 1, "quantity": 2, "mrp": 3, "rate": 4, "amount": 5}
    if all(legacy.get(k) == v - serial_index for k, v in columns.items()):
        return None  # classic layout: the fixed order already reads every column
    return columns


def _reconstruct_page(page, page_number: int, previous_map: dict[str, int] | None = None) -> tuple[list[NativeRow], list[str]]:
    warnings: list[str] = []
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return [], warnings

    rules = _vertical_rules(page)
    if len(rules) >= 4:
        bounds = rules + [max(w["x1"] for w in words) + 10.0]
    else:
        bounds = _header_columns(words)
    if not bounds or len(bounds) < 4:
        return [], warnings

    detected = _detect_serial_column(words, bounds)
    if detected is None:
        return [], warnings
    serial_index, serial_words = detected

    # median row pitch (used for row tolerance and continuation attach)
    ys = [_center_y(w) for w in serial_words]
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 10.0
    tol = max(3.0, pitch * 0.55)

    colmap = _column_map(words, bounds, _center_y(serial_words[0]), serial_index)
    if colmap is None and previous_map and max(previous_map.values()) < len(bounds) - 1:
        colmap = previous_map  # continuation page without a repeated header
    product_index = colmap["name"] if colmap else serial_index + 1

    rows: list[NativeRow] = []
    assigned: set[int] = set()
    for serial_word in serial_words:
        anchor = _center_y(serial_word)
        cells: dict[int, list[dict]] = {i: [] for i in range(len(bounds) - 1)}
        for idx, word in enumerate(words):
            if abs(_center_y(word) - anchor) > tol:
                continue
            column = _column_for(word, bounds)
            cells[column].append(word)
            assigned.add(idx)
        rows.append(_build_row(serial_word, cells, bounds, page_number, serial_index, colmap))

    # attach wrapped description lines that carry no serial anchor of their own
    if rows and tol:
        for idx, word in enumerate(words):
            if idx in assigned:
                continue
            column = _column_for(word, bounds)
            if column != product_index:
                continue
            nearest = min(rows, key=lambda r: abs(r._anchor - _center_y(word)))  # type: ignore[attr-defined]
            if abs(nearest._anchor - _center_y(word)) <= pitch * 0.95:  # type: ignore[attr-defined]
                # continuation text is appended in reading order later; store it
                nearest.evidence.append({"continuation": word["text"], "top": round(word["top"], 1)})
                assigned.add(idx)
    page._faheem_colmap = colmap  # type: ignore[attr-defined]  # reused by the next page
    return rows, warnings


def _column_for(word: dict, bounds: list[float]) -> int:
    centre = _center_x(word)
    for index in range(len(bounds) - 1):
        if bounds[index] <= centre < bounds[index + 1]:
            return index
    return len(bounds) - 2


def _build_row(
    serial_word: dict,
    cells: dict[int, list[dict]],
    bounds: list[float],
    page_number: int,
    serial_index: int,
    colmap: dict[str, int] | None = None,
) -> NativeRow:
    for column in cells:
        cells[column].sort(key=lambda w: w["x0"])
    serial = int(serial_word["text"]) if re.fullmatch(r"\d+", serial_word["text"].strip()) else None

    def col(name: str) -> list[dict]:
        return cells.get(colmap[name], []) if colmap and name in colmap else []

    def text(name: str) -> str:
        value = " ".join(w["text"] for w in col(name)).strip()
        return "" if value in ("-", "--", "—") else value

    if colmap:
        product_words = col("name")
        quantity_words = col("quantity")
        mrp_words = col("mrp")
        rate_words = col("rate")
        amount_words = col("amount")
    else:
        product_words = cells.get(serial_index + 1, [])
        quantity_words = cells.get(serial_index + 2, [])
        mrp_words = cells.get(serial_index + 3, [])
        rate_words = cells.get(serial_index + 4, [])
        amount_words = cells.get(serial_index + 5, []) if serial_index + 5 < len(bounds) - 1 else []

    description = _clean_description(product_words)
    quantity_raw = " ".join(w["text"] for w in quantity_words).replace(" ", "")
    paid, free = _parse_quantity(quantity_raw)
    free += _int(text("free")) if colmap and text("free") else 0
    manufacturer = text("manufacturer")
    if re.fullmatch(r"[\d/\-.]+", manufacturer):
        manufacturer = ""  # a manufacturing date, not a company
    mrp = _dec(mrp_words[0]["text"]) if mrp_words else Decimal("0.00")
    rate = _dec(rate_words[0]["text"]) if rate_words else Decimal("0.00")
    amount = _dec(amount_words[0]["text"]) if amount_words else Decimal("0.00")

    arithmetic_pass = paid > 0 and rate > 0 and (Decimal(paid) * rate).quantize(Decimal("0.01")) == amount
    evidence = [
        {
            "text": w["text"],
            "page": page_number,
            "x0": round(w["x0"], 1),
            "x1": round(w["x1"], 1),
            "top": round(w["top"], 1),
            "bottom": round(w["bottom"], 1),
        }
        for w in [serial_word, *product_words, *quantity_words, *mrp_words, *rate_words, *amount_words]
    ]
    row = NativeRow(
        serial=serial,
        description_raw=description,
        quantity_raw=quantity_raw,
        quantity_paid=paid,
        quantity_free=free,
        mrp=mrp,
        rate=rate,
        line_amount=amount,
        page=page_number,
        row_id=f"page-{page_number}-row-{serial}" if serial is not None else f"page-{page_number}-row",
        arithmetic_pass=arithmetic_pass,
        evidence=evidence,
        batch=text("batch")[:60],
        expiry_raw=text("expiry"),
        hsn=text("hsn")[:20],
        pack=text("pack")[:60],
        manufacturer=manufacturer[:150],
    )
    row._anchor = _center_y(serial_word)  # type: ignore[attr-defined]
    return row


# --------------------------------------------------------------------------- #
# document level
# --------------------------------------------------------------------------- #
def _detect_document_type(text: str) -> str:
    upper = text.upper()
    if "ROUGH ESTIMATE" in upper or "ESTIMATE" in upper:
        return "ESTIMATE"
    if "TAX INVOICE" in upper:
        return "TAX_INVOICE"
    if "INVOICE" in upper:
        return "INVOICE"
    if "RECEIPT" in upper or "BILL" in upper:
        return "RECEIPT"
    return ""


def _detect_number(text: str) -> str:
    m = re.search(
        r"(?:estimate|invoice|bill)\s*no\.?\s*[:.]?\s*([A-Za-z0-9\-/:]+)", text, re.I
    )
    return m.group(1).replace(":", "") if m else ""


def _detect_date(text: str) -> str:
    m = re.search(r"(\d{1,2}[\-/.]\d{1,2}[\-/.]\d{2,4})", text)
    return m.group(1) if m else ""


def _detect_party(text: str) -> str:
    m = re.search(r"(M/s[^\n]{0,80}?)(?:\s+ROUGH|\s+ESTIMATE|\s+INVOICE|\s*$)", text, re.I)
    return m.group(1).strip() if m else ""


def _printed_totals(text: str) -> dict[str, Decimal | None]:
    def find(pattern: str) -> Decimal | None:
        m = re.search(pattern, text, re.I)
        return _dec(m.group(1)) if m else None

    return {
        "bf": find(r"TOTAL\s+B/?F\s+([\d,]+\.\d{2})"),
        "cf": find(r"C/?F\s+([\d,]+\.\d{2})"),
        "subtotal": find(r"SUB\s*TOTAL\s+([\d,]+\.\d{2})"),
        "roundoff": find(r"ROUND\s*OFF\s+([\d,]+\.\d{2})"),
        "grand_total": find(r"GRAND\s+TOTAL\s+([\d,]+\.\d{2})"),
    }


def reconstruct(path: str | Path) -> NativeDocument | None:
    """Return a geometry-backed document, or ``None`` when unsupported.

    ``None`` means the caller should fall back to the legacy text parser.
    """
    if pdfplumber is None:
        return None
    path = Path(path)
    rows: list[NativeRow] = []
    page_counts: list[int] = []
    page_sums: list[Decimal] = []
    page_texts: list[str] = []
    warnings: list[str] = []

    with pdfplumber.open(str(path)) as pdf:
        previous_map: dict[str, int] | None = None
        for page_number, page in enumerate(pdf.pages, start=1):
            page_rows, page_warnings = _reconstruct_page(page, page_number, previous_map)
            previous_map = getattr(page, "_faheem_colmap", None) or previous_map
            warnings.extend(page_warnings)
            # Drop rows whose serial repeats a previous page's anchor is not
            # needed here; serials are globally unique in supported documents.
            page_rows = [r for r in page_rows if r.quantity_paid > 0 and r.rate > 0]
            rows.extend(page_rows)
            page_counts.append(len(page_rows))
            page_sums.append(sum((r.line_amount for r in page_rows), Decimal("0.00")))
            try:
                page_texts.append(page.extract_text() or "")
            except Exception:
                page_texts.append("")

    if len(rows) < 3:
        return None
    # Require a serial anchor for at least most rows; otherwise this is not the
    # ruled, numbered table layout this module targets.
    with_serial = sum(1 for r in rows if r.serial is not None)
    if with_serial < max(3, int(len(rows) * 0.8)):
        return None

    full_text = "\n".join(page_texts)
    printed = _printed_totals(full_text)
    subtotal = sum(page_sums, Decimal("0.00"))
    grand = printed.get("grand_total")
    # The rounding adjustment is often printed as a magnitude with no sign.
    # Derive the signed value from the grand total when it is available.
    roundoff: Decimal | None = None
    if grand is not None:
        roundoff = (grand - subtotal).quantize(Decimal("0.01"))
    elif printed.get("roundoff") is not None:
        roundoff = printed["roundoff"]
        warnings.append("roundoff sign not printed; magnitude retained")

    if with_serial < len(rows):
        warnings.append(f"{len(rows) - with_serial} row(s) without a serial anchor")

    return NativeDocument(
        rows=rows,
        page_counts=page_counts,
        page_sums=page_sums,
        printed=printed,
        subtotal=subtotal,
        roundoff=roundoff,
        grand_total=grand,
        document_type=_detect_document_type(full_text),
        document_number=_detect_number(full_text),
        document_date=_detect_date(full_text),
        party_name=_detect_party(full_text),
        warnings=warnings,
    )


def validate(doc: NativeDocument, *, tolerance: Decimal = Decimal("0.01")) -> dict:
    """Deterministic validation. Returns a report; never mutates the document."""
    issues: list[str] = []
    serials = [r.serial for r in doc.rows if r.serial is not None]

    arithmetic_failures = [r.serial for r in doc.rows if not r.arithmetic_pass]
    if arithmetic_failures:
        issues.append(f"{len(arithmetic_failures)} line(s) fail paid qty x rate = amount")

    if serials:
        expected = list(range(min(serials), max(serials) + 1))
        missing = sorted(set(expected) - set(serials))
        duplicates = sorted({s for s in serials if serials.count(s) > 1})
        if missing:
            issues.append(f"serial gaps: {missing[:10]}")
        if duplicates:
            issues.append(f"duplicate serials: {duplicates[:10]}")
        if min(serials) != 1:
            issues.append(f"serials do not start at 1 (starts {min(serials)})")

    printed = doc.printed or {}
    if printed.get("subtotal") is not None and abs(printed["subtotal"] - doc.subtotal) > tolerance:
        issues.append(
            f"subtotal mismatch: printed {printed['subtotal']} vs computed {doc.subtotal}"
        )
    if printed.get("grand_total") is not None and doc.roundoff is not None:
        recomputed = (doc.subtotal + doc.roundoff).quantize(Decimal("0.01"))
        if abs(recomputed - printed["grand_total"]) > tolerance:
            issues.append(
                f"grand total mismatch: printed {printed['grand_total']} vs {recomputed}"
            )
    # cumulative carry checks
    running = Decimal("0.00")
    for index, page_sum in enumerate(doc.page_sums):
        running += page_sum
    if printed.get("cf") is not None and abs(printed["cf"] - running) > tolerance and doc.grand_total is None:
        issues.append(f"carry-forward mismatch: printed {printed['cf']} vs computed {running}")

    total_free = sum(r.quantity_free for r in doc.rows)
    return {
        "item_count": doc.item_count,
        "page_counts": doc.page_counts,
        "page_sums": [str(s) for s in doc.page_sums],
        "subtotal": str(doc.subtotal),
        "roundoff": str(doc.roundoff) if doc.roundoff is not None else None,
        "grand_total": str(doc.grand_total) if doc.grand_total is not None else None,
        "arithmetic_failures": arithmetic_failures,
        "free_units": total_free,
        "issues": issues,
        "status": "valid" if not issues else "review_required",
        "document_type": doc.document_type,
    }
