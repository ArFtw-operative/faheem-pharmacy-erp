from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Item, InventoryMovement, User
from app.services import purchasing, purchase_automation as auto, settings_service, receipt_decision as rd, units
from tests.test_purchasing import draft, supplier, csv_bytes, stock
from tests.test_receipt_decisions import product
from tests.conftest import certain


def enable(db, mode="prepare"):
    settings_service.set_setting(db, "purchase_automation", mode)


def reviewed_history(db, sup, *, conflict=False, trusted=True):
    for n in range(3):
        name = f"HISTORY {n}MG TAB"
        product(db, name=name, pack="10S", upp=10)
        p = draft(db, f"H{n},{name},20X10S,B{n},May-2028,2,,10,20,20", sup=sup)
        p.invoice_no = f"HISTORY-{n}"
        rd.confirm(db, p, p.items[0], factor=200 if conflict and n == 2 else 10,
                   mrp_basis="MASTER_PACK", reason="Verified against supplier delivery")
        if not trusted:
            p.items[0].corrections = {**p.items[0].corrections, "_automation": {"action": "new_product"}}
        purchasing.post(db, p)
    db.flush()


@pytest.mark.parametrize("pack,n", [("10T",10),("15.",15),("1×10",10),("1*12",12),("20X5X10",10)])
def test_pack_normalization_preserves_retail_count(pack,n):
    parsed=units.parse_pack(pack)
    assert parsed.units_per_pack == n
    if pack == "20X5X10":
        assert parsed.outer_count == 100 and not parsed.confident


def test_clear_new_products_need_no_confirmation(db):
    auto_create(db)
    enable(db)
    p=draft(db, ",NEW 500TAB,10T,B1,May-2028,2.5,0.5,10,20,25",
            ",NEW SYRUP,1x60ML.,B2,May-2028,3,,10,20,30")
    assert all(l.status == "READY" for l in p.items)
    assert [l.receipt_decision['received_base_units'] for l in p.items] == [30,3]
    assert p.items[0].base_unit == 'TABLET'
    assert p.items[1].base_unit == 'BOTTLE'
    assert db.query(InventoryMovement).count() == 0


def test_three_reviewed_products_establish_scoped_layout_convention(db):
    sup=supplier(db)
    reviewed_history(db,sup)
    enable(db)
    p=draft(db,"N,NEW CAPSULE,10x1x10,B,May-2028,2.5,0.5,10,20,25",sup=sup)
    d=p.items[0].receipt_decision
    assert d['resolved'] and d['received_base_units']==30
    assert d['source']=='SUPPLIER_LAYOUT_CONVENTION'
    assert len(d['history_line_ids'])==3
    p.column_map=[{**c,'column':'Boxes' if c['field']=='quantity' else c['column']} for c in p.column_map]
    purchasing.refresh_line(db,p,p.items[0])
    assert not certain(p.items[0].receipt_decision)


@pytest.mark.parametrize('conflict,trusted',[(True,True),(False,False)])
def test_conflicting_or_automatic_history_cannot_authorize_new_nested_items(db,conflict,trusted):
    sup=supplier(db)
    reviewed_history(db,sup,conflict=conflict,trusted=trusted)
    enable(db)
    p=draft(db,"N,NEW TABLET,10x1x10,B,May-2028,2,,10,20,20",sup=sup)
    assert not certain(p.items[0].receipt_decision)


def test_unknown_count_does_not_become_one_pack(db):
    enable(db)
    p=draft(db,",UNIDENTIFIED,10,B,May-2028,2,,10,20,20")
    line=p.items[0]
    assert not (line.base_unit=='PACK' and line.units_per_pack==1)          # never silently one pack
    assert line.units_per_pack==10 and line.receipt_decision['received_base_units']==20
    assert not certain(line.receipt_decision)
    unknown=[i for i in line.issues if i['code']=='product_unit_unknown']
    assert unknown and unknown[0]['level']=='proposal'                      # shown for a person to check


