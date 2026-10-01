"""Acquisition cost, financial snapshots, quality, units and reconciliation."""
from datetime import date, datetime, timedelta
from app.utils import utcnow
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models import Batch, InventoryMovement, ItemUom, Purchase, PurchaseItem, SaleItem, User, Role, Permission
from app.services import financials as finance, inventory_service as inv, sales_service as sales, stock_ledger, refund_service, report_generator, settings_service, reports_service
from tests.conftest import login


def stock(db, rate='100', mrp='150', upp=1, loose=False, name='Financial medicine',base='UNIT',pack='PACK',qty=200,batch_no='FIN'):
    item=inv.create_item(db,name=name,units_per_pack=upp,loose_sale=loose,base_unit=base,pack_unit=pack)
    batch=inv.add_or_update_batch(db,item,batch_no=batch_no,expiry_date=date(2099,1,1),quantity=qty,mrp=mrp,purchase_rate=rate,movement_type='OPENING_STOCK')
    db.commit();return item,batch


def sale(db,item,quantity=1,**kwargs):
    doc=sales.create_sale(db,lines=[{'item_id':item.id,'quantity':quantity}],round_off_mode='NONE',**kwargs)
    db.commit();return doc


def result(db): return finance.summary(db,None,utcnow()+timedelta(days=1))


@pytest.mark.parametrize('rate,mrp,quantity,upp,loose,base,expected',[
 ('100','150',1,1,False,'UNIT',(150,100,50)),
 ('100','150',4,1,False,'UNIT',(600,400,200)),
 ('100','150',4,10,True,'TABLET',(60,40,20)),
 ('70','95',1,1,False,'BOTTLE',(95,70,25)),
 ('140','180',1,1,False,'BOTTLE',(180,140,40)),
])
def test_pack_loose_and_indivisible_costs(db,rate,mrp,quantity,upp,loose,base,expected):
    item,batch=stock(db,rate,mrp,upp,loose,base=base)
    doc=sale(db,item,quantity)
    res=result(db)
    assert (res['revenue'],res['cogs'],res['profit'])==expected
    assert res['financial_status']==finance.RESOLVED
    line=doc.items[0]
    move=db.scalar(select(InventoryMovement).where(InventoryMovement.reference_type=='SALE_ITEM',InventoryMovement.reference_id==line.id))
    assert move.cost_amount==-res['cogs'] and line.net_sale_value==res['revenue']
    assert line.cost_rate==finance.unit_cost(Decimal(rate),upp)


def test_multi_batch_and_snapshot_immutability(db):
    item,a=stock(db,'80','120',upp=10,loose=True,base='TABLET',pack='STRIP',qty=40,batch_no='A')
    b=inv.add_or_update_batch(db,item,batch_no='B',expiry_date=date(2099,2,1),quantity=60,purchase_rate=90,mrp=120,movement_type='OPENING_STOCK')
    doc=sale(db,item,100)
    assert len(doc.items)==2 and [l.quantity for l in doc.items]==[40,60]
    assert result(db)['cogs']==860 and result(db)['profit']==340
    a.purchase_rate=1000;b.purchase_rate=1000;a.mrp=2000;b.mrp=2000
    stock_ledger.sync_unit_prices(a);stock_ledger.sync_unit_prices(b);db.commit()
    assert result(db)['cogs']==860 and result(db)['revenue']==1200
    moves=list(db.scalars(select(InventoryMovement).where(InventoryMovement.movement_type=='SALE')))
    assert -sum(m.cost_amount for m in moves)==860


def test_discount_basis_and_revenue_reconciliation(db):
    item,batch=stock(db)
    doc=sale(db,item,1,discount=10)
    res=result(db)
    assert res['revenue']==140 and res['profit']==40 and res['mrp_margin']==50
    assert finance.summary(db,None,utcnow()+timedelta(days=1),basis='mrp')['profit']==50
    assert sum(value for _,value in finance.allocations(doc))==doc.total
    assert reports_service.sales_summary(db,None,utcnow()+timedelta(days=1))['total']==res['revenue']


def test_hierarchical_units_are_configured_not_inferred_from_content(db):
    item,batch=stock(db,'1000','1500',upp=100,loose=True,base='TABLET',pack='BOX',qty=1000)
    db.add_all([ItemUom(item_id=item.id,unit='BOX',parent_unit='STRIP',factor=10),ItemUom(item_id=item.id,unit='STRIP',parent_unit='TABLET',factor=10)])
    db.commit()
    doc=sales.create_sale(db,lines=[{'item_id':item.id,'quantity':3,'uom':'STRIP'}],round_off_mode='NONE');db.commit()
    line=doc.items[0]
    assert line.quantity==30 and line.sale_uom_factor==10 and line.sale_quantity==3
    assert result(db)['revenue']==450 and result(db)['cogs']==300
    syrup,sb=stock(db,'70','95',name='Syrup 200 ml',base='BOTTLE',pack='BOTTLE')
    assert sb.units_per_pack==1 and finance.batch_cost(sb)[0]==70


