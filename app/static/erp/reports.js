// Report selector -> parameters -> explicit generation -> spreadsheet document.
import { $, $$, api, esc, h, money, store, modal } from 'erp/core';
import * as keys from 'erp/keys';

const FILTERS = {
  invoice_ids: ['Invoices','hidden'],
  category: ['Category','select'], manufacturer: ['Brand / Manufacturer','select'], supplier: ['Supplier','select'],
  item: ['Item','lookup'], batch: ['Batch','text'], invoice: ['Invoice number','text'], customer: ['Customer','lookup'],
  profit_basis: ['Profit basis','select',[['realized','Actual sale value'],['mrp','MRP margin']]],
  group_by: ['Group by','select',[['day','Day'],['week','Week'],['month','Month']]],
  view: ['View','select',[['summary','Summary'],['detail','Bill detail']]],
  sort: ['Sort by','select',[['value','Sales Value'],['quantity','Quantity'],['item','Item Name']]],
  show: ['Show','select',[['sold','Sold Items Only'],['all','All Items']]],
  payment: ['Payment','select',[['','All'],['CASH','Cash'],['UPI','UPI'],['CARD','Card'],['SPLIT','Split bills']]],
  refund_method: ['Refund method','select',[['','All'],['CASH','Cash'],['UPI','UPI'],['CARD','Card']]],
  stock_status: ['Stock status','select',[['','All'],['positive','In Stock'],['zero','Out of Stock'],['low','Low Stock'],['expired','Expired']]],
  expiry_window: ['Expiry window','select',[['','Default / All'],['expired','Already Expired'],['30','Next 30 Days'],['60','Next 60 Days'],['90','Next 90 Days'],['180','Next 180 Days']]],
  movement_type: ['Movement type','select',[['','All'],['OPENING_STOCK','Opening Stock'],['PURCHASE','Purchase'],['SALE','Sale'],['SALE_VOID','Sale Void'],['SALE_RETURN','Sale Return'],['PURCHASE_RETURN','Purchase Return'],['ADJUSTMENT_IN','Adjustment In'],['ADJUSTMENT_OUT','Adjustment Out'],['REPACK_IN','Repack In'],['REPACK_OUT','Repack Out']]],
  inactive_days: ['No sale for','select',[['30','30 days'],['60','60 days'],['90','90 days'],['180','180 days'],['7','7 days'],['15','15 days'],['custom','Custom (days)']]],
  inactive_custom: ['Custom days','text'],
  split: ['Split by','select',[['','No split (whole range)'],['day','Day'],['week','Week'],['month','Month']]],
  level: ['Level','select',[['item','Product'],['batch','Product + batch']]],
  gst_view: ['View','select',[['line','Invoice lines'],['invoice','By invoice'],['item','By product'],['rate','By GST rate'],['hsn','By HSN'],['supplier','By supplier'],['month','By month']]],
  gst_rate: ['GST %','select',[['','All'],['0','0%'],['0.25','0.25%'],['3','3%'],['5','5%'],['12','12%'],['18','18%'],['28','28%'],['40','40%']]],
  top: ['Show top','select',[['25','Top 25'],['10','Top 10'],['50','Top 50'],['100','Top 100'],['all','All']]],
  rank_by: ['Rank by','select',[['quantity','Quantity sold'],['value','Sale value'],['bills','Number of bills']]],
  min_bills: ['Minimum bills','text'], min_value: ['Minimum purchase ₹','text'],
  exclude_no_mobile: ['Without mobile','select',[['','Include'],['1','Exclude']]],
  exclude_open_followup: ['With open follow-up','select',[['','Include'],['1','Exclude']]],
  followup_scope: ['Show','select',[['open','All open'],['overdue','Overdue'],['today','Today'],['next7','Next 7 days'],['all','Everything']]],
  operator: ['Created by','select'], customer_id: ['','hidden'],
  racks: ['Racks (Ctrl+click several)','multi'], rack: ['Rack','select'], box: ['Box','select'],
  location: ['Location','select',[['','Racks + unassigned'],['assigned','In a rack'],['unassigned','Unassigned only']]],
  stock_condition: ['Stock','select',[['','In stock'],['all','In stock + out of stock'],['zero','Out of stock only'],['expired','Expired only']]],
  item_status: ['Product status','select',[['','All'],['active','Active'],['disabled','Disabled']]],
  adjustment_type: ['Adjustment type','select',[['','All'],['LOOSE','Loose'],['DAMAGE','Damage'],['EXPIRED','Expired'],['COUNT','Count Correction'],['ADJUSTMENT_IN','Adjustment In'],['ADJUSTMENT_OUT','Adjustment Out'],['CUSTOMER_RETURN','Customer Return']]],
};
const option = (value,label) => `<option value="${esc(value)}">${esc(label)}</option>`;
const fmt = (column,value) => value == null ? '—' : column.kind === 'money' ? money(value) : column.kind === 'number' ? Number(value).toLocaleString('en-IN') : String(value).replaceAll('COST_MISSING','Cost data missing').replaceAll('COST_RESOLVED','Cost resolved').replaceAll('COST_AMBIGUOUS','Cost ambiguous');
const dateLabel = (d) => { const [y,m,day]=String(d).split('-'); return `${day}-${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][Number(m)-1]}-${y}`; };

