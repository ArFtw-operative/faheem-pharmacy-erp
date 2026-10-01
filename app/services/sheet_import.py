"""Read product rows from CSV / Excel files.

Shared by purchase-invoice import (supplier CSV/XLSX bills) and the inventory
item import. The header row is found automatically (suppliers often put their
name and address above the table) and columns are recognised by their header
text, so column order and extra columns do not matter. GST / tax columns are
ignored.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

SHEET_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}


class SheetError(Exception):
    pass


def _norm(text: object) -> str:
    """Header key: lowercase letters/digits only (``M.R.P.`` -> ``mrp``)."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip().replace(" ", "")


_ALIASES: dict[str, tuple[str, ...]] = {
    "serial": ("sno", "srno", "sr", "slno", "sl", "no", "sn"),
    "name": (
        "productname", "product", "itemname", "item", "particulars", "description",
        "medicine", "medicinename", "name", "itemdescription", "productdescription",
        "drugname", "brandname",
    ),
    "hsn": ("hsn", "hsncode", "hsnsac", "sac"),
    "pack": ("pack", "packing", "packsize", "pkg", "packaging", "unitpack"),
    "manufacturer": (
        "mfg", "mfr", "manufacturer", "company", "mfgname", "mfrname", "marketedby",
        "manufacturername", "companyname", "mfgby", "make",
    ),
    "batch": ("batch", "batchno", "batchnumber", "bno", "bnumber", "lot", "lotno"),
    "expiry": ("expiry", "exp", "expdate", "expirydate", "expdt", "expiry dt", "expon", "useby"),
    "quantity": ("qty", "quantity", "billedqty", "billqty", "nos", "units", "stock", "openingstock", "closingstock"),
    "free": ("free", "freeqty", "fr", "bonus", "sch", "scheme", "schqty", "freeunits"),
    "mrp": ("mrp", "mrprs", "mrpunit", "maxretailprice"),
    "rate": (
        "rate", "ptr", "prate", "purchaserate", "purrate", "purchaseprice", "cost",
        "costprice", "netrate", "unitprice", "price", "costrate",
    ),
    "amount": ("amount", "amt", "value", "total", "netamount", "linetotal", "netvalue", "totalamount"),
    "category": ("category", "type", "group", "itemtype", "producttype"),
    "generic_name": ("generic", "genericname", "composition", "salt", "molecule"),
    "strength": ("strength", "power", "dosage"),
    "barcode": ("barcode", "ean", "upc", "barcodeno"),
    "selling_rate": ("sellingrate", "salerate", "sellingprice", "saleprice", "sp"),
    "unit": ("unit", "uom"),
    # packaging (opening-stock / item sheets)
    "units_per_pack": ("unitsperpack", "perpack", "packqtyunits", "conversion", "upp", "tabsperstrip",
                       "unitsperstrip", "baseperpack"),
    "loose_sale": ("loose", "loosesale", "allowloose", "sellloose"),
    "loose_qty": ("looseqty", "looseunits", "loosetabs", "loosestock", "extraunits"),
    "base_unit": ("baseunit", "stockunit", "saleunit", "sellingunit"),
    "pack_unit": ("packunit", "purchaseunit", "purchaseuom"),
    "form": ("form", "dosageform"),
}
_HEADER_KEYS: dict[str, str] = {}
for _field, _names in _ALIASES.items():
    for _name in _names:
        _HEADER_KEYS.setdefault(_norm(_name), _field)

# Header words that must never map to a field (a "Mfg Date" column is a date,
# not the manufacturer; "GST Amount" is tax, not the line amount).
_REJECT_HEADER = re.compile(r"(gst|tax|cgst|sgst|igst|cess|disc|mfgdate|mfddate|manufacturingdate|mfgdt)")

_TOTALS_RE = re.compile(
    r"^\s*(sub\s*-?\s*total|grand\s*total|total|net\s*(amount|payable|total)|round\s*off|"
    r"less|add|discount|cgst|sgst|igst|gst|tax|amount\s*in\s*words|e\.?\s*&\s*o\.?\s*e)\b",
    re.I,
)


