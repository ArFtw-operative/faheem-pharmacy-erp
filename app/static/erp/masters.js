// Masters — data the pharmacy maintains itself, no developer involved.
//
//   Categories         the product category master (add, rename, reorder,
//                      activate/deactivate, merge, delete when unused). The
//                      database enforces that every product's category exists.
//   Units of measure   how each product is counted (detected automatically from
//                      its pack and form; Enter corrects an exception).
//
// F6 switches between the two lists.
import * as keys from "erp/keys";
import { $, $$, BOOT, api, debounce, esc, h, modal, unitName } from "erp/core";
import { Grid } from "erp/grid";

const t = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "—");

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  root.innerHTML = `<div class="masters">
    <div class="mtabs" role="tablist">
      <button type="button" data-panel="categories" role="tab">Categories</button>
      <button type="button" data-panel="uom" role="tab">Units of measure</button>
      <span class="hint">F6 switches</span>
    </div>
    <section class="mpanel" data-panel="categories"></section>
    <section class="mpanel" data-panel="uom" hidden></section>
  </div>`;
  const panels = {
    categories: categoriesPanel(ctx, $('.mpanel[data-panel="categories"]', root), CAN),
    uom: uomPanel(ctx, $('.mpanel[data-panel="uom"]', root), CAN),
  };
  let current = params.panel === "uom" ? "uom" : "categories";
  function show(name, focus = true) {
    current = name;
    $$(".mtabs [data-panel]", root).forEach((b) => b.classList.toggle("on", b.dataset.panel === name));
    $$(".mpanel", root).forEach((p) => { p.hidden = p.dataset.panel !== name; });
    panels[name].shown(focus);
  }
  $(".mtabs", root).addEventListener("click", (e) => { const b = e.target.closest("[data-panel]"); if (b) show(b.dataset.panel); });
  show(current, false);

  return {
    get keys() { return keys.bar("masters"); },
    onKey(e, name) {
      if (keys.matches("masters.panel", name)) { show(current === "categories" ? "uom" : "categories"); return true; }
      return panels[current].onKey(name);
    },
    onShow({ focus }) { if (focus) setTimeout(() => panels[current].shown(true), 0); },
  };
}

