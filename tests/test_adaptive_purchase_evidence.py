from decimal import Decimal

from app.models import Item
from app.services import inventory_service as inv, purchasing, receipt_decision as rd
from tests.test_purchasing import draft
from tests.test_purchase_automation import enable
from tests.test_receipt_decisions import product


def codes(line):
    return {i['code'] for i in line.issues if i['level'] in {'review','match','warn','block'}}


def test_existing_generic_count_does_not_bypass_new_product_checks(db):
    enable(db)
    item=inv.create_item(db,name='GENERIC BRAND',pack_size='12',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',GENERIC BRAND,12,B,May-2028,2,,10,20,20')
    assert p.items[0].item_id==item.id
    assert 'master_unit_unverified' in codes(p.items[0])
    assert not p.items[0].receipt_decision['resolved']


def test_known_dose_name_with_content_weight_requires_pack_evidence(db):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE TABLETS',pack_size='20GM',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',EXAMPLE TABLETS,20GM,B,May-2028,1,,10,20,10')
    assert 'strength_not_pack' in codes(p.items[0])
    assert not p.items[0].receipt_decision['resolved']


def test_unknown_weight_cannot_corroborate_tube_master(db):
    enable(db)
    item=inv.create_item(db,name='EXAMPLE POWDER',pack_size='300GM',base_unit='TUBE',pack_unit='TUBE',units_per_pack=1)
    # Simulate a legacy/manual catalogue definition. It must be checked too.
    item.base_unit=item.pack_unit='TUBE'
    item.packaging_source='MANUAL'
    db.flush()
    p=draft(db,',EXAMPLE POWDER,300GM,B,May-2028,1,,10,20,10')
    assert 'master_container_unverified' in codes(p.items[0])
    assert not p.items[0].receipt_decision['resolved']


def test_explicit_whole_pack_confirmation_can_resolve_unknown_count(db):
    enable(db)
    inv.create_item(db,name='WHOLE DEVICE PACK',pack_size='12',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',WHOLE DEVICE PACK,12,B,May-2028,2,,10,20,20')
    assert not p.items[0].receipt_decision['resolved']
    rd.confirm(db,p,p.items[0],factor=1,mrp_basis='MASTER_PACK',reason='Verified sealed pack sold whole')
    assert p.items[0].receipt_decision['resolved']
    assert p.items[0].receipt_decision['received_base_units']==2


def test_equivalent_existing_records_are_reused_without_new_product(db):
    enable(db)
    first=product(db,pack='10S',upp=10)
    second=product(db,pack='10S',upp=10)
    before=db.query(Item).count()
    p=draft(db,',EXAMPLE 40MG TAB,10S,B,May-2028,2,,10,20,20')
    assert p.items[0].item_id==first.id
    assert not p.items[0].new_product
    assert p.items[0].receipt_decision['received_base_units']==20
    purchasing.post(db,p)
    assert db.query(Item).count()==before
    assert second.id!=first.id


def test_identical_short_manufacturer_codes_are_not_rejected(db):
    enable(db)
    for _ in range(2):
        inv.create_item(db,name='EXAMPLE SOAP',manufacturer='ABC',pack_size='50GM',base_unit='PIECE',pack_unit='PIECE',units_per_pack=1)
    p=draft(db,',EXAMPLE SOAP,50GM,B,May-2028,1,,10,20,10')
    purchasing.correct(db,p,p.items[0],{'manufacturer':'ABC'})
    assert p.items[0].item is not None
    assert not p.items[0].new_product


def test_explicit_tube_label_corroborates_container_without_a_form_guess(db):
    enable(db)
    inv.create_item(db,name='EXAMPLE ORAL TUBE',pack_size='10GM',base_unit='TUBE',pack_unit='TUBE',units_per_pack=1)
    p=draft(db,',EXAMPLE ORAL TUBE,10GM,B,May-2028,1,,10,20,10')
    assert 'master_container_unverified' not in codes(p.items[0])
    assert p.items[0].receipt_decision['resolved']


def test_conflicting_duplicates_do_not_turn_into_new_product(db):
    enable(db)
    product(db,pack='10S',upp=10)
    contrary=product(db,pack='10S',upp=10)
    contrary.units_per_pack=20
    db.flush()
    p=draft(db,',EXAMPLE 40MG TAB,10S,B,May-2028,2,,10,20,20')
    assert p.items[0].item is None
    assert not p.items[0].new_product
    assert 'catalogue_conflict' in codes(p.items[0])


def test_cent_rounding_preserves_printed_net_amount_and_trace(db):
    enable(db)
    p=draft(db,',ROUNDING TABLET,10S,B,May-2028,3,,44.91,60,134.72')
    line=p.items[0]
    assert line.line_total==Decimal('134.72')
    evidence=next(i['evidence'] for i in line.issues if i['code']=='source_rounding')
    assert evidence==dict(calculated='134.73',printed='134.72',difference='-0.01')
    from app.services import purchase_automation as auto
    assert auto.assessment(db,p)['rounding'][0]['difference']=='-0.01'


def test_tax_inclusive_printed_amount_does_not_replace_taxable_value(db):
    enable(db)
    p=draft(db,',ROUNDING TABLET,10S,B,May-2028,2,,10,20,21')
    purchasing.correct(db,p,p.items[0],{'gst':'5'})
    assert p.items[0].line_total==20
    assert not any(i['code']=='source_rounding' for i in p.items[0].issues)


def test_shared_hsn_observation_is_separate_from_financial_checks(db):
    enable(db)
    p=draft(db,',EXAMPLE SOAP,75GM,B,May-2028,1,,10,20,10',
            ',OTHER SOAP,100GM,C,May-2028,1,,10,20,10')
    purchasing.correct(db,p,p.items[0],{'gst':'5','hsn':'12345678'})
    purchasing.correct(db,p,p.items[1],{'gst':'18','hsn':'12345678'})
    from app.services import purchase_automation as auto
    auto.prepare(db,p)
    result=auto.assessment(db,p)
    assert not any('gst_hsn_mixed' in e['codes'] for e in result['exceptions'])
    assert result['observations'][0]['lines']==[1,2]
    purchasing.update_header(db,p,{'charges':{'printed_gst':'99'}})
    assert any(b['code'].startswith('TAX_TOTAL') for b in auto.assessment(db,p)['document_blockers'])


def test_repeated_product_exceptions_share_one_group(db):
    enable(db)
    inv.create_item(db,name='UNKNOWN BRAND',pack_size='12',base_unit='PACK',pack_unit='PACK',units_per_pack=1)
    p=draft(db,',UNKNOWN BRAND,12,B,May-2028,1,,10,20,10',
            ',UNKNOWN BRAND,12,C,May-2028,2,,10,20,20')
    from app.services import purchase_automation as auto
    result=auto.assessment(db,p)
    assert len(result['exceptions'])==2
    assert len(result['exception_groups'])==1
    assert result['exception_groups'][0]['lines']==[1,2]
