import * as keys from "erp/keys";
// Customers — an ERP operational module (not a CRM):
//
//   Directory  who the customer is: fast server-side search, bills, net sales,
//              last visit; the record beside it with invoices (hover preview,
//              Enter opens the canonical bill in Sales), returns, follow-ups,
//              notes and an activity timeline derived from ERP transactions.
//   Inbox      what needs action: today / overdue / upcoming / completed.
//   Calendar   when it is due: day / week / month over the same follow-ups.
//
// One follow-up record behind POS, Inbox, Calendar and the customer record.
import { $, $$, BOOT, api, debounce, esc, fmtDateTime, h, modal, money, store } from "erp/core";
import { Grid } from "erp/grid";
import { followUpPopover } from "erp/followup";

const PANELS = ["directory", "inbox", "calendar"];
const d2 = (iso) => (iso ? iso.slice(0, 10).split("-").reverse().join("-") : "");
const dayName = (d) => d.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" });
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const STATE = { OVERDUE: ["Overdue", "bad"], TODAY: ["Today", "warn"], UPCOMING: ["Upcoming", "ok"], COMPLETED: ["Completed", "muted"], CANCELLED: ["Cancelled", "muted"] };

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  root.innerHTML = `<div class="masters customers">
    <div class="mtabs" role="tablist">
      <button type="button" data-panel="directory">Directory</button>
      <button type="button" data-panel="inbox">Inbox <span class="c-badge" hidden></span></button>
      <button type="button" data-panel="calendar">Calendar</button>
      <span class="hint">F6 switches</span>
    </div>
    <section class="mpanel" data-panel="directory"></section>
    <section class="mpanel" data-panel="inbox" hidden></section>
    <section class="mpanel" data-panel="calendar" hidden></section>
  </div>`;
  const badge = $(".c-badge", root);
  const setCounts = (c) => {
    if (!c) return;
    const n = (c.today || 0) + (c.overdue || 0);
    badge.hidden = !n; badge.textContent = n;
    badge.className = "c-badge" + (c.overdue ? " bad" : "");
  };
  const shared = { ctx, CAN, setCounts, refreshAll: () => Object.values(panels).forEach((p) => p.reload && p.reload()) };
  const panels = {
    directory: directoryPanel($('.mpanel[data-panel="directory"]', root), shared),
    inbox: inboxPanel($('.mpanel[data-panel="inbox"]', root), shared),
    calendar: calendarPanel($('.mpanel[data-panel="calendar"]', root), shared),
  };
  shared.openCustomer = (id) => { show("directory"); panels.directory.select(id); };
  let current = PANELS.includes(params.panel) ? params.panel : "directory";
  function show(name, focus = true) {
    current = name;
    $$(".mtabs [data-panel]", root).forEach((b) => b.classList.toggle("on", b.dataset.panel === name));
    $$(".mpanel", root).forEach((p) => { p.hidden = p.dataset.panel !== name; });
    panels[name].reload();
    if (focus) panels[name].focus();
    ctx.setKeys();
  }
  $(".mtabs", root).addEventListener("click", (e) => { const b = e.target.closest("[data-panel]"); if (b) show(b.dataset.panel); });
  show(current, false);
  if (params.customer) panels.directory.select(params.customer);

  return {
    navigate(t) { if (t && t.customer) { show("directory"); panels.directory.select(t.customer); } return true; },
    get keys() { return panels[current].keys ? panels[current].keys() : keys.bar("customers"); },
    onKey(e, name) {
      if (keys.matches("cust.panel", name)) { show(PANELS[(PANELS.indexOf(current) + 1) % PANELS.length]); return true; }
      return panels[current].onKey(e, name);
    },
    onDataChanged(areas) { if (areas.some((a) => a === "customers" || a === "sales")) panels[current].reload(); },
    onShow({ focus }) { panels[current].reload(); if (focus) setTimeout(() => panels[current].focus(), 0); },
  };
}

