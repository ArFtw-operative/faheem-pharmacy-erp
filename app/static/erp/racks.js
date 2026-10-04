// Racks — where products are kept. Left: every rack with its figures; right: the chosen
// rack (Products · Boxes · History · Snapshots · Settings). Moving products here and in
// Inventory uses the same server operation (one request, validated, recorded in history).
import { $, $$, BOOT, api, esc, fmtDateTime, fmtExpShort, h, modal, store } from "erp/core";
import * as keys from "erp/keys";
import { Grid } from "erp/grid";
import { moveProducts, pickLocation, resultText, showFailures } from "erp/locations";

const TABS = ["products", "boxes", "history", "snapshots", "settings"];
const TAB_LABEL = { products: "Products", boxes: "Boxes", history: "Movement history", snapshots: "Snapshots", settings: "Settings" };
const n = (v) => Number(v || 0).toLocaleString("en-IN");
const dayLabel = (d) => new Date(d + "T00:00:00").toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  ctx.setTitle("Racks");
  let racks = [], cfg = {}, metrics = {}, rack = null, tab = "products", box = "", q = "", status = "";
  root.innerHTML = `<div class="racks">
    <div class="rk-metrics"></div>
    <div class="filters">
      <label>Search<input class="rk-q" placeholder="Rack code or name" autocomplete="off"></label>
      <label>Status<select class="rk-status"><option value="">All</option><option value="active">Active</option><option value="inactive">Disabled</option></select></label>
      <span class="spacer"></span>
      ${CAN["rack.create"] ? '<button type="button" class="btn primary rk-new">New rack <kbd>F3</kbd></button>' : ""}
    </div>
    <div class="split rk-split"><div class="pane rk-left"><div class="pane-h">RACKS</div></div><div class="divider" title="Drag to resize · double-click resets"></div>
      <div class="pane rk-right"><div class="rk-head"></div>
        <div class="mtabs rk-tabs" role="tablist">${TABS.map((t) => `<button type="button" role="tab" data-tab="${t}">${TAB_LABEL[t]}</button>`).join("")}<span class="hint">F6 next tab</span></div>
        <section class="rk-body"></section></div></div></div>`;
  const body = $(".rk-body", root);

  // split-pane divider: drag to resize the rack detail, width remembered (a CSS variable, so a
  // narrow screen still stacks the panes)
  const split = $(".rk-split", root), divider = $(".rk-split > .divider", root);
  const setSplit = (pct) => split.style.setProperty("--rk-left", `${pct}%`);
  setSplit(store.get("racks:split", 38));
  divider.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const rect = split.getBoundingClientRect();
    document.body.classList.add("resizing");
    const move = (ev) => {
      const pct = Math.round(Math.min(70, Math.max(20, ((ev.clientX - rect.left) / rect.width) * 100)));
      setSplit(pct); store.set("racks:split", pct);
    };
    const up = () => { document.body.classList.remove("resizing"); document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  divider.addEventListener("dblclick", () => { store.set("racks:split", 38); setSplit(38); });

  // ---------------------------------------------------------------- rack list
  const list = new Grid({
    label: "Racks", storageKey: "racks-list", empty: CAN["rack.create"] ? "No racks yet. New rack (F3) creates the first one." : "No racks yet.",
    columns: [
      { key: "code", label: "Rack", width: 90, render: (r) => `<b class="mono">${esc(r.code)}</b>` },
      { key: "name", label: "Name", width: 170, render: (r) => esc(r.name || "") + (r.active ? "" : ' <span class="tag off">disabled</span>') },
      { key: "boxes", label: "Boxes", width: 56, align: "num", render: (r) => (r.boxes ? n(r.boxes) : '<span class="muted">—</span>') },
      { key: "products", label: "Products", width: 72, align: "num", render: (r) => n(r.products) },
      { key: "units", label: "Units", width: 76, align: "num", render: (r) => n(r.units) },
      { key: "expiring", label: "Expiring", width: 70, align: "num", render: (r) => (r.expiring ? `<span class="warn-t">${n(r.expiring)}</span>` : '<span class="muted">0</span>') },
      { key: "out_of_stock", label: "Out", width: 50, align: "num", title: "Products with no stock", render: (r) => n(r.out_of_stock) },
    ],
    rowClass: (r) => (r.active ? "" : "item-off"),
    onSelect: (r) => openRack(r),
    onActivate: () => focusBody(),
    contextMenu: (r) => [
      { label: "Add products…", key: keys.keyFor("racks.addProducts"), action: () => addProducts() },
      ...(CAN["rack.edit"] ? [{ label: "Edit rack…", key: keys.keyFor("racks.edit"), action: () => editRack() }] : []),
      ...(CAN["rack.disable"] ? [r && r.active ? { label: "Disable rack…", action: () => setRackStatus(false) } : { label: "Enable rack", action: () => setRackStatus(true) }] : []),
    ],
  });
  $(".rk-left", root).append(list.el);

  async function loadRacks(keepId = rack && rack.id) {
    try {
      const d = await api("/api/erp/racks?" + new URLSearchParams({ q, status }));
      racks = d.racks; cfg = d.config; metrics = d.metrics;
      renderMetrics();
      list.setRows(racks, { keep: true });
      const i = racks.findIndex((r) => r.id === keepId);
      if (i >= 0) list.select(i); else if (racks.length) list.select(0); else { rack = null; renderRack(); }
    } catch (err) { ctx.status(err.message, "error"); }
  }

  function renderMetrics() {
    const m = metrics;
    const item = (label, value, act, tip) => `<button type="button" class="rk-metric" data-act="${act}" title="${esc(tip)}"><b>${n(value)}</b><span>${label}</span></button>`;
    $(".rk-metrics", root).innerHTML = item("Racks", m.racks, "all", "All racks")
      + item("Active racks", m.active_racks, "active", "Show active racks")
      + item("Unassigned products", m.unassigned, "unassigned", "Open Inventory: products with no rack")
      + (m.boxes_enabled ? item("Without a box", m.without_box, "nobox", "Open Inventory: in a rack, no box") : "")
      + item("Moved today", m.moved_today, "today", "Products whose location changed today");
  }
  $(".rk-metrics", root).addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "unassigned" || act === "nobox") ctx.open("inventory", { location: act });
    else if (act === "active" || act === "all") { status = act === "active" ? "active" : ""; $(".rk-status", root).value = status; loadRacks(); }
    else if (act === "today" && rack) { showTab("history"); }
  });

  // ---------------------------------------------------------------- one rack
  async function openRack(r) {
    if (!r) return;
    try { rack = await api(`/api/erp/racks/${r.id}`); cfg = rack.config; } catch (err) { ctx.status(err.message, "error"); return; }
    box = "";
    renderRack();
  }
  function renderRack() {
    const head = $(".rk-head", root);
    if (!rack) { head.innerHTML = '<p class="muted es-pad">Choose a rack.</p>'; body.innerHTML = ""; return; }
    const r = rack;
    head.innerHTML = `<div class="rk-title"><b class="mono">${esc(r.code)}</b><span>${esc(r.name || "")}</span>
        <span class="tag ${r.active ? "ok" : "off"}">${r.active ? "Active" : "Disabled"}</span>
        ${r.categories.length ? `<span class="muted">Preferred for ${r.categories.map((c) => esc(((BOOT.category_options || []).find((x) => x.code === c) || {}).name || c)).join(", ")}</span>` : ""}</div>
      <div class="rk-stats">${[["Products", r.products], ["Batches", r.batches], ["Units", r.units], ["Expiring soon", r.expiring], ["Out of stock", r.out_of_stock],
        ...(cfg.location_boxes_enabled ? [["Boxes", r.boxes]] : [])].map(([l, v]) => `<span><b>${n(v)}</b> ${l}</span>`).join("")}
        ${CAN["purchase.view"] ? `<span><b>₹${Number(r.mrp_value || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</b> at MRP</span>` : ""}</div>`;
    $$(".rk-tabs [data-tab]", root).forEach((b) => {
      b.hidden = (b.dataset.tab === "boxes" && !cfg.location_boxes_enabled) || (b.dataset.tab === "history" && !CAN["rack.history.view"])
        || (b.dataset.tab === "snapshots" && !CAN["rack.snapshot.view"]);
    });
    if ($(`.rk-tabs [data-tab="${tab}"]`, root).hidden) tab = "products";
    showTab(tab, false);
  }
  function showTab(t, focus = true) {
    tab = t;
    $$(".rk-tabs [data-tab]", root).forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    if (!rack) return;
    ({ products: productsTab, boxes: boxesTab, history: historyTab, snapshots: snapshotsTab, settings: settingsTab })[t](focus);
  }
  $(".rk-tabs", root).addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });
  const nextTab = () => {
    const visible = TABS.filter((t) => !$(`.rk-tabs [data-tab="${t}"]`, root).hidden);
    showTab(visible[(visible.indexOf(tab) + 1) % visible.length]);
  };
  let bodyGrid = null;
  const focusBody = () => (bodyGrid ? bodyGrid.focus() : body.querySelector("input,select,button")?.focus());

  // ---------------------------------------------------------------- Products tab
  let prodGrid = null;
  async function productsTab(focus) {
    body.innerHTML = `<div class="filters rk-sub">
        ${cfg.location_boxes_enabled && rack.box_list.length ? `<label>Box<select class="rp-box"><option value="">All boxes</option><option value="none">No box</option>
          ${rack.box_list.map((b) => `<option value="${b.id}">${esc(b.code)}${b.name ? " · " + esc(b.name) : ""}</option>`).join("")}</select></label>` : ""}
        <label>Find<input class="rp-q" placeholder="Product in this rack" autocomplete="off"></label>
        <span class="spacer"></span><span class="rp-count muted"></span>
        ${CAN["rack.assign"] ? `<button type="button" class="btn primary rp-add">Add products <kbd>F4</kbd></button>
          <button type="button" class="btn rp-move">Move selected <kbd>F8</kbd></button>` : ""}</div><div class="rk-grid"></div>`;
    prodGrid = bodyGrid = new Grid({
      label: "Products in rack", storageKey: "rack-products", empty: "No products in this rack. Add products (F4) or move them here from Inventory (F8).",
      multi: true,
      columns: [
        { key: "code", label: "Code", width: 86, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
        { key: "name", label: "Product", width: 250, render: (r) => `<b>${esc(r.name)}</b>${r.active ? "" : ' <span class="tag off">disabled</span>'}` },
        ...(cfg.location_boxes_enabled ? [{ key: "box", label: "Box", width: 60, render: (r) => (r.box ? `<b class="mono">${esc(r.box)}</b>` : '<span class="muted">—</span>') }] : []),
        { key: "category", label: "Category", width: 90, render: (r) => esc(((BOOT.category_options || []).find((c) => c.code === r.category) || {}).name || r.category) },
        { key: "stock", label: "Stock", width: 110, align: "num", render: (r) => `<b>${n(r.stock)}</b> <small>${esc(r.stock_label)}</small>` },
        { key: "batches", label: "Batches", width: 60, align: "num" },
        { key: "first_expiry", label: "First expiry", width: 84, render: (r) => (r.first_expiry ? fmtExpShort(r.first_expiry) : '<span class="muted">—</span>') },
        { key: "since", label: "Here since", width: 100, render: (r) => esc(fmtDateTime(r.since).slice(0, 11)) },
      ],
      rowClass: (r) => (r.active ? (r.stock <= 0 ? "dim" : "") : "item-off"),
      onMarks: (m) => { $(".rp-count", body).textContent = m.length ? `${m.length} selected` : ""; },
      contextMenu: () => CAN["rack.assign"] ? [
        { label: `Move ${sel().length > 1 ? sel().length + " products" : ""} to another rack / box…`.replace("  ", " "), key: keys.keyFor("racks.move"), action: () => moveSelected() },
        { label: `Remove from this rack (Unassigned)`, action: () => unassignSelected() },
      ] : [],
    });
    $(".rk-grid", body).append(prodGrid.el);
    const reload = async () => {
      try {
        const d = await api(`/api/erp/racks/${rack.id}/products?` + new URLSearchParams({ box, q: $(".rp-q", body).value }));
        prodGrid.setRows(d.rows.map((r) => ({ ...r, rack_id: rack.id, rack: rack.code })), { keep: true });
        $(".rp-count", body).textContent = `${d.rows.length} product${d.rows.length === 1 ? "" : "s"}`;
      } catch (err) { ctx.status(err.message, "error"); }
    };
    $(".rp-box", body)?.addEventListener("change", (e) => { box = e.target.value; reload(); });
    let tmr;
    $(".rp-q", body).addEventListener("input", () => { clearTimeout(tmr); tmr = setTimeout(reload, 150); });
    $(".rp-add", body)?.addEventListener("click", addProducts);
    $(".rp-move", body)?.addEventListener("click", moveSelected);
    if ($(".rp-box", body)) $(".rp-box", body).value = box;
    await reload();
    if (focus) prodGrid.focus();
  }
  const sel = () => (prodGrid ? (prodGrid.markedRows.length ? prodGrid.markedRows : prodGrid.selected ? [prodGrid.selected] : []) : []);
  async function moveSelected() {
    if (tab !== "products") { showTab("products"); return; }
    const out = await moveProducts(ctx, sel());
    if (out) { prodGrid.clearMarks(); await refreshAll(); }
  }
  async function unassignSelected() {
    const rows = sel();
    if (!rows.length) return;
    if (!(await window.erpConfirm(`Remove ${rows.length === 1 ? rows[0].name : rows.length + " products"} from ${rack.code}? They become Unassigned (history keeps where they were).`))) return;
    try {
      const out = await api("/api/erp/locations/assign", { method: "POST", body: { item_ids: rows.map((r) => r.id), rack_id: null,
        expected: Object.fromEntries(rows.map((r) => [r.id, { rack_id: rack.id, box_id: r.box_id }])) } });
      ctx.status(resultText(out), out.failed.length ? "warn" : "ok");
      if (out.failed.length) await showFailures(out);
      prodGrid.clearMarks(); await refreshAll();
    } catch (err) { ctx.status(err.message, "error"); }
  }

  /** Add products: search / filter by category, mark many, assign them to this rack (same server operation as Inventory). */
  async function addProducts() {
    if (!rack || !CAN["rack.assign"]) return;
    if (!rack.active) { ctx.status(`${rack.code} is disabled — enable it first`, "warn"); return; }
    const settings = await api("/api/erp/locations/settings");
    const el = h(`<div class="rk-add">
      <div class="filters"><label>Find<input class="ra-q" placeholder="Name, code, generic" autocomplete="off"></label>
        <label>Category<select class="ra-cat"><option value="">All</option>${settings.categories.map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("")}</select></label>
        <label>Show<select class="ra-loc"><option value="unassigned">Unassigned only</option><option value="">All products</option></select></label>
        ${cfg.location_boxes_enabled && rack.box_list.some((b) => b.active) ? `<label>Into box<select class="ra-box"><option value="">— no box —</option>${rack.box_list.filter((b) => b.active).map((b) => `<option value="${b.id}">${esc(b.code)}${b.name ? " · " + esc(b.name) : ""}</option>`).join("")}</select></label>` : ""}
        <span class="spacer"></span><span class="ra-count muted"></span></div>
      <p class="hint">Space or Ctrl+click marks · Shift+↑↓ marks a range · Ctrl+A marks all shown · Enter assigns the marked products.</p>
      <div class="ra-grid"></div></div>`);
    const pick = new Grid({
      label: "Products to add", storageKey: "rack-add", multi: true, empty: "No products match.",
      columns: [
        { key: "code", label: "Code", width: 86, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
        { key: "name", label: "Product", width: 260, render: (r) => `<b>${esc(r.name)}</b>` },
        { key: "category_name", label: "Category", width: 100, render: (r) => esc(r.category_name || r.category) },
        { key: "stock", label: "Stock", width: 70, align: "num" },
        { key: "location", label: "Now in", width: 100, render: (r) => (r.location ? `<span class="mono">${esc(r.location)}</span>` : '<span class="muted">Unassigned</span>') },
      ],
      onMarks: (m) => { submitLabel(m.length || (pick.selected ? 1 : 0)); },
      onSelect: () => { if (!pick.markedRows.length) submitLabel(pick.selected ? 1 : 0); },
    });
    el.querySelector(".ra-grid").append(pick.el);
    const submitLabel = (k) => { const b = el.closest("form")?.querySelector("[type=submit]"); if (b) b.firstChild.textContent = `Assign ${k || 0} product${k === 1 ? "" : "s"} to ${rack.code} `; };
    const load = async () => {
      const u = new URLSearchParams({ q: el.querySelector(".ra-q").value, category: el.querySelector(".ra-cat").value, location: el.querySelector(".ra-loc").value, limit: 500 });
      try {
        const d = await api("/api/erp/inventory?" + u);
        pick.setRows(d.rows.filter((r) => r.rack_id !== rack.id));
        el.querySelector(".ra-count").textContent = `${d.total} shown${d.total > 500 ? " (first 500 — narrow the search)" : ""}`;
      } catch (err) { ctx.status(err.message, "error"); }
    };
    let tmr;
    el.querySelector(".ra-q").addEventListener("input", () => { clearTimeout(tmr); tmr = setTimeout(load, 150); });
    el.querySelectorAll("select.ra-cat, select.ra-loc").forEach((s) => s.addEventListener("change", load));
    el.querySelector(".ra-q").addEventListener("keydown", (e) => { if (e.key === "ArrowDown") { e.preventDefault(); pick.focus(); } });
    pick.el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); el.closest("form").requestSubmit(); } });
    const out = await modal({ title: `Add products to ${rack.code}${rack.name ? " — " + rack.name : ""}`, body: el, wide: true, submitLabel: `Assign 0 products to ${rack.code}`,
      onOpen: () => { load(); el.querySelector(".ra-q").focus(); },
      onSubmit: () => {
        const rows = pick.markedRows.length ? pick.markedRows : pick.selected ? [pick.selected] : [];
        if (!rows.length) throw new Error("Choose products first (↓ into the list; Space, Ctrl+click or Ctrl+A marks several)");
        return api("/api/erp/locations/assign", { method: "POST", body: { item_ids: rows.map((r) => r.id), rack_id: rack.id,
          box_id: el.querySelector(".ra-box")?.value || null,
          expected: Object.fromEntries(rows.map((r) => [r.id, r.rack_id ? { rack_id: r.rack_id, box_id: r.box_id } : null])) } });
      } });
    if (out) {
      ctx.status(resultText(out), out.failed.length ? "warn" : "ok");
      if (out.failed.length) await showFailures(out);
      await refreshAll();
    }
  }

  // ---------------------------------------------------------------- Boxes tab
  async function boxesTab(focus) {
    body.innerHTML = `<div class="filters rk-sub"><b>Boxes in ${esc(rack.code)}</b><span class="hint">Enter: show its products · codes are unique within this rack</span>
      <span class="spacer"></span>${CAN["box.manage"] ? '<button type="button" class="btn primary rb-new">New box <kbd>Alt+B</kbd></button>' : ""}</div><div class="rk-grid"></div>`;
    const g = bodyGrid = new Grid({
      label: "Boxes", storageKey: "rack-boxes", empty: "No boxes in this rack." + (CAN["box.manage"] ? " New box (Alt+B) adds one." : ""),
      columns: [
        { key: "code", label: "Box", width: 70, render: (r) => `<b class="mono">${esc(r.code)}</b>` },
        { key: "name", label: "Name", width: 180, render: (r) => esc(r.name || "") + (r.active ? "" : ' <span class="tag off">disabled</span>') },
        { key: "products", label: "Products", width: 72, align: "num" },
        { key: "units", label: "Units", width: 72, align: "num", render: (r) => n(r.units) },
        { key: "expiring", label: "Expiring", width: 70, align: "num" },
      ],
      rowClass: (r) => (r.active ? "" : "item-off"),
      onActivate: (r) => { box = String(r.id); showTab("products"); },
      contextMenu: (r) => [
        { label: "Show its products", action: () => { box = String(r.id); showTab("products"); } },
        ...(CAN["box.manage"] ? [{ label: "Rename…", action: () => editBox(r) },
          r.active ? { label: "Disable…", action: () => disableBox(r) } : { label: "Enable", action: () => boxStatus(r, { active: true }) }] : []),
      ],
    });
    $(".rk-grid", body).append(g.el);
    g.setRows(rack.box_list);
    $(".rb-new", body)?.addEventListener("click", newBox);
    if (focus) g.focus();
  }
  async function newBox() {
    if (!rack || !CAN["box.manage"]) return;
    const out = await modal({ title: `New box in ${rack.code}`, submitLabel: "Create box",
      body: `<div class="form-grid"><label>Box code *<input name="code" required maxlength="30" autofocus placeholder="B01"></label>
        <label>Name<input name="name" maxlength="80" placeholder="Upper left"></label><label class="full">Description<input name="description" maxlength="200"></label></div>`,
      onSubmit: (f) => api(`/api/erp/racks/${rack.id}/boxes`, { method: "POST", body: { code: f.elements.code.value, name: f.elements.name.value, description: f.elements.description.value } }) });
    if (out) { ctx.status("Box created", "ok"); await refreshAll(); showTab("boxes"); }
  }
  async function editBox(b) {
    const out = await modal({ title: `Box ${rack.code} / ${b.code}`, submitLabel: "Save",
      body: `<div class="form-grid"><label>Box code *<input name="code" required maxlength="30" value="${esc(b.code)}"></label>
        <label>Name<input name="name" maxlength="80" value="${esc(b.name || "")}"></label><label class="full">Description<input name="description" maxlength="200" value="${esc(b.description || "")}"></label></div>`,
      onSubmit: (f) => api(`/api/erp/boxes/${b.id}`, { method: "PUT", body: { code: f.elements.code.value, name: f.elements.name.value, description: f.elements.description.value } }) });
    if (out) { ctx.status("Box saved", "ok"); await refreshAll(); showTab("boxes"); }
  }
  async function boxStatus(b, payload) {
    try { await api(`/api/erp/boxes/${b.id}/status`, { method: "POST", body: payload }); ctx.status(`Box ${b.code} ${payload.active ? "enabled" : "disabled"}`, "ok"); await refreshAll(); showTab("boxes"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function disableBox(b) {
    if (!b.products) { if (await window.erpConfirm(`Disable box ${rack.code} / ${b.code}? It is empty.`)) await boxStatus(b, { active: false }); return; }
    const others = rack.box_list.filter((x) => x.active && x.id !== b.id);
    const out = await modal({ title: `Disable box ${rack.code} / ${b.code}`, submitLabel: "Disable box",
      body: `<div class="form-grid"><p class="full warn-box">Box ${esc(b.code)} holds ${n(b.products)} product(s), ${n(b.units)} unit(s). Choose where they go — nothing is left pointing at a disabled box.</p>
        <label class="chk full"><input type="radio" name="then" value="clear" checked> Keep them in ${esc(rack.code)} without a box</label>
        ${others.length ? `<label class="chk full"><input type="radio" name="then" value="move"> Move them to another box of ${esc(rack.code)}</label>
          <label class="full">Box<select name="to">${others.map((x) => `<option value="${x.id}">${esc(x.code)}${x.name ? " · " + esc(x.name) : ""}</option>`).join("")}</select></label>` : ""}</div>`,
      onSubmit: (f) => api(`/api/erp/boxes/${b.id}/status`, { method: "POST", body: { active: false, then: f.elements.then.value, move_to_box: f.elements.to?.value } }) });
    if (out) { ctx.status(`Box ${b.code} disabled${out.moved ? " · " + resultText(out.moved) : ""}`, "ok"); await refreshAll(); showTab("boxes"); }
  }

  // ---------------------------------------------------------------- History tab
  async function historyTab(focus) {
    body.innerHTML = `<div class="filters rk-sub"><b>Movements in and out of ${esc(rack.code)}</b><span class="spacer"></span><span class="rh-count muted"></span></div><div class="rk-grid"></div><div class="rh-changes"></div>`;
    const g = bodyGrid = new Grid({
      label: "Rack history", storageKey: "rack-history", empty: "No movements yet.",
      columns: [
        { key: "at", label: "When", width: 130, render: (r) => esc(fmtDateTime(r.at)) },
        { key: "direction", label: "", width: 40, render: (r) => ({ in: '<span class="ok-t">IN</span>', out: '<span class="bad-t">OUT</span>', within: "↔" })[r.direction] },
        { key: "product", label: "Product", width: 220, render: (r) => `<b>${esc(r.product)}</b>` },
        { key: "from", label: "From", width: 150, render: (r) => esc(r.from || "Unassigned") },
        { key: "to", label: "To", width: 150, render: (r) => esc(r.to || "Unassigned") },
        { key: "user", label: "By", width: 90 },
        { key: "reason", label: "Reason / reference", width: 200, render: (r) => esc([r.reason, r.reference].filter(Boolean).join(" · ") || r.source) },
      ],
    });
    $(".rk-grid", body).append(g.el);
    try {
      const d = await api(`/api/erp/racks/${rack.id}/history?limit=500`);
      g.setRows(d.rows);
      $(".rh-count", body).textContent = `${n(d.total)} movement${d.total === 1 ? "" : "s"}${d.total > 500 ? " (newest 500)" : ""}`;
      $(".rh-changes", body).innerHTML = d.changes.length ? `<details><summary>Rack changes (${d.changes.length})</summary><ul>${d.changes.map((c) => `<li>${esc(fmtDateTime(c.at))} · ${esc(c.user)} · ${esc(c.details)}</li>`).join("")}</ul></details>` : "";
    } catch (err) { ctx.status(err.message, "error"); }
    if (focus) g.focus();
  }

  // ---------------------------------------------------------------- Snapshots tab
  async function snapshotsTab(focus) {
    body.innerHTML = `<div class="filters rk-sub"><label>Days<select class="rs-days"><option>7</option><option selected>30</option><option>60</option><option>90</option></select></label>
      <label>Open a date<input type="date" class="rs-date"></label><span class="spacer"></span>
      <span class="hint">Each day is the rack at that day's close, rebuilt from location history and the stock ledger.</span></div>
      <div class="rs-split"><div class="rk-grid rs-days-grid"></div><div class="rs-detail"><p class="muted es-pad">Enter on a day opens what the rack held then.</p></div></div>`;
    const g = bodyGrid = new Grid({
      label: "Daily snapshots", storageKey: "rack-days",
      columns: [
        { key: "date", label: "Close of", width: 100, render: (r) => esc(dayLabel(r.date)) },
        { key: "products", label: "Products", width: 70, align: "num" },
        { key: "batches", label: "Batches", width: 64, align: "num" },
        { key: "units", label: "Units", width: 80, align: "num", render: (r) => n(r.units) },
      ],
      onActivate: (r) => openDay(r.date),
      onSelect: (r) => r && openDay(r.date, false),
    });
    $(".rs-days-grid", body).append(g.el);
    const today = new Date().toISOString().slice(0, 10);
    $(".rs-date", body).max = today;
    const load = async () => {
      try { const d = await api(`/api/erp/racks/${rack.id}/timeline?days=${$(".rs-days", body).value}${box ? "&box=" + box : ""}`); g.setRows(d.days); }
      catch (err) { ctx.status(err.message, "error"); }
    };
    $(".rs-days", body).addEventListener("change", load);
    $(".rs-date", body).addEventListener("change", (e) => e.target.value && openDay(e.target.value));
    await load();
    if (focus) g.focus();
  }
  let dayTimer;
  function openDay(day, now = true) {
    clearTimeout(dayTimer);
    dayTimer = setTimeout(async () => {
      const el = $(".rs-detail", body);
      if (!el) return;
      try {
        const d = await api(`/api/erp/racks/${rack.id}/snapshot?day=${day}`);
        el.innerHTML = `<p class="rs-asof"><b>${esc(rack.code)}</b> as of <b>${esc(dayLabel(d.date))}</b>, close of day (${esc(d.timezone)}) · ${n(d.products)} products · ${n(d.units)} units</p>
          <table class="rawtab rs-table"><thead><tr><th>Product</th>${cfg.location_boxes_enabled ? "<th>Box</th>" : ""}<th>Batch</th><th>Expiry</th><th class="num">Qty</th><th>Status</th></tr></thead><tbody>
          ${d.rows.map((r) => `<tr><td>${esc(r.item)}</td>${cfg.location_boxes_enabled ? `<td class="mono">${esc(r.box || "—")}</td>` : ""}<td class="mono">${esc(r.batch || "—")}</td>
            <td>${r.expiry ? fmtExpShort(r.expiry) : "—"}</td><td class="num">${n(r.quantity)}</td><td>${esc(r.status)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">Empty that day.</td></tr>'}</tbody></table>`;
      } catch (err) { el.innerHTML = `<p class="bad es-pad">${esc(err.message)}</p>`; }
    }, now ? 0 : 250);
  }

  // ---------------------------------------------------------------- Settings tab
  async function settingsTab(focus) {
    const s = await api("/api/erp/locations/settings");
    const canEdit = !!CAN["rack.edit"];
    body.innerHTML = `<div class="rk-settings">
      <form class="form-grid rs-rack">
        <label>Rack code *<input name="code" required maxlength="30" value="${esc(rack.code)}" ${canEdit ? "" : "disabled"}></label>
        <label>Name<input name="name" maxlength="80" value="${esc(rack.name || "")}" ${canEdit ? "" : "disabled"}></label>
        <label>Display order<input name="sort_order" inputmode="numeric" value="${rack.sort_order}" ${canEdit ? "" : "disabled"}></label>
        <label class="full">Description<input name="description" maxlength="500" value="${esc(rack.description || "")}" ${canEdit ? "" : "disabled"}></label>
        <div class="full"><span class="rs-label">Preferred rack for these categories</span>
          <details class="dd rs-cats" ${canEdit ? "" : "inert"}><summary class="dd-btn"><span class="dd-text"></span><span class="dd-arrow">▾</span></summary>
            <div class="dd-menu">${s.categories.map((c) => `<label class="chk"><input type="checkbox" name="cat" value="${esc(c.code)}" ${rack.categories.includes(c.code) ? "checked" : ""} ${canEdit ? "" : "disabled"}> ${esc(c.name)}${c.default_rack_id && c.default_rack_id !== rack.id ? ' <small class="muted">(another rack now — choosing it here moves the preference)</small>' : ""}</label>`).join("")}</div></details>
          <small class="hint">A suggestion when stock of these categories arrives — never applied by itself.</small></div>
        <div class="full rs-actions">${canEdit ? '<button type="submit" class="btn primary">Save rack</button>' : ""}
          ${CAN["rack.disable"] ? (rack.active ? '<button type="button" class="btn rs-disable">Disable rack…</button>' : '<button type="button" class="btn rs-enable">Enable rack</button>') : ""}</div>
      </form>
      ${canEdit ? `<form class="form-grid rs-global"><fieldset class="full"><legend>Location settings (all racks)</legend>
        ${Object.entries(s.labels).map(([k, label]) => `<label class="chk full"><input type="checkbox" name="${k}" ${s.config[k] ? "checked" : ""}> ${esc(label)}</label>`).join("")}
        <button type="submit" class="btn">Save settings</button></fieldset></form>` : ""}</div>`;
    const dd = $(".rs-cats", body);
    const ddText = () => {
      const names = $$('input[name="cat"]:checked', dd).map((x) => x.parentElement.textContent.replace(/\(another rack.*\)/, "").trim());
      $(".dd-text", dd).textContent = names.length ? names.join(", ") : "No category — choose…";
    };
    dd.addEventListener("change", ddText);
    dd.addEventListener("keydown", (e) => { if (e.key === "Escape" && dd.open) { e.preventDefault(); e.stopPropagation(); dd.open = false; dd.querySelector("summary").focus(); } });
    document.addEventListener("mousedown", (e) => { if (dd.isConnected && dd.open && !dd.contains(e.target)) dd.open = false; });
    ddText();
    $(".rs-rack", body).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        await api(`/api/erp/racks/${rack.id}`, { method: "PUT", body: { code: f.elements.code.value, name: f.elements.name.value, sort_order: f.elements.sort_order.value,
          description: f.elements.description.value, categories: $$('input[name="cat"]:checked', f).map((x) => x.value) } });
        ctx.status(`Rack ${f.elements.code.value} saved`, "ok"); await refreshAll();
      } catch (err) { ctx.status(err.message, "error"); }
    });
    $(".rs-global", body)?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/erp/locations/settings", { method: "PUT", body: Object.fromEntries(Object.keys(s.labels).map((k) => [k, e.target.elements[k].checked])) });
        ctx.status("Location settings saved", "ok"); await refreshAll();
      } catch (err) { ctx.status(err.message, "error"); }
    });
    $(".rs-disable", body)?.addEventListener("click", () => setRackStatus(false));
    $(".rs-enable", body)?.addEventListener("click", () => setRackStatus(true));
    bodyGrid = null;
    if (focus) body.querySelector("input:not([disabled])")?.focus();
  }

  // ---------------------------------------------------------------- rack create / edit / status
  async function newRack() {
    if (!CAN["rack.create"]) return;
    const out = await modal({ title: "New rack", submitLabel: "Create rack",
      body: `<div class="form-grid"><label>Rack code *<input name="code" required maxlength="30" autofocus placeholder="R-A01"></label>
        <label>Name<input name="name" maxlength="80" placeholder="Antibiotics"></label><label class="full">Description<input name="description" maxlength="500"></label>
        <p class="full hint">The code is what staff read on the shelf label and what POS shows first. It must be unique; names may repeat.</p></div>`,
      onSubmit: (f) => api("/api/erp/racks", { method: "POST", body: { code: f.elements.code.value, name: f.elements.name.value, description: f.elements.description.value } }) });
    if (out) { ctx.status(`Rack ${out.code} created`, "ok"); await loadRacks(out.id); }
  }
  function editRack() { if (rack) showTab("settings"); }
  async function setRackStatus(active) {
    if (!rack) return;
    try {
      if (active) { await api(`/api/erp/racks/${rack.id}/status`, { method: "POST", body: { active: true } }); ctx.status(`${rack.code} enabled`, "ok"); await refreshAll(); return; }
      if (!rack.products) {
        if (!(await window.erpConfirm(`Disable rack ${rack.code}? It is empty; it stays in history and reports.`))) return;
        await api(`/api/erp/racks/${rack.id}/status`, { method: "POST", body: { active: false } });
        ctx.status(`${rack.code} disabled`, "ok"); await refreshAll(); return;
      }
      const code = rack.code;
      const picked = await pickLocation({ title: `Disable rack ${code}`, exclude: rack.id,
        intro: `<span class="warn-t">Rack ${esc(code)} holds ${n(rack.products)} product(s), ${n(rack.units)} unit(s).</span> Choose where they go; then the rack is disabled. Nothing is left pointing at a disabled rack.`,
        verb: (t) => `Move ${n(rack.products)} products${t ? " to " + t : ""} and disable ${code}`,
        apply: (t) => { if (t.rack_id === rack.id) throw new Error("Choose a different rack"); return api(`/api/erp/racks/${rack.id}/status`, { method: "POST", body: { active: false, move_to_rack: t.rack_id, move_to_box: t.box_id } }); } });
      if (picked) { ctx.status(`${code} disabled · ${resultText(picked.result.moved)}`, "ok"); await refreshAll(); }
    } catch (err) { ctx.status(err.message, "error"); }
  }
  async function refreshAll() { await loadRacks(rack && rack.id); }

  $(".rk-new", root)?.addEventListener("click", newRack);
  let qt;
  $(".rk-q", root).addEventListener("input", (e) => { clearTimeout(qt); qt = setTimeout(() => { q = e.target.value.trim(); loadRacks(); }, 150); });
  $(".rk-q", root).addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); list.focus(); } });
  $(".rk-status", root).addEventListener("change", (e) => { status = e.target.value; loadRacks(); });
  list.el.addEventListener("keydown", (e) => { if (e.key === "ArrowRight" && !e.ctrlKey) { e.preventDefault(); focusBody(); } });

  const KEYS = {
    "racks.search": () => { $(".rk-q", root).focus(); $(".rk-q", root).select(); },
    "racks.new": newRack,
    "racks.addProducts": addProducts,
    "racks.refresh": refreshAll,
    "racks.tab": nextTab,
    "racks.move": moveSelected,
    "racks.edit": editRack,
    "racks.newBox": newBox,
  };
  loadRacks(params && params.rack ? Number(params.rack) : undefined);
  return {
    get keys() { return keys.bar("racks"); },
    onKey(e, name) { const fn = KEYS[keys.lookup("racks", name)]; if (!fn) return false; fn(); return true; },
    onShow({ focus }) { refreshAll(); if (focus) setTimeout(() => list.focus(), 0); },
    navigate(p) { if (p && p.rack) loadRacks(Number(p.rack)); },
    onDataChanged(areas) { if (areas.some((a) => a === "locations" || a === "inventory")) refreshAll(); },
  };
}
