import * as keys from "erp/keys";
// Purchases — the inventory intake authority.
//
//   Register   every supplier invoice: drafts under review, posted, cancelled.
//              F3 imports a supplier file (CSV / XLS / XLSX / digital PDF),
//              F4 starts a manual entry; Enter opens the document for review.
//   Suppliers  supplier master (code, GSTIN, contact, terms, credit days) and
//              the product mappings learned from confirmed invoices.
//   Returns    goods sent back to a supplier (PR-000001), out of a chosen batch.
//
// F6 switches panels. Only a posted purchase changes stock.
import { $, $$, BOOT, ApiError, api, debounce, esc, fmtDateTime, h, modal, money } from "erp/core";
import { Grid } from "erp/grid";
import { pickProduct } from "erp/purchase";

const PANELS = ["register", "suppliers", "returns"];
const DOC = { DRAFT: ["Draft", "warn"], PARTIAL: ["Partly posted", "warn"], POSTED: ["Posted", "ok"], CANCELLED: ["Cancelled", "muted"] };
const dmy = (iso) => (iso ? iso.slice(0, 10).split("-").reverse().join("/") : "");

async function upload(url, form) {
  const res = await fetch(url, { method: "POST", body: form, credentials: "same-origin", headers: { Accept: "application/json" } });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = data && data.detail;
    throw new ApiError(typeof d === "string" ? d : (d && d.message) || `Upload failed (${res.status})`, res.status, d);
  }
  return data;
}

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  root.innerHTML = `<div class="masters purchases">
    <div class="mtabs" role="tablist">
      <button type="button" data-panel="register" role="tab">Purchase register</button>
      <button type="button" data-panel="suppliers" role="tab">Suppliers</button>
      <button type="button" data-panel="returns" role="tab">Purchase returns</button>
      <span class="hint">F6 switches</span>
    </div>
    <section class="mpanel" data-panel="register"></section>
    <section class="mpanel" data-panel="suppliers" hidden></section>
    <section class="mpanel" data-panel="returns" hidden></section>
  </div>`;
  let suppliers = [];
  const loadSuppliers = async () => { suppliers = (await api("/api/erp/suppliers?all=1")).suppliers; return suppliers; };
  const panels = {
    register: registerPanel(ctx, $('.mpanel[data-panel="register"]', root), CAN, () => suppliers),
    suppliers: suppliersPanel(ctx, $('.mpanel[data-panel="suppliers"]', root), CAN, loadSuppliers),
    returns: returnsPanel(ctx, $('.mpanel[data-panel="returns"]', root), CAN, () => suppliers),
  };
  loadSuppliers().then(() => panels.register.suppliersLoaded()).catch((err) => ctx.status(err.message, "error"));
  let current = PANELS.includes(params.panel) ? params.panel : "register";
  function show(name, focus = true) {
    current = name;
    $$(".mtabs [data-panel]", root).forEach((b) => b.classList.toggle("on", b.dataset.panel === name));
    $$(".mpanel", root).forEach((p) => { p.hidden = p.dataset.panel !== name; });
    panels[name].reload();
    panels[name].shown(focus);
  }
  $(".mtabs", root).addEventListener("click", (e) => { const b = e.target.closest("[data-panel]"); if (b) show(b.dataset.panel); });
  show(current, false);

  async function importInvoice() {
    if (!CAN["purchase.create"]) { ctx.status("Importing purchases needs purchase rights", "warn"); return; }
    if (!suppliers.length) await loadSuppliers().catch(() => {});
    const active = suppliers.filter((s) => s.active);
    const out = await modal({
      title: "Import supplier invoice", wide: true, submitLabel: "Import invoice",
      body: `<div class="form-grid">
        <label class="full">Supplier file — CSV, XLS, XLSX, PDF, or a scan / photo (read by OCR, checked line by line)<input type="file" name="file" accept=".csv,.tsv,.txt,.xls,.xlsx,.xlsm,.pdf,.jpg,.jpeg,.png,.webp,.tif,.tiff,.bmp" required autofocus></label>
        <label class="full">Supplier<select name="supplier_id"><option value="">Choose…</option>${active.map((s) => `<option value="${s.id}">${esc(s.name)}${s.gst_number ? " · " + esc(s.gst_number) : ""}</option>`).join("")}</select></label>
        <label>Supplier invoice no.<input name="invoice_no" maxlength="60" placeholder="read from the file when present"></label>
        <label>Invoice date<input name="invoice_date" type="date"></label>
        <label>Supplier's invoice total ₹<input name="supplier_total" inputmode="decimal" placeholder="read from the file when present"></label>
        <p class="full hint">Configured automatic intake prepares products and checks quantities, totals and duplicates. When automatic posting is enabled, a fully verified invoice posts immediately; exceptions remain in a draft.
          <a href="/api/erp/purchases/template.csv">Download CSV template</a></p></div>`,
      onOpen: (form) => {
        // keyboard flow: Space opens the chooser; once a file is picked, move on — Enter submits
        form.file.addEventListener("change", () => { if (form.file.files.length) (form.supplier_id.value ? form.querySelector("[type=submit]") : form.supplier_id).focus(); });
        form.file.addEventListener("keydown", (e) => { if (e.key === "Enter" && form.file.files.length) { e.preventDefault(); form.requestSubmit(); } });
      },
      onSubmit: (form) => {
        if (!form.file.files.length) throw new Error("Choose the supplier's file (Space opens the file chooser)");
        const fd = new FormData(form);
        return upload("/api/erp/purchases/import", fd);
      },
    });
    if (out) {
      const drafts = out.drafts || [out.purchase.id];
      for (const d of drafts.slice(1).reverse()) await ctx.open("purchase", { id: d }, { focus: false });
      ctx.open("purchase", { id: drafts[0] });
      const a = out.summary.automation;
      ctx.status(drafts.length > 1 ? `File held ${drafts.length} invoices — opened each result`
        : out.purchase.status === "POSTED" ? `${out.purchase.reference_no}: ${out.summary.rows} lines received`
        : a && a.mode !== "off" ? `${a.resolved_rows}/${a.total_rows} rows resolved · ${a.exceptions.length} row exceptions · ${a.document_blockers.length} invoice checks`
        : `Imported ${out.summary.rows} line(s) · ${out.summary.blocking} need review`, out.summary.blocking || a?.document_blockers.length ? "warn" : "ok");
      panels.register.reload();
    }
  }

  async function manual() {
    if (!CAN["purchase.create"]) { ctx.status("Recording purchases needs purchase rights", "warn"); return; }
    if (!suppliers.length) await loadSuppliers().catch(() => {});
    const out = await modal({
      title: "Enter purchase manually", submitLabel: "Create draft",
      body: `<div class="form-grid">
        <label class="full">Supplier<select name="supplier_id" autofocus required><option value="">Choose…</option>${suppliers.filter((s) => s.active).map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></label>
        <label>Supplier invoice no.<input name="invoice_no" maxlength="60" required></label>
        <label>Invoice date<input name="invoice_date" type="date"></label>
        <label>Supplier's invoice total ₹<input name="supplier_total" inputmode="decimal"></label>
        <p class="full hint">Then F3 adds each line (product, batch, expiry, qty, rate, MRP).</p></div>`,
      onSubmit: (form) => {
        if (!form.supplier_id.value) throw new Error("Choose the supplier");
        return api("/api/erp/purchases", { method: "POST", body: {
          supplier_id: form.supplier_id.value, invoice_no: form.invoice_no.value, invoice_date: form.invoice_date.value,
          supplier_total: form.supplier_total.value } });
      },
    });
    if (out) { ctx.open("purchase", { id: out.purchase.id }); panels.register.reload(); }
  }

  return {
    importInvoice, manual, show,
    newReturn: () => { show("returns"); panels.returns.create(); },
    get keys() { return keys.bar("purchases"); },
    onKey(e, name) {
      if (keys.matches("purchases.panel", name)) { show(PANELS[(PANELS.indexOf(current) + 1) % PANELS.length]); return true; }
      if (keys.matches("purchases.import", name)) { importInvoice(); return true; }
      if (keys.matches("purchases.manual", name)) { manual(); return true; }
      if (keys.matches("purchases.supplier", name)) { show("suppliers"); panels.suppliers.create(); return true; }
      if (keys.matches("purchases.return", name)) { show("returns"); panels.returns.create(); return true; }
      return panels[current].onKey(e, name);
    },
    onShow({ focus }) { Object.values(panels).forEach((p) => p.reload()); if (focus) setTimeout(() => panels[current].shown(true), 0); },
  };
}