// ---------------------------------------------------------------------- directory + record
function directoryPanel(el, S) {
  const { ctx, CAN } = S;
  const F = { q: "", sort: "recent" };
  let rows = [], total = 0, rec = null, invoices = [], tab = "invoices", ctrl = null, loading = false;
  const colKey = `customers-columns:${BOOT.user?.username}`;
  el.innerHTML = `
    <div class="filters">
      <label>Search<input class="c-q" autocomplete="off" spellcheck="false" placeholder="name, mobile, customer ID, address or area"></label>
      <label>Sort<select class="c-sort"><option value="recent">Last visit</option><option value="name">Name</option><option value="sales">Total sales</option><option value="bills">Bills</option></select></label>
      <button type="button" class="btn c-cols">Columns ⚙</button>
      <span class="spacer"></span><span class="c-count muted"></span>
    </div>
    <div class="sales-split c-split"><div class="sales-list"></div><div class="divider" title="Drag to resize"></div><aside class="sales-detail c-rec"><p class="hint">Select a customer.</p></aside></div>`;
  const ALL = [
    { key: "customer_id", label: "Customer ID", width: 90, def: false, render: (r) => `<span class="mono">${esc(r.customer_id)}</span>` },
    { key: "name", label: "Customer", width: 190, def: true, render: (r) => `<b>${esc(r.name)}</b>` },
    { key: "mobile", label: "Mobile", width: 110, def: true, render: (r) => `<span class="mono">${esc(r.mobile || "—")}</span>` },
    { key: "alternate_mobile", label: "Alternate", width: 110, def: false },
    { key: "address", label: "Area / Address", width: 190, def: true },
    { key: "last_visit", label: "Last Visit", width: 96, def: true, render: (r) => d2(r.last_visit) || '<span class="muted">—</span>' },
    { key: "last_invoice", label: "Last Invoice", width: 140, def: false, render: (r) => (r.last_invoice ? `<span class="mono">${esc(r.last_invoice.no)}</span>` : "") },
    { key: "bills", label: "Bills", width: 60, def: true, align: "num" },
    { key: "net_sales", label: "Total Sales", width: 100, def: true, align: "num", render: (r) => (Number(r.net_sales) ? money(r.net_sales) : '<span class="muted">—</span>') },
    { key: "followup", label: "Follow-up", width: 100, def: true, render: (r) => (r.open_followups ? `<span class="warn">${d2(r.next_due)}</span>${r.open_followups > 1 ? ` +${r.open_followups - 1}` : ""}` : "") },
  ];
  const chosen = () => { const s = store.get(colKey, null); return ALL.filter((c) => (Array.isArray(s) ? s.includes(c.key) : c.def)); };
  const grid = new Grid({
    label: "Customers", storageKey: "customers", empty: "No customers match. Customers are created at the POS (Customer field or Alt+C).",
    columns: chosen(), onSelect: (r) => loadRecord(r), onActivate: () => focusRecord(), onNearEnd: () => more(),
    contextMenu: () => [
      { label: "New sale", key: keys.keyFor("cust.newSale"), action: () => newSale() },
      { label: "Follow up", key: keys.keyFor("cust.followup"), action: () => followUp() },
      { label: "Add note", key: keys.keyFor("cust.note"), action: () => addNote() },
      ...(CAN["customers.edit"] ? [{ label: "Edit details", key: keys.keyFor("cust.edit"), action: () => editDetails() }] : []),
    ],
  });
  $(".sales-list", el).append(grid.el);
  const side = $(".c-rec", el), split = $(".c-split", el);
  split.style.gridTemplateColumns = `${store.get("customers:split", 58)}% 6px 1fr`;
  $(".divider", el).addEventListener("mousedown", (e) => {
    e.preventDefault();
    const rect = split.getBoundingClientRect();
    const move = (ev) => { const pct = Math.min(80, Math.max(30, ((ev.clientX - rect.left) / rect.width) * 100)); split.style.gridTemplateColumns = `${pct}% 6px 1fr`; store.set("customers:split", Math.round(pct)); };
    const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  });

  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    try {
      const d = await api(`/api/erp/customers/directory?${new URLSearchParams({ q: F.q, sort: F.sort, limit: 200 })}`, { signal: ctrl.signal });
      rows = d.customers; total = d.total;
      grid.setRows(rows, { keep: true });
      $(".c-count", el).textContent = `${total} customers`;
      S.setCounts(d.followups);
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  async function more() {
    if (loading || rows.length >= total) return;
    loading = true;
    try {
      const d = await api(`/api/erp/customers/directory?${new URLSearchParams({ q: F.q, sort: F.sort, limit: 200, offset: rows.length })}`);
      rows = rows.concat(d.customers); grid.appendRows(d.customers);
    } catch (err) { ctx.status(err.message, "error"); }
    loading = false;
  }
  async function select(id) {
    let i = rows.findIndex((r) => r.id === Number(id));
    if (i < 0) {
      try { const r = await api(`/api/erp/customers/${id}`); F.q = r.mobile || r.name; $(".c-q", el).value = F.q; await reload(); } catch (err) { ctx.status(err.message, "error"); return; }
      i = rows.findIndex((r) => r.id === Number(id));
    }
    if (i >= 0) { grid.select(i); grid.focus(); }
  }

  // ------------------------------------------------------------ record
  const loadRecord = debounce(async (r) => {
    if (!r) { rec = null; side.innerHTML = '<p class="hint">Select a customer.</p>'; return; }
    try {
      const [c, inv] = await Promise.all([api(`/api/erp/customers/${r.id}`), api(`/api/erp/customers/${r.id}/invoices`)]);
      rec = c; invoices = inv.invoices; renderRecord();
    } catch (err) { ctx.status(err.message, "error"); }
  }, 80);
  function renderRecord() {
    const c = rec, s = c.stats;
    side.innerHTML = `
      <div class="c-head"><h3>${esc(c.name)} <span class="muted mono">${esc(c.customer_id)}</span></h3>
        <div class="c-actions">
          <button type="button" class="btn primary" data-a="sale">New sale <kbd>${esc(keys.keyFor("cust.newSale"))}</kbd></button>
          <button type="button" class="btn" data-a="follow">Follow up <kbd>${esc(keys.keyFor("cust.followup"))}</kbd></button>
          <button type="button" class="btn" data-a="note">Note <kbd>${esc(keys.keyFor("cust.note"))}</kbd></button>
          ${c.mobile ? `<a class="btn" href="tel:${esc(c.mobile)}">Call ${esc(c.mobile)}</a>` : ""}
        </div></div>
      <div class="c-facts">
        ${[["Mobile", c.mobile || "—"], ["Alternate", c.alternate_mobile || "—"], ["Area", c.area || "—"], ["Bills", s.bills], ["Net sales", "₹" + money(s.net_sales)],
          ["Avg bill", "₹" + money(s.average_bill)], ["Refunds", Number(s.refunds) ? "₹" + money(s.refunds) : "—"], ["Last visit", d2(s.last_visit) || "—"],
          ["First visit", d2(s.first_visit) || "—"], ["Follow-ups", s.open_followups ? `${s.open_followups} open · next ${d2(s.next_due)}` : "none open"]]
          .map(([k, v]) => `<div class="kv"><span>${k}</span><b>${esc(String(v))}</b></div>`).join("")}
      </div>
      <div class="mtabs c-tabs">${[["invoices", "Invoices"], ["returns", "Returns / refunds"], ["followups", "Follow-ups"], ["activity", "Activity"], ["details", "Details & notes"]]
        .map(([k, l]) => `<button type="button" data-tab="${k}" class="${tab === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <div class="c-body"></div>`;
    side.querySelector(".c-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) { tab = b.dataset.tab; renderRecord(); } });
    side.querySelector(".c-actions").addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]"); if (!a) return;
      ({ sale: newSale, follow: followUp, note: addNote })[a.dataset.a]();
    });
    renderTab();
  }
  function renderTab() {
    const body = side.querySelector(".c-body");
    if (tab === "invoices") {
      body.innerHTML = invoices.length ? `<table class="rawtab c-inv"><thead><tr><th>Date</th><th>Time</th><th>Invoice</th><th class="num">Items</th><th class="num">Gross</th><th class="num">Disc.</th><th class="num">Net</th><th>Status</th></tr></thead><tbody>
        ${invoices.map((s, i) => `<tr data-i="${i}" tabindex="-1"><td>${d2(s.date)}</td><td>${esc(fmtDateTime(s.date).slice(12))}</td>
          <td><a href="#" class="mono" data-bill="${s.id}">${esc(s.invoice_no)}</a>${s.type === "MANUAL" ? ' <span class="tag">manual</span>' : ""}</td>
          <td class="num">${s.items}</td><td class="num">${money(s.gross)}</td><td class="num">${Number(s.discount) ? money(s.discount) : ""}</td>
          <td class="num"><b>${money(s.total)}</b></td>
          <td class="${s.status === "CANCELLED" ? "muted" : s.returned ? "warn" : "ok"}">${s.status === "CANCELLED" ? "Voided" : s.returned ? `Returns −${money(s.refunded)}` : "Completed"}</td></tr>`).join("")}
        </tbody></table><p class="hint">Click an invoice number (or Enter on it) to open the bill in Sales · hover for its lines</p>` : '<p class="hint">No bills yet.</p>';
      body.querySelectorAll("[data-bill]").forEach((a) => {
        a.onclick = (e) => { e.preventDefault(); ctx.open("sales", { bill: Number(a.dataset.bill) }); };
        a.addEventListener("mouseenter", () => preview(a));
        a.addEventListener("mouseleave", hideTip);
      });
    } else if (tab === "followups" || tab === "activity" || tab === "returns") {
      body.innerHTML = '<p class="hint">Loading…</p>';
      (tab === "followups" ? api(`/api/erp/followups?view=all&customer=${rec.id}`) : api(`/api/erp/customers/${rec.id}/activity`)).then((d) => {
        if (tab === "followups") {
          const list = d.followups;
          body.innerHTML = list.length ? `<table class="rawtab"><thead><tr><th>Due</th><th>Reason</th><th>Note</th><th>Invoice</th><th>Status</th><th></th></tr></thead><tbody>
            ${list.map((f) => `<tr><td>${d2(f.due_date)} ${esc(f.due_time)}</td><td>${esc(f.reason_label)}</td><td>${esc(f.note)}</td><td class="mono">${esc(f.source_invoice)}</td>
              <td class="${(STATE[f.state] || ["", ""])[1]}">${(STATE[f.state] || [f.status])[0]}</td>
              <td>${f.status === "OPEN" && CAN["followups.manage"] ? `<button class="btn" data-done="${f.id}">Complete</button> <button class="btn" data-move="${f.id}">Reschedule</button>` : esc(f.completion_note)}</td></tr>`).join("")}</tbody></table>`
            : '<p class="hint">No follow-ups. F4 creates one.</p>';
          body.querySelectorAll("[data-done]").forEach((b) => { b.onclick = () => completeFu(list.find((f) => f.id === Number(b.dataset.done)), b); });
          body.querySelectorAll("[data-move]").forEach((b) => { b.onclick = () => moveFu(list.find((f) => f.id === Number(b.dataset.move)), b); });
        } else {
          const list = tab === "returns" ? d.activity.filter((a) => a.kind === "REFUND") : d.activity;
          body.innerHTML = list.length ? `<ul class="timeline">${list.map((a) => `<li class="k-${a.kind.toLowerCase()}"><span>${esc(fmtDateTime(a.at))}</span>
            ${a.sale_id ? `<a href="#" data-bill="${a.sale_id}">${esc(a.text)}</a>` : esc(a.text)}${a.amount ? ` <b>₹${money(a.amount)}</b>` : ""}</li>`).join("")}</ul>`
            : `<p class="hint">${tab === "returns" ? "No returns or refunds." : "No activity yet."}</p>`;
          body.querySelectorAll("[data-bill]").forEach((a) => { a.onclick = (e) => { e.preventDefault(); ctx.open("sales", { bill: Number(a.dataset.bill) }); }; });
        }
      }).catch((err) => { body.innerHTML = `<p class="bad">${esc(err.message)}</p>`; });
    } else {
      const c = rec;
      body.innerHTML = `<table class="kvtab">${[["Name", c.name], ["Mobile", c.mobile], ["Alternate mobile", c.alternate_mobile], ["Address", c.address], ["Area / locality", c.area],
        ["Document / reference", c.reference], ["Doctor", c.doctor_name], ["Created", fmtDateTime(c.created_at)], ["Last updated", fmtDateTime(c.updated_at)]]
        .map(([k, v]) => `<tr><th>${k}</th><td>${esc(v || "—")}</td></tr>`).join("")}</table>
        ${CAN["customers.edit"] ? `<p><button class="btn" data-edit>Edit details <kbd>${esc(keys.keyFor("cust.edit"))}</kbd></button></p>` : ""}
        <h4>Notes</h4><pre class="c-notes">${esc(c.notes || "No notes yet.")}</pre>`;
      const b = body.querySelector("[data-edit]"); if (b) b.onclick = editDetails;
    }
  }
  const tipCache = new Map();
  let tip = null;
  async function preview(a) {
    const id = Number(a.dataset.bill);
    try {
      const d = tipCache.get(id) || await api(`/api/erp/sales/${id}`);
      tipCache.set(id, d);
      if (!a.matches(":hover")) return;
      hideTip();
      tip = h(`<div class="bill-tip"><b class="mono">${esc(d.invoice_no)}</b> · ₹${money(d.total)} · ${esc(d.payment)}
        <table><tr><th>Product</th><th>Batch</th><th class="num">Qty</th><th class="num">MRP</th><th class="num">Disc</th><th class="num">Net</th></tr>
        ${d.lines.slice(0, 14).map((l) => `<tr><td>${esc(l.name)}</td><td class="mono">${esc(l.batch || "")}</td><td class="num">${l.qty} ${esc(l.unit)}</td>
          <td class="num">${money(l.rate)}</td><td class="num">${Number(l.discount) ? money(l.discount) : ""}</td><td class="num">${money(l.total)}</td></tr>`).join("")}</table></div>`);
      const box = a.getBoundingClientRect();
      tip.style.left = Math.max(8, Math.min(box.left - 200, window.innerWidth - 460)) + "px";
      tip.style.top = Math.min(box.bottom + 4, window.innerHeight - 280) + "px";
      tip.style.width = "440px";
      document.body.append(tip);
    } catch { /* best effort */ }
  }
  const hideTip = () => { if (tip) { tip.remove(); tip = null; } };
  function focusRecord() { const a = side.querySelector("[data-bill]"); if (a) a.focus(); }

  // ------------------------------------------------------------ quick actions
  const need = () => { if (!rec) { ctx.status("Select a customer first", "warn"); return false; } return true; };
  function newSale() {
    if (!need()) return;
    ctx.open("pos", { customer: { id: rec.id, name: rec.name, mobile: rec.mobile, customer_id: rec.customer_id }, note: `New sale for ${rec.name}` }, { fresh: true });
  }
  async function followUp() {
    if (!need()) return;
    if (!CAN["followups.manage"]) { ctx.status("Follow-ups need the follow-up right", "warn"); return; }
    const last = invoices.find((s) => s.status !== "CANCELLED");
    const out = await followUpPopover({ anchor: side.querySelector('[data-a="follow"]'), customer: rec, sale: last ? { id: last.id, invoice_no: last.invoice_no } : null, ctx });
    if (out) { S.refreshAll(); loadRecord(grid.selected); }
  }
  async function completeFu(f, anchor) {
    const note = await quickNote(anchor, "Completion note (optional)");
    if (note === null) return;
    try { const d = await api(`/api/erp/followups/${f.id}/complete`, { method: "POST", body: { note } }); S.setCounts(d.counts); ctx.status("Follow-up completed", "ok"); S.refreshAll(); loadRecord(grid.selected); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function moveFu(f, anchor) {
    const out = await followUpPopover({ anchor, customer: rec, followup: f, ctx });
    if (out) { S.refreshAll(); loadRecord(grid.selected); }
  }
  async function addNote() {
    if (!need()) return;
    const text = await quickNote(side.querySelector('[data-a="note"]'), `Note for ${rec.name}`);
    if (!text) return;
    try { rec = await api(`/api/erp/customers/${rec.id}/notes`, { method: "POST", body: { text } }); tab = "details"; renderRecord(); ctx.status("Note saved", "ok"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function editDetails() {
    if (!need() || !CAN["customers.edit"]) return;
    const c = rec;
    const f = (k, l, v, x = "") => `<label class="${x}">${l}<input name="${k}" value="${esc(v || "")}"></label>`;
    const out = await modal({
      title: `Edit ${c.name}`, wide: true, submitLabel: "Save",
      body: `<div class="form-grid">${f("name", "Name", c.name, "full")}${f("mobile", "Primary mobile", c.mobile)}${f("alternate_mobile", "Alternate mobile", c.alternate_mobile)}
        ${f("address", "Address", c.address, "full")}${f("city", "Area / locality", c.area)}${f("reference", "Document / reference (optional)", c.reference)}${f("doctor_name", "Doctor", c.doctor_name)}</div>`,
      onSubmit: (form) => api(`/api/erp/customers/${c.id}`, { method: "PUT", body: Object.fromEntries(["name", "mobile", "alternate_mobile", "address", "city", "reference", "doctor_name"].map((k) => [k, form.elements[k].value])) }),
    });
    if (out) { rec = out; renderRecord(); reload(); ctx.status("Customer saved", "ok"); }
  }

  const q = $(".c-q", el);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 160);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  $(".c-sort", el).onchange = (e) => { F.sort = e.target.value; reload(); };
  $(".c-cols", el).onclick = async () => {
    const cur = chosen().map((c) => c.key);
    const out = await modal({ title: "Customer columns", submitLabel: "Apply",
      body: `<div class="form-grid">${ALL.map((c) => `<label class="chk"><input type="checkbox" name="c" value="${c.key}" ${cur.includes(c.key) ? "checked" : ""}> ${esc(c.label)}</label>`).join("")}</div>`,
      onSubmit: (form) => { const v = [...form.querySelectorAll("input[name=c]:checked")].map((x) => x.value); if (!v.length) throw new Error("Choose at least one column"); return v; } });
    if (out) { store.set(colKey, out); grid.setColumns(chosen()); }
  };
  return {
    reload, select, focus: () => grid.focus(),
    onKey(e, name) {
      const act = { "cust.search": () => { q.focus(); q.select(); }, "cust.newSale": newSale, "cust.followup": followUp, "cust.note": addNote,
        "cust.edit": editDetails, "cust.refresh": reload };
      for (const [id, fn] of Object.entries(act)) if (keys.matches(id, name)) { fn(); return true; }
      return false;
    },
  };
}

/** A one-line note popover (completion note, customer note). Resolves text, "" or null (closed). */
function quickNote(anchor, label) {
  return new Promise((resolve) => {
    const el = h(`<form class="fu-pop"><label>${esc(label)}<input name="t" maxlength="500" autofocus></label>
      <footer><button type="button" class="btn" data-x>Close <kbd>Esc</kbd></button><button class="btn primary">Save <kbd>Enter</kbd></button></footer></form>`);
    const prev = document.activeElement;
    const close = (v) => { el.remove(); if (prev && prev.focus) prev.focus(); resolve(v); };
    el.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") { e.preventDefault(); close(null); } });
    el.querySelector("[data-x]").onclick = () => close(null);
    el.addEventListener("submit", (e) => { e.preventDefault(); close(el.t.value.trim()); });
    document.body.append(el);
    const box = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: 300, bottom: 200 };
    el.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 360)) + "px";
    el.style.top = Math.min(box.bottom + 6, window.innerHeight - 160) + "px";
    el.t.focus();
  });
}

