"""Local evidence-driven intake. No model calls, supplier branches or schema changes.

Evidence is recomputed before posting. Automatic decisions cannot train their
own supplier convention; only explicitly reviewed, posted receipts can do so.
"""
from __future__ import annotations

import re
import calendar
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models import Item, Purchase, PurchaseItem, SupplierProductMap
from app.services import medicine_reference as reference, packaging_service, receipt_decision as rd, settings_service, units
from app.utils import money

VERSION = 1
UOM_FIELDS = {"base_unit", "pack_unit", "units_per_pack", "dosage_form"}


def enabled(db):
    return settings_service.get_setting(db, "purchase_automation", "off") in {"prepare", "post"}


def active(db, purchase):
    return purchase.source_format != "MANUAL" and enabled(db)


def layout(purchase):
    # Ignore newly recognized invoice totals; unit semantics belong to these columns.
    fields = {"name", "supplier_code", "pack", "quantity", "free", "rate", "mrp", "amount"}
    return sorted((c.get("field"), c.get("column")) for c in (purchase.column_map or []) if c.get("field") in fields)


def _pack_key(raw):
    return re.sub(r"\s+", "", str(raw).upper()).replace("×", "X").replace("*", "X").rstrip(".")


def invoice_fingerprint(purchase):
    """Cross-format duplicate identity, independent of a PDF's truncated pack cell."""
    rows = []
    for line in purchase.items:
        try:
            paid, free = rd.quantities(rd.effective(line))
        except ValueError:
            return None
        rows.append((reference.normalize_name(line.product_name), line.batch_no.upper(),
                     str(line.expiry_date), format(paid.normalize(), 'f'), format(free.normalize(), 'f'),
                     str(money(line.rate)), str(money(line.mrp))))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest() if rows else None


def identify_posted_copy(db, purchase, user=None):
    """Use a complete, identical posted invoice to identify a duplicate's seller.

    Neither a similar supplier name nor a shared invoice number alone qualifies.
    """
    from app import audit
    from app.permissions import has_permission
    from app.services import purchasing, gst
    if len(purchase.items) < 3 or not purchase.invoice_no or purchase.supplier_total is None:
        return False
    fp = invoice_fingerprint(purchase)
    if not fp:
        return False
    possible = list(db.scalars(select(Purchase).where(Purchase.id != purchase.id, Purchase.status == 'POSTED',
                    func.lower(Purchase.invoice_no) == purchase.invoice_no.lower(),
                    Purchase.supplier_total == purchase.supplier_total).options(selectinload(Purchase.items))))
    matches = [p for p in possible if p.supplier_id and len(p.items) == len(purchase.items) and invoice_fingerprint(p) == fp]
    if len(matches) != 1 or (purchase.supplier_id and purchase.supplier_id != matches[0].supplier_id):
        return False
    old = matches[0]
    changed = not purchase.supplier_id
    purchase.supplier, purchase.supplier_id = old.supplier, old.supplier_id
    purchase.invoice_date = purchase.invoice_date or old.invoice_date
    charges = dict(purchase.charges or {})
    evidence = {"purchase_id": old.id, "fingerprint": fp}
    first = charges.get('_duplicate_evidence') != evidence
    charges['_duplicate_evidence'] = evidence
    seller = charges.get('_supplier_evidence') or {}
    # Older drafts did not retain structured seller evidence. Recover it only
    # from the unchanged file, and only for this completely corroborated copy.
    source = Path(purchase.source_file or '')
    if not seller and source.is_file() and source.stat().st_size <= 15*1024*1024:
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() == purchase.source_sha256:
            doc = purchasing._parse(db, source.name, content, None)
            if not doc.parts or len(doc.parts) == 1:
                part = (doc.parts or [doc])[0]
                seller = {"name": part.supplier_name, "gstin": part.supplier_gstin}
                charges['_supplier_evidence'] = seller
    purchase.charges = charges
    if seller.get('gstin') and not gst.gstin_problem(seller['gstin']) and not old.supplier.gst_number and has_permission(user,'supplier.manage'):
        purchasing.save_supplier(db, {'gst_number':seller['gstin']}, supplier=old.supplier, user=user)
    if first:
        audit.record(db, action=audit.A_UPDATE, entity_type='purchase', entity_id=purchase.id, user=user,
                     after=evidence, details=f"Identified duplicate of posted invoice {old.id}: all {len(purchase.items)} names, batches, quantities, expiry dates, rates and MRPs agree")
    return changed


