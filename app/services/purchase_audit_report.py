"""Supplier invoice audit from stored purchase evidence, not current stock balances."""
import json
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload
from app.models import Purchase, PurchaseItem
from app.services import gst, purchasing, receipt_decision


def columns(col):
    text = [('date','Received date'),('reference','ERP reference'),('invoice','Supplier invoice'),
            ('invoice_date','Invoice date'),('supplier','Supplier'),('gstin','Supplier GSTIN'),
            ('status','Invoice status'),('line','Line'),('item','Supplier product'),('code','Item code'),
            ('supplier_code','Supplier code'),('category','Category'),('manufacturer','Manufacturer'),
            ('form','Form'),('pack','Invoice pack'),('base_unit','Base unit'),('pack_unit','Retail pack unit'),
            ('batch','Batch'),('expiry','Expiry'),('hsn','HSN'),('line_status','Line status'),('quantity_raw','Printed quantity')]
    numeric = [('paid','Billed quantity'),('free','Free quantity'),('units_per_pack','Units / retail pack'),
               ('received','Received base units'),('equivalent','Received retail packs'),('gst_rate','GST %'),('discount','Line discount %')]
    amounts = [('rate','Invoice rate'),('mrp','Invoice MRP'),('amount','Invoice line amount'),
               ('taxable','Received taxable value'),('cgst','CGST'),('sgst','SGST'),('igst','IGST'),
               ('gst','GST amount'),('landed','Received value incl. GST')]
    tail = [('invoice_total','ERP invoice total'),('supplier_total','Printed invoice total'),
            ('difference','ERP minus printed total')]
    return ([col(k,l) for k,l in text] + [col(k,l,'number') for k,l in numeric]
            + [col(k,l,'money',total=k in {'taxable','cgst','sgst','igst','gst','landed'}) for k,l in amounts]
            + [col(k,l,'money') for k,l in tail]
            + [col('charges','Invoice charges / rounding'),col('source','Source file'),
               col('source_hash','Source SHA256',default=False),col('evidence','Receipt evidence',default=False),col('corrections','Corrections',default=False),
               col('raw','Original supplier values',default=False),col('issues','Review issues',default=False)])


def documents(db, p, start, end):
    q = select(Purchase)
    if p.get('period')!='all':
        q=q.where(Purchase.purchase_date >= start, Purchase.purchase_date < end,Purchase.status.in_(('POSTED','PARTIAL')))
    if p.get('supplier'): q = q.where(Purchase.supplier_id == int(p['supplier']))
    if p.get('invoice'): q = q.where(Purchase.invoice_no.ilike('%'+p['invoice']+'%'))
    return list(db.scalars(q.options(joinedload(Purchase.supplier),selectinload(Purchase.items).joinedload(PurchaseItem.item))
                          .order_by(Purchase.purchase_date,Purchase.id)).unique())


def rows(db, p, start, end, tz, day_of, matches, error):
    docs = documents(db,p,start,end)
    ids = {int(v) for v in p.get('invoice_ids','').split(',') if v}
    if ids - {d.id for d in docs}: raise error('Selected invoices do not belong to this supplier and date range. Refresh invoice selection.')
    result = []
    for doc in docs:
        if ids and doc.id not in ids: continue
        for line in doc.items:
            if not matches(line.item,p,name=line.product_name): continue
            v = purchasing.effective(line); decision = line.receipt_decision or {}
            try:
                paid,free=receipt_decision.quantities({'quantity':v.get('quantity',line.quantity),'free':v.get('free',line.quantity_free)})
            except ValueError:
                paid=free=None
            posted = line.status == 'POSTED'
            tax = None
            if posted:
                tax = dict(taxable=line.taxable_value,gst=line.gst_amount,cgst=line.cgst_amount,sgst=line.sgst_amount,
                           igst=line.igst_amount,landed=line.landed_total)
                if line.gst_amount is None:
                    tax = gst.line_breakdown(line,gst.bill_discount_factor(doc),doc.supply_type or 'INTRA',purchasing._received_packs(line))
            item = line.item
            packaging = (line.corrections or {}).get('_physical_adjustment',{}).get('definition',{})
            units=packaging.get('units_per_pack') or line.units_per_pack
            if posted and decision.get('resolved') and Decimal(decision.get('master_pack_equivalent') or '0'):
                units=Decimal(decision['received_base_units'])/Decimal(decision['master_pack_equivalent'])
            row = dict(date=day_of(doc.purchase_date,tz).isoformat(),reference=doc.reference_no or '',invoice=doc.invoice_no,
                       invoice_date=str(doc.invoice_date or ''),supplier=doc.supplier.name if doc.supplier else '',
                       gstin=doc.supplier.gst_number or '' if doc.supplier else '',status=doc.status,line=line.line_no,
                       item=v.get('name') or line.product_name,code=item.article_id if item else '',supplier_code=line.supplier_code,
                       category=line.category or (item.category if item else ''),manufacturer=v.get('manufacturer') or '',
                       form=packaging.get('dosage_form') or line.dosage_form or (item.dosage_form if item else ''),pack=v.get('pack') or line.pack_size or '',
                       base_unit=decision.get('base_unit') or packaging.get('base_unit') or line.base_unit or '',
                       pack_unit=packaging.get('pack_unit') or line.pack_unit or (item.pack_unit if item else ''),
                       units_per_pack=units,
                       batch=line.batch_no,expiry=str(line.expiry_date or ''),hsn=line.hsn_code,line_status=line.status,
                       paid=paid,free=free,quantity_raw=str(v.get('quantity',line.quantity_raw or line.quantity)),
                       received=decision.get('received_base_units') if posted else None,
                       equivalent=purchasing._received_packs(line) if posted else None,gst_rate=line.gst_rate,discount=line.discount,
                       rate=line.rate,mrp=line.mrp,amount=line.line_total,invoice_total=doc.total,supplier_total=doc.supplier_total,
                       difference=doc.total-doc.supplier_total if doc.supplier_total is not None else None,
                       charges=json.dumps({k:v for k,v in (doc.charges or {}).items() if not k.startswith('_')},ensure_ascii=False),source=doc.source_file,source_hash=doc.source_sha256,
                       evidence=json.dumps(decision,ensure_ascii=False),corrections=json.dumps(line.corrections or {},ensure_ascii=False),
                       raw=json.dumps(line.raw or {},ensure_ascii=False),issues=json.dumps(line.issues or [],ensure_ascii=False),
                       _drill=dict(url=f'/purchases/{doc.id}'))
            for key in ('taxable','cgst','sgst','igst','gst','landed'): row[key] = tax.get(key) if tax else None
            result.append(row)
    return result
