"""Rack reports for the report engine (Reports → Inventory Reports).

* Rack Inventory: what each rack / box held at the close of a day, batch by batch, from the
  dated locations and the stock ledger (location_service.inventory_rows). Unassigned stock is
  its own group, so Σ racks + Unassigned = the Current Stock report's total for the same day.
* Rack History: one line per day — products, batches and units in a rack at that day's close.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services import location_service as loc


def col(key, label, kind='text', default=True, total=False):
    return dict(key=key, label=label, kind=kind, default=default, total=total)


REPORTS = [
    dict(id='rack-inventory', title='Rack Inventory', group='Inventory Reports', permission='rack.report.view',
         description='Stock by rack and box at any date (unassigned stock shown separately)',
         filters=['as_of', 'racks', 'box', 'location', 'category', 'item', 'batch', 'expiry_window', 'stock_condition', 'item_status'],
         columns=[col('rack', 'Rack'), col('rack_name', 'Rack Name'), col('box', 'Box'), col('box_name', 'Box Name', default=False),
                  col('code', 'Code', default=False), col('item', 'Product'), col('category', 'Category'), col('batch', 'Batch'),
                  col('expiry', 'Expiry'), col('quantity', 'Available Qty', 'number', total=True), col('unit', 'Unit', default=False),
                  col('status', 'Status'), col('product_status', 'Product', default=False),
                  col('mrp_value', 'Value at MRP', 'money', False, True)]),
    dict(id='rack-history', title='Rack History', group='Inventory Reports', permission='rack.snapshot.view',
         description='A rack at the close of each day: products, batches, units',
         filters=['rack', 'box'],
         columns=[col('date', 'Date'), col('products', 'Products', 'number'), col('batches', 'Batches', 'number'),
                  col('units', 'Units', 'number')]),
]
IDS = {r['id'] for r in REPORTS}
ENUMS = {'location': {'', 'assigned', 'unassigned'}, 'stock_condition': {'', 'positive', 'zero', 'all', 'expired'},
         'item_status': {'', 'active', 'disabled'}}


def validate(p: dict, ReportError) -> None:
    for k, choices in ENUMS.items():
        if k in p and p[k] not in choices:
            raise ReportError('Choose a valid ' + k.replace('_', ' '))
    if p.get('racks') and not all(v.isdigit() for v in p['racks'].split(',')):
        raise ReportError('Choose valid racks')
    for k in ('rack', 'box'):
        if p.get(k) and not p[k].isdigit():
            raise ReportError(f'Choose a valid {k}')


def rows(db, rid, p, start, end, tz):
    """``end``: the UTC instant the last chosen day closes (the engine's as-of / To date)."""
    if rid == 'rack-history':
        if not p.get('rack'):
            raise ValueError('Choose a rack')
        first, last = date.fromisoformat(p['from']), date.fromisoformat(p['to'])
        days = (last - first).days + 1
        if days > 92:
            raise ValueError('Choose up to 92 days')
        return loc.timeline(db, int(p['rack']), days=days, end=last, box_id=int(p['box']) if p.get('box') else None)
    window = p.get('expiry_window', '')
    as_of = date.fromisoformat(p['to'])
    cond = p.get('stock_condition') or 'positive'
    out = loc.inventory_rows(
        db, end, rack_ids=[int(v) for v in p['racks'].split(',')] if p.get('racks') else None,
        box_id=int(p['box']) if p.get('box') else None, assigned=p.get('location', ''), category=p.get('category', ''),
        batch=p.get('batch', ''), expiry_before=as_of + timedelta(days=int(window)) if window.isdigit() else None,
        expired_only=window == 'expired' or cond == 'expired', stock='positive' if cond == 'expired' else cond,
        item_status=p.get('item_status', ''))
    if p.get('item'):
        out = [r for r in out if p['item'].casefold() in r['item'].casefold()]
    # shelf order: rack, box, product — unassigned stock last
    out.sort(key=lambda r: (r['rack'] == 'Unassigned', r['rack'], r['box'] == '', r['box'], r['item'].casefold(), r['expiry']))
    return out


def note(db, rid, rows_, p, end, tz) -> str:
    if rid == 'rack-history':
        return f"Each line is the rack at the close of that day ({tz.key}), rebuilt from location history and the stock ledger."
    assigned = sum(r['quantity'] for r in rows_ if r['rack'] != 'Unassigned')
    unassigned = sum(r['quantity'] for r in rows_ if r['rack'] == 'Unassigned')
    return (f"As of {date.fromisoformat(p['to']).strftime('%d-%b-%Y')} close of day ({tz.key}), from location history and the "
            f"stock ledger. In racks {assigned:,} · Unassigned {unassigned:,} · Total {assigned + unassigned:,} units.")
