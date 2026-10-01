"""Read a computer-generated (text) PDF invoice of any layout into a grid.

No layout is assumed. Three readings are tried and the one that makes the
most sense wins:

* **ruled tables** — PyMuPDF's table finder, for invoices drawn with grid lines;
* **word geometry** — words grouped into lines by position; the header line is
  the one the column mapper understands best; header positions become column
  bands and every word falls into its band (right-aligned numbers included);
  wrapped product names are joined; each page is read with the same bands;
* the legacy reconstructor (``pdf_tables``) for the layouts it was built for.

A reading is scored by how many core fields it found, how many lines it read
and how often ``qty × rate`` agrees with the printed amount. Invoice-level facts
(number, date, supplier name / GSTIN, grand total) come from the page text.
The result is handed to the same column mapper that reads CSV and Excel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_GSTIN = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d])\b")
_INV_NO = re.compile(r"\b(?:invoice|inv|bill|estimate|challan)\b\.?\s*(?:(?:no|number)\b\.?|#)\s*[:#.\-]?\s*([A-Za-z0-9][A-Za-z0-9_\-/]{1,59})"
                     r"|\b(?:invoice|bill)\s*:\s*([A-Za-z0-9][A-Za-z0-9_\-/]{1,59})", re.I)
_DATE = re.compile(r"\b(?:invoice\s+|bill\s+|inv\.?\s+)?date[d]?\s*[:.\-]?\s*(\d{1,2}[/\-.][A-Za-z0-9]{1,3}[/\-.]\d{2,4})", re.I)
_TOTAL = re.compile(r"\b(grand\s*total|net\s*(?:amount|payable|total)|total\s*amount|bill\s*amount|invoice\s*total|amount\s*payable)"
                    r"\s*(?:\(?\s*(?:rs\.?|inr|₹)\s*\)?)?\s*[:=]?\s*(?:rs\.?|₹)?\s*([\d,]+\.\d{2})", re.I)
_TOTALS_ROW = re.compile(r"^\s*(sub\s*-?\s*total|grand\s*total|total|net\s*(amount|payable)|round\s*off|less|add|"
                         r"cgst|sgst|igst|gst|taxable|amount\s*in\s*words|e\.?\s*&\s*o\.?\s*e|carried|b/f|c/f)\b", re.I)


@dataclass
class PdfGrid:
    method: str
    rows: list[list[str]] = field(default_factory=list)    # rows[0] is the header
    score: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class PdfFacts:
    invoice_no: str = ""
    invoice_date: str = ""
    supplier_name: str = ""
    supplier_gstin: str = ""
    grand_total: Decimal | None = None
    text: str = ""


# --------------------------------------------------------------------------- facts from page text
def facts(doc) -> PdfFacts:
    f = PdfFacts()
    pages = [p.get_text("text") for p in doc]
    f.text = "\n".join(pages)
    first = pages[0] if pages else ""
    for m in _INV_NO.finditer(f.text):
        value = m.group(1) or m.group(2)
        if value and value.lower() not in ("no", "number", "date", "dt", "to"):
            f.invoice_no = value
            break
    if m := _DATE.search(f.text):
        f.invoice_date = m.group(1)
    gst = _GSTIN.findall(f.text)
    if gst:
        f.supplier_gstin = gst[0]          # the seller's GSTIN is printed first on Indian invoices
    totals = [m.group(2) for m in _TOTAL.finditer(f.text)]
    if totals:
        try:
            f.grand_total = Decimal(totals[-1].replace(",", ""))
        except InvalidOperation:
            pass
    f.supplier_name = _biggest_text(doc[0]) if len(doc) else ""
    if not f.supplier_name:
        f.supplier_name = next((l.strip() for l in first.splitlines() if re.search(r"[A-Za-z]{3}", l)), "")[:120]
    return f


def _biggest_text(page) -> str:
    """The largest text in the top third of page one — usually the seller's name."""
    best = (0.0, "")
    top = page.rect.height / 3
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s["text"] for s in line["spans"]).strip()
            size = max((s["size"] for s in line["spans"]), default=0)
            if " - Extracted Bill Table" in text:
                return text.split(" - Extracted Bill Table")[0].strip()[:120]
            if line["bbox"][1] < top and re.search(r"[A-Za-z]{3}", text) and not _GSTIN.fullmatch(text) and not re.search(r"invoice|estimate|bill|gst", text, re.I):
                if size > best[0] + 0.5:
                    best = (size, text)
    return best[1][:120]


