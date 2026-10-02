"""Clear legacy stock before a fresh opening-stock import.

The ordinary stock/catalog reset preserves ledger and sales/purchase history:

* ``stock``   — every batch with stock gets an ``ADJUSTMENT_OUT`` to zero,
  reason recorded. Products stay; the next import matches them by name or
  barcode and may set their packaging (allowed now that stock is zero).
* ``catalog`` — the above, then every product moves to the recycle bin, so the
  import builds a clean product master with proper packaging. Old bills keep
  their snapshots; products can be restored from the bin.
"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import audit
from app.models import Batch, Item, User
from app.services import inventory_service, stock_ledger

MODES = ("stock", "catalog")


def preview(db: Session) -> dict:
    batches = list(db.scalars(select(Batch).where(Batch.quantity > 0)))
    return {
        "batches_with_stock": len(batches),
        "units": sum(b.quantity for b in batches),
        "products": db.query(Item).filter(Item.deleted_at.is_(None)).count(),
        "negative_batches": db.query(Batch).filter(Batch.quantity < 0).count(),
    }


def reset(db: Session, *, mode: str = "stock", reason: str = "", user: User | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    mismatches = stock_ledger.reconcile(db)
    if mismatches:
        raise stock_ledger.StockError(
            f"{len(mismatches)} batch(es) disagree with the ledger; run a reconciliation first"
        )
    reason = reason.strip() or "Inventory reset before fresh stock import"
    before = preview(db)
    for batch in db.scalars(select(Batch).where(Batch.quantity > 0).order_by(Batch.id)):
        stock_ledger.post(db, batch, "ADJUSTMENT_OUT", batch.quantity, reference_type="RESET",
                          reference_no="INVENTORY-RESET", reason=reason, user=user)
    binned = 0
    if mode == "catalog":
        for item in db.scalars(select(Item).where(Item.deleted_at.is_(None))):
            inventory_service.delete_item(db, item, user=user)
            binned += 1
    audit.record(db, action=audit.A_UPDATE, entity_type="inventory", entity_id="reset", user=user,
                 before=before, after={"mode": mode, "binned": binned},
                 details=f"Inventory reset ({mode}): {before['units']} units cleared, {binned} products binned")
    db.flush()
    return {**before, "mode": mode, "binned": binned}


def reset_purchases_and_stock(db: Session, *, user: User | None = None) -> dict:
    """Explicit administrative reset; caller owns the transaction.

    Sales, products and the reconciled ledger survive. Purchase documents and
    returns are removed, their batch links detached, and verified packaging
    facts retained independently so future imports do not lose learned units.
    """
    from app.services.purchase_automation import preserve_reviewed_pack_knowledge
    facts = preserve_reviewed_pack_knowledge(db, user=user)
    counts = {t:db.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar_one()
              for t in ('purchases','purchase_items','purchase_returns')}
    stock = reset(db, reason='User-requested development inventory and purchase reset', user=user)
    db.flush()
    db.execute(text('UPDATE batches SET purchase_id = NULL WHERE purchase_id IS NOT NULL'))
    # Keep historical quantities and references for sales/stock reconciliation;
    # only detach navigational IDs for documents that are being removed.
    db.execute(text("UPDATE inventory_movements SET reference_id = NULL WHERE reference_type IN ('PURCHASE','PURCHASE_RETURN')"))
    for table in ('purchase_returns','purchase_items','purchases'):
        db.execute(text(f'DELETE FROM {table}'))
    audit.record(db, action=audit.A_DELETE, entity_type='purchase', entity_id='reset', user=user,
                 before=counts, after={'reviewed_pack_facts_retained':facts},
                 details='User-requested purchase reset; catalogue, supplier knowledge and sales retained')
    db.expire_all()
    if stock_ledger.reconcile(db):
        raise stock_ledger.StockError('Inventory reset did not reconcile')
    return {'removed':counts, 'stock':stock, 'reviewed_pack_facts_retained':facts}
