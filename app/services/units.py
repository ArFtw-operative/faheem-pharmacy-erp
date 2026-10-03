"""Units, packaging and pricing rules — the one conversion engine.

Every module (purchase, POS, returns, adjustments, opening stock, imports)
converts through this file; nothing else multiplies by a pack size.

Model
-----
* Stock is always counted in the product's **base unit** (tablet, capsule,
  bottle, tube, piece …). Fractional stock never exists.
* ``units_per_pack`` is how many base units one **purchase pack** holds
  (a strip of 15 tablets → 15; a bottle bought and sold as a bottle → 1).
* ``loose_sale`` says whether the pack may be broken at the counter. When it
  is off and ``units_per_pack > 1``, POS quantities must be whole packs.
* Content (200 mL, 30 g) is product metadata, never stock.
* A batch snapshots ``units_per_pack`` when it is first received, so history
  is never reinterpreted with a later packaging definition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
BASE_UNITS = ("UNIT", "TABLET", "CAPSULE", "BOTTLE", "TUBE", "PIECE", "PACK", "STRIP",
              "VIAL", "AMPOULE", "SACHET", "JAR", "BOX", "KIT", "PAIR")
PACK_UNITS = ("PACK", "STRIP", "BOX", "BOTTLE", "TUBE", "PIECE", "VIAL", "SACHET", "KIT", "JAR", "UNIT")
DOSAGE_FORMS = ("", "TABLET", "CAPSULE", "SYRUP", "SUSPENSION", "DROPS", "INJECTION", "CREAM",
                "OINTMENT", "GEL", "LOTION", "POWDER", "SACHET", "INHALER", "SPRAY", "SOAP",
                "DEVICE", "KIT", "SUPPOSITORY", "OTHER")

_PLURAL = {"BOX": "boxes", "PIECE": "pieces", "PAIR": "pairs"}


def unit_label(unit: str, qty: int | None = None) -> str:
    """``TABLET`` → ``tablet`` / ``tablets``."""
    word = (unit or "unit").strip().upper() or "UNIT"
    if qty is not None and qty == 1:
        return word.lower()
    return _PLURAL.get(word, word.lower() + "s")


def normalize_unit(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = re.sub(r"[^A-Z]", "", str(value or "").upper())
    aliases = {"TAB": "TABLET", "TABS": "TABLET", "TABLETS": "TABLET", "CAP": "CAPSULE",
               "CAPS": "CAPSULE", "CAPSULES": "CAPSULE", "BTL": "BOTTLE", "BOTTLES": "BOTTLE",
               "PCS": "PIECE", "PC": "PIECE", "PIECES": "PIECE", "NOS": "PIECE", "STRIPS": "STRIP",
               "STP": "STRIP", "PKT": "PACK", "PACKET": "PACK", "PACKS": "PACK", "UNITS": "UNIT",
               "TUBES": "TUBE", "VIALS": "VIAL", "AMP": "AMPOULE", "BOXES": "BOX"}
    text = aliases.get(text, text)
    return text if text in allowed else default


def parse_bool(value: Any) -> bool | None:
    text = str(value if value is not None else "").strip().lower()
    if text in ("y", "yes", "true", "1", "loose", "on"):
        return True
    if text in ("n", "no", "false", "0", "off"):
        return False
    return None


# ---------------------------------------------------------------------------
# Pack parser
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PackInfo:
    """Structured reading of a raw pack description. ``raw`` is never lost."""

    raw: str
    kind: str = "UNKNOWN"          # COUNT | NESTED | CONTENT | KIT | SINGLE | UNKNOWN
    units_per_pack: int | None = None   # base units in the smallest purchasable pack
    outer_count: int | None = None      # e.g. 2 in 2X10S (packs per outer box)
    content_qty: Decimal | None = None  # e.g. 200 in 200ML
    content_unit: str = ""              # ML / G / ...
    confident: bool = False             # safe to use without human review
    strip: bool = False                 # written as a strip count: 10S, 15 S, 10'S
    unit_hint: str = ""                 # a unit named in the pack text: VIAL, PIECE, KIT


_CONTENT_UNITS = {"ML": "ML", "MLS": "ML", "L": "L", "LTR": "L", "G": "G", "GM": "G", "GMS": "G",
                  "GRM": "G", "GRMS": "G", "GRAM": "G", "GRAMS": "G", "KG": "KG", "MG": "MG", "MCG": "MCG"}


def parse_pack(raw: Any) -> PackInfo:
    """Interpret a pack description such as ``15S``, ``2X10S``, ``200ML``, ``KIT``.

    Only the shape is decided here. Whether an invoice quantity counts boxes or
    strips is a transaction question the parser never answers, so a nested
    pack (``2X10S``) reports the inner strip (10) with ``outer_count`` 2 and
    is *not* confident.
    """
    text = str(raw or "").strip()
    # Reference catalogues describe the retail pack in words; preserve the original.
    nested = re.fullmatch(r"box\s+of\s+(\d+)\s+(?:strips?|blisters?)\s+of\s+(\d+)\s+(tablets?|capsules?)", text, re.I)
    if nested and int(nested.group(1)) > 0 and int(nested.group(2)) > 0:
        return PackInfo(raw=text, kind="NESTED", units_per_pack=int(nested.group(2)), outer_count=int(nested.group(1)),
                        confident=False, strip=True, unit_hint="TABLET" if nested.group(3).lower().startswith("tab") else "CAPSULE")
    label = re.fullmatch(r"(?:strip|blister|pack)\s+of\s+(\d+)\s+(tablets?|capsules?)(?:\s+[a-z ]+)?", text, re.I)
    if label:
        count = int(label.group(1))
        hint = "TABLET" if label.group(2).lower().startswith("tab") else "CAPSULE"
        return PackInfo(raw=text, kind="COUNT", units_per_pack=count if count > 0 else None,
                        confident=count > 0, strip=True, unit_hint=hint)
    label = re.fullmatch(r"(bottle|tube|vial|ampoule|sachet|jar|packet)\s+of\s+(\d+(?:\.\d+)?)\s*(ml|gm|g|kg|l)\b.*", text, re.I)
    if label:
        container = {"PACKET": "PACK"}.get(label.group(1).upper(), label.group(1).upper())
        return PackInfo(raw=text, kind="CONTENT", units_per_pack=1, confident=True, unit_hint=container,
                        content_qty=Decimal(label.group(2)), content_unit=_CONTENT_UNITS[label.group(3).upper()])
    key = re.sub(r"\s+", "", text.upper()).replace("'", "").replace("’", "")
    key = key.replace("×", "X").replace("*", "X").rstrip(".")
    if not key:
        return PackInfo(raw=text)
    # typed/OCR look-alikes in a strip count: "lOS" → 10S, "I5S" → 15S (the raw text is kept)
    if re.fullmatch(r"[0-9LIO]{2,3}S", key) and re.search(r"[LIO]", key[:-1]) and key[0] != "O":
        key = key[:-1].translate(str.maketrans("LIO", "110")) + "S"
    if key in ("KIT", "1KIT"):
        return PackInfo(raw=text, kind="KIT", units_per_pack=1, confident=True, unit_hint="KIT")
    named = {"VIAL": "VIAL", "VAIL": "VIAL", "1VIAL": "VIAL", "AMP": "AMPOULE", "AMPOULE": "AMPOULE",
             "PCS": "PIECE", "PC": "PIECE", "1PCS": "PIECE", "1PC": "PIECE", "PIECE": "PIECE", "1N": "PIECE",
             "EACH": "PIECE", "EA": "PIECE", "NOS": "PIECE", "1NOS": "PIECE", "UNIT": "PIECE",
             "PAIR": "PAIR", "1PAIR": "PAIR", "TUBE": "TUBE", "BOTTLE": "BOTTLE", "BTL": "BOTTLE", "JAR": "JAR",
             "SACHET": "SACHET", "BOX": "BOX", "STRIP": "STRIP"}
    if key in named:
        return PackInfo(raw=text, kind="SINGLE", units_per_pack=1, confident=True, unit_hint=named[key])
    # 5ML, 200ML, 30GM, 5X5ML (5 ampoules of 5 mL)
    if m := re.fullmatch(r"(?:(\d+)X)?(\d+(?:\.\d+)?)(ML|MLS|L|LTR|G|GM|GMS|GRM|GRMS|GRAM|GRAMS|KG|MG|MCG)", key):
        outer = int(m.group(1)) if m.group(1) else None
        return PackInfo(raw=text, kind="CONTENT", units_per_pack=outer or 1, outer_count=outer,
                        content_qty=Decimal(m.group(2)), content_unit=_CONTENT_UNITS[m.group(3)],
                        confident=outer in (None, 1))
    # 1, 1PCS, 1N, 1NOS
    if m := re.fullmatch(r"(\d+)(S|PCS|PC|N|NOS|T|C|TAB|TABS|CAP|CAPS)?", key):
        n = int(m.group(1))
        if n <= 0:
            return PackInfo(raw=text)
        suffix = m.group(2) or ""
        return PackInfo(raw=text, kind="SINGLE" if n == 1 else "COUNT", units_per_pack=n, confident=True,
                        strip=suffix in ("S", "T", "C", "TAB", "TABS", "CAP", "CAPS"),
                        unit_hint={"PCS": "PIECE", "PC": "PIECE", "N": "PIECE", "NOS": "PIECE",
                                   "T": "TABLET", "C": "CAPSULE", "TAB": "TABLET", "TABS": "TABLET", "CAP": "CAPSULE", "CAPS": "CAPSULE"}.get(suffix, ""))
    # AxB, AxBxC with an optional trailing S / X
    if m := re.fullmatch(r"(\d+)X(\d+)(?:X(\d+))?(?:S|X|PCS)?", key):
        parts = [int(p) for p in m.groups() if p]
        if any(p <= 0 for p in parts):
            return PackInfo(raw=text)
        strip = key.endswith("S")
        if len(parts) == 2 and parts[0] == 1:
            # 1X10 / 1X3: one pack holding N — the pack is the purchasable unit
            return PackInfo(raw=text, kind="COUNT", units_per_pack=parts[1], outer_count=1, confident=True, strip=strip)
        inner, outer = parts[-1], parts[0]
        if len(parts) == 3:
            # Keep the retail count separate from intermediate cartons:
            # 20 × 5 × 10 contains 100 inner packs of ten, not twenty of fifty.
            inner, outer = (parts[1], parts[0]) if parts[2] == 1 else (parts[2], parts[0] * parts[1])
        return PackInfo(raw=text, kind="NESTED", units_per_pack=inner, outer_count=outer, confident=False, strip=strip)
    return PackInfo(raw=text)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
class UnitError(ValueError):
    pass


def to_base(quantity: int, units_per_pack: int, unit: str = "BASE") -> int:
    """Quantity in base units. ``unit`` is ``BASE`` or ``PACK``."""
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise UnitError("Quantity must be a whole number")
    upp = int(units_per_pack or 1)
    if upp < 1:
        raise UnitError("Pack size must be at least 1")
    unit = (unit or "BASE").upper()
    if unit == "BASE":
        return quantity
    if unit == "PACK":
        return quantity * upp
    raise UnitError(f"Unknown transaction unit {unit!r}")


def base_quantity(packs: Any, units_per_pack: Any) -> Decimal:
    """Exact base units in a (possibly fractional) number of packs: 2.5 strips of 15 → 37.5.

    Callers decide whether a fraction is allowed; stock postings use :func:`to_base`."""
    return _dec(packs) * max(int(units_per_pack or 1), 1)


def split_packs(base_qty: int, units_per_pack: int) -> tuple[int, int]:
    """``666, 15`` → ``(44, 6)``: whole packs and the loose remainder."""
    upp = max(int(units_per_pack or 1), 1)
    sign = -1 if base_qty < 0 else 1
    packs, loose = divmod(abs(int(base_qty)), upp)
    return sign * packs, sign * loose


def describe_stock(base_qty: int, units_per_pack: int, base_unit: str, pack_unit: str) -> str:
    """Human equivalent, derived and never stored: ``44 strips + 6 tablets``."""
    upp = max(int(units_per_pack or 1), 1)
    if upp == 1:
        return f"{base_qty} {unit_label(base_unit, base_qty)}"
    packs, loose = split_packs(base_qty, upp)
    parts = []
    if packs:
        parts.append(f"{packs} {unit_label(pack_unit, packs)}")
    if loose or not packs:
        parts.append(f"{loose} {unit_label(base_unit, loose)}")
    return " + ".join(parts)


_EXPR = re.compile(r"^\s*(?:(\d+)\s*[sSpP])?\s*(?:\+?\s*(\d+))?\s*$")


def parse_qty_expression(text: Any, units_per_pack: int) -> int:
    """Cashier shorthand: ``3`` → 3, ``1s`` → one pack, ``2s+3`` → 2 packs + 3 units."""
    raw = str(text if text is not None else "").strip()
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    m = _EXPR.match(raw)
    if not raw or not m or (m.group(1) is None and m.group(2) is None):
        raise UnitError(f"Quantity {raw!r} is not understood (use 3, 1s or 2s+3)")
    packs = int(m.group(1) or 0)
    return packs * max(int(units_per_pack or 1), 1) + int(m.group(2) or 0)


def check_sale_quantity(qty: int, *, units_per_pack: int, loose_sale: bool, pack_unit: str,
                        base_unit: str, name: str) -> None:
    """A non-loose product with a multi-unit pack sells whole packs only."""
    upp = max(int(units_per_pack or 1), 1)
    if qty <= 0:
        raise UnitError(f"Invalid quantity for {name}")
    if upp > 1 and not loose_sale and qty % upp:
        raise UnitError(
            f"{name} is sold as a full {unit_label(pack_unit, 1)} of {upp} {unit_label(base_unit, upp)}. "
            f"Loose quantity is not allowed."
        )


# ---------------------------------------------------------------------------
# Pricing (MRP-based retail; no GST is added at the counter)
# ---------------------------------------------------------------------------
CENT = Decimal("0.01")
UNIT_PLACES = Decimal("0.0001")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except Exception:
        return Decimal("0")


def unit_price(pack_price: Any, units_per_pack: int) -> Decimal:
    """Per-base-unit price kept to 4 places (₹32.10 / 15 → 2.1400)."""
    upp = max(int(units_per_pack or 1), 1)
    return (_dec(pack_price) / upp).quantize(UNIT_PLACES, rounding=ROUND_HALF_UP)


def display_unit_price(pack_price: Any, units_per_pack: int) -> Decimal:
    """Unit MRP as printed on the counter (2 places)."""
    return unit_price(pack_price, units_per_pack).quantize(CENT, rounding=ROUND_HALF_UP)


def unit_cost(pack_rate: Any, units_per_pack: Any) -> Decimal:
    """Cost of one base unit to 6 places (stock valuation keeps more precision than MRP)."""
    if int(units_per_pack or 0) < 1:
        raise ValueError("Purchase UOM conversion must be positive")
    rate = Decimal(str(pack_rate))
    if not rate.is_finite() or rate < 0:
        raise ValueError("Purchase rate must be a finite nonnegative amount")
    return (rate / Decimal(int(units_per_pack))).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def line_amount(pack_price: Any, units_per_pack: int, qty: int) -> Decimal:
    """MRP charged for ``qty`` base units.

    Priced proportionally from the pack MRP and rounded once, so a full strip
    always costs exactly its printed MRP (15 × 32.10/15 = 32.10) and loose
    tablets are never over-charged by per-unit rounding.
    """
    upp = max(int(units_per_pack or 1), 1)
    return (_dec(pack_price) * qty / upp).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Expiry policy
# ---------------------------------------------------------------------------
def is_expired(expiry: date | None, today: date | None = None) -> bool:
    """Printed expiry is a month (``02/2030``): stock is sellable through that
    whole month and expired from the 1st of the next."""
    if expiry is None:
        return False
    today = today or date.today()
    return (expiry.year, expiry.month) < (today.year, today.month)
