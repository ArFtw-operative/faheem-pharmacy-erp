import { BOOT, api, esc, h, modal, toBase, unitName } from 'erp/core';

// Item forms come from the form master (Categories & Forms → Item forms). The last entry of every
// form list creates a new form on the spot. pickCategory is the same for categories.
export const NEW_FORM = '__new__';
const units = () => BOOT.units || { base: [], pack: [] };
export const forms = () => (BOOT.item_forms || []).filter((f) => f.active !== false);
const byCode = (code) => forms().find((f) => f.code === code);

/** The form a product is stocked as: its dosage form, else the unit it is counted in. */
export function formFor(dosage = '', base = '') {
  if (byCode(dosage)) return dosage;
  const hit = forms().find((f) => f.dosage_form && f.dosage_form === dosage) || byCode(base)
    || forms().find((f) => f.base_unit === base && !f.counted) || forms().find((f) => f.base_unit === base);
  return hit ? hit.code : (byCode('UNIT') ? 'UNIT' : forms()[0]?.code || '');
}

/** <option>s for a form <select>, ending with "Create new form…". */
export function formOptions(selected = '', { blank = false, create = true } = {}) {
  return (blank ? `<option value="" ${selected ? '' : 'selected'}>—</option>` : '')
    + forms().map((f) => `<option value="${esc(f.code)}" ${f.code === selected ? 'selected' : ''}>${esc(f.name)}</option>`).join('')
    + (create ? `<option value="${NEW_FORM}">＋ Create new form…</option>` : '');
}

/** Ask for a new item form, save it in the master and add it to the boot list. Resolves with the form or null. */
export async function createForm() {
  const u = units();
  const opt = (list, v) => list.filter(Boolean).map((x) => `<option value="${esc(x)}" ${x === v ? 'selected' : ''}>${esc(unitName(x, 1))}</option>`).join('');
  const out = await modal({ title: 'Create new item form', submitLabel: 'Create form',
    body: `<div class="form-grid">
      <label class="full">Form name<input name="name" required maxlength="60" autofocus placeholder="e.g. Mouthwash bottles, Nasal sprays, Insulin pens"></label>
      <label>Stock is counted in<select name="base_unit">${opt(u.base, 'PIECE')}</select></label>
      <label>Retail pack<select name="pack_unit">${opt(u.pack, 'PIECE')}</select></label>
      <label class="chk full"><input type="checkbox" name="counted"> One pack holds several stock units (e.g. tablets in a strip, syringes in a box)</label>
      <label>Container holds<select name="content_unit"><option value="">— nothing measured —</option><option value="ML">mL</option><option value="G">g</option><option value="MG">mg</option></select></label>
      <p class="full hint">The new form is available everywhere a form is chosen. Categories & Forms → Item forms lists and hides forms.</p></div>`,
    onSubmit: (form) => api('/api/erp/item-forms', { method: 'POST', body: {
      name: form.elements.name.value, base_unit: form.elements.base_unit.value, pack_unit: form.elements.pack_unit.value,
      counted: form.elements.counted.checked, content_unit: form.elements.content_unit.value } }) });
  if (!out) return null;
  BOOT.item_forms = out.forms;
  return out.form;
}

export const NEW_CATEGORY = '__new__';

/**
 * Ask for a category from the live list (a drop-down, read from the database each time), with
 * "＋ New category…" at the bottom. Resolves with { code, name } or null; ``apply(code)`` runs on
 * Apply and its result is returned as ``result`` (an error keeps the dialog open).
 */
export async function pickCategory({ title = 'Change category', intro = '', extra = '', current = '', apply, wide = false, submitLabel = 'Apply' }) {
  const { categories } = await api('/api/erp/categories');
  const active = categories.filter((c) => c.active);
  const body = h(`<div class="form-grid">
    ${intro ? `<p class="full">${intro}</p>` : ''}
    <label class="full">Category<select name="category" autofocus>
      ${current ? '' : '<option value="" selected>— choose —</option>'}
      ${active.map((c) => `<option value="${esc(c.code)}" ${c.code === current ? 'selected' : ''}>${esc(c.name)} (${c.products})</option>`).join('')}
      <option value="${NEW_CATEGORY}">＋ New category…</option></select></label>
    <label class="full" data-new hidden>New category name<input name="new_name" maxlength="60" placeholder="e.g. OTC, Cosmetics, Veterinary"></label>
    ${extra ? `<div class="full">${extra}</div>` : ''}
  </div>`);
  const sel = body.querySelector('[name="category"]'), box = body.querySelector('[data-new]');
  sel.addEventListener('change', () => { box.hidden = sel.value !== NEW_CATEGORY; if (!box.hidden) box.querySelector('input').focus(); });
  let chosen = null;
  const out = await modal({ title, body, wide, submitLabel, onOpen: () => sel.focus(),
    onSubmit: async (form) => {
      let code = sel.value, name = sel.selectedOptions[0]?.textContent.replace(/ \(\d+\)$/, '');
      if (code === NEW_CATEGORY) {
        name = form.elements.new_name.value.trim();
        if (!name) throw new Error('Type the new category name');
        const made = await api('/api/erp/categories', { method: 'POST', body: { name } });
        code = made.code;
        BOOT.category_options = made.categories.filter((c) => c.active).map((c) => ({ code: c.code, name: c.name }));
        sel.insertAdjacentHTML('afterbegin', `<option value="${esc(code)}">${esc(name)}</option>`);
        sel.value = code; box.hidden = true;           // a failure below does not create it twice
      }
      if (!code) throw new Error('Choose a category');
      chosen = { code, name };
      return apply ? apply(code) : true;
    } });
  return out ? { ...chosen, result: out } : null;
}