def _human_units(row):
    c = row.corrections or {}
    d = row.receipt_decision or {}
    # Legacy receipts predate decision snapshots, but explicit UOM edits/acceptance
    # still show who reviewed their conversion. Auto-prepared rows never vote.
    return not c.get("_automation") and bool(c.get("_invoice_unit") or c.get("units_per_pack")
                                              or "units_suggested" in c.get("_accepted", []))


def preserve_reviewed_pack_knowledge(db, *, user=None):
    """Retain verified conversion facts across an authorized purchase reset.

    This stores no invoices, quantities, prices or customer data. Contrary
    examples are retained too, so resetting documents cannot erase a conflict.
    """
    saved = json.loads(settings_service.get_setting(db, 'purchase_reviewed_pack_knowledge', '[]'))
    facts = {r['source']:r for r in saved}
    for row in db.scalars(select(PurchaseItem).join(Purchase).where(
        Purchase.status.in_(("POSTED","PARTIAL")), PurchaseItem.status=='POSTED'
    ).options(selectinload(PurchaseItem.purchase), selectinload(PurchaseItem.item))):
        item, d = row.item, row.receipt_decision or {}
        if not item or not row.purchase.supplier_id or not _human_units(row) or units.parse_pack(row.pack_size).kind != 'NESTED':
            continue
        if item.base_unit not in {'TABLET','CAPSULE'}:
            continue
        master = row.units_per_pack
        if d.get('resolved') and Decimal(d.get('master_pack_equivalent') or '0') > 0:
            master = Decimal(d['received_base_units'])/Decimal(d['master_pack_equivalent'])
        factor = d.get('units_per_invoice_unit') if d.get('resolved') else row.units_per_pack
        if not factor or master != item.units_per_pack:
            continue
        fact = dict(supplier_id=row.purchase.supplier_id, layout=layout(row.purchase), item_id=item.id,
                    base=item.base_unit, master=item.units_per_pack, factor=factor,
                    basis=d.get('mrp_basis','MASTER_PACK'), pack=row.pack_size)
        fact['source'] = 'verified:' + hashlib.sha256(json.dumps(fact,sort_keys=True).encode()).hexdigest()[:16]
        facts[fact['source']] = fact
    settings_service.set_setting(db,'purchase_reviewed_pack_knowledge',json.dumps(list(facts.values())),user=user)
    return len(facts)


