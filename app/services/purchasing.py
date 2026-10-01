"""Purchases: the inventory intake authority.

Flow (nothing reaches stock before the last step)::

    file / manual entry → raw lines (evidence kept) → normalise → validate
      → product match (deterministic) → review & correction → POST (atomic)
      → batches + PURCHASE_RECEIPT / FREE_STOCK ledger movements

Line statuses: READY · CORRECTED · NEEDS_REVIEW · PRODUCT_MATCH_REQUIRED ·
INVALID · POSTED. Only an invoice whose lines are all READY/CORRECTED posts.

Rules that never bend:
* raw supplier values are kept; every correction records the old value, the
  new value, who and when;
* nothing is guessed — expiry, batch, product, pack conversion and cost are
  either proven or sent to review (fuzzy matching only *suggests* products);
* identifiers stay text; a batch in scientific notation is flagged;
* the same supplier invoice posts once (also enforced by a unique index);
* a total that does not reconcile is shown, and posting it needs an explicit
  acknowledgement.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.config import UPLOAD_DIR
from app.models import Item, Purchase, PurchaseItem, Supplier, SupplierProductMap, User
from app.sequences import next_number
from app.services import purchase_import, sheet_import, stock_ledger, units
from app.utils import money, utcnow

READY, CORRECTED, REVIEW, MATCH, INVALID, POSTED, CLOSED = (
    "READY", "CORRECTED", "NEEDS_REVIEW", "PRODUCT_MATCH_REQUIRED", "INVALID", "POSTED", "CLOSED")
DONE = {POSTED, CLOSED}          # a line that no longer changes: received, or closed as not received
POSTABLE = {READY, CORRECTED}
EDITABLE = ("name", "supplier_code", "batch", "expiry", "quantity", "free", "rate", "mrp", "amount", "gst", "gst_amount", "discount", "scheme",
            "hsn", "pack", "manufacturer", "category", "dosage_form", "base_unit", "pack_unit", "units_per_pack")
# warnings a person may accept as they are (everything else must be corrected)
ACCEPTABLE = {"mrp_below_rate", "expiry_soon", "amount_mismatch", "far_expiry", "units_suggested",
              "gst_slab", "gst_changed", "gst_hsn_mixed", "gst_amount_mismatch"}
_SCI = re.compile(r"^\d+(?:\.\d+[eE][+-]?\d+|[eE][+-]\d+)$")


class PurchaseError(Exception):
    def __init__(self, message: str, code: str = "", details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


# --------------------------------------------------------------------------- helpers
def description_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()[:250]


def parse_date(text: Any) -> date | None:
    """An invoice date: ISO ``2026-09-30`` or Indian ``30/09/2026``, ``30-09-26``, ``30-Sep-2026``."""
    if isinstance(text, date):
        return text
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y",
                "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:11].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _dec(text: Any) -> Decimal | None:
    if text in (None, ""):
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(text).replace(",", ""))
    if cleaned in ("", ".", "-"):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _actor(user: User | None) -> str:
    return (user.employee_id or user.username) if user else "system"


def effective(line: PurchaseItem) -> dict[str, str]:
    """The value of every field after corrections (raw where uncorrected)."""
    raw = dict(line.raw or {})
    for key, c in (line.corrections or {}).items():
        if not key.startswith("_") and isinstance(c, dict) and "value" in c:
            raw[key] = c["value"]
    return raw


def _supplier(db: Session, supplier_id: int | None) -> Supplier | None:
    return db.get(Supplier, supplier_id) if supplier_id else None


# --------------------------------------------------------------------------- creation
def _new_purchase(db: Session, *, supplier_id: int | None, invoice_no: str, invoice_date: date | None,
                  supplier_total: Any, source_format: str, user: User | None) -> Purchase:
    purchase = Purchase(
        supplier_id=supplier_id, invoice_no=(invoice_no or "").strip()[:60], invoice_date=invoice_date,
        supplier_total=money(_dec(supplier_total)) if _dec(supplier_total) is not None else None,
        source_format=source_format, status="DRAFT", created_by=user.id if user else None,
    )
    db.add(purchase)
    db.flush()
    return purchase


def _vocab(db: Session) -> dict[str, str]:
    from app.services import column_mapper, settings_service

    return column_mapper.vocabulary(settings_service.get_setting(db, "invoice_vocabulary", ""))


def _parse(db: Session, filename: str, content: bytes, supplier: Supplier | None) -> purchase_import.RawDocument:
    try:
        from app.services import invoice_agent

        return purchase_import.parse(filename, content, learned=(supplier.column_profile or {}) if supplier else None,
                                     vocab=_vocab(db), advisor=invoice_agent.advisor(db))
    except purchase_import.ImportError_ as exc:
        raise PurchaseError(str(exc), "UNREADABLE")


def _stage(db: Session, purchase: Purchase, part: purchase_import.RawDocument) -> None:
    """Put a parsed invoice's raw lines into the draft and check each one."""
    purchase.charges = dict(part.charges) or None
    purchase.column_map = part.column_map or None
    notes = list(part.warnings)
    if not purchase.supplier_id and (part.supplier_name or part.supplier_gstin):
        notes.append(f"Supplier on the invoice: {part.supplier_name or ''}{' · GSTIN ' + part.supplier_gstin if part.supplier_gstin else ''}"
                     " — not in the supplier master yet (F2 to choose, Purchases → Suppliers to add)")
    if part.customer_name:
        from rapidfuzz import fuzz

        from app.services import settings_service

        profile = settings_service.get_profile(db)
        ours = profile.get("name") if isinstance(profile, dict) else getattr(profile, "name", "")
        if ours and fuzz.token_set_ratio(ours.lower(), part.customer_name.lower()) < 70:
            notes.append(f"Invoice is addressed to “{part.customer_name}”, not {ours}")
    purchase.extraction_meta = "; ".join(notes)[:2000]
    for index, raw in enumerate(part.lines, start=1):
        line = PurchaseItem(line_no=index, raw=raw.raw, source_serial=raw.row,
                            source_page=raw.page, product_name=(raw.raw.get("name") or "")[:250],
                            description_raw=(raw.raw.get("name") or "")[:250], corrections={})
        purchase.items.append(line)
        db.flush()
        refresh_line(db, purchase, line)
    _refresh_totals(purchase)
    db.flush()


def import_file(db: Session, filename: str, content: bytes, *, supplier_id: int | None = None,
                invoice_no: str = "", invoice_date: date | None = None, supplier_total: Any = None,
                user: User | None = None) -> list[Purchase]:
    """Stage a supplier file for review: one draft per invoice found in it."""
    supplier = _supplier(db, supplier_id)
    doc = _parse(db, filename, content, supplier)
    if supplier is None and doc.supplier_gstin:
        # the seller's GSTIN printed on the invoice identifies the supplier exactly
        found = db.scalar(select(Supplier).where(func.upper(Supplier.gst_number) == doc.supplier_gstin.upper()))
        if found is not None:
            supplier, supplier_id = found, found.id
            doc = _parse(db, filename, content, supplier)          # re-read with that supplier's learned layout
    dup = db.scalar(select(Purchase).where(Purchase.source_sha256 == doc.sha256, Purchase.status != "CANCELLED"))
    if dup is not None:
        raise PurchaseError(f"This file was already imported as {dup.reference_no or 'draft #' + str(dup.id)} "
                            f"({dup.status.lower()}).", "DUPLICATE_FILE", {"purchase_id": dup.id})
    parts = doc.parts or [doc]
    folder = UPLOAD_DIR / "purchases"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc.sha256[:16]}{Path(filename).suffix.lower()}"
    path.write_bytes(content)
    single = len(parts) == 1
    out = []
    for part in parts:
        purchase = _new_purchase(
            db, supplier_id=supplier_id, invoice_no=(invoice_no if single and invoice_no else part.invoice_no),
            invoice_date=(invoice_date if single and invoice_date else parse_date(part.invoice_date)),
            supplier_total=supplier_total if single and supplier_total not in (None, "") else part.declared_total,
            source_format=doc.format, user=user)
        purchase.source_file, purchase.source_sha256 = str(path), doc.sha256
        _stage(db, purchase, part)
        audit.record(db, action=audit.A_CREATE, entity_type="purchase", entity_id=purchase.id, user=user,
                     after={"file": filename, "lines": len(part.lines), "format": doc.format, "invoice": purchase.invoice_no},
                     details=f"Purchase draft imported from {filename}: {len(part.lines)} line(s)")
        out.append(purchase)
    return out


