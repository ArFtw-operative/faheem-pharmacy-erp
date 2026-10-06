import * as keys from "erp/keys";
import { physicalEditor, pickCategory } from 'erp/physical-units';
import { locationHtml, moveProducts } from 'erp/locations';
// Inventory — product grid + batch pane + detail strip. Back-office control:
// stock changes only through ledgered adjustments (F5), packaging through the
// guarded product editor (Ctrl+E). Nothing here edits a stock number directly.
import { $, $$, BOOT, api, debounce, describe, esc, toBase, fmtDateTime, fmtExp, fmtExpShort, h, modal, money, parseQty, store, unitName } from "erp/core";
import { Grid } from "erp/grid";

const STATUS = { OK: "OK", LOW: "Low", OUT: "Out", EXPIRING: "Expiring", EXPIRED: "Expired stock" };
const LOSS = [["COUNT", "Count shortfall"], ["DAMAGE", "Damage"], ["EXPIRED", "Expired"], ["LOOSE", "Loose / lost"]];
const title = (s) => String(s || "").charAt(0) + String(s || "").slice(1).toLowerCase();

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const saved = store.get("inv:filters", {});
  const F = { q: "", form: "", loose: "", stock: "", expiry: "", category: "", supplier: "", state: "", packaging: "", location: "", sort: "", letter: "", ...saved, q: saved.q || "" };
  if (params && params.location) F.location = params.location;
  const FILTERS = ["letter", "category", "form", "loose", "stock", "expiry", "supplier", "state", "packaging", "location", "sort"];
  let total = 0, loading = false, detail = null, pane = "products";
  const details = new Map();

  root.innerHTML = `
  <div class="inv">
    <div class="filters">
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="name, generic, code, barcode" value="${esc(F.q)}"></label>
      <label>Starts with<select class="f-letter" title="Products whose name starts with this letter"><option value="">All</option>
        ${"ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").map((c) => `<option value="${c}">${c}</option>`).join("")}<option value="#">0–9 / other</option></select></label>
      <label>Category<select class="f-category"><option value="">All</option></select></label>
      <label>Form<select class="f-form"><option value="">All</option>${(BOOT.units.forms || []).filter(Boolean).map((f) => `<option value="${f}">${title(f)}</option>`).join("")}</select></label>
      <label>Loose<select class="f-loose"><option value="">All</option><option value="yes">Yes</option><option value="no">No</option></select></label>
      <label>Stock<select class="f-stock"><option value="">All</option><option value="in">In stock</option><option value="low">Low</option><option value="out">Out of stock</option></select></label>
      <label>Expiry<select class="f-expiry"><option value="">All</option><option value="expiring">Expiring soon</option><option value="expired">Has expired stock</option></select></label>
      <label>Supplier<select class="f-supplier"><option value="">All</option></select></label>
      <label>Packaging<select class="f-packaging" title="How products are counted (units of measure)"><option value="">All</option>
        <option value="generic">Counted as plain packs</option><option value="manual">Corrected by a user</option><option value="auto">Set automatically</option></select></label>
      <label>Show<select class="f-state"><option value="">Active + disabled</option><option value="active">Active only</option>
        <option value="disabled">Disabled</option><option value="deleted">Recycle bin</option></select></label>
      ${CAN["rack.view"] ? `<label>Location<select class="f-location"><option value="">All</option><option value="assigned">Assigned</option>
        <option value="unassigned">Unassigned</option><option value="nobox">In a rack, no box</option></select></label>` : '<select class="f-location" hidden><option value=""></option></select>'}
      <label>Sort<select class="f-sort"><option value="">Name</option>${CAN["rack.view"] ? '<option value="rack">Rack / box</option>' : ""}
        <option value="category">Category</option><option value="stock">Stock (high first)</option></select></label>
      <button type="button" class="btn f-reset">Reset</button>
      <button type="button" class="btn f-columns" title="Choose inventory columns">Columns ⚙</button>
      <span class="spacer"></span><span class="f-count muted"></span>
    </div>
    <div class="bulkbar" hidden></div>
    <div class="split">
      <div class="pane products"><div class="pane-h">PRODUCTS</div></div>
      <div class="divider" title="Drag to resize"></div>
      <div class="pane batches"><section class="inv-detail" aria-label="Product specifications"></section><div class="pane-h">BATCHES <span class="b-title muted"></span>${CAN["purchase.view"] && CAN["inventory.edit"] ? ' <button type="button" class="btn b-cost">Verify cost</button>' : ""}</div></div>
    </div>
    <div class="inv-totals" aria-label="Totals of the filtered products"></div>
  </div>`;
  for (const k of FILTERS) $(".f-" + k, root).value = F[k] || "";

  // category and supplier choices come from the database (Categories & Forms)
  let catOptions = (BOOT.category_options || []).map((c) => ({ ...c, active: true }));
  async function loadChoices() {
    try {
      const [c, sp] = await Promise.all([api("/api/erp/categories"), api("/api/erp/suppliers")]);
      catOptions = c.categories;
      const cs = $(".f-category", root), ss = $(".f-supplier", root);
      cs.innerHTML = '<option value="">All</option>' + catOptions.map((x) => `<option value="${esc(x.code)}">${esc(x.name)}${x.active ? "" : " (inactive)"}</option>`).join("");
      ss.innerHTML = '<option value="">All</option>' + sp.suppliers.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
      cs.value = F.category || ""; ss.value = F.supplier || "";
      if (CAN["rack.view"]) {
        const o = await api("/api/erp/locations/options");
        const ls = $(".f-location", root);
        ls.innerHTML = `<option value="">All</option><option value="assigned">Assigned</option><option value="unassigned">Unassigned</option>`
          + (o.config.location_boxes_enabled ? '<option value="nobox">In a rack, no box</option>' : "")
          + (o.racks.length ? `<optgroup label="Rack">${o.racks.map((r) => `<option value="rack:${r.id}">${esc(r.code)}${r.name ? " · " + esc(r.name) : ""}</option>`).join("")}</optgroup>` : "")
          + (o.config.location_boxes_enabled && o.racks.some((r) => r.boxes.length) ? `<optgroup label="Box">${o.racks.flatMap((r) => r.boxes.map((b) => `<option value="box:${b.id}">${esc(r.code)} / ${esc(b.code)}${b.name ? " · " + esc(b.name) : ""}</option>`)).join("")}</optgroup>` : "");
        if (F.location && !ls.querySelector(`option[value="${F.location}"]`)) F.location = "";
        ls.value = F.location || "";
      }
    } catch (err) { ctx.status(err.message, "error"); }
  }
  loadChoices();

  // ---------------------------------------------------------------- grids
  const products = new Grid({
    label: "Products", storageKey: "inv-products", empty: "No products match. Use New product; Inventory → Import loads a stock sheet.",
    columns: [
      { key: "code", label: "Item Code", width: 92, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
      { key: "name", label: "Product", width: 300, render: (r) => `<b>${esc(r.name)}</b>${r.deleted ? ' <span class="tag off">recycle bin</span>' : r.active ? "" : ' <span class="tag off">disabled</span>'}${r.generic_pack && !r.deleted ? ' <span class="tag" title="Counted as plain packs: set units per strip in Packaging to sell loose">pack</span>' : ""}` },
      { key: "category", label: "Category", width: 100, render: (r) => esc(r.category_name || r.category) },
      { key: "form", label: "Form", width: 78, render: (r) => esc(title(r.form)) },
      { key: "upp", label: "Pack", width: 72, align: "num", title: "Units per purchase pack", render: (r) => (r.upp > 1 ? r.upp : esc(r.pack_raw || "1")) },
      { key: "base_unit", label: "Base Unit", width: 80, render: (r) => esc(title(r.base_unit)) },
      { key: "loose", label: "Loose", width: 54, align: "center", render: (r) => (r.loose ? "Yes" : "No") },
      { key: "stock", label: "Current Stock", width: 110, align: "num", render: (r) => `<b>${r.stock}</b> <small>${esc(unitName(r.base_unit, r.stock))}</small>` },
      { key: "equivalent", label: "Equivalent", width: 150, render: (r) => esc(r.equivalent || '—') },
      { key: "reorder", label: "Reorder", width: 64, align: "num", render: (r) => r.reorder || '<span class="muted">—</span>' },
      { key: "status", label: "Status", width: 96, cellClass: (r) => "st-" + r.status.toLowerCase(), render: (r) => STATUS[r.status] || r.status },
      ...(CAN["rack.view"] ? [
        { key: "rack", label: "Rack", width: 70, render: (r) => (r.rack ? `<b class="mono">${esc(r.rack)}</b>${r.rack_active === false ? ' <span class="tag off" title="Rack disabled">off</span>' : ""}` : '<span class="muted">—</span>') },
        { key: "rack_name", label: "Rack Name", width: 120, default: false, render: (r) => esc(r.rack_name || "") },
        { key: "box", label: "Box", width: 56, render: (r) => (r.box ? `<b class="mono">${esc(r.box)}</b>` : "") },
      ] : []),
    ],
    onSelect: (r) => showProduct(r),
    onActivate: () => focusPane("batches"),
    onNearEnd: () => loadMore(),
    rowClass: (r) => (r.active === false || r.deleted ? "item-off" : ""),
    multi: true,                                   // Shift+↑↓ · Space · Ctrl+A · Ctrl+click mark products
    onMarks: (marked) => {
      const n = marked.length;
      $(".f-count", root).innerHTML = n ? `<b>${n} selected</b> · Esc clears` : `${total} product${total === 1 ? "" : "s"}`;
      renderBulk(marked);
    },
    contextMenu: (r) => [
      { label: "Stock adjustment", actionId: "inv.adjust", key: keys.keyFor("inv.adjust"), action: () => adjust() },
      { label: "Stock ledger", key: keys.keyFor("inv.ledger"), action: () => ledger() },
      { label: "Stock history", key: keys.keyFor("inv.history"), action: () => history() },
      { label: "Edit product", actionId: "inv.edit", key: keys.keyFor("inv.edit"), action: () => edit() },
      { label: "Packaging (units of measure)…", actionId: "inv.edit", action: () => edit("p") },
      ...(CAN["rack.assign"] && (picked(r).length < 2 || CAN["rack.bulk_move"]) ? [{ label: picked(r).length > 1 ? `Move ${picked(r).length} products to rack…` : "Move to rack / box…", key: keys.keyFor("inv.moveRack"), action: () => moveRack(picked(r)) }] : []),
      ...(CAN["inventory.edit"] ? [{ label: picked(r).length > 1 ? `Change category of ${picked(r).length} products…` : "Change category…", key: keys.keyFor("inv.category"), action: () => changeCategory(picked(r)) }] : []),
      ...(CAN["inventory.delete"] ? statusItems(picked(r)) : []),
    ].filter((x) => (x.actionId !== "inv.adjust" || CAN["adjustment.create"]) && (x.actionId !== "inv.edit" || CAN["inventory.edit"])),
  });
  const batches = new Grid({
    label: "Batches", storageKey: "inv-batches", empty: "No batches yet. Stock adjustment → Increase → New batch adds opening stock.",
    columns: [
      { key: "batch_no", label: "Batch", width: 96, render: (r) => `<span class="mono">${esc(r.batch_no || "—")}</span>` },
      { key: "expiry", label: "Expiry", width: 70, render: (r) => fmtExpShort(r.expiry) },
      { key: "pack_mrp", label: "Pack MRP", width: 74, align: "num", render: (r) => money(r.pack_mrp) },
      { key: "unit_mrp", label: "Unit MRP", width: 70, align: "num", render: (r) => (r.upp > 1 ? money(r.unit_mrp) : '<span class="muted">—</span>') },
      { key: "stock", label: "Stock", width: 60, align: "num", render: (r) => `<b>${r.stock}</b>` },
      { key: "equivalent", label: "Equivalent", width: 110, render: (r) => esc(r.equivalent || '—') },
      { key: "status", label: "Status", width: 84, cellClass: (r) => "bst-" + r.status.replace(/\s/g, "").toLowerCase(), render: (r) => esc(r.status) },
    ],
    rowClass: (r) => (r.stock <= 0 ? "dim" : ""),
    onSelect: () => renderDetail(),
    onActivate: () => ledger(true),
    contextMenu: () => [
      { label: "Batch ledger", key: keys.keyFor("inv.ledger"), action: () => ledger(true) },
      { label: "Adjust this batch", actionId: "inv.adjust", key: keys.keyFor("inv.adjust"), action: () => adjust() },
    ].filter((x) => x.actionId !== "inv.adjust" || CAN["adjustment.create"]),
  });
  const priceColumn = (key, label, width = 105) => ({key, label, width, align: "num", default: false,
    render: r => r[key] == null ? `<span class="muted">${r.pricing_varies?.includes(key) ? "Varies" : "—"}</span>` : money(r[key])});
  const extraPrices = [{...priceColumn("pack_mrp", "MRP / pack"), default: true}];
  if (CAN["purchase.view"]) extraPrices.push({...priceColumn("purchase_rate", "Rate / pack"), default: true}, priceColumn("unit_purchase_rate", "Rate / unit"),
    {key: "purchase_invoice", label: "Purchase Invoice", width: 130, default: false});
  const provenance = [
    {key: "supplier", label: "Supplier", width: 150, render: (r) => esc(r.supplier || "—")},
    {key: "invoice", label: "Supplier invoice", width: 120, default: false, render: (r) => r.invoice ? `<span class="mono">${esc(r.invoice)}</span>${r.purchase_ref ? ` <span class="muted mono">${esc(r.purchase_ref)}</span>` : ""}` : '<span class="muted">—</span>'},
    {key: "received", label: "Received", width: 88, render: (r) => r.received ? esc(fmtDateTime(r.received).slice(0, 11)) : '<span class="muted">—</span>'},
  ];
  const choices = {products: [...products.columns, ...extraPrices],
    batches: [...batches.columns, ...extraPrices.filter(c => c.key !== "pack_mrp"), ...provenance]};
  const order = {
    products: ["code", "name", "rack", "box", "rack_name", "upp", "purchase_rate", "pack_mrp", "stock", "equivalent", "form", "base_unit", "loose", "reorder", "status", "unit_purchase_rate", "purchase_invoice"],
    batches: ["batch_no", "supplier", "stock", "expiry", "pack_mrp", "purchase_rate", "unit_mrp", "equivalent", "status", "received", "invoice", "unit_purchase_rate", "purchase_invoice"],
  };
  for (const p of Object.keys(choices)) choices[p].sort((a, b) => order[p].indexOf(a.key) - order[p].indexOf(b.key));
  const grids = {products, batches};
  const columnKey = p => `inventory-columns:${BOOT.user?.username}:${p}`;
  function chosenColumns(p) {
    const saved = store.get(columnKey(p), null);
    // a column added after the choice was saved follows its companion (Box follows Rack)
    const picked = choices[p].filter(c => Array.isArray(saved) ? saved.includes(c.key) || (p === 'batches' && c.key === 'supplier')
      || (p === 'products' && c.key === 'box' && saved.includes('rack') && !saved.includes('_box_seen')) : c.default !== false);
    return picked.length ? picked : choices[p].filter(c => c.default !== false);
  }
  for (const p of Object.keys(grids)) grids[p].setColumns(chosenColumns(p));
  async function openColumns() {
    const body = `<p class="muted">Rates come from the latest received purchase invoice for each batch; opening stock uses its recorded batch rate. Product prices show Varies when batches differ.</p><div class="inventory-column-groups">` + Object.keys(grids).map(p => {
      const chosen = chosenColumns(p).map(c => c.key);
      return `<fieldset class="report-columns"><legend>${p === "products" ? "Product columns" : "Batch columns"}</legend>${choices[p].map(c => `<label><input type="checkbox" name="${p}" value="${esc(c.key)}" ${chosen.includes(c.key) ? "checked" : ""}> ${esc(c.label)}</label>`).join("")}<button type="button" class="btn" data-defaults="${p}">Defaults</button></fieldset>`;
    }).join("") + "</div>";
    await modal({title: "Inventory columns", body, wide: true, submitLabel: "Apply", onOpen: form => {
      form.setAttribute("role", "dialog"); form.setAttribute("aria-modal", "true"); form.setAttribute("aria-label", "Inventory columns");
      form.addEventListener("keydown", e => {
        if (e.key !== "Tab") return;
        const controls = $$("input,button", form), i = controls.indexOf(document.activeElement);
        e.preventDefault(); controls[(i + (e.shiftKey ? -1 : 1) + controls.length) % controls.length].focus();
      });
      $$('[data-defaults]', form).forEach(button => button.onclick = () => {
        const p = button.dataset.defaults;
        $$(`input[name="${p}"]`, form).forEach(box => box.checked = choices[p].find(c => c.key === box.value).default !== false);
      });
    }, onSubmit: form => {
      const selected = Object.fromEntries(Object.keys(grids).map(p => [p, new FormData(form).getAll(p)]));
      if (Object.values(selected).some(cols => !cols.length)) throw new Error("Select at least one column in each pane");
      selected.products.push('_box_seen');                    // the person has now decided about Box
      for (const p of Object.keys(grids)) {store.set(columnKey(p), selected[p]); grids[p].setColumns(chosenColumns(p));}
      return true;
    }});
  }
  $(".f-columns", root).onclick = openColumns;
  $(".pane.products", root).append(products.el);
  $(".pane.batches", root).append(batches.el);
  products.el.addEventListener("focus", () => { pane = "products"; });
  batches.el.addEventListener("focus", () => { pane = "batches"; });
  products.el.addEventListener("keydown", (e) => { if (e.key === "ArrowRight" && !e.ctrlKey) { e.preventDefault(); focusPane("batches"); } if (e.key === "Escape") { e.preventDefault(); $(".f-q", root).focus(); } });
  batches.el.addEventListener("keydown", (e) => { if (e.key === "ArrowLeft" || e.key === "Escape") { e.preventDefault(); focusPane("products"); } });

  // split-pane divider (width remembered)
  const split = $(".split", root);
  split.style.gridTemplateColumns = `${store.get("inv:split", 68)}% 6px 1fr`;
  $(".divider", root).addEventListener("mousedown", (e) => {
    e.preventDefault();
    const rect = split.getBoundingClientRect();
    const move = (ev) => {
      const pct = Math.min(85, Math.max(35, ((ev.clientX - rect.left) / rect.width) * 100));
      split.style.gridTemplateColumns = `${pct}% 6px 1fr`;
      store.set("inv:split", Math.round(pct));
    };
    const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });

  if ($('.b-cost', root)) $('.b-cost', root).onclick = verifyBatchCost;
  async function verifyBatchCost() {
    const batch = batches.selected;
    if (!batch || !detail) {ctx.status('Select a batch first', 'warn'); return;}
    const id = detail.id;
    const out = await modal({title: 'Verify purchase cost · ' + batch.batch_no, body: `<p class="muted">Rate is per purchase pack (${batch.upp} stock units). This updates future sales; historical costs require separate verification in Profit & Margin.</p><label>Purchase rate / pack<input name="purchase_rate" type="number" min="0" step="0.01" value="${esc(batch.acquisition_rate ?? '')}"></label><label>Verification reason / invoice reference<input name="reason" required></label><label class="chk"><input type="checkbox" name="confirm_zero"> Acquisition was genuinely free</label>`, submitLabel: 'Save verified cost',
      onSubmit: form => api(`/api/erp/inventory/${id}/batches/${batch.id}/cost`, {method: 'POST', body: {purchase_rate: form.elements.purchase_rate.value, reason: form.elements.reason.value, confirm_zero: form.elements.confirm_zero.checked}})});
    if (out) {details.delete(id); await refresh(id); ctx.status('Batch cost verified; historical sale snapshots retained', 'ok');}
  }
  function focusPane(p) {
    pane = p;
    (p === "batches" ? batches : products).focus();
  }

  // ---------------------------------------------------------------- loading
  let ctrl = null;
  function query(offset) {
    const u = new URLSearchParams({ q: F.q, form: F.form, loose: F.loose, stock: F.stock, expiry: F.expiry, category: F.category || "", supplier: F.supplier || "",
      state: F.state || "", packaging: F.packaging || "", location: F.location || "", sort: F.sort || "", letter: F.letter || "", offset, limit: 200 });
    return "/api/erp/inventory?" + u;
  }
  async function load({ keep = false } = {}) {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    store.set("inv:filters", F);
    try {
      const d = await api(query(0), { signal: ctrl.signal });
      if (!d) return; // A newer search cancelled this request.
      total = d.total;
      products.setRows(d.rows, { keep });
      $(".f-count", root).textContent = `${total} product${total === 1 ? "" : "s"}`;
      renderTotals(d.totals);
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  // footer: every product the filters match (not only the rows loaded), valued batch by batch
  function renderTotals(t) {
    const box = $(".inv-totals", root);
    if (!t) { box.innerHTML = ""; return; }
    const cell = (label, value) => `<div><span>${label}</span><b>${value}</b></div>`;
    box.innerHTML = cell("Total Items", Number(t.items).toLocaleString("en-IN"))
      + cell("Total Quantity", Number(t.quantity).toLocaleString("en-IN"))
      + (t.rate_value !== null ? cell("Total Rate Value", "₹" + money(t.rate_value)) : "")
      + cell("Total MRP Value", "₹" + money(t.mrp_value));
  }
  async function loadMore() {
    if (loading || products.rows.length >= total) return;
    loading = true;
    try { products.appendRows((await api(query(products.rows.length))).rows); } catch (err) { ctx.status(err.message, "error"); }
    loading = false;
  }
  const reload = debounce(() => load(), 120);

  const qIn = $(".f-q", root);
  qIn.addEventListener("input", () => { F.q = qIn.value.trim(); reload(); });
  qIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); reload.flush(); products.focus(); }
    if (e.key === "Escape") { e.preventDefault(); if (qIn.value) { qIn.value = ""; F.q = ""; reload.flush(); } }
  });
  for (const k of FILTERS) $(".f-" + k, root).addEventListener("change", (e) => { F[k] = e.target.value; load(); });
  $(".f-reset", root).onclick = () => {
    Object.assign(F, { q: "", form: "", loose: "", stock: "", expiry: "", category: "", supplier: "", state: "", packaging: "", location: "", sort: "", letter: "" });
    qIn.value = ""; for (const k of FILTERS) $(".f-" + k, root).value = "";
    load(); qIn.focus();
  };

  // ---------------------------------------------------------------- detail
  let dctrl = null;
  const fetchDetail = debounce(async (id) => {
    if (dctrl) dctrl.abort();
    dctrl = new AbortController();
    try {
      const d = await api("/api/erp/inventory/" + id, { signal: dctrl.signal });
      if (!d) return; // Selection moved while this detail request was loading.
      details.set(id, d);
      if (products.selected && products.selected.id === id) applyDetail(d);
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }, 70);
  function showProduct(r) {
    if (!r) { detail = null; batches.setRows([]); renderDetail(); return; }
    const c = details.get(r.id);
    if (c) applyDetail(c); else { $(".b-title", root).textContent = "· loading…"; }
    fetchDetail(r.id);
  }
  function applyDetail(d) {
    detail = d;
    $(".b-title", root).textContent = "· " + d.name;
    batches.setRows(d.batches, { keep: true });
    renderDetail();
  }
  function renderDetail() {
    const d = detail, el = $(".inv-detail", root);
    if (!d) { el.innerHTML = '<span class="muted">Select a product · see the shortcut bar for product actions</span>'; return; }
    const p = d.packaging;
    const fact = (label,value) => `<div><span class="muted">${esc(label)}</span><b>${esc(value)}</b></div>`;
    const allBatches = d.batches || [];
    const life = { DISABLED: ["Disabled — not sold in POS", "right-click → Enable"], DELETED: ["In the recycle bin", "right-click → Restore"] }[d.lifecycle];
    el.innerHTML = `${life ? `<div class="life-off"><b>${life[0]}</b><span>${life[1]}</span></div>` : ""}<div class="inv-report-title"><b>${esc(d.name)}</b><span class="mono muted">${esc(d.code)}</span>
      <span class="muted">${esc([d.category_name,title(d.form),d.manufacturer,d.strength].filter(Boolean).join(' · '))}</span></div>
      <div class="inv-report-facts">${fact('Status', life ? life[0] : 'Active')}${fact('Form',title(d.form) || '—')}${fact('Base unit',title(p.base_unit))}${fact('Manufacturer',d.manufacturer || '—')}
      ${fact('Current stock',`${d.stock} ${unitName(p.base_unit,d.stock)}`)}
      ${fact('Equivalent',d.equivalent || '—')}${fact('Available for sale', life ? `None (${d.lifecycle === 'DELETED' ? 'recycle bin' : 'disabled'})` : `${d.sellable} ${unitName(p.base_unit,d.sellable)}`)}
      ${fact('Packaging',p.pack_label || 'Not confirmed')}${p.content ? fact('Content',p.content) : ''}
      ${fact('Batches',`${allBatches.length} total · ${allBatches.filter(b => b.stock > 0).length} with stock`)}</div>
      ${CAN["rack.view"] ? `<div class="inv-loc"><span class="muted">Location</span><span>${locationHtml(d)}${d.rack_active === false ? ' <span class="tag off">rack disabled</span>' : ''}</span>
        ${CAN["rack.assign"] && !d.deleted ? `<button type="button" class="btn link inv-move">Move… <kbd>${esc(keys.keyFor("inv.moveRack"))}</kbd></button>` : ''}</div>
        <div class="inv-lochist" data-id="${d.id}"></div>` : ''}
      ${d.generic ? `<p class="hint"><b>Composition</b> ${esc(d.generic)}</p>` : ''}`;
    el.querySelector(".inv-move")?.addEventListener("click", () => moveRack([{ ...d, rack_id: d.rack_id, box_id: d.box_id }]));
    if (CAN["rack.view"]) locationHistory(d.id);
  }
  const locCache = new Map();
  async function locationHistory(id) {
    const el = $(`.inv-lochist[data-id="${id}"]`, root);
    if (!el) return;
    try {
      const d = locCache.get(id) || await api(`/api/erp/inventory/${id}/location`);
      locCache.set(id, d);
      if (!el.isConnected) return;
      const hint = !d.current && d.suggested ? `<p class="hint">Suggested: <b class="mono">${esc(d.suggested.label)}</b> (${esc({ CATEGORY: "set for its category", USUAL: "where its category usually is", LAST: "where it was before" }[d.suggested.source] || d.suggested.source)}) — not assigned yet</p>` : "";
      el.innerHTML = hint + (d.history.length ? `<details><summary>Location history (${d.history.length})</summary><table class="rawtab"><tbody>
        ${d.history.map((e) => `<tr><td>${esc(fmtDateTime(e.at))}</td><td>${esc(e.from || "Unassigned")} → <b>${esc(e.to || "Unassigned")}</b></td><td>${esc(e.user)}</td><td class="muted">${esc([e.reason, e.reference].filter(Boolean).join(" · "))}</td></tr>`).join("")}</tbody></table></details>` : "");
    } catch (err) { el.textContent = ""; }
  }
  async function refresh(id) {
    details.delete(id);
    await load({ keep: true });
    const i = products.rows.findIndex((r) => r.id === id);
    if (i >= 0) products.select(i); else fetchDetail(id);
  }

  // ---------------------------------------------------------------- stock adjustment (F5)
  async function adjust() {
    if (!CAN["adjustment.create"]) { ctx.status("You do not have permission to adjust stock", "warn"); return; }
    const d = detail;
    if (!d) { ctx.status("Select a product first", "warn"); return; }
    const p = d.packaging, base = p.base_unit;
    const chosen = pane === "batches" && batches.selected ? batches.selected.id : (d.batches.find((b) => b.stock > 0) || d.batches[0] || {}).id;
    const opts = d.batches.map((b) => `<option value="${b.id}" ${b.id === chosen ? "selected" : ""}>${esc(b.batch_no || "(no batch)")} · ${fmtExp(b.expiry)} · ${b.stock} ${esc(unitName(base, b.stock))}</option>`).join("");
    const body = h(`<div class="form-grid">
      <label class="full">Product<input value="${esc(d.name)}" disabled></label>
      <label>Adjustment<select name="direction"><option value="OUT">Decrease (stock out)</option><option value="IN">Increase (stock in)</option></select></label>
      <label>Batch<select name="batch_id">${opts}<option value="">New batch…</option></select></label>
      <label>Quantity (${esc(unitName(base))})<input name="quantity" autocomplete="off" placeholder="e.g. 3${p.units_per_pack > 1 ? ", 1s = " + p.units_per_pack + ", 2s+3" : ""}" required autofocus></label>
      <label class="loss">Reason type<select name="category">${LOSS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
      <label class="full">Reason<input name="reason" maxlength="300" placeholder="e.g. Physical count difference" required></label>
      <fieldset class="full newb" hidden><legend>New batch</legend>
        <label>Batch no<input name="batch_no" maxlength="60"></label>
        <label>Expiry (MM/YYYY)<input name="expiry" placeholder="02/2030"></label>
        <label>Pack MRP (₹ per ${esc(unitName(p.pack_unit, 1))})<input name="mrp" inputmode="decimal"></label>
        <label>Cost (₹ per ${esc(unitName(p.pack_unit, 1))})<input name="cost" inputmode="decimal"></label>
      </fieldset>
      <p class="full preview"></p></div>`);
    const f = (n) => body.querySelector(`[name="${n}"]`);
    const sync = () => {
      const dirIn = f("direction").value === "IN";
      if (!dirIn && !f("batch_id").value) f("batch_id").value = String(chosen || "");
      body.querySelector(".loss").hidden = dirIn;
      body.querySelector(".newb").hidden = !(dirIn && !f("batch_id").value);
      const b = d.batches.find((x) => String(x.id) === f("batch_id").value);
      const qty = parseQty(f("quantity").value, p.units_per_pack);
      const cur = b ? b.stock : 0;
      const pv = body.querySelector(".preview");
      if (!Number.isFinite(qty) || qty <= 0) { pv.textContent = `Current stock ${cur} ${unitName(base, cur)}${p.units_per_pack > 1 ? " (" + describe(cur, p.units_per_pack, base, p.pack_unit) + ")" : ""}`; return; }
      const res = dirIn ? cur + qty : cur - qty;
      pv.innerHTML = `Current <b>${cur}</b> ${dirIn ? "+" : "−"} <b>${qty}</b> ${esc(unitName(base, qty))} = <b class="${res < 0 ? "neg" : ""}">${res}</b> ${esc(unitName(base, res))}${p.units_per_pack > 1 && res >= 0 ? " (" + esc(describe(res, p.units_per_pack, base, p.pack_unit)) + ")" : ""}`;
    };
    body.addEventListener("input", sync);
    body.addEventListener("change", sync);
    const out = await modal({
      title: "Stock adjustment", body, submitLabel: "Post adjustment", onOpen: sync,
      onSubmit: async (form) => {
        const payload = Object.fromEntries(new FormData(form).entries());
        if (!payload.reason.trim()) throw new Error("A reason is required");
        return api(`/api/erp/inventory/${d.id}/adjust`, { method: "POST", body: payload });
      },
    });
    if (out) { ctx.status(`Stock adjusted: ${out.name} now ${out.stock} ${unitName(out.base_unit, out.stock)}`, "ok"); details.set(out.id, out); await refresh(out.id); }
  }

  // ---------------------------------------------------------------- product editor (Ctrl+E / F3)
  function productForm(d) {
    // a new product has no units yet: blank = detect from the pack and form on save
    const p = d ? d.packaging : { base_unit: "", pack_unit: "", units_per_pack: "", loose_sale: false, content: "", source: "AUTO" };
    const locked = d && d.packaging_locked;
    const sel = (name, list, value) => `<select name="${name}" ${locked ? "disabled" : ""}>${d ? "" : '<option value="" selected>Auto (from pack)</option>'}${list.map((u) => `<option value="${u}" ${u === value ? "selected" : ""}>${u ? title(u) : "—"}</option>`).join("")}</select>`;
    const el = h(`<div class="editor">
      <div class="etabs" role="tablist"><button type="button" data-t="g" class="on">General</button><button type="button" data-t="p">Packaging</button><button type="button" data-t="c">Price &amp; control</button><span class="hint">Alt+1/2/3</span></div>
      <div class="form-grid" data-p="g">
        <label class="full">Product name *<input name="name" value="${esc(d ? d.name : "")}" maxlength="250" required autofocus></label>
        <label>Generic / composition<input name="generic_name" value="${esc(d ? d.generic : "")}"></label>
        <label>Manufacturer<input name="manufacturer" value="${esc(d ? d.manufacturer : "")}"></label>
        <label>Item form (set in Packaging)<input data-form-display value="${esc(d?.form || p.base_unit || '')}" disabled></label>
        <label>Strength<input name="strength" value="${esc(d ? d.strength : "")}" placeholder="650 mg"></label>
        <label>Category<select name="category">${catOptions.filter((c) => c.active || (d && d.category === c.code)).map((c) => `<option value="${esc(c.code)}" ${d && d.category === c.code ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
        <label>Barcode<input name="barcode" value="${esc(d ? d.barcode : "")}"></label>
        <label>HSN<input name="hsn_code" value="${esc(d ? d.hsn : "")}"></label>
      </div>
      <div class="form-grid" data-p="p" hidden>
        ${d && d.packaging_convertible ? `<p class="full warn-box">Stock is counted per pack now (${d.stock} ${esc(unitName(p.pack_unit, d.stock))}). Setting units per purchase unit to N repacks it through the ledger: ${d.stock} × N. The strip MRP stays on each batch, so one unit = strip MRP ÷ N.</p>` : ""}
        ${locked ? `<p class="full warn-box">This product holds ${d.stock} ${esc(unitName(p.base_unit, d.stock))}. Stock unit and pack size can change only at zero stock, so existing stock is never reinterpreted. The loose-sale flag can still change.</p>` : ""}
        <input type="hidden" name="pack_size" value="${esc(d ? d.pack_raw : '')}">
        <div class="full" data-simple-packaging></div>
        ${d ? '<p class="full hint" data-inventory-equivalent aria-live="polite"></p>' : ''}
        <label>Loose sale<select name="loose_sale"><option value="auto">Automatic (from pack / form)</option><option value="true" ${d && p.loose_sale ? "selected" : ""}>Yes</option><option value="false" ${d && !p.loose_sale ? "selected" : ""}>No</option></select></label>
        ${p.content ? `<label>Content<input value="${esc(p.content)}" disabled></label>` : ""}
        <p class="full hint">Choose the physical form and units per strip/container. Existing supplier pack text is retained as source evidence.</p>
      </div>
      <div class="form-grid" data-p="c" hidden>
        <label>Default MRP (₹ per purchase unit)<input name="mrp" inputmode="decimal" value="${esc(d ? d.mrp : "")}"></label>
        <label>Reorder level (stock units)<input name="reorder_level" type="number" min="0" value="${d ? d.reorder : ""}"></label>
        <label>Rack<input name="rack" maxlength="20" value="${esc(d ? d.rack : "")}"></label>
        <p class="full hint">Batch MRP comes from each purchase/batch; this default only pre-fills new batches.</p>
      </div></div>`);
    const tabsEl = el.querySelector(".etabs");
    const show = (t) => {
      tabsEl.querySelectorAll("[data-t]").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
      el.querySelectorAll("[data-p]").forEach((x) => { x.hidden = x.dataset.p !== t; });
      const first = el.querySelector(`[data-p="${t}"] input:not([disabled]),[data-p="${t}"] select:not([disabled])`);
      if (first) first.focus();
    };
    tabsEl.addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (b) show(b.dataset.t); });
    el.addEventListener("keydown", (e) => { if (e.altKey && ["1", "2", "3"].includes(e.key)) { e.preventDefault(); show(["g", "p", "c"][Number(e.key) - 1]); } });
    const editor = physicalEditor({ form: d?.form || '', base: p.base_unit, count: p.units_per_pack || 1, quantities: false, locked });
    el.querySelector('[data-simple-packaging]').append(editor);
    el.simplePackaging = editor;
    el.newProduct = !d;
    editor.addEventListener('packaging-preview', () => {
      const v = editor.packagingValues();
      el.querySelector('[data-form-display]').value = title(v.dosage_form || v.base_unit);
    });
    if (d) {
      const preview = () => {
        const v = editor.packagingValues(), n = Number(v.units_per_pack), total = d.packaging_convertible ? toBase(d.stock, n) : d.stock;
        el.querySelector('[data-inventory-equivalent]').textContent = `Stock equivalent after saving: ${total} ${unitName(v.base_unit, total)}`;
      };
      editor.addEventListener('packaging-preview', preview);
      preview();
    }
    return el;
  }
  const collect = (form) => {
    const out = {};
    form.querySelectorAll("[name]").forEach((i) => { if (!i.disabled) out[i.name] = i.value; });
    const container = form.querySelector('.editor'), editor = container?.simplePackaging;
    if (editor && (container.newProduct || editor.packagingDirty) && !editor.querySelector('[name="physical_form"]').disabled) {
      const values = editor.packagingValues();
      for (const k of ['base_unit','pack_unit','units_per_pack','dosage_form']) out[k] = values[k];
      if (!out.pack_size && ['TABLET','CAPSULE'].includes(values.base_unit)) out.pack_size = `${values.units_per_pack}S`;
    }
    for (const k of Object.keys(out)) if (k.startsWith('physical_')) delete out[k];
    return out;
  };
  /** The marked products when the right-clicked one is among them (or nothing is marked: just that one). */
  const picked = (r) => {
    const marked = products.markedRows;
    return marked.length && (!r || marked.some((m) => m.id === r.id)) ? marked : r ? [r] : [];
  };
  /** Only the actions that change something, each saying exactly how many products it affects. */
  function statusItems(rows) {
    if (!rows.length) return [];
    const one = rows.length === 1;
    const binned = rows.filter((x) => x.deleted), live = rows.filter((x) => !x.deleted);
    const on = live.filter((x) => x.active), off = live.filter((x) => !x.active);
    const eligible = live.filter((x) => (x.stock || 0) <= 0);
    const count = (list, word) => (one ? "" : ` ${list.length} ${word}`);
    const items = [];
    if (binned.length) items.push({ label: `Restore${count(binned, "")} from recycle bin`.replace("  ", " "), action: () => setStatus(binned, "restore") });
    if (off.length) items.push({ label: `Enable${count(off, "disabled")}`, action: () => setStatus(off, "enable") });
    if (on.length) items.push({ label: `Disable${count(on, "enabled")} (not sold in POS)`, action: () => setStatus(on, "disable") });
    if (eligible.length) items.push({ label: one ? "Move to recycle bin (archive)" : `Archive ${eligible.length} with no stock (recycle bin)`, action: () => setStatus(eligible, "delete") });
    return items;
  }
  /** The bar above the grid while products are marked: what can be done to exactly these products. */
  function renderBulk(marked) {
    const bar = $(".bulkbar", root);
    if (!marked.length) { bar.hidden = true; bar.innerHTML = ""; return; }
    const acts = [];
    if (CAN["rack.assign"] && CAN["rack.bulk_move"] && marked.some((x) => !x.deleted)) acts.push({ label: `Move ${marked.length} to rack…`, key: keys.keyFor("inv.moveRack"), run: () => moveRack(marked) });
    if (CAN["inventory.edit"]) acts.push({ label: "Change category…", key: keys.keyFor("inv.category"), run: () => changeCategory(marked) });
    if (CAN["inventory.delete"]) acts.push(...statusItems(marked).map((x) => ({ label: x.label, run: x.action })));
    bar.innerHTML = `<b>${marked.length} product${marked.length === 1 ? "" : "s"} selected</b>`
      + acts.map((a, i) => `<button type="button" class="btn" data-i="${i}">${esc(a.label)}${a.key ? ` <kbd>${esc(a.key)}</kbd>` : ""}</button>`).join("")
      + '<button type="button" class="btn link" data-clear>Clear selection <kbd>Esc</kbd></button>';
    bar.hidden = false;
    bar.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.hasAttribute("data-clear")) { products.clearMarks(); products.focus(); return; }
      acts[Number(b.dataset.i)].run();
    };
  }
  async function moveRack(rows) {
    rows = rows.filter((x) => !x.deleted);
    if (!rows.length) { ctx.status("Select products first", "warn"); return; }
    const out = await moveProducts(ctx, rows);
    if (out) { rows.forEach((x) => { details.delete(x.id); locCache.delete(x.id); }); if (rows.length > 1) products.clearMarks(); await load({ keep: true }); if (detail) fetchDetail(detail.id); }
    products.focus();
  }
  /** Disable / enable / recycle bin / restore — nothing is erased; history keeps pointing at the products. */
  async function setStatus(rows, action) {
    const who = rows.length > 1 ? `${rows.length} products` : rows[0].name;
    const say = { disable: `Disable ${who}? They stay in stock, in their rack and in history, but cannot be sold in POS until enabled.`,
      delete: `Archive ${who} to the recycle bin? Nothing is erased: sales, purchases, the stock ledger and location history keep pointing at them, and Show → Recycle bin restores them. Only products with no stock can be archived.` }[action];
    if (say && !(await window.erpConfirm(say))) return;
    try {
      const out = await api("/api/erp/inventory/bulk/status", { method: "POST", body: { action, ids: rows.map((x) => x.id) } });
      const verb = { disable: "disabled", enable: "enabled", delete: "moved to the recycle bin", restore: "restored" }[action];
      ctx.status(`${out.done.length} ${verb}` + (out.unchanged?.length ? ` · ${out.unchanged.length} already so` : "")
        + (out.refused.length ? ` · ${out.refused.length} not changed: ${out.refused[0].reason}` : ""), out.refused.length ? "warn" : "ok");
      rows.forEach((x) => details.delete(x.id));
      products.clearMarks();
      await load({ keep: true });
    } catch (err) { ctx.status(err.message, "error"); }
  }
  /** Pick a category from the live drop-down (or create one) and set it on every selected product. */
  async function changeCategory(rows) {
    if (!rows.length) return;
    const now = [...new Set(rows.map((x) => x.category))];
    const out = await pickCategory({ current: now.length === 1 ? now[0] : "",
      intro: `${rows.length} product(s). Only the category changes — stock, prices and packaging stay as they are.`,
      apply: (code) => api("/api/erp/inventory/bulk/category", { method: "POST", body: { ids: rows.map((x) => x.id), category: code } }) });
    if (out) {
      ctx.status(`${out.name} set on ${out.result.changed} product(s)`, "ok");
      rows.forEach((x) => details.delete(x.id)); products.clearMarks();
      await loadChoices(); await load({ keep: true });
    }
    products.focus();
  }
  async function edit(tab = "") {
    if (!CAN["inventory.edit"]) { ctx.status("You do not have permission to edit products", "warn"); return; }
    if (!detail) { ctx.status("Select a product first", "warn"); return; }
    const d = detail;
    const out = await modal({
      title: "Edit product — " + d.name, body: productForm(d), wide: true,
      onOpen: (form) => { if (tab) form.querySelector(`[data-t="${tab}"]`)?.click(); },
      onSubmit: (form) => api(`/api/erp/inventory/${d.id}`, { method: "PUT", body: collect(form) }),
    });
    if (out) { ctx.status(`Saved ${out.name}`, "ok"); details.set(out.id, out); await refresh(out.id); }
  }
  async function newProduct() {
    if (!CAN["inventory.create"]) { ctx.status("You do not have permission to add products", "warn"); return; }
    const out = await modal({
      title: "New product", body: productForm(null), wide: true, submitLabel: "Create",
      onSubmit: (form) => api("/api/erp/inventory", { method: "POST", body: collect(form) }),
    });
    if (out) {
      ctx.status(`Created ${out.name} (${out.code}). Stock adjustment → Increase → New batch adds its opening stock.`, "ok");
      Object.assign(F, { q: out.code, form: "", loose: "", stock: "", expiry: "" });
      qIn.value = out.code;
      await load();
      products.focus();
    }
  }
  function ledger(batchOnly = false) {
    if (!detail) { ctx.status("Select a product first", "warn"); return; }
    const b = batchOnly || pane === "batches" ? batches.selected : null;
    ctx.open("ledger", b ? { item: detail.id, batch: b.id } : { item: detail.id });
  }
  async function importSheet() {
    if (!CAN["inventory.create"]) { ctx.status("Importing stock needs the product create right", "warn"); return; }
    const cats = catOptions.filter((c) => c.active !== false);
    const out = await modal({
      title: "Import opening stock (sheet)", wide: true, submitLabel: "Import",
      body: `<div class="form-grid">
        <label class="full">CSV or Excel file<input type="file" name="file" accept=".csv,.tsv,.txt,.xls,.xlsx,.xlsm" required autofocus></label>
        <label class="full">Category for rows without one<select name="default_category">${cats.map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("")}</select></label>
        <p class="full hint">Rows match existing products by barcode, then exact name; others become new products with the row's packaging.
          Qty counts packs (strips, bottles) and Loose Qty extra units. Stock posts as opening-stock ledger movements.
          <a href="/api/erp/inventory/import/template.csv">Download template</a></p>
        <div class="full import-result"></div></div>`,
      onOpen: (form) => {
        form.file.addEventListener("change", () => { if (form.file.files.length) form.querySelector("[type=submit]").focus(); });
      },
      onSubmit: async (form) => {
        if (!form.file.files.length) throw new Error("Choose the stock sheet (Space opens the file chooser)");
        const res = await fetch("/api/erp/inventory/import", { method: "POST", body: new FormData(form), credentials: "same-origin" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error((data && data.detail) || `Import failed (${res.status})`);
        return data;
      },
    });
    if (!out) return;
    ctx.status(`Imported ${out.rows} rows · ${out.created} new · ${out.updated} updated · ${out.batches} batches${out.errors.length ? ` · ${out.errors.length} row(s) with problems` : ""}`,
      out.errors.length ? "warn" : "ok");
    if (out.errors.length) {
      await modal({ title: "Rows that were not imported", wide: true, submitLabel: "Close",
        body: `<ul class="issues">${out.errors.slice(0, 200).map((e) => `<li class="bad">${esc(e)}</li>`).join("")}</ul>`, onSubmit: () => true });
    }
    details.clear(); load();
  }
  function history() {
    if (!detail) { ctx.status("Select a product first", "warn"); return; }
    const b = pane === "batches" ? batches.selected : null;
    ctx.open("history", b ? { item: detail.id, batch: b.id } : { item: detail.id });
  }

  const KEYS = {
    "inv.search": () => { qIn.focus(); qIn.select(); },
    "inv.columns": openColumns,
    "inv.new": newProduct,
    "inv.adjust": adjust,
    "inv.ledger": () => ledger(),
    "inv.history": history,
    "inv.edit": edit,
    "inv.moveRack": () => { const rows = products.markedRows.length ? products.markedRows : products.selected ? [products.selected] : []; if (CAN["rack.assign"]) moveRack(rows); },
    "inv.category": () => { const rows = products.markedRows.length ? products.markedRows : products.selected ? [products.selected] : []; if (CAN["inventory.edit"] && rows.length) changeCategory(rows); },
    "inv.export": () => window.open("/inventory/export/xlsx?" + new URLSearchParams({ q: F.q }), "_blank"),
  };
  load().then(() => { if (!products.rows.length) ctx.status("No products yet — use New product or the command palette → Import opening stock"); });
  return {
    get keys() { return keys.bar("inventory"); },
    onKey(e, name) { const fn = KEYS[keys.lookup("inventory", name)]; if (!fn) return false; fn(); return true; },
    onShow({ focus }) {
      loadChoices();   // categories may have been changed in Categories & Forms meanwhile
      if (detail) { details.delete(detail.id); load({ keep: true }); }
      if (focus) setTimeout(() => qIn.focus(), 0);
    },
    adjust, newProduct, importSheet,
    onDataChanged(areas) {
      if (areas.includes("masters")) loadChoices();
      if (areas.some((a) => ["inventory", "locations", "purchases", "masters"].includes(a))) {
        details.clear(); locCache.clear(); load({ keep: true });
        if (detail) fetchDetail(detail.id);
      }
    },
    navigate(p) {
      if (p && "location" in p) { F.location = p.location || ""; $(".f-location", root).value = F.location; load(); }
    },
  };
}
