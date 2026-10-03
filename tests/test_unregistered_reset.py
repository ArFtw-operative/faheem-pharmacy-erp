from datetime import date
import pytest
from sqlalchemy import select, text

from app.models import Batch, InventoryMovement, Purchase, User, Item
from app.services import purchasing, purchase_automation as auto, settings_service as settings, gst, inventory_reset, stock_ledger
from tests.test_purchase_automation import auto_create, enable, zero_tax_csv, reviewed_history
from tests.test_purchasing import supplier, draft
from tests.test_receipt_decisions import product


def test_unregistered_pharmacy_can_autopost_using_its_state(db):
    auto_create(db)          # the invoice holds a new product; product creation is opt-in
    sup=supplier(db)
    sup.gst_number='27AAPFU0939F1ZV'
    settings.set_setting(db,'gst_number','')
    settings.set_setting(db,'pharmacy_state_code','36')
    enable(db,'post')
    content=zero_tax_csv('N,NEW TABLET,10S,B1,May-2028,2,,10,20,20').replace(b',0\n',b',5\n')
    p=auto.import_file(db,'unregistered.csv',content,supplier_id=sup.id,invoice_no='NO-GSTIN',
                      invoice_date=date(2026,9,1),supplier_total='21',user=db.scalar(select(User)))[0]
    assert p.status=='POSTED'
    assert p.items[0].igst_amount==1 and p.items[0].cgst_amount==0
    assert not purchasing.gst_summary(db,p)['problems']


def test_supplier_registration_notes_do_not_become_posting_problems(db):
    settings.set_setting(db,'gst_number','')
    p=draft(db,',NEW TABLET,10S,B,May-2028,2,,10,20,20')
    p.supplier.gst_number='INVALID'
    problems=purchasing.gst_summary(db,p)['problems']
    assert not any('Pharmacy GSTIN' in s for s in problems)
    assert not problems
    assert any('Supplier GSTIN' in s for s in purchasing.gst_summary(db,p)['registration_notes'])
    assert gst.supply_type('', '36AAPFU0939F1ZW', our_state='36')[0]=='INTRA'


@pytest.mark.parametrize('store_gstin,seller_gstin', [('', ''), ('', '123'), ('INVALID', '27AAPFU0939F1ZX')])
def test_registration_numbers_never_block_automatic_posting(db, store_gstin, seller_gstin):
    auto_create(db)          # the invoice holds a new product; product creation is opt-in
    sup=supplier(db)
    sup.gst_number=seller_gstin
    settings.set_setting(db,'gst_number',store_gstin)
    enable(db,'post')
    p=auto.import_file(db,'record-only.csv',zero_tax_csv('N,NEW TABLET,10S,B1,May-2028,2,,10,20,20'),
                      supplier_id=sup.id,invoice_no='RECORD-ONLY',invoice_date=date(2026,9,1),
                      supplier_total='20',user=db.scalar(select(User)))[0]
    assert p.status=='POSTED'
    assert not purchasing.gst_summary(db,p)['problems']
    assert not auto.assessment(db,p)['document_blockers']


def test_printed_and_saved_gstin_difference_is_informational(db):
    enable(db)
    p=draft(db,'N,NEW TABLET,10S,B1,May-2028,2,,10,20,20')
    p.charges={**(p.charges or {}), '_supplier_evidence':{'gstin':'27AAPFU0939F1ZV'}}
    assert not any(b['code']=='SUPPLIER_CONFLICT' for b in auto.assessment(db,p)['document_blockers'])
    assert any('differs' in n for n in purchasing.gst_summary(db,p)['registration_notes'])


def test_reset_clears_purchases_and_stock_but_preserves_catalogue_and_reviewed_knowledge(db):
    sup=supplier(db)
    reviewed_history(db,sup)
    before_items=db.query(Item).count()
    before_movements=db.query(InventoryMovement).count()
    result=inventory_reset.reset_purchases_and_stock(db)
    assert result['removed']['purchases']==3
    assert db.query(Purchase).count()==0
    assert db.query(Item).count()==before_items
    assert sum(b.quantity for b in db.scalars(select(Batch)))==0
    assert db.query(InventoryMovement).count()>before_movements
    assert not stock_ledger.reconcile(db)
    assert all(b.purchase_id is None for b in db.scalars(select(Batch)))
    enable(db)
    p=draft(db,'N,NEW CAPSULE,10x1x10,B,May-2028,2.5,0.5,10,20,25',sup=sup)
    assert p.items[0].receipt_decision['received_base_units']==30
    assert all(str(i).startswith('verified:') for i in p.items[0].receipt_decision['history_line_ids'])


def test_reset_retains_contradictory_pack_evidence(db):
    sup=supplier(db)
    reviewed_history(db,sup,conflict=True)
    inventory_reset.reset_purchases_and_stock(db)
    enable(db)
    p=draft(db,'N,NEW TABLET,10x1x10,B,May-2028,2,,10,20,20',sup=sup)
    assert not p.items[0].receipt_decision['resolved']


def test_reset_transaction_can_roll_back(db):
    product(db)
    p=draft(db,',EXAMPLE 40MG TAB,15S,B,May-2028,2,,10,20,20')
    purchasing.post(db,p)
    db.commit()
    before=db.query(InventoryMovement).count()
    inventory_reset.reset_purchases_and_stock(db)
    db.rollback()
    assert db.query(Purchase).count()==1
    assert sum(b.quantity for b in db.scalars(select(Batch)))==30
    assert db.query(InventoryMovement).count()==before
