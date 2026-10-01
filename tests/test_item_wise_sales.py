"""Inventory → Item-wise Sales: sold, returned and net per product / batch, split by period."""
from __future__ import annotations

from app.services import inventory_service as inv, refund_service, report_generator as reports, sales_service

RANGE = {'period': 'this_month'}


def _setup(db):
    dolo = inv.create_item(db, name='DOLO 650 TAB', pack_size='15 S', base_unit='TABLET', pack_unit='STRIP', units_per_pack=15, loose_sale=True)
    inv.add_or_update_batch(db, dolo, batch_no='D1', quantity=2, unit='PACK', movement_type='OPENING_STOCK', mrp='30', purchase_rate='20')
    syp = inv.create_item(db, name='COUGH SYP', pack_size='100ML', base_unit='BOTTLE', pack_unit='BOTTLE', units_per_pack=1)
    inv.add_or_update_batch(db, syp, batch_no='S1', quantity=5, unit='PACK', movement_type='OPENING_STOCK', mrp='100')
    db.commit()
    a = sales_service.create_sale(db, lines=[{'item_id': dolo.id, 'quantity': 20}, {'item_id': syp.id, 'quantity': 2}], round_off_mode='NONE')
    b = sales_service.create_sale(db, lines=[{'item_id': syp.id, 'quantity': 1}], round_off_mode='NONE')
    v = sales_service.create_sale(db, lines=[{'item_id': syp.id, 'quantity': 1}], round_off_mode='NONE')
    db.commit()
    sales_service.void_sale(db, v, reason='wrong')
    refund_service.create_return(db, a, lines=[{'sale_item_id': a.items[0].id, 'quantity': 5}], refund_method='CASH')
    db.commit()
    return dolo, syp


def test_item_wise_sales_nets_returns_and_excludes_voids(db):
    _setup(db)
    rep = reports.generate(db, 'item-wise-sales', RANGE, ['item', 'sold', 'returned', 'net_qty', 'packs', 'value', 'refund', 'net_value', 'bills'], user=None)
    by = {r['item']: r for r in rep['rows']}
    assert (by['DOLO 650 TAB']['sold'], by['DOLO 650 TAB']['returned'], by['DOLO 650 TAB']['net_qty']) == (20, 5, 15)
    assert by['DOLO 650 TAB']['packs'] == '1 strip'
    assert by['COUGH SYP']['sold'] == 3 and by['COUGH SYP']['bills'] == 2 and by['COUGH SYP']['value'] == 300
    assert by['DOLO 650 TAB']['value'] == 40 and by['DOLO 650 TAB']['refund'] == 10 and by['DOLO 650 TAB']['net_value'] == 30
    assert rep['totals']['net_qty'] == 18


def test_item_wise_sales_split_batch_and_cost(db):
    _setup(db)
    rep = reports.generate(db, 'item-wise-sales', {**RANGE, 'split': 'day', 'level': 'batch'},
                           ['item', 'net_qty', 'cost', 'profit'], user=None)
    keys = [c['key'] for c in rep['columns']]
    assert keys[0] == 'period' and 'batch' in keys
    dolo = next(r for r in rep['rows'] if r['item'] == 'DOLO 650 TAB')
    assert dolo['batch'] == 'D1' and dolo['cost'] == 20 and dolo['profit'] == 10      # 15 tabs at 20/strip of 15
    assert rep['totals']['cost'] is None                                               # the syrup has no purchase rate
    everything = reports.generate(db, 'item-wise-sales', {**RANGE, 'show': 'all', 'item': 'COUGH'}, user=None)
    assert [r['item'] for r in everything['rows']] == ['COUGH SYP']
