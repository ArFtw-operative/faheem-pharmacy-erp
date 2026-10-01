"""Explicit, SQL-backed ERP reports. No report is queried by the selector page.

Money is kept as Decimal until serialization. Sales category/item totals allocate
bill discounts, vouchers and rounding over immutable sale-line values so they
reconcile to the invoice total. Stock as-of uses the signed inventory ledger.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.models import (Batch, Item, Purchase, PurchaseItem, PurchaseReturn, Sale,
                        SaleItem, SalePayment, SaleReturn, SaleReturnItem, InventoryMovement, Supplier, AuditLog, User)
from app.permissions import has_permission
from app.services import settings_service, inventory_pricing, financials, business_time
from app.services.stock_ledger import MOVEMENT_TYPES, MOVEMENT_LABELS
from app.utils import money, to_decimal

PERIODS = [('today', 'Today'), ('yesterday', 'Yesterday'), ('this_week', 'This Week'),
           ('last_week', 'Last Week'), ('last_7', 'Last 7 Days'), ('this_month', 'This Month'), ('last_month', 'Last Month'),
           ('last_30', 'Last 30 Days'), ('last_90', 'Last 90 Days'), ('this_quarter', 'This Quarter'), ('last_quarter', 'Last Quarter'),
           ('this_fy', 'This Financial Year'), ('last_fy', 'Last Financial Year'), ('custom', 'Custom Range')]
GROUPS = [('day', 'Day'), ('week', 'Week'), ('month', 'Month')]


def col(key, label, kind='text', default=True, total=False):
    return dict(key=key, label=label, kind=kind, default=default, total=total)


DATE = col('date', 'Date')
ITEM = col('item', 'Item')
QTY = col('quantity', 'Quantity', 'number', total=True)
VALUE = col('value', 'Sales Value', 'money', total=True)
BILLS = col('bills', 'Bills', 'number', total=True)
CATEGORY = col('category', 'Category', default=False)
BRAND = col('manufacturer', 'Manufacturer', default=False)
BATCH = col('batch', 'Batch', default=False)
PACK = col('pack', 'Pack')
OPTIONAL_SALE = [BATCH, CATEGORY, BRAND, col('customer', 'Customer', default=False), col('invoice', 'Bill Number', default=False)]
# cost and profit only when a user with financial rights asks for them
FINANCIAL_SALE = [{**col('cost', 'Cost', 'money', False, True), 'permission': 'reports.financials'},
                  {**col('profit', 'Profit', 'money', False, True), 'permission': 'reports.financials'}]
SALES_FILTERS = ['category', 'manufacturer', 'item', 'batch']
INVENTORY_FILTERS = ['as_of', 'category', 'manufacturer', 'supplier', 'item', 'batch', 'expiry_window', 'stock_status']
CATALOG = [
    dict(id='sales-summary', title='Sales Summary', group='Sales Reports', description='Daily / date-range sales report', permission='reports.sales', filters=['view', 'group_by']+SALES_FILTERS, columns=[DATE, VALUE, BILLS]),
    dict(id='bill-register', title='Bill Register', group='Sales Reports', description='Invoices, customers and bill values', permission='reports.sales', filters=['invoice', 'customer', 'customer_id', 'payment'], columns=[DATE, col('invoice','Bill Number'), col('customer','Customer'), col('sale_type','Sale Type',default=False), col('payment','Payment'), VALUE, col('discount','Bill Discount','money',False,True), col('status','Status',default=False)]),
    dict(id='item-sales', title='Item Sales Detail', group='Sales Reports', description='Item sales with bill, customer or batch detail', permission='reports.sales', filters=SALES_FILTERS+['sort','show'], columns=[ITEM, PACK, QTY, col('mrp','MRP / unit','money'), VALUE]+OPTIONAL_SALE+FINANCIAL_SALE),
    dict(id='category-sales', title='Category-wise Sales', group='Sales Reports', description='Sales by configured product category', permission='reports.sales', filters=['category','group_by'], columns=[DATE,col('category','Category'),QTY,VALUE,BILLS]),
    dict(id='brand-sales', title='Brand-wise Sales', group='Sales Reports', description='Sales by product manufacturer', permission='reports.sales', filters=['manufacturer','category','group_by'], columns=[DATE,col('manufacturer','Manufacturer'),QTY,VALUE,BILLS]),
    dict(id='sales-returns', title='Sales Returns', group='Sales Reports', description='Completed customer returns and refunds', permission='reports.sales', filters=['item','batch','refund_method'], columns=[DATE,col('reference','Return Number'),col('invoice','Bill Number'),ITEM,QTY,col('value','Refund','money',total=True),col('disposition','Disposition'),BATCH,col('reason','Reason',default=False)]),
    dict(id='purchase-summary', title='Purchase Summary', group='Purchase Reports', description='Purchases grouped by date', permission='reports.purchase', filters=['supplier','invoice','item','category','group_by'], columns=[DATE,col('bills','Invoices','number',total=True),col('value','Purchase Value','money',total=True)]),
    dict(id='purchase-register', title='Purchase Register', group='Purchase Reports', description='Supplier invoices and received purchases', permission='reports.purchase', filters=['supplier','invoice','item','category'], columns=[DATE,col('invoice','Invoice Number'),col('supplier','Supplier'),col('value','Purchase Value','money',total=True),col('status','Status')]),
    dict(id='supplier-purchases', title='Supplier-wise Purchase', group='Purchase Reports', description='Purchase totals by supplier', permission='reports.purchase', filters=['supplier','invoice','item','category'], columns=[col('supplier','Supplier'),col('bills','Invoices','number',total=True),col('value','Purchase Value','money',total=True)]),
    dict(id='purchase-returns', title='Purchase Returns', group='Purchase Reports', description='Goods returned to suppliers', permission='reports.purchase', filters=['supplier','invoice','item','category','batch'], columns=[DATE,col('invoice','Invoice Number'),col('supplier','Supplier'),ITEM,QTY,col('value','Return Value','money',total=True),BATCH,col('reason','Reason'),col('status','Status')]),
    dict(id='current-stock', title='Current Stock', group='Inventory Reports', description='Product stock at the selected date', permission='inventory.view', filters=INVENTORY_FILTERS, columns=[ITEM,PACK,col('unit','Stock Unit'),QTY,col('reorder','Reorder Level','number'),CATEGORY,BRAND,col('mrp','MRP / unit','money',False),col('value','Stock at MRP','money',False,True)]),
    dict(id='batch-stock', title='Batch-wise Stock', group='Inventory Reports', description='Stock by product and manufactured batch', permission='inventory.view', filters=INVENTORY_FILTERS, columns=[ITEM,col('batch','Batch'),col('expiry','Expiry'),col('unit','Stock Unit'),QTY,col('supplier','Supplier',default=False),CATEGORY,BRAND,col('mrp','MRP / unit','money',False),col('value','Stock at MRP','money',False,True)]),
    dict(id='expiry', title='Expiry Report', group='Inventory Reports', description='Expired and upcoming-expiry batches', permission='inventory.view', filters=INVENTORY_FILTERS, columns=[ITEM,col('batch','Batch'),col('expiry','Expiry'),col('days','Days to Expiry','number'),col('unit','Stock Unit'),QTY,col('status','Stock Status'),CATEGORY,BRAND]),
    dict(id='low-stock', title='Low Stock', group='Inventory Reports', description='Products at or below their reorder level', permission='inventory.view', filters=['as_of','category','manufacturer','supplier','item','stock_status'], columns=[ITEM,col('unit','Stock Unit'),QTY,col('reorder','Reorder Level','number'),col('shortfall','Shortfall','number'),CATEGORY,BRAND]),
    dict(id='stock-movement', title='Stock Movement', group='Inventory Reports', description='Signed stock ledger and document references', permission='inventory.view', filters=['category','manufacturer','supplier','item','batch','movement_type'], columns=[DATE,ITEM,col('batch','Batch'),col('type','Movement'),col('unit','Stock Unit'),QTY,col('balance','Batch Balance','number'),col('reference','Reference'),col('reason','Reason',default=False)]),
    dict(id='stock-loss', title='Stock Loss / Adjustment', group='Inventory Reports', description='Stock losses and manual corrections', permission='inventory.view', filters=['category','item','batch','adjustment_type'], columns=[DATE,ITEM,col('batch','Batch'),col('type','Adjustment'),QTY,col('reference','Reference'),col('reason','Reason')]),
    dict(id='profit', title='Profit & Margin', group='Financial / Control', description='Explicit revenue, cost and gross margin report', permission='reports.financials', filters=['group_by','profit_basis']+SALES_FILTERS, columns=[DATE,col('revenue','Net Revenue','money',total=True),col('cogs','Purchase Cost','money',total=True),col('profit','Gross Profit','money',total=True),col('margin','Margin %','money'),col('stock_loss','Unsellable Return Loss','money',False,True),col('net_profit','Profit after Return Loss','money',False,True),col('mrp_margin','MRP Margin','money',False,True)]),
    dict(id='discounts', title='Discount Report', group='Financial / Control', description='Item and bill discounts by invoice', permission='reports.sales', filters=['invoice','customer','customer_id'], columns=[DATE,col('invoice','Bill Number'),col('customer','Customer'),col('item_discount','Item Discount','money',total=True),col('discount','Bill Discount','money',total=True),col('voucher','Voucher','money',False,True),VALUE]),
    dict(id='payments', title='Payment Collection', group='Financial / Control', description='Collections and refund outflows by method', permission='reports.sales', filters=['payment','group_by'], columns=[DATE,col('payment','Payment'),col('collected','Collected','money',total=True),col('refunded','Refunded','money',total=True),col('value','Net Collection','money',total=True),BILLS]),
    dict(id='void-bills', title='Void / Cancelled Bills', group='Financial / Control', description='Cancelled invoices and recorded reasons', permission='reports.sales', filters=['invoice','customer','customer_id'], columns=[DATE,col('invoice','Bill Number'),col('customer','Customer'),VALUE,col('status','Status'),col('reason','Void Reason')]),
]
PRICE_COLUMNS = [col('pack_mrp', 'MRP / pack', 'money', False),
                 {**col('purchase_rate', 'Purchase Rate / pack', 'money', False), 'permission': 'purchase.view'},
                 {**col('unit_purchase_rate', 'Purchase Rate / unit', 'money', False), 'permission': 'purchase.view'},
                 {**col('purchase_invoice', 'Purchase Invoice', default=False), 'permission': 'purchase.view'}]
for report in CATALOG:
    if report['id'] in ('current-stock', 'batch-stock', 'expiry', 'low-stock'):
        report['columns'] += PRICE_COLUMNS
from app.services import report_extra  # noqa: E402  (inventory movement + customer reports)

CATALOG += report_extra.REPORTS
BY_ID = {r['id']: r for r in CATALOG}


class ReportError(ValueError):
    pass


def catalog(user):
    return [{**r, 'columns': visible_columns(r['columns'], user)} for r in CATALOG if has_permission(user, r['permission'])]


def visible_columns(columns, user):
    return [c for c in columns if user is None or not c.get('permission') or has_permission(user, c['permission'])]


def tz_for(db):
    return ZoneInfo(business_time.timezone_name(db))


def preset_dates(period, today):
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    previous_month_end = month - timedelta(days=1)
    presets = {
        'today': (today,today), 'yesterday': (today-timedelta(days=1),today-timedelta(days=1)),
        'this_week': (monday,today), 'last_week': (monday-timedelta(days=7),monday-timedelta(days=1)),
        'this_month': (month,today), 'last_month': (previous_month_end.replace(day=1),previous_month_end),
        'last_7': (today-timedelta(days=6),today), 'last_30': (today-timedelta(days=29),today), 'last_90': (today-timedelta(days=89),today),
    }
    # quarters and the Indian financial year (April - March)
    quarter = today.replace(month=(today.month-1)//3*3+1, day=1)
    last_quarter_end = quarter - timedelta(days=1)
    fy = date(today.year if today.month >= 4 else today.year-1, 4, 1)
    presets.update({'this_quarter': (quarter,today),
                    'last_quarter': (last_quarter_end.replace(month=(last_quarter_end.month-1)//3*3+1, day=1),last_quarter_end),
                    'this_fy': (fy,today), 'last_fy': (fy.replace(year=fy.year-1),fy-timedelta(days=1))})
    if period not in presets:
        raise ReportError('Choose a valid period')
    return presets[period]


def _category_names(db) -> dict:
    from app.services import category_service
    return category_service.names(db)


def _category_codes(db) -> list:
    """Master order: active categories plus any inactive one still on a product."""
    from app.models import Category
    used=set(db.scalars(select(Item.category).distinct()))
    return [c.code for c in db.scalars(select(Category).order_by(Category.sort_order,Category.code)) if c.is_active or c.code in used]


def options(db):
    today = datetime.now(tz_for(db)).date()
    return dict(today=today.isoformat(), periods=PERIODS, groups=GROUPS,
                presets={p: [d.isoformat() for d in preset_dates(p,today)] for p,_ in PERIODS if p != 'custom'},
                # the category master in its managed order; names are what users see
                categories=_category_codes(db),
                category_names=_category_names(db),
                manufacturers=list(db.scalars(select(Item.manufacturer).where(Item.manufacturer!='').distinct().order_by(Item.manufacturer))),
                suppliers=[dict(value=str(s.id),label=s.name) for s in db.scalars(select(Supplier).order_by(Supplier.name))],
                operators=[dict(value=str(u.id),label=u.full_name or u.username) for u in db.scalars(select(User).order_by(User.username))], movements=[(m,MOVEMENT_LABELS[m]) for m in MOVEMENT_TYPES])


def parameters(db, report, raw):
    if not isinstance(raw, dict):
        raise ReportError('Parameters must be an object')
    allowed = {'period','from','to'} | set(report['filters'])
    if set(raw)-allowed:
        raise ReportError('This report does not support: '+', '.join(sorted(set(raw)-allowed)))
    if any(not isinstance(v,(str,int)) or isinstance(v,bool) for v in raw.values()):
        raise ReportError('Parameter values must be text or numbers')
    p = {k:str(v).strip() for k,v in raw.items()}
    today = datetime.now(tz_for(db)).date()
    try:
        if 'as_of' in report['filters']:
            first = last = date.fromisoformat(p.get('as_of') or today.isoformat())
            if last>today: raise ReportError('Stock as-of date cannot be in the future')
        elif p.get('period','today') == 'custom':
            first,last=date.fromisoformat(p.get('from','')),date.fromisoformat(p.get('to',''))
        else:
            first,last=preset_dates(p.get('period','today'),today)
        if first>last: raise ReportError('From date must be on or before To date')
        # Bound query cost while retaining multi-year reports.
        if (last-first).days>3660: raise ReportError('Choose a date range of up to ten years')
        start=datetime.combine(first,time.min,tz_for(db)).astimezone(timezone.utc).replace(tzinfo=None)
        end=datetime.combine(last+timedelta(days=1),time.min,tz_for(db)).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError,OverflowError) as exc:
        raise ReportError(str(exc) if isinstance(exc,ReportError) else 'Enter valid From / To dates') from exc
    enums={'group_by':{'day','week','month'},'view':{'summary','detail'},'sort':{'value','quantity','item'},
           'profit_basis':{'realized','mrp'},'show':{'sold','all'},'payment':{'','CASH','UPI','CARD','SPLIT'},'refund_method':{'','CASH','UPI','CARD'},
           'stock_status':{'','positive','zero','low','expired'},'expiry_window':{'','expired','30','60','90','180'},
           'movement_type':set(MOVEMENT_TYPES)|{''},
           'adjustment_type':{'','LOOSE','DAMAGE','EXPIRED','COUNT','ADJUSTMENT_IN','ADJUSTMENT_OUT','CUSTOMER_RETURN'}}
    for k, choices in enums.items():
        if k in p and p[k] not in choices: raise ReportError('Choose a valid '+k.replace('_',' '))
    if p.get('supplier') and not p['supplier'].isdigit(): raise ReportError('Choose a valid supplier')
    report_extra.validate(p, ReportError)
    p.update({'from':first.isoformat(),'to':last.isoformat()})
    return p,start,end


def day_of(value,tz):
    return value.replace(tzinfo=timezone.utc).astimezone(tz).date()


def bucket(value,group,tz):
    d=day_of(value,tz)
    if group=='week': d-=timedelta(days=d.weekday())
    elif group=='month': d=d.replace(day=1)
    return d.isoformat()


def matches(item,p, *, name='', batch=''):
    if p.get('category') and (item.category if item else '')!=p['category']: return False
    if p.get('manufacturer') and (item.manufacturer if item else '')!=p['manufacturer']: return False
    if p.get('item') and p['item'].casefold() not in (name or (item.name if item else '')).casefold(): return False
    if p.get('batch') and p['batch'].casefold() not in batch.casefold(): return False
    return True


def line_allocations(sale):
    yield from financials.allocations(sale)


def customer_filter(p):
    """The Customer filter: the customer picked from the live search (exact), or the
    typed text matched against name, mobile and customer ID."""
    from app.models import Customer
    if p.get('customer_id'): return Sale.customer_id==int(p['customer_id'])
    like='%'+p['customer']+'%'
    return Sale.customer_id.in_(select(Customer.id).where(or_(Customer.name.ilike(like),Customer.mobile.ilike(like),Customer.customer_id.ilike(like))))


def sales(db,p,start,end, *, cancelled=False):
    q=select(Sale).where(Sale.sale_date>=start,Sale.sale_date<end)
    q=q.where(Sale.payment_status=='CANCELLED' if cancelled else Sale.payment_status!='CANCELLED')
    if p.get('invoice'): q=q.where(Sale.invoice_no.ilike('%'+p['invoice']+'%'))
    if p.get('customer_id') or p.get('customer'): q=q.where(customer_filter(p))
    if p.get('payment'): q=q.where(Sale.payments.any(SalePayment.mode==p['payment']) if p['payment']!='SPLIT' else Sale.payment_mode=='SPLIT')
    return db.scalars(q.options(selectinload(Sale.items).joinedload(SaleItem.item),joinedload(Sale.customer),selectinload(Sale.payments)).order_by(Sale.sale_date,Sale.id)).unique().all()


def sale_rows(db,rid,p,start,end,tz,requested):
    documents=sales(db,p,start,end,cancelled=rid=='void-bills')
    group=p.get('group_by','day')
    rows=[]
    void_reasons={}
    if rid=='void-bills' and documents:
        for entry in db.scalars(select(AuditLog).where(AuditLog.entity_type=='sale',AuditLog.entity_id.in_([s.invoice_no for s in documents]),AuditLog.details.startswith('Sale voided:')).order_by(AuditLog.id)):
            void_reasons[entry.entity_id]=entry.details.removeprefix('Sale voided:').strip()
    if rid in ('bill-register','discounts','void-bills') or (rid=='sales-summary' and p.get('view')=='detail'):
        for s in documents:
            rows.append(dict(date=day_of(s.sale_date,tz).isoformat(),invoice=s.invoice_no,customer=s.customer.name if s.customer else 'Walk-in',
                             sale_type='Home delivery' if s.customer_type=='HOME_DELIVERY' else 'Walk-in',payment=s.payment_mode,
                             value=s.total,discount=s.discount,item_discount=sum((l.discount for l in s.items),Decimal(0)),voucher=s.voucher,
                             status=s.payment_status,reason=void_reasons.get(s.invoice_no,'') if rid=='void-bills' else s.notes,_drill=dict(url=f'/sales/{s.id}')))
        if rid=='discounts': rows=[r for r in rows if r['discount'] or r['item_discount'] or r['voucher']]
        return rows
    grouped={}
    chosen=set(requested or []); batch_detail=rid=='item-sales' and 'batch' in chosen; invoice_detail=rid=='item-sales' and 'invoice' in chosen; customer_detail=rid=='item-sales' and 'customer' in chosen
    detail=batch_detail or invoice_detail or customer_detail
    for s in documents:
        for l,value in line_allocations(s):
            if not matches(l.item,p,name=l.product_name,batch=l.batch_no): continue
            category=(l.item.category if l.item else '') or 'Uncategorised'
            manufacturer=(l.item.manufacturer if l.item else '') or 'Unspecified'
            d=bucket(s.sale_date,group,tz)
            if rid=='sales-summary': key=d
            elif rid=='category-sales': key=(d,category)
            elif rid=='brand-sales': key=(d,manufacturer)
            else: key=(l.item_id,l.product_name,l.pack_size,l.mrp,l.batch_no if batch_detail else '',s.id if invoice_detail else s.customer_id if customer_detail else None)
            if key not in grouped:
                grouped[key]=dict(date=d,item=l.product_name,pack=l.pack_size or f'{l.units_per_pack} {l.base_unit}',category=category,
                                  manufacturer=manufacturer,quantity=0,mrp=l.mrp,value=Decimal(0),bills=0,batch=l.batch_no if detail else '',
                                  customer=s.customer.name if s.customer else 'Walk-in',invoice=s.invoice_no if detail else '',_bill_ids=set())
            row=grouped[key];row['quantity']+=l.quantity;row['value']+=value;row['_bill_ids'].add(s.id)
            if rid=='item-sales':
                unit=financials.effective_cost(l)
                if unit is None: row['_cost_missing']=True     # unknown cost is never counted as zero
                else: row['_cost']=row.get('_cost',Decimal(0))+to_decimal(unit)*l.quantity
            row['cat:'+category]=row.get('cat:'+category,Decimal(0))+value
    if rid=='sales-summary':
        # Net sales and Profit & Margin share the same eligible refund facts.
        for fact in financials.facts(db,start,end,p):
            if 'return_id' not in fact: continue
            d=bucket(fact['date'],group,tz)
            row=grouped.setdefault(d,dict(date=d,quantity=0,value=Decimal(0),_bill_ids=set()))
            row['value']+=fact['net_sales'];row['quantity']+=fact['quantity']
            key='cat:'+(fact.get('category') or 'Uncategorised')
            row[key]=row.get(key,Decimal(0))+fact['net_sales']
        # Keep empty/non-stock invoices in bill counts and total reconciliation.
        for s in documents:
            if not s.items:
                d=bucket(s.sale_date,group,tz)
                row=grouped.setdefault(d,dict(date=d,quantity=0,value=Decimal(0),_bill_ids=set()))
                row['value']+=s.total;row['cat:Uncategorised']=row.get('cat:Uncategorised',Decimal(0))+s.total;row['_bill_ids'].add(s.id)
    for row in grouped.values():
        row['bills']=len(row.pop('_bill_ids'))
        if rid=='item-sales':
            missing=row.pop('_cost_missing',False);cost=row.pop('_cost',Decimal(0))
            row['cost']=None if missing else money(cost)
            row['profit']=None if missing else money(row['value']-cost)
        if rid=='sales-summary':
            start_date=date.fromisoformat(row['date'])
            end_date=start_date if group=='day' else start_date+timedelta(days=6) if group=='week' else (start_date.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)
            row['_drill']=dict(report='bill-register' if row['bills'] else 'sales-returns',params=dict(period='custom',**{'from':max(start_date,date.fromisoformat(p['from'])).isoformat(),'to':min(end_date,date.fromisoformat(p['to'])).isoformat()}))
        elif rid in ('category-sales','brand-sales'):
            filters={k:v for k,v in p.items() if k in SALES_FILTERS}
            filters.update({('category' if rid=='category-sales' else 'manufacturer'):row.get('category_code',row['category']) if rid=='category-sales' else row['manufacturer']})
            row['_drill']=dict(report='item-sales',params=dict(period='custom',**{'from':p['from'],'to':p['to']},**filters))
    rows=list(grouped.values())
    if rid=='item-sales' and p.get('show')=='all':
        present={r['item'] for r in rows}
        for item in db.scalars(select(Item).where(Item.deleted_at.is_(None)).order_by(Item.name)):
            if item.name not in present and matches(item,p) and not p.get('batch'):
                rows.append(dict(item=item.name,pack=item.pack_size,quantity=0,mrp=Decimal(0),value=Decimal(0),category=item.category,manufacturer=item.manufacturer))
    if rid=='item-sales':
        sort=p.get('sort','value'); rows.sort(key=lambda r:r['item'].casefold() if sort=='item' else -r.get(sort,0))
    else: rows.sort(key=lambda r:(r['date'],r.get('category','') if rid=='category-sales' else r.get('manufacturer','')))
    return rows


def purchase_rows(db,rid,p,start,end,tz):
    if rid=='purchase-returns':
        q=select(PurchaseReturn).where(PurchaseReturn.return_date>=start,PurchaseReturn.return_date<end)
        if p.get('supplier'): q=q.where(PurchaseReturn.supplier_id==int(p['supplier']))
        rows=[]
        for ret,purchase,batch in db.execute(q.add_columns(Purchase,Batch).outerjoin(Purchase,Purchase.id==PurchaseReturn.purchase_id).outerjoin(Batch,Batch.id==PurchaseReturn.batch_id).options(joinedload(PurchaseReturn.item),joinedload(PurchaseReturn.supplier))).unique():
            invoice=purchase.invoice_no if purchase else ''
            if p.get('invoice') and p['invoice'].casefold() not in invoice.casefold(): continue
            if not matches(ret.item,p,name=ret.product_name,batch=batch.batch_no if batch else ''): continue
            rows.append(dict(date=day_of(ret.return_date,tz).isoformat(),invoice=invoice,supplier=ret.supplier.name if ret.supplier else 'Unspecified',item=ret.product_name,quantity=ret.quantity,value=ret.value,batch=batch.batch_no if batch else '',reason=ret.reason,status=ret.status))
        return rows
    q=select(Purchase).where(Purchase.purchase_date>=start,Purchase.purchase_date<end,Purchase.status.in_(('POSTED','PARTIAL')))
    if p.get('supplier'): q=q.where(Purchase.supplier_id==int(p['supplier']))
    if p.get('invoice'): q=q.where(Purchase.invoice_no.ilike('%'+p['invoice']+'%'))
    docs=db.scalars(q.options(joinedload(Purchase.supplier),selectinload(Purchase.items).joinedload(PurchaseItem.item)).order_by(Purchase.purchase_date,Purchase.id)).unique()
    grouped={};rows=[]
    for doc in docs:
        value=to_decimal(doc.total)
        if p.get('item') or p.get('category'):
            total_lines=sum((to_decimal(l.line_total) for l in doc.items),Decimal(0)); allocated=Decimal(0);value=Decimal(0)
            for i,l in enumerate(doc.items):
                v=money(doc.total-allocated) if i==len(doc.items)-1 else money(doc.total*l.line_total/total_lines) if total_lines else Decimal(0)
                allocated+=v
                if matches(l.item,p,name=l.product_name): value+=v
            if not any(matches(l.item,p,name=l.product_name) for l in doc.items): continue
        d=bucket(doc.purchase_date,p.get('group_by','day'),tz);supplier=doc.supplier.name if doc.supplier else 'Unspecified'
        if rid=='purchase-register': rows.append(dict(date=day_of(doc.purchase_date,tz).isoformat(),invoice=doc.invoice_no,supplier=supplier,value=value,status=doc.status,_drill=dict(url=f'/purchases/{doc.id}')));continue
        key=(doc.supplier_id,supplier) if rid=='supplier-purchases' else d
        row=grouped.setdefault(key,dict(date=d,supplier=supplier,bills=0,value=Decimal(0)))
        row['bills']+=1;row['value']+=value
        if rid=='supplier-purchases' and doc.supplier_id:
            row['_drill']=dict(report='purchase-register',params={k:v for k,v in p.items() if k!='group_by'}|{'supplier':str(doc.supplier_id),'period':'custom'})
    return rows or sorted(grouped.values(),key=lambda r:r.get('supplier','') if rid=='supplier-purchases' else r['date'])


def stock_rows(db,rid,p,end,tz):
    # Ledger sums are the source of truth, including returns and void reversals.
    balances=select(InventoryMovement.batch_id,func.sum(InventoryMovement.quantity).label('stock')).where(InventoryMovement.created_at<end).group_by(InventoryMovement.batch_id).subquery()
    q=select(Batch,Item,func.coalesce(balances.c.stock,0)).join(Item,Item.id==Batch.item_id).outerjoin(balances,balances.c.batch_id==Batch.id).where(Batch.created_at<end)
    if p.get('supplier'): q=q.where(Batch.supplier_id==int(p['supplier']))
    as_of=date.fromisoformat(p['to']);rows=[];grouped={}
    pricing_groups=defaultdict(list)
    default_reorder=settings_service.get_int(db,'low_stock_threshold',5)
    records=list(db.execute(q.options(joinedload(Batch.supplier)).order_by(Item.name,Batch.batch_no)).unique())
    prices=inventory_pricing.batch_prices(db, [b for b,_,_ in records], end)
    for batch,item,quantity in records:
        if not matches(item,p,batch=batch.batch_no): continue
        # An expiry month is sellable until its last day.
        expiry=(batch.expiry_date.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1) if batch.expiry_date else None
        days=(expiry-as_of).days if expiry else None
        window=p.get('expiry_window','')
        if window=='expired' and (days is None or days>=0): continue
        if window.isdigit() and (days is None or days<0 or days>int(window)): continue
        if rid=='expiry' and not window and (days is None or days>settings_service.get_int(db,'expiry_threshold_days',90)): continue
        if p.get('stock_status')=='expired' and (days is None or days>=0): continue
        if rid=='expiry' and quantity<=0: continue
        reorder=item.reorder_level or default_reorder
        row=dict(item=item.name,pack=item.pack_size or f'{item.units_per_pack} {item.base_unit}',unit=item.base_unit.lower(),quantity=int(quantity),reorder=reorder,category=item.category,manufacturer=item.manufacturer,batch=batch.batch_no,
                 expiry=batch.expiry_date.isoformat() if batch.expiry_date else '',days=days,status='Expired' if days is not None and days<0 else 'Sellable',supplier=batch.supplier.name if batch.supplier else '',mrp=batch.unit_mrp,value=money(to_decimal(batch.unit_mrp)*quantity))
        row.update(prices[batch.id])
        if rid in ('current-stock','low-stock'):
            acc=grouped.setdefault(item.id,{**row,'quantity':0,'value':Decimal(0)})
            acc['quantity']+=int(quantity);acc['value']+=row['value']
            # MRP can differ by batch; retain the common rate only.
            pricing_groups[item.id].append((row, int(quantity)))
        else: rows.append(row)
    if rid in ('current-stock','low-stock'):
        for item_id, acc in grouped.items():
            candidates=pricing_groups[item_id]
            active=[row for row, qty in candidates if qty > 0] or [row for row, _ in candidates]
            for key in ('mrp','pack_mrp','purchase_rate','unit_purchase_rate','purchase_invoice'):
                values={row[key] for row in active}
                acc[key]=next(iter(values)) if len(values)==1 else None
        rows=list(grouped.values())
        # Items with no received batch are still out of stock / below reorder.
        if not p.get('supplier') and not p.get('batch') and not p.get('expiry_window') and p.get('stock_status')!='expired':
            for item in db.scalars(select(Item).where(Item.created_at<end,Item.deleted_at.is_(None),~Item.batches.any())):
                if not matches(item,p): continue
                rows.append(dict(item=item.name,pack=item.pack_size,unit=item.base_unit.lower(),quantity=0,reorder=item.reorder_level or default_reorder,category=item.category,manufacturer=item.manufacturer,mrp=None,pack_mrp=item.mrp,purchase_rate=None,unit_purchase_rate=None,purchase_invoice='',value=Decimal(0)))
    if rid=='low-stock': rows=[r for r in rows if r['quantity']<=r['reorder']]
    status=p.get('stock_status')
    if status=='positive': rows=[r for r in rows if r['quantity']>0]
    elif status=='zero': rows=[r for r in rows if r['quantity']==0]
    elif status=='low': rows=[r for r in rows if r['quantity']<=r['reorder']]
    for row in rows: row['shortfall']=max(0,row['reorder']-row['quantity'])
    return rows


def movement_rows(db,rid,p,start,end,tz):
    q=select(InventoryMovement).where(InventoryMovement.created_at>=start,InventoryMovement.created_at<end)
    if rid=='stock-loss':
        q=q.where(InventoryMovement.movement_type.in_(['ADJUSTMENT_IN','ADJUSTMENT_OUT']))
    if p.get('movement_type'): q=q.where(InventoryMovement.movement_type==p['movement_type'])
    rows=[]
    for m in db.scalars(q.options(joinedload(InventoryMovement.item),joinedload(InventoryMovement.batch)).order_by(InventoryMovement.created_at,InventoryMovement.id)).unique():
        if not matches(m.item,p,batch=m.batch.batch_no): continue
        if p.get('supplier') and m.batch.supplier_id!=int(p['supplier']): continue
        adjustment=p.get('adjustment_type')
        if adjustment and adjustment not in (m.movement_type,m.reason.split(':')[0].upper()) and not {'LOOSE':'LOOSE','DAMAGE':'DAMAG','EXPIRED':'EXPIR','COUNT':'COUNT'}.get(adjustment,'!').casefold() in m.reason.casefold(): continue
        rows.append(dict(date=m.created_at.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M'),item=m.item.name,batch=m.batch.batch_no,type=m.movement_type,unit=m.item.base_unit.lower(),quantity=m.quantity,balance=m.balance_after,reference=m.reference_no or f'{m.reference_type} {m.reference_id or ""}',reason=m.reason))
    if rid=='stock-loss':
        # Unsellable customer returns do not create a stock movement, but are losses.
        q=select(SaleReturnItem,SaleReturn).join(SaleReturn).where(SaleReturn.status=='COMPLETED',SaleReturn.processed_at>=start,SaleReturn.processed_at<end,SaleReturnItem.disposition!='RESTOCK')
        for line,ret in db.execute(q):
            item=db.get(Item,line.item_id) if line.item_id else None
            if not matches(item,p,name=line.product_name,batch=line.batch_no): continue
            if p.get('supplier') and (not line.batch_id or db.get(Batch,line.batch_id).supplier_id!=int(p['supplier'])): continue
            if p.get('adjustment_type') and p['adjustment_type'] not in (line.disposition,'CUSTOMER_RETURN'): continue
            rows.append(dict(date=ret.processed_at.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%Y-%m-%d %H:%M'),item=line.product_name,batch=line.batch_no,type=line.disposition,quantity=-line.quantity,reference=ret.return_no,reason=ret.reason_note or ret.reason_code))
    return rows


def return_rows(db,p,start,end,tz):
    q=select(SaleReturn).where(SaleReturn.status=='COMPLETED',SaleReturn.processed_at>=start,SaleReturn.processed_at<end)
    if p.get('refund_method'): q=q.where(SaleReturn.refund_method==p['refund_method'])
    rows=[]
    for ret in db.scalars(q.options(joinedload(SaleReturn.sale),selectinload(SaleReturn.items)).order_by(SaleReturn.processed_at)).unique():
        for l in ret.items:
            item=db.get(Item,l.item_id) if l.item_id else None
            if not matches(item,p,name=l.product_name,batch=l.batch_no): continue
            rows.append(dict(date=day_of(ret.processed_at,tz).isoformat(),reference=ret.return_no,invoice=ret.sale.invoice_no,item=l.product_name,quantity=l.quantity,value=l.refund_amount,disposition=l.disposition,batch=l.batch_no,reason=ret.reason_note or ret.reason_code,_drill=dict(url=f'/sales/{ret.sale_id}')))
    return rows


def finance_rows(db,rid,p,start,end,tz):
    if rid=='profit':
        groups=defaultdict(list)
        basis=p.get('profit_basis') or settings_service.get_setting(db,'profit_basis','realized')
        for fact in financials.facts(db,start,end,p): groups[bucket(fact['date'],p.get('group_by','day'),tz)].append(fact)
        out=[]
        for day,rows in sorted(groups.items()):
            a=financials.aggregate(rows,basis)
            # profit = sales value - purchase cost, over the lines whose cost is known
            # (Costed Sales); lines without a purchase rate are counted, never guessed
            loss=a['costed_stock_loss']
            out.append(dict(date=day,revenue=a['revenue'],costed_revenue=a['costed_revenue'],cogs=a['costed_cogs'],profit=a['costed_profit'],
                            margin=a['costed_margin'],stock_loss=loss,net_profit=money(a['costed_profit']-loss) if a['costed_profit'] is not None else None,
                            mrp_margin=a['costed_mrp_margin'],missing_cost_lines=a['missing_cost_lines'],
                            financial_status='Complete' if not a['missing_cost_lines'] else 'Partial' if a['costed_cogs'] is not None else 'No cost'))
        return out
    grouped={};group=p.get('group_by','day')
    for s in sales(db,{},start,end):
        d=bucket(s.sale_date,group,tz)
        if rid=='profit':
            row=grouped.setdefault(d,dict(date=d,revenue=Decimal(0),cogs=Decimal(0)))
            row['revenue']+=s.total;row['cogs']+=sum((to_decimal(l.cost_rate)*l.quantity for l in s.items),Decimal(0))
        else:
            for payment in s.payments:
                if p.get('payment') and payment.mode!=p['payment']: continue
                row=grouped.setdefault((d,payment.mode),dict(date=d,payment=payment.mode,collected=Decimal(0),refunded=Decimal(0),bills=0))
                row['collected']+=payment.amount;row['bills']+=1
    q=select(SaleReturn).where(SaleReturn.status=='COMPLETED',SaleReturn.processed_at>=start,SaleReturn.processed_at<end).options(selectinload(SaleReturn.items))
    cost_ids={l.sale_item_id for ret in db.scalars(q) for l in ret.items if l.disposition=='RESTOCK' and l.sale_item_id}
    costs={l.id:l.cost_rate for l in db.scalars(select(SaleItem).where(SaleItem.id.in_(cost_ids)))} if cost_ids else {}
    for ret in db.scalars(q):
        d=bucket(ret.processed_at,group,tz)
        if rid=='profit':
            row=grouped.setdefault(d,dict(date=d,revenue=Decimal(0),cogs=Decimal(0)))
            row['revenue']-=ret.total_refund;row['cogs']-=sum((to_decimal(costs.get(l.sale_item_id,0))*l.quantity for l in ret.items if l.disposition=='RESTOCK'),Decimal(0))
        elif not p.get('payment') or p['payment']==ret.refund_method:
            row=grouped.setdefault((d,ret.refund_method),dict(date=d,payment=ret.refund_method,collected=Decimal(0),refunded=Decimal(0),bills=0))
            row['refunded']+=ret.total_refund
    for row in grouped.values():
        if rid=='profit': row.update(profit=money(row['revenue']-row['cogs']),margin=money((row['revenue']-row['cogs'])*100/row['revenue']) if row['revenue'] else Decimal(0))
        else: row['value']=row['collected']-row['refunded']
    return sorted(grouped.values(),key=lambda r:(r['date'],r.get('payment','')))


def generate(db,rid,raw,selected=None,user=None):
    report=BY_ID.get(rid)
    if report is None: raise ReportError('Unknown report')
    if selected is not None and (not isinstance(selected,list) or not selected or any(not isinstance(k,str) for k in selected)):
        raise ReportError('Select at least one valid column')
    p,start,end=parameters(db,report,raw);tz=tz_for(db)
    if rid=='profit': p.setdefault('profit_basis',settings_service.get_setting(db,'profit_basis','realized'))
    columns=visible_columns(report['columns'],user);note=''
    if p.get('category'):
        from app.services.inventory_service import categories
        known={c.upper():c for c in set(categories(db))|set(db.scalars(select(Item.category).distinct()))}
        wanted=str(p['category']).strip().upper().replace(' ','_')
        if wanted not in known: raise ReportError(f"Unknown category {p['category']!r}")
        p['category']=known[wanted]
    if rid=='sales-summary' and p.get('view')=='detail':
        columns=BY_ID['bill-register']['columns']
    if rid in report_extra.IDS: rows,_=report_extra.rows(db,rid,p,start,end,tz,_category_names(db))
    elif rid.startswith('purchase') or rid=='supplier-purchases': rows=purchase_rows(db,rid,p,start,end,tz)
    elif rid in ('current-stock','batch-stock','expiry','low-stock'):
        rows=stock_rows(db,rid,p,end,tz)
    elif rid in ('stock-movement','stock-loss'): rows=movement_rows(db,rid,p,start,end,tz)
    elif rid=='sales-returns': rows=return_rows(db,p,start,end,tz)
    elif rid in ('profit','payments'): rows=finance_rows(db,rid,p,start,end,tz)
    else: rows=sale_rows(db,rid,p,start,end,tz,selected)
    if rid=='sales-summary' and p.get('view','summary')=='summary':
        names=_category_names(db)
        present={key[4:] for row in rows for key in row if key.startswith('cat:')}
        used=set(db.scalars(select(Item.category).where(Item.category!='').distinct()))|present
        ordered=[c for c in _category_codes(db) if c in used]+sorted(used-set(_category_codes(db)))
        columns=[DATE]+[col('cat:'+code,names.get(code,code),'money',total=True) for code in ordered]+[col('value','Total','money',total=True),BILLS]
    if selected:
        allowed={c['key'] for c in columns}
        if set(selected)-allowed: raise ReportError('Unknown columns: '+', '.join(sorted(set(selected)-allowed)))
        displayed=[c for c in columns if c['key'] in selected]
    else:
        displayed=[c for c in columns if c['default']]
        if rid=='purchase-gst' and rows:   # grouped views: leave out the per-line columns they do not fill
            filled=[c for c in displayed if any(r.get(c['key']) not in ('',None) for r in rows)]
            displayed=filled or displayed
    if rid=='item-wise-sales':   # a split or batch level is meaningless without its column
        keys_shown={c['key'] for c in displayed}
        for key,on in (('batch',p.get('level')=='batch'),('period',bool(p.get('split')))):
            if on and key not in keys_shown: displayed.insert(0 if key=='period' else 3,next(c for c in columns if c['key']==key))
    totals={c['key']:sum((to_decimal(r.get(c['key']) or 0) for r in rows),Decimal(0)) for c in displayed if c['total']}
    for key in ('cost','profit'):   # one unknown cost makes the total unknown too
        if rid in ('item-sales','item-wise-sales') and key in totals and any(r.get(key) is None for r in rows): totals[key]=None
    if rid=='profit':
        for key in ('cogs','profit','stock_loss','net_profit','mrp_margin'):
            if key in totals and all(r[key] is None for r in rows): totals[key]=None
        if 'margin' in [c['key'] for c in displayed]:
            costed=sum((r['costed_revenue'] for r in rows),Decimal(0))
            profit=sum((r['profit'] for r in rows if r['profit'] is not None),Decimal(0))
            totals['margin']=money(profit*100/costed) if costed else None
        if p.get('profit_basis')=='mrp':
            columns=[{**c,'label':'MRP Sales Value'} if c['key']=='revenue' else c for c in columns]
            displayed=[{**c,'label':'MRP Sales Value'} if c['key']=='revenue' else c for c in displayed]
    names=_category_names(db)
    for row in rows:
        if row.get('category') and 'category_code' not in row:
            row['category_code']=row['category']; row['category']=names.get(row['category'],row['category'])
    # Only requested fields cross the API; optional customer/bill data stays hidden.
    clean=[]
    for row in rows:
        values={}
        for c in displayed:
            if c['key'] in row:
                values[c['key']]=row[c['key']]
            else:
                values[c['key']]=0 if c['kind'] in ('money','number') else ''
        if '_drill' in row: values['_drill']=row['_drill']
        clean.append(values)
    footer=[]
    if rid=='sales-summary' and p.get('view','summary')=='summary' and {'value','bills'}<=set(totals):
        footer=[dict(label='Average bill value',value=money(totals['value']/totals['bills']) if totals['bills'] else Decimal(0))]
    profile=settings_service.get_profile(db)
    return dict(id=rid,title=report['title'],pharmacy=profile.get('pharmacy_name') or 'FAHEEM PHARMACY',
                parameters=p,from_date=p['from'],to_date=p['to'],generated_at=datetime.now(tz).strftime('%d-%b-%Y %I:%M %p'),
                columns=displayed,available_columns=columns,rows=clean,totals=totals,footer=footer,note=note,
                data_quality={})
