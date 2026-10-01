"""Understand a supplier's invoice export whatever its column names.

Distributor software exports invoices in its own vocabulary — ``ProdName``,
``BatchNo``, ``IDisPer``, ``FeedNo``, ``NetAmt`` — often as one flat table in
which the invoice header repeats on every line. Instead of a fixed alias list
this module:

1. **tokenises** each header (camelCase, underscores, abbreviations:
   ``IGstPer`` → item · gst · percent, ``ProValue`` → product · amount);
2. **scores** it against every field's vocabulary, with qualifiers that veto a
   look-alike (``Mrp_Old``, ``RetPrice``, ``GstVal1``, ``SumGst``, ``MfgDate``);
3. **profiles the column's values** (numeric? month-year? GST slab? text?) and
   keeps a header guess only when the contents agree;
4. **infers** a missing core column from its contents alone (an unnamed column
   full of ``04/28`` values is the expiry), reported as an inference;
5. separates **invoice-level columns** (values constant per invoice: number,
   date, net amount, rounding, freight …) from line columns.

Every decision carries a reason, so the review screen can show exactly which
column fed which field and which columns were ignored.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------- vocabulary
# Abbreviation → meaning. The base list is data (invoice_vocabulary.json); a
# pharmacy extends it without code changes through the DB setting
# ``invoice_vocabulary`` (JSON object, e.g. {"feed": "invoice", "qtty": "qty"}):
#   python scripts/manage.py setting set invoice_vocabulary '{"qtty": "qty"}'
_BASE_VOCAB: dict[str, str] = json.loads((Path(__file__).with_name("invoice_vocabulary.json")).read_text(encoding="utf-8"))
_CANON: dict[str, str] = dict(_BASE_VOCAB)


def vocabulary(extra: dict | str | None = None) -> dict[str, str]:
    """Base vocabulary plus the pharmacy's own additions (bad entries are ignored)."""
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError:
            extra = {}
    vocab = dict(_BASE_VOCAB)
    for k, v in (extra or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k.strip():
            vocab[k.strip().lower()] = v.strip().lower()
    return vocab


def tokens(header: object, vocab: dict[str, str] | None = None) -> list[str]:
    """``IGstPer`` → [gst, pct] (item prefix dropped); ``Mrp_Old`` → [mrp, old]; ``Batch No.`` → [batch, no]."""
    text = str(header or "").strip()
    if not text:
        return []
    # dotted abbreviations are one word: M.R.P. → MRP, S.No. → SNo, Exp.Dt → Exp Dt
    text = re.sub(r"\b((?:[A-Za-z]\.){2,}[A-Za-z]?)\.?", lambda m: m.group(1).replace(".", ""), text)
    text = re.sub(r"\bC\.?D\.?\s*%", "Disc %", text, flags=re.I)     # CD% = cash discount
    text = text.replace("%", " % ")
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|%", text)
    raw = [p.lower() for p in parts]
    out = []
    # an item-level prefix: IDisPer, IGstPer, ISchPer, IComment — kept as a marker
    canon = vocab or _CANON
    if len(raw) > 1 and raw[0] == "i" and raw[1] in canon:
        raw = raw[1:]
        out.append("item")
    for p in raw:
        if p.isdigit():
            out.append("#")
        elif p in canon:
            out.append(canon[p])
        else:
            out.append(p)
    return out


# field → (required tokens, bonus tokens, veto tokens, base score)
_LINE_RULES: dict[str, list[tuple[set, set, set, int]]] = {
    "name": [({"product", "name"}, set(), {"customer", "supplier", "mfr", "party", "code"}, 100),
             ({"name"}, set(), {"customer", "supplier", "mfr", "party", "code", "invoice"}, 70),
             ({"product"}, {"description"}, {"code", "amount", "disc", "qty", "customer", "rate", "mfr", "#", "date", "barcode"}, 75)],
    "supplier_code": [({"product", "code"}, set(), {"mfr", "customer", "hsn", "barcode"}, 95),
                      ({"code"}, set(), {"mfr", "customer", "hsn", "invoice", "supplier", "party", "barcode"}, 45)],
    "batch": [({"batch"}, {"no"}, {"date", "mfr"}, 100)],
    "expiry": [({"expiry"}, {"date"}, {"mfr"}, 100)],
    "quantity": [({"qty"}, set(), {"free", "scheme", "cases", "amount"}, 100)],
    "free": [({"free"}, {"qty"}, {"amount"}, 100),
             ({"scheme", "qty"}, set(), set(), 90)],
    "rate": [({"rate"}, set(), {"mrp", "retail", "special", "stockist", "sale", "old", "disc", "scheme", "gst", "amount"}, 100)],
    "mrp": [({"mrp"}, set(), {"old", "incl", "amount"}, 100)],
    "amount": [({"product", "amount"}, set(), {"disc", "gst", "sum", "scheme"}, 100),
               ({"amount"}, set(), {"disc", "gst", "sum", "sub", "net", "grand", "invoice", "scheme", "freight", "#"}, 70),
               ({"net", "amount"}, set(), {"disc", "gst", "sum"}, 55)],
    "discount": [({"disc", "pct"}, set(), {"sum", "amount", "scheme"}, 100),
                 ({"disc"}, set(), {"sum", "amount", "scheme"}, 65)],
    "discount_amount": [({"disc", "amount"}, set(), {"sum", "scheme"}, 90)],
    "scheme": [({"scheme", "pct"}, set(), {"sum", "amount"}, 100)],
    "gst": [({"gst", "pct"}, set(), {"amount", "sum", "#", "value", "total"}, 100),
            ({"gst"}, set(), {"amount", "sum", "#", "value", "total", "taxable"}, 60)],
    # the tax charged on one line (lets the GST % be worked out or cross-checked)
    "gst_amount": [({"gst", "amount"}, set(), {"sum", "total", "taxable", "pct", "#"}, 90),
                   ({"gst", "value"}, set(), {"sum", "total", "taxable", "pct", "#"}, 70)],
    "hsn": [({"hsn"}, set(), set(), 100)],
    "pack": [({"pack"}, set(), {"qty", "amount"}, 100)],
    "manufacturer": [({"mfr", "name"}, set(), {"date", "code"}, 100),
                     ({"mfr"}, set(), {"date", "code", "#"}, 80)],
    "barcode": [({"barcode"}, set(), set(), 100)],
}
# invoice-level: the same value on every line of one invoice
_DOC_RULES: dict[str, list[tuple[set, set, set, int]]] = {
    "invoice_no": [({"invoice", "no"}, set(), {"order", "lr", "customer"}, 100), ({"invoice"}, set(), {"date", "amount", "on", "order", "customer"}, 60)],
    "invoice_date": [({"invoice", "date"}, set(), {"order", "lr"}, 100)],
    "net_total": [({"net", "amount"}, set(), {"disc", "gst"}, 100), ({"grand", "amount"}, set(), set(), 95),
                  ({"invoice", "amount"}, set(), set(), 85)],
    "round_off": [({"round"}, set(), set(), 100)],
    # total GST printed on the invoice (reconciled against the lines)
    "printed_gst": [({"sum", "gst"}, set(), {"pct"}, 100), ({"amount", "gst"}, set(), {"pct", "taxable"}, 60)],
    "bill_discount": [({"disc", "pct"}, set(), {"item", "scheme", "sum", "amount"}, 90)],
    "freight": [({"freight"}, set(), set(), 100)],
    "adjust": [({"adjust"}, set(), set(), 100)],
    "credit": [({"credit", "amount"}, set(), set(), 100)],
    "debit": [({"debit", "amount"}, set(), set(), 100)],
    "supplier_name": [({"supplier", "name"}, set(), set(), 100), ({"supplier"}, set(), {"code"}, 70)],
    "supplier_gstin": [({"supplier", "gst"}, {"no"}, set(), 90)],
    "customer_name": [({"customer", "name"}, set(), set(), 100)],
}
CORE = ("name", "quantity", "rate", "mrp", "batch", "expiry")


def _rule_score(toks: list[str], rules) -> tuple[int, str]:
    best, why = 0, ""
    ts = set(toks)
    for required, bonus, veto, base in rules:
        if required <= ts and not (veto & ts):
            extra = len(ts - required - bonus - {"no", "pct", "#", "item"})
            score = base - 12 * extra + (5 if "item" in ts else 0)
            if score > best:
                best, why = score, "+".join(sorted(required))
    return best, why


# --------------------------------------------------------------------------- value profiles
_MY = re.compile(r"^\s*(0?[1-9]|1[0-2])\s*[/\-.\s]\s*(\d{2}|\d{4})\s*$")
_MONTH_NAME = re.compile(r"^\s*[A-Za-z]{3,9}[\s\-/.']*\d{2,4}\s*$")
_DMY = re.compile(r"^\s*\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\s*$")
_NUM = re.compile(r"^\s*-?[\d,]*\.?\d+\s*$")


@dataclass
class Profile:
    filled: float = 0.0       # share of non-empty cells
    numeric: float = 0.0      # share of filled cells that are numbers
    integer: float = 0.0
    month_year: float = 0.0   # 04/28, Apr-2028, 2028-04-01, real dates
    text: float = 0.0         # has letters and is not a code-like token
    distinct: int = 0
    gst_slab: float = 0.0     # 0/5/12/18/28
    avg_len: float = 0.0


def profile(values: list[object]) -> Profile:
    cells = [v for v in values if v not in (None, "") and str(v).strip() != ""]
    p = Profile()
    if not values:
        return p
    p.filled = len(cells) / len(values)
    if not cells:
        return p
    strs = [str(v).strip() for v in cells]
    n = len(cells)
    nums = [v for v, s in zip(cells, strs) if isinstance(v, (int, float)) and not isinstance(v, bool) or _NUM.match(s)]
    p.numeric = len(nums) / n
    p.integer = sum(1 for s in strs if re.fullmatch(r"-?\d+(\.0+)?", s.replace(",", ""))) / n
    p.month_year = sum(1 for v, s in zip(cells, strs) if isinstance(v, (date, datetime)) or _MY.match(s) or _MONTH_NAME.match(s)
                       or re.fullmatch(r"\d{4}-\d{2}-\d{2}( .*)?", s) or _DMY.match(s)) / n
    p.text = sum(1 for s in strs if re.search(r"[A-Za-z]{2}", s) and " " in s or re.fullmatch(r"[A-Za-z][A-Za-z .\-&']{3,}", s)) / n
    p.distinct = len(set(strs))
    p.gst_slab = sum(1 for s in strs if re.fullmatch(r"(0|5|12|18|28|3|0\.25)(\.0+)?", s)) / n
    p.avg_len = sum(len(s) for s in strs) / n
    return p


def _content_ok(fld: str, p: Profile) -> tuple[int, str]:
    """Adjustment from the column's contents: agreement adds, contradiction vetoes."""
    if p.filled == 0:
        return -15, "empty column"
    if fld in ("quantity", "free", "rate", "mrp", "amount", "discount", "discount_amount", "scheme", "gst", "gst_amount",
               "net_total", "round_off", "freight", "adjust", "credit", "debit", "printed_gst"):
        if p.numeric < 0.8:
            return -100, "values are not numbers"
        if fld == "gst" and p.gst_slab >= 0.8:
            return 15, "values are GST slabs"
        if fld in ("quantity", "free") and p.integer < 0.6:
            return -10, "quantities are not whole"
        return 5, "numeric"
    if fld in ("expiry", "invoice_date"):
        return (15, "values look like dates") if p.month_year >= 0.7 else (-100, "values are not dates")
    if fld == "name":
        if p.numeric > 0.5:
            return -100, "values are numbers"
        return (10, "descriptive text") if p.text >= 0.5 or p.avg_len >= 6 else (-20, "short codes, not names")
    if fld == "manufacturer":
        return (5, "text") if p.numeric < 0.5 else (-100, "values are numbers")
    if fld == "hsn":
        return (5, "digits") if p.integer >= 0.8 else (-40, "not HSN digits")
    if fld == "batch":
        return (0, "") if p.month_year < 0.7 else (-100, "values are dates")
    return 0, ""


# --------------------------------------------------------------------------- mapping
@dataclass
class Mapping:
    header_row: int
    headers: list[str]
    columns: dict[str, int] = field(default_factory=dict)        # line field → column
    document: dict[str, int] = field(default_factory=dict)       # invoice field → column
    reasons: dict[str, str] = field(default_factory=dict)        # field → why
    inferred: list[str] = field(default_factory=list)            # fields found from contents only
    ignored: list[str] = field(default_factory=list)             # headers not used
    warnings: list[str] = field(default_factory=list)
    score: int = 0

    def describe(self) -> list[dict]:
        out = [{"field": f, "column": self.headers[c], "level": "line", "reason": self.reasons.get(f, "")}
               for f, c in self.columns.items()]
        out += [{"field": f, "column": self.headers[c], "level": "invoice", "reason": self.reasons.get(f, "")}
                for f, c in self.document.items() if not f.startswith("_")]
        out += [{"field": "", "column": h, "level": "ignored", "reason": "not needed for stock or cost"} for h in self.ignored]
        return out


def _column_values(rows: list[list[object]], col: int) -> list[object]:
    return [r[col] if col < len(r) else None for r in rows]


def _constant_share(values: list[object], groups: list[object] | None) -> float:
    """How invoice-level a column is: 1.0 when the value never changes within an invoice."""
    filled = [(g, v) for g, v in zip(groups or [0] * len(values), values) if v not in (None, "")]
    if len(filled) < 2:
        return 1.0 if filled else 0.0
    by: dict = {}
    for g, v in filled:
        by.setdefault(g, set()).add(str(v).strip())
    return sum(1 for s in by.values() if len(s) == 1) / len(by)


def header_key(header: object) -> str:
    return re.sub(r"[^a-z0-9%]+", "", str(header or "").lower())


LINE_FIELDS = tuple(_LINE_RULES)
DOC_FIELDS = tuple(_DOC_RULES)


def map_header(grid: list[list[object]], index: int, sample: int = 60, learned: dict[str, str] | None = None,
               vocab: dict[str, str] | None = None, hints: dict[str, str] | None = None) -> Mapping:
    header = grid[index]
    headers = [str(c).strip() if c is not None else "" for c in header]
    body = [r for r in grid[index + 1: index + 1 + sample] if any(str(c or "").strip() for c in r)]
    m = Mapping(header_row=index, headers=headers)
    toks = [tokens(h, vocab) for h in headers]
    profiles = [profile(_column_values(body, c)) for c in range(len(headers))]
    taken: set[int] = set()
    for c, h in enumerate(headers):
        fld = (learned or {}).get(header_key(h))
        if not fld:
            continue
        taken.add(c)
        if fld == "ignore":
            continue
        target = m.document if fld in DOC_FIELDS else m.columns
        if fld not in target:
            target[fld], m.reasons[fld] = c, f"column “{h}” — learned from an earlier correction for this supplier"
            m.score += 120
    # proposals from the AI reader: used only where the column's values agree
    for c, h in enumerate(headers):
        fld = (hints or {}).get(h)
        if not fld or c in taken:
            continue
        if fld == "ignore":
            taken.add(c)
            continue
        target = m.document if fld in DOC_FIELDS else m.columns
        adj, note = _content_ok(fld, profiles[c]) if body else (0, "")
        if fld in target or adj <= -100:
            continue
        target[fld], m.reasons[fld] = c, f"column “{h}” — proposed by the AI invoice reader; {note}".rstrip("; ")
        m.inferred.append(fld)
        taken.add(c)
        m.score += 60

    # invoice number first: it decides which columns are constant "per invoice"
    doc_cands: list[tuple[int, str, int, str]] = []
    for c, t in enumerate(toks):
        for fld, rules in _DOC_RULES.items():
            s, why = _rule_score(t, rules)
            if s:
                adj, note = _content_ok(fld, profiles[c]) if body else (0, "")
                doc_cands.append((s + adj, fld, c, f"header “{headers[c]}” ({why}); {note}".rstrip("; ")))
    doc_cands.sort(reverse=True)
    for s, fld, c, why in doc_cands:
        if s >= 50 and fld not in m.document and c not in taken:
            m.document[fld], m.reasons[fld] = c, why
            taken.add(c)
    groups = _column_values(body, m.document["invoice_no"]) if "invoice_no" in m.document else None
    # Invoice numbers are only unique within a supplier, including in bulk exports.
    supplier_cols = [m.document[f] for f in ("supplier_gstin", "supplier_name") if f in m.document]
    if supplier_cols:
        identities = [_column_values(body, c) for c in supplier_cols]
        groups = list(zip(*identities, groups or [""] * len(body)))
    # a candidate invoice-level column that varies within an invoice is really a line column
    learned_cols = {c for c, h in enumerate(headers) if (learned or {}).get(header_key(h)) or (hints or {}).get(h)}
    single_line = bool(body) and len(body) <= len({str(g) for g in groups}) if groups else len(body) <= 1
    for fld in list(m.document):
        c = m.document[fld]
        if fld not in ("invoice_no", "supplier_name", "supplier_gstin") and c not in learned_cols and _constant_share(_column_values(body, c), groups) < 0.9:
            taken.discard(m.document.pop(fld))
            m.reasons.pop(fld, None)
        elif fld == "printed_gst" and single_line and "sum" not in toks[c] and c not in learned_cols:
            # one line per invoice: "GST Amount" cannot be told from a total — read it as the line's GST
            taken.discard(m.document.pop(fld))
            m.reasons.pop(fld, None)

    line_cands: list[tuple[int, str, int, str]] = []
    for c, t in enumerate(toks):
        if c in taken:
            continue
        for fld, rules in _LINE_RULES.items():
            s, why = _rule_score(t, rules)
            if s and fld == "gst_amount" and (any(x.isdigit() for x in t) or (
                    groups and _constant_share(_column_values(body, c), groups) >= 0.9 and len(body) > len(set(map(str, groups))))):
                continue       # GstVal0..4: invoice-level GST per slab, not the tax of one line
            if s:
                adj, note = _content_ok(fld, profiles[c]) if body else (0, "")
                if adj <= -100 and s >= 95:        # unmistakable header, odd values → keep it, say so
                    adj = -30
                    m.warnings.append(f"Column “{headers[c]}” is read as {fld}, but its {note}")
                line_cands.append((s + adj, fld, c, f"header “{headers[c]}” ({why}); {note}".rstrip("; ")))
    line_cands.sort(key=lambda x: (-x[0], x[2]))
    for s, fld, c, why in line_cands:
        if s >= 50 and fld not in m.columns and c not in taken:
            m.columns[fld], m.reasons[fld] = c, why
            taken.add(c)
            m.score += s

    # contents-only inference for core fields the headers did not name
    free_cols = [c for c in range(len(headers)) if c not in taken and profiles[c].filled > 0.5]
    if "expiry" not in m.columns:
        c = next((c for c in free_cols if profiles[c].month_year >= 0.8 and _constant_share(
            _column_values(body, c), groups) < 0.9), None)
        if c is not None:
            m.columns["expiry"], m.reasons["expiry"] = c, f"column “{headers[c]}” holds month-year values"
            m.inferred.append("expiry"); taken.add(c); free_cols.remove(c)
    if "name" not in m.columns:
        texty = sorted((c for c in free_cols if profiles[c].text >= 0.6 and profiles[c].distinct > 1),
                       key=lambda c: -profiles[c].avg_len)
        if texty:
            c = texty[0]
            m.columns["name"], m.reasons["name"] = c, f"column “{headers[c]}” holds the longest descriptive text"
            m.inferred.append("name"); taken.add(c)
    if "mrp" in m.columns and "rate" in m.columns:   # MRP is never below the purchase rate on most lines
        mv, rv = _column_values(body, m.columns["mrp"]), _column_values(body, m.columns["rate"])
        pairs = [(_f(a), _f(b)) for a, b in zip(mv, rv) if _f(a) is not None and _f(b) is not None]
        if len(pairs) >= 3 and sum(1 for a, b in pairs if a < b) / len(pairs) > 0.6:
            m.warnings.append(f"MRP (“{headers[m.columns['mrp']]}”) is below Rate (“{headers[m.columns['rate']]}”) on most "
                              "lines — check whether the two columns are the other way round")
    m.ignored = [h for c, h in enumerate(headers) if h and c not in taken]
    for c, h in enumerate(headers):
        if h in m.ignored and "hsn" in toks[c]:
            bad = [str(v).strip() for v in _column_values(body, c) if str(v or "").strip()
                   and not re.fullmatch(r"\d{4}|\d{6}|\d{8}", str(v).strip())]
            if bad:
                m.warnings.append(f"Ignored HSN-like column {h!r} contains invalid codes (e.g. {bad[0]!r}); verify extraction. Original values remain in source evidence.")
    if "manufacturer" in m.columns:   # a second maker column (ComName + MfgName) fills blanks
        alt = next((c for c in range(len(headers)) if c not in taken and _rule_score(toks[c], _LINE_RULES["manufacturer"])[0] >= 60), None)
        if alt is not None:
            m.document["_manufacturer_alt"] = alt
    return m


def _f(v: object) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def find_table(grid: list[list[object]], max_scan: int = 40, learned: dict[str, str] | None = None,
               vocab: dict[str, str] | None = None, hints: dict[str, str] | None = None) -> Mapping | None:
    """The row that reads best as a header: product name plus at least two other core fields."""
    best: Mapping | None = None
    for index, row in enumerate(grid[:max_scan]):
        if sum(1 for c in row if str(c or "").strip()) < 3:
            continue
        m = map_header(grid, index, learned=learned, vocab=vocab, hints=hints)
        core = [f for f in CORE if f in m.columns]
        if "name" not in m.columns or len(core) < 3:
            continue
        rank = (len(core), m.score)
        if best is None or rank > (len([f for f in CORE if f in best.columns]), best.score):
            best = m
    return best
