"""ReceiptProposer: a count for every purchase line, weighed from evidence, when nothing certain decides it.

The billed and free quantities are facts and are never proposed. What is proposed is how
many stock units one invoice Qty is (and, for a product the catalogue does not have yet,
what its stock unit and retail pack are). Every reading the printed pack and the product
allow is a candidate; each is scored by independent evidence, all of it learned from the
pharmacy's own data or the reference catalogue — no product or supplier is named in code:

    retail prior        an invoice usually bills the pack the MRP is printed on (the retail pack)
    supplier history    how this supplier's posted nested-pack lines were billed (retail or outer)
    invoice consensus   how the other, already certain lines of the same invoice are billed
    product MRP         the invoice MRP against the product's own known MRP per retail pack
    reference price     the reference catalogue's MRP for the listed retail pack
    unit price          the MRP per tablet / bottle / piece typical in the pharmacy's batches
    physics             only whole stock units can arrive

The best candidate wins; its confidence is its share of the evidence (softmax). When no
candidate is supported the line is counted in whole purchase packs, which is always
physically right. A proposal is shown with its reasons, never posted without a person, and
a person posting it teaches the supplier packing alias for next time.
"""
from __future__ import annotations

import json
import math
import time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Batch, Item, Purchase, PurchaseItem
from app.services import packaging_parser as pp

# uncertainty a proposal may settle; anything else (a product variant, an unreadable quantity) stays with a person
SOFT = {"invoice_unit_ambiguous", "pack_unreadable", "master_unit_unverified", "master_container_unverified",
        "strength_not_pack", "conversion_missing"}
DEFAULT_WEIGHTS = {"retail_prior": 1.0, "supplier_history": 2.0, "invoice_consensus": 1.5, "product_mrp": 2.5,
                   "reference_price": 2.0, "unit_price": 0.8}
FALLBACK_CONFIDENCE = 0.85
_CACHE: dict = {}


def weights(db: Session) -> dict:
    from app.services import settings_service

    try:
        saved = json.loads(settings_service.get_setting(db, "purchase_count_weights", "") or "{}")
    except ValueError:
        saved = {}
    return {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in saved.items() if k in DEFAULT_WEIGHTS}}