def create_from_file(db: Session, filename: str, content: bytes, **kwargs) -> Purchase:
    """The first (usually only) draft staged from a file."""
    return import_file(db, filename, content, **kwargs)[0]


def remap(db: Session, purchase: Purchase, changes: dict[str, str], *, user: User | None = None) -> Purchase:
    """A person tells the system what a supplier column means. The choice is
    remembered for that supplier and the draft is re-read from its file.

    ``changes`` maps the supplier's column header to a field name, or ``ignore``.
    """
    from app.services import column_mapper

    _draft(purchase)
    if not purchase.source_file or not Path(purchase.source_file).is_file():
        raise PurchaseError("This draft has no supplier file to re-read")
    if not purchase.supplier_id:
        raise PurchaseError("Choose the supplier first — the column layout is remembered per supplier")
    allowed = set(column_mapper.LINE_FIELDS) | set(column_mapper.DOC_FIELDS) | {"ignore"}
    bad = {v for v in changes.values() if v not in allowed}
    if bad:
        raise PurchaseError("Unknown field: " + ", ".join(sorted(bad)))
    supplier = purchase.supplier or db.get(Supplier, purchase.supplier_id)
    profile = dict(supplier.column_profile or {})
    for header, fld in changes.items():
        key = column_mapper.header_key(header)
        if not key:
            continue
        # one column per field: a field moved to a new column forgets its old one
        for k, v in list(profile.items()):
            if v == fld and fld != "ignore":
                profile.pop(k)
        profile[key] = fld
    supplier.column_profile = profile
    content = Path(purchase.source_file).read_bytes()
    doc = _parse(db, purchase.source_file, content, supplier)
    part = next((p for p in (doc.parts or [doc]) if p.invoice_no == purchase.invoice_no), (doc.parts or [doc])[0])
    for line in list(purchase.items):
        purchase.items.remove(line)
        db.delete(line)
    db.flush()
    _stage(db, purchase, part)
    if part.declared_total is not None:
        purchase.supplier_total = part.declared_total
    audit.record(db, action=audit.A_UPDATE, entity_type="supplier", entity_id=supplier.code, user=user,
                 after={"column_profile": changes}, details=f"Column layout for {supplier.name} corrected; draft re-read")
    return purchase


def create_manual(db: Session, *, supplier_id: int | None, invoice_no: str = "", invoice_date: date | None = None,
                  supplier_total: Any = None, user: User | None = None) -> Purchase:
    purchase = _new_purchase(db, supplier_id=supplier_id, invoice_no=invoice_no, invoice_date=invoice_date,
                             supplier_total=supplier_total, source_format="MANUAL", user=user)
    audit.record(db, action=audit.A_CREATE, entity_type="purchase", entity_id=purchase.id, user=user,
                 details="Manual purchase draft created")
    return purchase


def _draft(purchase: Purchase) -> None:
    if purchase.status != "DRAFT":
        raise PurchaseError(f"Purchase is {purchase.status.lower()}; only drafts can change", "NOT_DRAFT")


def _open(purchase: Purchase) -> None:
    """Lines not yet received can still be worked on in a draft or a partly received purchase."""
    if purchase.status not in ("DRAFT", "PARTIAL"):
        raise PurchaseError(f"Purchase is {purchase.status.lower()}; it can no longer change", "NOT_OPEN")


def _open_line(line: PurchaseItem) -> None:
    if line.status in DONE:
        raise PurchaseError(f"Line {line.line_no} is {line.status.lower()} and can no longer change", "LINE_DONE")


def add_line(db: Session, purchase: Purchase, values: dict, *, user: User | None = None) -> PurchaseItem:
    _open(purchase)
    no = (max((l.line_no for l in purchase.items), default=0) or 0) + 1
    raw = {k: purchase_import.as_text(v) for k, v in values.items() if k in EDITABLE}
    line = PurchaseItem(line_no=no, raw=raw, product_name=raw.get("name", "")[:250],
                        description_raw=raw.get("name", "")[:250], corrections={})
    if values.get("item_id"):
        item = db.get(Item, int(values["item_id"]))
        if item is None:
            raise PurchaseError("Product not found")
        line.item, line.match_method, line.product_name = item, "MANUAL", item.name
        raw.setdefault("name", item.name)
    purchase.items.append(line)
    db.flush()
    refresh_line(db, purchase, line)
    _refresh_totals(purchase)
    db.flush()
    return line


def delete_line(db: Session, purchase: Purchase, line: PurchaseItem, *, user: User | None = None) -> None:
    _open(purchase)
    _open_line(line)
    audit.record(db, action=audit.A_DELETE, entity_type="purchase_line", entity_id=line.id, user=user,
                 before={"raw": line.raw, "corrections": line.corrections}, details=f"Line {line.line_no} removed")
    purchase.items.remove(line)
    db.delete(line)
    db.flush()
    _refresh_totals(purchase)


# --------------------------------------------------------------------------- normalise, match, validate
def _issue(issues: list, code: str, field: str, level: str, message: str) -> None:
    issues.append({"code": code, "field": field, "level": level, "message": message})