// ---------------------------------------------------------------------- inbox
function inboxPanel(el, S) {
  const { ctx, CAN } = S;
  let view = "today", rows = [];
  el.innerHTML = `<div class="filters">
      <div class="mtabs inbox-tabs">${[["today", "Today"], ["overdue", "Overdue"], ["upcoming", "Upcoming"], ["completed", "Completed"]]
        .map(([k, l], i) => `<button type="button" data-view="${k}">${l} <span class="n"></span> <kbd>${i + 1}</kbd></button>`).join("")}</div>
      <span class="spacer"></span><span class="hint">C complete · R reschedule · Enter open customer · I open invoice</span></div>`;
  const grid = new Grid({
    label: "Follow-up inbox", storageKey: "customer-inbox", empty: "Nothing here.",
    columns: [
      { key: "due_date", label: "Due", width: 110, render: (r) => `${r.state === "TODAY" ? "Today" : d2(r.due_date)} ${esc(r.due_time)}` },
      { key: "customer", label: "Customer", width: 180, render: (r) => `<b>${esc(r.customer)}</b>` },
      { key: "mobile", label: "Mobile", width: 110, render: (r) => `<span class="mono">${esc(r.mobile)}</span>` },
      { key: "reason_label", label: "Reason", width: 150 },
      { key: "note", label: "Note", width: 220 },
      { key: "last_visit", label: "Last Visit", width: 96, render: (r) => d2(r.last_visit) },
      { key: "source_invoice", label: "Invoice", width: 140, render: (r) => `<span class="mono">${esc(r.source_invoice)}</span>` },
      { key: "created_by", label: "By", width: 80 },
      { key: "state", label: "Status", width: 96, cellClass: (r) => (STATE[r.state] || ["", ""])[1], render: (r) => (STATE[r.state] || [r.status])[0] },
    ],
    onActivate: (r) => S.openCustomer(r.customer_id),
  });
  el.append(grid.el);
  async function reload() {
    try {
      const d = await api(`/api/erp/followups?view=${view}`);
      rows = d.followups; grid.setRows(rows, { keep: true });
      S.setCounts(d.counts);
      el.querySelectorAll("[data-view]").forEach((b) => {
        b.classList.toggle("on", b.dataset.view === view);
        const n = d.counts[b.dataset.view]; b.querySelector(".n").textContent = n ? `(${n})` : "";
      });
    } catch (err) { ctx.status(err.message, "error"); }
  }
  el.querySelector(".inbox-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-view]"); if (b) { view = b.dataset.view; reload(); grid.focus(); } });
  const anchorRow = () => grid.body.querySelector("tr.sel") || grid.el;
  grid.el.addEventListener("keydown", async (e) => {
    if (e.target !== grid.el || e.ctrlKey || e.altKey) return;
    const r = grid.selected;
    const k = e.key.toLowerCase();
    if (/^[1-4]$/.test(e.key)) { e.preventDefault(); view = ["today", "overdue", "upcoming", "completed"][Number(e.key) - 1]; reload(); return; }
    if (!r) return;
    if (k === "c" && r.status === "OPEN" && CAN["followups.manage"]) {
      e.preventDefault();
      const note = await quickNote(anchorRow(), `Complete follow-up · ${r.customer}`);
      if (note === null) return;
      try { await api(`/api/erp/followups/${r.id}/complete`, { method: "POST", body: { note } }); ctx.status(`Completed · ${r.customer}`, "ok"); S.refreshAll(); } catch (err) { ctx.status(err.message, "error"); }
    } else if (k === "r" && r.status === "OPEN" && CAN["followups.manage"]) {
      e.preventDefault();
      const out = await followUpPopover({ anchor: anchorRow(), customer: { id: r.customer_id, name: r.customer, mobile: r.mobile }, followup: r, ctx });
      if (out) S.refreshAll();
    } else if (k === "i" && r.source_sale_id) { e.preventDefault(); ctx.open("sales", { bill: r.source_sale_id }); }
  });
  return { reload, focus: () => grid.focus(), onKey: () => false,
    keys: () => [["1–4", "View"], ["C", "Complete"], ["R", "Reschedule"], ["Enter", "Customer"], ["I", "Invoice"], [keys.keyFor("cust.panel"), "Switch"]] };
}

