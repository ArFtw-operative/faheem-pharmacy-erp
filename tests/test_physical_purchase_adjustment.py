import pytest
from app.models import Batch
from app.services import purchasing, purchase_adjustment as physical, inventory_service as inv, stock_ledger
from tests.test_purchase_automation import enable
from tests.test_purchasing import draft, csv_bytes, supplier

def values(**kwargs):
    return dict(form='TABLET',units_per_pack='15',quantity='2',free='0',**kwargs)

def test_new_product_adjustment_receives_counted_tablets(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    line=p.items[0]
    assert line.receipt_decision['received_base_units']==30
    assert line.units_per_pack==15
    assert not db.query(Batch).count()
    purchasing.post(db,p)
    batch=db.query(Batch).one()
    assert batch.quantity==30 and batch.units_per_pack==15
    assert batch.mrp==20
    assert not stock_ledger.reconcile(db)

def test_matched_catalogue_change_is_staged_and_applied_atomically(db):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',EXAMPLE BRAND,15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    assert item.units_per_pack==1 and item.base_unit=='PACK'
    assert p.items[0].receipt_decision['received_base_units']==30
    purchasing.post(db,p)
    assert item.units_per_pack==15 and item.base_unit=='TABLET'
    assert db.query(Batch).one().quantity==30
    assert db.query(Batch).one().purchase_rate==10
    assert not stock_ledger.reconcile(db)

def test_free_fraction_is_counted_without_fractional_physical_stock(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2.5,0.5,10,20,25')
    physical.adjust(db,p,p.items[0],dict(form='TABLET',units_per_pack='15',quantity='2.5',free='0.5'))
    assert p.items[0].receipt_decision['received_base_units']==45
    assert p.items[0].line_total==25

@pytest.mark.parametrize('form,base,pack', [('SYRUP','BOTTLE','200ML'),('CREAM','TUBE','30GM'),('SOAP','PIECE','75GM')])
def test_whole_container_forms_receive_individual_units(db,form,base,pack):
    enable(db)
    p=draft(db,f',NEW BRAND,{pack},B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],dict(form=form,units_per_pack='1',quantity='2',free='1'))
    assert p.items[0].receipt_decision['received_base_units']==3
    assert p.items[0].receipt_decision['base_unit']==base

def test_unrepresentable_quantity_does_not_change_draft(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2,,10,20,20')
    original=p.items[0].corrections
    with pytest.raises(purchasing.PurchaseError,match='whole number'):
        physical.adjust(db,p,p.items[0],dict(form='TABLET',units_per_pack='15',quantity='0.5',free='0'))
    assert p.items[0].corrections==original

def test_changed_master_invalidates_staged_adjustment(db):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',EXAMPLE BRAND,15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    item.units_per_pack=20
    db.flush()
    purchasing.refresh_line(db,p,p.items[0])
    assert not p.items[0].receipt_decision['resolved']
    assert any(i['code']=='packaging_changed' for i in p.items[0].issues)

def test_posted_adjustment_teaches_next_invoice_without_staging(db):
    enable(db)
    sup=supplier(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='10x1x15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    row=',EXAMPLE BRAND,10x1x15,B,May-2028,2,,10,20,20'
    p=draft(db,row,sup=sup)
    physical.adjust(db,p,p.items[0],values())
    purchasing.post(db,p)
    following=purchasing.create_from_file(db,'next.csv',csv_bytes(row.replace(',B,',',C,')),supplier_id=sup.id,invoice_no='NEXT')
    assert following.items[0].receipt_decision['resolved']
    assert following.items[0].receipt_decision['received_base_units']==30

def test_adjustment_api_returns_equivalent_without_posting(client,db):
    from tests.conftest import login
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2,,10,20,20')
    db.commit()
    login(client)
    response=client.put(f'/api/erp/purchases/{p.id}/lines/{p.items[0].id}/physical-quantity',json=values())
    assert response.status_code==200,response.text
    assert response.json()['lines'][0]['receipt']['received_base_units']==30
    assert db.query(Batch).count()==0


def test_existing_stock_pack_change_is_ledgered_at_post(db):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    old=inv.add_or_update_batch(db,item,batch_no='OLD',quantity=2,unit='BASE',movement_type='OPENING_STOCK',mrp='20',purchase_rate='10')
    p=draft(db,',EXAMPLE BRAND,15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    assert old.quantity==2
    purchasing.post(db,p)
    assert old.quantity==30
    assert inv.stock_on_hand(db,item.id)==60
    assert not stock_ledger.reconcile(db)


def test_failed_post_rolls_back_staged_catalogue_and_stock(db,monkeypatch):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',EXAMPLE BRAND,15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    db.commit()
    def fail(*args,**kwargs):
        raise stock_ledger.StockError('Injected receipt failure')
    monkeypatch.setattr(inv,'add_or_update_batch',fail)
    with pytest.raises(purchasing.PurchaseError,match='Injected'):
        purchasing.post(db,p)
    assert item.units_per_pack==1 and item.base_unit=='PACK'
    assert not db.query(Batch).count()


def test_posted_line_cannot_be_adjusted(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    purchasing.post(db,p)
    with pytest.raises(purchasing.PurchaseError):
        physical.adjust(db,p,p.items[0],values())


def test_counting_strips_preserves_box_billing_and_posts_physical_units(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,1,,10,20,10')
    physical.adjust(db,p,p.items[0],values())
    line=p.items[0]
    assert line.line_total==10
    assert line.receipt_decision['paid']=='1'
    assert line.receipt_decision['received_base_units']==30
    purchasing.post(db,p)
    assert db.query(Batch).one().quantity==30
    assert db.query(Batch).one().purchase_rate==5
    assert not stock_ledger.reconcile(db)


def test_delivery_count_is_not_learned_as_supplier_conversion(db):
    enable(db)
    sup=supplier(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,2,,10,20,20',sup=sup)
    physical.adjust(db,p,p.items[0],dict(form='TABLET',units_per_pack='15',quantity='2',free='1'))
    assert p.items[0].receipt_decision['received_base_units']==45
    purchasing.post(db,p)
    from app.models import SupplierProductMap
    assert not db.query(SupplierProductMap).one().receipt_conventions
    next_p=draft(db,',NEW BRAND,10x1x15,C,May-2028,2,,10,20,20',sup=sup)
    assert not next_p.items[0].receipt_decision['resolved']


def test_changing_billed_quantity_invalidates_delivery_count(db):
    enable(db)
    p=draft(db,',NEW BRAND,10x1x15,B,May-2028,1,,10,20,10')
    physical.adjust(db,p,p.items[0],values())
    purchasing.correct(db,p,p.items[0],{'quantity':'3'})
    assert not p.items[0].receipt_decision['resolved']
    assert any(i['code']=='physical_count_stale' for i in p.items[0].issues)


@pytest.mark.parametrize('form,count,quantity,free,base,equivalent',[
    ('TABLET',15,'2.5','0.5','TABLET','3 strips'),
    ('TABLET',30,'2','0','TABLET','2 strips'),
    ('CAPSULE',10,'1.5','0','CAPSULE','1 strip + 5 capsules'),
    ('SYRUP',1,'2.5','0.5','BOTTLE',''),
    ('CREAM',1,'2','0','TUBE',''),
])
def test_purchase_columns_use_each_lines_physical_definition(db,form,count,quantity,free,base,equivalent):
    from app.routers.purchases import _line_view
    enable(db)
    p=draft(db,f',NEW BRAND,,B,May-2028,{quantity},{free},10,20,{float(quantity)*10}')
    physical.adjust(db,p,p.items[0],dict(form=form,units_per_pack=str(count),quantity=quantity,free=free))
    view=_line_view(p.items[0])['physical']
    assert view['form']==form and view['base_unit']==base
    assert view['received']==int((float(quantity)+float(free))*count)
    assert view['equivalent']==equivalent


def test_purchase_columns_show_staged_definition_before_master_changes(db):
    from app.routers.purchases import _line_view
    enable(db)
    item=inv.create_item(db,name='EXAMPLE BRAND',pack_size='15',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',EXAMPLE BRAND,15,B,May-2028,2,,10,20,20')
    physical.adjust(db,p,p.items[0],values())
    assert item.base_unit=='PACK'
    view=_line_view(p.items[0])['physical']
    assert view['base_unit']=='TABLET' and view['equivalent']=='2 strips'
