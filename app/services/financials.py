"""Canonical financial facts. Reports never discover historical acquisition cost.

Quantities are stock/base units. Each SaleItem is an actual batch allocation;
its units_per_pack and pack_mrp already snapshot the conversion and MRP.
"""
from decimal import Decimal, ROUND_HALF_UP
import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models import InventoryMovement, Purchase, PurchaseItem, Sale, SaleItem, SaleReturn, SaleReturnItem
from app.utils import money, to_decimal, utcnow

RESOLVED='COST_RESOLVED'
MISSING='COST_MISSING'
AMBIGUOUS='COST_AMBIGUOUS'
ZERO='COST_ZERO_CONFIRMED'
PRECISION=Decimal('0.000001')
log=logging.getLogger('pharmacy.financials')


def unit_cost(rate, factor):
    if int(factor or 0)<1: raise ValueError('Purchase UOM conversion must be positive')
    rate=Decimal(str(rate))
    if not rate.is_finite() or rate<0: raise ValueError('Purchase rate must be a finite nonnegative amount')
    return (rate/Decimal(factor)).quantize(PRECISION, rounding=ROUND_HALF_UP)


def batch_cost(batch):
    if to_decimal(batch.purchase_rate)>0:
        return unit_cost(batch.purchase_rate,batch.units_per_pack or 1),RESOLVED,'BATCH'
    if batch.cost_status==ZERO:
        return Decimal('0'),RESOLVED,'CONFIRMED_ZERO'
    return None,MISSING,'MANUAL_REVIEW'


def effective_cost(line):
    if line.financial_status in (MISSING,AMBIGUOUS): return None
    if line.financial_status==RESOLVED:
        return line.reconstructed_unit_cost if line.reconstructed_unit_cost is not None else line.cost_rate
    # Legacy positive snapshots remain valid. Legacy zero is never assumed free.
    return line.cost_rate if to_decimal(line.cost_rate)>0 else None


def snapshot_allocation(line,batch):
    cost,status,source=batch_cost(batch)
    line.cost_rate=cost if cost is not None else Decimal('0')
    line.financial_status=status;line.financial_cost_source=source
    line.line_cost=money(cost*line.quantity) if cost is not None else None
    if cost is None:
        log.warning('Cost missing: sale=%s line=%s item=%s batch=%s qty=%s factor=%s',line.sale_id,line.line_no,line.item_id,batch.id,line.quantity,line.units_per_pack)


def allocations(sale,use_snapshots=True):
    lines=sorted(sale.items,key=lambda line:line.id)
    subtotal=sum((to_decimal(l.line_total) for l in lines),Decimal(0));allocated=Decimal(0)
    for i,line in enumerate(lines):
        if use_snapshots and line.net_sale_value is not None: value=line.net_sale_value
        else:
            value=money(sale.total-allocated) if i==len(lines)-1 else money(sale.total*line.line_total/subtotal) if subtotal else Decimal(0)
        allocated+=value
        yield line,value


def finalize(sale):
    for line,value in allocations(sale,use_snapshots=False): line.net_sale_value=value


def gross_value(line,quantity=None):
    qty=line.quantity if quantity is None else quantity
    return money(line.pack_mrp*qty/(line.units_per_pack or 1)) if line.pack_mrp else money(line.mrp*qty)


def matches(line,filters):
    item=line.item
    return not ((filters.get('item') and filters['item'].casefold() not in line.product_name.casefold()) or
                (filters.get('batch') and filters['batch'].casefold() not in line.batch_no.casefold()) or
                (filters.get('category') and (item.category if item else '')!=filters['category']) or
                (filters.get('manufacturer') and (item.manufacturer if item else '')!=filters['manufacturer']))


