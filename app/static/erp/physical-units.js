import { esc, h, unitName } from 'erp/core';

const profiles = {
  TABLET: ['Tablets', 'TABLET', 'STRIP', 'TABLET'], CAPSULE: ['Capsules', 'CAPSULE', 'STRIP', 'CAPSULE'],
  SYRUP: ['Syrup bottles', 'BOTTLE', 'BOTTLE', 'SYRUP'], DROPS: ['Drop bottles', 'BOTTLE', 'BOTTLE', 'DROPS'],
  CREAM: ['Cream tubes', 'TUBE', 'TUBE', 'CREAM'], OINTMENT: ['Ointment tubes', 'TUBE', 'TUBE', 'OINTMENT'],
  GEL: ['Gel tubes', 'TUBE', 'TUBE', 'GEL'], POWDER: ['Powder packs', 'PACK', 'PACK', 'POWDER'],
  SOAP: ['Soap bars', 'PIECE', 'PIECE', 'SOAP'], INJECTION: ['Injection vials', 'VIAL', 'VIAL', 'INJECTION'],
  INHALER: ['Inhalers', 'PIECE', 'PIECE', 'INHALER'], SACHET: ['Sachets', 'SACHET', 'SACHET', 'SACHET'],
  PIECE: ['Pieces / devices', 'PIECE', 'PIECE', 'DEVICE'], UNIT: ['Other counted units', 'UNIT', 'PACK', ''],
  BOTTLE: ['Bottles', 'BOTTLE', 'BOTTLE', ''], TUBE: ['Tubes', 'TUBE', 'TUBE', ''], PACK: ['Whole packs', 'PACK', 'PACK', ''],
  VIAL: ['Vials', 'VIAL', 'VIAL', ''], AMPOULE: ['Ampoules', 'AMPOULE', 'BOX', 'INJECTION'],
  JAR: ['Jars', 'JAR', 'JAR', ''], BOX: ['Whole boxes', 'BOX', 'BOX', ''], KIT: ['Kits', 'KIT', 'KIT', 'KIT'], PAIR: ['Pairs', 'PAIR', 'PACK', 'DEVICE'],
};

export function equivalent(receipt) {
  return receipt?.resolved ? `${receipt.received_base_units} ${unitName(receipt.base_unit, receipt.received_base_units)}` : 'Quantity not confirmed';
}

export function physicalEditor({ form = '', base = '', count = 1, quantity = 1, free = 0, quantities = true, locked = false } = {}) {
  const initial = Object.hasOwn(profiles, form) ? form : Object.hasOwn(profiles, base) ? base : 'UNIT';
  const el = h(`<div class="form-grid physical-editor">
    <label class="full">Item form<select name="physical_form" ${locked ? 'disabled' : ''}>${Object.entries(profiles).map(([k,p]) => `<option value="${k}" ${k === initial ? 'selected' : ''}>${esc(p[0])}</option>`).join('')}</select></label>
    <label><span data-count-label>Units per strip</span><input name="physical_count" type="number" min="1" max="10000" step="1" value="${esc(count || 1)}" required ${locked ? 'disabled' : ''}></label>
    ${quantities ? `<label><span data-qty-label>Strip quantity (paid)</span><input name="physical_quantity" inputmode="decimal" value="${esc(quantity)}" required></label>
      <label><span data-free-label>Free strips</span><input name="physical_free" inputmode="decimal" value="${esc(free)}" required></label>` : ''}
    <p class="full hint" data-physical-preview aria-live="polite"></p>
  </div>`);
  const f = n => el.querySelector(`[name="physical_${n}"]`);
  const sync = () => {
    const p = profiles[f('form').value], solid = ['TABLET','CAPSULE'].includes(p[1]);
    if (!solid && f('form').value !== 'UNIT') f('count').value = '1';
    f('count').readOnly = !solid && f('form').value !== 'UNIT';
    el.querySelector('[data-count-label]').textContent = solid ? `${unitName(p[1], 2)} per strip` : 'Stock units per container';
    if (quantities) {
      el.querySelector('[data-qty-label]').textContent = `${solid ? 'Strip' : 'Container'} quantity (paid)`;
      el.querySelector('[data-free-label]').textContent = solid ? 'Free strips' : 'Free containers';
    }
    const n = Number(f('count').value), paid = quantities ? Number(f('quantity').value) : 1, bonus = quantities ? Number(f('free').value) : 0;
    const total = Math.round((paid + bonus) * n * 1e6) / 1e6;
    const valid = Number.isInteger(n) && n > 0 && paid >= 0 && bonus >= 0 && Number.isInteger(total) && total > 0;
    el.querySelector('[data-physical-preview]').textContent = valid
      ? quantities ? `(${paid} paid + ${bonus} free) × ${n} = ${total} ${unitName(p[1], total)} available` : `1 ${unitName(p[2], 1)} = ${n} ${unitName(p[1], n)}`
      : 'Enter quantities that produce a positive whole number of stock units.';
    el.dispatchEvent(new CustomEvent('packaging-preview', { bubbles: true }));
  };
  el.packagingDirty = false;
  const changed = () => { el.packagingDirty = true; sync(); };
  el.addEventListener('input', changed);
  el.addEventListener('change', changed);
  sync();
  el.packagingValues = () => {
    const p = profiles[f('form').value];
    return { form: f('form').value, base_unit: p[1], pack_unit: p[2], dosage_form: p[3], units_per_pack: f('count').value,
      quantity: quantities ? f('quantity').value : undefined, free: quantities ? f('free').value : undefined };
  };
  return el;
}
