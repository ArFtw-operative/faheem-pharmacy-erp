"""Operator-counted physical packaging, staged until a purchase is posted."""
from app.services import purchasing, receipt_decision as rd, inventory_service as inv

FORMS = {
    'TABLET': ('TABLET','STRIP','TABLET'), 'CAPSULE': ('CAPSULE','STRIP','CAPSULE'),
    'SYRUP': ('BOTTLE','BOTTLE','SYRUP'), 'DROPS': ('BOTTLE','BOTTLE','DROPS'),
    'CREAM': ('TUBE','TUBE','CREAM'), 'OINTMENT': ('TUBE','TUBE','OINTMENT'),
    'GEL': ('TUBE','TUBE','GEL'), 'POWDER': ('PACK','PACK','POWDER'),
    'SOAP': ('PIECE','PIECE','SOAP'), 'INJECTION': ('VIAL','VIAL','INJECTION'),
    'INHALER': ('PIECE','PIECE','INHALER'), 'SACHET': ('SACHET','SACHET','SACHET'),
    'PIECE': ('PIECE','PIECE','DEVICE'), 'UNIT': ('UNIT','PACK',''),
    'BOTTLE': ('BOTTLE','BOTTLE',''), 'TUBE': ('TUBE','TUBE',''), 'PACK': ('PACK','PACK',''),
    'VIAL': ('VIAL','VIAL',''), 'AMPOULE': ('AMPOULE','BOX','INJECTION'),
    'JAR': ('JAR','JAR',''), 'BOX': ('BOX','BOX',''), 'KIT': ('KIT','KIT','KIT'), 'PAIR': ('PAIR','PACK','DEVICE'),
}

def snapshot(item):
    return {k:getattr(item,k) for k in ('base_unit','pack_unit','units_per_pack','dosage_form')}

def adjust(db, purchase, line, values, *, user=None):
    purchasing._open(purchase)
    purchasing._open_line(line)
    form = values.get('form')
    if form not in FORMS:
        raise purchasing.PurchaseError('Choose the physical item form')
    n = values.get('units_per_pack')
    if isinstance(n,bool) or not str(n).isdigit() or not 1 <= int(n) <= 10000:
        raise purchasing.PurchaseError('Units per strip/container must be a whole number from 1 to 10000')
    base, pack, dosage = FORMS[form]
    if form not in {'TABLET','CAPSULE','UNIT'} and int(n)!=1:
        raise purchasing.PurchaseError('Whole containers are counted individually; use one unit per container')
    quantity, free = values.get('quantity'), values.get('free', '0')
    try:
        paid, bonus = rd.quantities(dict(quantity=quantity,free=free))
    except ValueError as exc:
        raise purchasing.PurchaseError(str(exc))
    if (paid+bonus)<=0 or ((paid+bonus)*int(n))%1:
        raise purchasing.PurchaseError('The total must be a positive whole number of physical stock units')
    definition = dict(base_unit=base,pack_unit=pack,units_per_pack=int(n),dosage_form=dosage)
    original = snapshot(line.item) if line.item else None
    item_id = line.item.id if line.item else None
    if line.item and original != definition:
        from app.permissions import has_permission
        if user is not None and not has_permission(user,'inventory.edit'):
            raise purchasing.PurchaseError('Editing an existing product packaging definition requires inventory edit permission')
        if inv.stock_on_hand(db,item_id) and (line.item.units_per_pack or 1)!=1:
            raise purchasing.PurchaseError('This product already holds individually counted stock. Correct its packaging at zero stock or select the proper size variant.')
    billed_paid,billed_free = rd.quantities(rd.effective(line))
    if billed_paid+billed_free <= 0:
        raise purchasing.PurchaseError('Correct the invoice billed quantity before adjusting received stock')
    changes = {**definition}
    if item_id:
        changes['item_id']=item_id
    else:
        changes['new_product']=True
    purchasing.correct(db,purchase,line,changes,user=user)
    if item_id:
        line.corrections={**(line.corrections or {}),'_physical_adjustment':dict(item_id=item_id,before=original,definition=definition)}
    rd.confirm(db,purchase,line,factor=int(n),mrp_basis='MASTER_PACK',
               reason=f'Operator counted {int(n)} {base.lower()}s per {pack.lower()} using the simple quantity adjustment',user=user)
    if (paid,bonus)!=(billed_paid,billed_free):
        # A count for this delivery is not a reusable supplier billing convention.
        stamp={**line.corrections['_invoice_unit'],'operator_counts':dict(
            paid=str(paid*int(n)),free=str(bonus*int(n)),
            billed_paid=str(billed_paid),billed_free=str(billed_free))}
        line.corrections={**line.corrections,'_invoice_unit':stamp}
        purchasing.refresh_line(db,purchase,line)
        purchasing._refresh_totals(purchase)
        from app import audit
        audit.record(db,action=audit.A_UPDATE,entity_type='purchase_line',entity_id=line.id,user=user,
                     after={'physical_count':stamp['operator_counts']},details='Counted received stock; preserved invoice billing')
    return line

def apply_at_post(db, line, *, user=None):
    stamp=(line.corrections or {}).get('_physical_adjustment')
    if not stamp or not line.item:
        return
    if snapshot(line.item)==stamp['definition']:
        return
    if snapshot(line.item)!=stamp['before']:
        raise purchasing.PurchaseError('Product packaging changed after this adjustment. Recheck the line before posting.')
    try:
        inv.update_item(db,line.item,user=user,**stamp['definition'],loose_sale='auto')
    except inv.InventoryError as exc:
        raise purchasing.PurchaseError(str(exc))