# purchase-invoice columns recognised by their exact header (checked before the
# tax/discount rejection below, which protects the amount column)
_EXACT_HEADERS = {
    "prodcode": "supplier_code", "productcode": "supplier_code", "itemcode": "supplier_code", "pcode": "supplier_code",
    "prodid": "supplier_code", "code": "supplier_code", "icode": "supplier_code",
    "gst": "gst", "gstper": "gst", "gstpercent": "gst", "gstrate": "gst", "taxrate": "gst", "gstpc": "gst",
    "disc": "discount", "discount": "discount", "discper": "discount", "discpercent": "discount", "discpc": "discount",
    "discamt": "discount_amount", "discountamount": "discount_amount", "discamount": "discount_amount",
}


def field_for_header(header: object) -> str | None:
    key = _norm(header)
    if key in _EXACT_HEADERS:
        return _EXACT_HEADERS[key]
    if not key or _REJECT_HEADER.search(key):
        return None
    if key in _HEADER_KEYS:
        return _HEADER_KEYS[key]
    # "Product Name / Description", "Qty (Nos)", "MRP (Rs)": try each word.
    # Codes, ids and dates ("Item Code", "Batch Date") are never a field.
    words = [_norm(p) for p in re.split(r"[^a-z0-9.]+", str(header).lower()) if _norm(p)]
    if any(w in ("code", "id", "date", "dt") for w in words):
        return None
    for part in words:
        if part in _HEADER_KEYS and part not in ("no", "sn", "sr", "type", "value", "total", "stock"):
            return _HEADER_KEYS[part]
    return None


# --------------------------------------------------------------------------- #
# value parsing
# --------------------------------------------------------------------------- #
def parse_number(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    if text in ("", ".", "-"):
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def has_fraction(value: object) -> bool:
    """True for a quantity like ``3.7`` or ``3.7+0.3`` — never silently truncated to whole units."""
    if value is None or value == "":
        return False
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)) % 1 != 0
    return any(Decimal(n) % 1 != 0 for n in re.findall(r"\d+\.\d+", str(value).replace(",", "")))


def parse_quantity_exact(value: object) -> tuple[Decimal, Decimal]:
    """Like parse_quantity but keeps fractions: ``2.5+0.5`` -> (2.5, 0.5).

    Distributors spread a free-goods scheme over the billed quantity (e.g. 5+1 billed
    as 2.5 + 0.5), so the paid and free parts can be fractional while what arrives is
    whole packs — or whole loose units."""
    if value is None or value == "":
        return Decimal(0), Decimal(0)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)), Decimal(0)
    text = str(value).strip().replace(",", "")
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)", text)
    if m:
        return Decimal(m.group(1)), Decimal(m.group(2))
    m = re.search(r"\d+(?:\.\d+)?", text)
    return (Decimal(m.group(0)), Decimal(0)) if m else (Decimal(0), Decimal(0))