// ---------------------------------------------------------------------- categories
function categoriesPanel(ctx, el, CAN) {
  let rows = [];
  el.innerHTML = `
    <div class="filters">
      <b>Product categories</b>
      <span class="hint">Used by products, purchases, inventory, POS and every report. Renaming updates all of them at once.</span>
      <span class="spacer"></span>
      ${CAN["inventory.edit"] ? '<button type="button" class="btn primary c-new">New category <kbd>F3</kbd></button>' : ""}
      <span class="c-count muted"></span>
    </div>
    <p class="m-help"><kbd>Enter</kbd> rename · <kbd>Space</kbd> activate / deactivate · <kbd>Alt+↑</kbd> <kbd>Alt+↓</kbd> reorder ·
      <kbd>F4</kbd> merge into another category (moves its products) · <kbd>Delete</kbd> delete an unused category.
      Inactive categories disappear from choosers but stay on existing products and in reports.</p>`;
  const grid = new Grid({
    label: "Categories", storageKey: "masters-categories", empty: "No categories.",
    columns: [
      { key: "pos", label: "#", width: 40, align: "num", render: (r) => rows.indexOf(r) + 1 },
      { key: "name", label: "Category", width: 260, render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "code", label: "Code", width: 160, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
      { key: "products", label: "Products", width: 90, align: "num" },
      { key: "active", label: "Status", width: 90, cellClass: (r) => (r.active ? "st-ok" : "st-out"), render: (r) => (r.active ? "Active" : "Inactive") },
    ],
    rowClass: (r) => (r.active ? "" : "dim"),
    onActivate: (r) => rename(r),
    contextMenu: () => CAN["inventory.edit"] ? [
      { label: "Rename", key: "Enter", action: () => rename(grid.selected) },
      { label: "Activate / deactivate", key: "Space", action: () => toggle(grid.selected) },
      { label: "Merge into…", key: keys.keyFor("masters.merge"), action: () => merge(grid.selected) },
      { label: "Delete", key: "Delete", action: () => remove(grid.selected) },
    ] : [],
  });
  el.append(grid.el);

  async function run(fn, done) {
    try { const d = await fn(); rows = d.categories; grid.setRows(rows, { keep: true }); count(); if (done) ctx.status(done, "ok"); return d; }
    catch (err) { ctx.status(err.message, "error"); return null; }
  }
  const count = () => { $(".c-count", el).textContent = `${rows.length} categories · ${rows.filter((r) => r.active).length} active`; };
  const load = () => run(() => api("/api/erp/categories"));
  const guard = () => { if (!CAN["inventory.edit"]) { ctx.status("Managing categories needs inventory edit rights", "warn"); return false; } return true; };

  async function add() {
    if (!guard()) return;
    const out = await modal({
      title: "New category", submitLabel: "Create",
      body: '<div class="form-grid"><label class="full">Category name<input name="name" maxlength="60" placeholder="e.g. Personal care" required autofocus></label><p class="full hint">A permanent code is made from the name (Personal care → PERSONAL_CARE). The name can be changed later.</p></div>',
      onSubmit: (form) => api("/api/erp/categories", { method: "POST", body: { name: form.name.value } }),
    });
    if (out) { rows = out.categories; grid.setRows(rows); grid.select(rows.findIndex((r) => r.code === out.code)); count(); grid.focus(); ctx.status(`Category ${out.code} created`, "ok"); }
  }
  async function rename(r) {
    if (!r || !guard()) return;
    const out = await modal({
      title: "Rename category", submitLabel: "Save",
      body: `<div class="form-grid"><label class="full">Name<input name="name" maxlength="60" value="${esc(r.name)}" required autofocus></label><p class="full hint">Code <b class="mono">${esc(r.code)}</b> stays the same; ${r.products} product(s) and all reports show the new name.</p></div>`,
      onSubmit: (form) => api(`/api/erp/categories/${encodeURIComponent(r.code)}`, { method: "PUT", body: { name: form.name.value } }),
    });
    if (out) { rows = out.categories; grid.setRows(rows, { keep: true }); ctx.status("Category renamed", "ok"); grid.focus(); }
  }
  const toggle = (r) => r && guard() && run(() => api(`/api/erp/categories/${encodeURIComponent(r.code)}`, { method: "PUT", body: { active: !r.active } }),
    `${r.name} ${r.active ? "deactivated" : "activated"}`);
  const moveBy = (r, step) => r && guard() && run(() => api(`/api/erp/categories/${encodeURIComponent(r.code)}`, { method: "PUT", body: { move: step } }))
    .then(() => { const i = rows.findIndex((x) => x.code === r.code); if (i >= 0) grid.select(i); grid.focus(); });
  async function merge(r) {
    if (!r || !guard()) return;
    const others = rows.filter((x) => x.code !== r.code);
    const out = await modal({
      title: `Merge ${r.name}`, submitLabel: "Merge",
      body: `<div class="form-grid"><p class="full">Moves ${r.products} product(s) from <b>${esc(r.name)}</b> into the chosen category, then removes ${esc(r.name)}. One transaction; recorded in the audit trail.</p>
        <label class="full">Merge into<select name="into" autofocus>${others.map((x) => `<option value="${esc(x.code)}">${esc(x.name)}${x.active ? "" : " (inactive)"}</option>`).join("")}</select></label></div>`,
      onSubmit: (form) => api(`/api/erp/categories/${encodeURIComponent(r.code)}/merge`, { method: "POST", body: { into: form.into.value } }),
    });
    if (out) { rows = out.categories; grid.setRows(rows); count(); grid.focus(); ctx.status(`${r.name} merged · ${out.moved} product(s) moved`, "ok"); }
  }
  async function remove(r) {
    if (!r || !guard()) return;
    if (r.products) { ctx.status(`${r.name} is used by ${r.products} product(s) — merge it (F4) instead`, "warn"); return; }
    if (!(await window.erpConfirm(`Delete category ${r.name}?`))) return;
    await run(() => api(`/api/erp/categories/${encodeURIComponent(r.code)}`, { method: "DELETE" }), `${r.name} deleted`);
    grid.focus();
  }
  grid.el.addEventListener("keydown", (e) => {
    if (e.target !== grid.el) return;
    if (e.key === " ") { e.preventDefault(); toggle(grid.selected); }
    else if (e.key === "Delete") { e.preventDefault(); remove(grid.selected); }
  });
  const btn = $(".c-new", el);
  if (btn) btn.onclick = add;
  load();
  return {
    shown(focus) { if (focus) grid.focus(); },
    onKey(name) {
      if (keys.matches("masters.new", name)) { add(); return true; }
      if (keys.matches("masters.merge", name)) { merge(grid.selected); return true; }
      if (keys.matches("masters.up", name)) { moveBy(grid.selected, -1); return true; }
      if (keys.matches("masters.down", name)) { moveBy(grid.selected, 1); return true; }
      if (keys.matches("masters.refresh", name)) { load(); return true; }
      if (keys.matches("masters.search", name)) { grid.focus(); return true; }
      return false;
    },
  };
}

// ---------------------------------------------------------------------- units of measure
function uomPanel(ctx, el, CAN) {
  let rows = [];
  el.innerHTML = `
    <div class="filters">
      <b>Units of measure</b>
      <label>Search<input class="m-q" autocomplete="off" placeholder="name, code or pack"></label>
      <label>Show<select class="m-scope">
        <option value="all">All products</option><option value="loose">Sold loose</option><option value="whole">Sold whole</option>
        <option value="undetected">Not detected (sold per unit)</option><option value="manual">Corrected by a user</option></select></label>
      <span class="spacer"></span><span class="m-count muted"></span>
    </div>
    <p class="m-help">Configured automatically from the printed pack and form — no manual loose-sale switch.
      Purchases, imports, POS, returns and adjustments all convert through this setup: 1 strip received = +10 tablets; 1 bottle = 1 bottle (200 mL is content, not stock).
      <kbd>Enter</kbd> corrects an exception.</p>`;
  const grid = new Grid({
    label: "Units of measure", storageKey: "masters-uom", empty: "No products.",
    columns: [
      { key: "code", label: "Code", width: 90, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
      { key: "name", label: "Product", width: 290, render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "pack_raw", label: "Pack", width: 70 },
      { key: "form", label: "Form", width: 86, render: (r) => esc(t(r.form)) },
      { key: "base_unit", label: "Sale / Stock unit", width: 110, render: (r) => esc(t(r.base_unit)) },
      { key: "pack_unit", label: "Purchase unit", width: 100, render: (r) => esc(t(r.pack_unit)) },
      { key: "upp", label: "Conversion", width: 120, render: (r) => (r.upp > 1 ? `1 ${esc(t(r.pack_unit).toLowerCase())} = ${r.upp} ${esc(unitName(r.base_unit, r.upp))}` : "1 : 1") },
      { key: "loose", label: "Loose", width: 56, align: "center", render: (r) => (r.loose ? "Yes" : "No") },
      { key: "content", label: "Content", width: 80 },
      { key: "stock", label: "Stock", width: 150, align: "num", render: (r) => `${r.stock} ${esc(unitName(r.base_unit, r.stock))}${r.upp > 1 && r.stock ? ` <small>${esc(r.stock_label)}</small>` : ""}` },
      { key: "source", label: "Set by", width: 86, cellClass: (r) => (r.source === "MANUAL" ? "st-low" : !r.detected ? "st-out" : ""), render: (r) => (r.source === "MANUAL" ? "Corrected" : r.detected ? "Auto" : "Not detected") },
    ],
    onActivate: (r) => correct(r),
  });
  el.append(grid.el);

  const q = $(".m-q", el), scope = $(".m-scope", el);
  let loaded = false;
  async function load() {
    try {
      const d = await api(`/api/erp/uom?scope=${scope.value}&q=${encodeURIComponent(q.value.trim())}`);
      rows = d.rows;
      grid.setRows(rows, { keep: true });
      $(".m-count", el).textContent = `${d.total} product${d.total === 1 ? "" : "s"}`;
      loaded = true;
    } catch (err) { ctx.status(err.message, "error"); }
  }
  const reload = debounce(load, 150);
  q.addEventListener("input", reload);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); grid.focus(); } });
  scope.addEventListener("change", load);
  grid.el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); q.focus(); } }, true);

  async function correct(r) {
    if (!CAN["inventory.edit"]) { ctx.status("Correcting a unit of measure needs inventory edit rights", "warn"); return; }
    const det = r.detected;
    const body = h(`<div class="form-grid">
      <p class="full"><b>${esc(r.name)}</b> · pack <b>${esc(r.pack_raw || "—")}</b> · stock ${r.stock} ${esc(unitName(r.base_unit, r.stock))}<br>
        <span class="hint">Detected: ${det ? esc(det.reason) : "nothing usable in the pack text"}</span></p>
      <label>Sale / stock unit<select name="base_unit">${BOOT.units.base.map((u) => `<option value="${u}" ${u === r.base_unit ? "selected" : ""}>${t(u)}</option>`).join("")}</select></label>
      <label>Purchase unit<select name="pack_unit">${BOOT.units.pack.map((u) => `<option value="${u}" ${u === r.pack_unit ? "selected" : ""}>${t(u)}</option>`).join("")}</select></label>
      <label>Sale units per purchase unit<input name="units_per_pack" type="number" min="1" max="10000" value="${r.upp}" autofocus></label>
      <label>Dosage form<select name="dosage_form">${BOOT.units.forms.map((u) => `<option value="${u}" ${u === r.form ? "selected" : ""}>${u ? t(u) : "—"}</option>`).join("")}</select></label>
      <p class="full preview"></p></div>`);
    const sync = () => {
      const upp = Number(body.querySelector("[name=units_per_pack]").value) || 1;
      const unit = body.querySelector("[name=base_unit]").value;
      const after = r.upp === 1 ? r.stock * upp : r.stock;
      body.querySelector(".preview").innerHTML = `${upp > 1 ? "Sold loose automatically" : "Sold whole"}. `
        + (r.stock ? `Stock becomes <b>${after}</b> ${esc(unitName(unit, after))}${r.upp === 1 && upp > 1 ? ` (${r.stock} × ${upp}, posted to the ledger)` : ""}.` : "Future stock is counted this way.");
    };
    body.addEventListener("input", sync);
    body.addEventListener("change", sync);
    const out = await modal({
      title: "Correct unit of measure — " + r.name, body, submitLabel: "Save correction", onOpen: sync,
      onSubmit: (form) => api(`/api/erp/inventory/${r.id}`, { method: "PUT", body: Object.fromEntries(new FormData(form).entries()) }),
    });
    if (out) { ctx.status(`${r.name}: unit of measure corrected`, "ok"); await load(); grid.focus(); }
  }

  return {
    shown(focus) { if (!loaded) load(); if (focus) q.focus(); },
    onKey(name) {
      if (keys.matches("masters.refresh", name)) { load(); return true; }
      if (keys.matches("masters.search", name)) { q.focus(); return true; }
      return false;
    },
  };
}
