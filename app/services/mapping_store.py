"""Mapping store: what people taught the system, so the same work never comes back.

* Product aliases (``supplier_product_maps``): supplier code / description → our product, with a
  trust level. A person overruling an alias marks it AMBIGUOUS: it is then only suggested,
  never applied, until someone confirms the new product again.
* Packaging aliases (``supplier_packaging_aliases``): supplier + printed pack (+ product) →
  how many base units one invoice Qty counts.
* Column roles live on the supplier (``column_profile``); each change is recorded here.
* Every change is written to ``mapping_history`` so a bad mapping can be traced and undone.

Bootstrap reads past posted purchases and creates aliases only where the supplier text maps
to exactly one product; ambiguous pairs are listed in the report, never applied.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (Item, MappingHistory, Purchase, PurchaseItem, SupplierPackagingAlias, SupplierProductMap, User)
from app.services import packaging_parser as pp
from app.utils import utcnow

ACTIVE, AMBIGUOUS = "ACTIVE", "AMBIGUOUS"
TRUST_BOOTSTRAP = Decimal("0.950")
TRUST_CONFIRMED = Decimal("1.000")
TRUST_OVERRULED = Decimal("0.500")


def history(db: Session, kind: str, action: str, *, supplier_id=None, key="", before=None, after=None,
            purchase_id=None, line_id=None, user: User | None = None) -> None:
    db.add(MappingHistory(kind=kind, action=action, supplier_id=supplier_id, key=str(key)[:250], before=before, after=after,
                          purchase_id=purchase_id, line_id=line_id, user_id=user.id if user else None))
    db.flush()


def usable(m: SupplierProductMap | None) -> bool:
    """An alias is applied automatically only while nobody has overruled it."""
    return m is not None and (m.status or ACTIVE) == ACTIVE and m.item is not None and m.item.deleted_at is None


# --------------------------------------------------------------------------- product aliases
def confirm_product(db: Session, m: SupplierProductMap, item_id: int, *, manual: bool, purchase=None, line=None,
                    user: User | None = None) -> SupplierProductMap:
    """A posted line used ``m``. A person choosing a different product overrules it."""
    before = {"item_id": m.item_id, "status": m.status, "trust": str(m.trust) if m.trust is not None else None}
    if m.item_id != item_id and manual:
        m.corrections = (m.corrections or 0) + 1
        m.item_id, m.receipt_conventions = item_id, None
        m.status, m.trust = AMBIGUOUS, TRUST_OVERRULED
        m.confirmed_by, m.confirmed_at = user.id if user else None, utcnow()
        action = "OVERRULE"
    elif m.item_id == item_id:
        if (m.status or ACTIVE) == AMBIGUOUS and manual:
            m.status, m.trust = ACTIVE, Decimal("0.900")          # confirmed again after an overrule
        else:
            m.status = m.status or ACTIVE
            m.trust = min(TRUST_CONFIRMED, (m.trust if m.trust is not None else TRUST_BOOTSTRAP) + Decimal("0.010"))
        action = "CONFIRM"
    else:
        return m                                                    # an automatic match never rewrites an alias
    m.last_used_at = utcnow()
    history(db, "PRODUCT_ALIAS", action, supplier_id=m.supplier_id, key=m.supplier_code or m.description_key,
            before=before, after={"item_id": m.item_id, "status": m.status, "trust": str(m.trust)},
            purchase_id=purchase.id if purchase else None, line_id=line.id if line else None, user=user)
    return m


def disable(db: Session, m: SupplierProductMap, *, user: User | None = None) -> None:
    history(db, "PRODUCT_ALIAS", "DISABLE", supplier_id=m.supplier_id, key=m.supplier_code or m.description_key,
            before={"item_id": m.item_id, "description": m.description_raw}, user=user)


# --------------------------------------------------------------------------- packaging aliases
def packaging_alias(db: Session, supplier_id: int | None, pack: str, item: Item | None) -> SupplierPackagingAlias | None:
    """The active alias for this supplier's printed pack and product, still valid for the product's
    current retail pack. A product whose pack definition changed needs fresh confirmation."""
    if not supplier_id or item is None or not (pack or "").strip():
        return None
    row = db.scalar(select(SupplierPackagingAlias).where(
        SupplierPackagingAlias.supplier_id == supplier_id, SupplierPackagingAlias.pack_key == pp.pack_key(pack),
        SupplierPackagingAlias.item_id == item.id))
    if row is None or row.status != ACTIVE:
        return None
    if row.retail_units != (item.units_per_pack or 1) or (row.base_unit and row.base_unit != item.base_unit):
        return None
    return row


def save_packaging_alias(db: Session, *, supplier_id: int, pack: str, item: Item, units_per_invoice_unit: int,
                         mrp_basis: str, source: str, purchase=None, line=None, user: User | None = None) -> SupplierPackagingAlias:
    key = pp.pack_key(pack)
    row = db.scalar(select(SupplierPackagingAlias).where(SupplierPackagingAlias.supplier_id == supplier_id,
                                                         SupplierPackagingAlias.pack_key == key,
                                                         SupplierPackagingAlias.item_id == item.id))
    before = None
    if row is None:
        row = SupplierPackagingAlias(supplier_id=supplier_id, pack_key=key, item_id=item.id, occurrences=0, corrections=0,
                                     created_by=user.id if user else None)
        db.add(row)
        action = "CREATE"
    else:
        before = {"units_per_invoice_unit": row.units_per_invoice_unit, "retail_units": row.retail_units, "status": row.status}
        changed = row.units_per_invoice_unit != units_per_invoice_unit or row.retail_units != (item.units_per_pack or 1)
        action = "OVERRULE" if changed else "CONFIRM"
        if changed:
            row.corrections = (row.corrections or 0) + 1
    row.raw_pack = (pack or "")[:60]
    row.base_unit = item.base_unit
    row.units_per_invoice_unit = int(units_per_invoice_unit)
    row.retail_units = item.units_per_pack or 1
    row.mrp_basis = mrp_basis
    row.occurrences = (row.occurrences or 0) + 1
    row.status = ACTIVE
    row.trust = TRUST_CONFIRMED if action != "OVERRULE" else Decimal("0.900")
    row.source = source
    db.flush()
    history(db, "PACKAGING_ALIAS", action, supplier_id=supplier_id, key=f"{key}|{item.id}", before=before,
            after={"units_per_invoice_unit": row.units_per_invoice_unit, "retail_units": row.retail_units,
                   "mrp_basis": mrp_basis, "source": source},
            purchase_id=purchase.id if purchase else None, line_id=line.id if line else None, user=user)
    return row


def remember_posted_line(db: Session, purchase: Purchase, line: PurchaseItem, *, user: User | None = None) -> None:
    """After posting: a conversion a person confirmed becomes a packaging alias and a product pack."""
    stamp = (line.corrections or {}).get("_invoice_unit") or {}
    decision = line.receipt_decision or {}
    if not purchase.supplier_id or line.item is None or not decision.get("resolved") or not line.pack_size:
        return
    if stamp.get("source", "CONFIRMED") != "CONFIRMED" or stamp.get("operator_counts") or not stamp or stamp.get("invoice_only"):
        return                                     # counts for one delivery / "this invoice only" are not a convention
    factor = decision.get("units_per_invoice_unit")
    if not isinstance(factor, int) or factor < 1:
        return
    save_packaging_alias(db, supplier_id=purchase.supplier_id, pack=line.pack_size, item=line.item,
                         units_per_invoice_unit=factor, mrp_basis=decision.get("mrp_basis") or "MASTER_PACK",
                         source="USER_CORRECTION", purchase=purchase, line=line, user=user)
    from app.services import packaging_store

    parsed = pp.parse(line.pack_size, master_form=line.item.dosage_form, product_name=line.item.name)
    upp = line.item.units_per_pack or 1
    packaging_store.upsert(db, line.item, parsed, source="USER_CORRECTION", verified=True,
                           purchase_to_retail=max(factor // upp, 1) if factor % upp == 0 else 1, retail_to_base=upp,
                           retail_unit=line.item.pack_unit, base_unit=line.item.base_unit,
                           purchase_unit="BOX" if factor > upp else line.item.pack_unit, user=user)


# --------------------------------------------------------------------------- bootstrap
def bootstrap(db: Session) -> dict:
    """Seed the store from history. Idempotent; never changes a mapping a person set."""
    from app.services.purchasing import description_key

    pairs: dict[tuple, set[int]] = defaultdict(set)
    names: dict[tuple, str] = {}
    rows = db.scalars(select(PurchaseItem).join(Purchase).where(
        Purchase.supplier_id.is_not(None), PurchaseItem.status == "POSTED", PurchaseItem.item_id.is_not(None))
        .options(selectinload(PurchaseItem.purchase)))
    for line in rows:
        code = line.supplier_code or ""
        key = (line.purchase.supplier_id, code, "" if code else description_key(line.description_raw or line.product_name))
        pairs[key].add(line.item_id)
        names[key] = line.description_raw or line.product_name
    created, confirmed, ambiguous = 0, 0, []
    existing = {(m.supplier_id, m.supplier_code, m.description_key): m for m in db.scalars(select(SupplierProductMap))}
    for key, items in pairs.items():
        m = existing.get(key)
        if len(items) > 1:
            ambiguous.append({"supplier_id": key[0], "code": key[1], "description": names[key], "items": sorted(items)})
            if m is not None and m.status is None:
                m.status, m.trust = AMBIGUOUS, TRUST_OVERRULED
                history(db, "PRODUCT_ALIAS", "BOOTSTRAP", supplier_id=key[0], key=key[1] or key[2],
                        after={"status": AMBIGUOUS, "items": sorted(items)})
            continue
        (item_id,) = items
        if m is None:
            db.add(SupplierProductMap(supplier_id=key[0], supplier_code=key[1], description_key=key[2][:250],
                                      description_raw=names[key][:250], item_id=item_id, uses=1, trust=TRUST_BOOTSTRAP,
                                      status=ACTIVE, source="BOOTSTRAP"))
            history(db, "PRODUCT_ALIAS", "BOOTSTRAP", supplier_id=key[0], key=key[1] or key[2], after={"item_id": item_id})
            created += 1
        elif m.status is None:
            if m.item_id == item_id:
                m.status, m.trust, m.source = ACTIVE, TRUST_BOOTSTRAP, m.source or "POSTED"
                confirmed += 1
            else:
                m.status, m.trust = AMBIGUOUS, TRUST_OVERRULED
                ambiguous.append({"supplier_id": key[0], "code": key[1], "description": names[key], "items": [m.item_id, item_id]})
    # aliases with no posted history (confirmed matches): trusted as people confirmed them
    for m in existing.values():
        if m.status is None:
            m.status, m.trust, m.source = ACTIVE, TRUST_BOOTSTRAP, m.source or "CONFIRMED"
    db.flush()
    packs = _bootstrap_packaging_aliases(db)
    return {"created": created, "confirmed": confirmed, "ambiguous": ambiguous, "packaging_aliases": packs}


def _bootstrap_packaging_aliases(db: Session) -> int:
    """Conversions people verified on posted lines (receipt_conventions) become packaging aliases."""
    made = 0
    rows = db.scalars(select(PurchaseItem).join(Purchase).where(
        Purchase.supplier_id.is_not(None), PurchaseItem.status == "POSTED", PurchaseItem.item_id.is_not(None))
        .options(selectinload(PurchaseItem.purchase), selectinload(PurchaseItem.item)))
    for line in rows:
        stamp = (line.corrections or {}).get("_invoice_unit") or {}
        d = line.receipt_decision or {}
        if not stamp or stamp.get("operator_counts") or stamp.get("source", "CONFIRMED") != "CONFIRMED" or not d.get("resolved"):
            continue
        item = line.item
        master = Decimal(d["received_base_units"]) / Decimal(d["master_pack_equivalent"]) if Decimal(d.get("master_pack_equivalent") or 0) else None
        if master != (item.units_per_pack or 1) or not line.pack_size:
            continue                               # the product's pack changed since: the old conversion proves nothing
        key = pp.pack_key(line.pack_size)
        if db.scalar(select(SupplierPackagingAlias.id).where(SupplierPackagingAlias.supplier_id == line.purchase.supplier_id,
                                                              SupplierPackagingAlias.pack_key == key,
                                                              SupplierPackagingAlias.item_id == item.id)):
            continue
        save_packaging_alias(db, supplier_id=line.purchase.supplier_id, pack=line.pack_size, item=item,
                             units_per_invoice_unit=int(d["units_per_invoice_unit"]), mrp_basis=d.get("mrp_basis") or "MASTER_PACK",
                             source="BOOTSTRAP", purchase=line.purchase, line=line)
        made += 1
    return made