def _normalise(db: Session, purchase: Purchase, line: PurchaseItem) -> list[dict]:
    v = effective(line)
    issues: list[dict] = []
    line.product_name = (v.get("name") or (line.item.name if line.item else "")).strip()[:250]
    line.supplier_code = (v.get("supplier_code") or "").strip()[:40]
    line.hsn_code = (v.get("hsn") or "").strip()[:20]
    line.pack_size = (v.get("pack") or "").strip()[:60]
    line.manufacturer = (v.get("manufacturer") or "").strip()[:150]

    batch = (v.get("batch") or "").strip()
    line.batch_no = batch[:60]
    if not batch:
        _issue(issues, "batch_missing", "batch", "review", "Batch number is missing")
    elif _SCI.match(batch) or "#" in batch:
        _issue(issues, "batch_corrupt", "batch", "review", f"Batch “{batch}” looks damaged by a spreadsheet (e.g. 2.61E+09) — enter it from the invoice")

    raw_exp = (v.get("expiry") or "").strip()
    line.expiry_raw = raw_exp[:40]
    exp = None if (not raw_exp or "#" in raw_exp) else sheet_import.parse_expiry(raw_exp)
    if exp is not None:
        exp = exp.replace(day=1)  # month/year only — the ERP never invents a day
    line.expiry_date = exp
    today = date.today()
    if not raw_exp:
        _issue(issues, "expiry_missing", "expiry", "review", "Expiry is missing")
    elif exp is None:
        _issue(issues, "expiry_invalid", "expiry", "review", f"Expiry “{raw_exp}” is not a month/year (e.g. May-2028)")
    else:
        if units.is_expired(exp, today):
            _issue(issues, "expired", "expiry", "review", f"Already expired ({exp:%b-%Y})")
        elif purchase.invoice_date and (exp.year, exp.month) < (purchase.invoice_date.year, purchase.invoice_date.month):
            _issue(issues, "expiry_before_invoice", "expiry", "review", "Expiry is before the invoice date")
        elif (exp.year - today.year) * 12 + exp.month - today.month <= 3:
            _issue(issues, "expiry_soon", "expiry", "warn", f"Expires soon ({exp:%b-%Y})")
        elif exp.year > today.year + 12:
            _issue(issues, "far_expiry", "expiry", "warn", f"Unusually far expiry ({exp:%b-%Y})")

    qraw = (v.get("quantity") or "").strip()
    fraw = (v.get("free") or "").strip()
    line.quantity_raw = qraw[:30]
    paid_x, free_in_qty = sheet_import.parse_quantity_exact(qraw)
    free_x = sheet_import.parse_quantity_exact(fraw)[0] + free_in_qty
    received = paid_x + free_x
    scheme_split = bool(paid_x % 1 or free_x % 1)     # a free-goods scheme spread over the billed quantity
    upp = (line.item.units_per_pack if line.item is not None else None) or line.units_per_pack
    if not scheme_split:
        line.quantity, line.quantity_free = int(paid_x), int(free_x)
    elif received % 1 == 0:                            # 2.5 billed + 0.5 free = 3 whole packs arrive
        line.quantity, line.quantity_free = int(received), 0
    else:                                              # 2.5 + 1 = 3.5 strips = 35 tablets arrive
        line.quantity, line.quantity_free = int(received), 0
        if not upp or upp <= 1:
            _issue(issues, "qty_fraction", "quantity", "review",
                   f"“{qraw}{'+' + fraw if fraw else ''}” = {_n(received)} packs received — half packs can only be stocked as loose units: "
                   "confirm how many units one pack holds, or correct the quantity from the invoice")
        elif (received * upp) % 1 != 0:
            _issue(issues, "qty_fraction", "quantity", "review",
                   f"“{qraw}{'+' + fraw if fraw else ''}” = {_n(received)} packs = {_n(received * upp)} units — not a whole number of units; "
                   "correct the quantity from the invoice")
    paid = paid_x
    if paid <= 0:
        _issue(issues, "qty_missing", "quantity", "review", "Quantity must be at least 1")

    rate, mrp = _dec(v.get("rate")), _dec(v.get("mrp"))
    line.rate = money(rate) if rate is not None else Decimal("0")
    line.mrp = money(mrp) if mrp is not None else Decimal("0")
    if rate is None:
        _issue(issues, "rate_missing", "rate", "review", "Purchase rate is missing")
    if mrp is None or mrp <= 0:
        _issue(issues, "mrp_missing", "mrp", "review", "MRP is missing")
    elif rate is not None and mrp < rate:
        _issue(issues, "mrp_below_rate", "mrp", "warn", f"MRP ₹{money(mrp)} is below the rate ₹{money(rate)}")
    gst = _dec(v.get("gst"))
    line.gst_rate = gst if gst is not None and 0 <= gst <= 40 else None
    if gst is not None and not (0 <= gst <= 40):
        _issue(issues, "gst_invalid", "gst", "review",
               f"GST “{v.get('gst')}” is not a GST percentage. How to fix: press Enter and type the GST % printed on the invoice line "
               "(e.g. 5); if the file's GST column holds amounts, open Columns (F9) and map it as GST amount.")
    disc_pct = _dec(v.get("discount"))
    disc_amt = _dec(v.get("discount_amount"))
    scheme_pct = _dec(v.get("scheme"))
    for label, pct in (("Discount", disc_pct), ("Scheme", scheme_pct)):
        if pct is not None and not (0 <= pct <= 100):
            _issue(issues, "pct_invalid", "discount" if label == "Discount" else "scheme", "review", f"{label} {pct}% is not a percentage")
    gross = money((rate or 0) * paid)
    item_disc = money(disc_amt) if disc_amt is not None else money(gross * disc_pct / 100) if disc_pct else Decimal("0")
    scheme = money((gross - item_disc) * scheme_pct / 100) if scheme_pct else Decimal("0")
    line.discount = item_disc + scheme
    taxable = money(gross - line.discount)          # the value stock is bought at, before GST
    amount = _dec(v.get("amount"))
    if (disc_pct is None and disc_amt is None and scheme_pct is None and amount is not None and rate is not None
            and paid and Decimal("0.5") * gross <= money(amount) < gross - Decimal("0.01")
            and abs(money(amount) - money(taxable * (1 + (line.gst_rate or Decimal(0)) / 100))) > Decimal("1")):
        # no discount column, but the supplier's printed line value is below qty × rate:
        # their figure is the value billed, the gap is the discount they gave
        line.discount = money(gross - money(amount))
        taxable = money(amount)
    line.line_total = taxable if rate is not None else (money(amount) if amount is not None else Decimal("0"))
    if scheme_split and rate is not None and received > 0 and not issues_has(issues, "qty_fraction"):
        # the value billed covers everything received: the cost per pack is spread over all of it
        what = f"{_n(received)} packs" if received % 1 == 0 else f"{_n(received)} packs = {_n(received * upp)} units"
        _issue(issues, "scheme_split", "quantity", "info",
               f"Scheme: {_n(paid_x)} billed + {_n(free_x)} free = {what} received · billed ₹{money(taxable)} before GST "
               f"= ₹{money(taxable / received)} per pack (invoice rate ₹{money(rate)})")
    if amount is not None and rate is not None and paid:
        # suppliers print the line value before discount, after discount, or with GST — any of them reconciles
        with_tax = money(taxable * (1 + (line.gst_rate or Decimal(0)) / 100))
        near = lambda x: abs(money(amount) - x) <= max(Decimal("1"), x * Decimal("0.01"))
        if not (near(gross) or near(taxable) or near(with_tax)):
            _issue(issues, "amount_mismatch", "amount", "warn",
                   f"Printed value ₹{money(amount)} matches neither qty × rate (₹{gross}), after discount (₹{taxable}) nor with GST (₹{with_tax})")
    _gst_checks(db, purchase, line, v, taxable if rate is not None else line.line_total, issues)
    return issues


def _gst_history(db: Session, purchase: Purchase, line: PurchaseItem) -> list[dict]:
    """After matching: the GST % against what this product carried on its latest posted purchase."""
    from app.services import gst as G

    issues: list[dict] = []
    item_id = line.item.id if line.item is not None else line.item_id     # set before the flush fills item_id
    if line.gst_rate is None or item_id is None:
        return issues
    rate_n = Decimal(line.gst_rate).normalize()
    last = G.last_known_rate(db, item_id, purchase.id)
    if last and last[0] != rate_n:
        _issue(issues, "gst_changed", "gst", "warn",
               f"GST changed for this product: {G.fmt_rate(last[0])} on {last[1] or 'its last purchase'}"
               f"{' (' + last[2].strftime('%d-%b-%Y') + ')' if last[2] else ''}, {G.fmt_rate(rate_n)} on this invoice. "
               "How to fix: if the rate really changed (supplier's new invoice, rate revision), press F6 to accept; "
               "if it is a typing error on the invoice file, press Enter and correct GST %.")
    return issues


def _received_packs(line: PurchaseItem) -> Decimal:
    """Packs that arrive: paid + free, with scheme splits (2.5 + 0.5) kept exact."""
    v = effective(line)
    paid, free_in = sheet_import.parse_quantity_exact((v.get("quantity") or "").strip())
    return paid + free_in + sheet_import.parse_quantity_exact((v.get("free") or "").strip())[0]


