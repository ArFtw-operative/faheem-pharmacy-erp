"""Read a supplier invoice file into raw lines — nothing is interpreted here.

Supported: CSV, XLSX, XLS and computer-generated (text) PDF. Photos and
scanned PDFs are refused: without a reliable text layer the ERP would have to
guess, and it prefers no data over invented data.

Every value is kept exactly as the supplier wrote it (as text). Identifiers —
batch, product code, HSN — stay strings; an Excel number is written without a
trailing ``.0`` and never in scientific notation. Normalisation and validation
happen later, in :mod:`app.services.purchasing`, which also keeps these raw
values next to the corrected ones.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.services import sheet_import

FIELDS = ("name", "supplier_code", "batch", "expiry", "quantity", "free", "rate", "mrp", "amount", "gst",
          "discount", "discount_amount", "scheme", "hsn", "pack", "manufacturer", "category", "barcode")
CHARGES = ("round_off", "freight", "adjust", "credit", "debit", "bill_discount", "printed_gst")
FORMATS = {".csv": "CSV", ".tsv": "CSV", ".txt": "CSV", ".xlsx": "XLSX", ".xlsm": "XLSX", ".xls": "XLS", ".pdf": "PDF"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".gif"}


class ImportError_(Exception):
    """The file cannot be imported (and why)."""


@dataclass
class RawLine:
    row: int
    raw: dict[str, str]
    page: int | None = None


@dataclass
class RawDocument:
    format: str
    sha256: str
    lines: list[RawLine] = field(default_factory=list)
    invoice_no: str = ""
    invoice_date: str = ""
    supplier_name: str = ""
    declared_total: Decimal | None = None
    warnings: list[str] = field(default_factory=list)
    charges: dict[str, str] = field(default_factory=dict)     # invoice-level amounts read from the file
    customer_name: str = ""                                   # who the invoice is addressed to
    supplier_gstin: str = ""                                  # seller GSTIN printed on the invoice
    method: str = ""                                          # how the table was read
    column_map: list[dict] = field(default_factory=list)      # which column fed which field, and why
    parts: list["RawDocument"] = field(default_factory=list)  # one per invoice when a file holds several


def as_text(value: object) -> str:
    """A cell as the supplier wrote it: identifiers intact, no ``.0``, no ``1e9``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(Decimal(repr(value)), "f")
    if isinstance(value, int):
        return str(value)
    return " ".join(str(value).split())


def _money(text: str) -> Decimal | None:
    cleaned = "".join(ch for ch in str(text or "") if ch.isdigit() or ch in ".-")
    if cleaned in ("", ".", "-"):
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def parse(filename: str, content: bytes, *, learned: dict[str, str] | None = None,
          vocab: dict[str, str] | None = None, advisor=None) -> RawDocument:
    suffix = Path(filename or "").suffix.lower()
    if not content:
        raise ImportError_("The file is empty")
    if suffix in IMAGE_SUFFIXES:
        raise ImportError_("Photos and scans are not imported (no reliable text). Use the supplier's CSV/Excel file, "
                           "a computer-generated PDF, or enter the invoice manually.")
    fmt = FORMATS.get(suffix)
    if fmt is None:
        raise ImportError_(f"Unsupported file type {suffix or filename!r}. Use CSV, XLS, XLSX or a digital PDF.")
    doc = RawDocument(format=fmt, sha256=hashlib.sha256(content).hexdigest())
    if fmt == "PDF":
        _parse_pdf(content, doc, learned=learned, vocab=vocab, advisor=advisor)
    else:
        _parse_sheet(filename, content, doc, learned=learned, vocab=vocab, advisor=advisor)
    if not doc.lines:
        raise ImportError_("No product lines were found in this file.")
    return doc


def _parse_sheet(filename: str, content: bytes, doc: RawDocument, *, learned=None, vocab=None, advisor=None) -> None:
    try:
        grids = sheet_import._grid(filename, content)
    except sheet_import.SheetError as exc:
        raise ImportError_(str(exc)) from exc
    _read_grids(grids, doc, learned=learned, vocab=vocab, advisor=advisor)
    doc.method = "sheet"


