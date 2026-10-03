"""PackagingConversionService: the one place that turns quantities into the words people read.

Arithmetic stays in :mod:`app.services.units` (base units, packs, prices); this module
answers the display questions every screen asks, so no page converts on its own:

* **Received stock** — the invoice quantity, billed + free, in the unit the supplier
  billed. It is a fact of the invoice and is known whenever the billed quantity is,
  even before anyone knows what one pack contains.
* **Stock equivalent** — what that receipt becomes in stock (strips, tablets) and what
  the containers hold (2 × 60 mL). Only this part depends on the pack conversion.

Package interpretation and received quantity are separate facts: billed 2 + free 1 is
3 purchase packs received whether or not the pack contents are known.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.services import units

UNRESOLVED = "Pack conversion unresolved"
PURCHASE_PACK = "purchase pack"
# count units: an equivalent in these is a number of separate things, not content
_COUNTED = {"TABLET", "CAPSULE", "PIECE", "SACHET", "VIAL", "AMPOULE", "STRIP", "UNIT", "PAIR"}
_CONTENT_LABEL = {"ML": "mL", "L": "L", "G": "g", "GRAM": "g", "KG": "kg", "MG": "mg", "MCG": "mcg", "DOSE": "doses"}


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def number(value: Any) -> str:
    """``3`` → "3", ``2.50`` → "2.5", ``1000`` → "1,000" (never scientific notation)."""
    d = _dec(value)
    if d is None:
        return ""
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return f"{d.normalize():,f}"


def label(unit: str, qty: Any = None) -> str:
    d = _dec(qty)
    if unit == PURCHASE_PACK:
        return PURCHASE_PACK if d == 1 else PURCHASE_PACK + "s"
    return units.unit_label(unit, 1 if d == 1 else None)


def content_of(pack_text: str, item=None) -> tuple[Decimal, str] | None:
    """What one container holds (60 mL, 20 g): the product master first, else the printed pack."""
    if item is not None and getattr(item, "content_qty", None) and getattr(item, "content_unit", ""):
        return Decimal(item.content_qty), item.content_unit.upper()
    info = units.parse_pack(pack_text)
    if info.kind == "CONTENT" and info.content_qty and not (info.outer_count and info.outer_count > 1):
        return info.content_qty, info.content_unit
    return None


def content_text(count: Any, content: tuple[Decimal, str] | None) -> str:
    if not content:
        return ""
    qty, unit = content
    return f"{number(count)} × {number(qty)} {_CONTENT_LABEL.get(unit, unit.lower())}"


def stock_equivalent(base_qty: int, *, units_per_pack: int, base_unit: str, pack_unit: str,
                     units_per_invoice_unit: Any = None, content: tuple[Decimal, str] | None = None) -> str:
    """Every level below the received unit: ``20 strips, 200 capsules`` · ``2 × 60 mL`` · ``3 pieces``."""
    upp = max(int(units_per_pack or 1), 1)
    factor = _dec(units_per_invoice_unit)
    parts: list[str] = []
    if upp > 1 and base_qty % upp == 0 and factor is not None and factor > upp:
        packs = base_qty // upp           # billed in outer packs: show the retail packs inside them
        parts.append(f"{number(packs)} {label(pack_unit, packs)}")
    if upp > 1 and base_qty % upp and not (factor is not None and factor == 1):
        parts.append(units.describe_stock(base_qty, upp, base_unit, pack_unit))   # 4 strips + 5 tablets
    elif base_unit in _COUNTED or not content:
        parts.append(f"{number(base_qty)} {label(base_unit, base_qty)}")
    if content:
        parts.append(content_text(base_qty, content))
    return ", ".join(dict.fromkeys(p for p in parts if p))


def received(paid: Any, free: Any) -> Decimal | None:
    """Billed + free in invoice units. None only when there is no usable billed quantity."""
    p, f = _dec(paid), _dec(free)
    if p is None and f is None:
        return None
    return (p or Decimal(0)) + (f or Decimal(0))


def invoice_unit(decision: dict, definition: dict) -> str:
    """The unit one invoice Qty counts, once the conversion is known."""
    factor = _dec(decision.get("units_per_invoice_unit"))
    upp = max(int(definition.get("units_per_pack") or 1), 1)
    if factor is None:
        return PURCHASE_PACK
    if factor == upp:
        return definition.get("pack_unit") or "PACK"
    if factor == 1:
        return definition.get("base_unit") or "UNIT"
    if factor > upp and factor % upp == 0:
        return "BOX"
    return PURCHASE_PACK


def receipt_view(decision: dict | None, definition: dict, *, pack_text: str = "", item=None,
                 paid: Any = None, free: Any = None) -> dict:
    """Received stock and stock equivalent of one purchase line, ready to show.

    ``decision`` is the line's receipt decision; ``paid``/``free`` are used when the
    decision has not been computed (the quantity is still known from the invoice).
    """
    d = decision or {}
    qty = received(d.get("paid", paid), d.get("free", free))
    resolved = bool(d.get("resolved"))
    unit = invoice_unit(d, definition) if resolved else PURCHASE_PACK
    content = content_of(pack_text, item)
    out = {"quantity": number(qty) if qty is not None else "", "unit": unit, "resolved": resolved,
           "stock": f"{number(qty)} {label(unit, qty)}" if qty is not None else "Billed quantity missing",
           "equivalent": UNRESOLVED, "base_quantity": None, "base_unit": definition.get("base_unit") or "UNIT"}
    if resolved and d.get("received_base_units") is not None:
        base = int(d["received_base_units"])
        out["base_quantity"] = base
        out["equivalent"] = stock_equivalent(
            base, units_per_pack=definition.get("units_per_pack") or 1, base_unit=out["base_unit"],
            pack_unit=definition.get("pack_unit") or "PACK", units_per_invoice_unit=d.get("units_per_invoice_unit"),
            content=content)
    elif qty is None:
        out["equivalent"] = ""
    return out


def unit_costs(value: Any, view: dict, decision: dict | None, definition: dict) -> dict:
    """What one invoice unit, one retail pack and one base unit cost, from a line value
    (e.g. the landed amount incl. GST). Each is None when it cannot be known exactly."""
    from app.utils import money

    total = _dec(value)
    d = decision or {}
    out = {"per_invoice_unit": None, "per_pack": None, "per_base": None,
           "invoice_unit": view.get("unit") or PURCHASE_PACK, "pack_unit": definition.get("pack_unit") or "PACK",
           "base_unit": definition.get("base_unit") or "UNIT"}
    if total is None:
        return out
    qty = _dec(view.get("quantity", "").replace(",", "")) if view.get("quantity") else None
    if qty:
        out["per_invoice_unit"] = str(money(total / qty))
    packs = _dec(d.get("master_pack_equivalent")) if d.get("resolved") else None
    if packs:
        out["per_pack"] = str(money(total / packs))
    base = view.get("base_quantity")
    if base and (definition.get("units_per_pack") or 1) > 1:
        out["per_base"] = str(money(total / base))
    return out


def receipt_levels(decision: dict, definition: dict) -> tuple[dict | None, dict | None]:
    """Purchase / retail / base levels of a receipt's paid and free ledger movements."""
    d = decision or {}
    if not d.get("resolved"):
        return None, None
    upp = max(int(definition.get("units_per_pack") or 1), 1)
    unit = invoice_unit(d, definition)
    paid, free = _dec(d.get("paid")) or Decimal(0), _dec(d.get("free")) or Decimal(0)
    split = bool(d.get("financial_split_only"))

    def level(invoice_qty: Decimal, base_units: int) -> dict | None:
        if not base_units:
            return None
        return {"purchase_quantity": invoice_qty, "purchase_uom": unit if unit != PURCHASE_PACK else "PACK",
                "retail_quantity": Decimal(base_units) / upp, "retail_uom": definition.get("pack_unit") or "PACK",
                "base_uom": definition.get("base_unit") or "UNIT"}

    paid_units, free_units = int(d.get("ledger_paid_units") or 0), int(d.get("ledger_free_units") or 0)
    return (level(paid + free if split else paid, paid_units), None if split else level(free, free_units))