export function create(ctx,params,root) {
  let catalog=ctx.boot.report_catalog || [], options=null, selected=null, document=null;
  let request=null, version=0, rowAt=0, canExport=!!ctx.boot.can?.['reports.export'];
  let invoiceVersion=0, invoicesLoading=false;
  const prefKey=() => `report-columns:${ctx.boot.user?.username}:${selected.id}:${$('select[name=view]',root)?.value || ''}:${selected.id==='supplier-purchases'&&$('[name=supplier]',root)?.value?'audit':'summary'}`;
  function cancel() { version++; if(request)request.abort();request=null; }
  function statusError(error) { if(error.name!=='AbortError')ctx.status(error.message,'error'); }
  function showSelector(focus=true) {
    cancel();selected=null;document=null;ctx.setTitle('Reports');ctx.setKeys();
    root.innerHTML=`<div class="report-module"><header class="report-heading"><h1>Reports</h1><p>Generate operational, sales, purchase and inventory reports</p></header><div class="report-selector"></div></div>`;
    const groups=[...new Set(catalog.map(r=>r.group))];
    $('.report-selector',root).innerHTML=groups.map(group=>`<section class="report-group"><h2>${esc(group)}</h2><div class="report-cards">${catalog.filter(r=>r.group===group).map(r=>`<button type="button" class="report-card" data-report="${r.id}"><strong>${esc(r.title)}</strong><span>${esc(r.description)}</span><small>Generate →</small></button>`).join('')}</div></section>`).join('') || '<p class="muted">No reports are available for your role.</p>';
    if(focus)$('[data-report]',root)?.focus();
  }
  function choices(name) {
    if(name==='movement_type')return [['','All'],...options.movements];
    if(name==='category')return [['','All'],...options.categories.map(v=>[v || '',(options.category_names||{})[v] || v || 'Uncategorised'])];
    if(name==='manufacturer')return [['','All'],...options.manufacturers.map(v=>[v,v])];
    if(name==='supplier')return [['','All'],...options.suppliers.map(s=>[s.value,s.label])];
    if(name==='operator')return [['','Everyone'],...(options.operators||[]).map(s=>[s.value,s.label])];
    if(name==='racks')return (options.racks||[]).map(s=>[s.value,s.label]);
    if(name==='rack')return [['','Choose a rack'],...(options.racks||[]).map(s=>[s.value,s.label])];
    if(name==='box')return [['','All boxes'],...(options.boxes||[]).map(s=>[s.value,s.label])];
    return FILTERS[name]?.[2] || [];
  }
  function filter(name) {
    const [label,type]=FILTERS[name];
    if(type==='hidden')return `<input type="hidden" name="${name}">`;
    if(type==='multi')return `<label>${label}<select name="${name}" multiple size="4">${choices(name).map(([v,l])=>option(v,l)).join('')}</select></label>`;
    if(type==='lookup')return `<label class="report-lookup">${label}<input name="${name}" placeholder="${name==='customer'?'Name, mobile or ID':'Product name or code'}" autocomplete="off" spellcheck="false" role="combobox" aria-autocomplete="list" aria-expanded="false"><div class="lookup-drop" role="listbox" hidden></div></label>`;
    return `<label>${label}${type==='select'?`<select name="${name}">${choices(name).map(([v,l])=>option(v,l)).join('')}</select>`:`<input name="${name}" placeholder="All" autocomplete="off">`}</label>`;
  }
  async function choose(id,preset={},focus=true) {
    cancel();const ownVersion=version;document=null;
    selected=catalog.find(r=>r.id===id);if(!selected)return;
    ctx.setTitle('Reports · '+selected.title);ctx.setKeys();
    root.innerHTML='<div class="report-module"><p class="muted">Loading report parameters…</p></div>';
    try { if(!options) options=await api('/reports/api/options'); } catch(error){statusError(error);showSelector();return;}
    if(ownVersion!==version)return;
    if(selected.id==='supplier-purchases'){supplierBrowser(preset);return;}
    const stock=selected.filters.includes('as_of');
    root.innerHTML=`<div class="report-module"><header class="report-heading report-breadcrumb"><button type="button" class="btn" data-selector>← Reports</button><h1>${esc(selected.title)}</h1></header>
      <form class="report-parameters" aria-label="Report parameters">
        ${stock?`<label>As of date<input name="as_of" type="date" value="${options.today}" max="${options.today}" required></label>`:`<label>Period<select name="period">${options.periods.map(([v,l])=>option(v,l)).join('')}</select></label><label>From<input name="from" type="date" required></label><label>To<input name="to" type="date" required></label>`}
        ${selected.filters.filter(n=>n!=='as_of').map(filter).join('')}
        ${selected.id==='supplier-purchases'?'<fieldset class="report-invoices" hidden><legend>Available invoices</legend><p data-invoice-notice></p><div data-invoice-list></div></fieldset>':''}
        <div class="report-generate"><button type="submit" class="btn primary" data-generate>Generate Report <kbd data-shortcut="reports.generate">${esc(keys.keyFor('reports.generate'))}</kbd></button><button type="button" class="btn" data-reset>Reset</button><button type="button" class="btn" data-columns>Columns ⚙</button></div>
      </form><div class="report-notice" role="status">Choose parameters, then generate the report.</div>
      <div class="report-result"></div></div>`;
    const form=$('form',root);
    for(const [name,value] of Object.entries(preset)){const field=form.elements.namedItem(name);if(field)field.value=value;}
    $$('.report-lookup input',form).forEach(lookupField);
    updatePeriod();
    if(selected.id==='supplier-purchases')loadInvoices();
    form.onsubmit=e=>{e.preventDefault();generate();};
    form.addEventListener('input',()=>{if(document)$('.report-notice',root).textContent='Parameters changed. Generate Report to apply them.';});
    form.addEventListener('change',e=>{if(e.target.name==='period')updatePeriod(); if(selected.id==='supplier-purchases'&&['supplier','period','from','to','invoice'].includes(e.target.name))loadInvoices(); if(e.target.matches('[data-invoice-id]'))syncInvoices(); if(document)$('.report-notice',root).textContent='Parameters changed. Generate Report to apply them.';});
    if(focus)$('select,input',form)?.focus();
  }
  function supplierBrowser(preset) {
    root.innerHTML=`<div class="report-module"><header class="report-heading report-breadcrumb"><button type="button" class="btn" data-selector>← Reports</button><h1>Supplier-wise Purchase</h1></header>
      <form class="report-parameters" aria-label="Supplier invoices"><input type="hidden" name="supplier"><input type="hidden" name="invoice_ids"><input type="hidden" name="period" value="all">
      <section class="report-suppliers"><h2>Suppliers</h2><div class="report-cards">${options.suppliers.map(s=>`<button type="button" class="report-card" data-supplier="${esc(s.value)}"><strong>${esc(s.label)}</strong><span>View all invoices →</span></button>`).join('')||'<p>No suppliers available.</p>'}</div></section>
      <fieldset class="report-invoices" hidden><legend>Invoices</legend><p data-invoice-notice></p><div data-invoice-list></div></fieldset>
      <button type="submit" data-generate hidden>Generate Report</button></form><div class="report-notice" role="status">Click a supplier to view all their invoices.</div><div class="report-result"></div></div>`;
    $('form',root).onsubmit=e=>{e.preventDefault();generate();};
    if(preset.supplier){$('[name=supplier]',root).value=preset.supplier;loadInvoices();}
    else $('[data-supplier]',root)?.focus();
  }
  function syncInvoices() {
    const field=$('[name=invoice_ids]',root);
    if(field)field.value=$$('[data-invoice-id]:checked',root).map(c=>c.value).join(',');
  }
  async function loadInvoices() {
    const own=++invoiceVersion, panel=$('.report-invoices',root), form=$('form',root);
    if(!panel)return;
    const supplier=form.elements.supplier.value;
    panel.hidden=!supplier; form.elements.invoice_ids.value='';
    $('[data-invoice-list]',panel).innerHTML=''; invoicesLoading=!!supplier;
    if(!supplier)return;
    $('[data-invoice-notice]',panel).textContent='Loading invoices...';
    const query=new URLSearchParams({supplier});
    try {
      const data=await api('/reports/api/purchase-invoices?'+query);
      if(own!==invoiceVersion||!panel.isConnected)return;
      $('[data-invoice-notice]',panel).textContent=data.invoices.length?'Click an invoice to open its complete audit. Draft and cancelled invoices are labelled and have no received value.':'No invoices for this supplier.';
      $('[data-invoice-list]',panel).innerHTML=data.invoices.map(d=>`<button type="button" class="btn report-invoice" data-audit-invoice="${d.id}"><b>${esc(d.invoice||d.reference||'Draft #'+d.id)}</b><span>${esc(d.reference)} · ${esc(d.date)} · ${esc(d.status)} · ${d.lines} lines · ₹${money(d.total)}</span></button>`).join('');
    } catch(error){if(own===invoiceVersion){$('[data-invoice-notice]',panel).textContent='Could not load invoices. Change supplier or period to retry.';statusError(error);}}
    finally {if(own===invoiceVersion)invoicesLoading=false;}
  }
  // Customer / Item filters: live search. Picking a customer filters on exactly that
  // customer; picking a product fills its exact name. Typed text still works as a contains-match.
  function lookupField(input) {
    const kind=input.name, drop=input.nextElementSibling, form=input.form, idField=kind==='customer'?form.elements.namedItem('customer_id'):null;
    let results=[], at=-1, timer=null, token=0;
    const close=()=>{drop.hidden=true;input.setAttribute('aria-expanded','false');at=-1;};
    const paint=()=>{drop.innerHTML=results.length?results.map((r,i)=>`<div class="lookup-opt${i===at?' on':''}" role="option" data-i="${i}"><b>${esc(r.label)}</b><small>${esc(r.detail||'')}</small></div>`).join(''):'<div class="lookup-none">No match</div>';drop.hidden=false;input.setAttribute('aria-expanded','true');};
    const pick=(r)=>{input.value=r.label;if(idField)idField.value=r.id;close();input.dispatchEvent(new Event('change',{bubbles:true}));};
    input.addEventListener('input',()=>{
      if(idField)idField.value='';
      clearTimeout(timer);const q=input.value.trim();
      if(q.length<2){results=[];close();return;}
      timer=setTimeout(async()=>{const mine=++token;try{const d=await api(`/reports/api/lookup?kind=${kind}&q=${encodeURIComponent(q)}`);if(mine!==token)return;results=d.results;at=results.length?0:-1;paint();}catch(error){statusError(error);}},160);
    });
    input.addEventListener('keydown',e=>{
      if(drop.hidden)return;
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();e.stopPropagation();if(results.length){at=(at+(e.key==='ArrowDown'?1:-1)+results.length)%results.length;paint();}}
      else if(e.key==='Enter'&&results[at]){e.preventDefault();e.stopPropagation();pick(results[at]);}
      else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();}
    });
    drop.addEventListener('mousedown',e=>{const o=e.target.closest('[data-i]');if(o){e.preventDefault();pick(results[Number(o.dataset.i)]);}});
    input.addEventListener('blur',()=>setTimeout(close,120));
  }
  function updatePeriod() {
    const period=$('[name=period]',root);if(!period)return;
    const from=$('[name=from]',root),to=$('[name=to]',root),custom=period.value==='custom';
    from.disabled=to.disabled=!custom;
    if(!custom){const range=options.presets[period.value];from.value=range[0];to.value=range[1];}
  }
  function parameters() {
    const form=new FormData($('form',root)), values=Object.fromEntries(form.entries());
    $$('select[multiple]',root).forEach(s=>{const all=form.getAll(s.name);if(all.length)values[s.name]=all.join(',');else delete values[s.name];});
    if($('[name=period]',root)?.value!=='custom'){delete values.from;delete values.to;}
    return values;
  }
  function availableColumns() {
    if(selected.id==='supplier-purchases'&&$('[name=supplier]',root)?.value)return selected.audit_columns;
    if(document && document.parameters.supplier === ($('[name=supplier]',root)?.value||'') && document.parameters.view === ($('select[name=view]',root)?.value))return document.available_columns;
    if(selected.id==='sales-summary'){
      if($('[name=view]',root)?.value==='detail')return catalog.find(r=>r.id==='bill-register').columns;
      return [{key:'date',label:'Date',kind:'text',default:true},...options.categories.filter(Boolean).map(code=>({key:'cat:'+code,label:(options.category_names||{})[code]||code,kind:'money',default:true})),{key:'value',label:'Total',kind:'money',default:true},{key:'bills',label:'Bills',kind:'number',default:true}];
    }
    return selected.columns;
  }
  async function generate(columnSelection) {
    if(!selected)return;
    const form=$('form',root);if(!form.reportValidity())return;
    if(selected.id==='supplier-purchases'&&form.elements.supplier.value&&(invoicesLoading||!form.elements.invoice_ids.value)){ctx.status(invoicesLoading?'Wait for invoices to load':'Select at least one available invoice','warn');return;}
    cancel();const ownVersion=version;request=new AbortController();
    const button=$('[data-generate]',root);button.disabled=true;$('.report-notice',root).textContent='Generating report…';
    const cols=columnSelection ?? store.get(prefKey(),null);
    try {
      const generated=await api('/reports/api/generate',{method:'POST',signal:request.signal,body:{report:selected.id,parameters:parameters(),...(Array.isArray(cols)&&cols.length?{columns:cols}:{})}});
      if(ownVersion!==version)return;
      document=generated;
      if(selected.id==='supplier-purchases'&&form.elements.supplier.value)textView=false;
      rowAt=0;renderDocument();$('.report-notice',root).textContent=`Report generated · ${document.rows.length} row${document.rows.length===1?'':'s'}`;
      ctx.status(document.title+' generated','ok');if(selected.id!=='supplier-purchases')$('.report-table-scroll',root)?.focus();
    } catch(error){statusError(error);if(ownVersion===version)$('.report-notice',root).textContent='Report could not be generated. Check the parameters and try again.';}
    finally{if(ownVersion===version){button.disabled=false;request=null;}}
  }
  function documentHTML(doc,print=false) {
    const cols=doc.columns;
    const totals=Object.keys(doc.totals).length;
    const firstText=cols.findIndex(c=>c.kind==='text');
    return `<article class="report-document"><header class="report-document-head"><strong>${esc(doc.pharmacy)}</strong><h2>${esc(doc.title.toUpperCase())}</h2><div><span>${doc.id==='supplier-purchases'&&doc.parameters.period==='all'?'All invoice history':doc.parameters.as_of? 'As of: '+dateLabel(doc.to_date):'From: '+dateLabel(doc.from_date)+'　 To: '+dateLabel(doc.to_date)}</span><span>Generated: ${esc(doc.generated_at)}</span></div></header>
      <div class="report-table-scroll" tabindex="0" aria-label="Generated ${esc(doc.title)}"><table class="report-table"><thead><tr>${cols.map(c=>`<th class="${c.kind}" scope="col">${esc(c.label)}</th>`).join('')}</tr></thead><tbody>
      ${doc.rows.map((row,i)=>`<tr data-row="${i}" ${row._drill?'class="drillable" title="Double-click or select and press Enter for details"':''}>${cols.map(c=>`<td class="${c.kind}">${esc(fmt(c,row[c.key]))}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${cols.length}" class="report-empty">No records match the selected parameters.</td></tr>`}
      </tbody>${totals?`<tfoot><tr>${cols.map((c,i)=>`<td class="${c.kind}">${i===firstText?'TOTAL':c.key in doc.totals?esc(fmt(c,doc.totals[c.key])):''}</td>`).join('')}</tr></tfoot>`:''}</table></div>
      ${totalsBar(doc)}
      ${doc.note?`<p class="report-note">${esc(doc.note)}</p>`:''}</article>`;
  }
  // Document view: the ERP text document the server rendered (same as print / PDF / text export)
  // totals of the whole filtered dataset (e.g. stock: items, quantity, Rate value, MRP value), pinned under the report
  const totalsBar=(doc)=>doc.footer.length?`<div class="report-totals-bar">${doc.footer.map(f=>`<div class="report-document-footer"><b>${esc(f.label)}</b><strong>${f.kind==='number'?Number(f.value).toLocaleString('en-IN'):'₹'+money(f.value)}</strong></div>`).join('')}</div>`:'';
  let textView=store.get('report-view','document')==='document';
  function toggleView(){textView=!textView;store.set('report-view',textView?'document':'grid');if(document)renderDocument();ctx.status(textView?'Document view (as printed)':'Grid view','ok');}
  function renderDocument() {
    const body=textView&&document.text?`<pre class="report-text" tabindex="0" aria-label="${esc(document.title)} document">${esc(document.text)}</pre>${totalsBar(document)}`:documentHTML(document);
    $('.report-result',root).innerHTML=`<div class="report-toolbar"><strong>${esc(document.title)}</strong><span class="muted">${document.id==='supplier-purchases'&&document.parameters.period==='all'?'All invoice history':dateLabel(document.from_date)+' – '+dateLabel(document.to_date)}</span><span class="spacer"></span>
      <button type="button" class="btn" data-view title="Switch grid / document (${esc(keys.keyFor('reports.view'))})">${textView?'Grid view':'Document view'} <kbd>${esc(keys.keyFor('reports.view'))}</kbd></button>
      <button type="button" class="btn" data-columns>Columns ⚙</button><button type="button" class="btn" data-filter>${document.id==='supplier-purchases'?'Suppliers / invoices':'Filter'}</button><button type="button" class="btn" data-print>Print</button>
      ${canExport?['pdf','xlsx','csv','txt'].map(fmt=>`<button type="button" class="btn" data-export="${fmt}">${{pdf:'PDF',xlsx:'Excel',csv:'CSV',txt:'Text'}[fmt]}</button>`).join(''):''}<button type="button" class="btn" data-refresh>Refresh</button></div>${body}`;
  }
  function openColumns() {
    if(!selected)return;
    const cols=availableColumns();const chosen=store.get(prefKey(),null) || (document?document.columns.map(c=>c.key):cols.filter(c=>c.default).map(c=>c.key));
    const prev=window.document.activeElement;
    const el=h(`<div class="modal-backdrop"><div class="modal small report-columns" role="dialog" aria-modal="true" aria-label="Choose report columns"><header><h2>Columns · ${esc(selected.title)}</h2></header><form><div class="modal-body">${cols.map(c=>`<label><input type="checkbox" name="column" value="${esc(c.key)}" ${chosen.includes(c.key)?'checked':''}> ${esc(c.label)}</label>`).join('')}<p class="modal-error" role="alert"></p></div><footer><button type="button" class="btn" data-defaults>Defaults</button><button type="button" class="btn" data-cancel>Cancel</button><button class="btn primary" type="submit">${document?'Apply & Generate':'Apply'}</button></footer></form></div></div>`);
    function close(){el.remove();prev?.focus();}
    $('form',el).onsubmit=e=>{e.preventDefault();const values=new FormData(e.target).getAll('column');if(!values.length){$('.modal-error',el).textContent='Select at least one column';return;}store.set(prefKey(),values);close();if(document)generate(values);};
    $('[data-cancel]',el).onclick=close;
    $('[data-defaults]',el).onclick=()=>$$('[name=column]',el).forEach(box=>box.checked=cols.find(c=>c.key===box.value).default);
    el.onkeydown=e=>{e.stopPropagation();if(e.key==='Escape'){e.preventDefault();close();}else if(e.key==='Tab'){const all=$$('input,button',el),i=all.indexOf(window.document.activeElement);e.preventDefault();all[(i+(e.shiftKey?-1:1)+all.length)%all.length].focus();}};
    window.document.body.append(el);$('input',el)?.focus();
  }
  function printReport() {
    if(!document){ctx.status('Generate a report first','warn');return;}
    const frame=window.document.createElement('iframe');frame.className='report-print-frame';window.document.body.append(frame);
    frame.onload=()=>{frame.contentWindow.focus();frame.contentWindow.print();setTimeout(()=>frame.remove(),1000);};
    if(textView&&document.text){frame.srcdoc=`<!doctype html><html><head><title>${esc(document.title)}</title><style>body{margin:0}pre{font:10px/1.35 "Courier New",monospace;white-space:pre}@page{size:landscape;margin:10mm}</style></head><body><pre>${esc(document.text)}</pre></body></html>`;return;}
    frame.srcdoc=`<!doctype html><html><head><title>${esc(document.title)}</title><style>body{font:11px Arial;color:#1b2327}table{width:100%;border-collapse:collapse}th,td{border:1px solid #d5dcdf;padding:5px}th{text-align:left}.money,.number{text-align:right}thead{display:table-header-group}tfoot{font-weight:bold}h2{font-size:13px}.report-document-head{text-align:center}.report-document-head>div{display:flex;justify-content:space-between}.report-document-footer{display:flex;justify-content:space-between;padding:7px}tr{break-inside:avoid}@page{size:landscape;margin:12mm}</style></head><body>${documentHTML(document,true)}</body></html>`;
  }
  async function exportReport(fmt='xlsx') {
    if(!document){ctx.status('Generate a report first','warn');return;}
    if(!canExport){ctx.status('You do not have export permission','warn');return;}
    try {
      const response=await fetch(`/reports/api/document/${document.token}.${fmt}`,{credentials:'same-origin'});
      if(!response.ok){const data=await response.json();throw new Error(data.detail || 'Could not export report');}
      const url=URL.createObjectURL(await response.blob()),anchor=window.document.createElement('a');anchor.href=url;anchor.download=`${document.id}_${document.from_date}_${document.to_date}.${fmt}`;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch(error){statusError(error);}
  }
  function selectRow(at) {
    if(!document?.rows.length)return;rowAt=Math.max(0,Math.min(at,document.rows.length-1));
    $$('[data-row]',root).forEach(row=>row.classList.toggle('selected',Number(row.dataset.row)===rowAt));$(`[data-row="${rowAt}"]`,root)?.scrollIntoView({block:'nearest'});
  }
  async function drill(at) {
    const target=document?.rows[at]?._drill;if(!target)return;
    if(target.url){
      // documents open in their own workspace tab — never a browser pop-up
      const bill=/^\/sales\/(\d+)/.exec(target.url),purchase=/^\/purchases\/(\d+)/.exec(target.url),cust=/^\/customers\/(\d+)/.exec(target.url);
      if(cust){ctx.open('customers',{customer:Number(cust[1])});return;}
      if(bill){ctx.open('sales',{bill:Number(bill[1])});return;}
      if(purchase){ctx.open('purchase',{id:Number(purchase[1])});return;}
      ctx.status('That document has no screen of its own','warn');return;
    }
    await choose(target.report,target.params);generate();
  }
  root.addEventListener('click',e=>{
    const button=e.target.closest('button');if(button?.dataset.report){choose(button.dataset.report);return;}
    if(button?.dataset.supplier){cancel();document=null;$('.report-result',root).innerHTML='';$('[name=supplier]',root).value=button.dataset.supplier;$$('[data-supplier]',root).forEach(b=>b.setAttribute('aria-pressed',String(b===button)));$('.report-notice',root).textContent='Choose an invoice to view its audit.';loadInvoices();return;}
    if(button?.dataset.auditInvoice){$('[name=invoice_ids]',root).value=button.dataset.auditInvoice;$$('[data-audit-invoice]',root).forEach(b=>b.setAttribute('aria-pressed',String(b===button)));generate();return;}
    if(button?.hasAttribute('data-selector'))showSelector();
    else if(button?.hasAttribute('data-reset'))choose(selected.id);
    else if(button?.hasAttribute('data-columns'))openColumns();
    else if(button?.hasAttribute('data-filter')){const form=$('form',root);form.hidden=!form.hidden;if(!form.hidden)$('input,select',form)?.focus();}
    else if(button?.hasAttribute('data-print'))printReport();
    else if(button?.hasAttribute('data-view'))toggleView();
    else if(button?.dataset.export)exportReport(button.dataset.export);
    else if(button?.hasAttribute('data-refresh'))generate();
    const row=e.target.closest('[data-row]');if(row)selectRow(Number(row.dataset.row));
  });
  root.addEventListener('dblclick',e=>{const row=e.target.closest('[data-row]');if(row)drill(Number(row.dataset.row));});
  root.addEventListener('keydown',e=>{
    if(e.target.matches('[data-report]')&&['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){
      e.preventDefault();const cards=$$('[data-report]',root),box=e.target.getBoundingClientRect(),dx=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0,dy=e.key==='ArrowUp'?-1:e.key==='ArrowDown'?1:0;
      const candidates=cards.filter(c=>c!==e.target).map(c=>({c,r:c.getBoundingClientRect()})).filter(({r})=>dx?(r.x-box.x)*dx>5:(r.y-box.y)*dy>5).sort((a,b)=>dx?Math.abs(a.r.y-box.y)*10+Math.abs(a.r.x-box.x)-Math.abs(b.r.y-box.y)*10-Math.abs(b.r.x-box.x):Math.abs(a.r.x-box.x)*2+Math.abs(a.r.y-box.y)-Math.abs(b.r.x-box.x)*2-Math.abs(b.r.y-box.y));candidates[0]?.c.focus();
    } else if(e.target.matches('.report-table-scroll')&&document){
      if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();selectRow(e.key==='Home'?0:e.key==='End'?document.rows.length-1:rowAt+(e.key==='ArrowDown'?1:-1));}
      else if(e.key==='Enter'){e.preventDefault();drill(rowAt);}
    }
  });
  showSelector(false);
  if(!catalog.length)api('/reports/api/catalog').then(data=>{catalog=data.reports;canExport=data.can_export;showSelector(ctx.isActive());}).catch(statusError);
  return {
    get keys(){return keys.bar('reports');},
    onKey(e,name){
      if(name==='Escape'&&selected){showSelector();return true;}
      const action=keys.lookup('reports',name);
      const handlers={'reports.view':toggleView,'reports.period':()=>{$('form',root)?.removeAttribute('hidden');$('[name=period],[name=as_of]',root)?.focus();},'reports.generate':()=>generate(),'reports.columns':openColumns,'reports.print':printReport,'reports.export':()=>exportReport(),'reports.refresh':()=>generate()};
      if(!handlers[action])return false;handlers[action]();return true;
    },
    onShow({focus}){if(focus)$('select,input,[data-report]',root)?.focus();},
    destroy(){cancel();},
  };
}