def receipt_evidence(db, purchase, line):
    pack = units.parse_pack(line.pack_size)
    if re.search(r"[a-z]", str(rd.effective(line).get("quantity", "")), re.I):
        return None  # Explicit Qty unit labels take precedence over layout memory.
    if not purchase.supplier_id or pack.kind != "NESTED":
        return None
    master = line.item.units_per_pack if line.item else line.units_per_pack
    base = line.item.base_unit if line.item else line.base_unit
    reference_pack = ((line.corrections or {}).get('_automation') or {}).get('reference') or {}
    if line.item and not reference_pack:
        reference_pack = reference.packaging_evidence(line.product_name, line.manufacturer, line.pack_size) or {}
    retail_supported = (reference_pack.get('uom') or {}).get('units_per_pack') == master
    candidates = {pack.units_per_pack, pack.units_per_pack * (pack.outer_count or 1)}
    if base not in {"TABLET", "CAPSULE"} or not master or master not in candidates:
        return None
    if master != pack.units_per_pack and not retail_supported:
        return None
    if master == 1 and not retail_supported:
        # N×1 also occurs as a reversed retail count (ten tablets, not one).
        # A convention learned from N×10 cannot establish that retail definition.
        return None
    history = list(db.scalars(select(PurchaseItem).join(Purchase).where(
        Purchase.supplier_id == purchase.supplier_id, Purchase.status.in_(("POSTED", "PARTIAL")),
        PurchaseItem.status == "POSTED", Purchase.id != purchase.id
    ).options(selectinload(PurchaseItem.purchase), selectinload(PurchaseItem.item))))
    votes, conflicts, exact = [], [], []
    for old in history:
        old_pack = units.parse_pack(old.pack_size)
        if old_pack.kind != "NESTED" or not _human_units(old) or layout(old.purchase) != layout(purchase):
            continue
        item = old.item
        if not item or item.base_unit not in {"TABLET", "CAPSULE"}:
            continue
        d = old.receipt_decision or {}
        factor = d.get("units_per_invoice_unit") if d.get("resolved") else old.units_per_pack
        basis = d.get("mrp_basis", "MASTER_PACK")
        # Legacy posted units are useful only if the reviewed definition matches
        # the current immutable retail count. A changed master invalidates it.
        historical_master = old.units_per_pack
        if d.get("resolved") and Decimal(d.get("master_pack_equivalent") or "0") > 0:
            historical_master = Decimal(d["received_base_units"]) / Decimal(d["master_pack_equivalent"])
        if not factor or historical_master != item.units_per_pack:
            continue
        if factor != old_pack.units_per_pack or basis != "MASTER_PACK":
            conflicts.append(old.id)
            continue
        votes.append(old)
        if line.item and item.id == line.item.id and _pack_key(old.pack_size) == _pack_key(line.pack_size):
            exact.append(old)
    saved = json.loads(settings_service.get_setting(db, 'purchase_reviewed_pack_knowledge', '[]'))
    live_ids = {r.item_id for r in votes}
    for fact in saved:
        if fact['supplier_id'] != purchase.supplier_id or sorted(tuple(c) for c in fact['layout']) != layout(purchase):
            continue
        item = db.get(Item, fact['item_id'])
        if not item or item.deleted_at or item.units_per_pack != fact['master'] or item.base_unit != fact['base']:
            continue
        old_pack = units.parse_pack(fact['pack'])
        if fact['factor'] != old_pack.units_per_pack or fact['basis'] != 'MASTER_PACK':
            conflicts.append(fact['source'])
            continue
        saved_vote = SimpleNamespace(item_id=item.id, id=fact['source'])
        if item.id not in live_ids:
            votes.append(saved_vote)
            live_ids.add(item.id)
        if line.item and line.item.id == item.id and _pack_key(fact['pack']) == _pack_key(line.pack_size):
            exact.append(saved_vote)
    if conflicts:
        return None
    # A repeat product may reuse its reviewed conversion. A new product needs
    # independent, unanimous examples of this supplier's solid-dose convention.
    distinct = {r.item_id for r in votes}
    if not exact and len(distinct) < 3:
        return None
    sources = exact or votes
    return {"units_per_invoice_unit": master, "mrp_basis": "MASTER_PACK",
            "source": "REVIEWED_SUPPLIER_HISTORY" if exact else "SUPPLIER_LAYOUT_CONVENTION",
            "evidence": [f"Supplier {purchase.supplier_id}: reviewed posted lines {', '.join(str(r.id) for r in sources)} use the retail pack with this Qty/Rate/MRP layout; no conflicting reviewed examples",
                         f"Printed packaging hierarchy and {'catalogue' if retail_supported else 'inner'} retail definition agree: {master} {base.lower()}s"],
            "history_line_ids": [r.id for r in sources]}