def facts(db,start,end,filters=None):
    filters=filters or {};rows=[]
    query=select(Sale).where(Sale.payment_status!='CANCELLED',Sale.sale_date<end).options(selectinload(Sale.items).selectinload(SaleItem.item))
    if start: query=query.where(Sale.sale_date>=start)
    for sale in db.scalars(query):
        for line,revenue in allocations(sale):
            if not matches(line,filters): continue
            cost=effective_cost(line)
            amount=line.line_cost if line.line_cost is not None and cost is not None else money(cost*line.quantity) if cost is not None else None
            rows.append(dict(date=sale.sale_date,sale_id=sale.id,sale_line_id=line.id,item_id=line.item_id,item=line.product_name,batch=line.batch_no,batch_id=line.batch_id,
                category=line.item.category if line.item else '',manufacturer=line.item.manufacturer if line.item else '',
                quantity=line.quantity,sale_uom=line.sale_uom or line.base_unit,conversion_factor=line.sale_uom_factor or 1,units_per_pack=line.units_per_pack,mrp=line.mrp,rate=cost,gross_sales=gross_value(line),discount=gross_value(line)-revenue,
                net_sales=revenue,cost=amount,stock_loss=Decimal(0),cost_status=RESOLVED if cost is not None else line.financial_status if line.financial_status==AMBIGUOUS else MISSING,
                cost_source=line.financial_cost_source or ('SALE_SNAPSHOT' if cost is not None else 'MANUAL_REVIEW')))
    returns=select(SaleReturn).where(SaleReturn.status=='COMPLETED',SaleReturn.processed_at<end).options(selectinload(SaleReturn.items))
    if start: returns=returns.where(SaleReturn.processed_at>=start)
    for ret in db.scalars(returns):
        for returned in ret.items:
            line=db.get(SaleItem,returned.sale_item_id) if returned.sale_item_id else None
            if line and line.sale.payment_status=='CANCELLED': continue
            if line and not matches(line,filters): continue
            if not line and any(filters.values()): continue
            cost=effective_cost(line) if line else None
            # Allocate the rounded original line cost cumulatively so a full
            # return reverses exactly its COGS, including repeating fractions.
            amount=None
            if cost is not None:
                original=line.line_cost if line.line_cost is not None else money(cost*line.quantity)
                prior=sum(r.quantity for r in db.scalars(select(SaleReturnItem).join(SaleReturn,SaleReturn.id==SaleReturnItem.return_id).where(
                    SaleReturn.status=='COMPLETED',SaleReturnItem.sale_item_id==line.id,
                    SaleReturnItem.id<returned.id)))
                amount=money(original*(prior+returned.quantity)/line.quantity)-money(original*prior/line.quantity)
            loss=amount if returned.disposition!='RESTOCK' else Decimal(0)
            rows.append(dict(date=ret.processed_at,sale_id=ret.sale_id,sale_line_id=returned.sale_item_id,item_id=returned.item_id,item=returned.product_name,batch=returned.batch_no,batch_id=returned.batch_id,
                category=line.item.category if line and line.item else '',manufacturer=line.item.manufacturer if line and line.item else '',
                quantity=-returned.quantity,sale_uom=line.base_unit if line else '',conversion_factor=1,units_per_pack=line.units_per_pack if line else 1,mrp=line.mrp if line else None,rate=cost,
                gross_sales=-gross_value(line,returned.quantity) if line else -returned.refund_amount,discount=Decimal(0),net_sales=-returned.refund_amount,
                cost=-amount if amount is not None else None,stock_loss=loss,cost_status=RESOLVED if cost is not None else MISSING,cost_source=line.financial_cost_source if line else 'MANUAL_REVIEW',return_id=ret.id,disposition=returned.disposition))
    return rows


def aggregate(rows,basis='realized'):
    revenue=money(sum((r['gross_sales'] if basis=='mrp' else r['net_sales'] for r in rows),Decimal(0)))
    missing={r['sale_line_id'] if r['sale_line_id'] is not None else ('sale',r['sale_id'],'return',r.get('return_id')) for r in rows if r['cost'] is None}
    cogs=None if missing else money(sum((r['cost'] for r in rows),Decimal(0)))
    losses=None if missing else money(sum((r['stock_loss'] for r in rows),Decimal(0)))
    profit=money(revenue-cogs) if cogs is not None else None
    return dict(revenue=revenue,cogs=cogs,profit=profit,margin=money(profit*100/revenue) if profit is not None and revenue else None,
        stock_loss=losses,net_profit=money(profit-losses) if profit is not None else None,
        realized_revenue=money(sum((r['net_sales'] for r in rows),Decimal(0))),
        mrp_margin=money(sum((r['gross_sales'] for r in rows),Decimal(0))-cogs) if cogs is not None else None,
        financial_status=RESOLVED if not missing else MISSING,missing_cost_lines=len(missing),profit_basis=basis,
        **costed(rows,basis))


def costed(rows,basis='realized'):
    """Profit over the lines whose purchase cost is known (sales value - cost).
    The strict figures above stay unknown while any line lacks a cost; these say
    what is known, next to the sales they cover, so a report never goes blank."""
    known=[r for r in rows if r['cost'] is not None]
    if not known:
        return dict(costed_revenue=Decimal(0),costed_cogs=None,costed_profit=None,costed_margin=None,
                    costed_mrp_margin=None,costed_stock_loss=None)
    revenue=money(sum((r['gross_sales'] if basis=='mrp' else r['net_sales'] for r in known),Decimal(0)))
    cogs=money(sum((r['cost'] for r in known),Decimal(0)))
    profit=money(revenue-cogs)
    return dict(costed_revenue=revenue,costed_cogs=cogs,costed_profit=profit,
                costed_margin=money(profit*100/revenue) if revenue else None,
                costed_mrp_margin=money(sum((r['gross_sales'] for r in known),Decimal(0))-cogs),
                costed_stock_loss=money(sum((r['stock_loss'] or 0 for r in known),Decimal(0))))


