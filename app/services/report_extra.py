"""Inventory movement and customer reports (plugged into report_generator).

Every figure is read from finalized transactions in SQL: voided bills are
excluded, completed refunds are subtracted, returned quantities do not count
as sales activity, and quantities are in each product's base unit (the shared
UOM of the stock ledger), so strips and loose tablets are never mixed.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select

from app.models import (FOLLOWUP_REASONS, Batch, Customer, CustomerFollowUp, Item, Sale, SaleItem, SaleReturn, SaleReturnItem,
                        Supplier, User)
from app.services import units
from app.utils import money, to_decimal


def col(key, label, kind='text', default=True, total=False):
    return dict(key=key, label=label, kind=kind, default=default, total=total)


INACTIVE = {'7': 7, '15': 15, '30': 30, '60': 60, '90': 90, '180': 180}
ENUMS = {'inactive_days': set(INACTIVE) | {'custom'}, 'top': {'10', '25', '50', '100', 'all'},
         'rank_by': {'quantity', 'value', 'bills'}, 'exclude_no_mobile': {'', '1'},
         'exclude_open_followup': {'', '1'}, 'followup_scope': {'overdue', 'today', 'next7', 'open', 'all'},
         'split': {'', 'day', 'week', 'month'}, 'level': {'item', 'batch'},
         'gst_view': {'line', 'invoice', 'item', 'rate', 'hsn', 'supplier', 'month'}}
INTS = ('inactive_custom', 'min_bills', 'operator', 'customer_id')
NUMBERS = ('min_value', 'gst_rate')

CUSTOMER = col('customer', 'Customer')
MOBILE = col('mobile', 'Mobile')
REPORTS = [
    dict(id='non-moving', title='Non-Moving Items', group='Inventory Reports', permission='inventory.view',
         description='Stock with no sale in the chosen inactivity period',
         filters=['inactive_days', 'inactive_custom', 'category', 'supplier', 'manufacturer', 'batch', 'expiry_window'],
         columns=[col('item', 'Product'), col('category', 'Category'), col('batch', 'Batch'), col('supplier', 'Supplier'),
                  col('quantity', 'Stock', 'number', total=True), col('unit', 'Unit', default=False), col('rate', 'Rate', 'money'),
                  col('mrp', 'MRP', 'money'), col('last_sold', 'Last Sold'), col('days_idle', 'Days Since Sale', 'number', False),
                  col('expiry', 'Expiry', default=False),
                  {**col('cost_value', 'Stock Value at Cost', 'money', False, True), 'permission': 'reports.financials'},
                  col('mrp_value', 'Potential MRP Value', 'money', False, True)]),
    dict(id='item-wise-sales', title='Item-wise Sales', group='Inventory Reports', permission='inventory.view',
         description='Quantity and value sold per product or batch, net of returns',
         filters=['split', 'level', 'category', 'manufacturer', 'supplier', 'item', 'batch', 'sort', 'show'],
         columns=[col('period', 'Period', default=False), col('code', 'Code'), col('item', 'Product'), col('pack', 'Pack'),
                  col('category', 'Category', default=False), col('manufacturer', 'Manufacturer', default=False),
                  col('batch', 'Batch', default=False), col('expiry', 'Expiry', default=False), col('unit', 'Unit', default=False),
                  col('sold', 'Qty Sold', 'number', total=True), col('returned', 'Qty Returned', 'number', total=True),
                  col('net_qty', 'Net Qty', 'number', total=True), col('packs', 'Net Qty (packs)', default=False),
                  col('mrp', 'MRP / Pack', 'money'), col('gross', 'MRP Value', 'money', False, True),
                  col('discount', 'Discount', 'money', False, True), col('value', 'Sales Value', 'money', total=True),
                  col('refund', 'Refunds', 'money', total=True), col('net_value', 'Net Sales', 'money', total=True),
                  col('bills', 'Bills', 'number'), col('customers', 'Customers', 'number', False),
                  col('first_sold', 'First Sale', default=False), col('last_sold', 'Last Sale'),
                  col('stock', 'Current Stock', 'number'),
                  {**col('cost', 'Purchase Cost', 'money', False, True), 'permission': 'reports.financials'},
                  {**col('profit', 'Profit', 'money', False, True), 'permission': 'reports.financials'}]),
    dict(id='purchase-gst', title='Purchase GST', group='Purchase Reports', permission='reports.purchase',
         description='GST paid to suppliers: CGST / SGST / IGST, rate before and after GST, returns reversed',
         filters=['gst_view', 'supplier', 'item', 'gst_rate', 'invoice'],
         columns=[col('date', 'Date'), col('reference', 'Document', default=False), col('invoice', 'Supplier Invoice'),
                  col('supplier', 'Supplier'), col('gstin', 'Supplier GSTIN', default=False), col('item', 'Product'),
                  col('hsn', 'HSN'), col('packs', 'Packs', 'number', total=True), col('gst_rate', 'GST %'),
                  col('taxable', 'Taxable Value', 'money', total=True), col('cgst', 'CGST', 'money', total=True),
                  col('sgst', 'SGST', 'money', total=True), col('igst', 'IGST', 'money', total=True),
                  col('gst', 'Total GST', 'money', total=True), col('landed', 'Value incl. GST', 'money', total=True),
                  col('rate_ex', 'Rate / Pack before GST', 'money', False), col('rate_incl', 'Rate / Pack incl. GST', 'money'),
                  col('lines', 'Lines', 'number', False), col('note', 'Note', default=False)]),
    dict(id='top-moving', title='Top Moving Items', group='Inventory Reports', permission='inventory.view',
         description='Products ranked by quantity, value or bills',
         filters=['category', 'supplier', 'manufacturer', 'top', 'rank_by'],
         columns=[col('rank', 'Rank', 'number'), col('item', 'Product'), col('category', 'Category'),
                  col('quantity', 'Qty Sold', 'number', total=True), col('unit', 'Unit', default=False), col('bills', 'Bills', 'number'),
                  col('value', 'Sales Value', 'money', total=True), col('stock', 'Current Stock', 'number')]),
    dict(id='frequent-customers', title='Frequent Customers', group='Customer Reports', permission='customers.view',
         description='Customers with the most bills in the period', filters=['min_bills', 'min_value'],
         columns=[CUSTOMER, MOBILE, col('bills', 'Bills', 'number', total=True), col('value', 'Total Sales', 'money', total=True),
                  col('average', 'Avg Bill', 'money'), col('last_visit', 'Last Visit')]),
    dict(id='customer-history', title='Customer Purchase History', group='Customer Reports', permission='customers.view',
         description='Every bill of one customer', filters=['customer', 'customer_id'],
         columns=[col('date', 'Date'), col('invoice', 'Invoice'), col('items', 'Items', 'number'), col('gross', 'Gross', 'money', total=True),
                  col('discount', 'Discount', 'money', total=True), col('value', 'Net', 'money', total=True),
                  col('returns', 'Returns', 'money', total=True), col('retained', 'Final Retained Sale', 'money', total=True)]),
    dict(id='customer-recovery', title='Customer Recovery', group='Customer Reports', permission='customers.view',
         description='Known customers who have not come back', filters=['inactive_days', 'inactive_custom', 'min_bills', 'min_value',
                                                                         'exclude_no_mobile', 'exclude_open_followup'],
         columns=[CUSTOMER, MOBILE, col('last_visit', 'Last Visit'), col('days_away', 'Days Away', 'number'),
                  col('last_invoice', 'Last Invoice'), col('bills', 'Bills', 'number', total=True),
                  col('value', 'Total Sales', 'money', total=True), col('last_value', 'Last Purchase Value', 'money', False),
                  col('open_followup', 'Open Follow-up', default=False)]),
    dict(id='top-customers', title='Top Customers by Sales', group='Customer Reports', permission='customers.view',
         description='Customers ranked by net sales', filters=['top'],
         columns=[col('rank', 'Rank', 'number'), CUSTOMER, MOBILE, col('bills', 'Bills', 'number', total=True),
                  col('gross', 'Gross Sales', 'money', total=True), col('discount', 'Discount', 'money', total=True),
                  col('value', 'Net Sales', 'money', total=True), col('average', 'Average Bill', 'money'), col('last_visit', 'Last Visit')]),
    dict(id='new-customers', title='New Customers', group='Customer Reports', permission='customers.view',
         description='First purchase within the period', filters=[],
         columns=[CUSTOMER, MOBILE, col('first_date', 'First Purchase'), col('first_invoice', 'First Invoice'),
                  col('first_value', 'First Bill Value', 'money', total=True), col('bills', 'Bills Since', 'number', total=True),
                  col('value', 'Sales Since', 'money', total=True)]),
    dict(id='customer-returns', title='Customer Returns / Refunds', group='Customer Reports', permission='customers.view',
         description='Returns and refunds by customer', filters=['customer', 'customer_id'],
         columns=[CUSTOMER, col('invoice', 'Invoice'), col('return_no', 'Return Ref'), col('date', 'Date'),
                  col('items', 'Returned Items'), col('quantity', 'Qty', 'number', total=True), col('value', 'Refund Value', 'money', total=True),
                  col('method', 'Method'), col('reason', 'Reason'), col('operator', 'Operator')]),
    dict(id='followups-due', title='Follow-up Due', group='Customer Reports', permission='customers.view',
         description='Overdue, today and upcoming follow-ups', filters=['followup_scope', 'operator'],
         columns=[CUSTOMER, MOBILE, col('due', 'Due Date'), col('reason', 'Reason'), col('invoice', 'Source Invoice'),
                  col('created_by', 'Created By'), col('status', 'Status')]),
    dict(id='followup-performance', title='Follow-up Performance', group='Customer Reports', permission='customers.view',
         description='Follow-ups created and completed by operator', filters=[],
         columns=[col('operator', 'Operator'), col('created', 'Created', 'number', total=True), col('completed', 'Completed', 'number', total=True),
                  col('on_time', 'Completed On Time', 'number', total=True), col('cancelled', 'Cancelled', 'number', total=True),
                  col('open', 'Still Open', 'number', total=True), col('overdue', 'Overdue', 'number', total=True)]),
]
IDS = {r['id'] for r in REPORTS}
FILTER_NAMES = set(ENUMS) | set(INTS) | set(NUMBERS)


def validate(p: dict, ReportError) -> None:
    for k, choices in ENUMS.items():
        if k in p and p[k] not in choices:
            raise ReportError('Choose a valid ' + k.replace('_', ' '))
    for k in INTS:
        if p.get(k) and not p[k].isdigit():
            raise ReportError(f'{k.replace("_", " ").title()} must be a whole number')
    for k in NUMBERS:
        if p.get(k):
            try:
                Decimal(p[k])
            except Exception:
                raise ReportError(f'{k.replace("_", " ").title()} must be a number')


def _day(value, tz):
    if not value:
        return ''
    return value.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%d-%b-%Y')


def _inactive_days(p):
    if p.get('inactive_days') == 'custom':
        return int(p.get('inactive_custom') or 0) or 30
    return INACTIVE.get(p.get('inactive_days') or '', 90)


def _utc(d: date, tz) -> datetime:
    return datetime.combine(d, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)


def _net_sold_lines(end):
    """Sale lines of finalized bills with quantity net of returns (only lines still sold count)."""
    returned = (select(SaleReturnItem.sale_item_id.label('sid'), func.sum(SaleReturnItem.quantity).label('rq'))
                .join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id).where(SaleReturn.status == 'COMPLETED')
                .group_by(SaleReturnItem.sale_item_id).subquery())
    net = (SaleItem.quantity - func.coalesce(returned.c.rq, 0)).label('net_qty')
    return (select(SaleItem.id, SaleItem.item_id, SaleItem.batch_id, SaleItem.sale_id, Sale.sale_date, net,
                   SaleItem.net_sale_value, SaleItem.line_total, SaleItem.quantity)
            .join(Sale, Sale.id == SaleItem.sale_id).outerjoin(returned, returned.c.sid == SaleItem.id)
            .where(Sale.payment_status != 'CANCELLED', Sale.sale_date < end, SaleItem.item_id.is_not(None)))


# --------------------------------------------------------------------------- inventory movement
def non_moving(db, p, start, end, tz, names):
    days = _inactive_days(p)
    to_day = date.fromisoformat(p['to'])
    since = _utc(to_day - timedelta(days=days - 1), tz)
    sold = _net_sold_lines(end).subquery()
    last = dict(db.execute(select(sold.c.item_id, func.max(sold.c.sale_date)).where(sold.c.net_qty > 0).group_by(sold.c.item_id)).all())
    active = {iid for iid, when in last.items() if when >= since}
    stmt = (select(Batch, Item, Supplier.name).join(Item, Item.id == Batch.item_id).outerjoin(Supplier, Supplier.id == Batch.supplier_id)
            .where(Item.deleted_at.is_(None)))
    stmt = stmt.where(Batch.quantity > 0)          # non-moving means stock is on hand
    if p.get('category'):
        stmt = stmt.where(Item.category == p['category'])
    if p.get('manufacturer'):
        stmt = stmt.where(Item.manufacturer == p['manufacturer'])
    if p.get('supplier'):
        stmt = stmt.where(Batch.supplier_id == int(p['supplier']))
    if p.get('batch'):
        stmt = stmt.where(Batch.batch_no.ilike(f"%{p['batch']}%"))
    window = p.get('expiry_window')
    if window == 'expired':
        stmt = stmt.where(Batch.expiry_date < to_day.replace(day=1))
    elif window:
        stmt = stmt.where(Batch.expiry_date <= to_day + timedelta(days=int(window)))
    rows = []
    for b, it, sup in db.execute(stmt.order_by(Item.name, Batch.expiry_date)):
        if it.id in active:
            continue
        when = last.get(it.id)
        idle = (to_day - when.replace(tzinfo=timezone.utc).astimezone(tz).date()).days if when else None
        upp = b.units_per_pack or 1
        rows.append(dict(item=it.name, category=names.get(it.category, it.category), category_code=it.category, batch=b.batch_no,
                         supplier=sup or '', quantity=b.quantity, unit=(it.base_unit or 'UNIT').lower(), rate=money(b.purchase_rate),
                         mrp=money(b.mrp), last_sold=_day(when, tz) if when else 'Never', days_idle=idle if idle is not None else '',
                         expiry=b.expiry_date.strftime('%b-%Y') if b.expiry_date else '',
                         cost_value=money(to_decimal(b.unit_cost) * b.quantity) if to_decimal(b.unit_cost) > 0 else None,
                         mrp_value=units.line_amount(b.mrp, upp, b.quantity)))
    rows.sort(key=lambda r: (r['last_sold'] != 'Never', -(r['days_idle'] or 10 ** 6) if r['days_idle'] != '' else 0, r['item']))
    note = (f'Non-moving: stock on hand with no sale in the {days} days up to {to_day:%d-%b-%Y}. Returned or voided quantities are '
            'not sales activity. Quantities are in each product’s base unit.')
    return rows, note


def top_moving(db, p, start, end, tz, names):
    sold = _net_sold_lines(end).where(Sale.sale_date >= start).subquery()
    stmt = (select(Item, func.sum(sold.c.net_qty), func.count(func.distinct(sold.c.sale_id)),
                   func.sum(func.coalesce(sold.c.net_sale_value, sold.c.line_total) * sold.c.net_qty / sold.c.quantity))
            .join(sold, sold.c.item_id == Item.id).where(sold.c.net_qty > 0).group_by(Item.id))
    if p.get('category'):
        stmt = stmt.where(Item.category == p['category'])
    if p.get('manufacturer'):
        stmt = stmt.where(Item.manufacturer == p['manufacturer'])
    if p.get('supplier'):
        stmt = stmt.where(Item.id.in_(select(Batch.item_id).where(Batch.supplier_id == int(p['supplier']))))
    data = db.execute(stmt).all()
    stock = dict(db.execute(select(Batch.item_id, func.sum(Batch.quantity)).where(Batch.quantity > 0).group_by(Batch.item_id)).all())
    rank_by = p.get('rank_by') or 'quantity'
    key = {'quantity': lambda r: r[1], 'value': lambda r: r[3] or 0, 'bills': lambda r: r[2]}[rank_by]
    data.sort(key=lambda r: (-key(r), r[0].name))
    top = p.get('top') or '25'
    if top != 'all':
        data = data[:int(top)]
    rows = [dict(rank=i, item=it.name, category=names.get(it.category, it.category), category_code=it.category, quantity=int(q or 0),
                 unit=(it.base_unit or 'UNIT').lower(), bills=int(bills or 0), value=money(v or 0), stock=int(stock.get(it.id, 0)))
            for i, (it, q, bills, v) in enumerate(data, 1)]
    note = ('Ranked by ' + {'quantity': 'quantity sold', 'value': 'sale value', 'bills': 'number of bills'}[rank_by] +
            '. Quantities are in each product’s base unit (tablets, bottles…) net of returns; voided bills are excluded.')
    return rows, note


def item_wise_sales(db, p, start, end, tz, names):
    """Per product (or batch), optionally split by day / week / month: sold, returned
    and net quantity in the product's base unit, MRP value, discount, sales value,
    refunds and net sales. Voided bills are excluded; returns count on the day
    they were processed. Cost and profit only where the batch cost is known."""
    from app.services import financials, units
    from app.services.report_generator import bucket, matches

    split, by_batch = p.get('split') or '', p.get('level') == 'batch'
    supplier_items = None
    if p.get('supplier'):
        supplier_items = set(db.scalars(select(Batch.item_id).where(Batch.supplier_id == int(p['supplier']))))
    rows: dict = {}

    def row_for(line, when):
        if not matches(line.item, p, name=line.product_name, batch=line.batch_no or ''):
            return None
        if supplier_items is not None and line.item_id not in supplier_items:
            return None
        # manual-bill lines are counted per typed unit, stock lines in the product's base unit: never one row
        manual = line.financial_cost_source == 'MANUAL_BILL'
        key = (('MB', line.item_id or line.product_name) if manual else (line.item_id or line.product_name),
               line.batch_id if by_batch else None, bucket(when, split, tz) if split else '')
        r = rows.get(key)
        if r is None:
            it = line.item
            r = rows[key] = dict(period=key[2], item_id=line.item_id, batch_id=line.batch_id if by_batch else None,
                                 code=it.article_id if it else '', item=line.product_name + (' · manual bill' if manual else ''),
                                 pack=(it.pack_size if it else '') or line.pack_size or '',
                                 category=names.get(it.category, it.category) if it else '', category_code=it.category if it else '',
                                 manufacturer=(it.manufacturer if it else '') or '', batch=line.batch_no if by_batch else '',
                                 expiry=line.expiry_date.strftime('%b-%Y') if by_batch and line.expiry_date else '',
                                 unit=(line.base_unit or 'UNIT').lower(), upp=line.units_per_pack or 1,
                                 pack_unit=(it.pack_unit if it else '') or 'PACK',
                                 mrp=money(line.pack_mrp or line.mrp or 0),
                                 sold=0, returned=0, gross=Decimal(0), value=Decimal(0), refund=Decimal(0), cost=Decimal(0),
                                 cost_missing=False, bill_ids=set(), customer_ids=set(), first=None, last=None)
        return r

    q = (select(SaleItem, Sale).join(Sale, Sale.id == SaleItem.sale_id)
         .where(Sale.payment_status != 'CANCELLED', Sale.sale_date >= start, Sale.sale_date < end))
    for line, sale in db.execute(q):
        r = row_for(line, sale.sale_date)
        if r is None:
            continue
        r['sold'] += line.quantity
        r['gross'] += financials.gross_value(line)
        r['value'] += to_decimal(line.net_sale_value if line.net_sale_value is not None else line.line_total)
        r['bill_ids'].add(sale.id)
        r['customer_ids'].add(sale.customer_id or 0)
        r['first'] = min(r['first'] or sale.sale_date, sale.sale_date)
        r['last'] = max(r['last'] or sale.sale_date, sale.sale_date)
        unit_cost = financials.effective_cost(line)
        if unit_cost is None:
            r['cost_missing'] = True
        else:
            r['cost'] += to_decimal(unit_cost) * line.quantity

    rq = (select(SaleReturnItem, SaleReturn).join(SaleReturn, SaleReturn.id == SaleReturnItem.return_id)
          .where(SaleReturn.status == 'COMPLETED', SaleReturn.processed_at >= start, SaleReturn.processed_at < end))
    for ret_line, ret in db.execute(rq):
        line = db.get(SaleItem, ret_line.sale_item_id) if ret_line.sale_item_id else None
        if line is None or line.sale.payment_status == 'CANCELLED':
            continue
        r = row_for(line, ret.processed_at)
        if r is None:
            continue
        r['returned'] += ret_line.quantity
        r['refund'] += to_decimal(ret_line.refund_amount)
        unit_cost = financials.effective_cost(line)
        if unit_cost is not None:
            r['cost'] -= to_decimal(unit_cost) * ret_line.quantity

    stock_q = select(Batch.id if by_batch else Batch.item_id, func.sum(Batch.quantity)).where(Batch.quantity > 0)
    stock = dict(db.execute(stock_q.group_by(Batch.id if by_batch else Batch.item_id)).all())
    out = []
    for r in rows.values():
        net = r['sold'] - r['returned']
        net_value = money(r['value'] - r['refund'])
        cost = None if r['cost_missing'] else money(r['cost'])
        out.append(dict(period=r['period'], code=r['code'], item=r['item'], pack=r['pack'], category=r['category'],
                        category_code=r['category_code'], manufacturer=r['manufacturer'], batch=r['batch'], expiry=r['expiry'],
                        unit=r['unit'], sold=r['sold'], returned=r['returned'], net_qty=net,
                        packs=units.describe_stock(net, r['upp'], r['unit'], r['pack_unit']), mrp=r['mrp'],
                        gross=money(r['gross']), discount=money(r['gross'] - r['value']), value=money(r['value']),
                        refund=money(r['refund']), net_value=net_value, bills=len(r['bill_ids']),
                        customers=len(r['customer_ids'] - {0}), first_sold=_day(r['first'], tz), last_sold=_day(r['last'], tz),
                        stock=int(stock.get(r['batch_id'] if by_batch else r['item_id'], 0) or 0),
                        cost=cost, profit=money(net_value - cost) if cost is not None else None,
                        _drill=dict(report='item-sales', params={'period': 'custom', 'from': p['from'], 'to': p['to'], 'item': r['item']})))
    if p.get('show') == 'all' and not split and not by_batch and not p.get('batch'):
        present = {r['item'] for r in out}
        for it in db.scalars(select(Item).where(Item.deleted_at.is_(None)).order_by(Item.name)):
            if it.name in present or not matches(it, p) or (supplier_items is not None and it.id not in supplier_items):
                continue
            out.append(dict(period='', code=it.article_id, item=it.name, pack=it.pack_size or '', category=names.get(it.category, it.category),
                            category_code=it.category, manufacturer=it.manufacturer or '', batch='', expiry='',
                            unit=(it.base_unit or 'UNIT').lower(), sold=0, returned=0, net_qty=0, packs='', mrp=None,
                            gross=Decimal(0), discount=Decimal(0), value=Decimal(0), refund=Decimal(0), net_value=Decimal(0),
                            bills=0, customers=0, first_sold='', last_sold='', stock=int(stock.get(it.id, 0) or 0), cost=Decimal(0), profit=Decimal(0)))
    sort = p.get('sort') or 'value'
    rank = (lambda r: r['item'].casefold()) if sort == 'item' else (lambda r: -r['net_qty']) if sort == 'quantity' else (lambda r: -r['net_value'])
    out.sort(key=lambda r: (r['period'], rank(r), r['item'].casefold(), r['batch']))
    for r in out:
        if r['period']:
            d = date.fromisoformat(r['period'])
            r['period'] = (d.strftime('%d-%b-%Y') if split == 'day' else f"Week of {d:%d-%b-%Y}" if split == 'week' else d.strftime('%b-%Y'))
    return out, ''


# --------------------------------------------------------------------------- customers
def _customer_bills(start=None, end=None):
    """Finalized bills per customer with refunds subtracted (net sales)."""
    refunds = (select(SaleReturn.sale_id.label('sid'), func.sum(SaleReturn.total_refund).label('refund'))
               .where(SaleReturn.status == 'COMPLETED').group_by(SaleReturn.sale_id).subquery())
    stmt = (select(Sale.customer_id, Sale.id, Sale.invoice_no, Sale.sale_date, Sale.total, Sale.discount,
                   func.coalesce(refunds.c.refund, 0).label('refund'))
            .outerjoin(refunds, refunds.c.sid == Sale.id)
            .where(Sale.payment_status != 'CANCELLED', Sale.customer_id.is_not(None)))
    if start is not None:
        stmt = stmt.where(Sale.sale_date >= start)
    if end is not None:
        stmt = stmt.where(Sale.sale_date < end)
    return stmt


def _drill_customer(cid):
    return dict(url=f'/customers/{cid}')


def customer_rows(db, rid, p, start, end, tz):
    customers = {c.id: c for c in db.scalars(select(Customer))}
    if rid in ('frequent-customers', 'top-customers', 'customer-recovery', 'new-customers'):
        period = db.execute(_customer_bills(start, end)).all() if rid != 'customer-recovery' else []
        agg: dict[int, dict] = {}
        for cid, sid, no, when, total, disc, refund in period:
            a = agg.setdefault(cid, dict(bills=0, value=Decimal(0), gross=Decimal(0), discount=Decimal(0), last=None))
            a['bills'] += 1
            a['value'] += to_decimal(total) - to_decimal(refund)
            a['discount'] += to_decimal(disc)
            a['gross'] += to_decimal(total) + to_decimal(disc)
            a['last'] = max(a['last'], when) if a['last'] else when
    if rid == 'frequent-customers':
        min_bills = int(p.get('min_bills') or 2)
        min_value = to_decimal(p.get('min_value') or 0)
        rows = [dict(customer=customers[c].name, mobile=customers[c].mobile, bills=a['bills'], value=money(a['value']),
                     average=money(a['value'] / a['bills']), last_visit=_day(a['last'], tz), _drill=_drill_customer(c))
                for c, a in agg.items() if a['bills'] >= min_bills and a['value'] >= min_value]
        rows.sort(key=lambda r: (-r['bills'], -r['value']))
        return rows, ('Bills are finalized invoices (voided excluded); a visit with two bills counts as two bills. '
                      'Total sales are net of refunds.')
    if rid == 'top-customers':
        ranked = sorted(agg.items(), key=lambda kv: -kv[1]['value'])
        top = p.get('top') or '25'
        if top != 'all':
            ranked = ranked[:int(top)]
        return [dict(rank=i, customer=customers[c].name, mobile=customers[c].mobile, bills=a['bills'], gross=money(a['gross']),
                     discount=money(a['discount']), value=money(a['value']), average=money(a['value'] / a['bills']),
                     last_visit=_day(a['last'], tz), _drill=_drill_customer(c)) for i, (c, a) in enumerate(ranked, 1)], \
            'Gross is bill value before bill discount; net sales subtract refunds. Manual bills are included.'
    if rid == 'new-customers':
        first = {}
        for cid, sid, no, when, total, disc, refund in db.execute(_customer_bills().order_by(Sale.sale_date, Sale.id)):
            first.setdefault(cid, (sid, no, when, total))
        rows = []
        for c, (sid, no, when, total) in first.items():
            if start <= when < end:
                a = agg.get(c, dict(bills=0, value=Decimal(0)))
                rows.append(dict(customer=customers[c].name, mobile=customers[c].mobile, first_date=_day(when, tz), first_invoice=no,
                                 first_value=money(total), bills=a['bills'], value=money(a['value']), _drill=dict(url=f'/sales/{sid}')))
        rows.sort(key=lambda r: r['first_date'])
        return rows, 'A new customer is one whose first finalized purchase falls in the period.'
    if rid == 'customer-recovery':
        days = _inactive_days(p)
        to_day = date.fromisoformat(p['to'])
        cutoff = _utc(to_day - timedelta(days=days - 1), tz)
        allb = db.execute(_customer_bills(end=end).order_by(Sale.sale_date)).all()
        agg = {}
        for cid, sid, no, when, total, disc, refund in allb:
            a = agg.setdefault(cid, dict(bills=0, value=Decimal(0), last=None, last_no='', last_value=Decimal(0), last_id=None))
            a['bills'] += 1
            a['value'] += to_decimal(total) - to_decimal(refund)
            a.update(last=when, last_no=no, last_value=to_decimal(total), last_id=sid)
        open_fu = {cid for (cid,) in db.execute(select(CustomerFollowUp.customer_id).where(CustomerFollowUp.status == 'OPEN'))}
        rows = []
        for c, a in agg.items():
            cu = customers[c]
            if a['last'] >= cutoff or a['bills'] < int(p.get('min_bills') or 1) or a['value'] < to_decimal(p.get('min_value') or 0):
                continue
            if p.get('exclude_no_mobile') == '1' and not (cu.mobile or '').strip():
                continue
            if p.get('exclude_open_followup') == '1' and c in open_fu:
                continue
            away = (to_day - a['last'].replace(tzinfo=timezone.utc).astimezone(tz).date()).days
            rows.append(dict(customer=cu.name, mobile=cu.mobile, last_visit=_day(a['last'], tz), days_away=away, last_invoice=a['last_no'],
                             bills=a['bills'], value=money(a['value']), last_value=money(a['last_value']),
                             open_followup='Yes' if c in open_fu else '', _drill=_drill_customer(c)))
        rows.sort(key=lambda r: (-r['bills'], r['days_away']))
        return rows, (f'Known customers with no purchase in the {days} days up to {to_day:%d-%b-%Y}. Open a row to create a follow-up '
                      'from the customer record.')
    if rid == 'customer-history':
        cid = int(p['customer_id']) if p.get('customer_id') else None
        if cid is None and p.get('customer'):
            like = f"%{p['customer']}%"
            match = db.scalars(select(Customer).where(or_(Customer.name.ilike(like), Customer.mobile.ilike(like),
                                                          Customer.customer_id.ilike(like))).limit(2)).all()
            cid = match[0].id if len(match) == 1 else None
        if cid is None or cid not in customers:
            from app.services.report_generator import ReportError
            raise ReportError('Choose the customer from the list (type a name, mobile or customer ID)')
        refunds = dict(db.execute(select(SaleReturn.sale_id, func.sum(SaleReturn.total_refund)).where(SaleReturn.status == 'COMPLETED')
                                  .group_by(SaleReturn.sale_id)).all())
        rows = []
        for s in db.scalars(select(Sale).where(Sale.customer_id == cid, Sale.payment_status != 'CANCELLED', Sale.sale_date >= start,
                                               Sale.sale_date < end).order_by(Sale.sale_date)):
            ret = to_decimal(refunds.get(s.id, 0))
            rows.append(dict(date=_day(s.sale_date, tz), invoice=s.invoice_no, items=len(s.items), gross=money(to_decimal(s.total) + to_decimal(s.discount)),
                             discount=money(s.discount), value=money(s.total), returns=money(ret), retained=money(to_decimal(s.total) - ret),
                             _drill=dict(url=f'/sales/{s.id}')))
        return rows, f'Bills of {customers[cid].name} ({customers[cid].mobile or "no mobile"}). Voided bills are excluded.'
    if rid == 'customer-returns':
        stmt = (select(SaleReturn, Sale, Customer).join(Sale, Sale.id == SaleReturn.sale_id).join(Customer, Customer.id == Sale.customer_id)
                .where(SaleReturn.status == 'COMPLETED', SaleReturn.processed_at >= start, SaleReturn.processed_at < end))
        if p.get('customer_id') or p.get('customer'):
            from app.services.report_generator import customer_filter
            stmt = stmt.where(customer_filter(p))
        users = {u.id: u.username for u in db.scalars(select(User))}
        rows = []
        for ret, s, cu in db.execute(stmt.order_by(SaleReturn.processed_at)):
            rows.append(dict(customer=cu.name, invoice=s.invoice_no, return_no=ret.return_no, date=_day(ret.processed_at, tz),
                             items=', '.join(i.product_name for i in ret.items)[:80], quantity=sum(i.quantity for i in ret.items),
                             value=money(ret.total_refund), method=ret.refund_method.title(), reason=(ret.reason_code or '').replace('_', ' ').title(),
                             operator=users.get(ret.processed_by_user_id, ''), _drill=dict(url=f'/sales/{s.id}')))
        return rows, 'Completed returns of bills billed to a known customer.'
    if rid == 'followups-due':
        from app.services import business_time

        today = business_time.current_business_date(db)
        scope = p.get('followup_scope') or 'open'
        stmt = select(CustomerFollowUp).where(CustomerFollowUp.status == 'OPEN') if scope != 'all' else select(CustomerFollowUp)
        if scope == 'overdue':
            stmt = stmt.where(CustomerFollowUp.due_date < today)
        elif scope == 'today':
            stmt = stmt.where(CustomerFollowUp.due_date == today)
        elif scope == 'next7':
            stmt = stmt.where(CustomerFollowUp.due_date >= today, CustomerFollowUp.due_date <= today + timedelta(days=7))
        if p.get('operator'):
            stmt = stmt.where(CustomerFollowUp.created_by == int(p['operator']))
        users = {u.id: u.username for u in db.scalars(select(User))}
        rows = []
        for fu in db.scalars(stmt.order_by(CustomerFollowUp.due_date, CustomerFollowUp.due_time)):
            cu = customers.get(fu.customer_id)
            state = fu.status if fu.status != 'OPEN' else ('Overdue' if fu.due_date < today else 'Today' if fu.due_date == today else 'Open')
            rows.append(dict(customer=cu.name if cu else '', mobile=cu.mobile if cu else '', due=fu.due_date.strftime('%d-%b-%Y') + (f' {fu.due_time}' if fu.due_time else ''),
                             reason=FOLLOWUP_REASONS.get(fu.reason, fu.reason), invoice=fu.source_sale.invoice_no if fu.source_sale else '',
                             created_by=users.get(fu.created_by, ''), status=state.title(), _drill=_drill_customer(fu.customer_id)))
        return rows, 'Follow-ups from POS and the Customers module (one record each).'
    if rid == 'followup-performance':
        from app.services import business_time

        today = business_time.current_business_date(db)
        users = {u.id: u.username for u in db.scalars(select(User))}
        stats: dict[int, dict] = {}
        for fu in db.scalars(select(CustomerFollowUp).where(CustomerFollowUp.created_at >= start, CustomerFollowUp.created_at < end)):
            s = stats.setdefault(fu.created_by or 0, dict(created=0, completed=0, on_time=0, cancelled=0, open=0, overdue=0))
            s['created'] += 1
            if fu.status == 'COMPLETED':
                s['completed'] += 1
                if fu.completed_at and fu.completed_at.replace(tzinfo=timezone.utc).astimezone(tz).date() <= fu.due_date:
                    s['on_time'] += 1
            elif fu.status == 'CANCELLED':
                s['cancelled'] += 1
            else:
                s['open'] += 1
                if fu.due_date < today:
                    s['overdue'] += 1
        return [dict(operator=users.get(u, 'system'), **s) for u, s in sorted(stats.items(), key=lambda kv: -kv[1]['created'])], \
            'Follow-ups created in the period, by the operator who created them.'
    return [], ''


def purchase_gst(db, p, start, end, tz):
    """GST paid on received purchases, less GST reversed by returns to suppliers. Line values come
    from each line's GST snapshot (taken when it was posted); lines posted before GST tracking are
    worked out with the same invoice arithmetic from their stored taxable value and GST %."""
    from app.models import Purchase, PurchaseItem, PurchaseReturn
    from app.services import gst as G
    from app.services.purchasing import _received_packs

    pol = G.policy(db)
    want_rate = Decimal(p['gst_rate']).normalize() if p.get('gst_rate') else None
    view = p.get('gst_view') or 'line'
    q = (select(PurchaseItem, Purchase).join(Purchase, Purchase.id == PurchaseItem.purchase_id)
         .where(PurchaseItem.status == 'POSTED', Purchase.status.in_(('POSTED', 'PARTIAL')),
                Purchase.purchase_date >= start, Purchase.purchase_date < end))
    if p.get('supplier'):
        q = q.where(Purchase.supplier_id == int(p['supplier']))
    if p.get('invoice'):
        q = q.where(Purchase.invoice_no.ilike(f"%{p['invoice']}%"))
    if p.get('item'):
        q = q.where(PurchaseItem.product_name.ilike(f"%{p['item']}%"))
    facts = []
    for line, doc in db.execute(q.order_by(Purchase.purchase_date, Purchase.id, PurchaseItem.line_no)):
        sup = doc.supplier
        mode = doc.supply_type or G.supply_type(pol.our_gstin, sup.gst_number if sup else '', our_state=pol.our_state)[0]
        if line.gst_amount is not None:
            b = {'taxable': line.taxable_value, 'gst': line.gst_amount, 'cgst': line.cgst_amount or Decimal(0),
                 'sgst': line.sgst_amount or Decimal(0), 'igst': line.igst_amount or Decimal(0), 'landed': line.landed_total,
                 'packs': _received_packs(line), 'rate': line.gst_rate}
        else:
            b = G.line_breakdown(line, G.bill_discount_factor(doc), mode, _received_packs(line))
        rate = Decimal(line.gst_rate).normalize() if line.gst_rate is not None else None
        if want_rate is not None and rate != want_rate:
            continue
        facts.append(dict(day=doc.purchase_date, reference=doc.reference_no or '', invoice=doc.invoice_no, supplier=sup.name if sup else '',
                          gstin=sup.gst_number if sup else '', item=line.product_name, hsn=line.hsn_code or '', rate=rate,
                          packs=b['packs'], taxable=b['taxable'], cgst=b['cgst'], sgst=b['sgst'], igst=b['igst'], gst=b['gst'],
                          landed=b['landed'], note='' if rate is not None else 'GST % not on the invoice (taken as 0%)',
                          drill=f'/purchases/{doc.id}'))
    # returns to suppliers reverse the GST that was paid on those packs
    rq = select(PurchaseReturn).where(PurchaseReturn.status != 'CANCELLED', PurchaseReturn.return_date >= start, PurchaseReturn.return_date < end)
    if p.get('supplier'):
        rq = rq.where(PurchaseReturn.supplier_id == int(p['supplier']))
    for ret in db.scalars(rq.order_by(PurchaseReturn.return_date)):
        line = db.get(PurchaseItem, ret.purchase_item_id) if ret.purchase_item_id else None
        if line is None and ret.batch_id:      # the line that brought this batch in carries its GST rate
            line = db.scalar(select(PurchaseItem).where(PurchaseItem.batch_id == ret.batch_id, PurchaseItem.status == 'POSTED')
                             .order_by(PurchaseItem.id.desc()).limit(1))
        if p.get('item') and p['item'].casefold() not in (ret.product_name or '').casefold():
            continue
        rate = Decimal(line.gst_rate).normalize() if line is not None and line.gst_rate is not None else None
        if want_rate is not None and rate != want_rate:
            continue
        value = Decimal(ret.value or 0)
        batch = db.get(Batch, ret.batch_id) if ret.batch_id else None
        incl = batch is not None and batch.rate_basis == 'INCL_GST'
        r = rate or Decimal(0)
        taxable = money(value * 100 / (100 + r)) if incl else money(value)
        gst = money(value - taxable) if incl else money(value * r / 100)
        doc = db.get(Purchase, ret.purchase_id) if ret.purchase_id else None
        sup = ret.supplier
        mode = (doc.supply_type if doc else '') or G.supply_type(pol.our_gstin, sup.gst_number if sup else '', our_state=pol.our_state)[0]
        cgst, sgst, igst = G.split(gst, mode)
        facts.append(dict(day=ret.return_date, reference=ret.reference_no or '', invoice=(doc.invoice_no if doc else '') + ' (return)',
                          supplier=sup.name if sup else '', gstin=sup.gst_number if sup else '', item=ret.product_name,
                          hsn=line.hsn_code if line is not None else '', rate=rate, packs=-Decimal(ret.quantity or 0),
                          taxable=-taxable, cgst=-cgst, sgst=-sgst, igst=-igst, gst=-gst, landed=-(taxable + gst),
                          note='returned to supplier: GST reversed', drill=f'/purchases/{doc.id}' if doc else None))
    fmt = G.fmt_rate

    def out(f, **extra):
        packs = f['packs']
        row = dict(date=_day(f['day'], tz) if f.get('day') else '', reference=f.get('reference', ''), invoice=f.get('invoice', ''),
                   supplier=f.get('supplier', ''), gstin=f.get('gstin', ''), item=f.get('item', ''), hsn=f.get('hsn', ''),
                   packs=float(packs) if packs % 1 else int(packs), gst_rate=fmt(f['rate']) if 'rate' in f else '',
                   taxable=money(f['taxable']), cgst=money(f['cgst']), sgst=money(f['sgst']), igst=money(f['igst']),
                   gst=money(f['gst']), landed=money(f['landed']),
                   rate_ex=money(f['taxable'] / packs) if packs else None, rate_incl=money(f['landed'] / packs) if packs else None,
                   lines=f.get('lines', 1), note=f.get('note', ''))
        row.update(extra)
        if f.get('drill'):
            row['_drill'] = dict(url=f['drill'])
        return row

    if view == 'line':
        return [out(f) for f in facts], ''
    keyf = {'invoice': lambda f: (f['reference'] or f['invoice'], f['invoice'], f['supplier']),
            'item': lambda f: (f['item'],), 'rate': lambda f: (f['rate'] if f['rate'] is not None else Decimal(-1),),
            'hsn': lambda f: (f['hsn'] or '—',), 'supplier': lambda f: (f['supplier'],),
            'month': lambda f: (f['day'].strftime('%Y-%m'),)}[view]
    groups: dict = {}
    for f in facts:
        k = keyf(f)
        g = groups.get(k)
        if g is None:
            g = groups[k] = dict(f, packs=Decimal(0), taxable=Decimal(0), cgst=Decimal(0), sgst=Decimal(0), igst=Decimal(0),
                                 gst=Decimal(0), landed=Decimal(0), lines=0, rates=set())
        for fld in ('packs', 'taxable', 'cgst', 'sgst', 'igst', 'gst', 'landed'):
            g[fld] += f[fld]
        g['lines'] += 1
        g['rates'].add(f['rate'])
    rows = []
    for k in sorted(groups, key=lambda k: tuple((0, x, '') if isinstance(x, Decimal) else (1, Decimal(0), str(x)) for x in k)):
        g = groups[k]
        blank = {'invoice': dict(item='', hsn=''), 'item': dict(invoice='', supplier='', date='', reference=''),
                 'rate': dict(invoice='', supplier='', item='', hsn='', date='', reference=''),
                 'hsn': dict(invoice='', supplier='', item='', date='', reference=''),
                 'supplier': dict(invoice='', item='', hsn='', date='', reference=''),
                 'month': dict(invoice='', supplier='', item='', hsn='', reference='')}[view]
        mixed = len(g['rates']) > 1
        row = out({**g, 'rate': None if mixed else next(iter(g['rates']))}, **blank)
        if mixed:
            row['gst_rate'] = 'mixed'
        if view == 'month':
            row['date'] = g['day'].strftime('%b-%Y')
        if view != 'invoice':
            row.pop('_drill', None)
        rows.append(row)
    return rows, ''


def rows(db, rid, p, start, end, tz, names):
    if rid == 'non-moving':
        return non_moving(db, p, start, end, tz, names)
    if rid == 'top-moving':
        return top_moving(db, p, start, end, tz, names)
    if rid == 'item-wise-sales':
        return item_wise_sales(db, p, start, end, tz, names)
    if rid == 'purchase-gst':
        return purchase_gst(db, p, start, end, tz)
    return customer_rows(db, rid, p, start, end, tz)