def _read_grids(grids, doc: RawDocument, *, learned=None, vocab=None, advisor=None) -> None:
    """Find the product table by *understanding* the columns (see column_mapper),
    whatever the supplier software calls them. Shared by CSV, Excel and PDF."""
    from app.services import column_mapper

    best, best_grid = None, None
    for grid in grids:
        m = column_mapper.find_table(grid, learned=learned, vocab=vocab)
        if m and (best is None or (len(m.columns), m.score) > (len(best.columns), best.score)):
            best, best_grid = m, grid
    missing = [f for f in column_mapper.CORE if best is None or f not in best.columns]
    if missing and advisor is not None:
        # the built-in reader is unsure: ask the AI reader what the columns mean (headers + a few rows only)
        grid, index = _likely_header(grids, vocab)
        if grid is not None:
            hints = advisor([str(c).strip() if c is not None else "" for c in grid[index]], grid[index + 1: index + 9])
            if hints:
                m = column_mapper.find_table(grid, learned=learned, vocab=vocab, hints=hints)
                if m and (best is None or len(m.columns) > len(best.columns)):
                    best, best_grid = m, grid
                    best.warnings.append("Column layout proposed by the AI invoice reader — check the lines before posting")
    if best is None:
        raise ImportError_(_diagnose(grids, vocab))
    m, grid = best, best_grid
    doc.column_map = m.describe()
    doc.warnings.extend(m.warnings)
    if m.inferred:
        doc.warnings.append("Found from the column contents (check them): " + ", ".join(m.inferred))
    doc.invoice_no = sheet_import._document_number(grid[:m.header_row])
    # letterhead above the table: seller GSTIN, invoice date, supplier name
    from app.services import pdf_invoice

    pre = "\n".join(" ".join(as_text(c) for c in r if as_text(c)) for r in grid[:m.header_row])
    if g := pdf_invoice._GSTIN.search(pre.upper()):
        doc.supplier_gstin = g.group(1)
    pre_date = (pdf_invoice._DATE.search(pre) or [None, ""])[1] if pre else ""
    doc.supplier_name = next((l.strip() for l in pre.splitlines() if re.search(r"[A-Za-z]{3}", l)
                              and not re.search(r"invoice|bill|gst|date|phone|ph\b|address|dl\s*no", l, re.I)), "")[:120]
    alt = m.document.get("_manufacturer_alt")
    groups: dict[str, RawDocument] = {}
    order: list[str] = []
    for offset, row in enumerate(grid[m.header_row + 1:], start=m.header_row + 2):
        cell = lambda c: row[c] if c is not None and c < len(row) else None
        raw = {f: as_text(cell(c)) for f, c in m.columns.items()}
        if alt is not None and not raw.get("manufacturer"):
            raw["manufacturer"] = as_text(cell(alt))
        name = raw.get("name", "")
        if not any(raw.values()):
            continue
        if not name or sheet_import._TOTALS_RE.match(name):
            # a printed totals line: remember the amount, never a product
            label = next((as_text(v) for v in row if sheet_import._TOTALS_RE.match(as_text(v) or "")), "")
            if label and raw.get("amount"):
                doc.charges.setdefault("_printed_totals", {})[label.lower()] = raw["amount"]
            continue
        key = as_text(cell(m.document.get("invoice_no"))) or ""
        part = groups.get(key)
        if part is None:
            part = groups[key] = RawDocument(format=doc.format, sha256=doc.sha256)
            order.append(key)
            part.invoice_no = key or doc.invoice_no
            part.invoice_date = as_text(cell(m.document.get("invoice_date"))) or pre_date
            total = _money(as_text(cell(m.document.get("net_total"))))
            part.declared_total = total
            part.customer_name = as_text(cell(m.document.get("customer_name")))
            for f in CHARGES:
                v = _money(as_text(cell(m.document.get(f))))
                if v is not None and v != 0:
                    part.charges[f] = str(v)
        part.lines.append(RawLine(row=offset, raw=raw))
    parts = [groups[k] for k in order]
    printed = doc.charges.pop("_printed_totals", {})
    if len(parts) == 1 and parts[0].declared_total is None:
        for label in ("grand total", "net amount", "net payable", "total"):
            hit = next((v for k, v in printed.items() if k.startswith(label) and _money(v) is not None), None)
            if hit:
                parts[0].declared_total = _money(hit)
                break
    for part in parts:
        part.column_map, part.warnings = doc.column_map, list(doc.warnings)
        part.supplier_gstin, part.supplier_name = doc.supplier_gstin, doc.supplier_name
    if len(parts) > 1:
        doc.parts = parts
        doc.warnings.append(f"This file holds {len(parts)} invoices; each becomes its own draft.")
    if parts:
        first = parts[0]
        doc.lines, doc.invoice_no, doc.invoice_date = first.lines, first.invoice_no, first.invoice_date
        doc.declared_total, doc.charges, doc.customer_name = first.declared_total, first.charges, first.customer_name


def _likely_header(grids, vocab):
    from app.services import column_mapper

    best = (None, 0, -1)
    for grid in grids:
        for index, row in enumerate(grid[:40]):
            filled = sum(1 for c in row if str(c or "").strip())
            if filled < 3:
                continue
            text = sum(1 for c in row if re.search(r"[A-Za-z]", str(c or "")))
            known = len(column_mapper.map_header(grid, index, vocab=vocab).columns)
            score = known * 10 + text
            if score > best[2]:
                best = (grid, index, score)
    return best[0], best[1]


def _diagnose(grids, vocab) -> str:
    """Explain what was understood when no product table could be found."""
    from app.services import column_mapper

    best = None
    for grid in grids:
        for index, row in enumerate(grid[:40]):
            if sum(1 for c in row if str(c or "").strip()) < 3:
                continue
            m = column_mapper.map_header(grid, index, vocab=vocab)
            if best is None or len(m.columns) > len(best.columns):
                best = m
    if best is None or not best.columns:
        return ("No product table was found. The file needs a header row with column names; "
                "none of the rows looked like one.")
    found = ", ".join(f"{f} ← “{best.headers[c]}”" for f, c in best.columns.items())
    missing = [f for f in column_mapper.CORE if f not in best.columns]
    return (f"The product table could not be read with confidence. Understood: {found}. "
            f"Not found: {', '.join(missing)}. Rename those columns in the file, or add the supplier's "
            "abbreviation to the invoice vocabulary.")


