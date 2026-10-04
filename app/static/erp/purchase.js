import * as keys from "erp/keys";
import { physicalEditor, formFor, formOptions, forms as itemForms, wireFormSelect, pickCategory } from 'erp/physical-units';
// Purchase document — one supplier invoice, reviewed line by line before it
// reaches stock. Every cell shows the corrected value; the supplier's original
// stays visible in the side panel (and in the audit trail). Nothing is posted
// until every line is Ready/Corrected, and posting is all-or-nothing.
//
//   Enter    correct the line          F4        match product
//   F2       invoice header             Shift+F4  create as new product
//   F3       add a line                 F6        accept the line's warnings
//   F8       next line needing review   F12       post to stock
//   Delete   remove the line            Alt+O     supplier's file
import { $, BOOT, api, debounce, esc, fmtDateTime, fmtExp, h, modal, money, toBase } from "erp/core";
import { Grid } from "erp/grid";
import { pickLocation } from "erp/locations";

export const STATUS = {
  READY: ["Ready", "ok"], CORRECTED: ["Corrected", "ok"], NEEDS_REVIEW: ["Needs review", "warn"],
  PRODUCT_MATCH_REQUIRED: ["Product?", "warn"], INVALID: ["Invalid", "bad"], POSTED: ["Posted", "ok"],
  CLOSED: ["Not received", "muted"],
};
const DOC = { DRAFT: "Draft", PARTIAL: "Partly posted", POSTED: "Posted", CANCELLED: "Cancelled" };
// confidence gate (server: confidence_gate.py)
const GATE = { AUTO_ACCEPT: ["Auto", "ok"], AUTO_ACCEPT_WITH_WARNING: ["Warning", "warn"], REVIEW: ["Review", "warn"], BLOCK: ["Blocked", "bad"] };
const ATTENTION = ["REVIEW", "BLOCK"];
const FIELD_NAMES = { product: "product", quantity: "quantity", batch: "batch", expiry: "expiry", packaging: "pack conversion",
  amount: "amount", gst: "GST", mrp: "MRP" };
const DONE = ["POSTED", "CLOSED"];
const READYISH = ["READY", "CORRECTED"];
// postable by a person: ready, or open only because of routine warnings that posting accepts
const postableRow = (r) => READYISH.includes(r.status) || (r.status === "NEEDS_REVIEW" && r.gate?.state === "AUTO_ACCEPT_WITH_WARNING");
const FIELDS = [
  ["name", "Supplier description", "full"], ["supplier_code", "Supplier product code"], ["pack", "Pack"],
  ["batch", "Batch"], ["expiry", "Expiry (month-year)"], ["quantity", "Qty (packs)"], ["free", "Free (packs)"],
  ["rate", "Rate / pack"], ["mrp", "MRP / pack"], ["discount", "Disc %"], ["gst", "GST %"], ["amount", "Amount (before GST)"],
  ["hsn", "HSN"], ["manufacturer", "Manufacturer"],
];
const t = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "");
const catName = (code) => ((BOOT.category_options || []).find((c) => c.code === code) || {}).name || t(code);

/** Make ``el`` the field the modal focuses when it opens. */
const focusField = (form, el) => { if (!el) return; form.querySelectorAll("[autofocus]").forEach((x) => x.removeAttribute("autofocus")); el.setAttribute("autofocus", ""); };

/** Product chooser (search + ranked suggestions). Resolves with {id, name, …} or null.
 *  With ``allowManual`` the last row is always "enter manually", resolving with {manual: true, name}. */
