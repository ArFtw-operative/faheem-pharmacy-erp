import {chromium} from 'playwright';
import assert from 'node:assert/strict';
const base='http://127.0.0.1:8025';
const browser=await chromium.launch({headless:true});
try {
 const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/login');await page.fill('[name=username]','shortcut-test');await page.fill('[name=password]','isolated-browser-test');await page.click('button[type=submit]');await page.waitForURL(u=>!u.pathname.startsWith('/login'));
 const items=[];
 for(const name of ['Verified-cost bottle','Missing-cost bottle']){
  let response=await page.request.post(base+'/api/erp/inventory',{data:{name:name+' '+Date.now(),base_unit:'BOTTLE',pack_unit:'BOTTLE',units_per_pack:1,mrp:150}});assert.equal(response.ok(),true);const item=await response.json();items.push(item);
  response=await page.request.post(base+`/api/erp/inventory/${item.id}/adjust`,{data:{direction:'IN',quantity:10,batch_no:'FINBROWSER',expiry:'12/2099',mrp:150,reason:'Isolated financial test'}});assert.equal(response.ok(),true);
 }
 await page.goto(base+'/app/inventory');await page.locator('.f-q').fill(items[0].name);await page.locator('.b-title').filter({hasText:items[0].name}).waitFor();
 await page.locator('.b-cost').click();await page.locator('.modal [name=purchase_rate]').fill('100');await page.locator('.modal [name=reason]').fill('Verified acquisition cost in isolated test');await page.locator('.modal button[type=submit]').click();await page.locator('.modal').waitFor({state:'detached'});
 let response=await page.request.post(base+'/api/sales',{data:{payment_mode:'CASH',lines:items.map(i=>({item_id:i.id,quantity:1}))}});assert.equal(response.ok(),true,await response.text());const bill=await response.json();
 await page.goto(base+'/reports');await page.locator('[data-report=profit]').click();await page.locator('.report-parameters').waitFor();await page.keyboard.press('Alt+g');await page.locator('.report-document').waitFor();
 assert.match(await page.locator('.report-table').innerText(),/Cost data missing/);assert.match(await page.locator('.report-table tfoot').innerText(),/—/);
 assert.equal(await page.locator('.report-table').innerText().then(t=>t.includes('100.00%')),false);
 const today=await page.locator('[name=from]').inputValue();response=await page.request.get(base+`/api/reports/profit-margin?from=${today}`);let financial=await response.json();
 assert.equal(financial.totals.revenue,300);assert.equal(financial.totals.cogs,null);assert.equal(financial.dataQuality.missing_cost_lines,1);
 await page.screenshot({path:'/tmp/pharmacy-profit-missing-cost.png'});
 await page.locator('[data-cost-issues]').click();await page.locator('.modal [name=purchase_rate]').fill('80');await page.locator('.modal [name=reason]').fill('Verified original opening stock cost; isolated test');await page.locator('.modal button[type=submit]').click();await page.locator('.modal').waitFor({state:'detached'});
 await page.waitForFunction(()=>!document.querySelector('[data-generate]').disabled);
 response=await page.request.get(base+`/api/reports/profit-margin?from=${today}`);financial=await response.json();
 assert.equal(financial.totals.cogs,180);assert.equal(financial.totals.profit,120);assert.equal(financial.totals.margin,40);assert.equal(financial.dataQuality.missing_cost_lines,0);
 await page.screenshot({path:'/tmp/pharmacy-profit-resolved.png'});
 // Confirm future batch verification cannot rewrite the prior sale snapshot.
 response=await page.request.post(base+`/api/erp/inventory/${items[0].id}/batches/${(await (await page.request.get(base+`/api/erp/inventory/${items[0].id}`)).json()).batches[0].id}/cost`,{data:{purchase_rate:120,reason:'Later receipt cost in isolated test'}});assert.equal(response.ok(),true);
 const profitability=await (await page.request.get(base+`/api/reports/item-profitability?from=${today}`)).json();assert.equal(profitability.totals.cogs,180);
 assert.deepEqual(errors,[]);
 console.log('PASS: batch cost verification, missing cost masks profit/margin, historical-cost repair, persisted corrected COGS, item-profitability API and historical snapshot immutability');
}finally{await browser.close();}