// ---------------------------------------------------------------------- calendar
function calendarPanel(el, S) {
  const { ctx, CAN } = S;
  let mode = "month", cursor = new Date(), items = [];
  cursor.setHours(0, 0, 0, 0);
  el.innerHTML = `<div class="filters">
      <div class="mtabs cal-modes">${[["day", "Day"], ["week", "Week"], ["month", "Month"]].map(([k, l]) => `<button type="button" data-mode="${k}">${l}</button>`).join("")}</div>
      <button type="button" class="btn" data-step="-1">‹ Prev</button><b class="cal-title"></b><button type="button" class="btn" data-step="1">Next ›</button>
      <button type="button" class="btn" data-today>Today</button>
      <span class="spacer"></span><span class="hint">←→↑↓ move · D/W/M view · Enter opens the day · T today</span></div>
    <div class="cal" tabindex="0"></div>`;
  const cal = el.querySelector(".cal");
  const range = () => {
    const a = new Date(cursor), b = new Date(cursor);
    if (mode === "week") { a.setDate(a.getDate() - ((a.getDay() + 6) % 7)); b.setTime(a.getTime()); b.setDate(a.getDate() + 6); }
    if (mode === "month") { a.setDate(1); a.setDate(a.getDate() - ((a.getDay() + 6) % 7)); b.setTime(a.getTime()); b.setDate(a.getDate() + 41); }
    return [a, b];
  };
  async function reload() {
    const [a, b] = range();
    try {
      const d = await api(`/api/erp/followups?view=all&start=${iso(a)}&end=${iso(b)}`);
      items = d.followups; S.setCounts(d.counts); render();
    } catch (err) { ctx.status(err.message, "error"); }
  }
  function render() {
    el.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("on", x.dataset.mode === mode));
    el.querySelector(".cal-title").textContent = mode === "month" ? cursor.toLocaleDateString("en-IN", { month: "long", year: "numeric" })
      : mode === "week" ? `Week of ${dayName(range()[0])}` : cursor.toLocaleDateString("en-IN", { weekday: "long", day: "2-digit", month: "long", year: "numeric" });
    const byDay = {};
    items.forEach((f) => { (byDay[f.due_date] = byDay[f.due_date] || []).push(f); });
    const today = iso(new Date());
    const ev = (f) => `<button type="button" class="cal-ev s-${(f.state || "").toLowerCase()}" data-fu="${f.id}" title="${esc(f.customer)} · ${esc(f.reason_label)}">${esc(f.due_time ? f.due_time + " " : "")}${esc(f.customer)}</button>`;
    if (mode === "day") {
      const list = byDay[iso(cursor)] || [];
      cal.className = "cal day";
      cal.innerHTML = list.length ? list.map((f) => `<div class="cal-line">${ev(f)} <span>${esc(f.reason_label)}</span> <span class="muted">${esc(f.mobile)}</span> <span class="muted">${esc(f.note)}</span></div>`).join("")
        : '<p class="hint">No follow-ups on this day.</p>';
    } else {
      const [a] = range(), n = mode === "week" ? 7 : 42;
      cal.className = "cal " + mode;
      cal.innerHTML = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="cal-h">${d}</div>`).join("") +
        Array.from({ length: n }, (_, i) => { const d = new Date(a); d.setDate(a.getDate() + i); const k = iso(d); const list = byDay[k] || [];
          return `<div class="cal-d${k === today ? " today" : ""}${k === iso(cursor) ? " sel" : ""}${mode === "month" && d.getMonth() !== cursor.getMonth() ? " out" : ""}" data-day="${k}">
            <span class="cal-n">${d.getDate()}</span>${list.slice(0, mode === "week" ? 12 : 3).map(ev).join("")}${list.length > (mode === "week" ? 12 : 3) ? `<span class="muted">+${list.length - 3} more</span>` : ""}</div>`; }).join("");
    }
  }
  async function eventPopover(f, anchor) {
    const pop = h(`<div class="fu-pop cal-pop"><header><b>${esc(f.customer)}</b><span class="muted">${esc(f.mobile)}</span></header>
      <div>${esc(f.reason_label)} · due ${d2(f.due_date)} ${esc(f.due_time)}</div>${f.note ? `<div class="muted">${esc(f.note)}</div>` : ""}
      ${f.source_invoice ? `<div>Related invoice: <a href="#" data-bill class="mono">${esc(f.source_invoice)}</a></div>` : ""}
      <footer>${f.status === "OPEN" && CAN["followups.manage"] ? '<button class="btn" data-done>Complete</button><button class="btn" data-move>Reschedule</button>' : `<span class="muted">${esc(f.status)}</span>`}
      <button class="btn" data-open>Open customer</button><button class="btn" data-x>Close <kbd>Esc</kbd></button></footer></div>`);
    const close = () => { pop.remove(); cal.focus(); };
    pop.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") close(); });
    pop.querySelector("[data-x]").onclick = close;
    pop.querySelector("[data-open]").onclick = () => { close(); S.openCustomer(f.customer_id); };
    const bill = pop.querySelector("[data-bill]"); if (bill) bill.onclick = (e) => { e.preventDefault(); close(); ctx.open("sales", { bill: f.source_sale_id }); };
    const done = pop.querySelector("[data-done]");
    if (done) done.onclick = async () => { const note = await quickNote(done, "Completion note (optional)"); if (note === null) return; close();
      try { await api(`/api/erp/followups/${f.id}/complete`, { method: "POST", body: { note } }); ctx.status("Follow-up completed", "ok"); S.refreshAll(); } catch (err) { ctx.status(err.message, "error"); } };
    const move = pop.querySelector("[data-move]");
    if (move) move.onclick = async () => { close(); const out = await followUpPopover({ anchor, customer: { id: f.customer_id, name: f.customer, mobile: f.mobile }, followup: f, ctx }); if (out) S.refreshAll(); };
    document.body.append(pop);
    const box = anchor.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(box.left, window.innerWidth - 360)) + "px";
    pop.style.top = Math.min(box.bottom + 4, window.innerHeight - 220) + "px";
    pop.querySelector("button").focus();
  }
  el.addEventListener("click", (e) => {
    const m = e.target.closest("[data-mode]"); if (m) { mode = m.dataset.mode; reload(); return; }
    const s = e.target.closest("[data-step]"); if (s) { step(Number(s.dataset.step)); return; }
    if (e.target.closest("[data-today]")) { cursor = new Date(); cursor.setHours(0, 0, 0, 0); reload(); return; }
    const ev = e.target.closest("[data-fu]"); if (ev) { eventPopover(items.find((f) => f.id === Number(ev.dataset.fu)), ev); return; }
    const d = e.target.closest("[data-day]"); if (d) { cursor = new Date(d.dataset.day + "T00:00:00"); render(); }
  });
  function step(n) {
    if (mode === "month") cursor.setMonth(cursor.getMonth() + n); else cursor.setDate(cursor.getDate() + (mode === "week" ? 7 : 1) * n);
    reload();
  }
  cal.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.altKey) return;
    const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    if (e.key in moves && mode !== "day") {
      e.preventDefault();
      const before = [cursor.getMonth(), range()[0].getTime()];
      cursor.setDate(cursor.getDate() + moves[e.key]);
      if (mode === "month" ? cursor.getMonth() !== before[0] : range()[0].getTime() !== before[1]) reload(); else render();
    } else if (e.key === "Enter") { e.preventDefault(); mode = "day"; reload(); }
    else if ("dwm".includes(e.key.toLowerCase()) && e.key.length === 1) { e.preventDefault(); mode = { d: "day", w: "week", m: "month" }[e.key.toLowerCase()]; reload(); }
    else if (e.key.toLowerCase() === "t") { e.preventDefault(); cursor = new Date(); cursor.setHours(0, 0, 0, 0); reload(); }
    else if (e.key === "PageDown" || e.key === "PageUp") { e.preventDefault(); step(e.key === "PageDown" ? 1 : -1); }
  });
  return { reload, focus: () => cal.focus(), onKey: () => false,
    keys: () => [["←→↑↓", "Day"], ["D/W/M", "View"], ["Enter", "Open day"], ["PgUp/PgDn", "Period"], ["T", "Today"], [keys.keyFor("cust.panel"), "Switch"]] };
}