# --------------------------------------------------------------------------- learned statistics
def unit_price_prior(db: Session) -> dict[str, tuple[float, float]]:
    """(mean, spread) of log MRP per stock unit for each stock unit, from the pharmacy's batches."""
    hit = _CACHE.get("prior")
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    rows = db.execute(select(Item.base_unit, Batch.mrp, Batch.units_per_pack)
                      .join(Batch, Batch.item_id == Item.id).where(Batch.mrp > 0)).all()
    groups: dict[str, list[float]] = {}
    for base, mrp, upp in rows:
        groups.setdefault(base or "UNIT", []).append(math.log(float(mrp) / max(int(upp or 1), 1)))
    prior = {}
    for base, values in groups.items():
        if len(values) < 25:
            continue
        values.sort()
        mid = values[len(values) // 2]
        mad = sorted(abs(v - mid) for v in values)[len(values) // 2] or 0.5
        prior[base] = (mid, max(mad * 1.4826, 0.35))
    _CACHE["prior"] = (time.monotonic(), prior)
    return prior


def supplier_history(db: Session, supplier_id: int | None, exclude: int | None = None) -> tuple[float, float]:
    """(retail, outer) weighted counts: how this supplier's posted nested-pack lines were billed.
    Confirmed conversions weigh double; reviewed facts kept across a data reset count too."""
    if not supplier_id:
        return 0.0, 0.0
    retail = outer = 0.0
    rows = db.execute(select(PurchaseItem.pack_size, PurchaseItem.receipt_decision, PurchaseItem.corrections)
                      .join(Purchase).where(Purchase.supplier_id == supplier_id, PurchaseItem.status == "POSTED",
                                            Purchase.id != (exclude or 0))).all()
    for pack, decision, corrections in rows:
        d = decision or {}
        reading = pp.parse(pack)
        if not d.get("resolved") or not reading.levels or len(reading.levels) < 2 or not d.get("master_pack_equivalent"):
            continue
        master = Decimal(d["received_base_units"]) / Decimal(d["master_pack_equivalent"])
        factor = d.get("units_per_invoice_unit")
        weight = 2.0 if (corrections or {}).get("_invoice_unit") else 1.0
        if factor and master and Decimal(str(factor)) == master:
            retail += weight
        elif factor and master and Decimal(str(factor)) > master:
            outer += weight
    from app.services import settings_service

    for fact in json.loads(settings_service.get_setting(db, "purchase_reviewed_pack_knowledge", "[]") or "[]"):
        if fact.get("supplier_id") == supplier_id and fact.get("factor") and fact.get("master"):
            retail += 2.0 if fact["factor"] == fact["master"] else 0.0
            outer += 2.0 if fact["factor"] > fact["master"] else 0.0
    return retail, outer


def invoice_consensus(purchase: Purchase, line: PurchaseItem) -> tuple[int, int]:
    """(retail, outer) among the invoice's other nested-pack lines settled by certain evidence."""
    retail = outer = 0
    for other in purchase.items:
        d = other.receipt_decision or {}
        if other is line or not d.get("resolved") or d.get("source") in ("PROPOSED", None) or not d.get("master_pack_equivalent"):
            continue
        reading = pp.parse(other.pack_size)
        if len(reading.levels) < 2:
            continue
        master = Decimal(d["received_base_units"]) / Decimal(d["master_pack_equivalent"])
        factor = Decimal(str(d.get("units_per_invoice_unit") or 0))
        retail += factor == master
        outer += factor > master
    return retail, outer


def known_retail_mrp(db: Session, item: Item | None) -> Decimal | None:
    """The product's MRP per retail pack: its latest batch (normalised to the current pack), else the master."""
    if item is None:
        return None
    upp = max(int(item.units_per_pack or 1), 1)
    b = db.scalar(select(Batch).where(Batch.item_id == item.id, Batch.mrp > 0).order_by(Batch.id.desc()).limit(1))
    if b is not None:
        return Decimal(b.mrp) * upp / max(int(b.units_per_pack or 1), 1)
    return Decimal(item.mrp) if item.mrp else None


def price_z(db: Session, base: str, unit_mrp) -> float | None:
    """How unusual an MRP per stock unit is for this kind of unit in the pharmacy (robust z-score)."""
    prior = unit_price_prior(db).get(base)
    if not prior or not unit_mrp or float(unit_mrp) <= 0:
        return None
    return (math.log(float(unit_mrp)) - prior[0]) / prior[1]


# --------------------------------------------------------------------------- scoring
def _near(ratio: float, tight: float) -> float:
    """+1 when the ratio is within ``tight`` of 1, −1 when it is far (beyond 2.2×), 0 in between."""
    if ratio <= 0:
        return 0.0
    dev = abs(math.log(ratio))
    if dev <= math.log(1 + tight):
        return 1.0
    if dev >= math.log(2.2):
        return -1.0
    return 0.0


def _softmax(scores: dict) -> dict:
    top = max(scores.values())
    exp = {k: math.exp(v - top) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


def _received(decision: dict) -> Decimal:
    return Decimal(decision.get("paid") or 0) + Decimal(decision.get("free") or 0)


def propose(db: Session, purchase: Purchase, line: PurchaseItem, decision: dict) -> dict | None:
    """A confirmation-shaped proposal for an unresolved line, or None (left to a person)."""
    from app.services import medicine_reference, receipt_decision as rd

    codes = {i["code"] for i in decision.get("issues") or []}
    if not codes or not codes <= SOFT or decision.get("paid") is None or _received(decision) <= 0:
        return None
    d = rd.definition(line)
    m = int(d.get("units_per_pack") or 0)
    base = d.get("base_unit") or "UNIT"
    if m < 1:
        return None                                    # no stock unit yet: the product definition comes first
    form = d.get("dosage_form") or (line.item.dosage_form if line.item else "") or ""
    reading = pp.parse(line.pack_size, master_form=form, description=line.product_name)
    received = _received(decision)
    w = weights(db)
    # candidate factors (stock units per invoice Qty) and how each reads
    cands: dict[int, str] = {m: f"one retail {(d.get('pack_unit') or 'pack').lower()} of {m}"}
    generic = base in ("PACK", "UNIT") and m == 1
    if not generic and m > 1:
        outers = set()
        if reading.retail and reading.purchase and reading.purchase.unit == "BOX":
            outers.add(reading.retail.quantity)
        if reading.levels and len(reading.levels) >= 2:
            outers.add(reading.levels[0].quantity)
        for k in sorted(o for o in outers if o and o > 1):
            cands[m * k] = f"one box of {k} × {m}"
        cands.setdefault(1, f"one {base.lower()}")
    cands = {f: why for f, why in cands.items() if (received * f) % 1 == 0}
    if not cands:
        return None
    if len(cands) == 1:
        (f, why), = cands.items()
        reasons = [f"Counted as {why}" + (" — the catalogue counts this product in whole packs; set the strip / unit count in "
                                          "Inventory if it is sold loose" if generic else "")]
        conf = 0.93 if base not in ("PACK", "UNIT") else 0.9
        return _result(f, conf, reasons, {f: 1.0}, cands)
    scores = {f: 0.0 for f in cands}
    reasons: dict[int, list[str]] = {f: [] for f in cands}
    for f in cands:                                                 # retail prior
        scores[f] += w["retail_prior"] * (1.0 if f == m else (-1.5 if f == 1 else 0.0))
    nested = len(reading.levels) >= 2 and any(f > m for f in cands)
    if nested:
        r, o = supplier_history(db, purchase.supplier_id, exclude=purchase.id)
        if r + o:
            p_retail = (r + 1) / (r + o + 2)
            for f in cands:
                if f == m:
                    scores[f] += w["supplier_history"] * (2 * p_retail - 1)
                elif f > m:
                    scores[f] += w["supplier_history"] * (1 - 2 * p_retail)
            reasons[m].append(f"this supplier billed the retail pack on {r:g} of {r + o:g} earlier nested-pack lines")
        r, o = invoice_consensus(purchase, line)
        if r + o:
            p_retail = (r + 1) / (r + o + 2)
            for f in cands:
                scores[f] += w["invoice_consensus"] * ((2 * p_retail - 1) if f == m else (1 - 2 * p_retail) if f > m else 0)
            reasons[m].append(f"{r} of {r + o} certain nested-pack lines on this invoice are billed by the retail pack")
    mrp = Decimal(line.mrp or 0)
    known = known_retail_mrp(db, line.item)
    if mrp > 0 and known:
        for f in cands:
            fit = _near(float(mrp / (known * f / m)), 0.18)
            scores[f] += w["product_mrp"] * fit
            if fit > 0:
                reasons[f].append(f"invoice MRP ₹{mrp} matches the product's MRP for this quantity")
    ref = medicine_reference.price_evidence(line.product_name, line.manufacturer)
    if mrp > 0 and ref and ref["count"] and ref["unit"] in ("TABLET", "CAPSULE", base):
        for f in cands:
            fit = _near(float(mrp) / (ref["price"] * f / ref["count"]), 0.35)
            scores[f] += w["reference_price"] * fit
            if fit > 0:
                reasons[f].append(f"reference MRP ₹{ref['price']:g} for {ref['count']} matches")
    prior = unit_price_prior(db).get(base)
    if mrp > 0 and prior:
        mu, sd = prior
        for f in cands:
            z = (math.log(float(mrp) / f) - mu) / sd          # MRP per stock unit under this reading
            scores[f] -= w["unit_price"] * abs(z)
    share = _softmax(scores)
    best = max(share, key=share.get)
    out_reasons = [f"Counted as {cands[best]}"] + reasons[best]
    z = price_z(db, base, mrp / best) if mrp > 0 else None
    if z is not None and abs(z) > 3:
        share[best] = min(share[best], 0.6)            # the winner itself looks implausibly priced: say so
        out_reasons.append(f"but ₹{(mrp / best):.2f} MRP per {base.lower()} is unusual here — check the product's pack")
    alternatives = sorted((f for f in cands if f != best), key=lambda f: -share[f])
    if alternatives:
        out_reasons.append("other reading: " + ", ".join(f"{cands[f]} ({share[f]:.0%})" for f in alternatives))
    return _result(best, round(share[best], 3), out_reasons, share, cands)


def _result(factor: int, confidence: float, reasons: list[str], share: dict, cands: dict) -> dict:
    # the invoice prints the MRP of what it bills: per invoice unit
    return {"units_per_invoice_unit": int(factor), "mrp_basis": "INVOICE_UNIT", "source": "PROPOSED",
            "evidence": reasons, "confidence": confidence,
            "proposal": {"confidence": confidence, "candidates": {str(f): round(share.get(f, 0), 3) for f in cands},
                         "reading": {str(f): cands[f] for f in cands}}}


# --------------------------------------------------------------------------- new products
def propose_definition(db: Session, line: PurchaseItem) -> dict | None:
    """Stock unit and retail pack for a product the catalogue does not have, from the printed pack,
    the description, the reference catalogue and the pharmacy's price levels. Always returns a
    definition: when nothing decides, the product is counted in whole packs."""
    from app.services import medicine_reference

    reading = pp.parse(line.pack_size, description=line.product_name)
    form = reading.dosage_form
    container = pp.CONTAINER.get(form, "")
    mrp = Decimal(line.mrp or 0)
    prior = unit_price_prior(db)
    ref = medicine_reference.price_evidence(line.product_name, line.manufacturer)
    cands: dict[tuple, list[str]] = {}
    strength_only = bool(reading.content and reading.content[1] in ("MG", "MCG"))
    if form in pp.SOLID and (strength_only or reading.base is None or (reading.base.quantity == 1 and not reading.levels[1:])):
        # tablets per strip not printed: count strips, which is always right; refine in Inventory to sell loose
        if not (reading.ambiguous_count and reading.levels and reading.levels[0].quantity > 1):
            return {"base_unit": "STRIP", "pack_unit": "STRIP", "units_per_pack": 1, "dosage_form": form,
                    "confidence": FALLBACK_CONFIDENCE, "reasons": ["counted in strips (tablets per strip are not printed)"]}
    if reading.base is not None and reading.base.unit and not reading.ambiguous_count and not strength_only:
        base = reading.base.unit
        retail = reading.retail_to_base or 1
        if base in ("TABLET", "CAPSULE"):
            cands[(base, "STRIP", retail, form)] = [f"strip of {retail} read from the pack"]
        elif base == "PIECE" and reading.base.quantity > 1:
            cands[("PIECE", "BOX", reading.base.quantity, form)] = [f"box of {reading.base.quantity} pieces read from the pack"]
            cands[("PIECE", "PIECE", 1, form)] = ["one piece per invoice Qty (box of pieces billed singly)"]
        else:
            cands[(base, base, 1, form)] = [f"one {base.lower()} per invoice Qty"]
    if reading.ambiguous_count and form in pp.SOLID and reading.levels:
        n = reading.levels[0].quantity
        unit = "CAPSULE" if form in ("CAPSULE", "SOFTGEL", "ROTACAP") else "TABLET"
        if n > 1:
            cands[(unit, "STRIP", n, form)] = [f"strip of {n} (the pack text read the other way round)"]
        cands[(unit, "STRIP", 1, form)] = ["one per pack, as printed"]
    if reading.base is not None and not reading.base.unit and reading.base.quantity > 1 and not reading.ambiguous_count:
        per = reading.retail_to_base if reading.retail_to_base and reading.retail_to_base > 1 else reading.base.quantity
        cands[("UNIT", "STRIP", per, form)] = [f"{per} units per strip (form not stated)"]
    if container and not cands:
        cands[(container, container, 1, form)] = [f"one {container.lower()} per invoice Qty"]
    if not cands:
        if form in pp.SOLID:
            return {"base_unit": "STRIP", "pack_unit": "STRIP", "units_per_pack": 1, "dosage_form": form,
                    "confidence": FALLBACK_CONFIDENCE, "reasons": ["counted in strips (tablets per strip are not printed)"]}
        return {"base_unit": "PACK", "pack_unit": "PACK", "units_per_pack": 1, "dosage_form": form or "",
                "confidence": FALLBACK_CONFIDENCE, "reasons": ["counted in whole packs (the pack text does not say more)"]}
    scores = {}
    for key, why in cands.items():
        base, pack_unit, retail, _ = key
        score = 0.0
        if ref and ref["count"] and ref["unit"] in (base, "TABLET", "CAPSULE") and mrp > 0:
            fit = _near(float(mrp) / (ref["price"] * retail / ref["count"]), 0.35)
            score += 2.0 * fit + (1.0 if ref["count"] == retail else 0.0)
            if fit > 0:
                why.append(f"reference MRP ₹{ref['price']:g} for {ref['count']} agrees")
        p = prior.get(base)
        if p and mrp > 0:
            z = (math.log(float(mrp) / retail) - p[0]) / p[1]
            score -= 0.8 * abs(z)
        scores[key] = score
    share = _softmax(scores)
    best = max(share, key=share.get)
    base, pack_unit, retail, form_ = best
    conf = round(min(0.97, share[best]) if len(share) > 1 else 0.93, 3)
    return {"base_unit": base, "pack_unit": pack_unit, "units_per_pack": retail, "dosage_form": form_ or "",
            "confidence": conf, "reasons": cands[best] + ([f"other reading: {', '.join(f'{k[2]} per {k[1].lower()} ({share[k]:.0%})' for k in share if k != best)}"]
                                                          if len(share) > 1 else [])}


def category_for(db: Session, dosage_form: str) -> str:
    """The category most products of this form already have (≥60 % of at least 10), else blank."""
    if not dosage_form:
        return ""
    rows = db.execute(select(Item.category, func.count(Item.id)).where(Item.dosage_form == dosage_form, Item.deleted_at.is_(None))
                      .group_by(Item.category)).all()
    total = sum(n for _, n in rows)
    if total < 10:
        return ""
    cat, n = max(rows, key=lambda r: r[1])
    return cat if n / total >= 0.6 else ""
