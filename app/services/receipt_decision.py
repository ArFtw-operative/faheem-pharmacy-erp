"""Evidence-based invoice units. No network, LLM, or supplier-name branches.

Financial quantities may be fractional; physical base units must be integral.
An invoice's billing unit is independent of the product master's retail pack.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

from sqlalchemy import select
from app.models import SupplierProductMap
from app.services import units

VERSION = 1
_NUMBER = r"(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?"


def quantity(value, *, combined=True):
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal(0), Decimal(0)
    text = re.sub(r"\s+(?:nos?\.?|pcs?\.?|packs?|strips?|boxes)\s*$", "", text, flags=re.I)
    pattern = rf"({_NUMBER})(?:\s*\+\s*({_NUMBER}))?" if combined else rf"({_NUMBER})"
    match = re.fullmatch(pattern, text)
    if not match:
        raise ValueError("Use a non-negative quantity such as 3, 2.5 or 2.5+0.5; no extra text")
    parts = [Decimal(v.replace(",", "")) if v else Decimal(0) for v in match.groups()]
    if any(v > 10000000 or v.as_tuple().exponent < -6 for v in parts):
        raise ValueError("Quantity exceeds the supported range or six decimal places")
    return parts[0], parts[1] if len(parts) > 1 else Decimal(0)


def quantities(values):
    paid, embedded = quantity(values.get("quantity"))
    separate, _ = quantity(values.get("free"), combined=False)
    if embedded and separate:
        raise ValueError("Free quantity appears inside Qty and in Free; confirm it once to prevent double counting")
    return paid, embedded or separate


def effective(line):
    values = dict(line.raw or {})
    values.update({k: v["value"] for k, v in (line.corrections or {}).items()
                   if not k.startswith("_") and isinstance(v, dict) and "value" in v})
    return values


def definition(line):
    staged=(line.corrections or {}).get('_physical_adjustment')
    if staged and line.item and staged.get('item_id')==line.item.id:
        return staged['definition']
    obj=line.item or line
    return {k:getattr(obj,k) for k in ('base_unit','pack_unit','units_per_pack','dosage_form')}


def signature(purchase, line):
    """Changing identity, raw pack, layout, or master conversion invalidates memory."""
    v = effective(line)
    item = line.item
    identity = [purchase.supplier_id, line.supplier_code, v.get("name", "").strip().casefold(),
                v.get("pack", "").strip().upper(), v.get("manufacturer", "").strip().casefold(),
                [{k: c.get(k) for k in ("field", "column", "level")} for c in (purchase.column_map or [])], item.id if item else None,
                item.units_per_pack if item else line.units_per_pack,
                item.base_unit if item else line.base_unit]
    annotation = re.sub(r"[\d.,+\s]", "", str(v.get("quantity", ""))).casefold()
    if annotation:
        identity.append(annotation)
    current = {k:getattr(item,k) for k in ('base_unit','pack_unit','units_per_pack','dosage_form')} if item else None
    if (line.corrections or {}).get('_physical_adjustment') and definition(line) != current:
        identity.append(definition(line))
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def decision_scope(purchase, line):
    # Signature includes stable layout evidence, not the particular quantity/batch.
    return signature(purchase, line)


def mapping(db, purchase, line):
    if not purchase.supplier_id or line.item is None:
        return None
    from app.services.purchasing import description_key
    code = line.supplier_code or ""
    key = "" if code else description_key(line.description_raw or line.product_name)
    return db.scalar(select(SupplierProductMap).where(
        SupplierProductMap.supplier_id == purchase.supplier_id,
        SupplierProductMap.supplier_code == code, SupplierProductMap.description_key == key,
        SupplierProductMap.item_id == line.item.id))


def master_problems(line):
    """Independent physical evidence must corroborate an existing master too."""
    from app.models import Item
    from app.services import packaging_service
    item = line.item
    if item is None:
        return []
    pack = units.parse_pack(effective(line).get('pack'))
    form = packaging_service.detect_form(Item(name=line.product_name, generic_name='', dosage_form=''))
    base, master = item.base_unit, item.units_per_pack or 1
    problems = []
    def add(code, field, message):
        problems.append(dict(code=code, field=field, level='review', message=message))
    if pack.kind == 'CONTENT' and form in {'TABLET','CAPSULE'}:
        add('strength_not_pack', 'pack', 'The printed weight/volume does not establish the count of solid doses, even though the catalogue repeats it. Verify the physical pack.')
    if pack.kind in {'COUNT','NESTED'} and base in {'PACK','UNIT'} and master == 1 and (pack.units_per_pack or 1) > 1:
        add('master_unit_unverified', 'units_per_pack', f"Printed pack {pack.raw!r} contains a count, but the catalogue records one generic {base.lower()}. Establish the sale unit and retail count once.")
    if form in {'TABLET','CAPSULE'} and base not in {form, 'UNIT'}:
        add('master_form_conflict', 'base_unit', f'The description identifies {form.lower()}s, but the catalogue counts {base.lower()}s. Verify the product definition.')
    tube_label = re.search(r'\bTUBES?\b', line.product_name, re.I)
    if pack.kind == 'CONTENT' and base == 'TUBE' and form in {'','POWDER','SACHET'} and not tube_label:
        add('master_container_unverified', 'base_unit', 'Weight alone does not establish a tube; the description and catalogue do not corroborate this container definition.')
    return problems


def decide(line, confirmation=None, *, validate_master=False):
    v = effective(line)
    issues, evidence = [], []
    out = {"version": VERSION, "resolved": False, "issues": issues, "evidence": evidence,
           "paid": None, "free": None, "received_base_units": None,
           "invoice_units": None, "units_per_invoice_unit": None, "master_pack_equivalent": None,
           "base_unit": definition(line)['base_unit'] or 'UNIT',
           "source": "UNRESOLVED", "candidates": []}

    def issue(code, message, field="pack"):
        issues.append({"code": code, "field": field, "level": "review", "message": message})

    try:
        paid, free = quantities(v)
    except ValueError as exc:
        issue("quantity_invalid", str(exc), "quantity")
        return out
    total = paid + free
    out.update(paid=str(paid), free=str(free), invoice_units=str(total))
    if total <= 0:
        issue("quantity_invalid", "Paid plus free quantity must be greater than zero", "quantity")
        return out
    item = line.item
    master = definition(line)['units_per_pack']
    pack = units.parse_pack(v.get("pack"))
    if validate_master and not (confirmation and confirmation.get('source') in {'CONFIRMED','SUPPLIER_MEMORY'}):
        problems = master_problems(line)
        if problems:
            issues.extend(problems)
            return out
    if item and pack.kind == "CONTENT":
        master_info = units.parse_pack(item.pack_size)
        scales = {"ML": ("ML", 1), "L": ("ML", 1000), "G": ("G", 1), "KG": ("G", 1000)}
        if master_info.kind == "CONTENT" and pack.content_unit in scales and master_info.content_unit in scales:
            a, b = scales[pack.content_unit], scales[master_info.content_unit]
            if (a[0], pack.content_qty * a[1]) != (b[0], master_info.content_qty * b[1]):
                issue("content_conflict", f"Invoice content {pack.raw!r} differs from product content {item.pack_size!r}. Select the correct size variant.")
                return out
    factor = None
    basis = "MASTER_PACK"
    if confirmation:
        factor = confirmation["units_per_invoice_unit"]
        basis = confirmation["mrp_basis"]
        out["source"] = confirmation.get("source", "CONFIRMED")
        evidence.extend(confirmation.get("evidence") or ["Invoice unit confirmed by an operator for this product and source pack"])
    elif master:
        annotated = re.search(r"\s+(nos?\.?|pcs?\.?|packs?|strips?|boxes)\s*$", str(v.get("quantity", "")), re.I)
        if annotated:
            unit = annotated.group(1).lower().rstrip(".")
            expected = 1 if unit in {"no", "nos", "pc", "pcs"} else master if unit in {"pack", "packs", "strip", "strips"} else None
            if expected != master or unit.startswith("box"):
                issue("quantity_unit_label", "The Qty column names a unit that differs from the product pack. Verify the invoice-unit conversion and MRP basis.", "units_per_invoice_unit")
                return out
        # A nested printed carton never establishes the transaction level by itself.
        if pack.kind == "NESTED" or (pack.kind == "CONTENT" and (pack.outer_count or 1) > 1):
            inner = pack.units_per_pack if pack.kind == "NESTED" else 1
            outer = inner * (pack.outer_count or 1)
            out["candidates"] = [{"units_per_invoice_unit": n} for n in sorted({1, inner, outer, master})]
            issue("invoice_unit_ambiguous", f"Pack {pack.raw!r} may describe inner packs or a carton. Confirm the number of {out['base_unit'].lower()}s counted by one invoice Qty.", "units_per_invoice_unit")
        elif item and pack.confident:
            from app.services import packaging_service
            from app.models import Item
            probe = Item(name=line.product_name, pack_size=pack.raw, dosage_form=item.dosage_form,
                         generic_name="", base_unit="UNIT", pack_unit="PACK", units_per_pack=1)
            parsed = packaging_service.resolve(probe)
            expected = parsed.units_per_pack if parsed else pack.units_per_pack
            if expected != master:
                issue("pack_conflict", f"Invoice pack implies {expected} sale units; product master says {master}. Confirm the invoice unit or select the correct product.", "units_per_invoice_unit")
            else:
                factor = master
                out["source"] = "PACK_AND_MASTER"
                evidence.append(f"Printed pack and matched product agree on {master} base units")
        elif pack.raw and pack.kind == "UNKNOWN":
            issue("pack_unreadable", f"Pack {pack.raw!r} is not understood. Confirm its physical conversion.", "units_per_invoice_unit")
        else:
            factor = master
            out["source"] = "PRODUCT_MASTER" if item else "NEW_PRODUCT_DEFINITION"
            evidence.append(f"Uses {'matched' if item else 'confirmed new'} product definition: {master} base units per pack")
    else:
        issue("conversion_missing", "Match a product or confirm its packaging before computing inventory", "units_per_pack")
    if factor is None:
        return out
    if not master:
        issue("conversion_missing", "Define the product's retail pack before confirming the invoice unit", "units_per_pack")
        return out
    physical = total * factor
    out["units_per_invoice_unit"] = factor
    out["mrp_basis"] = basis
    if physical % 1:
        issue("physical_fraction", f"{total} × {factor} = {physical} {out['base_unit'].lower()}s. Physical stock must be whole units; verify the pack and quantities.", "quantity")
        return out
    if physical > 2147483647:
        issue("quantity_invalid", "Physical stock exceeds the supported integer range", "quantity")
        return out
    paid_base, free_base = paid * factor, free * factor
    # 2.5+0.5 of 15 = 45 tablets; 37.5/7.5 are accounting equivalents, not half tablets.
    split = bool(paid_base % 1 or free_base % 1)
    out.update(resolved=True, received_base_units=int(physical),
               paid_base_equivalent=str(paid_base), free_base_equivalent=str(free_base),
               ledger_paid_units=int(physical) if split else int(paid_base),
               ledger_free_units=0 if split else int(free_base),
               financial_split_only=split, master_pack_equivalent=str(physical / master))
    evidence.append(f"({paid} billed + {free} free) × {factor} = {int(physical)} {out['base_unit'].lower()}s")
    if split:
        evidence.append("Paid/free fractions are financial allocations; the ledger receives whole physical units")
    return out


def resolve(db, purchase, line):
    scope = decision_scope(purchase, line)
    confirmed = (line.corrections or {}).get("_invoice_unit")
    if confirmed and confirmed.get("scope") != scope:
        confirmed = None
    if confirmed is None:
        learned = mapping(db, purchase, line)
        entry = (learned.receipt_conventions or {}).get(scope) if learned else None
        if entry:
            confirmed = {**entry, "source": "SUPPLIER_MEMORY"}
    if confirmed is None:
        from app.services import purchase_automation
        if purchase_automation.enabled(db):
            confirmed = purchase_automation.receipt_evidence(db, purchase, line)
    from app.services import purchase_automation
    staged=(line.corrections or {}).get('_physical_adjustment')
    if staged and line.item:
        from app.services.purchase_adjustment import snapshot
        if snapshot(line.item) not in (staged['before'],staged['definition']):
            confirmed=None
            result=decide(line,validate_master=True)
            result['resolved']=False
            result['issues'].append(dict(code='packaging_changed',field='pack',level='review',message='Product packaging changed after adjustment; recheck this line.'))
        else:
            result=decide(line,confirmed,validate_master=purchase_automation.active(db,purchase))
    else:
        result = decide(line, confirmed, validate_master=purchase_automation.active(db, purchase))
    if confirmed and confirmed.get("history_line_ids"):
        result["history_line_ids"] = confirmed["history_line_ids"]
    if not line.pack_size:
        from app.services.medicine_reference import unique_pack_evidence
        result["reference_evidence"] = unique_pack_evidence(line.product_name, line.manufacturer)
    result["scope"] = scope
    line.receipt_decision = result
    return result


def confirm(db, purchase, line, *, factor, mrp_basis, reason, user=None):
    from app.services import purchasing
    from app import audit
    from app.utils import utcnow
    purchasing._open(purchase)
    purchasing._open_line(line)
    if isinstance(factor, bool) or len(str(factor)) > 7 or not str(factor).isdigit() or not 1 <= int(factor) <= 1000000:
        raise purchasing.PurchaseError("Units per invoice Qty must be a whole number from 1 to 1000000")
    if not isinstance(mrp_basis, str) or mrp_basis not in {"MASTER_PACK", "INVOICE_UNIT", "BASE"}:
        raise purchasing.PurchaseError("MRP basis must be MASTER_PACK, INVOICE_UNIT or BASE")
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise purchasing.PurchaseError("Describe how the conversion was verified (at least five characters)")
    if not line.item and not (line.new_product and line.units_per_pack):
        raise purchasing.PurchaseError("Match a product or define the new product first")
    stamp = {"units_per_invoice_unit": int(factor), "mrp_basis": mrp_basis,
             "scope": decision_scope(purchase, line), "reason": reason.strip()[:500],
             "by": purchasing._actor(user), "at": utcnow().isoformat(), "source": "CONFIRMED"}
    before = (line.corrections or {}).get("_invoice_unit")
    line.corrections = {**(line.corrections or {}), "_invoice_unit": stamp}
    purchasing.refresh_line(db, purchase, line)
    purchasing._refresh_totals(purchase)
    audit.record(db, action=audit.A_UPDATE, entity_type="purchase_line", entity_id=line.id, user=user,
                 before={"invoice_unit": before}, after={"invoice_unit": stamp}, details="Invoice unit verified")
    return line


def mrp_per_master_pack(line):
    d = line.receipt_decision or {}
    master = definition(line)['units_per_pack']
    basis = d.get("mrp_basis", "MASTER_PACK")
    if basis == "BASE":
        return line.mrp * master
    if basis == "INVOICE_UNIT":
        return line.mrp * master / d["units_per_invoice_unit"]
    return line.mrp


def remember(db, purchase, line):
    stamp = (line.corrections or {}).get("_invoice_unit")
    if not stamp or (line.receipt_decision or {}).get("scope") != stamp.get("scope"):
        return
    m = mapping(db, purchase, line)
    if m:
        # New-product confirmation had no id before posting. Re-key after creation.
        scope = decision_scope(purchase, line)
        m.receipt_conventions = {**(m.receipt_conventions or {}), scope: {**stamp, "scope": scope}}
