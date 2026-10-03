"""ProductMatcher: the matching cascade, with an explanation for every score.

    0 saved supplier alias (mapping store)           purchasing._match   → applied
    1 supplier item code / barcode / GTIN            purchasing._match   → applied
    2 exact name (unique)                            purchasing._match   → applied
    3 normalised name — superficial differences only → this module        → applied when unique
      (ROSUBEST 10 TAB = ROSUBEST-10 TABLET; OMNI GEL 20 GMS = OMNIGEL 20G)
    4 shortlist + RapidFuzz + attribute comparison   → suggestions with signals, never applied
    5 exception inbox with the top candidates

Normalisation never removes what changes the medicine: strengths and formulation markers
(SR, CR, ER, XR, DS, FORTE, PLUS, M, H, CV, D …) stay in the key, so ``DRUG 5 mg`` can never
meet ``DRUG 10 mg``. Price is a supporting signal only.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Item
from app.services import packaging_parser as pp

_WORDS = {"TABLETS": "TAB", "TABLET": "TAB", "TABS": "TAB", "CAPSULES": "CAP", "CAPSULE": "CAP", "CAPS": "CAP",
          "SYRUP": "SYP", "SYR": "SYP", "SUSPENSION": "SUSP", "INJECTION": "INJ", "INJ": "INJ", "OINTMENT": "OINT",
          "DROPS": "DROP", "CREAMS": "CREAM", "SACHETS": "SACHET", "GRAMS": "G", "GRAM": "G", "GMS": "G", "GM": "G",
          "GRM": "G", "GRMS": "G", "MILLILITRE": "ML", "MLS": "ML", "VAIL": "VIAL"}
_UNITS = ("MG", "MCG", "G", "KG", "ML", "L", "IU", "%")
FORM_TOKENS = {"TAB", "CAP", "SYP", "SUSP", "INJ", "OINT", "DROP", "CREAM", "GEL", "LOTION", "SACHET", "POWDER",
               "SPRAY", "VIAL", "AMP", "INHALER", "SOFTGEL", "SG", "DT", "MD"}
MARKERS = {"SR", "CR", "ER", "XR", "XL", "MR", "OD", "DS", "FORTE", "PLUS", "M", "H", "CV", "D", "D3", "LS", "AM", "AT",
           "MAX", "KID", "KIDS", "JUNIOR", "PAED", "NEO", "NEW", "TRIO", "MF", "LC", "AZ", "OZ"}
DEFAULT_WEIGHTS = {"supplierAlias": 0.30, "name": 0.30, "strength": 0.12, "dosageForm": 0.08, "suffix": 0.08,
                   "packaging": 0.05, "mrp": 0.04, "history": 0.03}


def tokens(text: str) -> list[str]:
    """Superficial normalisation: case, punctuation, hyphens, spacing, unit and form spellings."""
    t = str(text or "").upper().replace("&", " AND ")
    t = re.sub(r"(\d)\s*[-/]?\s*(MG|MCG|GMS|GRMS|GRM|GM|G|KG|MLS|ML|L|IU|%)\b", r"\1 \2", t)      # 20GM → 20 GM
    t = re.sub(r"([A-Z])(\d)", r"\1 \2", t)                                                     # ROSUBEST10 → ROSUBEST 10
    t = re.sub(r"(\d)([A-Z]{3,})", r"\1 \2", t)                                                 # 650TAB → 650 TAB
    words = re.findall(r"\d+(?:\.\d+)?|[A-Z]+|%", t)
    return [_WORDS.get(w, w) for w in words]


def key(text: str) -> str:
    """Compact comparison key: OMNI GEL 20 GMS → OMNIGEL20G."""
    return "".join(tokens(text))


def core_key(text: str) -> str:
    """The key without form words (for products named without their form)."""
    return "".join(w for w in tokens(text) if w not in FORM_TOKENS)


def attributes(text: str) -> dict:
    words = tokens(text)
    strengths, i = [], 0
    while i < len(words):
        w = words[i]
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            unit = words[i + 1] if i + 1 < len(words) and words[i + 1] in _UNITS else ""
            strengths.append((Decimal(w).normalize(), unit))
            i += 2 if unit else 1
            continue
        i += 1
    form, _ = pp.detect_dosage_form(description=text)
    brand = next((w for w in words if not w.isdigit() and w not in FORM_TOKENS and w not in MARKERS), "")
    return {"brand": brand, "strengths": strengths, "form": form,
            "markers": sorted({w for w in words if w in MARKERS}), "tokens": words}


def _numbers(strengths) -> set:
    return {v for v, _ in strengths}


def strength_conflict(a: dict, b: dict) -> bool:
    na, nb = _numbers(a["strengths"]), _numbers(b["strengths"])
    return bool(na and nb and na != nb)


def form_conflict(a: str, b: str) -> bool:
    fam = lambda f: {"SOFTGEL": "CAPSULE", "ROTACAP": "CAPSULE", "SUSPENSION": "SYRUP", "AMPOULE": "INJECTION",
                     "VIAL": "INJECTION"}.get(f, f)
    return bool(a and b and fam(a) != fam(b))


# --------------------------------------------------------------------------- level 3: normalised name
_INDEX: dict = {"sig": None, "full": {}, "core": {}}


def _index(db: Session) -> dict:
    sig = db.execute(select(func.count(Item.id), func.max(Item.id), func.max(Item.updated_at), func.sum(func.length(Item.name)),
                            func.min(Item.created_at)).where(Item.deleted_at.is_(None))).one()
    if _INDEX["sig"] != tuple(sig):
        full, core = {}, {}
        for iid, name in db.execute(select(Item.id, Item.name).where(Item.deleted_at.is_(None))):
            full.setdefault(key(name), set()).add(iid)
            core.setdefault(core_key(name), set()).add(iid)
        _INDEX.update(sig=tuple(sig), full=full, core=core)
    return _INDEX


def normalized_match(db: Session, description: str, pack: str = "") -> tuple[Item | None, str]:
    """A product whose name differs only superficially, when exactly one exists. (item, reason)."""
    k = key(description)
    if not k or len(k) < 4:
        return None, ""
    idx = _index(db)
    ids = idx["full"].get(k, set())
    want = attributes(description)
    if len(ids) == 1:
        item = db.get(Item, next(iter(ids)))
        if item is not None and not form_conflict(want["form"], item.dosage_form or attributes(item.name)["form"]):
            return item, f"normalised name {k} is identical"
        return None, ""
    if ids:
        return None, ""
    ids = idx["core"].get(core_key(description), set())
    if len(ids) != 1 or not want["form"]:
        return None, ""
    item = db.get(Item, next(iter(ids)))
    from app.services import packaging_service

    have = item.dosage_form or packaging_service.detect_form(item) if item is not None else ""
    if item is None or not have or form_conflict(want["form"], have):
        return None, ""                                # the product's form must be known and agree
    return item, f"same name without form words, and both are {want['form'].lower()}s"


# --------------------------------------------------------------------------- explainable scores
def weights(db: Session | None = None) -> dict:
    if db is None:
        return dict(DEFAULT_WEIGHTS)
    from app.services import settings_service

    try:
        saved = json.loads(settings_service.get_setting(db, "purchase_match_weights", "") or "{}")
    except ValueError:
        saved = {}
    out = dict(DEFAULT_WEIGHTS)
    out.update({k: float(v) for k, v in saved.items() if k in DEFAULT_WEIGHTS})
    return out


def explain(db: Session, line, item: Item, *, alias: bool = False, history: bool = False, w: dict | None = None) -> dict:
    """Score one candidate for one invoice line, with every signal and the reasons in words."""
    from rapidfuzz import fuzz

    w = w or weights(db)
    want, have = attributes(line.product_name), attributes(item.name)
    reasons = []
    s = {"supplierAlias": 1.0 if alias else 0.0,
         "name": round(max(fuzz.token_set_ratio(" ".join(want["tokens"]), " ".join(have["tokens"])),
                           fuzz.ratio(key(line.product_name), key(item.name))) / 100, 3)}
    if want["strengths"] and have["strengths"]:
        s["strength"] = 0.0 if strength_conflict(want, have) else 1.0
        reasons.append(("strength " + ", ".join(f"{format(v, 'f')} {u.lower()}".strip() for v, u in want["strengths"])
                        + (" matches" if s["strength"] else f" differs from {', '.join(format(v, 'f') for v, _ in have['strengths'])}")))
    else:
        s["strength"] = 0.5
    item_form = item.dosage_form or have["form"]
    s["dosageForm"] = 0.5 if not (want["form"] and item_form) else (0.0 if form_conflict(want["form"], item_form) else 1.0)
    if s["dosageForm"] != 0.5:
        reasons.append(f"dosage form {'matches' if s['dosageForm'] else 'differs'} ({(want['form'] or '').lower()} / {item_form.lower()})")
    s["suffix"] = 1.0 if want["markers"] == have["markers"] else 0.0
    if want["markers"] or have["markers"]:
        reasons.append("formulation markers " + ("match" if s["suffix"] else f"differ ({'/'.join(want['markers']) or 'none'} vs {'/'.join(have['markers']) or 'none'})"))
    if line.pack_size and item.pack_size:
        same = pp.same_package(line.pack_size, item.pack_size)
        s["packaging"] = 1.0 if same else 0.5
        reasons.append("package compatible" if same else f"pack {item.pack_size} differs from {line.pack_size}")
    else:
        s["packaging"] = 0.5
    if line.mrp and item.mrp:
        ratio = float(min(line.mrp, item.mrp) / max(line.mrp, item.mrp))
        s["mrp"] = round(ratio, 3)
        reasons.append("MRP within historical range" if ratio >= 0.85 else "MRP differs (supporting signal only)")
    else:
        s["mrp"] = 0.5
    s["history"] = 1.0 if history else 0.0
    if alias:
        reasons.insert(0, "supplier alias found")
    if history:
        reasons.append("supplier history found")
    score = sum(w[k] * s[k] for k in w) / sum(w.values())
    capped = s["strength"] == 0.0 or s["suffix"] == 0.0 or s["dosageForm"] == 0.0
    if capped:
        score = min(score, 0.6)                       # a strength / formulation / form mismatch never auto-accepts
    return {"productId": item.id, "name": item.name, "pack": item.pack_size, "score": round(score, 3),
            "signals": s, "reasons": reasons, "capped": capped}


def inspect(db: Session, purchase, line, limit: int = 5) -> dict:
    """Match Inspector: why the line is matched as it is, and the best alternatives."""
    from app.models import PurchaseItem, Purchase, SupplierProductMap
    from app.services import purchasing

    w = weights(db)
    bought = set()
    if purchase.supplier_id:
        bought = set(db.scalars(select(PurchaseItem.item_id).join(Purchase).where(
            Purchase.supplier_id == purchase.supplier_id, PurchaseItem.status == "POSTED", PurchaseItem.item_id.is_not(None))))
    alias_ids = set()
    if purchase.supplier_id:
        k = purchasing.description_key(line.description_raw or line.product_name)
        alias_ids = set(db.scalars(select(SupplierProductMap.item_id).where(
            SupplierProductMap.supplier_id == purchase.supplier_id,
            (SupplierProductMap.description_key == k) | ((SupplierProductMap.supplier_code == line.supplier_code) & (SupplierProductMap.supplier_code != "")))))
    current = None
    if line.item is not None:
        current = explain(db, line, line.item, alias=line.item.id in alias_ids, history=line.item.id in bought, w=w)
        current["method"] = line.match_method
    cands = []
    for s in purchasing.suggestions(db, line, limit=limit + 1):
        if line.item is not None and s["item_id"] == line.item.id:
            continue
        item = db.get(Item, s["item_id"])
        cands.append(explain(db, line, item, alias=item.id in alias_ids, history=item.id in bought, w=w))
    cands.sort(key=lambda c: -c["score"])
    return {"line": line.line_no, "description": line.product_name, "matched": current, "candidates": cands[:limit], "weights": w}