def test_missing_cost_masks_profit_and_confirmed_free_cost_is_distinct(db):
    item,batch=stock(db,'0')
    doc=sale(db,item)
    res=result(db)
    assert res['revenue']==150
    assert res['cogs'] is None and res['profit'] is None and res['margin'] is None
    assert res['missing_cost_lines']==1 and doc.items[0].financial_status==finance.MISSING
    batch.cost_status=finance.ZERO;db.commit()
    second=sale(db,item)
    assert second.items[0].financial_status==finance.RESOLVED and second.items[0].line_cost==0
    assert finance.aggregate([r for r in finance.facts(db,None,utcnow()+timedelta(days=1)) if r['sale_id']==second.id])['margin']==100
    # Updating today's batch never fills yesterday's unknown sale snapshot.
    batch.purchase_rate=100;stock_ledger.sync_unit_prices(batch);db.commit()
    assert result(db)['cogs'] is None


def test_returns_reverse_original_cost_and_expose_damage_separately(db):
    item,batch=stock(db)
    doc=sale(db,item,2)
    batch.purchase_rate=900;stock_ledger.sync_unit_prices(batch);db.commit()
    refund_service.create_return(db,doc,lines=[{'sale_item_id':doc.items[0].id,'quantity':1,'disposition':'RESTOCK'}],reason_code='WRONG_ITEM');db.commit()
    assert result(db)['revenue']==150 and result(db)['cogs']==100 and result(db)['profit']==50
    refund_service.create_return(db,doc,lines=[{'sale_item_id':doc.items[0].id,'quantity':1,'disposition':'DAMAGE'}],reason_code='DAMAGED');db.commit()
    res=result(db)
    assert res['revenue']==0 and res['cogs']==0 and res['stock_loss']==100 and res['net_profit']==-100
    returned=db.scalar(select(InventoryMovement).where(InventoryMovement.movement_type=='SALE_RETURN'))
    assert returned.cost_amount==100


def test_fractional_unit_cost_partial_returns_reconcile_exactly(db):
    item,batch=stock(db,'100','150',upp=3,loose=True,base='TABLET',pack='STRIP')
    doc=sale(db,item,3)
    assert doc.items[0].cost_rate==Decimal('33.333333') and doc.items[0].line_cost==100
    for _ in range(3):
        refund_service.create_return(db,doc,lines=[{'sale_item_id':doc.items[0].id,'quantity':1}],reason_code='WRONG_ITEM');db.commit()
    moves=list(db.scalars(select(InventoryMovement).where(InventoryMovement.movement_type.in_(['SALE','SALE_RETURN']))))
    assert sum(m.cost_amount for m in moves)==0
    assert result(db)['cogs']==0 and result(db)['revenue']==0


def test_backfill_preserves_original_zero_and_requires_historical_evidence(db):
    item,batch=stock(db,'0')
    doc=sale(db,item);line=doc.items[0]
    line.financial_status='LEGACY_UNCHECKED';line.cost_rate=0;line.line_cost=None;line.net_sale_value=None
    movement=db.scalar(select(InventoryMovement).where(InventoryMovement.movement_type=='SALE'))
    movement.unit_cost_snapshot=None;movement.cost_amount=None
    batch.purchase_rate=999;stock_ledger.sync_unit_prices(batch);db.commit()
    review=finance.backfill(db)
    assert review['unresolved']==1 and line.financial_status=='LEGACY_UNCHECKED'
    purchase=Purchase(invoice_no='ORIGINAL',purchase_date=doc.sale_date-timedelta(days=1),total=100,status='POSTED');db.add(purchase);db.flush()
    db.add(PurchaseItem(purchase_id=purchase.id,item_id=item.id,product_name=item.name,batch_no=batch.batch_no,quantity=1,rate=100,mrp=150,line_total=100))
    receipt=stock_ledger.post(db,batch,'PURCHASE_RECEIPT',1,reference_type='PURCHASE',reference_id=purchase.id)
    receipt.created_at=doc.sale_date-timedelta(hours=1);db.commit()
    review=finance.backfill(db,apply=True);db.commit()
    assert review['reconstructed']==1 and line.cost_rate==0 and line.reconstructed_unit_cost==100
    assert line.cost_reconstruction_version=='cost-v1' and line.financial_cost_source=='PURCHASE_LINE'
    assert result(db)['cogs']==100 and finance.backfill(db,apply=True)['resolved']==1


def test_mandatory_missing_cost_rolls_back_transaction(db):
    item,batch=stock(db,'0')
    settings_service.set_setting(db,'require_sale_cost','true');db.commit()
    with pytest.raises(sales.SaleError,match='cost missing'):
        try: sale(db,item)
        except sales.SaleError: db.rollback();raise
    assert batch.quantity==200 and db.scalar(select(SaleItem.id)) is None


