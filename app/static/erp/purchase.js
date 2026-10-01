import * as keys from "erp/keys";
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
import { $, BOOT, api, debounce, esc, fmtDateTime, fmtExp, h, modal, money } from "erp/core";
import { Grid } from "erp/grid";

export const STATUS = {
  READY: ["Ready", "ok"], CORRECTED: ["Corrected", "ok"], NEEDS_REVIEW: ["Needs review", "warn"],
  PRODUCT_MATCH_REQUIRED: ["Product?", "warn"], INVALID: ["Invalid", "bad"], POSTED: ["Posted", "ok"],
  CLOSED: ["Not received", "muted"],
};
const DOC = { DRAFT: "Draft", PARTIAL: "Partly posted", POSTED: "Posted", CANCELLED: "Cancelled" };
const DONE = ["POSTED", "CLOSED"];
const READYISH = ["READY", "CORRECTED"];
const FIELDS = [
  ["name", "Supplier description", "full"], ["supplier_code", "Supplier product code"], ["pack", "Pack"],
  ["batch", "Batch"], ["expiry", "Expiry (month-year)"], ["quantity", "Qty (packs)"], ["free", "Free (packs)"],
  ["rate", "Rate / pack"], ["mrp", "MRP / pack"], ["discount", "Disc %"], ["gst", "GST %"], ["amount", "Amount (before GST)"],
  ["hsn", "HSN"], ["manufacturer", "Manufacturer"],
];
const t = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "");

/** Make ``el`` the field the modal focuses when it opens. */
const focusField = (form, el) => { if (!el) return; form.querySelectorAll("[autofocus]").forEach((x) => x.removeAttribute("autofocus")); el.setAttribute("autofocus", ""); };