def prepare_line(db, purchase, line):
    """Prepare identity and new product units; never overwrite explicit UOM edits."""
    if not active(db, purchase) or line.item is not None:
        return
    c = dict(line.corrections or {})
    if UOM_FIELDS.intersection(c):
        return
    # Reuse exact names with canonical typography only when manufacturer/form/
    # pack independently agree. Never ignore release markers or choose fuzzy hits.
    key = reference.normalize_name(line.product_name)
    found = [i for i in db.scalars(select(Item).where(Item.deleted_at.is_(None)))
             if reference.normalize_name(i.name) == key
             and _pack_key(i.pack_size) == _pack_key(line.pack_size)
             and (not line.manufacturer or reference.maker_compatible(line.manufacturer, i.manufacturer))]
    if len(found) == 1:
        line.item, line.match_method, line.new_product = found[0], "CANONICAL_NAME_PACK", False
        line.corrections = {**c, "_automation": {"version": VERSION, "action": "match", "item_id": found[0].id}}
        return
    pack = units.parse_pack(line.pack_size)
    ref = reference.packaging_evidence(line.product_name, line.manufacturer, line.pack_size)
    probe = Item(name=line.product_name, pack_size=line.pack_size or (ref["pack"] if ref else ""),
                 dosage_form=ref["uom"]["dosage_form"] if ref else "", generic_name="")
    uom = packaging_service.resolve(probe)
    if ref and pack.kind == 'NESTED':
        r = ref['uom']
        uom = packaging_service.UOM(r['base_unit'], r['pack_unit'], r['units_per_pack'], None, '',
                                   r['dosage_form'], f"Catalogue retail pack {ref['pack']!r} agrees with a level of the printed hierarchy {pack.raw!r}")
    if pack.strip and not pack.unit_hint and not packaging_service.detect_form(probe) and not ref:
        # "10S" establishes ten doses in a strip, but does not distinguish tablets
        # from capsules. Preserve the physical count without inventing a form.
        uom = packaging_service.UOM("UNIT", "STRIP", pack.units_per_pack, None, "", "",
                                   f"Printed strip contains {pack.units_per_pack} units; dosage form is unspecified")
    if pack.kind == "SINGLE" and pack.units_per_pack == 1 and uom is None:
        # An explicitly printed single piece does not need a clinical form guess.
        uom = packaging_service.UOM("UNIT", "UNIT", 1, None, "", "", "Invoice explicitly identifies one unit per pack")
    # Unknown counts must not silently become one PACK. Content is always whole
    # containers, and a literal single item is a valid one-unit definition.
    supported = bool(uom and (pack.confident or pack.kind == "NESTED" or (not pack.raw and ref)))
    if pack.kind == 'CONTENT' and uom and uom.dosage_form in {'TABLET','CAPSULE'}:
        supported = False  # 500 mg is a dose strength, not a count of tablets.
    if pack.kind in {"COUNT", "NESTED"} and uom and uom.base_unit in {"PACK", "UNIT"} and uom.pack_unit != "STRIP":
        supported = False
    if not supported:
        if c.get("_automation"):
            line.new_product, line.units_per_pack = False, None
            c.pop("_automation", None)
            line.corrections = c
        return
    line.new_product = True
    line.base_unit, line.pack_unit, line.units_per_pack, line.dosage_form = (
        uom.base_unit, uom.pack_unit, uom.units_per_pack, uom.dosage_form)
    c.pop("_units_suggested", None)
    c["_automation"] = {"version": VERSION, "action": "new_product", "reason": uom.reason,
                        "reference": ref, "source_pack": line.pack_size}
    line.corrections = c


def review_issues(db, purchase, line, issues):
    if not active(db, purchase):
        return issues
    c = line.corrections or {}
    if purchase.supplier_id and line.supplier_code and line.match_method != "MANUAL":
        remembered = db.scalar(select(SupplierProductMap).where(
            SupplierProductMap.supplier_id == purchase.supplier_id,
            SupplierProductMap.supplier_code == line.supplier_code))
        if remembered and remembered.description_raw and reference.normalize_name(remembered.description_raw) != reference.normalize_name(line.product_name):
            issues.append({"code": "supplier_code_changed", "field": "name", "level": "review",
                           "message": f"Supplier code {line.supplier_code} previously meant {remembered.description_raw!r}. The new description changes identity; verify the product once before reusing its mapping."})
    # Detect the older fallback definition too, rather than inflating coverage
    # by calling an unidentified ten-dose pack one inventory unit.
    if not line.item and not UOM_FIELDS.intersection(c):
        info = units.parse_pack(line.pack_size)
        form = packaging_service.detect_form(Item(name=line.product_name, generic_name='', dosage_form=line.dosage_form or ''))
        if info.kind == 'CONTENT' and form in {'TABLET','CAPSULE'}:
            issues.append({"code": "strength_not_pack", "field": "pack", "level": "review",
                           "message": "This field describes medicine strength, not the number of tablets or capsules in a pack."})
        if info.kind in {"COUNT", "NESTED"} and line.base_unit in {"", "PACK", "UNIT"} and not (info.strip and line.pack_unit == "STRIP"):
            issues.append({"code": "product_unit_unknown", "field": "base_unit", "level": "review",
                           "message": "The invoice gives a count but does not establish the sale unit. Supply the product's form or a verified supplier mapping once; subsequent imports reuse it."})
    if (line.receipt_decision or {}).get("resolved"):
        issues = [i for i in issues if i["code"] != "units_suggested"]
    # Broad four-digit tariff headings are not product-level tax identity.
    for i in issues:
        if i["code"] == "gst_hsn_mixed" and len(line.hsn_code) == 4 and line.gst_source == "FILE":
            i["level"] = "info"
            i["message"] = f"Broad HSN heading {line.hsn_code} contains different printed GST rates; individual rates and invoice tax totals are still checked."
        if i["code"] == "expiry_soon" and line.expiry_date:
            expiry = line.expiry_date
            last_day = expiry.replace(day=calendar.monthrange(expiry.year, expiry.month)[1])
            minimum = max(0, settings_service.get_int(db, "purchase_min_shelf_days", 30))
            if (last_day - date.today()).days >= minimum:
                i["level"] = "info"
                i["message"] += f"; meets the configured {minimum}-day minimum remaining shelf life. Prioritize this batch for sale."
    return issues