def summary(db,start,end,filters=None,basis='realized'):
    return aggregate(facts(db,start,end,filters),basis)


def historical_cost(db,line):
    # Historical snapshots and ledger evidence take priority over invoices.
    movements=list(db.scalars(select(InventoryMovement).where(InventoryMovement.reference_type=='SALE_ITEM',InventoryMovement.reference_id==line.id,InventoryMovement.movement_type=='SALE')))
    valid=[m for m in movements if m.unit_cost_snapshot is not None and m.financial_status==RESOLVED]
    if valid:
        values={m.unit_cost_snapshot for m in valid}
        if len(values)==1: return next(iter(values)),RESOLVED,'STOCK_LEDGER'
        return None,AMBIGUOUS,'MANUAL_REVIEW'
    # Never substitute the batch's current rate for a legacy sale snapshot.
    receipts=select(InventoryMovement,PurchaseItem).join(PurchaseItem,(PurchaseItem.purchase_id==InventoryMovement.reference_id)&(PurchaseItem.item_id==line.item_id)).join(Purchase,Purchase.id==PurchaseItem.purchase_id).where(
        InventoryMovement.batch_id==line.batch_id,InventoryMovement.reference_type=='PURCHASE',InventoryMovement.movement_type=='PURCHASE_RECEIPT',
        InventoryMovement.created_at<=line.sale.sale_date,Purchase.purchase_date<=line.sale.sale_date).order_by(InventoryMovement.created_at.desc(),InventoryMovement.id.desc())
    from app.services.stock_ledger import normalize_batch_no
    candidates=[];receipt_id=None
    for movement,invoice_line in db.execute(receipts):
        if normalize_batch_no(invoice_line.batch_no)!=normalize_batch_no(line.batch_no): continue
        if receipt_id is not None and movement.id!=receipt_id: break
        receipt_id=movement.id
        if invoice_line.rate>0:
            candidates.append(unit_cost(invoice_line.rate,movement.units_per_pack or 1))
    if len(set(candidates))==1: return candidates[0],RESOLVED,'PURCHASE_LINE'
    return None,AMBIGUOUS if candidates else MISSING,'MANUAL_REVIEW'


def reconstruct_ledger_costs(db,line,cost):
    for move in db.scalars(select(InventoryMovement).where(InventoryMovement.reference_type=='SALE_ITEM',InventoryMovement.reference_id==line.id,InventoryMovement.movement_type=='SALE')):
        if move.unit_cost_snapshot is None:
            move.unit_cost_snapshot=cost
            move.cost_amount=-money(cost*abs(move.quantity)) if cost is not None else None
            move.financial_status=RESOLVED if cost is not None else MISSING
        previous=0
        for reverse in db.scalars(select(InventoryMovement).where(InventoryMovement.reversal_of_id==move.id).order_by(InventoryMovement.id)):
            qty=abs(reverse.quantity)
            if reverse.unit_cost_snapshot is None and move.cost_amount is not None:
                original=abs(move.cost_amount)
                part=money(original*(previous+qty)/abs(move.quantity))-money(original*previous/abs(move.quantity))
                reverse.unit_cost_snapshot=cost;reverse.cost_amount=part if reverse.quantity>0 else -part;reverse.financial_status=RESOLVED
            previous+=qty


def backfill(db,apply=False):
    result=dict(total=0,resolved=0,reconstructed=0,unresolved=0,lines=[])
    for sale in db.scalars(select(Sale).options(selectinload(Sale.items)).order_by(Sale.id)):
        for line,revenue in allocations(sale):
            result['total']+=1;cost=effective_cost(line);source=line.financial_cost_source or 'SALE_SNAPSHOT'
            status=RESOLVED if cost is not None else MISSING
            reconstructed=False
            if cost is None:
                cost,status,source=historical_cost(db,line);reconstructed=cost is not None
            result['reconstructed' if reconstructed else 'resolved' if cost is not None else 'unresolved']+=1
            result['lines'].append(dict(sale_id=sale.id,sale_line_id=line.id,item=line.product_name,batch=line.batch_no,quantity=line.quantity,units_per_pack=line.units_per_pack,
                mrp=str(line.mrp),revenue=str(revenue),unit_cost=str(cost) if cost is not None else None,status=status,source=source))
            if apply and line.financial_status!='COST_RESOLVED':
                line.financial_status=status;line.financial_cost_source=source
                line.net_sale_value=revenue;line.line_cost=money(cost*line.quantity) if cost is not None else None
                if reconstructed:
                    line.reconstructed_unit_cost=cost;line.cost_reconstructed_at=utcnow();line.cost_reconstruction_version='cost-v1'
                reconstruct_ledger_costs(db,line,cost)
    return result
