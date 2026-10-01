"""Inventory and reports must display the same recorded invoice prices."""
from datetime import date, datetime
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.models import Purchase, PurchaseItem, Supplier, InventoryMovement, User, Role, Permission
from app.services import inventory_service as inv, inventory_pricing, report_generator as reports
from tests.conftest import login


def receive(db, item, supplier, invoice, rate, when):
    purchase = Purchase(supplier_id=supplier.id, invoice_no=invoice, purchase_date=when, total=rate)
    db.add(purchase); db.flush()
    db.add(PurchaseItem(purchase_id=purchase.id, item_id=item.id, product_name=item.name,
                        batch_no='B1', quantity=1, rate=rate, mrp=30, line_total=rate))
    batch = inv.add_or_update_batch(db, item, batch_no='B1', expiry_date=date(2026, 10, 1),
        quantity=1, unit='PACK', mrp=30, purchase_rate=rate, supplier_id=supplier.id,
        purchase_id=purchase.id, movement_type='PURCHASE_RECEIPT', reference_type='PURCHASE',
        reference_id=purchase.id, reference_no=invoice)
    movement = db.scalars(select(InventoryMovement).where(InventoryMovement.reference_id == purchase.id,
                                                           InventoryMovement.reference_type == 'PURCHASE')).one()
    movement.created_at = when
    batch.created_at = datetime(2026, 9, 1)
    db.commit()
    return purchase, batch


def test_latest_invoice_rate_shared_with_inventory_and_reports(client, db):
    item = inv.create_item(db, name='Invoice rate tablets', units_per_pack=10, base_unit='TABLET', pack_unit='STRIP')
    supplier = Supplier(name='Rate supplier'); db.add(supplier); db.flush()
    first, batch = receive(db, item, supplier, 'FIRST', Decimal('12.50'), datetime(2026, 9, 1, 12))
    _, batch = receive(db, item, supplier, 'SECOND', Decimal('17.50'), datetime(2026, 9, 20, 12))
    assert batch.purchase_id == first.id  # Original link must not hide subsequent invoices.
    login(client)
    detail = client.get(f'/api/erp/inventory/{item.id}').json()
    row = detail['batches'][0]
    assert Decimal(str(row['purchase_rate'])) == Decimal('17.50')
    assert Decimal(str(row['unit_purchase_rate'])) == Decimal('1.75')
    assert row['purchase_invoice'] == 'SECOND' and Decimal(str(row['pack_mrp'])) == 30
    grid = client.get('/api/erp/inventory').json()['rows'][0]
    assert grid['purchase_rate'] == row['purchase_rate']
    item.reorder_level = 100
    db.commit()
    for rid in ('current-stock', 'batch-stock', 'expiry', 'low-stock'):
        document = reports.generate(db, rid, {'as_of': '2026-09-25'},
            ['item', 'purchase_rate', 'unit_purchase_rate', 'pack_mrp', 'purchase_invoice'])
        assert document['rows'][0]['purchase_rate'] == Decimal('17.50')
    earlier = reports.generate(db, 'batch-stock', {'as_of': '2026-09-10'}, ['purchase_rate', 'purchase_invoice'])
    assert earlier['rows'][0] == {'purchase_rate': Decimal('12.50'), 'purchase_invoice': 'FIRST'}
    # A zero invoice rate is a real price, not a missing value / fallback.
    receive(db, item, supplier, 'FREE', Decimal('0'), datetime(2026, 9, 22, 12))
    assert inventory_pricing.batch_prices(db, [batch])[batch.id]['purchase_rate'] == 0


def test_mixed_batch_product_prices_do_not_invent_an_average(db):
    item = inv.create_item(db, name='Mixed rates', base_unit='BOTTLE', units_per_pack=1)
    a = inv.add_or_update_batch(db,item,batch_no='A',quantity=3,purchase_rate=5,mrp=10,movement_type='OPENING_STOCK')
    b = inv.add_or_update_batch(db,item,batch_no='B',quantity=3,purchase_rate=7,mrp=12,movement_type='OPENING_STOCK')
    prices = inventory_pricing.batch_prices(db, [a,b])
    product = inventory_pricing.product_prices([a,b], prices)
    assert product['purchase_rate'] is None and product['pack_mrp'] is None
    assert 'purchase_rate' in product['pricing_varies']
    doc = reports.generate(db,'current-stock',{'as_of':datetime.now(reports.tz_for(db)).date().isoformat()},['purchase_rate','pack_mrp'])
    assert doc['rows'][0]['purchase_rate'] is None and doc['rows'][0]['pack_mrp'] is None


def test_inventory_only_user_cannot_read_invoice_rates(client, db):
    item = inv.create_item(db,name='Restricted rate')
    inv.add_or_update_batch(db,item,batch_no='PRIVATE',quantity=2,purchase_rate=5,mrp=10,movement_type='OPENING_STOCK')
    db.commit(); login(client)
    user=db.scalars(select(User)).first()
    role=Role(name='Stock access only',permissions=[db.scalar(select(Permission).where(Permission.code=='inventory.view'))])
    db.add(role);db.flush();user.role_id=role.id;db.commit()
    assert 'purchase_rate' not in client.get('/api/erp/inventory').text
    assert 'purchase_rate' not in client.get(f'/api/erp/inventory/{item.id}').text
    catalog=reports.catalog(user)
    assert all(c['key']!='purchase_rate' for r in catalog for c in r['columns'])
    with pytest.raises(reports.ReportError,match='Unknown columns'):
        reports.generate(db,'batch-stock',{'as_of':datetime.now(reports.tz_for(db)).date().isoformat()},['purchase_rate'],user=user)