def assessment(db, purchase):
    from app.services import purchasing
    s = purchasing.summary(purchase)
    blockers = []
    if not purchase.supplier_id:
        blockers.append({"code": "NO_SUPPLIER", "message": "Supplier identity is missing"})
    seller = (purchase.charges or {}).get("_supplier_evidence") or {}
    if seller.get("gstin") and purchase.supplier and purchase.supplier.gst_number and seller["gstin"].upper() != purchase.supplier.gst_number.upper():
        blockers.append({"code": "SUPPLIER_CONFLICT", "message": "Printed supplier GSTIN differs from the selected supplier"})
    if not purchase.invoice_no:
        blockers.append({"code": "NO_INVOICE_NO", "message": "Supplier invoice number is missing"})
    if not purchase.invoice_date:
        blockers.append({"code": "NO_INVOICE_DATE", "message": "Invoice date is missing"})
    if s["difference"] is None or abs(Decimal(s["difference"])) > Decimal("0.01"):
        blockers.append({"code": "TOTAL_UNVERIFIED", "message": "A printed invoice total must reconcile to within ₹0.01"})
    # PDF row counts/reader names are informational. Explicit extraction failures
    # and identity/mapping warnings remain a document-level hold.
    notes = purchase.extraction_meta or ""
    benign = r"Read \d+ product rows across \d+ pages; page counts: [\d, ]+; PDF table read by \w+ reading"
    substantive = re.sub(benign, "", notes).strip("; ")
    duplicate = (purchase.charges or {}).get('_duplicate_evidence') or {}
    if duplicate.get('fingerprint') == invoice_fingerprint(purchase):
        substantive = re.sub(r"Supplier on the invoice:.*?— not in the supplier master yet \(.*?\)", "", substantive).strip('; ')
    if substantive or (purchase.charges or {}).get("_extraction_issues"):
        blockers.append({"code": "SOURCE_WARNING", "message": substantive or "Extraction controls failed"})
    tax = purchasing.gst_summary(db, purchase)
    if tax["problems"]:
        blockers.append({"code": "TAX_TOTAL_UNVERIFIED", "message": "; ".join(tax["problems"])})
    printed_tax = purchasing._dec((purchase.charges or {}).get("printed_gst"))
    if printed_tax is not None and abs(printed_tax - Decimal(s["totals"]["gst"])) > Decimal("0.02"):
        blockers.append({"code": "TAX_TOTAL_DIFFERENCE", "message": "Printed tax total differs from calculated tax by more than ₹0.02"})
    last_error = (purchase.charges or {}).get("_auto_post_error")
    if last_error:
        blockers.append(last_error)
    dup = db.scalar(select(Purchase.id).where(Purchase.supplier_id == purchase.supplier_id,
                   Purchase.status.in_(("POSTED", "PARTIAL")), Purchase.id != purchase.id,
                   func.lower(Purchase.invoice_no) == purchase.invoice_no.lower())) if purchase.supplier_id else None
    if dup:
        blockers.append({"code": "DUPLICATE_INVOICE", "purchase_id": dup, "message": "Supplier invoice already received"})
    exceptions = []
    for l in purchase.items:
        if l.status in purchasing.DONE:
            continue
        codes = [i["code"] for i in (l.issues or []) if i["level"] in {"block", "review", "warn", "match"} and not i.get("accepted")]
        if l.status not in purchasing.POSTABLE:
            codes.append("line_not_ready")
        if not l.batch_no:
            codes.append("batch_missing")
        if not l.expiry_date:
            codes.append("expiry_missing")
        if not (l.receipt_decision or {}).get("resolved"):
            codes.append("conversion_unresolved")
        values = rd.effective(l)
        rate, mrp = purchasing._dec(values.get("rate")), purchasing._dec(values.get("mrp"))
        if rate is None:
            codes.append("rate_missing")
        if mrp is None or mrp <= 0:
            codes.append("mrp_missing")
        if l.gst_rate is None and printed_tax != 0:
            codes.append("gst_unverified")
        amount = purchasing._dec(values.get("amount"))
        if amount is not None and rate is not None:
            try:
                paid, _ = rd.quantities(values)
                gross = money(paid * rate)
                net = money(l.line_total)
                taxed = money(net * (1 + (l.gst_rate or Decimal(0)) / 100))
                if min(abs(money(amount) - n) for n in (gross, net, taxed)) > Decimal("0.02"):
                    codes.append("line_amount_unreconciled")
            except ValueError:
                pass  # quantity validator already supplies the precise exception
        if codes:
            exceptions.append({"line": l.line_no, "name": l.product_name, "codes": sorted(set(codes))})
    n = len(purchase.items)
    return {"version": VERSION, "mode": settings_service.get_setting(db, "purchase_automation", "off"),
            "ready_for_unattended_post": bool(n and not exceptions and not blockers and purchase.status == "DRAFT"),
            "resolved_rows": n-len(exceptions), "total_rows": n,
            "coverage_percent": round(100*(n-len(exceptions))/n, 2) if n else 0,
            "exceptions": exceptions, "document_blockers": blockers,
            "exception_counts": dict(Counter(c for e in exceptions for c in e["codes"]))}


