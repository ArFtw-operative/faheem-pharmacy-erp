"""ConfidenceGate: per-field confidence for each purchase line, and the decision it leads to.

Confidence is per field, not per row: one weak field does not make the whole row manual.
Critical fields (a mistake damages stock) drive the decision — product, billed quantity,
batch, expiry and the pack multiplier. A weak pack conversion never hides the received
quantity; it only stops the stock conversion until it is confirmed.

    AUTO_ACCEPT               every critical field ≥ auto threshold          receive automatically
    AUTO_ACCEPT_WITH_WARNING  ≥ warning threshold                             receive, show a warning
    REVIEW                    ≥ review threshold, or anything still open      exception inbox
    BLOCK                     a critical field below review, or impossible    not received until fixed

Thresholds are settings (``purchase_gate_thresholds``), not constants.
Each value also records where it came from (provenance).
"""
from __future__ import annotations

import json
from decimal import Decimal

AUTO_ACCEPT, WARNING, REVIEW, BLOCK = "AUTO_ACCEPT", "AUTO_ACCEPT_WITH_WARNING", "REVIEW", "BLOCK"
PROPOSAL_CODES = {"count_proposed", "new_product_unconfirmed", "product_unit_unknown", "variant_proposed", "match_proposed"}
DEFAULTS = {"auto": 0.97, "warn": 0.90, "review": 0.75}
CRITICAL = ("product", "quantity", "batch", "expiry", "packaging")

# product match method → confidence and provenance
_PRODUCT = {"SUPPLIER_MAP": (0.995, "SAVED_ALIAS"), "CODE": (0.99, "SOURCE_FILE"), "EXACT_NAME": (0.98, "PRODUCT_MASTER"),
            "CANONICAL_NAME_PACK": (0.975, "PRODUCT_MASTER"), "NORMALIZED_NAME": (0.97, "PRODUCT_MASTER"),
            "PROPOSED_MATCH": (0.9, "FUZZY_MATCH"),
            "MANUAL": (1.0, "USER_CORRECTION"), "NEW_PRODUCT": (1.0, "USER_CORRECTION"), "GS1": (0.995, "GS1_BARCODE")}
# receipt decision source → pack multiplier confidence and provenance
_PACK = {"CONFIRMED": (1.0, "USER_CORRECTION"), "SUPPLIER_PACKING_ALIAS": (0.995, "SAVED_ALIAS"),
         "SUPPLIER_MEMORY": (0.995, "SAVED_ALIAS"), "PACK_AND_MASTER": (0.99, "PACKAGING_RULE"),
         "REVIEWED_SUPPLIER_HISTORY": (0.98, "SUPPLIER_PROFILE"), "PRODUCT_MASTER": (0.975, "PRODUCT_MASTER"),
         "NEW_PRODUCT_DEFINITION": (0.975, "USER_CORRECTION"), "SUPPLIER_LAYOUT_CONVENTION": (0.95, "SUPPLIER_PROFILE")}


def thresholds(db=None) -> dict:
    if db is None:
        return dict(DEFAULTS)
    from app.services import settings_service

    try:
        saved = json.loads(settings_service.get_setting(db, "purchase_gate_thresholds", "") or "{}")
    except ValueError:
        saved = {}
    out = dict(DEFAULTS)
    out.update({k: float(v) for k, v in saved.items() if k in DEFAULTS and 0 < float(v) <= 1})
    return out


def _only_acceptable_warnings(line) -> bool:
    from app.services import purchasing

    open_ = [i for i in (line.issues or []) if not i.get("accepted") and i.get("level") not in ("info", "proposal")]
    return bool(open_) and all(i.get("level") == "warn" and i["code"] in purchasing.ACCEPTABLE
                               and i["code"] not in purchasing.BULK_ACCEPTABLE_EXCLUDED for i in open_)


def _open_codes(line, levels=None) -> set[str]:
    return {i["code"] for i in (line.issues or []) if not i.get("accepted")
            and (i.get("level") in levels if levels else i.get("level") not in ("info", "proposal"))}