/** Make a form <select> open "Create new form…" when its last option is chosen. */
export function wireFormSelect(select, onChange = () => {}) {
  let previous = select.value;
  select.addEventListener('change', async () => {
    if (select.value !== NEW_FORM) { previous = select.value; onChange(); return; }
    const made = await createForm();
    const keepBlank = !!select.querySelector('option[value=""]');
    select.innerHTML = formOptions(made ? made.code : previous, { blank: keepBlank });
    previous = select.value;
    select.focus();
    onChange();
  });
}

export function physicalEditor({ form = '', base = '', count = 1, quantity = 1, free = 0, quantities = true, locked = false } = {}) {
  const initial = formFor(form, base);
  const el = h(`<div class="form-grid physical-editor">
    <label class="full">Item form<select name="physical_form" ${locked ? 'disabled' : ''}>${formOptions(initial, { create: !locked })}</select></label>
    <label><span data-count-label>Units per strip</span><input name="physical_count" type="number" min="1" max="10000" step="1" value="${esc(count || 1)}" required ${locked ? 'disabled' : ''}></label>
    ${quantities ? `<label><span data-qty-label>Strip quantity (paid)</span><input name="physical_quantity" inputmode="decimal" value="${esc(quantity)}" required></label>
      <label><span data-free-label>Free strips</span><input name="physical_free" inputmode="decimal" value="${esc(free)}" required></label>` : ''}
    <p class="full hint" data-physical-preview aria-live="polite"></p>
  </div>`);
  const f = n => el.querySelector(`[name="physical_${n}"]`);
  const sync = () => {
    const p = byCode(f('form').value) || { base_unit: 'UNIT', pack_unit: 'PACK', counted: true, dosage_form: '' };
    const solid = !!p.counted;
    if (!solid) f('count').value = '1';
    f('count').readOnly = !solid;
    const packWord = unitName(p.pack_unit, 1);
    el.querySelector('[data-count-label]').textContent = solid ? `${unitName(p.base_unit, 2)} per ${packWord}` : 'Stock units per container';
    if (quantities) {
      el.querySelector('[data-qty-label]').textContent = `${solid ? packWord.replace(/^./, (c) => c.toUpperCase()) : 'Container'} quantity (paid)`;
      el.querySelector('[data-free-label]').textContent = solid ? `Free ${unitName(p.pack_unit, 2)}` : 'Free containers';
    }
    const n = Number(f('count').value), paid = quantities ? Number(f('quantity').value) : 1, bonus = quantities ? Number(f('free').value) : 0;
    const total = toBase(paid + bonus, n);
    const valid = Number.isInteger(n) && n > 0 && paid >= 0 && bonus >= 0 && Number.isInteger(total) && total > 0;
    el.querySelector('[data-physical-preview]').textContent = valid
      ? quantities ? `(${paid} paid + ${bonus} free) × ${n} = ${total} ${unitName(p.base_unit, total)} available` : `1 ${unitName(p.pack_unit, 1)} = ${n} ${unitName(p.base_unit, n)}`
      : 'Enter quantities that produce a positive whole number of stock units.';
    el.dispatchEvent(new CustomEvent('packaging-preview', { bubbles: true }));
  };
  el.packagingDirty = false;
  const changed = () => { el.packagingDirty = true; sync(); };
  el.addEventListener('input', (e) => { if (e.target !== f('form')) changed(); });
  wireFormSelect(f('form'), changed);
  sync();
  el.packagingValues = () => {
    const p = byCode(f('form').value) || {};
    return { form: f('form').value, base_unit: p.base_unit, pack_unit: p.pack_unit, dosage_form: p.dosage_form || '', units_per_pack: f('count').value,
      quantity: quantities ? f('quantity').value : undefined, free: quantities ? f('free').value : undefined };
  };
  return el;
}
