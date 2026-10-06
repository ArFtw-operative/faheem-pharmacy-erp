// Udhaar at the POS, the completion banner, manual bills kept out of sales, the Udhaar Ledger,
// the Counter Report, the Inventory totals footer and the logo on an empty workspace — in a real browser.
//   UDHAAR_TEST_URL=http://127.0.0.1:8031 UDHAAR_TEST_USER=… UDHAAR_TEST_PASSWORD=… node scripts/test-udhaar-counter-browser.mjs
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
const base = process.env.UDHAAR_TEST_URL || 'http://127.0.0.1:8025';
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/login');
  await page.fill('[name=username]', process.env.UDHAAR_TEST_USER || 'shortcut-test');
  await page.fill('[name=password]', process.env.UDHAAR_TEST_PASSWORD || 'isolated-browser-test');
  await page.click('button[type=submit]');
  await page.waitForURL((url) => !url.pathname.startsWith('/login'));
  const tag = 'Udtest' + Date.now().toString(36);
  const r = await page.request.post(base + '/api/erp/inventory', { data: { name: `${tag} tab`, base_unit: 'TABLET', pack_unit: 'TABLET', units_per_pack: 1 } });
  const item = await r.json();
  assert.equal((await page.request.post(base + `/api/erp/inventory/${item.id}/adjust`, { data: { direction: 'IN', quantity: 50, batch_no: 'UD1', expiry: '12/2099', mrp: '100', reason: 'Browser test' } })).ok(), true);
  const mobile = '98' + String(Date.now()).slice(-8);
  assert.equal((await page.request.post(base + '/api/customers', { data: { name: `${tag} customer`, mobile } })).ok(), true);
  const vis = (sel) => page.locator('.screen:not([hidden]) ' + sel);
  await page.goto(base + '/app/pos'); await page.locator('.pos').first().waitFor();
  await page.keyboard.press('Alt+n'); await page.waitForTimeout(400);       // a fresh bill
  const add = async (qty) => {
    await vis('.q').fill(tag); await vis('.pos-search .drop tr.on').waitFor();
    await page.keyboard.press('Enter'); await vis('.qty-in').waitFor();
    await page.keyboard.type(String(qty)); await page.keyboard.press('Enter');
  };
  // Udhaar without a customer is refused; with one it needs (and proposes) due + reminder dates
  await add(2);
  await page.keyboard.press('Shift+F9');
  await vis('.cq').fill(mobile); await vis('.cdrop tr.on').waitFor(); await page.keyboard.press('Enter');
  await page.keyboard.press('Shift+F9');
  await page.waitForFunction(() => document.querySelector('.screen:not([hidden]) .ud-due')?.value);
  assert.ok(await vis('.pay-udhaar').isVisible());
  await page.keyboard.press('F12');
  assert.match(await vis('.pos-summary').innerText(), /UDHAAR OUTSTANDING/);
  await page.keyboard.press('Enter');
  await vis('.done-banner.udhaar').waitFor();
  assert.match(await vis('.done-banner').innerText(), /SALE COMPLETED — UDHAAR — ₹200\.00 OUTSTANDING — DUE \d\d [A-Z]{3} \d{4}/);
  while (await vis('.pos-overlay').count()) { await page.keyboard.press('n'); await page.waitForTimeout(200); }
  // a cash bill says PAID
  await add(1);
  await page.keyboard.press('F9'); await page.keyboard.press('F12'); await page.keyboard.press('Enter');
  await vis('.done-banner.paid').waitFor();
  assert.match(await vis('.done-banner').innerText(), /SALE COMPLETED — CASH — ₹100\.00 PAID/);
  while (await vis('.pos-overlay').count()) { await page.keyboard.press('n'); await page.waitForTimeout(200); }
  // a manual bill is saved as a record, never a sale
  await page.keyboard.press('Alt+l');
  await vis('.q').fill(`${tag} typed item`); await page.waitForTimeout(400); await page.keyboard.press('Enter');
  await vis('input.mi-rate').first().fill('50'); await page.keyboard.press('Enter');
  await page.keyboard.press('F12'); await page.keyboard.press('Enter');
  await vis('.done-banner.manual').waitFor();
  while (await vis('.pos-overlay').count()) { await page.keyboard.press('n'); await page.waitForTimeout(200); }
  const sales = await (await page.request.get(base + '/api/erp/sales?q=' + tag)).json();
  assert.equal(sales.sales.length, 2);
  assert.equal((await (await page.request.get(base + '/api/erp/manual-bills?q=' + tag)).json()).total, 1);
  // Udhaar Ledger: part payment leaves the rest owed
  await page.goto(base + '/app/udhaar'); await vis('.ud-views').waitFor();
  await vis('.f-q').fill(mobile); await page.waitForTimeout(500);
  await vis('.grid tbody tr').first().click();
  await page.keyboard.press('F4'); await page.fill('.modal [name=amount]', '50'); await page.keyboard.press('Enter');
  await page.waitForFunction(() => /Partially Paid/.test(document.querySelector('.screen:not([hidden]) .grid tbody tr')?.innerText || ''));
  assert.match(await vis('.grid tbody tr').first().innerText(), /150\.00/);
  // Counter Report: Udhaar given and collected, drill-down to the bill
  await page.goto(base + '/app/counter'); await vis('.c-modes').waitFor();
  await vis('.c-modes tr[data-mode=UDHAAR]').click(); await vis('.c-docs').waitFor();
  assert.match(await vis('.c-drill').innerText(), /Udhaar bills/);
  // Inventory: totals of the filtered products
  await page.goto(base + '/app/inventory'); await vis('.inv-totals div').first().waitFor();
  await vis('.f-q').fill(tag); await page.waitForTimeout(800);
  assert.match((await vis('.inv-totals').innerText()).replace(/\s+/g, ' '), /Total Items 1 Total Quantity 47 .*Total MRP Value ₹4,700\.00/);
  // every tab closed: the pharmacy logo
  while (await page.locator('.wtab').count()) {
    await page.locator('.wtab [data-close]').first().click(); await page.waitForTimeout(200);
    if (await page.locator('.modal').count()) await page.keyboard.press('Enter');
  }
  assert.match(await page.evaluate(() => getComputedStyle(document.querySelector('#workspace')).backgroundImage), /full-stacked-color\.svg/);
  assert.deepEqual(errors, []);
  console.log('Udhaar / counter / manual bill / totals browser test passed');
} finally { await browser.close(); }
