"""Financial diagnostics and reporting consume the same immutable domain facts."""
from decimal import Decimal, InvalidOperation
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import audit
from app.database import get_db
from app.deps import require_permission
from app.models import Batch, Item, SaleItem, User
from app.permissions import has_permission
from app.routing import OffloadRoute
from app.services import financials, report_generator, stock_ledger
from app.utils import money, utcnow
router=APIRouter(tags=['financials'],route_class=OffloadRoute)


@router.get('/api/reports/profit-margin')
def profit_margin(from_date:str=Query(alias='from'),to:str='',groupBy:str='day',item:str='',category:str='',brand:str='',batch:str='',profit_basis:str='realized',db:Session=Depends(get_db),user:User=Depends(require_permission('reports.financials'))):
    params={'period':'custom','from':from_date,'to':to or from_date,'group_by':groupBy,'profit_basis':profit_basis,'item':item,'category':category,'manufacturer':brand,'batch':batch}
    try:
        report=report_generator.generate(db,'profit',params,user=user)
        p,start,end=report_generator.parameters(db,report_generator.BY_ID['profit'],params)
    except (report_generator.ReportError,ValueError) as exc: raise HTTPException(400,str(exc))
    missing=financials.summary(db,start,end,p,profit_basis)['missing_cost_lines']
    return dict(period={'from':report['from_date'],'to':report['to_date']},rows=report['rows'],totals=report['totals'],dataQuality={'missing_cost_lines':missing})


@router.get('/api/reports/item-profitability')
def item_profitability(from_date:str=Query(alias='from'),to:str='',item:str='',category:str='',brand:str='',batch:str='',db:Session=Depends(get_db),user:User=Depends(require_permission('reports.financials'))):
    params={'period':'custom','from':from_date,'to':to or from_date,'item':item,'category':category,'manufacturer':brand,'batch':batch}
    try: p,start,end=report_generator.parameters(db,report_generator.BY_ID['profit'],params)
    except report_generator.ReportError as exc: raise HTTPException(400,str(exc))
    facts=financials.facts(db,start,end,p)
    groups={}
    for fact in facts:
        key=(fact['item_id'],fact['batch_id'],fact['rate'],fact['mrp'])
        groups.setdefault(key,[]).append(fact)
    rows=[]
    for group in groups.values():
        first=group[0];summary=financials.aggregate(group)
        rows.append(dict(item=first['item'],batch=first['batch'],quantity=sum(r['quantity'] for r in group),mrp=first['mrp'],rate=first['rate'],cost_source=first['cost_source'],**summary))
    return dict(period={'from':p['from'],'to':p['to']},rows=rows,totals=financials.aggregate(facts),dataQuality={'missingCostLines':financials.aggregate(facts)['missing_cost_lines']})


@router.get('/api/reports/cost-issues')
def cost_issues(offset:int=0,limit:int=100,from_date:str=Query(default='',alias='from'),to:str='',item:str='',category:str='',brand:str='',batch:str='',db:Session=Depends(get_db),user:User=Depends(require_permission('reports.financials'))):
    unresolved=[line for line in db.scalars(select(SaleItem).order_by(SaleItem.id)) if financials.effective_cost(line) is None]
    if from_date:
        params={'period':'custom','from':from_date,'to':to or from_date,'item':item,'category':category,'manufacturer':brand,'batch':batch}
        try: p,start,end=report_generator.parameters(db,report_generator.BY_ID['profit'],params)
        except report_generator.ReportError as exc: raise HTTPException(400,str(exc))
        affected={f['sale_line_id'] for f in financials.facts(db,start,end,p) if f['cost'] is None}
        unresolved=[line for line in unresolved if line.id in affected]
    return dict(total=len(unresolved),rows=[dict(sale_id=l.sale_id,sale_line_id=l.id,item=l.product_name,batch_id=l.batch_id,batch=l.batch_no,quantity=l.quantity,
        units_per_pack=l.units_per_pack,original_cost_rate=l.cost_rate,sale_date=l.sale.sale_date,invoice=l.sale.invoice_no,status=l.financial_status,source=l.financial_cost_source or 'MANUAL_REVIEW') for l in unresolved[max(0,offset):max(0,offset)+min(max(1,limit),500)]])


def verified_rate(data):
    if not isinstance(data,dict): raise HTTPException(400,'Expected an object')
    reason=str(data.get('reason') or '').strip()
    if not reason: raise HTTPException(400,'A verification reason is required')
    try: rate=Decimal(str(data['purchase_rate']))
    except (KeyError,InvalidOperation,ValueError): raise HTTPException(400,'A valid purchase rate is required')
    if not rate.is_finite() or rate<0 or rate>Decimal('9999999999'): raise HTTPException(400,'Invalid purchase rate')
    if rate==0 and data.get('confirm_zero') is not True: raise HTTPException(400,'Confirm genuinely zero acquisition cost explicitly')
    return rate,reason