def _gst_checks(db: Session, purchase: Purchase, line: PurchaseItem, v: dict, taxable: Decimal, issues: list) -> None:
    """GST % resolution and checks for one line. Every message says what is wrong and how to fix it."""
    from app.services import gst as G

    pol = G.policy(db)
    when = purchase.invoice_date or date.today()
    printed_amt = _dec(v.get("gst_amount"))
    line.gst_source = "FILE" if line.gst_rate is not None else ""
    if line.gst_rate is None and not issues_has(issues, "gst_invalid"):
        if printed_amt is not None and taxable and taxable > 0:
            implied = printed_amt / taxable * 100
            slab = G.nearest_slab(implied, pol.valid_for(when) | pol.rates_before)
            if slab is not None:
                line.gst_rate, line.gst_source = slab, "DERIVED"
                _issue(issues, "gst_derived", "gst", "info",
                       f"GST % not printed: {G.fmt_rate(slab)} worked out from the GST amount ₹{money(printed_amt)} on taxable ₹{money(taxable)}")
            else:
                _issue(issues, "gst_missing", "gst", "review",
                       f"GST % not printed, and the GST amount ₹{money(printed_amt)} on taxable ₹{money(taxable)} is {implied:.2f}% — not a GST slab. "
                       "How to fix: press Enter and type the GST % from the invoice; if the taxable value looks wrong, check the discount / scheme columns (F9).")
        else:
            others = [l for l in purchase.items if l is not line and (_dec(effective(l).get("gst")) is not None or _dec(effective(l).get("gst_amount")) is not None)]
            last = G.last_known_rate(db, line.item.id if line.item is not None else line.item_id, purchase.id)
            hint = f" This product was bought at {G.fmt_rate(last[0])} GST on {last[1] or 'an earlier purchase'}." if last else ""
            if others:
                _issue(issues, "gst_missing", "gst", "review",
                       f"GST % is missing on this line, while {len(others)} other line(s) of this invoice show GST.{hint} "
                       "How to fix: press Enter and type the GST % printed on the invoice line.")
            else:
                line.gst_source = "NONE"
                _issue(issues, "gst_not_shown", "gst", "info",
                       "This invoice file shows no GST, so the line is taken as 0% GST." + hint +
                       " If the paper invoice shows GST, open Columns (F9) and map its GST % column; otherwise nothing to do.")
    rate = line.gst_rate
    if rate is None:
        return
    rate_n = Decimal(rate).normalize()
    valid = pol.valid_for(when)
    if valid and rate_n not in valid:
        slabs = ", ".join(G.fmt_rate(r) for r in sorted(valid))
        if pol.changed_on and when >= pol.changed_on and rate_n in pol.rates_before:
            new = {Decimal(12): "5%", Decimal(28): "18% or 40%"}.get(rate_n, "a current slab")
            msg = (f"GST {G.fmt_rate(rate_n)} is an old slab: from {pol.changed_on:%d-%b-%Y} the {G.fmt_rate(rate_n)} rate was withdrawn "
                   f"(medicines at 12% moved to 5%, most 28% items to 18% or 40%), and this invoice is dated {when:%d-%b-%Y}. "
                   f"How to fix: look at the paper invoice. If it also prints {G.fmt_rate(rate_n)}, the supplier billed an old rate — ask them "
                   f"for a corrected invoice or credit note, then press Enter and set GST % to the corrected rate (normally {new}). "
                   f"If the paper shows {new}, the file is wrong: press Enter and set GST % to {new}.")
        else:
            msg = (f"GST {G.fmt_rate(rate_n)} is not a GST slab for an invoice dated {when:%d-%b-%Y} (valid: {slabs}). "
                   "How to fix: check the invoice line and press Enter to set the GST % it prints. "
                   "If the slabs themselves have changed, update them: manage.py setting set gst_rates \"0,5,18,40\".")
        _issue(issues, "gst_slab", "gst", "warn", msg)
    if line.hsn_code:
        for other in purchase.items:
            if other is line or other.status in DONE or other.hsn_code != line.hsn_code or other.gst_rate is None:
                continue
            if Decimal(other.gst_rate).normalize() != rate_n:
                _issue(issues, "gst_hsn_mixed", "gst", "warn",
                       f"HSN {line.hsn_code} is billed at {G.fmt_rate(rate_n)} here but at {G.fmt_rate(other.gst_rate)} on line {other.line_no}. "
                       "One HSN code normally carries one GST rate. How to fix: compare both lines on the paper invoice and correct the "
                       "wrong GST % (Enter); if both are right (different products under one HSN), press F6.")
                break
    if printed_amt is not None and taxable:
        factor = G.bill_discount_factor(purchase)
        expected = money(taxable * factor * rate_n / 100)
        if abs(printed_amt - expected) > max(Decimal("1"), expected * Decimal("0.01")):
            implied = printed_amt / (taxable * factor) * 100 if taxable * factor else Decimal(0)
            _issue(issues, "gst_amount_mismatch", "gst", "warn",
                   f"The invoice prints GST ₹{money(printed_amt)} for this line, but {G.fmt_rate(rate_n)} of the taxable ₹{money(taxable * factor)} "
                   f"is ₹{expected} (the printed amount is {implied:.2f}%). How to fix: if the GST % is wrong, press Enter and correct it; "
                   "if the taxable value is off, check the rate, discount and scheme columns (Columns, F9).")


def _cost_per_pack(line: PurchaseItem, include_gst: bool) -> Decimal:
    """What one pack cost the shop: the billed value (after discounts, scheme and bill discount)
    spread over every pack received, plus the GST paid on it when the policy includes GST."""
    packs = _received_packs(line)
    if not packs:
        return line.rate
    value = line.landed_total if include_gst else line.taxable_value
    return money(Decimal(value) / packs) if value is not None else line.rate


def _n(d: Decimal) -> str:
    """2.0 -> "2", 2.50 -> "2.5" (never scientific notation)."""
    return str(int(d)) if d % 1 == 0 else format(d.normalize(), "f")


def _loose_receipt_units(line: PurchaseItem, item: Item) -> int | None:
    """A scheme split that arrives as part packs (2.5 + 1 = 3.5 strips) is received in loose units
    (35 tablets). None for everything received in whole packs."""
    v = effective(line)
    paid, free_in = sheet_import.parse_quantity_exact((v.get("quantity") or "").strip())
    received = paid + free_in + sheet_import.parse_quantity_exact((v.get("free") or "").strip())[0]
    if received % 1 == 0:
        return None
    units = received * (item.units_per_pack or 1)
    if units % 1:
        raise PurchaseError(f"Line {line.line_no}: {_n(received)} packs is not a whole number of units", "LINES_NOT_READY")
    return int(units)


def issues_has(issues: list, code: str) -> bool:
    return any(i["code"] == code for i in issues)


def _match(db: Session, purchase: Purchase, line: PurchaseItem) -> None:
    """Deterministic matching only. Suggestions come separately and are never applied."""
    if line.item is not None and line.match_method in ("MANUAL", "SUPPLIER_MAP", "CODE", "EXACT_NAME"):
        return
    if line.new_product:
        return
    line.item, line.match_method = None, ""
    if purchase.supplier_id:
        key = description_key(line.product_name)
        m = None
        if line.supplier_code:
            m = db.scalar(select(SupplierProductMap).where(SupplierProductMap.supplier_id == purchase.supplier_id,
                                                           SupplierProductMap.supplier_code == line.supplier_code))
        if m is None and key:
            m = db.scalar(select(SupplierProductMap).where(SupplierProductMap.supplier_id == purchase.supplier_id,
                                                           SupplierProductMap.supplier_code == "",
                                                           SupplierProductMap.description_key == key))
        if m is not None and m.item and m.item.deleted_at is None:
            line.item, line.match_method = m.item, "SUPPLIER_MAP"
            return
    if line.supplier_code:
        item = db.scalar(select(Item).where(Item.deleted_at.is_(None),
                                            (Item.article_id == line.supplier_code) | (Item.barcode == line.supplier_code)))
        if item is not None:
            line.item, line.match_method = item, "CODE"
            return
    if line.product_name:
        item = db.scalar(select(Item).where(Item.deleted_at.is_(None),
                                            func.lower(Item.name) == line.product_name.strip().lower()))
        if item is not None:
            line.item, line.match_method = item, "EXACT_NAME"