# --------------------------------------------------------------------------- word geometry
def _lines(page, y_tol: float = 2.5) -> list[list[tuple]]:
    words = sorted(page.get_text("words"), key=lambda w: (round(w[1], 1), w[0]))
    lines: list[list[tuple]] = []
    for w in words:
        mid = (w[1] + w[3]) / 2
        if lines and abs(((lines[-1][0][1] + lines[-1][0][3]) / 2) - mid) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w[0]) for l in lines]


def _cells(line: list[tuple], gap: float | None = None) -> list[tuple[float, float, str]]:
    """Split a line into cells where the horizontal gap is wider than a space."""
    if not line:
        return []
    heights = sorted(w[3] - w[1] for w in line)
    g = gap if gap is not None else max(heights[len(heights) // 2] * 0.9, 4.0)
    cells, cur = [], [line[0]]
    for w in line[1:]:
        if w[0] - cur[-1][2] > g:
            cells.append(cur)
            cur = [w]
        else:
            cur.append(w)
    cells.append(cur)
    return [(c[0][0], c[-1][2], " ".join(w[4] for w in c)) for c in cells]


def _bands(header: list[tuple[float, float, str]], width: float) -> list[tuple[float, float]]:
    """Column x-ranges: boundaries halfway between neighbouring header cells."""
    out = []
    for i, (x0, x1, _) in enumerate(header):
        left = 0 if i == 0 else (header[i - 1][1] + x0) / 2
        right = width if i == len(header) - 1 else (x1 + header[i + 1][0]) / 2
        out.append((left, right))
    return out


def _place(line: list[tuple], bands: list[tuple[float, float]]) -> list[str]:
    cols = [""] * len(bands)
    for x0, x1, text in _cells(line):
        mid = (x0 + x1) / 2
        best, overlap = None, -1.0
        for i, (l, r) in enumerate(bands):
            ov = min(x1, r) - max(x0, l)
            if ov > overlap or (ov == overlap and best is not None and abs(mid - (l + r) / 2) < abs(mid - sum(bands[best]) / 2)):
                best, overlap = i, ov
        if best is not None:
            cols[best] = (cols[best] + " " + text).strip()
    return cols


def _header_score(cells: list[str]) -> int:
    from app.services import column_mapper

    grid = [cells, cells]            # header-only scoring: contents are checked later on the full grid
    m = column_mapper.map_header(grid, 0, sample=0)
    return len([f for f in column_mapper.CORE if f in m.columns]) * 10 + len(m.columns)


def _gutters(lines: list[list[tuple]], width: float, header: list[tuple] | None = None) -> list[tuple[float, float]]:
    """Column spans from the whitespace the data rows leave empty.

    Every body line marks the x-ranges its words cover; x positions that (almost)
    no line covers are gutters between columns. This works whatever the
    alignment — left-aligned text, right-aligned numbers, centred headers."""
    res = 2
    cells = int(width / res) + 2
    cover = [0] * cells
    for line in lines:
        seen = set()
        for w in line:
            for x in range(int(w[0] / res), int(w[2] / res) + 1):
                seen.add(min(x, cells - 1))
        for x in seen:
            cover[x] += 1
    limit = max(1, int(len(lines) * 0.06))
    spans, start = [], None
    for x, c in enumerate(cover):
        busy = c > limit
        if busy and start is None:
            start = x
        elif not busy and start is not None:
            spans.append((start * res, x * res))
            start = None
    if start is not None:
        spans.append((start * res, cells * res))
    # merge spans split by a gap narrower than a space inside one column
    merged: list[list[float]] = []
    for x0, x1 in spans:
        if merged and x0 - merged[-1][1] < 3:
            merged[-1][1] = x1
        else:
            merged.append([x0, x1])
    return [(a, b) for a, b in merged]


def _assign(words: list[tuple], spans: list[tuple[float, float]]) -> list[str]:
    cols = [""] * len(spans)
    for w in words:
        mid = (w[0] + w[2]) / 2
        i = min(range(len(spans)), key=lambda k: 0 if spans[k][0] <= mid <= spans[k][1]
                else min(abs(mid - spans[k][0]), abs(mid - spans[k][1])))
        cols[i] = (cols[i] + " " + w[4]).strip()
    return cols


def _align_header(words: list[tuple], spans: list[tuple[float, float]]) -> list[str]:
    """Give each data column its header text.

    Header cells and data columns come in the same left-to-right order, so they
    are aligned by dynamic programming (order preserved, a column may have no
    header). The distance is the best of left-edge, right-edge and centre
    alignment, which handles left-aligned text, right-aligned numbers and
    centred headers alike. Two-line headers are joined per column."""
    by_line: dict[int, list[tuple]] = {}
    for w in words:
        by_line.setdefault(round((w[1] + w[3]) / 2), []).append(w)
    cells: list[tuple[float, float, str]] = []
    for key in sorted(by_line):
        cells.extend(_cells(sorted(by_line[key], key=lambda w: w[0])))
    # merge a second header line into the cell above it (same column)
    heads: list[list] = []
    for x0, x1, text in sorted(cells, key=lambda c: (c[0])):
        if heads and min(x1, heads[-1][1]) - max(x0, heads[-1][0]) > 0.3 * min(x1 - x0, heads[-1][1] - heads[-1][0]):
            heads[-1] = [min(heads[-1][0], x0), max(heads[-1][1], x1), heads[-1][2] + " " + text]
        else:
            heads.append([x0, x1, text])
    H, S = heads, spans
    n, m = len(H), len(S)
    out = [""] * m
    if not n:
        return out

    def dist(h, s):
        return min(abs(h[0] - s[0]), abs(h[1] - s[1]), abs((h[0] + h[1]) / 2 - (s[0] + s[1]) / 2))

    if n > m:        # more header cells than columns: nearest column, joined
        for h in H:
            j = min(range(m), key=lambda k: dist(h, S[k]))
            out[j] = (out[j] + " " + h[2]).strip()
        return out
    INF = float("inf")
    cost = [[INF] * m for _ in range(n)]
    back = [[-1] * m for _ in range(n)]
    for j in range(m):
        cost[0][j] = dist(H[0], S[j])
    for i in range(1, n):
        best, arg = INF, -1
        for j in range(m):
            if j - 1 >= 0 and cost[i - 1][j - 1] < best:
                best, arg = cost[i - 1][j - 1], j - 1
            if best < INF:
                cost[i][j], back[i][j] = best + dist(H[i], S[j]), arg
    j = min(range(m), key=lambda k: cost[n - 1][k])
    for i in range(n - 1, -1, -1):
        out[j] = H[i][2]
        j = back[i][j]
    return out


def _is_number(v: str) -> bool:
    return bool(re.fullmatch(r"-?[\d,]+(\.\d+)?", v.replace(" ", "")))


def geometry(doc) -> PdfGrid | None:
    # 1. the header line: the one the column mapper understands best (one or two text lines)
    best_header = None
    for pno, page in enumerate(doc):
        lines = _lines(page)
        for i, line in enumerate(lines[:80]):
            cells = _cells(line)
            if len(cells) < 3:
                continue
            options = [([w for w in line], cells, 1)]
            if i + 1 < len(lines):
                merged = _merge_header(cells, _cells(lines[i + 1]))
                if merged:
                    options.append((line + lines[i + 1], merged, 2))
            for words, cand, height in options:
                s = _header_score([c[2] for c in cand])
                if best_header is None or s > best_header[0]:
                    best_header = (s, pno, i, height, words)
    if best_header is None or best_header[0] < 30:
        return None
    hscore, pno, at, height, header_words = best_header
    # 2. body lines on every page after the header (stop at totals)
    body: list[tuple[int, list[tuple]]] = []
    for p in range(pno, len(doc)):
        lines = _lines(doc[p])
        start = at + height if p == pno else 0
        for line in lines[start:]:
            text = " ".join(w[4] for w in line)
            if _TOTALS_ROW.match(text) or re.match(r"^\s*(continued|page\s+\d)", text, re.I):
                if p == len(doc) - 1 or not re.match(r"^\s*(continued|page)", text, re.I):
                    continue
            body.append((p, line))
    numeric_lines = [l for _, l in body if sum(1 for w in l if _is_number(w[4])) >= 2]
    if not numeric_lines:
        return None
    width = max(doc[p].rect.width for p in range(len(doc)))
    spans = _gutters(numeric_lines, width)
    if len(spans) < 3:
        return None
    header = _align_header(header_words, spans)
    rows = [header]
    name_col = _name_column(header)
    for p, line in body:
        vals = _assign(line, spans)
        if p > pno and _header_score(vals) >= hscore - 5:
            continue                                   # header repeated on a new page
        if not any(_is_number(v) for i, v in enumerate(vals) if i != name_col and v):
            # a wrapped product name continues the line above; anything else is page furniture
            if name_col is not None and len(rows) > 1 and vals[name_col] and sum(1 for v in vals if v) == 1:
                rows[-1][name_col] = (rows[-1][name_col] + " " + vals[name_col]).strip()
            continue
        rows.append(vals)
    if len(rows) < 2:
        return None
    return PdfGrid(method="geometry", rows=rows)


def _merge_header(top: list[tuple], below: list[tuple]) -> list[tuple] | None:
    """Join a second header line (units, “No.”) onto the cells above it."""
    if not below or len(below) > len(top) + 2 or any(re.fullmatch(r"[\d,.]+", c[2]) for c in below):
        return None
    out = [list(c) for c in top]
    for x0, x1, text in below:
        mid = (x0 + x1) / 2
        j = min(range(len(out)), key=lambda k: abs(((out[k][0] + out[k][1]) / 2) - mid))
        out[j][2] = f"{out[j][2]} {text}"
        out[j][0], out[j][1] = min(out[j][0], x0), max(out[j][1], x1)
    return [tuple(c) for c in out]


def _name_column(headers: list[str]) -> int | None:
    from app.services import column_mapper

    m = column_mapper.map_header([headers, headers], 0, sample=0)
    return m.columns.get("name")


# --------------------------------------------------------------------------- ruled tables
def ruled(doc) -> PdfGrid | None:
    rows: list[list[str]] = []
    header: list[str] | None = None
    for page in doc:
        try:
            found = page.find_tables()      # lines strategy: drawn grids
        except Exception:
            return None
        for t in found.tables:
            data = [[(c or "").replace("\n", " ").strip() for c in r] for r in t.extract()]
            if not data:
                continue
            if header is None:
                idx = max(range(min(len(data), 6)), key=lambda i: _header_score(data[i]))
                if _header_score(data[idx]) < 30:
                    continue
                header = data[idx]
                rows.append(header)
                data = data[idx + 1:]
            elif _header_score(data[0]) >= _header_score(header) - 5:
                data = data[1:]
            for r in data:
                if len(r) == len(header) and any(r) and not _TOTALS_ROW.match(next((v for v in r if v), "")):
                    rows.append(r)
    if header is None or len(rows) < 2:
        return None
    return PdfGrid(method="ruled", rows=rows)


# --------------------------------------------------------------------------- choosing
def score(grid: PdfGrid) -> float:
    """How much sense a reading makes: core fields, lines read, arithmetic that agrees."""
    from app.services import column_mapper

    m = column_mapper.find_table(grid.rows)
    if m is None:
        return 0.0
    core = sum(1 for f in column_mapper.CORE if f in m.columns)
    body = grid.rows[m.header_row + 1:]
    agree = total = 0
    q, r, a = (m.columns.get(k) for k in ("quantity", "rate", "amount"))
    if None not in (q, r, a):
        for row in body:
            try:
                qty, rate, amt = (float(str(row[c]).replace(",", "").split("+")[0]) for c in (q, r, a))
            except (ValueError, IndexError):
                continue
            total += 1
            if amt and abs(qty * rate - amt) <= max(1.0, amt * 0.12):   # 12%: discounts / tax-inclusive values
                agree += 1
    ratio = agree / total if total else 0.5
    return core * 100 + min(len(body), 500) * 0.5 + ratio * 150