@router.post('/api/erp/inventory/{item_id}/batches/{batch_id}/cost')
async def set_batch_cost(item_id:int,batch_id:int,request:Request,db:Session=Depends(get_db),user:User=Depends(require_permission('inventory.edit'))):
    if not has_permission(user,'purchase.view'): raise HTTPException(403,'Purchase access is required')
    batch=db.get(Batch,batch_id)
    if not batch or batch.item_id!=item_id: raise HTTPException(404,'Batch not found')
    data=await request.json();rate,reason=verified_rate(data)
    if rate!=money(rate): raise HTTPException(400,'Purchase pack rate must have at most two decimal places')
    before=audit.snapshot(batch);batch.purchase_rate=money(rate);batch.cost_status=financials.ZERO if rate==0 else financials.RESOLVED
    stock_ledger.sync_unit_prices(batch)
    audit.record(db,action=audit.A_UPDATE,entity_type='batch',entity_id=batch.id,user=user,before=before,after=audit.snapshot(batch),details='Verified purchase cost: '+reason)
    db.commit()
    return dict(batch_id=batch.id,purchase_rate=batch.purchase_rate,unit_cost=batch.unit_cost,status=batch.cost_status)


@router.post('/api/reports/cost-issues/{line_id}/resolve')
async def resolve_historical_cost(line_id:int,request:Request,db:Session=Depends(get_db),user:User=Depends(require_permission('reports.financials'))):
    line=db.get(SaleItem,line_id)
    if line is None: raise HTTPException(404,'Sale line not found')
    if financials.effective_cost(line) is not None: raise HTTPException(409,'A resolved historical snapshot cannot be overwritten')
    data=await request.json();rate,reason=verified_rate(data)
    # Operator supplies the original purchase PACK rate; use the sale's own
    # snapshotted conversion, never the product's current packaging.
    cost=financials.unit_cost(rate,line.units_per_pack or 1)
    line.reconstructed_unit_cost=cost;line.line_cost=money(cost*line.quantity);line.financial_status=financials.RESOLVED
    line.financial_cost_source='MANUAL_VERIFIED';line.cost_reconstructed_at=utcnow();line.cost_reconstruction_version='manual-v1'
    financials.reconstruct_ledger_costs(db,line,cost)
    audit.record(db,action=audit.A_UPDATE,entity_type='sale_item',entity_id=line.id,user=user,after={'verified_unit_cost':str(cost),'source':'MANUAL_VERIFIED'},details='Historical cost verification: '+reason)
    db.commit()
    return dict(sale_line_id=line.id,status=line.financial_status,cost_source=line.financial_cost_source,unit_cost=cost,cogs=line.line_cost)


@router.put('/api/erp/inventory/{item_id}/uoms')
async def configure_uoms(item_id:int,request:Request,db:Session=Depends(get_db),user:User=Depends(require_permission('inventory.edit'))):
    from app.models import ItemUom
    from app.services import uom_service
    item=db.get(Item,item_id)
    if item is None: raise HTTPException(404,'Product not found')
    data=await request.json();rows=data.get('units') if isinstance(data,dict) else None
    if not isinstance(rows,list): raise HTTPException(400,'Provide units as a list')
    graph={}
    try:
        for row in rows:
            unit=str(row['unit']).strip().upper();parent=str(row['parent_unit']).strip().upper();raw=row['factor']
            if not unit or not parent or len(unit)>20 or len(parent)>20 or isinstance(raw,bool) or int(raw)!=raw or int(raw)<1 or int(raw)>1000000: raise ValueError('Invalid unit conversion')
            if unit in graph: raise ValueError('Duplicate unit')
            graph[unit]=(parent,int(raw))
        uom_service.validate(item,graph)
    except (ValueError,TypeError,KeyError) as exc: raise HTTPException(400,str(exc))
    for row in db.scalars(select(ItemUom).where(ItemUom.item_id==item_id)): db.delete(row)
    db.flush()
    for unit,(parent,conversion) in graph.items(): db.add(ItemUom(item_id=item_id,unit=unit,parent_unit=parent,factor=conversion))
    audit.record(db,action=audit.A_UPDATE,entity_type='item',entity_id=item_id,user=user,after=graph,details='Configured sale UOM hierarchy; historical sale conversions retained')
    db.commit()
    return {'item_id':item_id,'units':rows}