def _product_issues(db: Session, line: PurchaseItem) -> list[dict]:
    issues: list[dict] = []
    if line.item is not None:
        return issues
    if not line.new_product:
        _issue(issues, "no_product", "name", "match", "Choose the matching product, or confirm it as a new product")
        return issues
    from app.services import category_service

    if not line.product_name:
        _issue(issues, "new_name", "name", "review", "Enter the new product's name")
    if not line.category or line.category not in category_service.names(db):
        _issue(issues, "new_category", "category", "review", "Choose a category for the new product")
    if not line.units_per_pack:
        _issue(issues, "new_uom", "units_per_pack", "review", "Confirm how many sale units one pack holds")
    elif (line.corrections or {}).get("_units_suggested"):
        base = (line.base_unit or "unit").lower()
        _issue(issues, "units_suggested", "units_per_pack", "warn",
               f"Check units: 1 {(line.pack_unit or 'pack').lower()} = {line.units_per_pack} {base}(s) — read from pack “{line.pack_size}”, "
               "which can mean more than one thing. F6 confirms, Enter changes it.")
    return issues


def refresh_line(db: Session, purchase: Purchase, line: PurchaseItem) -> PurchaseItem:
    """Re-derive everything from raw + corrections, then set the status."""
    if line.status in DONE:
        return line
    issues = _normalise(db, purchase, line)
    _match(db, purchase, line)
    if issues_has(issues, "qty_fraction") and line.item is not None:
        issues = _normalise(db, purchase, line)      # part packs are judged by the matched product's pack size
    if not issues_has(issues, "gst_slab"):
        issues += _gst_history(db, purchase, line)
    issues += _product_issues(db, line)
    accepted = set((line.corrections or {}).get("_accepted") or [])
    for i in issues:
        i["accepted"] = i["code"] in accepted and i["code"] in ACCEPTABLE
    open_ = [i for i in issues if not i["accepted"]]
    if any(i["level"] == "block" for i in open_):
        line.status = INVALID
    elif any(i["level"] == "match" for i in open_):
        line.status = MATCH
    elif any(i["level"] in ("review", "warn") for i in open_):
        line.status = REVIEW
    else:
        line.status = CORRECTED if any(not k.startswith("_") for k in (line.corrections or {})) else READY
    line.issues = issues
    line.match_status = line.match_method or ("NEW" if line.new_product else "REVIEW")
    return line


def _refresh_totals(purchase: Purchase) -> None:
    purchase.total = Decimal(totals(purchase, received_only=True)["total"])
    _apply_gst(purchase)


def _apply_gst(purchase: Purchase) -> None:
    """GST snapshot of every open line: taxable after bill discount, CGST/SGST or IGST, landed value
    and the rate per pack including GST. Posted lines keep the snapshot they were posted with."""
    from sqlalchemy.orm import object_session

    from app.services import gst as G

    db = object_session(purchase)
    if db is None:
        return
    pol = G.policy(db)
    mode, _ = G.supply_type(pol.our_gstin, purchase.supplier.gst_number if purchase.supplier else "")
    if not any(l.status == POSTED for l in purchase.items):   # fixed once stock has been received on it
        purchase.supply_type = mode
    factor = G.bill_discount_factor(purchase)
    for line in purchase.items:
        if line.status in DONE:
            continue
        b = G.line_breakdown(line, factor, purchase.supply_type or mode, _received_packs(line))
        line.taxable_value, line.gst_amount, line.landed_total = b["taxable"], b["gst"], b["landed"]
        line.cgst_amount, line.sgst_amount, line.igst_amount = b["cgst"], b["sgst"], b["igst"]
        line.landed_rate = b["rate_incl"]


def gst_summary(db: Session, purchase: Purchase) -> dict:
    """The invoice's GST: per-slab table, totals, CGST/SGST vs IGST and why, the GST printed on the
    invoice (when the file has it) with the difference, and the GSTIN checks — each with how to fix."""
    from app.services import gst as G

    pol = G.policy(db)
    supplier_gstin = purchase.supplier.gst_number if purchase.supplier else ""
    mode, why = G.supply_type(pol.our_gstin, supplier_gstin)
    mode = purchase.supply_type or mode
    factor = G.bill_discount_factor(purchase)
    rows = [G.line_breakdown(l, factor, mode, _received_packs(l)) for l in purchase.items if l.status != CLOSED]
    total = sum((r["gst"] for r in rows), Decimal(0))
    printed = _dec((purchase.charges or {}).get("printed_gst"))
    problems = []
    if printed is not None and abs(printed - total) > Decimal("1"):
        slabs = "; ".join(f"{G.fmt_rate(r['rate'])}: ₹{money(r['gst'])} on ₹{money(r['taxable'])}" for r in G.by_rate(rows))
        problems.append(f"The invoice prints total GST ₹{money(printed)}, the lines add up to ₹{money(total)} "
                        f"(difference ₹{money(printed - total)}). Per slab: {slabs}. How to fix: find the line(s) whose GST % differs from the "
                        "paper invoice (Needs review / GST warnings first) and correct them; a missing or extra line also shows here.")
    ours = G.gstin_problem(pol.our_gstin)
    if ours:
        problems.append(f"Pharmacy GSTIN {ours}. It decides CGST + SGST versus IGST. How to fix: "
                        "python scripts/manage.py setting set gst_number <your 15-character GSTIN>.")
    theirs = G.gstin_problem(supplier_gstin)
    if theirs:
        problems.append(f"Supplier GSTIN {theirs}. How to fix: Purchases → Suppliers, edit "
                        f"{purchase.supplier.name if purchase.supplier else 'the supplier'} and enter the GSTIN printed on their invoice.")
    return {"mode": mode, "mode_reason": why, "total": str(money(total)), "printed": str(printed) if printed is not None else None,
            "by_rate": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in G.by_rate(rows)],
            "cost_includes_gst": pol.cost_includes_gst, "problems": problems,
            "missing_rate_lines": sum(1 for l in purchase.items if l.status not in DONE and l.gst_rate is None and l.gst_source != "NONE")}


# --------------------------------------------------------------------------- corrections
def correct(db: Session, purchase: Purchase, line: PurchaseItem, changes: dict, *, user: User | None = None) -> PurchaseItem:
    """Apply corrections. Raw values stay; each change records old/new/who/when."""
    _open(purchase)
    _open_line(line)
    corrections = dict(line.corrections or {})
    stamp = {"by": _actor(user), "at": utcnow().isoformat(timespec="seconds")}
    raw = line.raw or {}
    for field, value in changes.items():
        if field in EDITABLE:
            value = purchase_import.as_text(value)
            if value == (raw.get(field) or "") and field in corrections:
                corrections.pop(field)          # back to what the supplier wrote
            elif value != (raw.get(field) or ""):
                corrections[field] = {"value": value, "raw": raw.get(field, ""), **stamp}
            if field in ("category", "dosage_form", "base_unit", "pack_unit", "units_per_pack"):
                _set_new_product_field(line, field, value)
                if field == "units_per_pack":
                    corrections.pop("_units_suggested", None)
    if "item_id" in changes:
        item_id = int(changes["item_id"] or 0) or None
        item = db.get(Item, item_id) if item_id else None
        if item_id and (item is None or item.deleted_at is not None):
            raise PurchaseError("Product not found")
        line.item, line.match_method, line.new_product = item, "MANUAL" if item else "", False
        corrections["_product"] = {"value": item_id, **stamp}
    if changes.get("new_product"):
        line.item, line.match_method, line.new_product = None, "", True
        if _suggest_new_product_units(line):
            corrections["_units_suggested"] = True
        corrections["_product"] = {"value": "NEW", **stamp}
    if "accept" in changes:
        acc = set(corrections.get("_accepted") or [])
        acc.update(c for c in (changes.get("accept") or []) if c in ACCEPTABLE)
        corrections["_accepted"] = sorted(acc)
    line.corrections = corrections
    refresh_line(db, purchase, line)
    _refresh_totals(purchase)
    db.flush()
    return line


