// POS item search: Enter adds the row the cashier highlighted with ↑↓ (it used to re-run the search,
// jump back to the first row and reopen a line already on the bill instead of adding the new item).
//   POS_PICK_TEST_URL=http://127.0.0.1:8031 POS_PICK_USER=… POS_PICK_PASSWORD=… node scripts/test-pos-pick-browser.mjs
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
const base = process.env.POS_PICK_TEST_URL || 'http://127.0.0.1:8025';
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/login');
  await page.fill('[name=username]', process.env.POS_PICK_USER || 'shortcut-test');
  await page.fill('[name=password]', process.env.POS_PICK_PASSWORD || 'isolated-browser-test');
  await page.click('button[type=submit]');
  await page.waitForURL((url) => !url.pathname.startsWith('/login'));
  // six stocked items that all match one search term
  const tag = 'Pickrow' + Date.now().toString(36);
  const names = [];
  for (let k = 0; k < 6; k++) {
    const name = `${tag} ${String.fromCharCode(65 + k)} tab`;
    const r = await page.request.post(base + '/api/erp/inventory', { data: { name, base_unit: 'TABLET', pack_unit: 'TABLET', units_per_pack: 1 } });
    assert.equal(r.ok(), true);
    const item = await r.json();
    const s = await page.request.post(base + `/api/erp/inventory/${item.id}/adjust`, { data: { direction: 'IN', quantity: 20, batch_no: 'PICK' + k, expiry: '12/2099', mrp: '10', reason: 'Browser test' } });
    assert.equal(s.ok(), true);
    names.push(name);
  }
  await page.goto(base + '/app/pos');
  await page.locator('.pos').first().waitFor();
  const scr = page.locator('.screen:not([hidden])');
  const lines = () => scr.locator('.bill tbody tr').evaluateAll((trs) => trs.map((t) => t.querySelector('.prod').firstChild.textContent));
  for (let k = 0; k < names.length; k++) {
    await scr.locator('.q').fill(tag);
    await scr.locator('.pos-search .drop tr.on').waitFor();
    for (let j = 0; j < k; j++) await page.keyboard.press('ArrowDown');
    assert.equal(await scr.locator('.pos-search .drop tr.on b').first().textContent(), names[k]);
    await page.keyboard.press('Enter');
    await scr.locator('.qty-in').waitFor();
    await page.keyboard.type('2'); await page.keyboard.press('Enter');
    await page.waitForFunction((n) => document.querySelectorAll('.screen:not([hidden]) .bill tbody tr').length === n && !document.querySelector('.screen:not([hidden]) .qty-in'), k + 1);
  }
  assert.deepEqual(await lines(), names);
  assert.deepEqual(await scr.locator('.bill tbody td.qty').allTextContents(), names.map(() => '2'));
  // clicking a row already on the bill reopens that line and says so — nothing is added or lost
  await scr.locator('.q').fill(tag);
  await scr.locator('.pos-search .drop tr[data-i="5"]').click();
  await scr.locator('.qty-in').waitFor();
  assert.match(await page.locator('#st-msg').textContent(), /already on the bill \(line 6/);
  await page.keyboard.press('Escape');
  assert.equal((await lines()).length, 6);
  assert.deepEqual(errors, []);
  console.log('POS pick test passed');
} finally { await browser.close(); }