export function pickProduct({ title = "Choose product", initial = "", suggestionsUrl = "", searchUrl = "/api/erp/purchase-products", allowManual = false } = {}) {
  let rows = [], at = 0, ctrl = null;
  const body = h(`<div class="picker">
    <input name="q" autocomplete="off" spellcheck="false" placeholder="Type product name, code or barcode" value="${esc(initial)}" autofocus>
    <div class="picker-list" role="listbox"></div>
    <p class="hint">↑↓ choose · Enter selects · suggestions are ranked by similarity and are never applied without you${allowManual ? " · not in the list? choose “Enter manually”" : ""}</p></div>`);
  const list = body.querySelector(".picker-list");
  const input = body.querySelector("input");
  const count = () => rows.length + (allowManual ? 1 : 0);
  const render = () => {
    const typed = input.value.trim();
    list.innerHTML = (rows.length ? rows.map((r, i) => `<div class="pick${i === at ? " on" : ""}" data-i="${i}" role="option" aria-selected="${i === at}">
      <b>${esc(r.name)}</b> <span class="muted">${esc(r.pack || "")}${r.manufacturer ? " · " + esc(r.manufacturer) : ""}</span>
      ${r.score != null ? `<span class="score">${r.score}%${r.note ? " · " + esc(r.note) : ""}</span>` : ""}</div>`).join("")
      : allowManual ? "" : '<div class="muted pick-empty">No products — type to search</div>')
      + (allowManual ? `<div class="pick pick-manual${at === rows.length ? " on" : ""}" data-i="${rows.length}" role="option" aria-selected="${at === rows.length}">
      ✎ <b>Enter manually</b>${typed ? ` — “${esc(typed)}”` : ""} <span class="muted">type every detail yourself; match or create the product later</span></div>` : "");
    const on = list.querySelector(".pick.on");
    if (on) on.scrollIntoView({ block: "nearest" });
  };
  const search = debounce(async (q) => {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    try {
      if (!q.trim()) {
        rows = suggestionsUrl ? (await api(suggestionsUrl, { signal: ctrl.signal })).suggestions.map((s) => ({ ...s, id: s.item_id })) : [];
      } else rows = (await api(`${searchUrl}?q=${encodeURIComponent(q)}`, { signal: ctrl.signal })).items;
      at = 0; render();
    } catch (err) { if (err.name !== "AbortError") list.innerHTML = `<div class="bad">${esc(err.message)}</div>`; }
  }, 120);
  input.addEventListener("input", () => { if (allowManual) render(); search(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (count()) { at = (at + (e.key === "ArrowDown" ? 1 : -1) + count()) % count(); render(); }
    }
  });
  list.addEventListener("mousedown", (e) => {
    const p = e.target.closest('.pick');
    if (!p) return;
    e.preventDefault();
    at = Number(p.dataset.i);
    // Keep the clicked row intact so the second click can deliver dblclick.
    list.querySelectorAll('.pick').forEach((row,i) => {
      row.classList.toggle('on',i === at);
      row.setAttribute('aria-selected',String(i === at));
    });
    input.focus({preventScroll:true});
  });
  list.addEventListener("dblclick", () => body.closest("form")?.requestSubmit());
  suggestionsUrl && !initial ? search.flush("") : search.flush(initial);
  return modal({ title, body, wide: true, submitLabel: "Select",
    onSubmit: () => {
      if (rows[at]) return rows[at];
      if (allowManual) return { manual: true, name: input.value.trim() };
      throw new Error("Choose a product from the list");
    } });
}

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const id = Number(params.id);
  let doc = null, lines = [];
  // the status chips filter the lines; the selection is kept by line id, so it survives switching filters
  let view = null, switching = false;          // null until the first load picks the inbox or all lines
  const gateOf = (l) => l.gate?.state || "";
  const needsAttention = (l) => !DONE.includes(l.status) && ATTENTION.includes(gateOf(l));
  const isProposed = (l) => !DONE.includes(l.status) && !!l.gate?.proposed && !ATTENTION.includes(gateOf(l));
  const markedIds = new Set();
  const markedRows = () => lines.filter((l) => markedIds.has(l.id));                 // any status (category, roll back)
  const selectedRows = () => markedRows().filter((l) => !DONE.includes(l.status));   // open lines (post, correct)
  root.innerHTML = `<div class="pdoc">
    <div class="pdoc-head"></div>
    <div class="pdoc-bar"></div>
    <div class="filters pdoc-filters"></div>
    <div class="pdoc-split"><div class="pdoc-lines"></div><aside class="pdoc-side"></aside></div>
  </div>`;
  const head = $(".pdoc-head", root), bar = $(".pdoc-bar", root), side = $(".pdoc-side", root);
  const draft = () => doc && doc.purchase.status === "DRAFT";
  const isOpen = () => doc && ["DRAFT", "PARTIAL"].includes(doc.purchase.status);
  const canEdit = () => isOpen() && CAN["purchase.create"] !== false;
  const corr = (r, f) => r.corrections && r.corrections[f];
  const cell = (r, f, text) => {
    const c = corr(r, f);
    const bad = (r.issues || []).some((i) => i.field === f && !i.accepted && i.level !== "info");
    return `<span class="${c ? "corr" : ""}${bad ? " flag" : ""}" ${c ? `title="Supplier wrote: ${esc(c.raw || "(blank)")}"` : ""}>${text}</span>`;
  };

  const grid = new Grid({
    label: "Invoice lines", storageKey: "purchase-lines", empty: "No lines. F3 adds a line.",
    columns: [
      { key: "line_no", label: "#", width: 36, align: "num" },
      { key: "status", label: "Status", width: 96, cellClass: (r) => "ps-" + STATUS[r.status][1], render: (r) => STATUS[r.status][0] },
      { key: "name", label: "Supplier description", width: 230, render: (r) => cell(r, "name", esc(r.name) + (r.supplier_code ? ` <span class="muted mono">${esc(r.supplier_code)}</span>` : "")) },
      { key: "item", label: "Our product", width: 210, render: (r) => r.item ? `${esc(r.item.name)} <span class="tag">${esc(matchLabel(r.match))}</span>`
        : r.new_product ? `<span class="tag new">NEW</span> ${esc(r.name)}` : '<span class="bad">— F4 to match</span>' },
      { key: "product_category", label: "Category", width: 84, render: (r) => r.product_category ? `<span class="tag">${esc(catName(r.product_category))}</span>` : '<span class="muted">—</span>' },
      ...(CAN["rack.view"] ? [{ key: "location", label: "Rack", width: 104, render: (r) => locCell(r.location) }] : []),
      { key: "batch", label: "Batch", width: 96, render: (r) => cell(r, "batch", `<span class="mono">${esc(r.batch || "—")}</span>`) },
      { key: "expiry", label: "Expiry", width: 74, render: (r) => cell(r, "expiry", r.expiry ? fmtExp(r.expiry) : `<span class="muted">${esc(r.expiry_raw || "—")}</span>`) },
      { key: "pack", label: "Packing", width: 82, render: (r) => cell(r, "pack", `<span class="mono">${esc(r.pack || "—")}</span>`) },
      { key: "qty", label: "Billed", width: 50, align: "num", render: (r) => cell(r, "quantity", esc(r.receipt?.paid ?? r.qty)) },
      { key: "free", label: "Free", width: 46, align: "num", render: (r) => cell(r, "free", esc(r.receipt?.free ?? r.free ?? "")) },
      { key: 'received_stock', label: 'Received stock', width: 138, render: r => esc(r.stock?.stock || '') },
      { key: 'equivalent', label: 'Stock equivalent', width: 205, render: r => r.stock?.resolved || DONE.includes(r.status) || !canEdit()
        ? `<span title="${esc(r.stock?.hierarchy || '')}">${esc(r.stock?.equivalent || '')}</span>`
        : `<button type="button" class="linkish" data-pack-fix="${r.id}" title="${esc(r.stock?.hierarchy || 'Confirm what one invoice Qty is')}">${esc(r.stock?.equivalent || '')} ✎</button>` },
      { key: 'gate', label: 'Confidence', width: 104, cellClass: r => 'gate-' + (GATE[r.gate?.state]?.[1] || 'muted'),
        render: r => r.gate?.state && !DONE.includes(r.status) ? `<span title="${esc(gateTitle(r))}">${isProposed(r) ? "Proposed" : GATE[r.gate.state][0]}${r.gate.confidence != null ? ` ${Math.round(r.gate.confidence * 100)}%` : ''}</span>` : '' },
      { key: 'form', label: 'Form', width: 90, render: r => esc((r.physical?.form || '').toLowerCase().replace(/^./, c => c.toUpperCase()) || '—') },
      { key: 'base_unit', label: 'Base Unit', width: 90, render: r => esc((r.physical?.base_unit || '').toLowerCase().replace(/^./, c => c.toUpperCase()) || '—') },
      { key: "rate", label: "Rate", width: 70, align: "num", render: (r) => cell(r, "rate", money(r.rate)) },
      { key: "mrp", label: "MRP", width: 70, align: "num", render: (r) => cell(r, "mrp", money(r.mrp)) },
      { key: "discount", label: "Disc", width: 62, align: "num", render: (r) => (Number(r.discount) ? money(r.discount) : "") },
      { key: "gst", label: "GST%", width: 50, align: "num", render: (r) => cell(r, "gst", r.gst !== "" ? Number(r.gst) : (r.gst_source === "NONE" ? '<span class="muted">0</span>' : '<span class="bad">?</span>')) },
      { key: "gst_amount", label: "GST ₹", width: 70, align: "num", render: (r) => (r.gst_amount ? money(r.gst_amount) : "") },
      { key: "rate_incl", label: "Rate incl. GST", width: 92, align: "num", render: (r) => (r.rate_incl ? `<b>${money(r.rate_incl)}</b>` : "") },
      { key: "amount", label: "Amount", width: 86, align: "num", render: (r) => cell(r, "amount", money(r.amount)) },
      { key: "landed", label: "Amount incl. GST", width: 104, align: "num", render: (r) => (r.landed ? `<b>${money(r.landed)}</b>` : "") },
      { key: "issue", label: "Issue", width: 260, render: (r) => issueText(r) },
    ],
    multi: true,
    canMark: (r) => r.status !== "CLOSED",
    onMarks: (visible) => {
      if (switching) return;           // the grid is being refilled for another filter
      const shown = new Set(grid.rows.map((r) => r.id));
      for (const id of [...markedIds]) if (shown.has(id)) markedIds.delete(id);
      for (const r of visible) markedIds.add(r.id);
      renderBar();
    },
    rowClass: (r) => (DONE.includes(r.status) ? (r.status === "CLOSED" ? "dim" : "") : STATUS[r.status][1] === "ok" ? "" : "needs"),
    onSelect: (r) => renderSide(r),
    onActivate: (r) => (canEdit() && !DONE.includes(r.status) ? editLine(r) : openLedger(r)),
    contextMenu: (r) => canEdit() && !DONE.includes(r.status) ? [
      { label: 'Adjust quantity / form', action: () => adjustQuantity(r) },
      { label: 'Correct packing (what one Qty is)', key: keys.keyFor("purchase.packaging"), action: () => packagingDialog(r) },
      { label: 'Match inspector (why this match)', key: keys.keyFor("purchase.inspect"), action: () => inspector(r) },
      { label: 'Verify invoice unit', action: () => verifyInvoiceUnit(r) },
      { label: 'Medicine reference', action: () => showReference(r) },
      { label: "Correct line", key: "Enter", action: () => editLine(grid.selected) },
      { label: "Match product", key: keys.keyFor("purchase.product"), action: () => matchProduct(grid.selected) },
      { label: "Create as new product", key: keys.keyFor("purchase.newProduct"), action: () => newProduct(grid.selected) },
      { label: "Accept warnings", key: keys.keyFor("purchase.accept"), action: () => accept(grid.selected) },
      { label: "Remove line", key: keys.keyFor("purchase.removeLine"), action: () => removeLine(grid.selected) },
      { label: "Change category only", key: keys.keyFor("purchase.category"), action: () => changeCategory() },
      ...rackItems(r),
      { label: "Mark / unmark for posting", key: "Space", action: () => grid.toggleMark() },
      ...(draft() ? [{ label: "Delete purchase draft", key: keys.keyFor("purchase.delete"), action: deleteDoc },
        { label: "Cancel purchase draft", key: keys.keyFor("purchase.cancel"), action: cancelDoc }] : []),
    ] : [{ label: "Stock ledger of the product", key: "Enter", action: () => openLedger(grid.selected) },
      ...(CAN["purchase.create"] ? [{ label: "Change category only", key: keys.keyFor("purchase.category"), action: () => changeCategory() }] : []),
      ...rackItems(r),
      ...(r.status === "POSTED" && CAN["purchase.post"] ? [{ label: "Roll back to draft (take out of stock)", key: keys.keyFor("purchase.rollback"), action: () => rollback() }] : [])],
  });
  $(".pdoc-lines", root).append(grid.el);

  /** Where the stock goes: set on the line (bold), already the product's place, or only suggested (italic, "?"). */
  function locCell(l) {
    if (!l || l.state === "NONE") return '<span class="muted">—</span>';
    if (l.state === "CONFIRMED") return `<b class="loc-code" title="Set for this receipt${l.rack_name ? " · " + esc(l.rack_name) : ""}">${esc(l.label)}</b>`;
    if (l.state === "CURRENT") return `<span class="loc-code" title="The product's current place${l.rack_name ? " · " + esc(l.rack_name) : ""}">${esc(l.label)}</span>`;
    const why = { CATEGORY: "set for its category", USUAL: "where its category usually is", LAST: "where it was before" }[l.source] || "";
    return `<i class="loc-sug" title="Suggested — ${esc(why)}. ${esc(keys.keyFor("purchase.rackSuggested"))} accepts">${esc(l.label)}?</i>`;
  }
  function rackItems(r) {
    if (!CAN["rack.assign"]) return [];
    const cat = r && r.product_category;
    return [
      { label: markedRows().length > 1 ? `Set rack for ${markedRows().length} lines…` : "Set rack / box…", key: keys.keyFor("purchase.rack"), action: () => setRack() },
      ...(suggestedRows().length ? [{ label: `Accept suggested racks (${suggestedRows().length})`, key: keys.keyFor("purchase.rackSuggested"), action: () => acceptSuggested() }] : []),
      ...(cat ? [{ label: `Select all ${catName(cat)} lines`, action: () => selectCategory(cat) }] : []),
    ];
  }
  const pickRows = () => (markedRows().length ? markedRows() : grid.selected ? [grid.selected] : []);
  const suggestedRows = () => (markedRows().length ? markedRows() : lines).filter((l) => l.location?.state === "SUGGESTED");
  function selectCategory(cat) {
    const ids = lines.filter((l) => l.product_category === cat && l.status !== "CLOSED").map((l) => l.id);
    ids.forEach((i) => markedIds.add(i));
    grid.setMarks(grid.rows.map((r, i) => (markedIds.has(r.id) ? i : -1)).filter((i) => i >= 0));
    renderBar();
    ctx.status(`${ids.length} ${catName(cat)} line(s) selected — ${keys.keyFor("purchase.rack")} sets their rack`, "ok");
  }
  /** One rack / box for the selected lines (stock posts there); received lines move their product now. */
  async function setRack() {
    if (!CAN["rack.assign"]) { ctx.status("Setting racks needs the rack assign right", "warn"); return; }
    const rows = pickRows();
    if (!rows.length) { ctx.status("Select lines first (Space marks one, Shift+↑↓ several)", "warn"); return; }
    const first = rows[0].location;
    const set = rows.filter((r) => r.location?.state === "CONFIRMED").length;
    const picked = await pickLocation({ title: `Rack for ${rows.length} line(s)`, allowClear: set > 0,
      clearLabel: `Instead, clear the rack chosen on ${set} line(s)`,
      intro: `${rows.length} line(s). Open lines receive into it when posted; already received lines move their product now (stock is not changed).`,
      current: rows.length === 1 && first && first.rack_id ? { rack_id: first.rack_id, box_id: first.box_id } : null,
      verb: (t) => (t === "Unassigned" ? "Clear the rack" : `Set ${t || "rack"} on ${rows.length} line(s)`),
      apply: (t) => api(`/api/erp/purchases/${id}/lines/location`, { method: "POST", body: { line_ids: rows.map((r) => r.id), rack_id: t.rack_id, box_id: t.box_id } }) });
    if (picked) { render(picked.result); ctx.status(`Rack ${picked.label} on ${picked.result.result.changed} line(s)${picked.result.result.moved ? ` · ${picked.result.result.moved} product(s) moved` : ""}`, "ok"); }
    grid.focus();
  }
  async function acceptSuggested() {
    if (!CAN["rack.assign"]) return;
    const rows = suggestedRows();
    if (!rows.length) { ctx.status("No suggested racks to accept", "ok"); return; }
    try {
      const out = await api(`/api/erp/purchases/${id}/lines/location`, { method: "POST", body: { line_ids: rows.map((r) => r.id), suggested: true } });
      render(out);
      ctx.status(`Suggested rack confirmed on ${out.result.changed} line(s)`, "ok");
    } catch (err) { ctx.status(err.message, "error"); }
  }

  const matchLabel = (m) => ({ SUPPLIER_MAP: "supplier map", CODE: "code", EXACT_NAME: "exact name", NORMALIZED_NAME: "normalised name",
    CANONICAL_NAME_PACK: "name + pack", MANUAL: "chosen", NEW_PRODUCT: "created" })[m] || m.toLowerCase();
  function gateTitle(r) {
    const g = r.gate || {};
    const f = Object.entries(g.fields || {}).map(([k, v]) => `${FIELD_NAMES[k] || k} ${Math.round(v * 100)}% (${(g.provenance || {})[k] || "—"})`);
    return `${GATE[g.state]?.[0] || ""}: weakest is ${FIELD_NAMES[g.weakest] || g.weakest}\n${f.join("\n")}`;
  }
  function issueText(r) {
    const open = (r.issues || []).filter((i) => !i.accepted).sort((a, b) => (a.level === "info") - (b.level === "info"));
    if (!open.length) return (r.issues || []).length ? '<span class="muted">warnings accepted</span>' : "";
    return `<span class="${open[0].level === "warn" ? "warn" : open[0].level === "info" ? "muted" : "bad"}">${esc(open[0].message)}</span>${open.length > 1 ? ` <span class="muted">+${open.length - 1}</span>` : ""}`;
  }

  // ---------------------------------------------------------------- render
  function render(data, { keep = true } = {}) {
    doc = data;
    lines = data.lines;
    const p = data.purchase, s = data.summary;
    ctx.setTitle(p.reference_no || `Draft · ${p.invoice_no || "#" + p.id}`);
    const diff = s.difference == null ? "" : Number(s.difference);
    head.innerHTML = `
      <div class="kv"><span>Document</span><b class="mono">${esc(p.reference_no || "DRAFT #" + p.id)}</b> <span class="doc-st ds-${p.status.toLowerCase()}">${DOC[p.status] || p.status}</span></div>
      <div class="kv"><span>Supplier</span><b>${p.supplier ? esc(p.supplier) : '<span class="bad">not set — F2</span>'}</b></div>
      <div class="kv"><span>Invoice no.</span><b class="mono">${p.invoice_no ? esc(p.invoice_no) : '<span class="bad">— F2</span>'}</b></div>
      <div class="kv"><span>Invoice date</span><b>${p.invoice_date ? esc(p.invoice_date.split("-").reverse().join("/")) : "—"}</b></div>
      <div class="kv"><span>Supplier total</span><b class="num">${p.supplier_total ? "₹" + money(p.supplier_total) : "—"}</b></div>
      <div class="kv" title="${esc(breakdown(s.totals))}"><span>Calculated</span><b class="num">₹${money(s.calculated_total)}</b><small class="muted">${esc(shortBreakdown(s.totals))}</small></div>
      <div class="kv gst-kv" title="${esc(gstTitle(s.gst))}"><span>GST ${s.gst ? (s.gst.mode === "INTER" ? "(IGST)" : "(CGST+SGST)") : ""}</span><b class="num ${s.gst && s.gst.problems.length ? "warn" : ""}">₹${money(s.gst ? s.gst.total : 0)}</b>${s.gst && s.gst.printed ? `<small class="muted">invoice ₹${money(s.gst.printed)}</small>` : ""}</div>
      <div class="kv"><span>Difference</span><b class="num ${diff ? "bad" : "ok"}">${diff === "" ? "—" : "₹" + money(diff)}</b></div>
      <div class="kv"><span>Source</span><b>${esc(p.format || "—")}${p.has_source ? ` <a href="/api/erp/purchases/${p.id}/source" target="_blank" class="muted">file ↗</a>` : ""}</b></div>
      ${p.posted_at ? `<div class="kv"><span>Posted</span><b>${esc(fmtDateTime(p.posted_at))}</b></div>` : ""}
      ${p.cancel_reason ? `<div class="kv"><span>Cancelled</span><b>${esc(p.cancel_reason)}</b></div>` : ""}`;
    renderBar();
    applyView(keep);
    if (!lines.length) renderSide(null);
    ctx.setKeys();
  }

  const VIEWS = { READY: "Ready", CORRECTED: "Corrected", NEEDS_REVIEW: "Needs review", PRODUCT_MATCH_REQUIRED: "Product match",
    INVALID: "Invalid", POSTED: "Posted", CLOSED: "Not received", SELECTED: "Selected", ATTENTION: "Needing attention",
    GATE_WARN: "Accepted with a warning", GATE_AUTO: "Auto-accepted", PROPOSED: "Proposed count" };
  // filters on top of the status chips (find · category · rack · form), so a person can narrow to,
  // say, every FMCG line without a rack, mark them all (Ctrl+A) and set the rack once (Alt+L)
  const LF = { q: "", cat: "", rack: "", form: "" };
  const lineForm = (l) => (l.physical?.form || l.dosage_form || "").toUpperCase();
  const rackState = (l) => l.location?.state || "NONE";
  function lineFilter(l) {
    if (LF.q) {
      const t = LF.q.toLowerCase();
      if (![l.name, l.item?.name, l.supplier_code, l.batch, l.pack, l.manufacturer].some((x) => (x || "").toLowerCase().includes(t))) return false;
    }
    if (LF.cat && (l.product_category || "") !== LF.cat) return false;
    if (LF.form && lineForm(l) !== LF.form) return false;
    if (LF.rack) {
      const s = rackState(l);
      if (LF.rack === "none" && s !== "NONE") return false;
      if (LF.rack === "suggested" && s !== "SUGGESTED") return false;
      if (LF.rack === "set" && s !== "CONFIRMED") return false;
      if (LF.rack === "current" && s !== "CURRENT") return false;
      if (LF.rack === "unplaced" && (s === "CONFIRMED" || s === "CURRENT")) return false;
      if (LF.rack.startsWith("r:") && !((s === "CONFIRMED" || s === "CURRENT") && String(l.location.rack_id) === LF.rack.slice(2))) return false;
    }
    return true;
  }
  const filtering = () => !!(LF.q || LF.cat || LF.rack || LF.form);
  function renderFilters() {
    const fl = $(".pdoc-filters", root);
    const count = (pred) => lines.filter(pred).length;
    const opt = (v, label, n) => `<option value="${esc(v)}">${esc(label)}${n !== undefined ? ` (${n})` : ""}</option>`;
    if (!fl.dataset.built) {
      // built once: re-rendering would replace the search box under the cursor while typing
      fl.dataset.built = "1";
      fl.innerHTML = `
        <label>Find<input class="lf-q" placeholder="product, supplier text, batch, pack" autocomplete="off"></label>
        <label>Category<select class="lf-cat"></select></label>
        ${CAN["rack.view"] ? '<label>Rack<select class="lf-rack"></select></label>' : ""}
        <label>Form<select class="lf-form"></select></label>
        <button type="button" class="btn lf-clear" hidden>Clear filters</button>
        <span class="spacer"></span><span class="muted lf-count"></span>
        <button type="button" class="btn lf-markall" title="Mark every line shown (Ctrl+A in the grid)">Mark all shown</button>
        ${CAN["rack.assign"] ? `<button type="button" class="btn primary lf-rackset">Set rack… <kbd>${esc(keys.keyFor("purchase.rack"))}</kbd></button>` : ""}`;
      for (const [k, cls] of [["cat", ".lf-cat"], ["rack", ".lf-rack"], ["form", ".lf-form"]]) {
        const sel = $(cls, fl);
        if (sel) sel.onchange = () => { LF[k] = sel.value; applyView(false); setTimeout(() => grid.focus(), 0); };
      }
      const q = $(".lf-q", fl);
      let t;
      q.oninput = () => { clearTimeout(t); t = setTimeout(() => { LF.q = q.value.trim(); applyView(false); }, 150); };
      q.onkeydown = (e) => {
        if (e.key === "ArrowDown" || e.key === "Enter") {
          e.preventDefault(); e.stopPropagation(); clearTimeout(t);
          if (LF.q !== q.value.trim()) { LF.q = q.value.trim(); applyView(false); }
          grid.focus();
        } else if (e.key === "Escape" && q.value) { e.preventDefault(); e.stopPropagation(); q.value = ""; LF.q = ""; applyView(false); }
      };
      $(".lf-clear", fl).onclick = () => { Object.assign(LF, { q: "", cat: "", rack: "", form: "" }); q.value = ""; applyView(false); grid.focus(); };
      $(".lf-markall", fl).onclick = () => { grid.markAll(); grid.focus(); };
      $(".lf-rackset", fl)?.addEventListener("click", () => setRack());
    }
    // options and counts follow the lines (after every correction, match or rack change)
    const cats = [...new Set(lines.map((l) => l.product_category || ""))].sort((a, b) => catName(a).localeCompare(catName(b)));
    const forms = [...new Set(lines.map(lineForm).filter(Boolean))].sort();
    const racks = new Map();
    for (const l of lines) if (["CONFIRMED", "CURRENT"].includes(rackState(l)) && l.location.rack_id) racks.set(String(l.location.rack_id), l.location.label.split(" / ")[0]);
    const fill = (cls, k, html) => {
      const sel = $(cls, fl);
      if (!sel) return;
      sel.innerHTML = html;
      if (![...sel.options].some((o) => o.value === LF[k])) LF[k] = "";
      sel.value = LF[k];
    };
    fill(".lf-cat", "cat", opt("", "All") + cats.map((c) => opt(c, c ? catName(c) : "No category", count((l) => (l.product_category || "") === c))).join(""));
    fill(".lf-rack", "rack", opt("", "All")
      + opt("unplaced", "Needs a rack (none or only suggested)", count((l) => !["CONFIRMED", "CURRENT"].includes(rackState(l))))
      + opt("none", "No rack, no suggestion", count((l) => rackState(l) === "NONE"))
      + opt("suggested", "Suggested only", count((l) => rackState(l) === "SUGGESTED"))
      + opt("set", "Set on this purchase", count((l) => rackState(l) === "CONFIRMED"))
      + opt("current", "Already in a rack", count((l) => rackState(l) === "CURRENT"))
      + [...racks].map(([id, code]) => opt("r:" + id, "In " + code, count((l) => ["CONFIRMED", "CURRENT"].includes(rackState(l)) && String(l.location.rack_id) === id))).join(""));
    fill(".lf-form", "form", opt("", "All") + forms.map((f) => opt(f, f.charAt(0) + f.slice(1).toLowerCase(), count((l) => lineForm(l) === f))).join(""));
    $(".lf-clear", fl).hidden = !filtering();
    $(".lf-count", fl).textContent = filtering() ? `${grid.rows.length} of ${lines.length} lines` : "";
  }
  function applyView(keep = true) {
    for (const id of [...markedIds]) if (!lines.some((l) => l.id === id)) markedIds.delete(id);
    if (view === "SELECTED" && !markedIds.size) view = "all";
    if (view === null) view = lines.some(needsAttention) ? "ATTENTION" : lines.some(isProposed) ? "PROPOSED" : "all";
    if (view === "ATTENTION" && !lines.some(needsAttention)) { view = "all"; if (keep) ctx.status("Nothing needs attention any more — every open line is accepted", "ok"); }
    const rows = view === "all" ? lines : view === "SELECTED" ? lines.filter((l) => markedIds.has(l.id))
      : view === "ATTENTION" ? lines.filter(needsAttention)
      : view === "PROPOSED" ? lines.filter(isProposed)
      : view === "GATE_WARN" ? lines.filter((l) => !DONE.includes(l.status) && gateOf(l) === "AUTO_ACCEPT_WITH_WARNING" && !isProposed(l))
      : view === "GATE_AUTO" ? lines.filter((l) => !DONE.includes(l.status) && gateOf(l) === "AUTO_ACCEPT")
      : lines.filter((l) => l.status === view);
    const shown = filtering() ? rows.filter(lineFilter) : rows;
    switching = true;
    try {
      grid.setRows(shown, { keep });
      grid.setMarks(shown.map((r, i) => (markedIds.has(r.id) ? i : -1)).filter((i) => i >= 0));
    } finally { switching = false; }
    renderBar();
    renderFilters();
    if (!shown.length) renderSide(null);
  }
  function setView(v) {
    view = view === v || !v ? "all" : v;
    applyView(false);
    renderBar();
    ctx.status(view === "all" ? `All ${lines.length} lines` : `Showing ${grid.rows.length} ${VIEWS[view].toLowerCase()} line(s) · click the chip again for all`);
    grid.focus?.();
  }

  function renderBar() {
    if (!doc) return;
    const p = doc.purchase, s = doc.summary, c = s.counts;
    const marked = selectedRows();
    const markedReady = marked.filter(postableRow).length;
    const postBtn = isOpen() && CAN["purchase.post"]
      ? marked.length
        ? `<button type="button" class="btn primary p-post" ${markedReady === marked.length ? "" : "disabled"} title="${markedReady === marked.length ? "" : "Only Ready or Corrected lines can be posted"}">Post ${marked.length} selected <kbd>${esc(keys.keyFor("purchase.post"))}</kbd></button>`
        : s.postable
          ? `<button type="button" class="btn primary p-post">${p.status === "PARTIAL" ? `Post remaining ${s.open}` : "Post to stock"} <kbd>${esc(keys.keyFor("purchase.post"))}</kbd></button>`
          : `<button type="button" class="btn primary p-post" ${s.partly_postable && s.ready ? "" : "disabled"} title="${s.ready ? `${s.blocking} line(s) need you first; the ready ones can go to stock now` : "Every open line needs attention first"}">Post ${s.ready} ready <kbd>${esc(keys.keyFor("purchase.post"))}</kbd></button>`
      : "";
    const chip = (v, label, n, tone = "") => `<button type="button" class="chip ${tone}${view === v ? " on" : ""}" data-view="${v}" ${n || v === "all" || view === v ? "" : "disabled"} aria-pressed="${view === v}" title="${v === "all" ? "Show every line" : `Show only ${label.toLowerCase()} lines`}">${label} ${n}</button>`;
    const g = s.gate || {};
    const openCount = (g.AUTO_ACCEPT || 0) + (g.AUTO_ACCEPT_WITH_WARNING || 0) + (g.REVIEW || 0) + (g.BLOCK || 0);
    const inbox = isOpen() && openCount ? `<span class="inbox-sum" title="Confidence gate: lines are auto-accepted only when product, quantity, batch, expiry and pack conversion are all certain">
        ${openCount} open · <b class="ok">${lines.filter((l) => !DONE.includes(l.status) && l.stock?.resolved).length} counted</b> · <b class="warn">${lines.filter(isProposed).length} proposed</b> · <b class="${(g.REVIEW || 0) + (g.BLOCK || 0) ? "bad" : "ok"}">${(g.REVIEW || 0) + (g.BLOCK || 0)} need you</b></span>
      ${chip("ATTENTION", "Needs attention", (g.REVIEW || 0) + (g.BLOCK || 0), (g.REVIEW || 0) + (g.BLOCK || 0) ? "warn" : "")}
      ${chip("PROPOSED", "Proposed — check", lines.filter(isProposed).length, lines.some(isProposed) ? "warn" : "")}
      ${chip("GATE_WARN", "Warnings", lines.filter((l) => !DONE.includes(l.status) && gateOf(l) === "AUTO_ACCEPT_WITH_WARNING" && !isProposed(l)).length)}${chip("GATE_AUTO", "Auto-accepted", g.AUTO_ACCEPT || 0, "ok")}
      <span class="sep"></span>` : "";
    bar.innerHTML = `${inbox}
      ${chip("all", "All lines", s.rows)}
      ${chip("READY", "Ready", c.READY, "ok")}${chip("CORRECTED", "Corrected", c.CORRECTED, "ok")}
      ${chip("NEEDS_REVIEW", "Needs review", c.NEEDS_REVIEW, c.NEEDS_REVIEW ? "warn" : "")}
      ${chip("PRODUCT_MATCH_REQUIRED", "Product match", c.PRODUCT_MATCH_REQUIRED, c.PRODUCT_MATCH_REQUIRED ? "warn" : "")}
      ${c.INVALID ? chip("INVALID", "Invalid", c.INVALID, "bad") : ""}
      ${c.POSTED ? chip("POSTED", "Posted", c.POSTED, "ok") : ""}
      ${c.CLOSED ? chip("CLOSED", "Not received", c.CLOSED) : ""}
      ${markedRows().length && CAN["purchase.create"] ? `<button type="button" class="chip p-cat" title="Change only the category of the selected lines (${esc(keys.keyFor("purchase.category"))})">Category… <kbd>${esc(keys.keyFor("purchase.category"))}</kbd></button>` : ""}
      ${markedRows().length && CAN["rack.assign"] ? `<button type="button" class="chip p-rack" title="Set the rack / box of the selected lines">Rack… <kbd>${esc(keys.keyFor("purchase.rack"))}</kbd></button>` : ""}
      ${CAN["rack.assign"] && isOpen() && suggestedRows().length ? `<button type="button" class="chip p-rsug" title="Confirm each line's suggested rack">Accept ${suggestedRows().length} suggested rack${suggestedRows().length === 1 ? "" : "s"} <kbd>${esc(keys.keyFor("purchase.rackSuggested"))}</kbd></button>` : ""}
      ${marked.length ? `${chip("SELECTED", "Selected", marked.length, "sel")}${markedReady < marked.length ? `<span class="hint">${marked.length - markedReady} not ready</span>` : ""}<button type="button" class="chip clear-sel" title="Clear the selection (Esc)">✕ clear</button>`
        : isOpen() && s.ready ? '<span class="hint">Shift+↑↓ selects lines · Space marks one · Ctrl+A all</span>' : ""}
      <span class="spacer"></span>
      ${s.gst && (s.gst.problems.length || s.gst.missing_rate_lines) ? `<button type="button" class="chip warn gst-chip" title="GST details (${esc(keys.keyFor("purchase.gst"))})">GST ⚠ ${s.gst.problems.length + (s.gst.missing_rate_lines ? 1 : 0)}</button>` : ""}
      ${p.warnings ? `<span class="hint" title="${esc(p.warnings)}">⚠ file notes</span>` : ""}
      ${s.automation?.mode !== "off" && s.automation && isOpen() ? `<button type="button" class="chip automation-chip" title="Lines certain enough to post without anyone (unattended mode)">Unattended-ready ${s.automation.resolved_rows}/${s.automation.total_rows}</button>` : ""}
      ${isOpen() && CAN["purchase.create"] && queued().length ? `<button type="button" class="btn p-queue" title="Products prepared from the invoice that are not in the catalogue yet">Confirm ${queued().length} new product${queued().length === 1 ? "" : "s"}…</button>` : ""}
      ${["POSTED", "PARTIAL"].includes(p.status) && CAN["purchase.post"] && c.POSTED ? `<button type="button" class="btn p-rollback" title="Take posted lines out of stock and back to review">Roll back to draft <kbd>${esc(keys.keyFor("purchase.rollback"))}</kbd></button>` : ""}
      ${postBtn}`;
    const automation = $(".automation-chip", bar);
    if (automation) automation.onclick = async () => {
      const a = s.automation;
      const out = await modal({ title: "Automatic intake", wide: true, submitLabel: "Recheck evidence",
        body: `<p>${a.resolved_rows} of ${a.total_rows} rows have resolved checks. This measures readiness, not independently verified accuracy.</p>
          ${a.document_blockers.length ? `<p><b>Invoice checks</b></p><ul>${a.document_blockers.map(b => `<li>${esc(b.message)}</li>`).join("")}</ul>` : ""}
          ${a.observations?.length ? `<p class="muted">${a.observations.length} HSN classifications have differing printed rates, retained for reference. Financial checks remain separate.</p>` : ""}
          ${a.rounding?.length ? `<p class="muted">Supplier cent rounding retained on ${a.rounding.length} lines.</p>` : ""}
          ${a.exceptions.length ? `<p><b>Products requiring information</b></p><ul>${(a.exception_groups || a.exceptions.map(e => ({ ...e, lines: [e.line] }))).map(e => `<li>Lines ${esc(e.lines.join(", "))}: ${esc(e.name)} — ${esc(e.codes.join(", ").replaceAll("_", " "))}</li>`).join("")}</ul>` : "<p>No row exceptions.</p>"}`,
        onSubmit: () => api(`/api/erp/purchases/${p.id}/prepare`, { method: "POST" }) });
      if (out) await load();
    };
    const post = $(".p-post", bar);
    if (post) post.onclick = () => postDoc();
    const cat = $(".p-cat", bar);
    if (cat) cat.onclick = () => changeCategory();
    const rk = $(".p-rack", bar);
    if (rk) rk.onclick = () => setRack();
    const rs = $(".p-rsug", bar);
    if (rs) rs.onclick = () => acceptSuggested();
    const back = $(".p-rollback", bar);
    if (back) back.onclick = () => rollback();
    const queue = $(".p-queue", bar);
    if (queue) queue.onclick = () => confirmNewProducts();
    const gchip = $(".gst-chip", bar);
    if (gchip) gchip.onclick = () => gstPanel();
    bar.querySelectorAll("[data-view]").forEach((b) => { b.onclick = () => setView(b.dataset.view); });
    const clear = $(".clear-sel", bar);
    if (clear) clear.onclick = () => { markedIds.clear(); grid.clearMarks(); if (view === "SELECTED") setView("all"); else renderBar(); };
  }

  function gstTitle(g) {
    if (!g) return "";
    return `${g.mode_reason}\n` + g.by_rate.map((r) => `${r.rate === null ? "no GST %" : Number(r.rate) + "%"}: GST ₹${money(r.gst)} on ₹${money(r.taxable)}`).join("\n");
  }
  function gstPanel() {
    const g = doc && doc.summary.gst;
    if (!g) return;
    const rows = g.by_rate.map((r) => `<tr><td>${r.rate === null ? '<span class="bad">not on invoice</span>' : Number(r.rate) + "%"}</td><td class="num">${r.lines}</td><td class="num">${money(r.taxable)}</td>
      <td class="num">${money(r.cgst)}</td><td class="num">${money(r.sgst)}</td><td class="num">${money(r.igst)}</td><td class="num"><b>${money(r.gst)}</b></td></tr>`).join("");
    const probs = [...g.problems, ...(g.missing_rate_lines ? [`${g.missing_rate_lines} line(s) have no GST %. How to fix: open the Needs review chip, select each line with a GST-missing note and press Enter to type the GST % from the invoice.`] : [])];
    modal({ title: `GST on ${doc.purchase.reference_no || "this purchase"}`, wide: true, submitLabel: "Close",
      body: `<p class="muted">${esc(g.mode_reason)}. Stock is costed ${g.cost_includes_gst ? "including" : "before"} GST.</p>
        ${(g.registration_notes || []).map((note) => `<p class="muted">${esc(note)}</p>`).join("")}
        <table class="grid-lite"><thead><tr><th>GST %</th><th class="num">Lines</th><th class="num">Taxable</th><th class="num">CGST</th><th class="num">SGST</th><th class="num">IGST</th><th class="num">GST</th></tr></thead>
        <tbody>${rows}</tbody><tfoot><tr><th>Total</th><th></th><th></th><th></th><th></th><th></th><th class="num">₹${money(g.total)}</th></tr></tfoot></table>
        ${g.printed ? `<p>GST printed on the invoice: <b>₹${money(g.printed)}</b> ${Math.abs(Number(g.printed) - Number(g.total)) <= 1 ? '<span class="ok">— matches</span>' : '<span class="bad">— does not match</span>'}</p>` : '<p class="muted">The invoice file does not print a GST total, so it cannot be cross-checked.</p>'}
        ${probs.length ? `<h4>To fix</h4><ul class="issues">${probs.map((x) => `<li class="warn">${esc(x)}</li>`).join("")}</ul>` : '<p class="ok">No GST problems.</p>'}`,
      onSubmit: () => true });
  }

  function breakdown(t) {
    if (!t) return "";
    const parts = [`Taxable ₹${money(t.taxable)}`];
    if (Number(t.bill_discount)) parts.push(`− bill discount ₹${money(t.bill_discount)}`);
    parts.push(`+ GST ₹${money(t.gst)}`);
    for (const c of t.charges || []) if (c.code !== "bill_discount") parts.push(`${c.code === "credit" ? "−" : "+"} ${c.label} ₹${money(c.value)}`);
    return parts.join(" ") + ` = ₹${money(t.total)}`;
  }
  const shortBreakdown = (t) => (t ? `taxable ${money(t.taxable)} + GST ${money(t.gst)}${(t.charges || []).some((c) => c.code !== "bill_discount") ? " + charges" : ""}` : "");

  async function columns() {
    const p = doc.purchase;
    if (!p.column_map || !p.column_map.length) { ctx.status("This purchase was entered manually — there are no supplier columns", "warn"); return; }
    let fields = [];
    try { fields = (await api("/api/erp/purchases/fields")).fields; } catch (err) { ctx.status(err.message, "error"); return; }
    const opts = (cur) => `<option value="ignore" ${cur ? "" : "selected"}>— not used —</option>` +
      fields.map((f) => `<option value="${f.code}" ${f.code === cur ? "selected" : ""}>${esc(f.label)}</option>`).join("");
    const rows = p.column_map.map((c, i) => `<tr><td class="mono">${esc(c.column)}</td>
      <td><select name="c${i}" data-col="${esc(c.column)}" data-was="${esc(c.field)}">${opts(c.field)}</select></td>
      <td class="muted">${esc(c.reason)}</td></tr>`).join("");
    const out = await modal({
      title: "Supplier columns", wide: true, submitLabel: draft() ? "Save and re-read file" : "Close",
      body: `<p class="hint">How this supplier's file was understood. Change a column's meaning and the draft is re-read from the file;
        the choice is remembered for ${esc(p.supplier || "this supplier")} and used on every future import.
        ${draft() ? "Corrections already made on the lines are replaced by the re-read values." : ""}</p>
        <table class="rawtab coltab"><thead><tr><th>Supplier column</th><th>Read as</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>`,
      onOpen: (form) => { if (!draft()) form.querySelectorAll("select").forEach((s) => { s.disabled = true; }); },
      onSubmit: (form) => {
        if (!draft()) return true;
        const changes = {};
        form.querySelectorAll("select[data-col]").forEach((s) => { if (s.value !== (s.dataset.was || "ignore")) changes[s.dataset.col] = s.value; });
        if (!Object.keys(changes).length) return true;
        return api(`/api/erp/purchases/${id}/columns`, { method: "PUT", body: { changes } });
      },
    });
    if (out && out.lines) { render(out, { keep: false }); ctx.status("Columns saved for this supplier — file re-read", "ok"); }
    grid.focus();
  }

  function renderSide(r) {
    if (!r) { side.innerHTML = `<p class="hint">${isOpen() ? "F3 adds a line. F2 edits the invoice header." : ""}</p>`; return; }
    const p = r.physical || {};
    const reportRow = (label, value) => `<tr><th>${esc(label)}</th><td>${esc(value ?? '—')}</td></tr>`;
    const issues = (r.issues || []).filter(i => !i.accepted && i.level !== 'info').map(i => `<li>${esc(i.message)}</li>`).join('');
    const prod = `<b>${esc(r.item?.name || r.name)}</b><br><span class="muted">${esc(r.item?.code || r.supplier_code || '')}</span>`;
    const receipt = r.receipt || {};
    const facts = [reportRow('Form', p.form ? t(p.form) : '—'),
      reportRow('Received stock', r.stock?.stock), reportRow('Stock equivalent', r.stock?.equivalent),
      reportRow('Packaging', p.units_per_pack ? `1 ${t(p.pack_unit)} = ${p.units_per_pack} ${t(p.base_unit)}` : 'Not confirmed'),
      reportRow('Batch', r.batch || '—'), reportRow('Expiry', r.expiry ? fmtExp(r.expiry) : r.expiry_raw || '—'),
      reportRow('Billed quantity', receipt.paid ?? r.qty), reportRow('Free quantity on invoice', receipt.free ?? r.free ?? 0),
      reportRow('Retail MRP', `₹${money(r.mrp)}`)].join('');
    side.innerHTML = `
      <h3>Line ${r.line_no} report <span class="ps-${STATUS[r.status][1]}">${STATUS[r.status][0]}</span></h3>
      <div class="side-prod">${prod}</div><table class="rawtab"><tbody>${facts}</tbody></table>${costTable(r)}
      ${issues ? `<p><b>Needs attention</b></p><ul class="issues">${issues}</ul>` : '<p class="ok">Checks passed.</p>'}
      ${r.corrections?._physical_adjustment && r.status !== 'POSTED' ? '<p class="hint">Corrected packaging will apply to Inventory when this line is posted.</p>' : ''}`;
  }

  async function adjustQuantity(r) {
    if (!r || !canEdit() || DONE.includes(r.status)) return;
    const counted = r.corrections?._invoice_unit?.operator_counts;
    const count = r.units_per_pack || r.item?.upp || 1;
    const editor = physicalEditor({ form: r.dosage_form || '', base: r.item?.base_unit || r.base_unit,
      count, quantity: counted ? Number(counted.paid) / count : r.receipt?.resolved ? Number(r.receipt.paid_base_equivalent) / count : r.receipt?.paid ?? r.qty,
      free: counted ? Number(counted.free) / count : r.receipt?.resolved ? Number(r.receipt.free_base_equivalent) / count : r.receipt?.free ?? r.free ?? 0 });
    const body = h('<div><p>Count the stock you receive. The invoice billed quantity, rate and amount stay unchanged. MRP is per retail strip/container. Packaging corrections are applied when this purchase is posted.</p></div>');
    body.append(editor);
    const out = await modal({ title: `Adjust line ${r.line_no} — ${r.name}`, body, submitLabel: 'Save adjustment',
      onSubmit: () => api(`/api/erp/purchases/${id}/lines/${r.id}/physical-quantity`, { method: 'PUT', body: editor.packagingValues() }) });
    if (out) { await load(); ctx.status('Physical quantity and equivalent updated', 'ok'); }
  }

  async function verifyInvoiceUnit(r) {
    if (!guard()) return;
    const saved = r.corrections?._invoice_unit || {};
    const out = await modal({ title: `Invoice unit: line ${r.line_no}`, submitLabel: "Confirm conversion",
      body: `<p>Supplier pack: <b>${esc(r.pack || "not supplied")}</b>. One invoice Qty represents how many physical ${esc(t(r.receipt?.base_unit || "UNIT"))}s?</p>
        <label>Base units per invoice Qty<input name="factor" type="number" min="1" max="1000000" step="1" required value="${esc(saved.units_per_invoice_unit || "")}" autofocus></label>
        <label>The printed MRP is per<select name="basis"><option value="MASTER_PACK">Product's retail pack / strip</option><option value="INVOICE_UNIT">Invoice unit / box</option><option value="BASE">Single base unit / tablet</option></select></label>
        <label>How was this verified?<input name="reason" required minlength="5" maxlength="500" placeholder="e.g. Supplier confirmed Qty counts strips of 10"></label>
        <p class="hint">After posting, this convention is remembered for this supplier, product, pack and file layout. Changes require fresh verification.</p>`,
      onOpen: form => { form.elements.basis.value = saved.mrp_basis || "MASTER_PACK"; },
      onSubmit: form => api(`/api/erp/purchases/${id}/lines/${r.id}/invoice-unit`, {method: "PUT", body: {
        units_per_invoice_unit: form.elements.factor.value, mrp_basis: form.elements.basis.value, reason: form.elements.reason.value}}) });
    if (out) render(out);
    grid.focus();
  }

  async function showReference(r) {
    try {
      const data = await api(`/api/erp/medicine-reference?q=${encodeURIComponent(r.name)}`);
      await modal({title: "Medicine packaging references", submitLabel: "Close", wide: true,
        body: `<p>Reference data supports identification. Confirm the exact brand, strength, formulation, manufacturer and current pack against the invoice.</p>${data.candidates.length ? data.candidates.map(c => `<p><b>${esc(c.name)}</b><br>${esc(c.manufacturer)} · ${esc(c.pack)}${c.discontinued ? " · marked discontinued in source" : ""}<br><small>${esc(c.source)} row ${c.source_row} · ${esc(c.match)}</small></p>`).join("") : "<p>No reference found. Load the local medicine catalogue with the supplied import command.</p>"}`,
        onSubmit: () => true});
    } catch (err) { ctx.status(err.message, "error"); }
    grid.focus();
  }

  // what one pack cost: invoice rate → after discounts → + GST → rate incl. GST (stock is costed at this)
  function costTable(r) {
    if (!r.landed) return "";
    const g = doc?.summary?.gst || {}, c = r.cost || {};
    const split = Number(r.igst) ? `IGST ₹${money(r.igst)}` : `CGST ₹${money(r.cgst)} + SGST ₹${money(r.sgst)}`;
    const per = (v, unit) => v != null ? `₹${money(v)} per ${esc(t(unit))}` : "";
    const head = c.per_pack != null ? per(c.per_pack, c.pack_unit) : per(c.per_invoice_unit, c.invoice_unit) || "—";
    const extra = [c.per_pack != null && c.invoice_unit !== c.pack_unit ? per(c.per_invoice_unit, c.invoice_unit) : "", per(c.per_base, c.base_unit)].filter(Boolean).join(" · ");
    return `<table class="costtab"><tbody>
      <tr><th>Invoice billed rate</th><td class="num">₹${money(r.rate)}</td></tr>
      <tr><th>Taxable value${Number(r.discount) ? " (after discount / scheme)" : ""}</th><td class="num">₹${money(r.taxable)}</td></tr>
      <tr><th>GST ${r.gst !== "" ? Number(r.gst) + "%" : "0% (not on invoice)"}</th><td class="num">+ ₹${money(r.gst_amount)}<br><small class="muted">${split}</small></td></tr>
      <tr><th>Value incl. GST</th><td class="num">₹${money(r.landed)}</td></tr>
      <tr class="total"><th>Cost incl. GST</th><td class="num"><b>${head}</b>${extra ? `<br><small class="muted">${extra}</small>` : ""}</td></tr>
    </tbody></table>${r.gst_worked_out ? `<p class="hint">Worked out from the invoice. This line was posted before GST tracking, so its stock was costed before GST at ₹${money(r.batch_rate || r.rate)} per pack — not re-costed.</p>`
      : g.cost_includes_gst === false ? '<p class="hint">Stock is costed before GST (purchase_cost_includes_gst is off).</p>' : ""}`;
  }

  // ---------------------------------------------------------------- actions
  async function run(fn, ok) {
    try { const d = await fn(); render(d); if (ok) ctx.status(ok, "ok"); return d; }
    catch (err) { ctx.status(err.message, "error"); return null; }
  }
  const load = () => run(() => api(`/api/erp/purchases/${id}`));
  const guard = (headerToo = false) => {
    if (!isOpen() || (headerToo && !draft())) {
      ctx.status(doc?.purchase.status === "PARTIAL" ? "Part of this invoice is already in stock — the header is fixed; open lines can still be corrected"
        : `This purchase is ${DOC[doc?.purchase.status] || ""} — it can no longer change`, "warn");
      return false;
    }
    if (!CAN["purchase.create"]) { ctx.status("Correcting purchases needs purchase rights", "warn"); return false; }
    return true;
  };
  const putLine = (r, body, ok) => run(() => api(`/api/erp/purchases/${id}/lines/${r.id}`, { method: "PUT", body }), ok);

  function fieldInputs(r, { blank = false } = {}) {
    return FIELDS.map(([f, label, cls]) => {
      const key = { quantity: "qty" }[f] || f;
      let value = blank ? "" : r[key] ?? "";
      if (f === "expiry" && !blank) value = r.expiry ? fmtExp(r.expiry) : r.expiry_raw || "";
      if (f === "discount" && !blank) value = (r.corrections?.discount?.value ?? r.raw?.discount ?? "");
      const raw = blank ? undefined : r.raw?.[f];
      const hint = raw !== undefined && String(raw) !== String(value) ? `<small class="muted">Supplier: ${esc(raw || "(blank)")}</small>` : "";
      return `<label class="${cls || ""}">${esc(label)}<input name="${f}" value="${esc(value)}" autocomplete="off" spellcheck="false">${hint}</label>`;
    }).join("");
  }
  function changed(form, r) {
    const out = {};
    for (const [f] of FIELDS) {
      const el = form.elements[f];
      if (el && el.value !== el.defaultValue) out[f] = el.value.trim();
    }
    return out;
  }

  async function editLine(r) {
    if (!r || !guard()) return;
    const issues = (r.issues || []).filter((i) => !i.accepted).map((i) => `<li class="${i.level === "warn" ? "warn" : i.level === "info" ? "muted" : "bad"}">${esc(i.message)}</li>`).join("");
    const out = await modal({
      title: `Correct line ${r.line_no}`, wide: true, submitLabel: "Save correction",
      body: `${issues ? `<ul class="issues">${issues}</ul>` : ""}<div class="form-grid three">${fieldInputs(r)}</div>
        ${r.new_product ? newProductFields(r) : ""}
        <p class="hint">The supplier's original value is kept; your correction is recorded with your name and time.</p>`,
      onOpen: (form) => { wireNewProduct(form); const f = (r.issues || []).find((i) => !i.accepted); focusField(form, f && form.elements[f.field]); },
      onSubmit: (form) => {
        const body = { ...changed(form, r), ...(r.new_product ? newProductValues(form) : {}) };
        if (!Object.keys(body).length) return null;
        return api(`/api/erp/purchases/${id}/lines/${r.id}`, { method: "PUT", body });
      },
    });
    if (out) { render(out); ctx.status(`Line ${r.line_no}: ${STATUS[out.lines.find((l) => l.id === r.id).status][0]}`, "ok"); }
    grid.focus();
  }

  function newProductFields(r) {
    const cats = BOOT.category_options || [];
    const opt = (list, v) => list.map((x) => `<option value="${esc(x)}" ${x === v ? "selected" : ""}>${esc(t(x) || "—")}</option>`).join("");
    return `<fieldset class="form-grid three np"><legend>New product</legend>
      <label>Category<select name="np_category"><option value="">General (default)</option>${cats.map((c) => `<option value="${esc(c.code)}" ${c.code === r.category ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
      <label>Form<select name="np_form">${formOptions(r.dosage_form || r.base_unit ? formFor(r.dosage_form, r.base_unit) : "", { blank: true })}</select></label>
      <label>Units per pack<input name="np_units_per_pack" inputmode="numeric" value="${esc(r.units_per_pack ?? "")}"></label>
      <label>Sale unit (base)<select name="np_base_unit">${opt(BOOT.units.base || [], r.base_unit || "UNIT")}</select></label>
      <label>Purchase unit (pack)<select name="np_pack_unit">${opt(BOOT.units.pack || [], r.pack_unit || "PACK")}</select></label>
      <p class="hint">Detected from pack “${esc(r.pack || "—")}” — confirm before posting.</p></fieldset>`;
  }
  const newProductValues = (form) => {
    const out = Object.fromEntries(["category", "units_per_pack", "base_unit", "pack_unit"]
      .map((k) => [k, form.elements["np_" + k] ? form.elements["np_" + k].value : undefined]).filter(([, v]) => v !== undefined));
    if (form.elements.np_form) out.dosage_form = (itemForms().find((f) => f.code === form.elements.np_form.value) || {}).dosage_form ?? "";
    return out;
  };
  /** Choosing a form fills the sale and purchase units it is stocked with. */
  function wireNewProduct(form) {
    const sel = form.elements.np_form;
    if (!sel) return;
    wireFormSelect(sel, () => {
      const f = itemForms().find((x) => x.code === sel.value);
      if (!f) return;
      for (const [k, v] of [["np_base_unit", f.base_unit], ["np_pack_unit", f.pack_unit]]) {
        const el = form.elements[k];
        if (el && [...el.options].some((o) => o.value === v)) el.value = v;
      }
      if (!f.counted && form.elements.np_units_per_pack) form.elements.np_units_per_pack.value = "1";
    });
  }

  async function bulkMatch(marked) {
    let sug = {};
    try { sug = (await api(`/api/erp/purchases/${id}/lines/suggest`, { method: "POST", body: { line_ids: marked.map((m) => m.id) } })).suggestions; }
    catch (err) { ctx.status(err.message, "error"); return; }
    const rows = marked.map((m, i) => {
      const list = sug[String(m.id)] || [];
      const top = list[0];
      return `<tr><td><input type="checkbox" name="use${i}" data-line="${m.id}" ${top && top.score >= 75 ? "checked" : ""} ${top ? "" : "disabled"}></td>
        <td>${m.line_no}</td><td>${esc(m.name)} <span class="muted">${esc(m.pack || "")}</span></td>
        <td>${top ? `<select name="pick${i}">${list.map((x) => `<option value="${x.item_id}">${esc(x.name)} · ${x.score}%${x.note ? " · " + esc(x.note) : ""}</option>`).join("")}</select>`
          : '<span class="muted">no similar product — create it as new (Shift+F4)</span>'}</td></tr>`;
    }).join("");
    const strong = marked.filter((m) => { const t = (sug[String(m.id)] || [])[0]; return t && t.score >= 75; }).length;
    const out = await modal({
      title: `Match ${marked.length} selected line(s)`, wide: true, submitLabel: "Apply ticked matches",
      body: `<p class="hint">Suggestions are ranked by name similarity (strength and pack differences lower the score). Nothing is applied
        until you tick it; ${strong} strong match(es) are pre-ticked. Confirmed matches are remembered for this supplier after posting.</p>
        <table class="rawtab coltab"><thead><tr><th></th><th>#</th><th>Supplier description</th><th>Our product</th></tr></thead><tbody>${rows}</tbody></table>`,
      onSubmit: (form) => {
        const matches = {};
        marked.forEach((m, i) => { const c = form.elements["use" + i]; if (c && c.checked) matches[m.id] = form.elements["pick" + i].value; });
        if (!Object.keys(matches).length) throw new Error("Tick at least one match (Space on a checkbox)");
        return api(`/api/erp/purchases/${id}/lines/bulk`, { method: "POST", body: { matches } });
      },
    });
    if (out) { grid.clearMarks(); render(out); ctx.status(`${out.result.changed} line(s) matched`, "ok"); }
    grid.focus();
  }

  async function bulkNew(marked) {
    const cats = BOOT.category_options || [];
    const out = await modal({
      title: `Create ${marked.length} selected line(s) as new products`, wide: true, submitLabel: "Mark as new products",
      body: `<div class="form-grid"><label class="full">Category for all of them<select name="category" autofocus><option value="">General (default)</option>
        ${cats.map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("")}</select></label>
        <p class="full hint">Units are detected from each line's pack (15S → strip of 15 tablets, 200ML → bottle). A line whose pack cannot be
        read with certainty stays in review for you to confirm. Products are created only when the purchase is posted.</p></div>
        <table class="rawtab"><thead><tr><th>#</th><th>Description</th><th>Pack</th></tr></thead><tbody>
        ${marked.slice(0, 200).map((m) => `<tr><td>${m.line_no}</td><td>${esc(m.name)}</td><td class="mono">${esc(m.pack || "—")}</td></tr>`).join("")}</tbody></table>`,
      onSubmit: (form) => {
        return api(`/api/erp/purchases/${id}/lines/bulk`, { method: "POST", body: { line_ids: marked.map((m) => m.id), changes: { new_product: true, category: form.category.value } } });
      },
    });
    if (out) {
      grid.clearMarks(); render(out);
      const st = out.result.statuses || {};
      const review = (st.NEEDS_REVIEW || 0) + (st.PRODUCT_MATCH_REQUIRED || 0);
      ctx.status(`${out.result.changed} line(s) set as new products${review ? ` · ${review} need a look (units or other issues) — F8 goes to them` : ""}`, review ? "warn" : "ok");
    }
    grid.focus();
  }

  async function matchProduct(r) {
    if (!guard()) return;
    const marked = selectedRows().filter((m) => !DONE.includes(m.status));
    if (marked.length > 1) return bulkMatch(marked);
    if (!r) return;
    const item = await pickProduct({ title: `Match line ${r.line_no}: ${r.name}`, suggestionsUrl: `/api/erp/purchases/${id}/lines/${r.id}/suggestions` });
    if (item) await putLine(r, { item_id: item.id }, `Line ${r.line_no} → ${item.name} (remembered for this supplier after posting)`);
    grid.focus();
  }

  async function newProduct(r) {
    if (!guard()) return;
    const marked = selectedRows().filter((m) => !DONE.includes(m.status) && !m.item);
    if (marked.length > 1) return bulkNew(marked);
    if (!r) return;
    let d = r;
    if (!r.new_product) {
      const out = await putLine(r, { new_product: true });
      if (!out) return;
      d = out.lines.find((l) => l.id === r.id);
    }
    const out = await modal({
      title: `New product from line ${d.line_no}`, wide: true, submitLabel: "Save",
      body: `<div class="form-grid"><label class="full">Product name<input name="name" value="${esc(d.name)}" autofocus></label></div>${newProductFields(d)}
        <p class="hint">The product is created only when the purchase is posted.</p>`,
      onOpen: wireNewProduct,
      onSubmit: (form) => {
        const body = newProductValues(form);
        if (form.elements.name.value.trim() !== d.name) body.name = form.elements.name.value.trim();
        return api(`/api/erp/purchases/${id}/lines/${d.id}`, { method: "PUT", body });
      },
    });
    if (out) render(out);
    grid.focus();
  }

  async function accept(r) {
    if (!guard()) return;
    const marked = selectedRows();
    if (marked.length) {
      let n = 0;
      for (const m of marked) {
        const codes = (m.issues || []).filter((i) => i.level === "warn" && !i.accepted).map((i) => i.code);
        if (!codes.length) continue;
        const out = await putLine(m, { accept: codes });
        if (!out) return;
        n++;
      }
      ctx.status(n ? `Warnings accepted on ${n} line(s)` : "No warnings to accept on the selected lines", n ? "ok" : "warn");
      return;
    }
    if (!r) return;
    const codes = (r.issues || []).filter((i) => i.level === "warn" && !i.accepted).map((i) => i.code);
    if (!codes.length) { ctx.status("Nothing to accept — only warnings can be accepted; other issues must be corrected", "warn"); return; }
    await putLine(r, { accept: codes }, `Line ${r.line_no}: warnings accepted`);
  }

  async function removeLine(r) {
    if (!guard()) return;
    const marked = selectedRows();
    if (marked.length > 1) {
      if (!(await window.erpConfirm(`Remove ${marked.length} selected lines?`))) return;
      for (const m of marked) if (!(await run(() => api(`/api/erp/purchases/${id}/lines/${m.id}`, { method: "DELETE" })))) return;
      ctx.status(`${marked.length} lines removed`, "ok");
      grid.focus();
      return;
    }
    if (!r || DONE.includes(r.status)) return;
    if (!(await window.erpConfirm(`Remove line ${r.line_no} (${r.name})?`))) return;
    await run(() => api(`/api/erp/purchases/${id}/lines/${r.id}`, { method: "DELETE" }), "Line removed");
    grid.focus();
  }

  async function addLine() {
    if (!guard()) return;
    const item = await pickProduct({ title: "Add line — choose product or enter manually", allowManual: true });
    if (!item) return;
    if (item.manual) return addManualLine(item.name);
    const out = await modal({
      title: `Add ${item.name}`, wide: true, submitLabel: "Add line",
      body: `<div class="form-grid three">${fieldInputs({ name: item.name, pack: item.pack || "", manufacturer: item.manufacturer || "" }, { blank: false })}</div>
        <p class="hint">Quantities are in purchase packs (${esc(t(item.pack_unit))} of ${item.upp} ${esc(t(item.base_unit))}s).</p>`,
      onOpen: (form) => focusField(form, form.elements.batch),
      onSubmit: (form) => {
        const body = { item_id: item.id };
        for (const [f] of FIELDS) if (form.elements[f].value.trim()) body[f] = form.elements[f].value.trim();
        return api(`/api/erp/purchases/${id}/lines`, { method: "POST", body });
      },
    });
    if (out) { render(out, { keep: false }); grid.select(out.lines.findIndex((l) => l.id === out.line_id)); ctx.status("Line added", "ok"); }
    grid.focus();
  }

  /** A line typed in full by hand — no product chosen yet. It is matched (F4) or created as new (Shift+F4) before posting. */
  async function addManualLine(name) {
    const out = await modal({
      title: "Add line — manual entry", wide: true, submitLabel: "Add line",
      body: `<div class="form-grid three">${fieldInputs({ name }, { blank: false })}</div>
        <label class="chk"><input type="checkbox" name="as_new" checked> Create as a new product when posted (untick to match an existing product later with ${esc(keys.keyFor("purchase.product") || "F4")})</label>
        <p class="hint">Quantities are in purchase packs. Every value you type is kept on the line and can be corrected before posting.</p>`,
      onOpen: (form) => focusField(form, form.elements.name.value ? form.elements.pack : form.elements.name),
      onSubmit: (form) => {
        if (!form.elements.name.value.trim()) { focusField(form, form.elements.name); throw new Error("Type the product description"); }
        const body = {};
        for (const [f] of FIELDS) if (form.elements[f].value.trim()) body[f] = form.elements[f].value.trim();
        return api(`/api/erp/purchases/${id}/lines`, { method: "POST", body }).then((d) => ({ ...d, as_new: form.elements.as_new.checked }));
      },
    });
    if (!out) { grid.focus(); return; }
    render(out, { keep: false });
    grid.select(out.lines.findIndex((l) => l.id === out.line_id));
    const line = out.lines.find((l) => l.id === out.line_id);
    if (out.as_new && line) return newProduct(line);
    ctx.status(`Line ${line ? line.line_no : ""} added — ${keys.keyFor("purchase.product") || "F4"} matches it to a product`, "ok");
    grid.focus();
  }

  async function editHeader() {
    if (!guard(true)) return;
    let suppliers = [];
    try { suppliers = (await api("/api/erp/suppliers")).suppliers; } catch (err) { ctx.status(err.message, "error"); return; }
    const p = doc.purchase;
    const out = await modal({
      title: "Invoice header", submitLabel: "Save",
      body: `<div class="form-grid">
        <label class="full">Supplier<select name="supplier_id" autofocus><option value="">Choose…</option>${suppliers.map((s) => `<option value="${s.id}" ${s.id === p.supplier_id ? "selected" : ""}>${esc(s.name)}${s.gst_number ? " · " + esc(s.gst_number) : ""}</option>`).join("")}</select></label>
        <label>Supplier invoice no.<input name="invoice_no" value="${esc(p.invoice_no)}" maxlength="60"></label>
        <label>Invoice date<input name="invoice_date" type="date" value="${esc(p.invoice_date)}"></label>
        <label>Supplier's invoice total ₹<input name="supplier_total" inputmode="decimal" value="${esc(p.supplier_total)}"></label>
        <fieldset class="full form-grid three"><legend>Invoice charges (from the file; correct if needed)</legend>
          ${[["bill_discount", "Bill discount %"], ["freight", "Freight ₹"], ["adjust", "Adjustment ₹"], ["debit", "Debit note ₹"], ["credit", "Credit note ₹"], ["round_off", "Round off ₹"]]
            .map(([k, label]) => `<label>${label}<input name="ch_${k}" inputmode="decimal" value="${esc((p.charges || {})[k] || "")}"></label>`).join("")}</fieldset>
        <label class="full">Notes<input name="notes" value="${esc(p.notes)}"></label>
        <p class="full hint">New supplier? Purchases → Suppliers (F7) adds one.</p></div>`,
      onSubmit: (form) => api(`/api/erp/purchases/${id}`, { method: "PUT", body: {
        supplier_id: form.supplier_id.value, invoice_no: form.invoice_no.value, invoice_date: form.invoice_date.value,
        supplier_total: form.supplier_total.value, notes: form.notes.value,
        charges: Object.fromEntries(["bill_discount", "freight", "adjust", "debit", "credit", "round_off"].map((k) => [k, form.elements["ch_" + k].value])) } }),
    });
    if (out) { render(out); ctx.status("Header saved — lines re-checked", "ok"); }
    grid.focus();
  }

  async function postDoc(acceptDifference = false, lineIds = null) {
    if (!guard()) return;
    if (!CAN["purchase.post"]) { ctx.status("Posting purchases needs the purchase post right", "warn"); return; }
    const s = doc.summary;
    if (lineIds === null && selectedRows().length) {
      const marked = selectedRows();
      const notReady = marked.filter((r) => !postableRow(r));
      if (notReady.length) {
        grid.select(lines.indexOf(notReady[0]));
        ctx.status(`${notReady.length} selected line(s) are not ready (line ${notReady.map((r) => r.line_no).slice(0, 8).join(", ")}) — correct them or unmark with Space`, "warn");
        return;
      }
      lineIds = marked.map((r) => r.id);
    }
    if (lineIds === null && !s.postable && s.blocking && doc.purchase.supplier && doc.purchase.invoice_no) {
      // some lines need a person: offer to post every line that is ready (they wait, the rest go to stock)
      const ready = lines.filter((l) => !DONE.includes(l.status) && postableRow(l));
      if (ready.length && await window.erpConfirm(`${s.blocking} line(s) still need you (shown under Needs attention). Post the other ${ready.length} ready line(s) now?`))
        return postDoc(acceptDifference, ready.map((l) => l.id));
    }
    if (lineIds === null && !s.postable) {
      nextIssue();
      ctx.status(s.blocking ? `${s.blocking} line(s) still need review — or select the ready lines (Shift+↑↓) and post just those`
        : "Set the supplier and invoice number (F2) before posting", "warn");
      return;
    }
    const count = lineIds ? lineIds.length : s.open;
    const value = lineIds ? lines.filter((l) => lineIds.includes(l.id)).reduce((a, l) => a + Number(l.amount || 0), 0) : Number(s.calculated_total);
    const rest = lineIds ? s.open - lineIds.length : 0;
    const scope = lineIds ? lines.filter((l) => lineIds.includes(l.id)) : lines.filter((l) => !DONE.includes(l.status));
    const proposed = scope.filter(isProposed).length;
    const warned = scope.filter((l) => l.status === "NEEDS_REVIEW" && gateOf(l) === "AUTO_ACCEPT_WITH_WARNING").length;
    if (!acceptDifference && !(await window.erpConfirm(`Post ${count} line(s) of ${doc.purchase.invoice_no} into stock (₹${money(value)}${lineIds ? " before GST" : ""})?`
      + (proposed ? ` ${proposed} proposed count(s) are accepted as shown.` : "")
      + (warned ? ` ${warned} line(s) with routine warnings (expiry soon, GST changed …) are accepted.` : "")
      + (rest ? ` ${rest} line(s) stay open for review.` : "") + " Posted lines can be rolled back to draft (Alt+B) while their stock is untouched."))) return;
    try {
      const out = await api(`/api/erp/purchases/${id}/post`, { method: "POST", body: { accept_difference: acceptDifference, accept_warnings: true, ...(lineIds ? { line_ids: lineIds } : {}) } });
      grid.clearMarks();
      render(out);
      ctx.status(out.purchase.status === "PARTIAL" ? `${count} line(s) received under ${out.purchase.reference_no} · ${out.summary.open} still open`
        : `Posted ${out.purchase.reference_no} — stock received`, "ok");
    } catch (err) {
      if (err.detail && err.detail.code === "TOTAL_DIFFERENCE") {
        if (await window.erpConfirm(`${err.message}\n\nPost with the difference acknowledged (recorded in the audit trail)?`)) return postDoc(true, lineIds);
      } else ctx.status(err.message, "error");
    }
    grid.focus();
  }

  /** The new-product queue: products prepared from the invoice (units read from the pack) waiting for a person. */
  const queued = () => lines.filter((l) => !DONE.includes(l.status) && (l.issues || []).some((i) => i.code === "new_product_unconfirmed" && !i.accepted));
  async function confirmNewProducts() {
    const rows = markedRows().filter((r) => queued().includes(r)).length ? markedRows().filter((r) => queued().includes(r)) : queued();
    if (!rows.length) { ctx.status("No new products are waiting", "ok"); return; }
    const picked = await pickCategory({ title: `Confirm ${rows.length} new product(s)`, wide: true, submitLabel: "Confirm",
      intro: `${rows.length} product(s) are not in the catalogue as printed. Each keeps the units read from its pack
        (shown below); the products are created when the purchase is posted. Choose their category to confirm them.`,
      extra: `<table class="rawtab"><thead><tr><th>#</th><th>Product</th><th>Pack</th><th>Units</th></tr></thead><tbody>
      ${rows.slice(0, 300).map((r) => `<tr><td>${r.line_no}</td><td>${esc(r.name)}</td><td class="mono">${esc(r.pack || "—")}</td>
        <td>${r.units_per_pack ? `1 ${esc(t(r.pack_unit || "pack").toLowerCase())} = ${r.units_per_pack} ${esc(t(r.base_unit || "unit").toLowerCase())}` : "—"}</td></tr>`).join("")}</tbody></table>
      <p class="hint">A product that already exists under another name should be matched instead (F4) — matching is remembered for this supplier.</p>`,
      apply: (code) => api(`/api/erp/purchases/${id}/lines/bulk`, { method: "POST", body: { line_ids: rows.map((r) => r.id), changes: { new_product: true, category: code } } }) });
    const out = picked && picked.result;
    if (out) { grid.clearMarks(); render(out); ctx.status(`${out.result.changed} new product(s) confirmed`, "ok"); }
    grid.focus();
  }

  /** Change only the category of the selected lines (or the current one): one click on a category applies it. */
  async function changeCategory() {
    if (CAN["purchase.create"] === false) { ctx.status("Changing categories needs purchase rights", "warn"); return; }
    const rows = markedRows().length ? markedRows() : grid.selected ? [grid.selected] : [];
    if (!rows.length) { ctx.status("Select lines first (Space marks one, Shift+↑↓ several)", "warn"); return; }
    const now = [...new Set(rows.map((r) => r.product_category).filter(Boolean))];
    const picked = await pickCategory({ title: "Change category only", current: now.length === 1 ? now[0] : "",
      intro: `${rows.length} line(s) · now ${now.length ? now.map((c) => esc(catName(c))).join(", ") : "no category"}. Only the category changes —
        quantities, prices and stock stay as they are. Matched products change in the product master; new products get it when created.`,
      apply: (code) => api(`/api/erp/purchases/${id}/lines/category`, { method: "POST", body: { line_ids: rows.map((r) => r.id), category: code } }) });
    const out = picked && picked.result;
    if (out) {
      render(out);
      ctx.status(`Category ${catName(out.result.category)} on ${out.result.lines} line(s)${out.result.products ? ` · ${out.result.products} product(s) updated` : ""}`, "ok");
    }
    grid.focus();
  }

  /** Posted lines back to review: their stock is taken out again through the ledger (all or nothing). */
  async function rollback() {
    if (!doc || !["POSTED", "PARTIAL"].includes(doc.purchase.status)) { ctx.status("Only a posted purchase can be rolled back", "warn"); return; }
    if (!CAN["purchase.post"]) { ctx.status("Rolling back a purchase needs the purchase post right", "warn"); return; }
    const posted = markedRows().filter((r) => r.status === "POSTED");
    const all = lines.filter((r) => r.status === "POSTED");
    const scope = posted.length ? posted : all;
    const out = await modal({
      title: posted.length ? `Roll back ${posted.length} selected line(s) to draft` : `Roll back ${doc.purchase.reference_no} to draft`, submitLabel: "Roll back",
      body: `<div class="form-grid"><p class="full">${scope.length} posted line(s) will be taken out of stock and returned to review with every correction kept.
        ${posted.length && posted.length < all.length ? `${all.length - posted.length} other posted line(s) stay in stock.` : "The purchase becomes a draft again."}
        If any of that stock was already sold, returned or adjusted, nothing changes — use a purchase return instead.</p>
        <label class="full">Reason<input name="reason" required minlength="5" maxlength="300" autofocus placeholder="e.g. Wrong quantities posted"></label></div>`,
      onSubmit: (form) => api(`/api/erp/purchases/${id}/rollback`, { method: "POST", body: { reason: form.reason.value, ...(posted.length ? { line_ids: posted.map((r) => r.id) } : {}) } }),
    });
    if (out) { markedIds.clear(); grid.clearMarks(); render(out); ctx.status(`${scope.length} line(s) rolled back · ${DOC[out.purchase.status]}`, "ok"); }
    grid.focus();
  }

  /** Correction dialog for a questionable pack: what one invoice Qty is, saved for this invoice, the product or the supplier. */
  async function packagingDialog(r) {
    if (!r || !guard()) return;
    let p;
    try { p = await api(`/api/erp/purchases/${id}/lines/${r.id}/packaging`); } catch (err) { ctx.status(err.message, "error"); return; }
    const rd = p.reading || {}, lv = (x) => (x ? `${x.quantity} ${t(x.unit || "unit").toLowerCase()}` : "—");
    let scope = "supplier";
    const body = h(`<div class="pack-fix">
      <p>Supplier packing: <b class="mono">${esc(p.raw || "(none)")}</b> · billed ${esc(r.stock?.quantity || "")} + free ${esc(r.receipt?.free ?? "0")} — the received quantity does not change here.</p>
      <p class="muted">Read as: purchase ${esc(lv(rd.purchase))} · retail ${esc(lv(rd.retail))} · base ${esc(lv(rd.base))}${p.container ? ` · container ${esc(p.container.quantity)} ${esc(String(p.container.unit).toLowerCase().replace("gram", "g"))}` : ""}
        · ${esc(rd.confidence || "")}${(rd.issues || []).length ? ` — ${esc(rd.issues[0])}` : ""}</p>
      <div class="form-grid three">
        <label class="full">Item form<select name="form">${formOptions(p.form ? formFor(p.form, p.base_unit) : formFor("", p.base_unit))}</select></label>
        <label>Units per retail pack<input name="units_per_retail" type="number" min="1" max="10000" value="${esc(p.units_per_retail)}"></label>
        <label>Retail packs per box<input name="retail_per_outer" type="number" min="1" max="1000" value="${esc(p.retail_per_outer)}"></label>
        <fieldset class="full"><legend>One invoice Qty is</legend>
          <label class="chk"><input type="radio" name="level" value="RETAIL"> one retail pack (strip / bottle / tube)</label>
          <label class="chk"><input type="radio" name="level" value="OUTER"> one outer box of retail packs</label>
          <label class="chk"><input type="radio" name="level" value="BASE"> one base unit (tablet / piece)</label></fieldset>
        <label class="full">How was this checked?<input name="reason" maxlength="300" placeholder="e.g. Carton says 10 strips of 10"></label>
      </div>
      <p class="pack-preview hint" aria-live="polite"></p>
      <div class="pack-scope"><button type="button" class="btn" data-scope="invoice">Save for this invoice</button>
        <button type="button" class="btn" data-scope="product">Save as product packaging</button>
        <button type="button" class="btn primary" data-scope="supplier">Save supplier alias <kbd>Enter</kbd></button></div>
      <p class="hint">Saving writes the mapping store only: the next invoice with this packing from this supplier resolves without review.</p></div>`);
    const f = (n) => body.querySelector(`[name="${n}"]`);
    body.querySelector(`[name="level"][value="${p.level}"]`).checked = true;
    const preview = () => {
      const n = Number(f("units_per_retail").value) || 1, outer = Number(f("retail_per_outer").value) || 1;
      const level = body.querySelector('[name="level"]:checked').value, qty = Number(r.stock?.quantity?.replace(/,/g, "") || 0);
      const per = { RETAIL: n, OUTER: n * outer, BASE: 1 }[level];
      body.querySelector(".pack-preview").textContent = `${qty} × ${per} = ${toBase(qty, per)} stock units`;
    };
    body.addEventListener("input", preview); body.addEventListener("change", preview); preview();
    wireFormSelect(f("form"), preview);
    body.querySelectorAll("[data-scope]").forEach((b) => { b.onclick = () => { scope = b.dataset.scope; body.closest("form")?.requestSubmit(); }; });
    const out = await modal({ title: `Packing of line ${r.line_no} — ${r.name}`, body, wide: true, submitLabel: "Save supplier alias",
      onSubmit: () => api(`/api/erp/purchases/${id}/lines/${r.id}/packaging`, { method: "PUT", body: {
        form: f("form").value, units_per_retail: f("units_per_retail").value, retail_per_outer: f("retail_per_outer").value,
        level: body.querySelector('[name="level"]:checked').value, scope, reason: f("reason").value } }) });
    if (out) { render(out); ctx.status(`Line ${r.line_no}: packing saved ${scope === "invoice" ? "for this invoice" : scope === "product" ? "as product packaging" : "as a supplier alias"}`, "ok"); }
    grid.focus();
  }

  /** Match Inspector: why the line got its product and pack, and the next best candidates. */
  async function inspector(r) {
    if (!r) return;
    let d;
    try { d = await api(`/api/erp/purchases/${id}/lines/${r.id}/inspect`); } catch (err) { ctx.status(err.message, "error"); return; }
    const sig = (s) => Object.entries(s.signals).map(([k, v]) => `<span class="sig ${v >= 0.95 ? "ok" : v <= 0.05 ? "bad" : "muted"}">${esc(k)} ${Math.round(v * 100)}</span>`).join(" ");
    const m = d.matched;
    const pk = d.packaging || {};
    await modal({ title: `Match inspector — line ${d.line}`, wide: true, submitLabel: "Close", onSubmit: () => true,
      body: `<p><b>${esc(d.description)}</b></p>
        ${m ? `<p>Matched to <b>${esc(m.name)}</b> (${esc(matchLabel(m.method || ""))}) · confidence ${Math.round(m.score * 100)}%</p>
          <ul class="inspect">${m.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><p>${sig(m)}</p>` : '<p class="warn">No product matched.</p>'}
        ${d.candidates.length ? `<h4>Other candidates</h4><table class="grid-lite"><tbody>${d.candidates.map((c) => `<tr><td>${esc(c.name)}</td><td class="num">${Math.round(c.score * 100)}%${c.capped ? " ⚠" : ""}</td><td>${esc(c.reasons.join("; "))}</td></tr>`).join("")}</tbody></table>` : ""}
        <h4>Packing</h4><p>${esc(pk.raw || "—")} → ${esc(pk.handler || "")}: ${(pk.levels || []).map((l) => `${l.quantity} ${esc((l.unit || "").toLowerCase())}`).join(" × ") || "not read"} · ${esc(pk.confidence || "")}
          ${pk.decision?.resolved ? ` · conversion from ${esc(pk.decision.source.toLowerCase().replaceAll("_", " "))}` : " · conversion not confirmed yet"}</p>
        ${(pk.decision?.evidence || []).length ? `<ul class="inspect">${pk.decision.evidence.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        ${d.gate ? `<h4>Confidence</h4><p>${esc(GATE[d.gate.state][0])} · ${Object.entries(d.gate.fields).map(([k, v]) => `${esc(FIELD_NAMES[k] || k)} ${Math.round(v * 100)}% <small class="muted">${esc(d.gate.provenance[k] || "")}</small>`).join(" · ")}</p>` : ""}` });
    grid.focus();
  }

  async function deleteDoc() {
    if (!guard(true)) return;
    if (!(await window.erpConfirm("Delete this purchase draft and all its unreceived lines?"))) return;
    try { await api(`/api/erp/purchases/${id}`, { method: "DELETE" }); ctx.status("Purchase draft deleted", "ok"); ctx.close(); }
    catch (err) { ctx.status(err.message, "error"); }
  }

  async function cancelDoc() {
    if (doc && doc.purchase.status === "PARTIAL") return closeRemaining();
    if (!guard(true)) return;
    const out = await modal({
      title: "Cancel purchase draft", submitLabel: "Cancel draft",
      body: '<div class="form-grid"><label class="full">Reason<input name="reason" required autofocus maxlength="300"></label><p class="full hint">Nothing was received into stock; the draft stays in the register as Cancelled.</p></div>',
      onSubmit: (form) => api(`/api/erp/purchases/${id}/cancel`, { method: "POST", body: { reason: form.reason.value } }),
    });
    if (out) { render(out); ctx.status("Draft cancelled", "ok"); }
  }

  async function closeRemaining() {
    if (!CAN["purchase.post"]) { ctx.status("Closing a purchase needs the purchase post right", "warn"); return; }
    const out = await modal({
      title: `Close ${doc.summary.open} remaining line(s)`, submitLabel: "Close as not received",
      body: `<div class="form-grid"><label class="full">Reason<input name="reason" required autofocus maxlength="300" placeholder="Short supplied / will come on a new invoice"></label>
        <p class="full hint">Those lines are marked Not received and the purchase is complete. Stock already received stays as it is.</p></div>`,
      onSubmit: (form) => api(`/api/erp/purchases/${id}/close`, { method: "POST", body: { reason: form.reason.value } }),
    });
    if (out) { render(out); ctx.status(`Purchase ${out.purchase.reference_no} complete`, "ok"); }
    grid.focus();
  }

  function nextIssue() {
    const shown = grid.rows;                       // the lines the current chip filter shows
    if (!shown.length) return;
    const start = Math.max(grid.index, 0);
    for (let k = 1; k <= shown.length; k++) {
      const i = (start + k) % shown.length;
      if (!["READY", "CORRECTED", "POSTED", "CLOSED"].includes(shown[i].status)) { grid.select(i); grid.focus(); return; }
    }
    ctx.status(view === "all" ? "Every line is ready" : "No open issue among the lines shown — click All lines to see the rest", "ok");
  }
  const openLedger = (r) => { if (r && r.item) ctx.open("ledger", { item: r.item.id }); };

  grid.el.addEventListener("keydown", (e) => {
    if (e.target !== grid.el) return;
    if (e.key === "Delete") { e.preventDefault(); removeLine(grid.selected); }
  });
  grid.el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pack-fix]");
    if (b) { e.preventDefault(); packagingDialog(lines.find((l) => l.id === Number(b.dataset.packFix))); }
  });
  load().then(() => { if (doc && doc.summary.blocking) nextIssue(); });

  return {
    get keys() { return keys.bar("purchase").filter(([, label]) => isOpen() || !["Header", "Add line", "Product", "New product", "Accept", "Post"].includes(label)); },
    onKey(e, name) {
      const act = {
        "purchase.header": editHeader, "purchase.add": addLine, "purchase.product": () => matchProduct(grid.selected),
        "purchase.newProduct": () => newProduct(grid.selected), "purchase.accept": () => accept(grid.selected),
        "purchase.next": nextIssue, "purchase.post": () => postDoc(), "purchase.refresh": load, "purchase.cancel": cancelDoc,
        "purchase.delete": deleteDoc, "purchase.removeLine": () => removeLine(grid.selected),
        "purchase.columns": columns, "purchase.gst": gstPanel, "purchase.category": changeCategory, "purchase.rollback": rollback,
        "purchase.rack": setRack, "purchase.rackSuggested": acceptSuggested,
        "purchase.filter": () => { const q = $(".lf-q", root); q?.focus(); q?.select(); },
        "purchase.packaging": () => packagingDialog(grid.selected), "purchase.inspect": () => inspector(grid.selected),
        "purchase.inbox": () => setView(view === "ATTENTION" ? "all" : "ATTENTION"),
        "purchase.source": () => doc && doc.purchase.has_source ? window.open(`/api/erp/purchases/${id}/source`, "_blank") : ctx.status("No supplier file (manual entry)", "warn"),
      };
      for (const [aid, fn] of Object.entries(act)) if (keys.matches(aid, name)) { fn(); return true; }
      return false;
    },
    onShow({ focus }) { if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