def test_voided_sale_has_no_revenue_or_cogs(db):
    item,batch=stock(db)
    doc=sale(db,item,2)
    sales.void_sale(db,doc,reason='Wrong bill');db.commit()
    assert result(db)['revenue']==0 and result(db)['cogs']==0
    moves=list(db.scalars(select(InventoryMovement).where(InventoryMovement.movement_type.in_(['SALE','SALE_CANCEL']))))
    assert sum(m.cost_amount for m in moves)==0 and batch.quantity==200


def test_financial_apis_and_verified_repair_preserve_raw_history(client,db):
    item,batch=stock(db,'0');doc=sale(db,item);login(client)
    today=datetime.now(report_generator.tz_for(db)).date().isoformat()
    report=client.get('/api/reports/profit-margin',params={'from':today,'to':today}).json()
    assert report['totals']['cogs'] is None and report['dataQuality']['missing_cost_lines']==1
    issues=client.get('/api/reports/cost-issues').json();assert issues['total']==1
    assert client.post(f'/api/reports/cost-issues/{doc.items[0].id}/resolve',json={'purchase_rate':100}).status_code==400
    repaired=client.post(f'/api/reports/cost-issues/{doc.items[0].id}/resolve',json={'purchase_rate':100,'reason':'Verified original opening stock invoice'})
    assert repaired.status_code==200
    db.expire_all();assert doc.items[0].cost_rate==0 and doc.items[0].reconstructed_unit_cost==100
    assert client.get('/api/reports/item-profitability',params={'from':today}).json()['totals']['profit']==50
    assert client.post(f'/api/reports/cost-issues/{doc.items[0].id}/resolve',json={'purchase_rate':200,'reason':'Changed today'}).status_code==409
    user=db.scalars(select(User)).first();role=Role(name='Stock only',permissions=[db.scalar(select(Permission).where(Permission.code=='inventory.view'))]);db.add(role);db.flush();user.role_id=role.id;db.commit()
    assert client.get('/api/reports/cost-issues').status_code==403


def test_purchase_return_changes_remaining_stock_not_sale_snapshot(db):
    from app.services import purchase_service
    item,batch=stock(db)
    doc=sale(db,item,2)
    purchase_service.create_return(db,item_id=item.id,batch_id=batch.id,quantity=5,value=500,reason='Supplier return');db.commit()
    assert batch.quantity==193 and result(db)['cogs']==200
    returned=db.scalar(select(InventoryMovement).where(InventoryMovement.movement_type=='PURCHASE_RETURN'))
    assert returned.cost_amount==-500 and doc.items[0].line_cost==200


def test_failed_api_sale_rolls_back_all_allocations(client,db):
    first,a=stock(db,name='Known cost')
    second,b=stock(db,'0',name='Unknown cost')
    settings_service.set_setting(db,'require_sale_cost','true');db.commit();login(client)
    response=client.post('/api/sales',json={'payment_mode':'CASH','lines':[{'item_id':first.id,'quantity':1},{'item_id':second.id,'quantity':1}]})
    assert response.status_code==400
    db.expire_all();assert a.quantity==200 and b.quantity==200
    assert not list(db.scalars(select(SaleItem)))
    assert not list(db.scalars(select(InventoryMovement).where(InventoryMovement.movement_type=='SALE')))


def test_profit_and_generated_sales_summary_reconcile_after_refunds(db):
    item,batch=stock(db)
    doc=sale(db,item,4,discount=20)
    refund_service.create_return(db,doc,lines=[{'sale_item_id':doc.items[0].id,'quantity':1,'disposition':'RESTOCK'}],reason_code='WRONG_ITEM');db.commit()
    today=datetime.now(report_generator.tz_for(db)).date().isoformat()
    params={'period':'custom','from':today,'to':today}
    summary=report_generator.generate(db,'sales-summary',params)
    profit=report_generator.generate(db,'profit',params)
    assert summary['totals']['value']==profit['totals']['revenue']==435
    assert profit['totals']['cogs']==300


def test_profit_report_counts_lines_with_a_known_purchase_rate(client,db):
    known,_=stock(db,'100','150',name='Costed medicine',batch_no='K1')
    legacy,lb=stock(db,'0','40',upp=10,loose=True,base='TABLET',pack='STRIP',name='Legacy tablet',batch_no='L1')
    sale(db,known);sale(db,legacy,5)
    login(client)
    today=datetime.now(report_generator.tz_for(db)).date().isoformat()
    t=client.get('/api/reports/profit-margin',params={'from':today,'to':today}).json()['totals']
    assert float(t['revenue'])==170 and float(t['cogs'])==100 and float(t['profit'])==50