def test_changed_source_discards_automatic_new_product_units(db):
    enable(db)
    p=draft(db,",NEW TABLET,10S,B,May-2028,2,,10,20,20")
    purchasing.correct(db,p,p.items[0],{'pack':'broken'})
    assert not certain(p.items[0].receipt_decision)
    assert p.items[0].units_per_pack!=10                                    # the old automatic units are gone


def test_explicit_product_conversion_is_preserved(db):
    enable(db)
    p=draft(db,",NEW TABLET,10S,B,May-2028,2,,10,20,20")
    purchasing.correct(db,p,p.items[0],{'base_unit':'TABLET','units_per_pack':'20'})
    auto.prepare(db,p)
    assert p.items[0].units_per_pack==20


def test_repeated_new_product_batches_create_one_item(db):
    auto_create(db)
    enable(db)
    p=draft(db,",NEW TABLET,10S,B1,May-2028,2,,10,20,20",
            ",NEW TABLET,10S,B2,May-2028,3,,10,20,30")
    purchasing.post(db,p)
    assert p.items[0].item_id == p.items[1].item_id
    assert db.query(Item).filter(Item.name=='NEW TABLET').count()==1
    assert stock(db,p.items[0].item)==50


def test_supplier_code_reassignment_is_held(db):
    sup=supplier(db)
    product(db,name='EXAMPLE 10MG TAB',pack='10S',upp=10)
    p=draft(db,'CODE,EXAMPLE 10MG TAB,10S,B1,May-2028,1,,10,20,10',sup=sup)
    purchasing.post(db,p)
    enable(db)
    p2=draft(db,'CODE,EXAMPLE 20MG TAB,10S,B2,May-2028,1,,10,20,10',sup=sup)
    assert 'supplier_code_changed' in {i['code'] for i in p2.items[0].issues}
    assert p2.items[0].status=='NEEDS_REVIEW'


def zero_tax_csv(*rows):
    lines = csv_bytes(*rows).decode().splitlines()
    return ('\n'.join([lines[0]+',GST %']+[r+',0' for r in lines[1:]])+'\n').encode()


def auto_create(db):
    """These tests cover automatic product creation, which a pharmacy must switch on (default off)."""
    settings_service.set_setting(db, 'purchase_auto_create_products', 'on')
    db.commit()


def auto_import(db, *, total='20', row=None, user=None, invoice='AUTO-1'):
    sup=supplier(db)
    sup.gst_number='36AAPFU0939F1ZW'
    settings_service.set_setting(db,'gst_number','36AAPFU0939F1ZW')
    enable(db,'post')
    row=row or 'N,NEW TABLET,10S,B1,May-2028,2,,10,20,20'
    return auto.import_file(db,'auto.csv',zero_tax_csv(row),supplier_id=sup.id,
                            invoice_no=invoice,invoice_date=date(2026,9,1),supplier_total=total,user=user)[0]


def test_complete_authorized_import_posts_once_and_retry_returns_original(db):
    auto_create(db)
    user=db.scalar(select(User).where(User.username=='admin')) or db.scalar(select(User))
    p=auto_import(db,user=user)
    assert p.status=='POSTED', str(auto.assessment(db,p))
    before=db.query(InventoryMovement).count()
    again=auto.import_file(db,'retry.csv',zero_tax_csv('N,NEW TABLET,10S,B1,May-2028,2,,10,20,20'),user=user)[0]
    assert again.id==p.id and db.query(InventoryMovement).count()==before
    assert stock(db,p.items[0].item)==20


