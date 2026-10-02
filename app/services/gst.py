"""GST on supplier purchases — one engine for review, posting, stock cost and reports.

The invoice arithmetic (same for every supplier and file layout):

    taxable (line)   = qty × rate − item discount − scheme discount
    taxable (net)    = taxable (line) × (1 − bill discount %)          bill discount is before tax
    GST (line)       = taxable (net) × GST %                           rounded per line, to the paisa
                       intra-state → CGST + SGST (half each) · inter-state → IGST
    landed (line)    = taxable (net) + GST
    rate incl. GST   = landed ÷ packs received (paid + free, scheme splits included)

Sales never add GST: MRP already includes it. Stock received from purchases is
costed at the rate *including* GST (``purchase_cost_includes_gst``, default on):
GST paid to the supplier is part of what the medicine cost the shop. A shop that
claims input-tax credit can switch it off (``manage.py setting set
purchase_cost_includes_gst false``). Batches record which basis their rate uses,
and nothing already received is ever re-costed.

Rate slabs and the date they changed are settings, not code:

    gst_rates              valid slabs now            default "0,0.25,3,5,18,40"
    gst_rates_before       slabs before the change    default "0,0.25,3,5,12,18,28"
    gst_rates_changed_on   the change date            default "2025-09-22" (GST rate rationalisation)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import money, to_decimal

_GSTIN = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]$")
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
    "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra & Nagar Haveli and Daman & Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman & Nicobar", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other territory",
}


# --------------------------------------------------------------------------- GSTIN
def gstin_problem(gstin: str) -> str:
    """Why a GSTIN is not valid ("" when it is): format, state code and check digit."""
    g = (gstin or "").strip().upper()
    if not g:
        return "missing"
    if not _GSTIN.match(g):
        return f"“{g}” is not in GSTIN format (15 characters: 2-digit state code, PAN, entity number, Z, check digit)"
    if g[:2] not in STATES:
        return f"“{g}” starts with {g[:2]}, which is not an Indian state code"
    total = 0
    for i, ch in enumerate(g[:14]):
        product = _B36.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    check = _B36[(36 - total % 36) % 36]
    if g[14] != check:
        return f"“{g}” fails the GSTIN check digit (last character should be {check}) — a character was mistyped"
    return ""


def state_of(gstin: str) -> str:
    g = (gstin or "").strip()
    return g[:2] if len(g) >= 2 and g[:2].isdigit() else ""


def supply_type(our_gstin: str, supplier_gstin: str, *, our_state: str = "") -> tuple[str, str]:
    """("INTRA" | "INTER", reason). Unknown states are treated as intra-state, and say so."""
    ours, theirs = state_of(our_gstin) or (our_state if our_state in STATES else ""), state_of(supplier_gstin)
    if ours and theirs:
        if ours == theirs:
            return "INTRA", f"supplier and pharmacy both in {STATES.get(ours, ours)} → CGST + SGST"
        return "INTER", f"supplier in {STATES.get(theirs, theirs)}, pharmacy in {STATES.get(ours, ours)} → IGST"
    missing = " and ".join(x for x, v in (("the pharmacy's state", ours), ("the supplier's GSTIN", theirs)) if not v)
    return "INTRA", f"assumed intra-state (CGST + SGST) because {missing} is not set"


# --------------------------------------------------------------------------- slabs
def _rates(text: str) -> set[Decimal]:
    out = set()
    for part in (text or "").split(","):
        try:
            out.add(Decimal(part.strip()).normalize())
        except Exception:
            continue
    return out


@dataclass
class Policy:
    rates: set = field(default_factory=set)
    rates_before: set = field(default_factory=set)
    changed_on: date | None = None
    cost_includes_gst: bool = True
    our_gstin: str = ""
    our_state: str = ""

    def valid_for(self, when: date | None) -> set[Decimal]:
        if self.changed_on and when and when < self.changed_on:
            return self.rates_before
        return self.rates


def policy(db: Session) -> Policy:
    from app.services.settings_service import get_setting

    try:
        changed = date.fromisoformat(get_setting(db, "gst_rates_changed_on", "2025-09-22") or "")
    except ValueError:
        changed = None
    return Policy(
        rates=_rates(get_setting(db, "gst_rates", "0,0.25,3,5,18,40")),
        rates_before=_rates(get_setting(db, "gst_rates_before", "0,0.25,3,5,12,18,28")),
        changed_on=changed,
        cost_includes_gst=(get_setting(db, "purchase_cost_includes_gst", "true") or "true").lower() in ("1", "true", "yes", "on"),
        our_gstin=(get_setting(db, "gst_number", "") or "").strip().upper(),
        our_state=(get_setting(db, "pharmacy_state_code", "") or "").strip(),
    )


def fmt_rate(r: Decimal | None) -> str:
    if r is None:
        return "—"
    r = Decimal(r).normalize()
    return f"{int(r)}%" if r % 1 == 0 else f"{format(r, 'f')}%"


def nearest_slab(rate: Decimal, slabs: set[Decimal], tolerance: Decimal = Decimal("0.2")) -> Decimal | None:
    best = min(slabs, key=lambda s: abs(s - rate), default=None)
    return best if best is not None and abs(best - rate) <= tolerance else None


# --------------------------------------------------------------------------- invoice breakdown
def bill_discount_factor(purchase) -> Decimal:
    pct = to_decimal((purchase.charges or {}).get("bill_discount") or 0)
    return Decimal(1) - pct / 100 if 0 < pct < 100 else Decimal(1)


def split(gst: Decimal, mode: str) -> tuple[Decimal, Decimal, Decimal]:
    """(CGST, SGST, IGST). Halves are rounded so CGST + SGST is exactly the GST."""
    if mode == "INTER":
        return Decimal("0"), Decimal("0"), gst
    cgst = money(gst / 2)
    return cgst, gst - cgst, Decimal("0")


def line_breakdown(line, factor: Decimal, mode: str, received: Decimal | None = None) -> dict:
    """GST and landed cost of one purchase line (values as the invoice arithmetic prints them)."""
    taxable = money(to_decimal(line.line_total) * factor)
    rate = to_decimal(line.gst_rate) if line.gst_rate is not None else Decimal("0")
    gst = money(taxable * rate / 100)
    cgst, sgst, igst = split(gst, mode)
    landed = taxable + gst
    packs = received if received is not None else Decimal((line.quantity or 0) + (line.quantity_free or 0))
    return {"taxable": taxable, "rate": rate if line.gst_rate is not None else None, "gst": gst, "cgst": cgst, "sgst": sgst,
            "igst": igst, "landed": landed, "packs": packs,
            "rate_ex": money(taxable / packs) if packs else Decimal("0"),
            "rate_incl": money(landed / packs) if packs else Decimal("0")}


def by_rate(rows: list[dict]) -> list[dict]:
    """GST summary per slab (the table printed at the bottom of every GST invoice)."""
    acc: dict = {}
    for r in rows:
        k = r["rate"] if r["rate"] is not None else Decimal("-1")
        a = acc.setdefault(k, {"rate": r["rate"], "taxable": Decimal(0), "cgst": Decimal(0), "sgst": Decimal(0), "igst": Decimal(0), "gst": Decimal(0), "lines": 0})
        for f in ("taxable", "cgst", "sgst", "igst", "gst"):
            a[f] += r[f]
        a["lines"] += 1
    return [acc[k] for k in sorted(acc)]


def last_known_rate(db: Session, item_id: int | None, before_purchase_id: int | None = None):
    """The GST % this product carried on its latest posted purchase: (rate, reference, date) or None."""
    if not item_id:
        return None
    from app.models import Purchase, PurchaseItem

    q = (select(PurchaseItem.gst_rate, Purchase.reference_no, Purchase.invoice_date)
         .join(Purchase, Purchase.id == PurchaseItem.purchase_id)
         .where(PurchaseItem.item_id == item_id, PurchaseItem.status == "POSTED", PurchaseItem.gst_rate.is_not(None)))
    if before_purchase_id:
        q = q.where(Purchase.id != before_purchase_id)
    row = db.execute(q.order_by(Purchase.id.desc(), PurchaseItem.id.desc()).limit(1)).first()
    return (to_decimal(row[0]).normalize(), row[1] or "", row[2]) if row else None