/** Product chooser (search + ranked suggestions). Resolves with {id, name, …} or null. */
export function pickProduct({ title = "Choose product", initial = "", suggestionsUrl = "", searchUrl = "/api/erp/purchase-products" } = {}) {
  let rows = [], at = 0, ctrl = null;
  const body = h(`<div class="picker">
    <input name="q" autocomplete="off" spellcheck="false" placeholder="Type product name, code or barcode" value="${esc(initial)}" autofocus>
    <div class="picker-list" role="listbox"></div>
    <p class="hint">↑↓ choose · Enter selects · suggestions are ranked by similarity and are never applied without you</p></div>`);
  const list = body.querySelector(".picker-list");
  const render = () => {
    list.innerHTML = rows.length ? rows.map((r, i) => `<div class="pick${i === at ? " on" : ""}" data-i="${i}" role="option">
      <b>${esc(r.name)}</b> <span class="muted">${esc(r.pack || "")}${r.manufacturer ? " · " + esc(r.manufacturer) : ""}</span>
      ${r.score != null ? `<span class="score">${r.score}%${r.note ? " · " + esc(r.note) : ""}</span>` : ""}</div>`).join("")
      : '<div class="muted pick-empty">No products — type to search</div>';
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
  const input = body.querySelector("input");
  input.addEventListener("input", () => search(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (rows.length) { at = (at + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length; render(); }
    }
  });
  list.addEventListener("mousedown", (e) => { const p = e.target.closest(".pick"); if (p) { at = Number(p.dataset.i); render(); } });
  list.addEventListener("dblclick", () => body.closest("form")?.requestSubmit());
  suggestionsUrl && !initial ? search.flush("") : search.flush(initial);
  return modal({ title, body, wide: true, submitLabel: "Select",
    onSubmit: () => { if (!rows[at]) throw new Error("Choose a product from the list"); return rows[at]; } });
}

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const id = Number(params.id);
  let doc = null, lines = [];
  // the status chips filter the lines; the selection is kept by line id, so it survives switching filters
  let view = "all", switching = false;
  const markedIds = new Set();
  const selectedRows = () => lines.filter((l) => markedIds.has(l.id) && !DONE.includes(l.status));
  root.innerHTML = `<div class="pdoc">
    <div class="pdoc-head"></div>
    <div class="pdoc-bar"></div>
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
      { key: "batch", label: "Batch", width: 96, render: (r) => cell(r, "batch", `<span class="mono">${esc(r.batch || "—")}</span>`) },
      { key: "expiry", label: "Expiry", width: 74, render: (r) => cell(r, "expiry", r.expiry ? fmtExp(r.expiry) : `<span class="muted">${esc(r.expiry_raw || "—")}</span>`) },
      { key: "qty", label: "Billed", width: 50, align: "num", render: (r) => cell(r, "quantity", esc(r.receipt?.paid ?? r.qty)) },
      { key: "free", label: "Free", width: 46, align: "num", render: (r) => cell(r, "free", esc(r.receipt?.free ?? r.free ?? "")) },
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
    canMark: (r) => !DONE.includes(r.status),
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
      { label: "Correct line", key: "Enter", action: () => editLine(grid.selected) },
      { label: "Match product", key: keys.keyFor("purchase.product"), action: () => matchProduct(grid.selected) },
      { label: "Create as new product", key: keys.keyFor("purchase.newProduct"), action: () => newProduct(grid.selected) },
      { label: "Accept warnings", key: keys.keyFor("purchase.accept"), action: () => accept(grid.selected) },
      { label: "Remove line", key: keys.keyFor("purchase.removeLine"), action: () => removeLine(grid.selected) },
      { label: "Mark / unmark for posting", key: "Space", action: () => grid.toggleMark() },
      ...(draft() ? [{ label: "Delete purchase draft", key: keys.keyFor("purchase.delete"), action: deleteDoc },
        { label: "Cancel purchase draft", key: keys.keyFor("purchase.cancel"), action: cancelDoc }] : []),
    ] : [{ label: "Stock ledger of the product", key: "Enter", action: () => openLedger(grid.selected) }],
  });
  $(".pdoc-lines", root).append(grid.el);

  const matchLabel = (m) => ({ SUPPLIER_MAP: "supplier map", CODE: "code", EXACT_NAME: "exact name", MANUAL: "chosen", NEW_PRODUCT: "created" })[m] || m.toLowerCase();
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
    INVALID: "Invalid", POSTED: "Posted", CLOSED: "Not received", SELECTED: "Selected" };
  function applyView(keep = true) {
    for (const id of [...markedIds]) if (!lines.some((l) => l.id === id && !DONE.includes(l.status))) markedIds.delete(id);
    if (view === "SELECTED" && !markedIds.size) view = "all";
    const rows = view === "all" ? lines : view === "SELECTED" ? lines.filter((l) => markedIds.has(l.id)) : lines.filter((l) => l.status === view);
    switching = true;
    try {
      grid.setRows(rows, { keep });
      grid.setMarks(rows.map((r, i) => (markedIds.has(r.id) ? i : -1)).filter((i) => i >= 0));
    } finally { switching = false; }
    renderBar();
    if (!rows.length) renderSide(null);
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
    const markedReady = marked.filter((r) => READYISH.includes(r.status)).length;
    const postBtn = isOpen() && CAN["purchase.post"]
      ? marked.length
        ? `<button type="button" class="btn primary p-post" ${markedReady === marked.length ? "" : "disabled"} title="${markedReady === marked.length ? "" : "Only Ready or Corrected lines can be posted"}">Post ${marked.length} selected <kbd>${esc(keys.keyFor("purchase.post"))}</kbd></button>`
        : `<button type="button" class="btn primary p-post" ${s.postable ? "" : "disabled"} title="${s.postable ? "" : "Every open line must be Ready or Corrected — or select the ready ones (Shift+↑↓) and post those"}">${p.status === "PARTIAL" ? `Post remaining ${s.open}` : "Post to stock"} <kbd>${esc(keys.keyFor("purchase.post"))}</kbd></button>`
      : "";
    const chip = (v, label, n, tone = "") => `<button type="button" class="chip ${tone}${view === v ? " on" : ""}" data-view="${v}" ${n || v === "all" || view === v ? "" : "disabled"} aria-pressed="${view === v}" title="${v === "all" ? "Show every line" : `Show only ${label.toLowerCase()} lines`}">${label} ${n}</button>`;
    bar.innerHTML = `
      ${chip("all", "All lines", s.rows)}
      ${chip("READY", "Ready", c.READY, "ok")}${chip("CORRECTED", "Corrected", c.CORRECTED, "ok")}
      ${chip("NEEDS_REVIEW", "Needs review", c.NEEDS_REVIEW, c.NEEDS_REVIEW ? "warn" : "")}
      ${chip("PRODUCT_MATCH_REQUIRED", "Product match", c.PRODUCT_MATCH_REQUIRED, c.PRODUCT_MATCH_REQUIRED ? "warn" : "")}
      ${c.INVALID ? chip("INVALID", "Invalid", c.INVALID, "bad") : ""}
      ${c.POSTED ? chip("POSTED", "Posted", c.POSTED, "ok") : ""}
      ${c.CLOSED ? chip("CLOSED", "Not received", c.CLOSED) : ""}
      ${marked.length ? `${chip("SELECTED", "Selected", marked.length, "sel")}${markedReady < marked.length ? `<span class="hint">${marked.length - markedReady} not ready</span>` : ""}<button type="button" class="chip clear-sel" title="Clear the selection (Esc)">✕ clear</button>`
        : isOpen() && s.ready ? '<span class="hint">Shift+↑↓ selects lines · Space marks one · Ctrl+A all</span>' : ""}
      <span class="spacer"></span>
      ${s.gst && (s.gst.problems.length || s.gst.missing_rate_lines) ? `<button type="button" class="chip warn gst-chip" title="GST details (${esc(keys.keyFor("purchase.gst"))})">GST ⚠ ${s.gst.problems.length + (s.gst.missing_rate_lines ? 1 : 0)}</button>` : ""}
      ${p.warnings ? `<span class="hint" title="${esc(p.warnings)}">⚠ file notes</span>` : ""}
      ${s.automation?.mode !== "off" && s.automation && isOpen() ? `<button type="button" class="chip automation-chip">Automatic ${s.automation.resolved_rows}/${s.automation.total_rows} · ${s.automation.exceptions.length} exceptions</button>` : ""}
      ${postBtn}`;
    const automation = $(".automation-chip", bar);
    if (automation) automation.onclick = async () => {
      const a = s.automation;
      const out = await modal({ title: "Automatic intake", wide: true, submitLabel: "Recheck evidence",
        body: `<p>${a.resolved_rows} of ${a.total_rows} rows have resolved checks. This measures readiness, not independently verified accuracy.</p>
          ${a.document_blockers.length ? `<p><b>Invoice checks</b></p><ul>${a.document_blockers.map(b => `<li>${esc(b.message)}</li>`).join("")}</ul>` : ""}
          ${a.exceptions.length ? `<p><b>Rows requiring information</b></p><ul>${a.exceptions.map(e => `<li>Line ${e.line}: ${esc(e.name)} — ${esc(e.codes.join(", ").replaceAll("_", " "))}</li>`).join("")}</ul>` : "<p>No row exceptions.</p>"}`,
        onSubmit: () => api(`/api/erp/purchases/${p.id}/prepare`, { method: "POST" }) });
      if (out) await load();
    };
    const post = $(".p-post", bar);
    if (post) post.onclick = () => postDoc();
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
    const raw = r.raw || {};
    const rows = FIELDS.filter(([f]) => raw[f] !== undefined || corr(r, f)).map(([f, label]) => {
      const c = corr(r, f);
      return `<tr><th>${esc(label)}</th><td class="mono">${esc(raw[f] ?? "")}</td><td class="mono">${c ? `<b>${esc(c.value)}</b><br><small class="muted">${esc(c.by)} · ${esc(fmtDateTime(c.at))}</small>` : ""}</td></tr>`;
    }).join("");
    const issues = (r.issues || []).map((i) => `<li class="${i.accepted ? "muted" : i.level === "warn" ? "warn" : i.level === "info" ? "muted" : "bad"}">${esc(i.message)}${i.accepted ? " (accepted)" : ""}</li>`).join("");
    const prod = r.item ? `<b>${esc(r.item.name)}</b><br><span class="muted">${esc(r.item.code)} · ${esc(r.item.pack || "")} · 1 ${esc(t(r.item.pack_unit))} = ${r.item.upp} ${esc(t(r.item.base_unit))}</span>`
      : r.new_product ? `<span class="tag new">NEW PRODUCT</span> ${esc(r.name)}<br><span class="muted">${esc(r.category || "General")} · ${r.units_per_pack ? `1 ${esc(t(r.pack_unit || "PACK"))} = ${r.units_per_pack} ${esc(t(r.base_unit || "UNIT"))}` : "units not set"}</span>`
      : '<span class="bad">Not matched — F4 choose, Shift+F4 create new</span>';
    const receipt = r.receipt || {};
    const qtyNote = receipt.resolved ? `<p class="hint"><b>Receives ${receipt.received_base_units} ${esc(t(receipt.base_unit))}s</b><br>${(receipt.evidence || []).map(esc).join("<br>")}</p>` : '<p class="hint">Physical quantity awaits product / invoice-unit verification.</p>';
    side.innerHTML = `
      <h3>Line ${r.line_no} <span class="ps-${STATUS[r.status][1]}">${STATUS[r.status][0]}</span></h3>
      <div class="side-prod">${prod}</div>${qtyNote}${costTable(r)}
      ${issues ? `<ul class="issues">${issues}</ul>` : '<p class="ok">No issues.</p>'}
      ${canEdit() && !DONE.includes(r.status) ? '<button type="button" data-invoice-unit>Verify invoice unit</button><button type="button" data-reference>Medicine reference</button>' : ""}
      ${rows ? `<table class="rawtab"><thead><tr><th></th><th>Supplier</th><th>Corrected</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      ${r.status === "POSTED" && r.batch_id ? '<p class="hint">Enter opens the product\'s stock ledger.</p>' : ""}`;
    side.querySelector("[data-invoice-unit]")?.addEventListener("click", () => verifyInvoiceUnit(r));
    side.querySelector("[data-reference]")?.addEventListener("click", () => showReference(r));
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
    const packs = Number(r.landed) && Number(r.rate_incl) ? Number(r.landed) / Number(r.rate_incl) : 0;
    const g = doc?.summary?.gst || {};
    const split = Number(r.igst) ? `IGST ₹${money(r.igst)}` : `CGST ₹${money(r.cgst)} + SGST ₹${money(r.sgst)}`;
    const upp = r.item ? r.item.upp : r.units_per_pack;
    const perUnit = upp > 1 ? ` <small class="muted">(₹${money(Number(r.rate_incl) / upp)} per ${esc(t(r.item ? r.item.base_unit : r.base_unit || "unit"))})</small>` : "";
    return `<table class="costtab"><tbody>
      <tr><th>Invoice rate / pack</th><td class="num">₹${money(r.rate)}</td></tr>
      <tr><th>Taxable value${Number(r.discount) ? " (after discount / scheme)" : ""}</th><td class="num">₹${money(r.taxable)}</td></tr>
      <tr><th>GST ${r.gst !== "" ? Number(r.gst) + "%" : "0% (not on invoice)"}</th><td class="num">+ ₹${money(r.gst_amount)}<br><small class="muted">${split}</small></td></tr>
      <tr><th>Value incl. GST</th><td class="num">₹${money(r.landed)}</td></tr>
      <tr class="total"><th>Rate / pack incl. GST${packs ? ` <small class="muted">÷ ${Math.round(packs * 100) / 100} packs</small>` : ""}</th><td class="num"><b>₹${money(r.rate_incl)}</b>${perUnit}</td></tr>
    </tbody></table>${r.gst_worked_out ? `<p class="hint">Worked out from the invoice. This line was posted before GST tracking, so its stock was costed before GST at ₹${money(r.batch_rate || r.rate)} per pack${upp > 1 ? "" : ""} — not re-costed.</p>`
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
      onOpen: (form) => { const f = (r.issues || []).find((i) => !i.accepted); focusField(form, f && form.elements[f.field]); },
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
      <label>Form<select name="np_dosage_form">${opt(["", ...(BOOT.units.forms || []).filter(Boolean)], r.dosage_form)}</select></label>
      <label>Units per pack<input name="np_units_per_pack" inputmode="numeric" value="${esc(r.units_per_pack ?? "")}"></label>
      <label>Sale unit (base)<select name="np_base_unit">${opt(BOOT.units.base || [], r.base_unit || "UNIT")}</select></label>
      <label>Purchase unit (pack)<select name="np_pack_unit">${opt(BOOT.units.pack || [], r.pack_unit || "PACK")}</select></label>
      <p class="hint">Detected from pack “${esc(r.pack || "—")}” — confirm before posting.</p></fieldset>`;
  }
  const newProductValues = (form) => Object.fromEntries(["category", "dosage_form", "units_per_pack", "base_unit", "pack_unit"]
    .map((k) => [k, form.elements["np_" + k] ? form.elements["np_" + k].value : undefined]).filter(([, v]) => v !== undefined));

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
    const item = await pickProduct({ title: "Add line — choose product" });
    if (!item) return;
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
      const notReady = marked.filter((r) => !READYISH.includes(r.status));
      if (notReady.length) {
        grid.select(lines.indexOf(notReady[0]));
        ctx.status(`${notReady.length} selected line(s) are not ready (line ${notReady.map((r) => r.line_no).slice(0, 8).join(", ")}) — correct them or unmark with Space`, "warn");
        return;
      }
      lineIds = marked.map((r) => r.id);
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
    if (!acceptDifference && !(await window.erpConfirm(`Post ${count} line(s) of ${doc.purchase.invoice_no} into stock (₹${money(value)}${lineIds ? " before GST" : ""})?`
      + (rest ? ` ${rest} line(s) stay open for review.` : "") + " Posted lines cannot be edited afterwards."))) return;
    try {
      const out = await api(`/api/erp/purchases/${id}/post`, { method: "POST", body: { accept_difference: acceptDifference, ...(lineIds ? { line_ids: lineIds } : {}) } });
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
  load().then(() => { if (doc && doc.summary.blocking) nextIssue(); });

  return {
    get keys() { return keys.bar("purchase").filter(([, label]) => isOpen() || !["Header", "Add line", "Product", "New product", "Accept", "Post"].includes(label)); },
    onKey(e, name) {
      const act = {
        "purchase.header": editHeader, "purchase.add": addLine, "purchase.product": () => matchProduct(grid.selected),
        "purchase.newProduct": () => newProduct(grid.selected), "purchase.accept": () => accept(grid.selected),
        "purchase.next": nextIssue, "purchase.post": () => postDoc(), "purchase.refresh": load, "purchase.cancel": cancelDoc,
        "purchase.delete": deleteDoc, "purchase.removeLine": () => removeLine(grid.selected),
        "purchase.columns": columns, "purchase.gst": gstPanel,
        "purchase.source": () => doc && doc.purchase.has_source ? window.open(`/api/erp/purchases/${id}/source`, "_blank") : ctx.status("No supplier file (manual entry)", "warn"),
      };
      for (const [aid, fn] of Object.entries(act)) if (keys.matches(aid, name)) { fn(); return true; }
      return false;
    },
    onShow({ focus }) { if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
