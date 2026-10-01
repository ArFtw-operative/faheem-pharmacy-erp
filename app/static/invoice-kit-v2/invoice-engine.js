/* Same renderer powers preview and browser print. No scale-to-fit. */
(function(root){
  const sizes={A4:[210,297],A2:[420,594],A5:[148,210],LETTER:[215.9,279.4],THERMAL80:[80,297],THERMAL58:[58,297]};
  const columns=[
    {key:'sr',label:'Sr.',weight:3,align:'center'},
    {key:'hsn',label:'HSN',weight:7},
    {key:'name',label:'Description',weight:24},
    {key:'pack',label:'Pack',weight:7},
    {key:'manufacturer',label:'Mfr.',weight:5},
    {key:'batch',label:'Batch no.',weight:8},
    {key:'expiry',label:'Expiry',weight:7},
    {key:'quantity',label:'Qty',weight:4,align:'right'},
    {key:'free',label:'Free',weight:4,align:'right'},
    {key:'mrp',label:'MRP',weight:7,align:'right'},
    {key:'rate',label:'Rate',weight:7,align:'right'},
    {key:'discountPercent',label:'Dis %',weight:5,align:'right'},
    {key:'gstPercent',label:'GST %',weight:5,align:'right'},
    {key:'amount',label:'Amount',weight:9,align:'right'}
  ];
  const defaults={version:2,paper:'A4',marginMm:7,fontPt:8.5,rowPadding:3,monochrome:false,logo:'horizontal-color.svg',showLogo:true,showName:true,name:'Faheem Pharmacy',showTitle:true,showCopyLabel:true,title:'SALES INVOICE',showBuyer:true,showInvoiceDetails:true,showTaxSummary:true,showBank:true,showTerms:true,showPayments:true,showSignature:true,signature:'For Faheem Pharmacy',showItemCount:true,showAmountWords:true,showSample:true,
    pharmacyFields:[{id:'address',label:'Address',value:'Sample pharmacy address, city, postcode',visible:true},{id:'phone',label:'Phone',value:'Add your business phone',visible:true},{id:'email',label:'Email',value:'Add your business email',visible:true},{id:'gstin',label:'GSTIN',value:'Add your GSTIN if applicable',visible:true},{id:'dl',label:'Drug licence',value:'Add your licence details',visible:true}],
    buyerFields:[{id:'name',label:'Name',visible:true},{id:'address',label:'Address',visible:true},{id:'phone',label:'Phone',visible:true},{id:'gstin',label:'GSTIN',visible:true},{id:'dl',label:'Drug licence',visible:true}],
    invoiceFields:[{id:'number',label:'Invoice no.',visible:true},{id:'date',label:'Date',visible:true},{id:'dueDate',label:'Due date',visible:true},{id:'reference',label:'Reference',visible:true},{id:'cashier',label:'Cashier',visible:true}],
    bankFields:[{id:'bank',label:'Bank',value:'Add your bank name',visible:true},{id:'account',label:'Account',value:'Add account details',visible:true},{id:'ifsc',label:'IFSC',value:'Add IFSC',visible:true}],
    terms:'Keep this invoice for reference. Contact the pharmacy for billing queries.',
    columns:columns.map(c=>({...c,visible:true})),
    totalFields:[{id:'subtotalPaise',label:'Subtotal',visible:true},{id:'discountPaise',label:'Discount',visible:true},{id:'taxPaise',label:'Total tax',visible:true},{id:'roundingPaise',label:'Round off',visible:true},{id:'totalPaise',label:'Net amount',visible:true},{id:'paidPaise',label:'Received',visible:true},{id:'duePaise',label:'Balance due',visible:true}]
  };
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=v=>new Intl.NumberFormat('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(v/100);
  function sample(count=5){
    const base=[
      {name:'Paracetamol 500 mg tablets',hsn:'DEMO',pack:'10 tabs',manufacturer:'DEMO',batch:'P0126',expiry:'08/27',quantity:2,free:0,mrp:4000,rate:3500,discountPercent:10,gstPercent:5},
      {name:'Cetirizine 10 mg tablets',hsn:'DEMO',pack:'10 tabs',manufacturer:'DEMO',batch:'C0426',expiry:'11/27',quantity:1,free:0,mrp:5000,rate:4200,discountPercent:10,gstPercent:5},
      {name:'Vitamin C 500 mg tablets',hsn:'DEMO',pack:'10 tabs',manufacturer:'DEMO',batch:'V0326',expiry:'02/28',quantity:2,free:1,mrp:14000,rate:12500,discountPercent:10,gstPercent:12},
      {name:'Digital thermometer',hsn:'DEMO',pack:'1 pc',manufacturer:'DEMO',batch:'T0226',expiry:'—',quantity:1,free:0,mrp:22500,rate:19900,discountPercent:0,gstPercent:12},
      {name:'Oral rehydration salts',hsn:'DEMO',pack:'1 sachet',manufacturer:'DEMO',batch:'O0726',expiry:'03/28',quantity:4,free:0,mrp:2500,rate:2250,discountPercent:0,gstPercent:5}
    ];
    const items=Array.from({length:count},(_,i)=>({...base[i%5],id:String(i+1),sr:i+1}));
    for(const i of items){i.discountPaise=Math.round(i.quantity*i.rate*i.discountPercent/100);i.taxablePaise=i.quantity*i.rate-i.discountPaise;i.taxPaise=Math.round(i.taxablePaise*i.gstPercent/100);i.amount=i.taxablePaise+i.taxPaise;}
    const subtotalPaise=items.reduce((a,i)=>a+i.quantity*i.rate,0),discountPaise=items.reduce((a,i)=>a+i.discountPaise,0),taxPaise=items.reduce((a,i)=>a+i.taxPaise,0),unrounded=subtotalPaise-discountPaise+taxPaise,totalPaise=Math.round(unrounded/100)*100;
    const taxSummary=[5,12].map(rate=>{const relevant=items.filter(i=>i.gstPercent===rate),tax=relevant.reduce((a,i)=>a+i.taxPaise,0);return {rate,taxablePaise:relevant.reduce((a,i)=>a+i.taxablePaise,0),cgstPaise:Math.floor(tax/2),sgstPaise:tax-Math.floor(tax/2),taxPaise:tax};});
    return {sample:true,number:'FPH-260919-0142',date:'19/09/2026',dueDate:'19/09/2026',reference:'SAMPLE-0142',cashier:'EMP0001',buyer:{name:'Sample customer',address:'Sample customer address',phone:'—',gstin:'—',dl:'—'},items,taxSummary,totals:{subtotalPaise,discountPaise,taxPaise,roundingPaise:totalPaise-unrounded,totalPaise,paidPaise:totalPaise,duePaise:0},paymentText:'UPI · Paid in full',amountWords:amountWords(totalPaise)};
  }
  function amountWords(paise){
    const ones=['','One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten','Eleven','Twelve','Thirteen','Fourteen','Fifteen','Sixteen','Seventeen','Eighteen','Nineteen'];const tens=['','','Twenty','Thirty','Forty','Fifty','Sixty','Seventy','Eighty','Ninety'];
    const words=n=>n<20?ones[n]:n<100?tens[Math.floor(n/10)]+(n%10?' '+ones[n%10]:''):n<1000?ones[Math.floor(n/100)]+' Hundred'+(n%100?' '+words(n%100):''):n<100000?words(Math.floor(n/1000))+' Thousand'+(n%1000?' '+words(n%1000):''):n<10000000?words(Math.floor(n/100000))+' Lakh'+(n%100000?' '+words(n%100000):''):words(Math.floor(n/10000000))+' Crore'+(n%10000000?' '+words(n%10000000):'');
    return 'Rupees '+(words(Math.floor(paise/100))||'Zero')+(paise%100?' and '+words(paise%100)+' Paise':'')+' Only';
  }
  function normalize(input){
    const s={...structuredClone(defaults),...input};
    if(!sizes[s.paper])throw new Error('Choose a supported paper size.');
    for(const key of ['pharmacyFields','bankFields','buyerFields','invoiceFields','columns','totalFields'])if(!Array.isArray(s[key]))throw new Error('Invalid settings: '+key);
    if(!s.columns.some(c=>c.visible))throw new Error('Show at least one item column.');
    if(s.columns.some(c=>!columns.some(base=>base.key===c.key))||new Set(s.columns.map(c=>c.key)).size!==s.columns.length)throw new Error('Invalid or duplicate column keys.');
    s.marginMm=Math.max(2,Math.min(15,Number(s.marginMm)||7));s.fontPt=Math.max(8,Math.min(11,Number(s.fontPt)||8.5));s.rowPadding=Math.max(2,Math.min(8,Number(s.rowPadding)||3));
    if(!['horizontal-color.svg','horizontal-mono.svg','symbol-color.svg','symbol-mono.svg'].includes(s.logo))s.logo='horizontal-color.svg';
    return s;
  }
  function validate(data){
    const errors=[];
    if(!data?.number||!Array.isArray(data.items)||!data.items.length)errors.push('The invoice needs a number and at least one item.');
    for(const item of data?.items||[])if(!item.name||!Number.isSafeInteger(item.amount)||item.amount<0||!Number.isFinite(item.quantity)||item.quantity<=0)errors.push('Each item needs a name, quantity and valid amount.');
    const t=data?.totals||{};
    if(!['subtotalPaise','discountPaise','taxPaise','roundingPaise','totalPaise','paidPaise','duePaise'].every(k=>Number.isSafeInteger(t[k])))errors.push('Use integer paise for invoice totals.');
    if(t.subtotalPaise-t.discountPaise+t.taxPaise+t.roundingPaise!==t.totalPaise)errors.push('Invoice totals do not reconcile.');
    if((data?.items||[]).reduce((a,i)=>a+i.amount,0)+t.roundingPaise!==t.totalPaise)errors.push('Line totals do not match net amount.');
    if(t.totalPaise-t.paidPaise!==t.duePaise)errors.push('Received and balance due do not reconcile.');
    return [...new Set(errors)];
  }
  function splitColumns(settings){
    const enabled=settings.columns.filter(c=>c.visible),width=sizes[settings.paper][0];
    const primary=width<100?['name','quantity','amount']:width<160?['sr','name','batch','expiry','quantity','rate','gstPercent','amount']:enabled.map(c=>c.key);
    let main=enabled.filter(c=>primary.includes(c.key));
    if(!main.length)main.push(enabled[0]);
    // a receipt roll: the name takes the room, Qty and Amount stay on one line
    if(width<100){const w={name:58,quantity:13,amount:29},short={quantity:'Qty',amount:'Amount'};main=main.map(c=>({...c,weight:w[c.key]??c.weight,label:short[c.key]??c.label}));}
    const shown=new Set(main.map(c=>c.key));
    return {main,extra:enabled.filter(c=>!shown.has(c.key))};
  }
  function cell(item,key){if(key==='amount'&&Number.isFinite(item.displayAmount))return money(item.displayAmount);if(['mrp','rate','amount'].includes(key))return money(item[key]);return item[key]??'—';}
  function row(item,index,s,parts){
    const main=parts.main.map(c=>`<td class="${c.align||''}" data-col="${c.key}">${esc(cell({...item,sr:index+1},c.key))}</td>`).join('');
    const extra=parts.extra.length?`<tr class="item-extra"><td colspan="${parts.main.length}">${parts.extra.map(c=>`<span><b>${esc(c.label)}:</b> ${esc(cell({...item,sr:index+1},c.key))}</span>`).join('')}</td></tr>`:'';
    return `<tbody class="item-group" data-index="${index}"><tr>${main}</tr>${extra}</tbody>`;
  }
  function header(data,s,page){
    const fields=(list,obj)=>list.filter(f=>f.visible).map(f=>`<div class="field-line">${f.label?`<span>${esc(f.label)}:</span>`:''}<strong>${esc(obj?obj[f.id]:f.value)}</strong></div>`).join('');
    const logo=s.showLogo?`<img class="invoice-logo" src="${root.INVOICE_KIT_ASSETS||(root.INVOICE_KIT&&root.INVOICE_KIT.assets)||'assets/'}${s.monochrome?s.logo.replace('-color','-mono'):s.logo}" alt="${esc(s.name)} logo">`:'';
    return `<div class="invoice-top"><div class="brand-block">${logo}${s.showName?`<strong class="business-name">${esc(s.name)}</strong>`:''}</div>${(s.showTitle||data.sample||s.showCopyLabel!==false)?`<div class="document-title">${s.showTitle?`<strong>${esc(s.title)}</strong>`:''}${data.sample?'<span>SAMPLE · NOT VALID FOR SALE</span>':''}${s.showCopyLabel!==false?`<span class="copy-label">Customer copy${page>1?' · Continued':''}</span>`:''}</div>`:''}</div><div class="parties"><section class="pharmacy-block">${fields(s.pharmacyFields)}</section>${s.showBuyer?`<section><h2>Buyer’s details</h2>${fields(s.buyerFields,data.buyer)}</section>`:''}${s.showInvoiceDetails?`<section><h2>Invoice details</h2>${fields(s.invoiceFields,data)}</section>`:''}</div>`;
  }
  function summary(data,s){
    const t=data.totals;
    const info=`${s.showBank?`<section class="bank"><h2>Bank details</h2>${s.bankFields.filter(f=>f.visible).map(f=>`<div class="field-line"><span>${esc(f.label)}:</span><strong>${esc(f.value)}</strong></div>`).join('')}</section>`:''}${s.showTerms?`<section class="terms"><h2>Terms & notes</h2><p>${esc(s.terms)}</p></section>`:''}${s.showPayments?`<p class="payment-text"><b>Payment:</b> ${esc(data.paymentText||'—')}</p>`:''}`;
    // Single full-width box: the totals as an aligned horizontal table.
    const visible=s.totalFields.filter(f=>f.visible);
    const receipt=sizes[s.paper][0]<100;
    const totals=receipt?`<table class="totals-vertical"><tbody>${visible.map(f=>`<tr class="${f.id==='totalPaise'?'net-total':''}"><th>${esc(f.label)}</th><td>${f.id==='discountPaise'&&t[f.id]?'-':''}${money(t[f.id])}</td></tr>`).join('')}</tbody></table>`:`<table class="totals-horizontal"><thead><tr>${visible.map(f=>`<th>${esc(f.label)}</th>`).join('')}</tr></thead><tbody><tr>${visible.map(f=>`<td class="${f.id==='totalPaise'?'net-total':''}">${f.id==='discountPaise'&&t[f.id]?'-':''}${money(t[f.id])}</td>`).join('')}</tr></tbody></table>`;
    return `<div class="final-summary"><div class="summary-grid">${info?`<div class="bank-terms">${info}</div>`:''}<div class="amount-summary">${totals}${s.showItemCount?`<p class="quantity-total">Line items: ${data.items.length} · Paid quantity: ${data.items.reduce((n,i)=>n+i.quantity,0)}${(data.payments||[]).length?' · Paid by: '+data.payments.map(p=>esc(p.method)+' '+(p.amountPaise/100).toFixed(2)).join(' + '):''}${data.tenderedPaise!=null&&(data.totals||{}).changePaise?' · Cash tendered '+(data.tenderedPaise/100).toFixed(2)+', change '+(data.totals.changePaise/100).toFixed(2):''}</p>`:''}</div></div>${s.showAmountWords?`<p class="amount-words"><b>Amount in words:</b> ${esc(data.amountWords||'')}</p>`:''}${s.showSignature?`<div class="signature"><span>${esc(s.signature)}</span><span>Authorised signatory</span></div>`:''}</div>`;
  }
  async function paginate(container,data,settings,printStyle){
    const errors=validate(data);if(errors.length)throw new Error(errors.join(' '));
    const s=normalize(settings),parts=splitColumns(s),[width,height]=sizes[s.paper],thermal=width<100,margin=thermal?Math.min(s.marginMm,width===58?3:4):s.marginMm;
    container.replaceChildren();
    const pages=[];
    function makePage(){
      const el=document.createElement('article');el.className='invoice-sheet'+(thermal?' thermal':'')+(s.monochrome?' monochrome':'');el.setAttribute('aria-label','Invoice page '+(pages.length+1));el.style.cssText=`--sheet-width:${width}mm;--sheet-height:${height}mm;--sheet-margin:${margin}mm;--body-font:${s.fontPt}pt;--cell-pad:${s.rowPadding}px;`;
      el.innerHTML=`<div class="sheet-inner"><div class="page-content">${header(data,s,pages.length+1)}<table class="invoice-table"><colgroup>${parts.main.map(c=>`<col style="width:${c.weight/parts.main.reduce((n,x)=>n+x.weight,0)*100}%">`).join('')}</colgroup><thead><tr>${parts.main.map(c=>`<th class="${c.align||''}" data-col="${c.key}">${esc(c.label)}</th>`).join('')}</tr></thead></table><div class="page-summary"><p class="continuation">Continued on next page</p></div></div><footer class="page-footer"><span>${esc(data.number)} · Amounts in INR</span><strong class="page-label"></strong></footer></div>`;
      // VOID / RETURNED stamp: absolutely positioned, so pagination is unaffected; prints too.
      if(data.stamp){const st=document.createElement('div');st.className='invoice-stamp stamp-'+String(data.stamp).toLowerCase().replace(/[^a-z]+/g,'-');st.textContent=data.stamp;if(data.stampNote){const n=document.createElement('small');n.textContent=data.stampNote;st.appendChild(n);}el.appendChild(st);}
      container.append(el);const page={el,table:el.querySelector('.invoice-table'),content:el.querySelector('.page-content'),inner:el.querySelector('.sheet-inner'),footer:el.querySelector('.page-footer'),summary:el.querySelector('.page-summary'),rows:[]};pages.push(page);return page;
    }
    function fits(page){return page.content.getBoundingClientRect().height+page.footer.getBoundingClientRect().height<=page.inner.getBoundingClientRect().height+.25;}
    let page=makePage();
    // Images get explicit boxes, and fonts are local system fonts. Wait before measuring.
    await document.fonts.ready;
    for(let i=0;i<data.items.length;i++){
      const html=row(data.items[i],i,s,parts);page.table.insertAdjacentHTML('beforeend',html);let group=page.table.lastElementChild;
      if(!fits(page)){
        group.remove();if(!page.rows.length)throw new Error('One item is taller than a printable page. Shorten its display text or increase the paper size.');
        page=makePage();page.table.insertAdjacentHTML('beforeend',html);group=page.table.lastElementChild;
        if(!fits(page))throw new Error('One item is taller than a printable page. Its content was not clipped.');
      }
      page.rows.push(i);
    }
    page.summary.innerHTML=summary(data,s);
    if(!fits(page)){
      const previous=page,last=previous.rows.pop();previous.table.lastElementChild.remove();previous.summary.innerHTML='<p class="continuation">Continued on next page</p>';
      page=makePage();page.table.insertAdjacentHTML('beforeend',row(data.items[last],last,s,parts));page.rows.push(last);page.summary.innerHTML=summary(data,s);
      if(!previous.rows.length){previous.el.remove();pages.splice(pages.indexOf(previous),1);}
      if(!fits(page))throw new Error('The final summary is too tall for the selected paper. Reduce optional details or use a larger paper size.');
    }
    const rules=[];
    pages.forEach((p,index)=>{
      p.el.querySelector('.page-label').textContent=`Page ${index+1} of ${pages.length}`;
      p.el.querySelector('.continuation')?.replaceChildren(document.createTextNode(`Continued on page ${index+2} · Subtotal on this page: ${money(p.rows.reduce((n,i)=>n+data.items[i].amount,0))}`));
      let pageHeight=height;
      if(thermal){pageHeight=Math.ceil((p.content.getBoundingClientRect().height+p.footer.getBoundingClientRect().height)*25.4/96+2*margin+2);p.el.style.setProperty('--sheet-height',pageHeight+'mm');}
      p.el.style.page='invoice-'+(index+1);
      rules.push(`@page invoice-${index+1}{size:${width}mm ${pageHeight}mm;margin:0}`);
    });
    if(printStyle)printStyle.textContent=rules.join('\n');
    return {pages:pages.length,settings:s,rowsPerPage:pages.map(p=>p.rows.length),widthMm:width};
  }
  const api={sizes,columns,defaults,sample,normalize,validate,splitColumns,paginate,esc,money,amountWords};root.InvoiceKit=api;if(typeof module!=='undefined')module.exports=api;
})(globalThis);