def parse_quantity(value: object) -> tuple[int, int]:
    """``100`` -> (100, 0); ``100+20`` -> (100, 20); ``12.0`` -> (12, 0)."""
    if value is None or value == "":
        return 0, 0
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return int(value), 0
    text = str(value).strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*\+\s*(\d+(?:\.\d+)?)", text)
    if m:
        return int(float(m.group(1))), int(float(m.group(2)))
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return (int(float(m.group(0))), 0) if m else (0, 0)


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_expiry(value: object) -> date | None:
    """Expiry from a cell. Month-only values land on the 1st of that month."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    if not text or text in ("-", "na", "n/a", "nil"):
        return None
    try:
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ t].*)?", text):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", text):
            return date(_year(int(m.group(3))), int(m.group(2)), int(m.group(1)))
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text):
            return date(int(m.group(1)), int(m.group(2)), 1)
        if m := re.fullmatch(r"(\d{1,2})\s*[-/.]\s*(\d{2,4})", text):
            return date(_year(int(m.group(2))), int(m.group(1)), 1)
        if m := re.fullmatch(r"([a-z]{3})[a-z]*[\s\-/.']*(\d{2,4})", text):
            month = _MONTHS.get(m.group(1))
            if month:
                return date(_year(int(m.group(2))), month, 1)
    except ValueError:
        return None
    return None


def clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return "" if text in ("-", "--", "—", "NA", "N/A", "nil", "None") else text


# --------------------------------------------------------------------------- #
# file reading
# --------------------------------------------------------------------------- #
def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _csv_rows(content: bytes) -> list[list[object]]:
    text = _decode(content)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _xlsx_sheets(content: bytes) -> list[list[list[object]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # corrupt / password protected / not really xlsx
        raise SheetError(f"Could not open the Excel file: {exc}") from exc
    sheets = []
    try:
        for ws in wb.worksheets:
            sheets.append([list(row) for row in ws.iter_rows(values_only=True)])
    finally:
        wb.close()
    return sheets


def _xls_sheets(content: bytes) -> list[list[list[object]]]:
    """Old Excel (.xls). Dates come back as real dates, numbers as numbers."""
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as exc:
        raise SheetError(f"Could not open the .xls file: {exc}") from exc
    sheets = []
    for sheet in book.sheets():
        rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                    except Exception:
                        row.append(cell.value)
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        sheets.append(rows)
    return sheets


def _grid(filename: str, content: bytes) -> list[list[list[object]]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        return _xls_sheets(content)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_sheets(content)
    if suffix in (".csv", ".tsv", ".txt"):
        return [_csv_rows(content)]
    raise SheetError(f"Unsupported spreadsheet type: {suffix or filename}")


@dataclass
class SheetTable:
    columns: dict[str, int]          # field -> column index
    headers: list[str]
    rows: list[dict] = field(default_factory=list)   # field -> raw value, plus "_row"
    skipped: int = 0
    totals: dict = field(default_factory=dict)        # printed totals rows: label -> amount cell
    document_number: str = ""        # "Invoice No: X" printed above the table


_DOC_NO_RE = re.compile(r"\b(?:invoice|inv|bill|challan|estimate)\b\.?\s*(?:(?:no|number)\b\.?|#)\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,40})"
                        r"|\b(?:invoice|bill)\s*:\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,40})", re.I)


def _document_number(preamble: list[list[object]]) -> str:
    for row in preamble:
        cells = [clean_text(c) for c in row if clean_text(c)]
        for index, cell in enumerate(cells):
            m = _DOC_NO_RE.search(cell)
            value = m and (m.group(1) or m.group(2))
            if value and value.lower() not in ("no", "number", "date", "to", "dt"):
                return value
            # label and value in neighbouring cells: "Invoice No" | "INV-778"
            if re.fullmatch(r"(invoice|inv|bill)\s*(no|number)?\.?\s*:?", cell, re.I) and index + 1 < len(cells):
                return cells[index + 1][:40]
    return ""


def _find_header(grid: list[list[object]], required: str) -> tuple[int, dict[str, int], list[str]] | None:
    best: tuple[int, dict[str, int], list[str]] | None = None
    for index, row in enumerate(grid[:40]):
        columns: dict[str, int] = {}
        for col, cell in enumerate(row):
            name = field_for_header(cell)
            if name and name not in columns:
                columns[name] = col
        if required not in columns:
            continue
        if len(columns) >= 2 and (best is None or len(columns) > len(best[1])):
            best = (index, columns, [clean_text(c) for c in row])
    return best


def read_table(filename: str, content: bytes, *, required: str = "name") -> SheetTable:
    """Locate the product table in a CSV/XLSX file and return its rows."""
    if not content:
        raise SheetError("The file is empty")
    for grid in _grid(filename, content):
        found = _find_header(grid, required)
        if found is None:
            continue
        header_index, columns, headers = found
        table = SheetTable(columns=columns, headers=headers, document_number=_document_number(grid[:header_index]))
        for offset, raw in enumerate(grid[header_index + 1:], start=header_index + 2):
            values = {
                name: (raw[col] if col < len(raw) else None) for name, col in columns.items()
            }
            name = clean_text(values.get(required))
            if not name:
                if any(clean_text(v) for v in raw):
                    table.skipped += 1
                    # a totals label in another column ("Grand Total" under Code)
                    label = next((clean_text(v) for v in raw if _TOTALS_RE.match(clean_text(v) or "")), "")
                    if label and values.get("amount") not in (None, ""):
                        table.totals[label.lower()] = values.get("amount")
                continue
            if _TOTALS_RE.match(name) or field_for_header(name) == required:
                table.skipped += 1  # totals row or a repeated header on a new page
                if _TOTALS_RE.match(name) and values.get("amount") not in (None, ""):
                    table.totals[name.lower()] = values.get("amount")
                continue
            values["_row"] = offset
            table.rows.append(values)
        return table
    raise SheetError(
        "Could not find a header row. The first row of the table needs column names, "
        "e.g. Product Name, Batch, Expiry, Qty, MRP, Rate."
    )
