import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const base=process.env.SHORTCUT_TEST_URL || 'http://127.0.0.1:8025';
const browser=await chromium.launch({headless:true});
try {
const page=await browser.newPage({viewport:{width:1366,height:768}});
const errors=[];let generated=0;
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(r.url().endsWith('/reports/api/generate'))generated++;});
await page.goto(base+'/login');await page.fill('[name=username]','shortcut-test');await page.fill('[name=password]','isolated-browser-test');await page.click('button[type=submit]');await page.waitForURL(u=>!u.pathname.startsWith('/login'));
const items=[];
for(const [name,category] of [['Report Medicine','Medicine'],['Report Baby','Baby Care']]){
 const response=await page.request.post(base+'/api/erp/inventory',{data:{name,category,manufacturer:'Report Brand',base_unit:'TABLET',pack_unit:'TABLET',units_per_pack:1}});
 assert.equal(response.ok(),true);const item=await response.json();items.push(item);
 const stock=await page.request.post(base+`/api/erp/inventory/${item.id}/adjust`,{data:{direction:'IN',quantity:25,batch_no:'REPORT',expiry:'12/2099',mrp:'10',reason:'Report test'}});assert.equal(stock.ok(),true);
}
const sale=await page.request.post(base+'/api/sales',{data:{payment_mode:'UPI',discount_pct:10,lines:items.map(i=>({item_id:i.id,quantity:2}))}});assert.equal(sale.ok(),true,await sale.text());
const bill=await sale.json();
await page.goto(base+'/reports');await page.locator('[data-report="sales-summary"]').waitFor();
assert.equal(await page.locator('.report-card').count(),20);
assert.equal(generated,0);assert.equal(await page.locator('.report-document').count(),0);
assert.equal(await page.locator('.report-module').innerText().then(t=>t.includes('₹')),false);
await page.screenshot({path:'/tmp/pharmacy-reports-selector.png'});
await page.locator('[data-report="sales-summary"]').focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('Enter');
await page.locator('.report-parameters').waitFor();assert.equal(await page.locator('h1').innerText(),'Bill Register');
assert.equal(await page.evaluate(()=>document.activeElement.name),'period');assert.equal(generated,0);
await page.keyboard.press('Escape');await page.locator('[data-report="sales-summary"]').click();await page.locator('.report-parameters').waitFor();
assert.equal(await page.locator('[name=from]').isDisabled(),true);
await page.selectOption('[name=period]','custom');assert.equal(await page.locator('[name=from]').isEnabled(),true);
const today=await page.locator('[name=from]').inputValue();await page.locator('[name=to]').fill(today);
assert.equal(generated,0);await page.keyboard.press('Alt+g');await page.locator('.report-document').waitFor();
assert.equal(generated,1);
assert.match(await page.locator('.report-table thead').innerText(),/Medicine/i);assert.match(await page.locator('.report-table thead').innerText(),/Baby Care/i);
assert.match(await page.locator('.report-table tfoot').innerText(),new RegExp(Number(bill.total).toFixed(2)));
assert.equal(await page.locator('.report-document').innerText().then(t=>t.includes('Cost of Goods Sold')),false);
await page.screenshot({path:'/tmp/pharmacy-sales-summary.png'});
await page.locator('.report-table tbody tr').dblclick();await page.waitForFunction(()=>document.querySelector('h1')?.textContent==='Bill Register');await page.locator('.report-document').waitFor();
assert.match(await page.locator('.report-table').innerText(),new RegExp(bill.invoice_no));
await page.keyboard.press('Escape');await page.locator('[data-report="item-sales"]').click();await page.locator('.report-parameters').waitFor();
await page.selectOption('[name=category]','MEDICINE');await page.keyboard.press('Alt+g');await page.locator('.report-document').waitFor();
assert.equal(await page.locator('.report-table tbody tr').count(),1);assert.match(await page.locator('.report-table tbody').innerText(),/Report Medicine/);
await page.keyboard.press('Alt+c');await page.getByRole('dialog',{name:'Choose report columns'}).waitFor();
await page.locator('[name=column][value=mrp]').uncheck();await page.locator('[name=column][value=category]').check();
await page.getByRole('button',{name:'Apply & Generate'}).click();await page.waitForFunction(()=>!document.querySelector('[data-generate]').disabled);
assert.equal(await page.locator('.report-table thead').innerText().then(t=>t.includes('MRP')),false);assert.match(await page.locator('.report-table thead').innerText(),/Category/);
for(const [label,fmt] of [['CSV','csv'],['Excel','xlsx'],['PDF','pdf']]){
 const downloading=page.waitForEvent('download');await page.getByRole('button',{name:label,exact:true}).click();const download=await downloading;await download.saveAs('/tmp/pharmacy-report.'+fmt);
 const bytes=await readFile('/tmp/pharmacy-report.'+fmt);assert.ok(bytes.length>30);
 if(fmt==='pdf')assert.equal(bytes.subarray(0,4).toString(),'%PDF');
 if(fmt==='csv'){assert.match(bytes.toString(),/Report Medicine/);assert.equal(bytes.toString().includes('Report Baby'),false);assert.equal(bytes.toString().includes('MRP'),false);}
}
await page.keyboard.press('Escape');await page.locator('[data-report="item-sales"]').click();await page.locator('.report-parameters').waitFor();await page.keyboard.press('Alt+g');await page.locator('.report-document').waitFor();
assert.equal(await page.locator('.report-table thead').innerText().then(t=>t.includes('MRP')),false);
await page.locator('[data-reset]').click();await page.locator('.report-parameters').waitFor();assert.equal(await page.locator('.report-document').count(),0);
await page.keyboard.press('Escape');await page.locator('[data-report="category-sales"]').click();await page.locator('.report-parameters').waitFor();await page.keyboard.press('Alt+g');await page.locator('.report-document').waitFor();
await page.locator('.report-table tbody tr').filter({hasText:/Medicine/i}).dblclick();await page.waitForFunction(()=>document.querySelector('h1')?.textContent==='Item-wise Sales');await page.locator('.report-document').waitFor();assert.equal(await page.locator('[name=category]').inputValue(),'MEDICINE');
// Every card produces a document through its own relevant parameter set.
for(const id of ['purchase-summary','purchase-register','supplier-purchases','purchase-returns','current-stock','batch-stock','expiry','low-stock','stock-movement','stock-loss','profit','discounts','payments','void-bills','brand-sales','sales-returns']){
 await page.keyboard.press('Escape');await page.locator(`[data-report="${id}"]`).click();await page.locator('.report-parameters').waitFor();
 const response=page.waitForResponse(r=>r.url().endsWith('/reports/api/generate'));
 await page.keyboard.press('Alt+g');const result=await response;assert.equal(result.ok(),true,await result.text());await page.locator('.report-document').waitFor();
}
await page.keyboard.press('Escape');await page.keyboard.press('Alt+p');await page.locator('.pos').waitFor();await page.keyboard.press('Alt+r');await page.locator('[data-report="sales-summary"]').waitFor();
assert.deepEqual(errors,[]);
console.log('PASS: no figures or generation at landing; keyboard cards/parameters; SQL sales/category filters; drill-down; remembered columns; selected-column CSV/Excel/PDF; all 20 reports; Alt+R Reports');
} finally {await browser.close();}