def _set_new_product_field(line: PurchaseItem, field: str, value: str) -> None:
    if field == "units_per_pack":
        line.units_per_pack = int(value) if str(value).strip().isdigit() and int(value) > 0 else None
    elif field == "category":
        from app.services import category_service

        line.category = category_service.code_for(value) if value else ""
    else:
        setattr(line, field, (value or "").upper()[:20])


def _suggest_new_product_units(line: PurchaseItem) -> bool:
    """Pre-fill the new product's units from its pack + name.

    Certain readings (a plain strip/count, a content pack, a single) are used
    as they are. An ambiguous pack (20X10S: strip of 10 or box of 200?) gets
    its most likely value pre-filled, and True is returned so the line asks a
    person to confirm it — nothing ambiguous posts unconfirmed."""
    from app.services import packaging_service

    probe = Item(name=line.product_name, pack_size=line.pack_size, dosage_form="", generic_name="",
                 base_unit="UNIT", pack_unit="PACK", units_per_pack=1, loose_sale=False)
    uom = packaging_service.resolve(probe)
    info = units.parse_pack(line.pack_size)
    if uom is not None:
        line.base_unit = line.base_unit or uom.base_unit
        line.pack_unit = line.pack_unit or uom.pack_unit
        line.dosage_form = line.dosage_form or uom.dosage_form
        if info.confident or info.kind == "CONTENT" or not line.pack_size or uom.units_per_pack == 1 and info.units_per_pack == 1:
            line.units_per_pack = line.units_per_pack or uom.units_per_pack
            return False
    line.category = line.category or ""
    if not line.units_per_pack and info.confident and info.units_per_pack:
        line.units_per_pack = info.units_per_pack      # a certain reading (10S, 1S, 1X15)
        return False
    if info.kind == "NESTED":
        # 20X10S / 20X5X10: the strip (innermost count) is what a pharmacy buys and sells
        guess = int(re.findall(r"\d+", line.pack_size or "")[-1])
    else:
        guess = (uom.units_per_pack if uom is not None and uom.units_per_pack > 1 else None) or info.units_per_pack
    if not line.units_per_pack and guess:
        line.units_per_pack = guess
        return True
    if not line.units_per_pack and info.units_per_pack is None and re.fullmatch(r"\s*1\s*[sS]?\s*", line.pack_size or ""):
        line.units_per_pack = 1        # a single: one unit per pack whatever the unit is
    return False


def bulk(db: Session, purchase: Purchase, line_ids: list[int], changes: dict | None = None, *,
         matches: dict[int, int] | None = None, user: User | None = None) -> dict:
    """Apply the same correction to many lines (or confirmed product matches per line).

    Each line is corrected exactly as if done one by one (raw kept, audited per
    line); lines already received are skipped. Returns counts per outcome.
    """
    _open(purchase)
    by_id = {l.id: l for l in purchase.items}
    wanted = [int(i) for i in line_ids] if line_ids else list((matches or {}).keys())
    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise PurchaseError("Some selected lines are not on this purchase", "BAD_LINES")
    done = skipped = 0
    for lid in wanted:
        line = by_id[lid]
        if line.status in DONE:
            skipped += 1
            continue
        if matches is not None:
            correct(db, purchase, line, {"item_id": matches[lid]}, user=user)
        else:
            correct(db, purchase, line, dict(changes or {}), user=user)
        done += 1
    statuses = {}
    for lid in wanted:
        statuses[by_id[lid].status] = statuses.get(by_id[lid].status, 0) + 1
    audit.record(db, action=audit.A_UPDATE, entity_type="purchase", entity_id=purchase.id, user=user,
                 after={"lines": wanted, "changes": changes or {"matches": len(matches or {})}},
                 details=f"Bulk correction on {done} line(s)")
    return {"changed": done, "skipped": skipped, "statuses": statuses}


# --------------------------------------------------------------------------- suggestions (never applied automatically)
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml|iu|%)", re.I)
# words that describe the form or pack, not the product — they must not make two names look alike
_FORM_WORDS = {"tab", "tabs", "tablet", "tablets", "cap", "caps", "capsule", "capsules", "syp", "syrup", "susp",
               "suspension", "inj", "injection", "oint", "ointment", "cream", "gel", "drop", "drops", "sachet",
               "sachets", "strip", "strips", "bottle", "new", "plus", "forte", "ds", "sr", "er", "xr", "mr"}