// ---------------------------------------------------------------------- register
function registerPanel(ctx, el, CAN, getSuppliers) {
  const F = { q: "", status: "", supplier: "" };
  el.innerHTML = `
    <div class="filters">
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="invoice, PUR no., supplier, product or batch"></label>
      <label>Status<select class="f-status"><option value="">All</option><option value="DRAFT">Draft (under review)</option><option value="PARTIAL">Partly posted</option><option value="POSTED">Posted</option><option value="CANCELLED">Cancelled</option></select></label>
      <label>Supplier<select class="f-supplier"><option value="">All</option></select></label>
      <span class="spacer"></span>
      ${CAN["purchase.create"] ? `<button type="button" class="btn primary r-import">Import invoice <kbd>${esc(keys.keyFor("purchases.import"))}</kbd></button>
        <button type="button" class="btn r-manual">Manual entry <kbd>${esc(keys.keyFor("purchases.manual"))}</kbd></button>` : ""}
      <span class="r-count muted"></span>
    </div>
    <p class="m-help">Enter opens the invoice. Drafts are reviewed line by line (supplier values kept, corrections audited) and posted with F12 — only then does stock change.</p>`;
  const grid = new Grid({
    label: "Purchase register", storageKey: "purchase-register", empty: "No purchases yet. F3 imports a supplier invoice, F4 enters one manually.",
    columns: [
      { key: "reference_no", label: "Document", width: 110, render: (r) => `<span class="mono">${esc(r.reference_no || "DRAFT #" + r.id)}</span>` },
      { key: "status", label: "Status", width: 86, cellClass: (r) => "ps-" + DOC[r.status][1], render: (r) => DOC[r.status][0] },
      { key: "supplier", label: "Supplier", width: 220, render: (r) => esc(r.supplier || "—") },
      { key: "invoice_no", label: "Invoice no.", width: 120, render: (r) => `<span class="mono">${esc(r.invoice_no || "—")}</span>` },
      { key: "invoice_date", label: "Inv. date", width: 86, render: (r) => dmy(r.invoice_date) },
      { key: "lines", label: "Lines", width: 56, align: "num" },
      { key: "open_lines", label: "To review", width: 76, align: "num", cellClass: (r) => (r.open_lines && ["DRAFT", "PARTIAL"].includes(r.status) ? "ps-warn" : ""), render: (r) => (["DRAFT", "PARTIAL"].includes(r.status) ? r.open_lines : "") },
      { key: "total", label: "Amount", width: 96, align: "num", render: (r) => money(r.total) },
      { key: "supplier_total", label: "Supplier total", width: 100, align: "num", render: (r) => (r.supplier_total ? money(r.supplier_total) : "") },
      { key: "format", label: "Source", width: 64 },
      { key: "created_at", label: "Created", width: 128, render: (r) => esc(fmtDateTime(r.created_at)) },
      { key: "posted_at", label: "Posted", width: 128, render: (r) => esc(fmtDateTime(r.posted_at)) },
    ],
    rowClass: (r) => (r.status === "CANCELLED" ? "dim" : ""),
    onActivate: (r) => ctx.open("purchase", { id: r.id }),
    contextMenu: (r) => [
      { label: "Open purchase", key: "Enter", action: () => ctx.open("purchase", { id: r.id }) },
      ...(CAN["purchase.create"] && ["DRAFT", "CANCELLED"].includes(r.status) ? [
        { label: "Delete draft", key: keys.keyFor("purchases.delete"), action: () => deleteDraft(r) },
        ...(r.status === "DRAFT" ? [{ label: "Cancel draft", key: keys.keyFor("purchases.cancel"), action: () => cancelDraft(r) }] : []),
      ] : []),
      { label: "Refresh register", key: keys.keyFor("purchases.refresh"), action: reload },
    ],
  });
  el.append(grid.el);
  let ctrl = null;
  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    const qs = new URLSearchParams({ q: F.q, status: F.status, supplier: F.supplier, limit: 500 });
    try {
      const d = await api("/api/erp/purchases?" + qs, { signal: ctrl.signal });
      grid.setRows(d.purchases, { keep: true });
      const c = d.counts || {};
      $(".r-count", el).textContent = `${d.total} shown · ${c.DRAFT || 0} draft · ${c.PARTIAL || 0} partly posted · ${c.POSTED || 0} posted`;
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  async function deleteDraft(r = grid.selected) {
    if (!r || !CAN["purchase.create"]) return;
    if (!["DRAFT", "CANCELLED"].includes(r.status)) { ctx.status("Received purchases cannot be deleted; use a purchase return", "warn"); return; }
    if (!(await window.erpConfirm(`Delete draft ${r.invoice_no || "#" + r.id} and its unreceived lines?`))) return;
    try { await api(`/api/erp/purchases/${r.id}`, { method: "DELETE" }); await reload(); ctx.status("Purchase draft deleted", "ok"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function cancelDraft(r = grid.selected) {
    if (!r || r.status !== "DRAFT" || !CAN["purchase.create"]) return;
    const out = await modal({ title: "Cancel purchase draft", submitLabel: "Cancel draft",
      body: '<label>Reason<input name="reason" required autofocus maxlength="300"></label>',
      onSubmit: (form) => api(`/api/erp/purchases/${r.id}/cancel`, { method: "POST", body: { reason: form.reason.value } }) });
    if (out) { await reload(); ctx.status("Purchase draft cancelled", "ok"); }
  }
  const q = $(".f-q", el);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 180);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  $(".f-status", el).onchange = (e) => { F.status = e.target.value; reload(); };
  $(".f-supplier", el).onchange = (e) => { F.supplier = e.target.value; reload(); };
  const imp = $(".r-import", el), man = $(".r-manual", el);
  if (imp) imp.onclick = () => ctx.open("purchases").then((t) => t && t.screen.importInvoice());
  if (man) man.onclick = () => ctx.open("purchases").then((t) => t && t.screen.manual());
  reload();
  return {
    reload,
    suppliersLoaded() {
      const s = $(".f-supplier", el);
      s.innerHTML = '<option value="">All</option>' + getSuppliers().map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
      s.value = F.supplier;
    },
    shown(focus) { if (focus) grid.focus(); },
    onKey(e, name) {
      if (keys.matches("purchases.delete", name)) { deleteDraft(); return true; }
      if (keys.matches("purchases.cancel", name)) { cancelDraft(); return true; }
      if (keys.matches("purchases.search", name)) { q.focus(); q.select(); return true; }
      if (keys.matches("purchases.refresh", name)) { reload(); return true; }
      return false;
    },
  };
}

// ---------------------------------------------------------------------- suppliers
function suppliersPanel(ctx, el, CAN, loadSuppliers) {
  let rows = [];
  el.innerHTML = `
    <div class="filters">
      <b>Suppliers</b><span class="hint">Distributors and agencies you buy from. Inactive suppliers stay on old invoices.</span>
      <span class="spacer"></span>
      ${CAN["supplier.manage"] ? `<button type="button" class="btn primary s-new">New supplier <kbd>${esc(keys.keyFor("purchases.supplier"))}</kbd></button>` : ""}
      <span class="s-count muted"></span>
    </div>
    <p class="m-help"><kbd>Enter</kbd> edit · <kbd>Space</kbd> activate / deactivate · <kbd>M</kbd> product mappings learned from confirmed invoices.</p>`;
  const grid = new Grid({
    label: "Suppliers", storageKey: "suppliers", empty: "No suppliers. F7 adds one.",
    columns: [
      { key: "code", label: "Code", width: 84, render: (r) => `<span class="mono">${esc(r.code)}</span>` },
      { key: "name", label: "Supplier", width: 240, render: (r) => `<b>${esc(r.name)}</b>` },
      { key: "gst_number", label: "GSTIN", width: 140, render: (r) => `<span class="mono">${esc(r.gst_number)}</span>` },
      { key: "phone", label: "Phone", width: 110 },
      { key: "contact", label: "Contact", width: 120 },
      { key: "credit_days", label: "Credit days", width: 80, align: "num", render: (r) => r.credit_days || "" },
      { key: "payment_terms", label: "Terms", width: 100 },
      { key: "purchases", label: "Invoices", width: 70, align: "num" },
      { key: "value", label: "Purchased", width: 100, align: "num", render: (r) => money(r.value) },
      { key: "last", label: "Last posted", width: 96, render: (r) => dmy(r.last) },
      { key: "active", label: "Status", width: 76, cellClass: (r) => (r.active ? "st-ok" : "st-out"), render: (r) => (r.active ? "Active" : "Inactive") },
    ],
    rowClass: (r) => (r.active ? "" : "dim"),
    onActivate: (r) => edit(r),
    contextMenu: () => [
      ...(CAN["supplier.manage"] ? [{ label: "Edit", key: "Enter", action: () => edit(grid.selected) },
        { label: "Activate / deactivate", key: "Space", action: () => toggle(grid.selected) }] : []),
      { label: "Product mappings", key: "M", action: () => mappings(grid.selected) },
    ],
  });
  el.append(grid.el);
  async function reload() {
    try { rows = await loadSuppliers(); grid.setRows(rows, { keep: true }); $(".s-count", el).textContent = `${rows.length} suppliers`; }
    catch (err) { ctx.status(err.message, "error"); }
  }
  const guard = () => { if (!CAN["supplier.manage"]) { ctx.status("Managing suppliers needs supplier rights", "warn"); return false; } return true; };
  function form(s = {}) {
    const f = (name, label, attrs = "", cls = "") => `<label class="${cls}">${label}<input name="${name}" value="${esc(s[name] ?? "")}" ${attrs}></label>`;
    return `<div class="form-grid">
      ${f("name", "Supplier name", 'required maxlength="150" autofocus', "full")}
      ${f("code", "Code", 'maxlength="20" placeholder="automatic"')}${f("gst_number", "GSTIN", 'maxlength="15" placeholder="29ABCDE1234F1Z5"')}
      ${f("contact", "Contact person", 'maxlength="120"')}${f("phone", "Phone", 'maxlength="40"')}
      ${f("email", "Email", 'type="email" maxlength="120"')}${f("payment_terms", "Payment terms", 'maxlength="60" placeholder="e.g. 30 days"')}
      ${f("credit_days", "Credit days", 'inputmode="numeric"')}
      <label class="full">Address<input name="address" value="${esc(s.address ?? "")}"></label></div>`;
  }
  const values = (fm) => Object.fromEntries(["name", "code", "gst_number", "contact", "phone", "email", "payment_terms", "credit_days", "address"].map((k) => [k, fm.elements[k].value.trim()]));
  async function create() {
    if (!guard()) return;
    const out = await modal({ title: "New supplier", wide: true, submitLabel: "Create", body: form(),
      onSubmit: (fm) => api("/api/erp/suppliers", { method: "POST", body: values(fm) }) });
    if (out) { await reload(); grid.select(rows.findIndex((r) => r.id === out.supplier.id)); ctx.status(`Supplier ${out.supplier.name} added (${out.supplier.code})`, "ok"); }
    grid.focus();
  }
  async function edit(s) {
    if (!s || !guard()) return;
    const out = await modal({ title: `Edit ${s.name}`, wide: true, submitLabel: "Save", body: form(s),
      onSubmit: (fm) => api(`/api/erp/suppliers/${s.id}`, { method: "PUT", body: values(fm) }) });
    if (out) { await reload(); ctx.status("Supplier saved", "ok"); }
    grid.focus();
  }
  async function toggle(s) {
    if (!s || !guard()) return;
    try { await api(`/api/erp/suppliers/${s.id}`, { method: "PUT", body: { active: !s.active } }); await reload(); ctx.status(`${s.name} ${s.active ? "deactivated" : "activated"}`, "ok"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function mappings(s) {
    if (!s) return;
    let list = [];
    try { list = (await api(`/api/erp/suppliers/${s.id}/mappings`)).mappings; } catch (err) { ctx.status(err.message, "error"); return; }
    const body = h(`<div><p class="hint">How ${esc(s.name)} names products → our product. Used automatically on the next import from this supplier.
      ${CAN["supplier.manage"] ? "Delete removes a wrong mapping." : ""}</p><div class="map-grid"></div></div>`);
    const g = new Grid({ label: "Mappings", empty: "No mappings yet — they are learned when you confirm a product on an invoice.",
      columns: [
        { key: "supplier_code", label: "Supplier code", width: 110, render: (r) => `<span class="mono">${esc(r.supplier_code || "—")}</span>` },
        { key: "description", label: "Supplier description", width: 240 },
        { key: "item", label: "Our product", width: 220 },
        { key: "uses", label: "Used", width: 56, align: "num" },
      ] });
    g.setRows(list);
    body.querySelector(".map-grid").append(g.el);
    g.el.addEventListener("keydown", async (e) => {
      if (e.key !== "Delete" || !g.selected || !CAN["supplier.manage"]) return;
      e.preventDefault(); e.stopPropagation();
      try { list = (await api(`/api/erp/suppliers/${s.id}/mappings/${g.selected.id}`, { method: "DELETE" })).mappings; g.setRows(list); } catch (err) { ctx.status(err.message, "error"); }
    });
    setTimeout(() => g.focus(), 0);
    await modal({ title: `Product mappings — ${s.name}`, wide: true, submitLabel: "Close", body, onSubmit: () => true });
    grid.focus();
  }
  grid.el.addEventListener("keydown", (e) => {
    if (e.target !== grid.el) return;
    if (e.key === " ") { e.preventDefault(); toggle(grid.selected); }
    else if (e.key.toLowerCase() === "m" && !e.ctrlKey && !e.altKey) { e.preventDefault(); mappings(grid.selected); }
  });
  const btn = $(".s-new", el);
  if (btn) btn.onclick = create;
  reload();
  return {
    reload, create,
    shown(focus) { if (focus) grid.focus(); },
    onKey(e, name) {
      if (keys.matches("purchases.refresh", name)) { reload(); return true; }
      if (keys.matches("purchases.search", name)) { grid.focus(); return true; }
      return false;
    },
  };
}

// ---------------------------------------------------------------------- purchase returns
function returnsPanel(ctx, el, CAN, getSuppliers) {
  el.innerHTML = `
    <div class="filters">
      <b>Purchase returns</b><span class="hint">Goods sent back to the supplier (expired, damaged, short-dated). Stock leaves the chosen batch.</span>
      <span class="spacer"></span>
      ${CAN["purchase.return"] ? `<button type="button" class="btn primary t-new">New return <kbd>${esc(keys.keyFor("purchases.return"))}</kbd></button>` : ""}
    </div>
    <p class="m-help">Quantity accepts 3 (units), 1s (one pack) or 2s+3. Value defaults to the batch's purchase rate.</p>`;
  const grid = new Grid({
    label: "Purchase returns", storageKey: "purchase-returns", empty: "No purchase returns. F8 records one.",
    columns: [
      { key: "reference_no", label: "Return no.", width: 100, render: (r) => `<span class="mono">${esc(r.reference_no)}</span>` },
      { key: "date", label: "Date", width: 128, render: (r) => esc(fmtDateTime(r.date)) },
      { key: "supplier", label: "Supplier", width: 200 },
      { key: "product", label: "Product", width: 230 },
      { key: "batch", label: "Batch", width: 96, render: (r) => `<span class="mono">${esc(r.batch)}</span>` },
      { key: "quantity", label: "Qty", width: 60, align: "num" },
      { key: "value", label: "Value", width: 90, align: "num", render: (r) => money(r.value) },
      { key: "reason", label: "Reason", width: 240 },
    ],
  });
  el.append(grid.el);
  async function reload() {
    try { grid.setRows((await api("/api/erp/purchase-returns")).returns, { keep: true }); } catch (err) { ctx.status(err.message, "error"); }
  }
  async function create() {
    if (!CAN["purchase.return"]) { ctx.status("Purchase returns need return rights", "warn"); return; }
    const item = await pickProduct({ title: "Purchase return — choose product" });
    if (!item) return;
    let info;
    try { info = await api(`/api/erp/purchase-returns/batches?item_id=${item.id}`); } catch (err) { ctx.status(err.message, "error"); return; }
    if (!info.batches.length) { ctx.status(`${item.name} has no stock to return`, "warn"); return; }
    const out = await modal({
      title: `Return ${item.name} to supplier`, wide: true, submitLabel: "Record return",
      body: `<div class="form-grid">
        <label class="full">Batch<select name="batch_id" autofocus>${info.batches.map((b) => `<option value="${b.id}" data-supplier="${b.supplier_id || ""}">${esc(b.batch_no || "no batch")} · exp ${esc(dmy(b.expiry).slice(3))} · ${esc(b.stock_label)} · ${esc(b.supplier || "no supplier")}</option>`).join("")}</select></label>
        <label>Supplier<select name="supplier_id"><option value="">From the batch</option>${getSuppliers().map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></label>
        <label>Quantity (${esc(String(info.item.base_unit).toLowerCase())}s · 1s = one ${esc(String(info.item.pack_unit).toLowerCase())})<input name="quantity" required autocomplete="off"></label>
        <label>Value ₹ (blank = purchase rate)<input name="value" inputmode="decimal"></label>
        <label>Reason<input name="reason" required maxlength="300" placeholder="Expired / damaged / short expiry"></label></div>`,
      onSubmit: (fm) => api("/api/erp/purchase-returns", { method: "POST", body: {
        batch_id: fm.batch_id.value, supplier_id: fm.supplier_id.value, quantity: fm.quantity.value, value: fm.value.value, reason: fm.reason.value } }),
    });
    if (out) { grid.setRows(out.returns); grid.select(0); ctx.status(`Return ${out.reference_no} recorded — stock reduced`, "ok"); }
    grid.focus();
  }
  const btn = $(".t-new", el);
  if (btn) btn.onclick = create;
  reload();
  return {
    reload, create,
    shown(focus) { if (focus) grid.focus(); },
    onKey(e, name) {
      if (keys.matches("purchases.refresh", name)) { reload(); return true; }
      return false;
    },
  };
}