def fields(line) -> tuple[dict, dict]:
    """(confidence per field, provenance per field) for one line."""
    codes = _open_codes(line)
    all_codes = {i["code"] for i in (line.issues or [])}
    conf, prov = {}, {}
    ocr = (line.raw or {}).get("_ocr")                      # values read by OCR carry its confidence
    source = "OCR" if ocr else "SOURCE_FILE"
    # product
    if line.item is not None or line.new_product:
        method = line.match_method or ("NEW_PRODUCT" if line.new_product else "")
        conf["product"], prov["product"] = _PRODUCT.get(method, (0.9, "FUZZY_MATCH"))
        if line.new_product and "units_suggested" in codes:
            conf["product"] = 0.85
        if "new_product_unconfirmed" in all_codes:
            auto = (line.corrections or {}).get("_automation") or {}
            conf["product"], prov["product"] = float(auto.get("confidence") or 0.9), "PROPOSED"   # created when a person posts
    else:
        conf["product"], prov["product"] = 0.0, "NONE"
    # quantity (billed + free): known whenever the invoice gives it
    conf["quantity"] = 0.0 if codes & {"quantity_invalid", "qty_missing"} else (0.85 if "qty_fraction" in codes else 1.0)
    prov["quantity"] = source
    # batch: text, never a number; missing is a warning (FMCG often has none), damaged is a block
    conf["batch"] = 0.0 if "batch_corrupt" in codes else (0.92 if not line.batch_no else 1.0)
    prov["batch"] = source
    # expiry
    if codes & {"expiry_invalid", "expired", "expiry_before_invoice"}:
        conf["expiry"] = 0.0
    elif not line.expiry_date:
        conf["expiry"] = 0.92
    elif "expiry_soon" in codes:
        conf["expiry"] = 0.93
    else:
        conf["expiry"] = 1.0
    prov["expiry"] = source
    # pack multiplier (changes stock)
    d = line.receipt_decision or {}
    if d.get("resolved") and d.get("source") == "PROPOSED":
        conf["packaging"], prov["packaging"] = float((d.get("proposal") or {}).get("confidence") or 0.85), "PROPOSED"
    elif d.get("resolved"):
        conf["packaging"], prov["packaging"] = _PACK.get(d.get("source"), (0.9, "PACKAGING_RULE"))
        if d.get("source") == "SUPPLIER_PACKING_ALIAS" and float(d.get("trust") or 1) < 0.95:
            conf["packaging"] = 0.95                     # learned from a single posting: shown until confirmed again
    else:
        conf["packaging"], prov["packaging"] = (0.5, "NONE")
    # arithmetic and price (supporting, not critical)
    conf["amount"] = 0.9 if "amount_mismatch" in all_codes and "amount_mismatch" in codes else 1.0
    conf["gst"] = 0.8 if codes & {"gst_missing", "gst_invalid"} else (0.9 if codes & {"gst_slab", "gst_changed", "gst_amount_mismatch"} else 1.0)
    conf["mrp"] = 0.9 if codes & {"mrp_below_rate"} else 1.0
    if ocr:
        factor = Decimal(str(ocr.get("confidence", 0.9)))
        if not ocr.get("reconciled") and not set(line.corrections or {}) & {"quantity", "free", "rate", "amount", "mrp"}:
            factor = min(factor, Decimal("0.6"))
        for k in ("quantity", "batch", "expiry"):
            conf[k] = float(min(Decimal(str(conf[k])), factor))
    return conf, prov


def assess(line, limits: dict | None = None) -> dict:
    """The gate's verdict for one line: state, overall confidence, per-field detail."""
    from app.services import purchasing

    limits = limits or DEFAULTS
    conf, prov = fields(line)
    critical = min(conf[k] for k in CRITICAL)
    proposed = bool(_open_codes(line, levels=("proposal",)) & PROPOSAL_CODES)
    if line.status == purchasing.INVALID or any(conf[k] == 0.0 for k in ("quantity", "batch", "expiry")):
        state = BLOCK
    elif "unit_price_unusual" in _open_codes(line):
        state = REVIEW                                   # the count looks implausibly priced: a person looks first
    elif proposed and line.status in purchasing.POSTABLE:
        state = WARNING                                  # counted by proposal: a person looks, then posts
    elif line.status == purchasing.REVIEW and _only_acceptable_warnings(line) and critical >= limits["review"]:
        state = WARNING                                  # expiry soon, GST changed …: accepted when a person posts
    elif line.status in (purchasing.REVIEW, purchasing.MATCH) or critical < limits["warn"]:
        state = REVIEW if critical >= limits["review"] or conf["product"] == 0.0 or conf["packaging"] >= 0.5 else BLOCK
    elif critical >= limits["auto"] and min(conf["amount"], conf["gst"], conf["mrp"]) >= limits["warn"]:
        state = AUTO_ACCEPT if not _open_codes(line) else WARNING
    else:
        state = WARNING
    weakest = min(CRITICAL, key=lambda k: conf[k])
    return {"state": state, "confidence": round(critical, 3), "weakest": weakest, "proposed": proposed,
            "fields": {k: round(v, 3) for k, v in conf.items()}, "provenance": prov}


def line_state(line, limits: dict | None = None) -> str:
    from app.services import purchasing

    if line.status in purchasing.DONE:
        return AUTO_ACCEPT if line.status == purchasing.POSTED else REVIEW
    return assess(line, limits)["state"]
