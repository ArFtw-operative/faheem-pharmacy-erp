// Live refresh between workspace tabs and PCs, in a real browser:
//  - a category / item form added in Categories & Forms is offered at once by the purchase line's new-product
//    choosers (same window) and within one refresh round when added on another PC;
//  - an item form added while Inventory is open appears in its Form filter;
//  - products a purchase creates appear in an Inventory tab that is already open — also when the purchase is
//    posted seconds after the page was opened (before the first refresh round), and right after F12 Post.
//   LIVE_SYNC_TEST_URL=http://127.0.0.1:8031 LIVE_SYNC_TEST_USER=… LIVE_SYNC_TEST_PASSWORD=… \
//   [LIVE_SYNC_TEST_TOTP=<base32 secret of an account that already has two-step sign-in>] node scripts/test-live-sync-browser.mjs
// Run it against a scratch server and a copy of the data, never the pharmacy's live database.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';

const base = process.env.LIVE_SYNC_TEST_URL || 'http://127.0.0.1:8025';
const user = process.env.LIVE_SYNC_TEST_USER || 'shortcut-test';
const password = process.env.LIVE_SYNC_TEST_PASSWORD || 'isolated-browser-test';
let secret = process.env.LIVE_SYNC_TEST_TOTP || '';
const totp = (s) => {
  const bits = [...s.replace(/\s/g, '').toUpperCase()].map((c) => 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'.indexOf(c).toString(2).padStart(5, '0')).join('');
  const key = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const hash = crypto.createHmac('sha1', key).update(counter).digest(), o = hash[19] & 15;
  return ((hash.readUInt32BE(o) & 0x7fffffff) % 1000000).toString().padStart(6, '0');
};
async function login(page) {
  await page.goto(base + '/login');
  await page.fill('[name=username]', user); await page.fill('[name=password]', password);
  await page.click('button[type=submit]');
  await page.waitForURL((u) => !u.pathname.endsWith('/login'));
  if (page.url().includes('/2fa/setup')) {
    secret = (await page.locator('.secret').innerText()).replace(/\s/g, '');
    await page.fill('[name=code]', totp(secret)); await page.click('button[type=submit]');
    await page.request.post(base + '/login/2fa/done');
  } else if (page.url().includes('/2fa')) {
    assert.ok(secret, 'this account has two-step sign-in: set LIVE_SYNC_TEST_TOTP');
    await page.fill('[name=code]', totp(secret)); await page.click('button[type=submit]');
  }
}
const api = async (page, method, path, data) => {
  const r = await page.request.fetch(base + path, { method, data });
  assert.ok(r.ok(), `${method} ${path}: ${await r.text()}`);
  return r.json();
};
const tag = Date.now().toString(36).toUpperCase();
let n = 0;
/** A purchase whose one line becomes a new product; posted unless `post` is false. */
async function purchaseNew(page, { post = true } = {}) {
  const name = `LIVE SYNC ${tag} ${++n} TAB`, invoice = `LS-${tag}-${n}`;
  const sup = (await api(page, 'POST', '/api/erp/suppliers', { name: `Live Sync Supplier ${tag} ${n}` })).supplier;
  const csv = 'Product Code,Product Name,Pack,Batch,Expiry,Qty,Free,Rate,MRP,Amount\n' + `,${name},10S,L${tag}${n},May-2029,3,,20.00,30.00,60.00\n`;
  const doc = await page.request.post(base + '/api/erp/purchases/import', {
    multipart: { file: { name: 'live-sync.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) }, supplier_id: String(sup.id), invoice_no: invoice },
  }).then((r) => r.json());
  await api(page, 'POST', `/api/erp/purchases/${doc.purchase.id}/lines/bulk`, { line_ids: doc.lines.map((l) => l.id), changes: { new_product: true } });
  if (post) await api(page, 'POST', `/api/erp/purchases/${doc.purchase.id}/post`, {});
  return { name, invoice, id: doc.purchase.id };
}
const screen = (page, module) => page.locator(`.screen[data-screen="${module}"]:not([hidden])`);
const openModule = (page, module) => page.evaluate((m) => document.querySelector(`#modules [data-module="${m}"]`).click(), module);
async function shows(page, text, ms = 12000) {
  for (const end = Date.now() + ms; Date.now() < end; await page.waitForTimeout(200)) {
    if (await screen(page, 'inventory').getByText(text).count()) return true;
  }
  return false;
}

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = []; page.on('pageerror', (e) => errors.push(e.message));
  await login(page);

  // 1. Inventory just opened: a purchase posted before the first refresh round still shows
  await page.goto(base + '/app/inventory');
  await screen(page, 'inventory').locator('.f-count').waitFor();
  assert.ok(await shows(page, (await purchaseNew(page)).name), 'Inventory opened just now misses a product a purchase created');

  // 2. an item form added while Inventory is open appears in its Form filter
  const formName = 'Lozenge ' + tag;
  await api(page, 'POST', '/api/erp/item-forms', { name: formName, base_unit: 'PIECE', pack_unit: 'STRIP', counted: true });
  await page.waitForFunction((f) => [...document.querySelectorAll('.screen[data-screen="inventory"]:not([hidden]) .f-form option')]
    .some((o) => o.textContent === f), formName, { timeout: 12000 });

  // 3. Categories & Forms → the purchase line's new-product Category chooser, same window, no waiting
  const draft = await purchaseNew(page, { post: false });
  await openModule(page, 'purchases');
  await screen(page, 'purchases').getByText(draft.invoice).first().click();
  await page.keyboard.press('Enter');
  await screen(page, 'purchase').waitFor();
  const purchaseTab = await page.locator('#wtabs [data-tab].on').getAttribute('data-tab');
  const lineChoices = async () => {
    await page.locator(`#wtabs [data-tab="${purchaseTab}"]`).click();
    await screen(page, 'purchase').getByText(draft.name).first().click();
    await page.keyboard.press('Enter');
    const sel = page.locator('.modal-backdrop select[name=np_category]');
    await sel.waitFor();
    const out = { categories: await sel.locator('option').allTextContents(),
                  forms: await page.locator('.modal-backdrop select[name=np_form] option').allTextContents() };
    await page.keyboard.press('Escape');
    await page.locator('.modal-backdrop').waitFor({ state: 'detached' });
    return out;
  };
  const catA = 'Live Cat A ' + tag;
  await openModule(page, 'masters');
  await screen(page, 'masters').locator('.c-new').click();
  await page.locator('.modal-backdrop input[name=name]').fill(catA);
  await page.keyboard.press('Enter');
  await page.locator('.modal-backdrop').waitFor({ state: 'detached' });
  await screen(page, 'masters').getByText(catA).first().waitFor();
  const now = await lineChoices();
  assert.ok(now.categories.includes(catA), 'a category made in Categories & Forms is missing from the purchase chooser');
  assert.ok(now.forms.includes(formName), 'an item form added in this window is missing from the purchase form chooser');

  // 4. another PC adds a category while the purchase is open here: offered after one refresh round
  const other = await browser.newContext(); const pc2 = await other.newPage(); await login(pc2);
  const catB = 'Live Cat B ' + tag;
  await api(pc2, 'POST', '/api/erp/categories', { name: catB });
  await other.close();
  await page.waitForTimeout(5000);
  assert.ok((await lineChoices()).categories.includes(catB), 'a category added on another PC never reached this purchase chooser');

  // 5. F12 Post in the purchase tab, then straight to the Inventory tab that is already open
  await openModule(page, 'inventory');
  await page.locator(`#wtabs [data-tab="${purchaseTab}"]`).click();
  await screen(page, 'purchase').getByRole('button', { name: /Post to stock/ }).click();
  const dlg = page.locator('.modal-backdrop');
  if (await dlg.waitFor({ timeout: 3000 }).then(() => true, () => false)) {
    await dlg.locator('[data-yes], button[type=submit]').first().click();
    await dlg.waitFor({ state: 'detached', timeout: 30000 });
  }
  await page.locator('#st-msg', { hasText: /Posted/ }).waitFor({ timeout: 30000 });
  await openModule(page, 'inventory');
  assert.ok(await shows(page, draft.name, 1500), 'the open Inventory tab does not show the posted product at once');

  assert.deepEqual(errors, []);
  console.log('Live sync test passed');
} finally { await browser.close(); }