@pytest.mark.parametrize('total,row,code',[
    ('999',None,'TOTAL_UNVERIFIED'),
    (None,None,'TOTAL_UNVERIFIED'),
    ('20','N,NEW TABLET,10S,,May-2028,2,,10,20,20','batch_missing'),
    ('20','N,NEW TABLET,10S,B1,,2,,10,20,20','expiry_missing'),
    ('20','N,NEW TABLET,10S,B1,May-2020,2,,10,20,20','expired'),
])
def test_unattended_post_requires_document_and_traceability_evidence(db,total,row,code):
    user=db.scalar(select(User))
    p=auto_import(db,user=user,total=total,row=row)
    assert p.status=='DRAFT' and db.query(InventoryMovement).count()==0
    a=auto.assessment(db,p)
    assert code in [b['code'] for b in a['document_blockers']]+[c for e in a['exceptions'] for c in e['codes']]


def test_automatic_post_still_requires_post_permission(db):
    p=auto_import(db,user=None)
    assert p.status=='DRAFT' and db.query(InventoryMovement).count()==0


def test_modified_invoice_number_case_does_not_duplicate_stock(db):
    auto_create(db)
    user=db.scalar(select(User))
    p=auto_import(db,user=user)
    sup=p.supplier
    p2=auto.import_file(db,'changed.csv',zero_tax_csv('N,NEW TABLET,10S,B2,May-2028,2,,10,20,20'),
                       supplier_id=sup.id,invoice_no='auto-1',invoice_date=date(2026,9,1),supplier_total='20',user=user)[0]
    assert p2.status=='DRAFT'
    assert 'DUPLICATE_INVOICE' in [b['code'] for b in auto.assessment(db,p2)['document_blockers']]
    assert stock(db,p.items[0].item)==20


def test_quantity_label_prevents_supplier_convention_reuse(db):
    sup=supplier(db)
    reviewed_history(db,sup)
    enable(db)
    p=draft(db,'N,NEW TABLET,10X10S,B,May-2028,2 boxes,,10,20,20',sup=sup)
    assert not certain(p.items[0].receipt_decision)


def test_reference_only_infers_form_without_replacing_name_or_strength(db,tmp_path,monkeypatch):
    from app import config
    from app.services.medicine_reference import Catalog
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    source=tmp_path/'reference.csv'
    source.write_text('name,manufacturer_name,pack_size_label\nCombination NT 400mg/10mg Tablet,Maker Ltd,strip of 10 tablets\n')
    Catalog(tmp_path/'medicine-reference.sqlite').ingest(source)
    enable(db)
    p=draft(db,',Combination NT 400,10S,B,May-2028,2,,10,20,20')
    purchasing.correct(db,p,p.items[0],{'manufacturer':'Maker'})
    l=p.items[0]
    assert l.product_name=='Combination NT 400' and l.base_unit=='TABLET'
    assert l.item is None
    assert l.corrections['_automation']['reference']['evidence_kind']=='form_only_variant_consensus'


def test_mixed_reference_forms_do_not_manufacture_identity(db,tmp_path,monkeypatch):
    from app import config
    from app.services.medicine_reference import Catalog, packaging_evidence
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    source=tmp_path/'reference.csv'
    source.write_text('name,manufacturer_name,pack_size_label\nExample 10mg Tablet,Maker Ltd,strip of 10 tablets\nExample 10mg Capsule,Maker Ltd,strip of 10 capsules\n')
    Catalog(tmp_path/'medicine-reference.sqlite').ingest(source)
    assert packaging_evidence('Example','Maker','10') is None


