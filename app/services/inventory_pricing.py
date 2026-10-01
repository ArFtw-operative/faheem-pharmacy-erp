"""Purchase invoice pricing shared by the inventory workspace and stock reports."""
from sqlalchemy import select
from app.models import InventoryMovement, Purchase, PurchaseItem
from app.services.stock_ledger import normalize_batch_no
from app.services import units


def batch_prices(db, batches, end=None):
    batches = list(batches)
    by_id = {b.id: b for b in batches}
    prices = {b.id: dict(purchase_rate=b.purchase_rate,
                        unit_purchase_rate=units.unit_price(b.purchase_rate, b.units_per_pack or 1),
                        pack_mrp=b.mrp, purchase_invoice='', rate_source='Recorded batch rate')
              for b in batches}
    if not by_id:
        return prices
    # A batch can be received on several invoices. Its original purchase_id is
    # not updated on subsequent receipts, so use the receipt ledger instead.
    query = (select(InventoryMovement, PurchaseItem, Purchase)
             .join(Purchase, Purchase.id == InventoryMovement.reference_id)
             .join(PurchaseItem, (PurchaseItem.purchase_id == Purchase.id) &
                   (PurchaseItem.item_id == InventoryMovement.item_id))
             .where(InventoryMovement.batch_id.in_(by_id),
                    InventoryMovement.reference_type == 'PURCHASE',
                    InventoryMovement.movement_type == 'PURCHASE_RECEIPT',
                    InventoryMovement.quantity > 0)
             .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc(), PurchaseItem.id.desc()))
    if end is not None:
        query = query.where(InventoryMovement.created_at < end)
    found = set()
    for movement, line, purchase in db.execute(query):
        batch = by_id[movement.batch_id]
        if batch.id in found or normalize_batch_no(line.batch_no or '') != normalize_batch_no(batch.batch_no or ''):
            continue
        found.add(batch.id)
        prices[batch.id].update(purchase_rate=line.rate,
                               unit_purchase_rate=units.unit_price(line.rate, batch.units_per_pack or 1),
                               purchase_invoice=purchase.invoice_no, rate_source='Purchase invoice')
    return prices


def product_prices(batches, prices):
    """Only show a single product price when its stocked batches agree."""
    batches = list(batches)
    active = [b for b in batches if b.quantity > 0] or batches
    result = dict(pricing_varies=[])
    for key in ('purchase_rate', 'unit_purchase_rate', 'pack_mrp'):
        values = {prices[b.id][key] for b in active}
        result[key] = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            result['pricing_varies'].append(key)
    invoices = {prices[b.id]['purchase_invoice'] for b in active}
    result['purchase_invoice'] = next(iter(invoices)) if len(invoices) == 1 else 'Multiple invoices' if invoices else ''
    return result