def quality(d: RawDocument) -> float:
    """How complete and self-consistent a reading is (used to pick between PDF readings)."""
    if not d.lines:
        return 0.0
    core = ("name", "quantity", "rate", "mrp", "batch", "expiry")
    def present(l, f):
        v = (l.raw.get(f) or "").strip()
        return bool(v) and not (f in ("rate", "mrp", "quantity") and (_money(v) or 0) == 0)
    complete = sum(sum(1 for f in core if present(l, f)) / len(core) for l in d.lines) / len(d.lines)
    agree = total = 0
    for l in d.lines:
        q, r, a = (_money((l.raw.get(k) or "").split("+")[0]) for k in ("quantity", "rate", "amount"))
        if q and r and a:
            total += 1
            agree += abs(q * r - a) <= max(Decimal("1"), a * Decimal("0.12"))
    return complete * 100 + (agree / total if total else 0.3) * 60 + min(len(d.lines), 500) * 0.05


def _parse_pdf(content: bytes, doc: RawDocument, *, learned=None, vocab=None, advisor=None) -> None:
    """Any computer-generated PDF: several readings are tried and the most sensible one kept."""
    import pymupdf

    from app.services import pdf_invoice

    try:
        pdf = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ImportError_(f"Could not open the PDF: {exc}") from exc
    if pdf.needs_pass:
        raise ImportError_("The PDF is password protected")
    text = "\n".join(page.get_text("text") for page in pdf)
    if len(text.strip()) < 40:
        pdf.close()
        raise ImportError_("This PDF is a scan (no text layer). Scans are not imported — use the supplier's CSV/Excel "
                           "file, a computer-generated PDF, or enter the invoice manually.")
    candidates: list[RawDocument] = []
    try:
        facts = pdf_invoice.facts(pdf)
        for reader in (pdf_invoice.ruled, pdf_invoice.geometry):
            try:
                grid = reader(pdf)
            except Exception:
                grid = None
            if grid is None:
                continue
            d = RawDocument(format=doc.format, sha256=doc.sha256)
            try:
                _read_grids([grid.rows], d, learned=learned, vocab=vocab, advisor=advisor)
            except ImportError_:
                continue
            d.method = grid.method
            candidates.append(d)
    finally:
        pdf.close()
    legacy = _legacy_pdf(content, doc)
    if legacy is not None:
        candidates.append(legacy)
    if not candidates:
        raise ImportError_("No invoice table could be read from this PDF. Use the supplier's CSV/Excel file or "
                           "enter the invoice manually.")
    best = max(candidates, key=quality)
    for f in ("lines", "invoice_no", "invoice_date", "declared_total", "charges", "customer_name", "column_map",
              "parts", "method", "supplier_name"):
        setattr(doc, f, getattr(best, f))
    doc.warnings.extend(best.warnings)
    # facts printed on the page fill what the table did not say
    doc.invoice_no = doc.invoice_no or facts.invoice_no
    doc.invoice_date = doc.invoice_date or facts.invoice_date
    doc.supplier_name = doc.supplier_name or facts.supplier_name
    doc.supplier_gstin = facts.supplier_gstin
    if doc.declared_total is None:
        doc.declared_total = facts.grand_total
    for part in doc.parts:
        part.supplier_gstin = doc.supplier_gstin
        part.invoice_no = part.invoice_no or facts.invoice_no
        part.invoice_date = part.invoice_date or facts.invoice_date
    doc.warnings.append(f"PDF table read by {doc.method} reading")


def _legacy_pdf(content: bytes, doc: RawDocument) -> RawDocument | None:
    from app.services import pdf_tables

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(content)
        path = Path(fh.name)
    try:
        native = pdf_tables.reconstruct(path)
    except Exception:
        native = None
    finally:
        path.unlink(missing_ok=True)
    if native is None or not native.rows:
        return None
    d = RawDocument(format=doc.format, sha256=doc.sha256, method="estimate layout")
    d.invoice_no = native.document_number or ""
    d.invoice_date = native.document_date or ""
    d.supplier_name = native.party_name or ""
    d.declared_total = native.grand_total
    d.warnings.extend(native.warnings)
    for index, r in enumerate(native.rows, start=1):
        raw = {
            "name": r.description_raw, "batch": r.batch, "expiry": r.expiry_raw, "quantity": r.quantity_raw,
            "rate": as_text(r.rate), "mrp": as_text(r.mrp), "amount": as_text(r.line_amount),
            "hsn": r.hsn, "pack": r.pack, "manufacturer": r.manufacturer,
        }
        if r.quantity_free:
            raw["free"] = str(r.quantity_free)
        d.lines.append(RawLine(row=r.serial or index, raw=raw, page=r.page))
    return d
