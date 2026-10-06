// Udhaar Ledger — what customers owe the pharmacy (pay later). Never card payments.
//
//   One row per Udhaar bill (or opening balance): original, paid, balance, due date, days overdue,
//   status (Upcoming · Due Today · Overdue · Partially Paid · Paid) and the reminder state.
//   F4 receive payment · F5 customer ledger · F6 send reminder · F7 change dates · Ctrl+P statement
//   Repayments are added to the ledger; the original bill is never changed.
import { $, BOOT, api, debounce, esc, fmtDateTime, modal, money, num } from "erp/core";
import { Grid } from "erp/grid";

const VIEWS = [["open", "Owed"], ["overdue", "Overdue"], ["today", "Due today"], ["reminders", "Reminders due"], ["paid", "Paid"], ["all", "All"]];
const STATUS_CLASS = { Overdue: "st-out", "Due Today": "ps-warn", "Partially Paid": "ps-warn", Upcoming: "st-ok", Paid: "muted", Cancelled: "muted" };
const d2 = (iso) => (iso ? iso.slice(0, 10).split("-").reverse().join("-") : "");
const todayIso = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const F = { view: (params && params.view) || "open", q: "" };
  let rows = [], ledger = null, ctrl = null;

  root.innerHTML = `<div class="sales-h udhaar">
    <div class="filters">
      <div class="ud-views" role="tablist">${VIEWS.map(([k, l]) => `<button type="button" class="btn" data-view="${k}">${l}</button>`).join("")}</div>
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="customer, mobile, ID or bill no."></label>
      <span class="spacer"></span>
      <div class="ud-sum"></div>
      ${CAN["udhaar.manage"] ? '<button type="button" class="btn ud-opening">Opening balance…</button><button type="button" class="btn ud-settings">Settings</button>' : ""}
    </div>
    <div class="sales-split ud-split"><div class="sales-list"></div><aside class="sales-detail ud-detail"><p class="hint">Select a row.</p></aside></div>
  </div>`;
  const side = $(".ud-detail", root);
  const grid = new Grid({
    label: "Udhaar", storageKey: "udhaar-ledger", empty: "Nothing owed here.",
    columns: [
      { key: "customer", label: "Customer", width: 160, render: (r) => `<b>${esc(r.customer)}</b>` },
      { key: "mobile", label: "Mobile", width: 104, render: (r) => `<span class="mono">${esc(r.mobile)}</span>` },
      { key: "invoice", label: "Invoice", width: 140, render: (r) => `<span class="mono">${esc(r.invoice)}</span>` },
      { key: "sale_date", label: "Sale date", width: 88, render: (r) => d2(r.sale_date) },
      { key: "original", label: "Original", width: 86, align: "num", render: (r) => money(r.original) },
      { key: "paid", label: "Paid", width: 80, align: "num", render: (r) => (num(r.paid) ? money(r.paid) : '<span class="muted">—</span>') },
      { key: "balance", label: "Balance", width: 90, align: "num", render: (r) => `<b>${money(r.balance)}</b>` },
      { key: "due_date", label: "Due date", width: 88, render: (r) => d2(r.due_date) },
      { key: "days_overdue", label: "Days overdue", width: 70, align: "num", render: (r) => (r.days_overdue ? `<b class="warn">${r.days_overdue}</b>` : "") },
      { key: "status", label: "Status", width: 104, cellClass: (r) => STATUS_CLASS[r.status] || "", render: (r) => esc(r.status) },
      { key: "reminder", label: "Reminder", width: 150, cellClass: (r) => (r.reminder.due ? "ps-warn" : ""),
        render: (r) => `${esc(r.reminder.text)}${r.reminder.text === "Scheduled" ? ` <small class="muted">${d2(r.reminder_date)}</small>` : ""}` },
    ],
    rowClass: (r) => (r.status === "Overdue" ? "ud-overdue" : r.status === "Paid" || r.status === "Cancelled" ? "dim" : ""),
    onSelect: (r) => showLedger(r),
    onActivate: () => receive(),
    contextMenu: () => [
      { label: "Receive payment", key: "F4", action: () => receive() },
      { label: "View ledger", key: "F5", action: () => grid.selected && showLedger(grid.selected, true) },
      { label: "Send reminder", key: "F6", action: () => remind() },
      { label: "Change due / reminder date", key: "F7", action: () => dates() },
      { label: "Print statement", key: "Ctrl+P", action: () => statement() },
      { label: "Share statement on WhatsApp", key: "", action: () => shareStatement() },
      { label: "Open the bill in Sales", key: "", action: () => openBill() },
    ],
  });
  $(".sales-list", root).append(grid.el);

  // ---------------------------------------------------------------- data
  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    root.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === F.view));
    try {
      const d = await api(`/api/erp/udhaar?${new URLSearchParams({ view: F.view, q: F.q })}`, { signal: ctrl.signal });
      rows = d.rows;
      grid.setRows(rows, { keep: true });
      const t = d.totals;
      $(".ud-sum", root).innerHTML = `<span>Owed <b>₹${money(t.outstanding)}</b></span><span class="warn">Overdue <b>₹${money(t.overdue)}</b></span><span>${t.customers} customer${t.customers === 1 ? "" : "s"}</span>${F.view !== "open" ? `<span class="muted">Shown ₹${money(t.shown_balance)}</span>` : ""}`;
      if (!rows.length) { side.innerHTML = '<p class="hint">Nothing here.</p>'; ledger = null; }
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }

  const showLedger = debounce(async (r, focusIt = false) => {
    if (!r) return;
    try {
      const d = await api(`/api/erp/udhaar/customers/${r.customer_id}/ledger`);
      if (!grid.selected || grid.selected.customer_id !== r.customer_id) return;
      ledger = d;
      renderLedger(r);
      if (focusIt) side.focus();
    } catch (err) { ctx.status(err.message, "error"); }
  }, 80);
  function renderLedger(r) {
    const d = ledger, s = d.summary;
    side.innerHTML = `<h3>${esc(d.customer.name)} <span class="muted mono">${esc(d.customer.customer_id)}</span></h3>
      <p class="muted">${esc(d.customer.mobile || "no mobile")} · ${s.custom_days ? s.days + " days to pay" : "store default " + s.days + " days"}${s.limit !== null ? ` · limit ₹${money(s.limit)}` : ""}</p>
      <div class="ud-cards"><div><span>Owes</span><b>₹${money(s.outstanding)}</b></div><div class="${num(s.overdue) ? "warn" : ""}"><span>Overdue</span><b>₹${money(s.overdue)}</b></div>
        ${s.available !== null ? `<div><span>Limit left</span><b>₹${money(s.available)}</b></div>` : ""}</div>
      <div class="ud-actions">
        ${CAN["udhaar.receive"] ? '<button type="button" class="btn primary" data-a="receive">Receive payment <kbd>F4</kbd></button><button type="button" class="btn" data-a="remind">Send reminder <kbd>F6</kbd></button>' : ""}
        <button type="button" class="btn" data-a="statement">Print statement <kbd>Ctrl+P</kbd></button>
        ${CAN["udhaar.receive"] ? '<button type="button" class="btn" data-a="share">Share on WhatsApp</button>' : ""}
        ${CAN["udhaar.manage"] ? '<button type="button" class="btn" data-a="terms">Limit / days</button>' : ""}
      </div>
      <h4>Ledger</h4>
      <table class="rawtab ud-ledger"><thead><tr><th>Date</th><th>Particulars</th><th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th></tr></thead><tbody>
      ${d.lines.map((l) => `<tr class="${l.entry_id === r.id ? "on" : ""}"><td>${d2(l.date)}</td><td>${esc(l.type)} <span class="mono">${esc(l.reference)}</span>${l.note ? `<br><small class="muted">${esc(l.note)}</small>` : ""}</td>
        <td class="num">${l.debit ? money(l.debit) : ""}</td><td class="num">${l.credit ? money(l.credit) : ""}</td><td class="num"><b>${money(l.balance)}</b></td></tr>`).join("") || '<tr><td colspan="5" class="muted">No entries</td></tr>'}
      </tbody></table>
      <h4>Reminders sent</h4>
      ${d.reminders.length ? `<ul class="issues">${d.reminders.map((m) => `<li>${esc(fmtDateTime(m.at))} · ${esc(m.channel)}${m.automatic ? " (automatic)" : ""} · ${esc(m.invoice)} · ₹${money(m.balance)}
        · <span class="${m.state === "FAILED" ? "warn" : "muted"}">${esc(m.state === "NOTED" ? "noted" : m.state.toLowerCase())}</span>${m.error ? ` <small class="warn">${esc(m.error)}</small>` : ""} <small class="muted">by ${esc(m.by)}</small></li>`).join("")}</ul>` : '<p class="muted">None yet.</p>'}`;
  }
  side.tabIndex = -1;
  side.addEventListener("click", (e) => {
    const b = e.target.closest("[data-a]");
    if (!b) return;
    ({ receive, remind, statement, share: shareStatement, terms })[b.dataset.a]();
  });

  // ---------------------------------------------------------------- actions
  const current = () => grid.selected;
  async function after(msg) { ctx.status(msg, "ok"); await reload(); if (grid.selected) showLedger(grid.selected); grid.focus(); }

  async function receive() {
    const r = current();
    if (!r || !CAN["udhaar.receive"]) return;
    if (num(r.balance) <= 0) { ctx.status("Nothing is owed on this row", "warn"); return; }
    const out = await modal({
      title: `Receive Udhaar — ${r.customer}`, submitLabel: "Receive",
      body: `<div class="form-grid">
        <label>Amount received (₹)<input name="amount" inputmode="decimal" value="${num(r.balance)}" autofocus required></label>
        <label>Received by<select name="mode"><option value="CASH">Cash</option><option value="UPI">UPI</option><option value="CARD">Card</option></select></label>
        <label class="full">Apply to<select name="apply"><option value="entry">This bill — ${esc(r.invoice)} (owes ₹${money(r.balance)})</option><option value="oldest">Oldest dues first (all of ${esc(r.customer)}'s bills)</option></select></label>
        <label>Reference<input name="reference" maxlength="80" placeholder="UTR / note (optional)"></label>
        <label>Note<input name="note" maxlength="200"></label>
        <p class="full hint">A part payment leaves the rest owed — e.g. ₹500 on ₹2,000 leaves ₹1,500. The bill itself is never changed.</p></div>`,
      onSubmit: (form) => api(`/api/erp/udhaar/customers/${r.customer_id}/receive`, { method: "POST", body: {
        amount: form.amount.value, mode: form.mode.value, entry_id: form.apply.value === "entry" ? r.id : null,
        reference: form.reference.value, note: form.note.value } }),
    });
    if (out) await after(`Received ₹${money(out.received)} from ${r.customer} · still owes ₹${money(out.outstanding)}`);
    else grid.focus();
  }

  async function remind() {
    const r = current();
    if (!r || !CAN["udhaar.receive"]) return;
    if (num(r.balance) <= 0) { ctx.status("Nothing is owed on this row", "warn"); return; }
    let text = "";
    try { text = (await api(`/api/erp/udhaar/${r.id}/reminder-text`)).text; } catch { /* preview only */ }
    const out = await modal({
      title: `Remind ${r.customer} — ₹${money(r.balance)}`, submitLabel: "Send / note reminder", wide: true,
      body: `<div class="form-grid"><label class="full">How<select name="channel" autofocus><option value="WHATSAPP">WhatsApp message to ${esc(r.mobile)}</option>
          <option value="CALL">I phoned the customer</option><option value="IN_PERSON">I told the customer in person</option></select></label>
        <label class="full">WhatsApp message<textarea rows="4" readonly>${esc(text)}</textarea></label>
        <label class="full">Note (call / in person)<input name="note" maxlength="300" placeholder="e.g. will pay on Saturday"></label></div>`,
      onSubmit: (form) => api(`/api/erp/udhaar/${r.id}/remind`, { method: "POST", body: { channel: form.channel.value, note: form.note.value } }),
    });
    if (out) await after(`Reminder recorded for ${r.customer}`);
    else grid.focus();
  }

  async function dates() {
    const r = current();
    if (!r || !CAN["udhaar.receive"] || r.status === "Paid" || r.status === "Cancelled") return;
    const out = await modal({
      title: `Dates — ${r.invoice}`, submitLabel: "Save",
      body: `<div class="form-grid"><label>Due date<input type="date" name="due" value="${r.due_date}" required autofocus></label>
        <label>Next reminder<input type="date" name="rem" value="${r.reminder_date < todayIso() ? todayIso() : r.reminder_date}" min="${todayIso()}" required></label></div>`,
      onSubmit: (form) => api(`/api/erp/udhaar/${r.id}/dates`, { method: "POST", body: { due_date: form.due.value, reminder_date: form.rem.value } }),
    });
    if (out) await after(`Dates saved for ${r.invoice}`);
    else grid.focus();
  }

  function statement() {
    const r = current();
    if (!r) return;
    window.open(`/api/erp/udhaar/customers/${r.customer_id}/statement?print=1`, "_blank", "noopener");
  }
  async function shareStatement() {
    const r = current();
    if (!r || !CAN["udhaar.receive"]) return;
    try { await api(`/api/erp/udhaar/customers/${r.customer_id}/statement/whatsapp`, { method: "POST" }); ctx.status(`Statement queued on WhatsApp for ${r.customer}`, "ok"); }
    catch (err) { ctx.status(err.message, "error"); }
  }
  async function terms() {
    const r = current();
    if (!r || !ledger || !CAN["udhaar.manage"]) return;
    const s = ledger.summary;
    const out = await modal({
      title: `Udhaar terms — ${r.customer}`, submitLabel: "Save",
      body: `<div class="form-grid"><label>Udhaar limit (₹)<input name="limit" inputmode="decimal" value="${s.limit ?? ""}" placeholder="no limit" autofocus></label>
        <label>Days to pay<input name="days" inputmode="numeric" value="${s.custom_days ? s.days : ""}" placeholder="store default"></label>
        <p class="full hint">Empty = no limit / the store's default days. New Udhaar bills propose their due date from these.</p></div>`,
      onSubmit: (form) => api(`/api/erp/udhaar/customers/${r.customer_id}/terms`, { method: "PUT", body: { limit: form.limit.value, days: form.days.value } }),
    });
    if (out) await after(`Udhaar terms saved for ${r.customer}`);
  }
  function openBill() {
    const r = current();
    if (r && r.sale_id) ctx.open("sales", { bill: r.sale_id });
  }

  // opening balance: what a customer owed before the ERP (or from the old notebook)
  async function opening() {
    let picked = null;
    const out = await modal({
      title: "Udhaar opening balance", submitLabel: "Add", wide: true,
      body: `<div class="form-grid"><label class="full">Customer (name or mobile)<input name="cq" autocomplete="off" autofocus></label>
        <div class="full ud-pick"></div>
        <label>Amount owed (₹)<input name="amount" inputmode="decimal" required></label>
        <label>Due date<input type="date" name="due" min="${todayIso()}" required></label>
        <label>Reminder on<input type="date" name="rem" min="${todayIso()}" required></label>
        <label class="full">Note<input name="note" maxlength="300" placeholder="e.g. from the old Udhaar book"></label></div>`,
      onOpen: (form) => {
        const pick = form.querySelector(".ud-pick");
        const search = debounce(async () => {
          const term = form.cq.value.trim();
          if (!term) { pick.innerHTML = ""; return; }
          const d = await api("/api/erp/customers?q=" + encodeURIComponent(term));
          pick.innerHTML = d.customers.slice(0, 6).map((c, i) => `<label class="chk"><input type="radio" name="cust" value="${i}"> ${esc(c.name)} · ${esc(c.mobile || "no mobile")} · ${esc(c.customer_id)}</label>`).join("") || '<p class="muted">No customer — add them first (POS or Customers)</p>';
          pick.onchange = async (e) => {
            picked = d.customers[Number(e.target.value)];
            const s = await api(`/api/erp/udhaar/customers/${picked.id}`);
            form.due.value = s.due_date; form.rem.value = s.reminder_date;
          };
        }, 150);
        form.cq.addEventListener("input", search);
      },
      onSubmit: (form) => {
        if (!picked) throw new Error("Choose the customer");
        return api(`/api/erp/udhaar/customers/${picked.id}/opening`, { method: "POST", body: {
          amount: form.amount.value, due_date: form.due.value, reminder_date: form.rem.value, note: form.note.value } });
      },
    });
    if (out) await after(`Opening balance added for ${picked.name}`);
  }

  async function settings() {
    const s = await api("/api/erp/udhaar/settings");
    const out = await modal({
      title: "Udhaar settings", submitLabel: "Save", wide: true,
      body: `<div class="form-grid"><label>Default days to pay<input name="days" inputmode="numeric" value="${s.days}" autofocus></label>
        <label class="chk"><input type="checkbox" name="auto" ${s.auto_reminders ? "checked" : ""}> Send WhatsApp reminders automatically on the reminder date</label>
        <label class="full">Reminder message<textarea name="template" rows="4">${esc(s.template)}</textarea></label>
        <p class="full hint">You can use {customer} {pharmacy} {balance} {invoice_no} {bill_date} {due_date} {total}.</p></div>`,
      onSubmit: (form) => api("/api/erp/udhaar/settings", { method: "PUT", body: { days: form.days.value, auto_reminders: form.auto.checked, template: form.template.value } }),
    });
    if (out) ctx.status("Udhaar settings saved", "ok");
  }

  // ---------------------------------------------------------------- filters
  root.querySelector(".ud-views").addEventListener("click", (e) => { const b = e.target.closest("[data-view]"); if (b) { F.view = b.dataset.view; reload(); } });
  const q = $(".f-q", root);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 200);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  $(".ud-opening", root)?.addEventListener("click", opening);
  $(".ud-settings", root)?.addEventListener("click", settings);
  ctx.setTitle("Udhaar Ledger");
  reload();

  return {
    navigate(p) { if (p && p.view) { F.view = p.view; reload(); } return true; },
    get keys() { return [["F4", "Receive"], ["F5", "Ledger"], ["F6", "Remind"], ["F7", "Dates"], ["Ctrl+P", "Statement"]]; },
    onKey(e, name) {
      const act = { F4: receive, F5: () => grid.selected && showLedger(grid.selected, true), F6: remind, F7: dates, "Ctrl+P": statement };
      if (act[name]) { act[name](); return true; }
      return false;
    },
    onDataChanged(areas) { if (areas.some((a) => a === "sales" || a === "customers")) reload(); },
    onShow({ focus }) { reload(); if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