def test_post_failure_keeps_complete_draft_and_no_partial_inventory(db,monkeypatch):
    auto_create(db)
    from app.services import inventory_service
    from app.services import stock_ledger
    original=inventory_service.add_or_update_batch
    calls=[]
    def fail_second(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:
            raise stock_ledger.StockError('Injected receipt conflict')
        return original(*args,**kwargs)
    monkeypatch.setattr(inventory_service,'add_or_update_batch',fail_second)
    enable(db,'post')
    sup=supplier(db)
    sup.gst_number='36AAPFU0939F1ZW'
    settings_service.set_setting(db,'gst_number',sup.gst_number)
    p=auto.import_file(db,'two.csv',zero_tax_csv('N,NEW TABLET,10S,B1,May-2028,2,,10,20,20',
                      'N,NEW TABLET,10S,B2,May-2028,2,,10,20,20'),supplier_id=sup.id,
                      invoice_no='ROLLBACK',invoice_date=date(2026,9,1),supplier_total='40',user=db.scalar(select(User)))[0]
    assert p.status=='DRAFT' and all(l.status=='READY' for l in p.items)
    assert db.query(InventoryMovement).count()==0
    assert db.query(Item).filter_by(name='NEW TABLET').count()==0
    assert p.charges['_automation']['document_blockers'][-1]['code']=='STOCK_ERROR'


def test_short_expiry_remains_visible_but_only_policy_breaches_block(db):
    auto_create(db)
    from datetime import timedelta
    enable(db)
    future=date.today()+timedelta(days=65)
    p=draft(db,f',NEW TABLET,10S,B1,{future:%b-%Y},2,,10,20,20')
    assert p.items[0].status=='READY'
    assert any(i['code']=='expiry_soon' and i['level']=='info' for i in p.items[0].issues)
    settings_service.set_setting(db,'purchase_min_shelf_days','120')
    purchasing.refresh_line(db,p,p.items[0])
    assert p.items[0].status=='NEEDS_REVIEW'


def test_strip_count_does_not_invent_tablet_form(db):
    enable(db)
    p=draft(db,',UNIDENTIFIED BRAND,10S,B,May-2028,3,,10,20,30')
    assert p.items[0].base_unit=='UNIT' and not p.items[0].dosage_form
    assert p.items[0].receipt_decision['received_base_units']==30


def test_strength_is_not_a_retail_pack_count(db):
    enable(db)
    p=draft(db,',NEW TABLET,500MG,B,May-2028,2,,10,20,20')
    assert not certain(p.items[0].receipt_decision)
    assert 'strength_not_pack' in {i['code'] for i in p.items[0].issues}


def test_cross_format_copy_identifies_supplier_only_when_every_row_agrees(db):
    product(db)
    rows=[f'C{n},EXAMPLE 40MG TAB,15S,B{n},May-2028,1,,10,20,10' for n in range(3)]
    p=draft(db,*rows,total='30')
    purchasing.post(db,p)
    enable(db)
    # This checks the row fingerprint alone; layout-profile recognition is tested in test_supplier_profiles.
    from app.models import SupplierInvoiceProfile
    db.query(SupplierInvoiceProfile).delete()
    # Different file bytes/layout but identical invoice identity and line values.
    other=purchasing.create_from_file(db,'copy.csv',csv_bytes(*rows)+b'\n',invoice_no=p.invoice_no,supplier_total='30')
    assert other.supplier_id is None
    auto.prepare(db,other)
    assert other.supplier_id==p.supplier_id
    assert 'DUPLICATE_INVOICE' in [b['code'] for b in auto.assessment(db,other)['document_blockers']]
    other.supplier_id=None
    other.supplier=None
    purchasing.correct(db,other,other.items[0],{'batch':'DIFFERENT'})
    auto.prepare(db,other)
    assert other.supplier_id is None


def test_autopost_rejects_small_line_error_even_if_invoice_total_agrees(db):
    user=db.scalar(select(User))
    p=auto_import(db,user=user,row='N,NEW TABLET,10S,B1,May-2028,2,,10,20,20.50')
    assert p.status=='DRAFT'
    assert 'line_amount_unreconciled' in auto.assessment(db,p)['exceptions'][0]['codes']


def test_missing_prices_do_not_overwrite_batch_prices_on_auto_post(db):
    user=db.scalar(select(User))
    p=auto_import(db,user=user,row='N,NEW TABLET,10S,B1,May-2028,2,,,,20')
    assert p.status=='DRAFT'
    codes=auto.assessment(db,p)['exceptions'][0]['codes']
    assert {'rate_missing','mrp_missing'}<=set(codes)


def test_n_times_one_requires_retail_evidence_not_a_one_tablet_guess(db,tmp_path,monkeypatch):
    auto_create(db)
    from app import config
    from app.services.medicine_reference import Catalog
    sup=supplier(db)
    reviewed_history(db,sup)
    enable(db)
    p=draft(db,'X,NEW 10MG TAB,10X1,B1,May-2028,2,,10,20,20',sup=sup)
    assert not certain(p.items[0].receipt_decision)
    monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    source=tmp_path/'ref.csv'
    source.write_text('name,manufacturer_name,pack_size_label\nNEW 10mg Tablet,Maker Ltd,strip of 10 tablets\n')
    Catalog(tmp_path/'medicine-reference.sqlite').ingest(source)
    purchasing.correct(db,p,p.items[0],{'manufacturer':'Maker'})
    assert p.items[0].receipt_decision['received_base_units']==20
    p.invoice_no='NEW'
    purchasing.post(db,p)
    again=purchasing.create_from_file(db,'next.csv',csv_bytes('X,NEW 10MG TAB,10X1,B2,May-2028,2,,10,20,20'),
                                     supplier_id=sup.id,invoice_no='NEXT')
    assert again.items[0].receipt_decision['received_base_units']==20


def test_existing_upload_api_posts_verified_invoice_and_returns_idempotent_result(client,db):
    auto_create(db)
    from tests.conftest import login
    login(client)
    enable(db,'post')
    sup=supplier(db)
    sup.gst_number='36AAPFU0939F1ZW'
    settings_service.set_setting(db,'gst_number',sup.gst_number)
    sid=sup.id
    db.commit()
    content=zero_tax_csv('N,NEW TABLET,10S,B1,May-2028,2.5,0.5,10,20,25')
    data={'supplier_id':str(sid),'invoice_no':'API-AUTO','invoice_date':'2026-09-01','supplier_total':'25'}
    first=client.post('/api/erp/purchases/import',data=data,files={'file':('supplier.csv',content,'text/csv')})
    assert first.status_code==200, first.text
    document=first.json()
    assert document['purchase']['status']=='POSTED'
    assert document['lines'][0]['receipt']['received_base_units']==30
    retry=client.post('/api/erp/purchases/import',data=data,files={'file':('supplier.csv',content,'text/csv')})
    assert retry.status_code==200 and retry.json()['purchase']['id']==document['purchase']['id']
    assert sum(m.quantity for m in db.scalars(select(InventoryMovement)))==30





def test_new_products_are_proposed_and_never_posted_unattended(db):
    from app.services import confidence_gate
    enable(db)
    p=draft(db,'N,BRAND NEW 10MG TAB,10S,B1,May-2028,2,,10,20,20')
    line=p.items[0]
    assert line.new_product and line.units_per_pack==10            # units prepared from the pack
    assert line.status=='READY'                                     # counted: a person posts it
    issue=next(i for i in line.issues if i['code']=='new_product_unconfirmed')
    assert issue['level']=='proposal'
    assert confidence_gate.assess(line)['state']==confidence_gate.WARNING
    assert 'new_product_unconfirmed' in auto.assessment(db,p)['exceptions'][0]['codes']   # not unattended
    purchasing.correct(db,p,line,{'new_product':True})              # Shift+F4: a person confirms it
    assert not any(i['code']=='new_product_unconfirmed' for i in line.issues)


def test_ambiguous_count_is_decided_by_price_evidence_and_shown(db):
    enable(db)
    p=draft(db,',BEGROEASE-50 TABLET,10X1,B1,May-2028,1,,150,320,150')
    line=p.items[0]
    # 10X1: ten packs of one tablet, or a strip of ten? ₹320 for one tablet is implausible: a strip of ten
    assert line.new_product and line.units_per_pack==10
    auto_info=line.corrections['_automation']
    assert auto_info['proposed'] and 'other reading' in auto_info['reason']
    assert not certain(line.receipt_decision)