def prepare(db, purchase, *, user=None):
    from app.services import purchasing
    if (purchase.charges or {}).get("_auto_post_error"):
        purchase.charges = {k:v for k,v in purchase.charges.items() if k != "_auto_post_error"}
    for line in purchase.items:
        purchasing.refresh_line(db, purchase, line)
    if enabled(db) and identify_posted_copy(db, purchase, user=user):
        for line in purchase.items:
            purchasing.refresh_line(db, purchase, line)
    purchasing._refresh_totals(purchase)
    return assessment(db, purchase)


def import_file(db, filename, content, *, user=None, **kwargs):
    from app import audit
    from app.permissions import has_permission
    from app.services import purchasing
    # A retry returns the original document; it never replays stock movements.
    try:
        drafts = purchasing.import_file(db, filename, content, user=user, **kwargs)
    except purchasing.PurchaseError as exc:
        if exc.code != "DUPLICATE_FILE" or not enabled(db):
            raise
        existing = db.get(Purchase, exc.details["purchase_id"])
        drafts = list(db.scalars(select(Purchase).where(Purchase.source_sha256 == existing.source_sha256,
                                                       Purchase.status != "CANCELLED").order_by(Purchase.id)))
    for purchase in drafts:
        if not enabled(db) or purchase.status != "DRAFT":
            continue
        report = prepare(db, purchase, user=user)
        if report["ready_for_unattended_post"] and report["mode"] == "post" and has_permission(user, "purchase.post"):
            try:
                with db.begin_nested():
                    purchasing.post(db, purchase, user=user)
            except purchasing.PurchaseError as exc:
                error = {"code": exc.code, "message": str(exc)}
                report["document_blockers"].append(error)
                report["ready_for_unattended_post"] = False
                purchase.charges = {**(purchase.charges or {}), "_auto_post_error": error}
        purchase.charges = {**(purchase.charges or {}), "_automation": report}
        audit.record(db, action=audit.A_UPDATE, entity_type="purchase_automation", entity_id=purchase.id,
                     user=user, after=report, details=f"Automatic intake: {report['resolved_rows']}/{report['total_rows']} rows resolved; {purchase.status}")
    return drafts