def _core(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return [w for w in words if w not in _FORM_WORDS and not re.fullmatch(r"\d+(s|x\d+s?)", w)]


def suggestions(db: Session, line: PurchaseItem, limit: int = 6) -> list[dict]:
    """Likely products for a person to choose from — never applied automatically.

    Scored on the brand/generic words only (form words and pack counts removed);
    the leading brand word must be close, and a different strength is heavily
    penalised. Candidates come from indexed prefix look-ups, not the whole
    catalogue, so this stays fast at any size.
    """
    from rapidfuzz import fuzz

    core = _core(line.product_name)
    if not core:
        return []
    brand = core[0]
    probes = {brand[:4]} | ({brand[:3]} if len(brand) >= 3 else set())
    stmt = select(Item.id, Item.name, Item.pack_size).where(
        Item.deleted_at.is_(None), or_(*[func.lower(Item.name).like(f"{p}%") for p in probes],
                                       *[func.lower(Item.name).like(f"% {p}%") for p in probes]))
    pool = db.execute(stmt.limit(2000)).all()
    want = {(float(a), b.lower()) for a, b in _STRENGTH.findall(line.product_name)}
    target = " ".join(core)
    out = []
    for r in pool:
        cand = _core(r.name)
        if not cand:
            continue
        head = fuzz.ratio(brand, cand[0])
        if head < 70:
            continue
        score = 0.6 * fuzz.token_set_ratio(target, " ".join(cand)) + 0.4 * head
        reasons = []
        have = {(float(a), b.lower()) for a, b in _STRENGTH.findall(r.name)}
        nums_want = {float(n) for w in core for n in re.findall(r"\d+(?:\.\d+)?", w)}
        nums_have = {float(n) for w in cand for n in re.findall(r"\d+(?:\.\d+)?", w)}
        if (want and have and not (want & have)) or (nums_want and nums_have and not (nums_want & nums_have)):
            score -= 40
            reasons.append("different strength")
        if line.pack_size and r.pack_size and re.sub(r"\W", "", r.pack_size.upper()) != re.sub(r"\W", "", line.pack_size.upper()):
            score -= 8
            reasons.append(f"pack {r.pack_size}")
        out.append({"item_id": r.id, "name": r.name, "pack": r.pack_size, "score": max(int(score), 0),
                    "note": ", ".join(reasons)})
    out.sort(key=lambda r: (-r["score"], r["name"]))
    return [r for r in out if r["score"] >= 55][:limit]


# --------------------------------------------------------------------------- document view
CHARGE_LABELS = {"bill_discount": "Bill discount %", "printed_gst": "GST printed on invoice", "freight": "Freight", "adjust": "Adjustment",
                 "debit": "Debit note", "credit": "Credit note", "round_off": "Round off"}


def totals(purchase: Purchase, *, received_only: bool = False) -> dict:
    """Invoice arithmetic: taxable value − bill discount + GST + charges − credit ± round off.

    ``received_only`` leaves out lines closed as not delivered (the value actually bought)."""
    lines = [l for l in purchase.items if not (received_only and l.status == CLOSED)]
    taxable = money(sum((l.line_total or 0 for l in lines), Decimal("0")))
    charges = {k: Decimal(str(v)) for k, v in (purchase.charges or {}).items() if _dec(v) is not None}
    bill_pct = charges.get("bill_discount", Decimal("0"))
    bill_disc = money(taxable * bill_pct / 100) if bill_pct else Decimal("0")
    factor = (1 - bill_pct / 100) if bill_pct else Decimal("1")
    gst = money(sum((money((l.line_total or 0) * factor * (l.gst_rate or 0) / 100) for l in lines), Decimal("0")))
    extra = sum((charges.get(k, Decimal("0")) for k in ("freight", "adjust", "debit", "round_off")), Decimal("0")) \
        - charges.get("credit", Decimal("0"))
    total = money(taxable - bill_disc + gst + extra)
    return {"taxable": str(taxable), "bill_discount": str(bill_disc), "gst": str(gst),
            "charges": [{"code": k, "label": CHARGE_LABELS.get(k, k), "value": str(v)} for k, v in charges.items() if k != "printed_gst"],
            "total": str(total)}


def summary(purchase: Purchase) -> dict:
    counts = {s: 0 for s in (READY, CORRECTED, REVIEW, MATCH, INVALID, POSTED, CLOSED)}
    for l in purchase.items:
        counts[l.status] = counts.get(l.status, 0) + 1
    t = totals(purchase)
    calculated = Decimal(t["total"])
    diff = money(purchase.supplier_total - calculated) if purchase.supplier_total is not None else None
    open_lines = [l for l in purchase.items if l.status not in DONE]
    blocking = sum(1 for l in open_lines if l.status not in POSTABLE)
    header_ok = bool(purchase.supplier_id) and bool(purchase.invoice_no)
    return {"rows": len(purchase.items), "counts": counts, "calculated_total": str(calculated), "totals": t,
            "supplier_total": str(purchase.supplier_total) if purchase.supplier_total is not None else None,
            "difference": str(diff) if diff is not None else None, "blocking": blocking,
            "open": len(open_lines), "ready": len(open_lines) - blocking, "difference_ack": bool(purchase.difference_ack),
            # every remaining line can go in at once
            "postable": purchase.status in ("DRAFT", "PARTIAL") and bool(open_lines) and blocking == 0 and header_ok,
            # some ready lines can go in now (select them and post)
            "partly_postable": purchase.status in ("DRAFT", "PARTIAL") and len(open_lines) > blocking and header_ok}


def get(db: Session, purchase_id: int) -> Purchase | None:
    return db.scalar(select(Purchase).where(Purchase.id == purchase_id)
                     .options(selectinload(Purchase.items).selectinload(PurchaseItem.item), selectinload(Purchase.supplier)))


# --------------------------------------------------------------------------- posting
def post(db: Session, purchase: Purchase, *, user: User | None = None, accept_difference: bool = False,
         line_ids: list[int] | None = None) -> Purchase:
    """Receive lines into stock in one transaction — or nothing.

    Without ``line_ids`` every line not yet received is posted. With them only
    those lines go in (partial receipt); the rest stay open for review and the
    purchase is PARTIAL until its last line is received or closed.
    """
    from app.services import category_service, inventory_service

    _open(purchase)
    if not purchase.supplier_id:
        raise PurchaseError("Choose the supplier before posting", "NO_SUPPLIER")
    if not purchase.invoice_no:
        raise PurchaseError("Enter the supplier's invoice number before posting", "NO_INVOICE_NO")
    open_lines = [l for l in purchase.items if l.status not in DONE]
    for line in open_lines:
        refresh_line(db, purchase, line)
    _apply_gst(purchase)
    from app.services import gst as G
    cost_incl = G.policy(db).cost_includes_gst
    if line_ids is None:
        target = open_lines
    else:
        wanted = {int(i) for i in line_ids}
        target = [l for l in purchase.items if l.id in wanted]
        if len(target) != len(wanted):
            raise PurchaseError("Some selected lines are not on this purchase", "BAD_LINES")
        done = [l.line_no for l in target if l.status in DONE]
        if done:
            raise PurchaseError(f"Line(s) {', '.join(map(str, done))} are already received or closed", "LINE_DONE")
    if not target:
        raise PurchaseError("There are no lines to post", "EMPTY")
    not_ready = [l.line_no for l in target if l.status not in POSTABLE]
    if not_ready:
        raise PurchaseError(f"{len(not_ready)} selected line(s) still need review: {', '.join(map(str, not_ready[:12]))}",
                            "LINES_NOT_READY", {"lines": not_ready})
    dup = db.scalar(select(Purchase).where(Purchase.supplier_id == purchase.supplier_id,
                                           Purchase.status.in_(("POSTED", "PARTIAL")),
                                           func.lower(Purchase.invoice_no) == purchase.invoice_no.lower(),
                                           Purchase.id != purchase.id))
    if dup is not None:
        raise PurchaseError(f"Invoice {purchase.invoice_no} from this supplier was already posted as {dup.reference_no}",
                            "DUPLICATE_INVOICE", {"purchase_id": dup.id})
    info = summary(purchase)
    differs = info["difference"] not in (None, "0.00") and abs(Decimal(info["difference"])) > Decimal("0.01")
    if differs and not (accept_difference or purchase.difference_ack):
        raise PurchaseError(f"Supplier total ₹{info['supplier_total']} and calculated total ₹{info['calculated_total']} "
                            f"differ by ₹{info['difference']}. Correct the lines or post with the difference acknowledged.",
                            "TOTAL_DIFFERENCE", info)
    first = purchase.reference_no is None
    reference = purchase.reference_no or f"PUR-{next_number(db, 'purchase_ref'):06d}"
    try:
        with db.begin_nested():
            for line in sorted(target, key=lambda l: l.line_no):
                item = line.item
                if item is None and line.new_product:
                    item = inventory_service.create_item(
                        db, name=line.product_name, category=category_service.ensure(db, line.category, user=user),
                        pack_size=line.pack_size, manufacturer=line.manufacturer, hsn_code=line.hsn_code, mrp=line.mrp,
                        base_unit=line.base_unit or "UNIT", pack_unit=line.pack_unit or "PACK",
                        units_per_pack=line.units_per_pack or 1, dosage_form=line.dosage_form or None, user=user)
                    item.packaging_source = "MANUAL"     # units confirmed by a person during review
                    line.item, line.match_method = item, "NEW_PRODUCT"
                if item is None:
                    raise PurchaseError(f"Line {line.line_no} has no product", "LINES_NOT_READY")
                loose = _loose_receipt_units(line, item)
                batch = inventory_service.add_or_update_batch(
                    db, item, batch_no=line.batch_no, expiry_date=line.expiry_date,
                    quantity=loose if loose is not None else line.quantity, free=0 if loose is not None else line.quantity_free,
                    unit="BASE" if loose is not None else "PACK", movement_type="PURCHASE_RECEIPT",
                    purchase_rate=_cost_per_pack(line, cost_incl),
                    mrp=line.mrp, supplier_id=purchase.supplier_id, purchase_id=purchase.id, reference_type="PURCHASE",
                    reference_id=purchase.id, reference_no=reference,
                    reason=f"Purchase {reference} · {purchase.supplier.name if purchase.supplier else ''} inv {purchase.invoice_no}",
                    user=user)
                batch.rate_basis = "INCL_GST" if cost_incl else "EXCL_GST"
                line.batch_id, line.status = batch.id, POSTED
                _remember_mapping(db, purchase, line, user)
            if differs and accept_difference:
                purchase.difference_ack = True
            purchase.reference_no = reference
            complete = all(l.status in DONE for l in purchase.items)
            purchase.status = "POSTED" if complete else "PARTIAL"
            if first:
                purchase.posted_at, purchase.posted_by = utcnow(), user.id if user else None
                purchase.purchase_date = purchase.posted_at
            _refresh_totals(purchase)
            db.flush()
    except stock_ledger.BatchConflict as exc:
        raise PurchaseError(str(exc), "BATCH_CONFLICT")
    except stock_ledger.StockError as exc:
        raise PurchaseError(str(exc), "STOCK_ERROR")
    remaining = sum(1 for l in purchase.items if l.status not in DONE)
    audit.record(db, action=audit.A_CREATE, entity_type="purchase", entity_id=reference, user=user,
                 after={"lines": [l.line_no for l in target], "remaining": remaining, "total": str(purchase.total),
                        "supplier_total": info["supplier_total"], "difference": info["difference"]},
                 details=f"Purchase {reference}: {len(target)} line(s) received"
                         + (f", {remaining} still open" if remaining else f" — complete, ₹{purchase.total}")
                         + (f", difference ₹{info['difference']} acknowledged" if accept_difference and differs else ""))
    return purchase


def close_remaining(db: Session, purchase: Purchase, *, reason: str, user: User | None = None) -> Purchase:
    """Finish a partly received purchase: the lines still open were not delivered."""
    if purchase.status != "PARTIAL":
        raise PurchaseError("Only a partly received purchase can close its remaining lines", "NOT_PARTIAL")
    if not (reason or "").strip():
        raise PurchaseError("A reason is required (e.g. short supplied, will come on a new invoice)")
    stamp = {"by": _actor(user), "at": utcnow().isoformat(timespec="seconds"), "reason": reason.strip()[:300]}
    closed = []
    for line in purchase.items:
        if line.status not in DONE:
            line.corrections = {**(line.corrections or {}), "_closed": stamp}
            line.status = CLOSED
            closed.append(line.line_no)
    purchase.status = "POSTED"
    _refresh_totals(purchase)
    db.flush()
    audit.record(db, action=audit.A_UPDATE, entity_type="purchase", entity_id=purchase.reference_no, user=user,
                 after={"closed_lines": closed, "reason": reason},
                 details=f"Purchase {purchase.reference_no}: {len(closed)} line(s) closed as not received — {reason.strip()}")
    return purchase


def _remember_mapping(db: Session, purchase: Purchase, line: PurchaseItem, user: User | None) -> None:
    """A human-confirmed line teaches the supplier mapping for next time."""
    if not purchase.supplier_id or line.item is None or line.match_method not in ("MANUAL", "NEW_PRODUCT", "SUPPLIER_MAP"):
        return
    code = line.supplier_code or ""
    key = "" if code else description_key(line.description_raw or line.product_name)
    m = db.scalar(select(SupplierProductMap).where(SupplierProductMap.supplier_id == purchase.supplier_id,
                                                   SupplierProductMap.supplier_code == code,
                                                   SupplierProductMap.description_key == key))
    if m is None:
        db.add(SupplierProductMap(supplier_id=purchase.supplier_id, supplier_code=code, description_key=key,
                                  description_raw=(line.description_raw or line.product_name)[:250], item_id=line.item.id,
                                  confirmed_by=user.id if user else None, uses=1))
    else:
        m.uses = (m.uses or 0) + 1
        if m.item_id != line.item.id and line.match_method == "MANUAL":
            m.item_id, m.confirmed_by, m.confirmed_at = line.item.id, user.id if user else None, utcnow()


def cancel(db: Session, purchase: Purchase, *, reason: str, user: User | None = None) -> Purchase:
    _draft(purchase)
    if not (reason or "").strip():
        raise PurchaseError("A reason is required to cancel a draft")
    purchase.status, purchase.cancelled_at = "CANCELLED", utcnow()
    purchase.cancelled_by, purchase.cancel_reason = user.id if user else None, reason.strip()[:500]
    audit.record(db, action=audit.A_UPDATE, entity_type="purchase", entity_id=purchase.id, user=user,
                 after={"status": "CANCELLED", "reason": reason}, details="Purchase draft cancelled")
    return purchase


def update_header(db: Session, purchase: Purchase, data: dict, *, user: User | None = None) -> Purchase:
    _draft(purchase)
    if "supplier_id" in data:
        sid = int(data["supplier_id"] or 0) or None
        if sid and db.get(Supplier, sid) is None:
            raise PurchaseError("Supplier not found")
        purchase.supplier_id = sid
    if "invoice_no" in data:
        purchase.invoice_no = str(data["invoice_no"] or "").strip()[:60]
    if "invoice_date" in data:
        purchase.invoice_date = parse_date(data["invoice_date"])
        if data["invoice_date"] and purchase.invoice_date is None:
            raise PurchaseError("Invoice date must be like 30/09/2026")
        if purchase.invoice_date and purchase.invoice_date > date.today():
            raise PurchaseError("Invoice date cannot be in the future")
    if "supplier_total" in data:
        d = _dec(data["supplier_total"])
        purchase.supplier_total = money(d) if d is not None else None
    if "notes" in data:
        purchase.notes = str(data["notes"] or "")[:2000]
    if isinstance(data.get("charges"), dict):
        charges = dict(purchase.charges or {})
        for k, v in data["charges"].items():
            if k not in CHARGE_LABELS:
                continue
            d = _dec(v)
            if d is None or d == 0:
                charges.pop(k, None)
            elif k == "bill_discount" and not (0 <= d <= 100):
                raise PurchaseError("Bill discount must be a percentage between 0 and 100")
            else:
                charges[k] = str(money(d))
        purchase.charges = charges or None
        _refresh_totals(purchase)
    for line in purchase.items:          # supplier mapping and expiry-vs-invoice depend on the header
        refresh_line(db, purchase, line)
    db.flush()
    return purchase


# --------------------------------------------------------------------------- suppliers
def save_supplier(db: Session, data: dict, *, supplier: Supplier | None = None, user: User | None = None) -> Supplier:
    name = " ".join(str(data.get("name") or (supplier.name if supplier else "")).split())[:150]
    if not name:
        raise PurchaseError("Supplier name is required")
    clash = db.scalar(select(Supplier).where(func.lower(Supplier.name) == name.lower(),
                                             Supplier.id != (supplier.id if supplier else 0)))
    if clash is not None:
        raise PurchaseError(f"A supplier named {name} already exists")
    new = supplier is None
    if new:
        supplier = Supplier(name=name)
        db.add(supplier)
        db.flush()
        supplier.code = (str(data.get("code") or "").strip().upper()[:20]) or f"SUP{supplier.id:04d}"
    before = None if new else {c: getattr(supplier, c) for c in ("name", "gst_number", "phone", "credit_days", "is_active")}
    supplier.name = name
    for f, n in (("contact", 120), ("phone", 40), ("email", 120), ("gst_number", 30), ("address", 2000), ("payment_terms", 60)):
        if f in data:
            setattr(supplier, f, str(data.get(f) or "").strip()[:n])
    if data.get("code") and not new:
        supplier.code = str(data["code"]).strip().upper()[:20]
    if "credit_days" in data:
        try:
            supplier.credit_days = max(int(str(data["credit_days"] or 0)), 0)
        except ValueError:
            raise PurchaseError("Credit days must be a whole number")
    if "active" in data:
        supplier.is_active = bool(data["active"])
    if supplier.gst_number and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]", supplier.gst_number.upper()):
        raise PurchaseError("GSTIN must look like 29ABCDE1234F1Z5")
    supplier.gst_number = supplier.gst_number.upper()
    db.flush()
    audit.record(db, action=audit.A_CREATE if new else audit.A_UPDATE, entity_type="supplier", entity_id=supplier.code,
                 user=user, before=before, after={"name": supplier.name}, details=f"Supplier {supplier.name} saved")
    return supplier
