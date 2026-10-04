import * as keys from "erp/keys";
// Sales — bill register, bill detail, returns, refunds and exchanges.
//
//   ↑↓ bills (detail follows)   hover a row: quick preview of its lines
//   F6  return / refund items   F7  exchange: return, then a new bill for the same customer
//   Ctrl+P reprint              Alt+X void (stock goes back to its batches)
//
// A return never edits the bill: it is its own document (SR-…) with its own
// refund method; manual-bill lines never touch stock. An exchange is exactly a
// return followed by a new sale — two documents, nothing netted in secret.
import { $, BOOT, api, debounce, esc, fmtDateTime, fmtExpShort, h, modal, money, store } from "erp/core";
import { Grid } from "erp/grid";
import { createStudio } from "erp/studio";
import { WA_ICON, askPhone, deliveriesHtml, sendInvoice, waStatus } from "erp/whatsapp";

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const REASONS = { CUSTOMER_RETURN: "Customer returned", WRONG_ITEM: "Wrong item", DAMAGED_ITEM: "Damaged item", EXPIRY_ISSUE: "Expiry issue",
  INCORRECT_QUANTITY: "Incorrect quantity", INCORRECT_PRICE: "Incorrect price", PRESCRIPTION_CHANGED: "Prescription changed",
  DUPLICATE_BILLING: "Duplicate billing", OTHER: "Other" };
const DISPOSITION = { RESTOCK: "Back to stock (same batch)", DAMAGE: "Damaged — not restocked", EXPIRED: "Expired — not restocked", NO_RESTOCK: "Not restocked" };

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const today = new Date();
  const F = { q: "", start: iso(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6)), end: iso(today),
    payment: "", status: "", type: "" };
  let rows = [], total = 0, detail = null, ctrl = null, loading = false;
  const cache = new Map();

  root.innerHTML = `<div class="sales-h">
    <div class="filters">
      <label>From<input type="date" class="f-start" value="${F.start}"></label>
      <label>To<input type="date" class="f-end" value="${F.end}"></label>
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="bill no., customer, mobile, product or batch"></label>
      <label>Payment<select class="f-pay"><option value="">All</option><option value="CASH">Cash</option><option value="UPI">UPI</option><option value="CARD">Card</option><option value="SPLIT">Split</option></select></label>
      <label>Bill type<select class="f-type"><option value="">All</option><option value="INVENTORY">Stock bills</option><option value="MANUAL">Manual bills</option></select></label>
      <label>Status<select class="f-status"><option value="">All</option><option value="PAID">Completed</option><option value="CANCELLED">Voided</option><option value="RETURNED">With returns</option></select></label>
      <span class="spacer"></span><span class="s-sum muted"></span>
    </div>
    <div class="sales-split"><div class="sales-list"></div><div class="divider" title="Drag to resize"></div><aside class="sales-detail"><p class="hint">Select a bill.</p></aside></div>
  </div>`;
  const side = $(".sales-detail", root);
  const split = $(".sales-split", root);
  // Faheem Pharmacy Invoice Studio, inside this Sales tab: ↑↓ in the bill list picks the invoice
  const studio = createStudio({ ctx, closeLabel: "Bill detail", onClose: () => closeStudio() });
  let studioOpen = false;
  // resizable right pane (width remembered separately for bill detail and studio)
  const splitKey = () => (studioOpen ? "sales:splitStudio" : "sales:split");
  const applySplit = () => { split.style.gridTemplateColumns = `${store.get(splitKey(), studioOpen ? 42 : 70)}% 6px 1fr`; };
  split.querySelector(".divider").addEventListener("mousedown", (e) => {
    e.preventDefault();
    const rect = split.getBoundingClientRect();
    const move = (ev) => {
      const pct = Math.min(82, Math.max(25, ((ev.clientX - rect.left) / rect.width) * 100));
      split.style.gridTemplateColumns = `${pct}% 6px 1fr`;
      store.set(splitKey(), Math.round(pct));
    };
    const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
  function openStudio() {
    const r = grid.selected;
    if (!r) { ctx.status("Select a bill first", "warn"); return; }
    studioOpen = true;
    split.classList.add("with-studio");
    side.replaceWith(studio.el);
    applySplit();
    studio.show(r);
    ctx.setKeys();
    grid.focus();
  }
  function closeStudio() {
    studioOpen = false;
    split.classList.remove("with-studio");
    studio.el.replaceWith(side);
    applySplit();
    showDetail(grid.selected);
    ctx.setKeys();
    grid.focus();
  }
  const grid = new Grid({
    label: "Bills", storageKey: "sales-register", empty: "No bills in this period.",
    columns: [
      { key: "date", label: "Date / time", width: 128, render: (r) => esc(fmtDateTime(r.date)) },
      { key: "invoice_no", label: "Bill no.", width: 142, render: (r) => `<span class="mono">${esc(r.invoice_no)}</span>${r.type === "MANUAL" ? ' <span class="tag">manual</span>' : ""}` },
      { key: "customer", label: "Customer", width: 170, render: (r) => `${esc(r.customer)}${r.mobile ? ` <span class="muted">${esc(r.mobile)}</span>` : ""}` },
      { key: "items", label: "Items", width: 52, align: "num" },
      { key: "total", label: "Amount", width: 90, align: "num", render: (r) => `<b>${money(r.total)}</b>` },
      { key: "payment", label: "Paid by", width: 150, render: (r) => esc(r.payment) },
      { key: "status", label: "Status", width: 96, cellClass: (r) => (r.status === "CANCELLED" ? "st-out" : r.returned ? "ps-warn" : "st-ok"),
        render: (r) => (r.status === "CANCELLED" ? "Voided" : r.returned ? "Returns" : "Completed") },
      { key: "user", label: "Cashier", width: 90 },
    ],
    rowClass: (r) => (r.status === "CANCELLED" ? "dim" : ""),
    onSelect: (r) => (studioOpen ? studio.show(r) : showDetail(r)),
    onActivate: () => openStudio(),
    onNearEnd: () => more(),
    contextMenu: () => [
      { label: "Return / refund items", key: keys.keyFor("sales.return"), action: () => doReturn() },
      { label: "Exchange", key: keys.keyFor("sales.exchange"), action: () => doReturn(true) },
      { label: "Edit bill", key: keys.keyFor("sales.edit"), action: () => editBill() },
      { label: "Invoice studio", key: "Enter", action: () => openStudio() },
      { label: "Print invoice", key: keys.keyFor("sales.reprint"), action: () => reprint() },
      { label: "Void bill", key: keys.keyFor("sales.void"), action: () => voidBill() },
    ],
  });
  $(".sales-list", root).append(grid.el);

  // ---------------------------------------------------------------- data
  const qs = (extra = {}) => new URLSearchParams(Object.entries({ ...F, ...extra }).filter(([, v]) => v !== "")).toString();
  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    try {
      const d = await api(`/api/erp/sales?${qs({ limit: 200, status: F.status === "RETURNED" ? "" : F.status })}`, { signal: ctrl.signal });
      rows = F.status === "RETURNED" ? d.sales.filter((s) => s.returned) : d.sales;
      total = d.total;
      grid.setRows(rows, { keep: true });
      $(".s-sum", root).textContent = `${d.total} bills · ₹${money(d.value)} (voided bills excluded)`;
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  async function more() {
    if (loading || rows.length >= total || F.status === "RETURNED") return;
    loading = true;
    try {
      const d = await api(`/api/erp/sales?${qs({ limit: 200, offset: rows.length })}`);
      rows = rows.concat(d.sales);
      grid.appendRows(d.sales);
    } catch (err) { ctx.status(err.message, "error"); }
    loading = false;
  }
  async function load(id, fresh = false) {
    if (!fresh && cache.has(id)) return cache.get(id);
    const d = await api(`/api/erp/sales/${id}`);
    cache.set(id, d);
    return d;
  }

  // ---------------------------------------------------------------- detail
  const showDetail = debounce(async (r) => {
    if (!r) { side.innerHTML = '<p class="hint">Select a bill.</p>'; detail = null; return; }
    try { detail = await load(r.id); renderDetail(); } catch (err) { ctx.status(err.message, "error"); }
  }, 60);
  function renderDetail() {
    const d = detail;
    if (!d) return;
    const lines = d.lines.map((l) => `<tr class="${l.returned >= l.qty ? "dim" : ""}"><td>${esc(l.name)}${l.manual ? ' <span class="tag">manual</span>' : ""}
      <br><small class="muted mono">${esc(l.batch || "")}${l.expiry ? " · " + fmtExpShort(l.expiry) : ""}</small></td>
      <td class="num">${l.qty}${l.returned ? `<br><small class="warn">−${l.returned} ret.</small>` : ""}</td>
      <td class="num">${money(l.rate)}</td><td class="num">${Number(l.discount) ? "−" + money(l.discount) : ""}</td><td class="num"><b>${money(l.total)}</b></td></tr>`).join("");
    const returns = d.returns.map((r) => `<li><span class="mono">${esc(r.return_no)}</span> · ${esc(r.business_date)} · ₹${money(r.total_refund)} ${esc(r.refund_method)}
      ${r.reason_code ? `· ${esc(REASONS[r.reason_code] || r.reason_code)}` : ""}</li>`).join("");
    side.innerHTML = `
      <h3><span class="mono">${esc(d.invoice_no)}</span> ${d.status === "CANCELLED" ? '<span class="doc-st ds-cancelled">VOIDED</span>' : d.type === "MANUAL" ? '<span class="doc-st ds-draft">MANUAL</span>' : ""}</h3>
      <p class="muted">${esc(fmtDateTime(d.date))} · ${esc(d.customer)}${d.mobile ? " · " + esc(d.mobile) : ""} · ${esc(d.customer_type === "HOME_DELIVERY" ? "Home delivery" : "Walk-in")} · by ${esc(d.user || "—")}</p>
      <table class="rawtab bill"><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Disc</th><th class="num">Amount</th></tr></thead><tbody>${lines}</tbody></table>
      <table class="kvtab">
        <tr><th>Items total</th><td class="num">${money(d.subtotal)}</td></tr>
        ${Number(d.bill_discount) ? `<tr><th>Bill discount</th><td class="num">−${money(d.bill_discount)}</td></tr>` : ""}
        ${Number(d.round_off) ? `<tr><th>Round off</th><td class="num">${money(d.round_off)}</td></tr>` : ""}
        <tr><th>Bill amount</th><td class="num"><b>₹${money(d.total)}</b></td></tr>
        <tr><th>Paid by</th><td class="num">${esc(d.payment)}</td></tr>
        ${d.tendered ? `<tr><th>Received / change</th><td class="num">${money(d.tendered)} / ${money(d.change || 0)}</td></tr>` : ""}
        ${Number(d.refunded) ? `<tr><th>Refunded</th><td class="num warn">−${money(d.refunded)}</td></tr>` : ""}
      </table>
      ${returns ? `<h4>Returns</h4><ul class="issues">${returns}</ul>` : ""}
      ${CAN["whatsapp.send"] && d.status !== "CANCELLED" ? `<div class="wa-panel"><h4>${WA_ICON} WhatsApp</h4>
        <div class="wa-actions"><button type="button" class="btn wa-btn wa-send">${WA_ICON} <span>WhatsApp</span> <kbd data-shortcut="sales.whatsapp">${esc(keys.keyFor("sales.whatsapp"))}</kbd></button>
        <span class="wa-state muted small"></span></div><div class="wa-list"><p class="muted">Loading…</p></div></div>` : ""}
      ${d.notes ? `<p class="hint">${esc(d.notes)}</p>` : ""}
      <p class="hint">${d.status === "CANCELLED" ? "Voided bills cannot be returned." : `Enter invoice studio · F6 return · F7 exchange · Ctrl+P print${d.locked ? "" : " · F4 edit · Alt+X void"}`}</p>`;
    loadWhatsApp(d);
  }

  // WhatsApp: the bill's deliveries, and send / resend (the same stored invoice — never a new bill)
  let waList = [];
  async function loadWhatsApp(d) {
    const panel = $(".wa-panel", side);
    if (!panel) return;
    try {
      const out = await api(`/api/erp/sales/${d.id}/whatsapp`);
      if (!detail || detail.id !== d.id) return;
      waList = out.messages;
      $(".wa-list", side).innerHTML = deliveriesHtml(waList);
      const btn = $(".wa-send span", side);
      if (btn) btn.textContent = waList.some((m) => m.whatsapp_status === "sent") ? "Resend WhatsApp" : "WhatsApp";
      $(".wa-state", side).textContent = out.status.connected ? "" : out.status.message;
      $(".wa-state", side).className = "wa-state small " + (out.status.connected ? "muted" : "warn");
    } catch (err) { $(".wa-list", side).innerHTML = `<p class="warn">${esc(err.message)}</p>`; }
  }
  async function whatsapp() {
    const d = detail;
    if (!d || !CAN["whatsapp.send"]) return;
    if (d.status === "CANCELLED") { ctx.status("A voided bill's invoice cannot be sent", "warn"); return; }
    const st = await waStatus(true);
    if (!st.connected) { ctx.status(st.message, "warn"); return; }
    // the bill's customer: the server checks primary, then alternate mobile; no customer number: ask
    let phone = "";
    if (!d.mobile && !d.customer_id) {
      const last = waList[0];
      phone = await askPhone({ anchor: $(".wa-send", side), initial: (last && last.customer_phone) || "", invoiceNo: d.invoice_no });
      if (!phone) return;
    }
    try { await sendInvoice(ctx, d, phone, { onUpdate: () => loadWhatsApp(d), anchor: $(".wa-send", side), hasCustomer: !!d.customer_id }); loadWhatsApp(d); }
    catch (err) { ctx.status(`WhatsApp: ${err.message}`, "error"); }
  }
  side.addEventListener("click", async (e) => {
    if (e.target.closest(".wa-send")) { whatsapp(); return; }
    const retry = e.target.closest("[data-retry]");
    if (retry) {
      try { await api(`/api/erp/whatsapp/messages/${retry.dataset.retry}/retry`, { method: "POST" }); ctx.status("WhatsApp: sending again…"); if (detail) loadWhatsApp(detail); }
      catch (err) { ctx.status(err.message, "error"); }
    }
  });

  // hover preview: the bill's lines without leaving the list
  let tip = null, tipTimer = null;
  grid.body.addEventListener("mouseover", (e) => {
    const tr = e.target.closest("tr[data-i]");
    clearTimeout(tipTimer);
    if (!tr) return;
    const r = rows[Number(tr.dataset.i)];
    tipTimer = setTimeout(async () => {
      try {
        const d = await load(r.id);
        if (!tr.isConnected) return;
        hideTip();
        tip = h(`<div class="bill-tip"><b class="mono">${esc(d.invoice_no)}</b> · ₹${money(d.total)} · ${esc(d.payment)}
          <table>${d.lines.slice(0, 12).map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${l.qty}</td><td class="num">${money(l.total)}</td></tr>`).join("")}</table>
          ${d.lines.length > 12 ? `<small class="muted">+${d.lines.length - 12} more</small>` : ""}</div>`);
        const box = tr.getBoundingClientRect();
        tip.style.left = Math.min(box.left + 260, window.innerWidth - 380) + "px";
        tip.style.top = Math.min(box.bottom + 2, window.innerHeight - 260) + "px";
        document.body.append(tip);
      } catch { /* preview is best-effort */ }
    }, 350);
  });
  const hideTip = () => { if (tip) { tip.remove(); tip = null; } };
  grid.body.addEventListener("mouseleave", () => { clearTimeout(tipTimer); hideTip(); });
  grid.el.addEventListener("scroll", hideTip);

  // ---------------------------------------------------------------- actions
  const current = () => (grid.selected ? grid.selected : null);
  function reprint() {
    const r = current();
    if (!r) return;
    if (r.status === "CANCELLED") { ctx.status("A voided bill cannot be printed", "warn"); return; }
    if (!studioOpen) openStudio();
    setTimeout(() => studio.print(), 400);
  }

  async function doReturn(exchange = false) {
    const r = current();
    if (!r) return;
    if (!CAN["sales.refund"]) { ctx.status("Returns need the refund right", "warn"); return; }
    if (r.status === "CANCELLED") { ctx.status("A voided bill cannot be returned", "warn"); return; }
    let info;
    try { info = await api(`/api/sales/${r.id}/refundable`); } catch (err) { ctx.status(err.message, "error"); return; }
    const open = info.lines.filter((l) => l.remaining > 0);
    if (!open.length) { ctx.status("Everything on this bill has already been returned", "warn"); return; }
    const out = await modal({
      title: `${exchange ? "Exchange" : "Return"} — ${info.invoice_no}`, wide: true, submitLabel: exchange ? "Return, then new bill" : "Record return",
      body: `<table class="rawtab ret"><thead><tr><th>Item</th><th class="num">Sold</th><th class="num">Returned</th><th class="num">Can return</th><th class="num">Refund / unit</th><th class="num">Return qty</th></tr></thead><tbody>
        ${open.map((l, i) => `<tr><td>${esc(l.name)} <small class="muted mono">${esc(l.batch_no || "")}</small>${l.batch_id ? "" : ' <span class="tag">manual</span>'}</td>
          <td class="num">${l.sold}</td><td class="num">${l.returned}</td><td class="num">${l.remaining}</td><td class="num">${money(l.unit_refund)}</td>
          <td class="num"><input name="q${i}" data-line="${l.sale_item_id}" data-max="${l.remaining}" data-unit="${l.unit_refund}" inputmode="numeric" class="num" style="width:64px" ${i === 0 ? "autofocus" : ""}></td></tr>`).join("")}
        </tbody></table>
        <div class="form-grid three">
          <label>Reason<select name="reason">${Object.entries(REASONS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label>
          <label>Returned goods<select name="disposition">${Object.entries(DISPOSITION).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label>
          <label>Refund by<select name="method"><option value="CASH">Cash</option><option value="UPI">UPI</option><option value="CARD">Card</option></select></label>
          <label class="full">Note<input name="note" maxlength="300" placeholder="required when the reason is Other"></label>
        </div>
        <p class="hint"><b class="r-total">Refund ₹0.00</b> · Tab moves between quantities · manual-bill lines never change stock${exchange ? " · after the return a new bill opens for the same customer" : ""}</p>`,
      onOpen: (form) => {
        const upd = () => {
          let t = 0;
          form.querySelectorAll("input[data-line]").forEach((inp) => { const q = Math.min(Number(inp.value) || 0, Number(inp.dataset.max)); t += q * Number(inp.dataset.unit); });
          form.querySelector(".r-total").textContent = `Refund ₹${money(t)}`;
        };
        form.addEventListener("input", upd);
      },
      onSubmit: (form) => {
        const lines = [];
        for (const inp of form.querySelectorAll("input[data-line]")) {
          const v = inp.value.trim();
          if (!v) continue;
          const qn = Number(v);
          if (!Number.isInteger(qn) || qn < 0 || qn > Number(inp.dataset.max)) throw new Error(`Return quantity must be a whole number up to ${inp.dataset.max}`);
          if (qn > 0) lines.push({ sale_item_id: Number(inp.dataset.line), quantity: qn });
        }
        if (!lines.length) throw new Error("Enter the quantity being returned on at least one line");
        return api(`/api/sales/${r.id}/refund`, { method: "POST", body: {
          lines, reason_code: form.reason.value, reason_note: form.note.value, disposition: form.disposition.value, refund_method: form.method.value } });
      },
    });
    if (!out) { grid.focus(); return; }
    const ret = out.return;
    cache.delete(r.id);
    ctx.status(`Return ${ret.return_no} recorded · refund ₹${money(ret.total_refund)} by ${ret.refund_method}`, "ok");
    await reload();
    detail = await load(r.id, true); renderDetail();
    if (exchange) {
      const d = detail;
      ctx.open("pos", { customer: d.customer_id ? { id: d.customer_id, name: d.customer, mobile: d.mobile, customer_id: d.customer_code } : null,
        note: `Exchange for ${d.invoice_no}: return ${ret.return_no} refunded ₹${money(ret.total_refund)} — bill the replacement items now` }, { fresh: true });
    } else grid.focus();
  }

  async function editBill() {
    const r = current();
    if (!r) return;
    if (!CAN["sales.void"]) { ctx.status("Editing bills needs the void / edit right", "warn"); return; }
    const d = detail && detail.id === r.id ? detail : await load(r.id);
    if (d.locked) { ctx.status(d.locked, "warn"); return; }
    // back to the POS tab it was billed in (if still open and free), otherwise a new POS tab
    let origin = null;
    try { origin = JSON.parse(sessionStorage.getItem("erp:saleTabs") || "{}")[r.id] || null; } catch { origin = null; }
    if (origin && ctx.goto(origin, { edit: r.id })) { ctx.status(`Editing ${r.invoice_no} in its original POS tab`, "ok"); return; }
    ctx.open("pos", { edit: r.id }, { fresh: true });
  }

  async function voidBill() {
    const r = current();
    if (!r) return;
    if (!CAN["sales.void"]) { ctx.status("Voiding bills needs the void right", "warn"); return; }
    const d = detail && detail.id === r.id ? detail : await load(r.id);
    if (d.locked) { ctx.status(d.locked, "warn"); return; }
    const out = await modal({
      title: `Void ${d.invoice_no}`, submitLabel: "Void bill",
      body: `<div class="form-grid"><label class="full">Reason<input name="reason" required autofocus maxlength="200"></label>
        <p class="full hint">The bill is kept, marked Voided; its stock goes back to the same batches.</p></div>`,
      onSubmit: (form) => api(`/api/erp/sales/${r.id}/void`, { method: "POST", body: { reason: form.reason.value } }),
    });
    if (out) { cache.set(r.id, out); ctx.status(`${out.invoice_no} voided`, "ok"); await reload(); detail = out; renderDetail(); }
    grid.focus();
  }

  // ---------------------------------------------------------------- filters
  const q = $(".f-q", root);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 200);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  for (const [cls, key] of [["f-start", "start"], ["f-end", "end"], ["f-pay", "payment"], ["f-type", "type"], ["f-status", "status"]]) {
    $("." + cls, root).addEventListener("change", (e) => { F[key] = e.target.value; reload(); });
  }
  // opened on one bill (from a report or another screen): find it whatever the current filters
  async function navigate(p) {
    if (!p || !p.bill) return;
    try {
      const d = await load(p.bill, true);
      const day = d.date.slice(0, 10);
      F.q = d.invoice_no; q.value = d.invoice_no;
      if (day < F.start) { F.start = day; $(".f-start", root).value = day; }
      if (day > F.end) { F.end = day; $(".f-end", root).value = day; }
      await reload();
      const i = rows.findIndex((r) => r.id === d.id);
      if (i >= 0) { grid.select(i); if (p.studio) openStudio(); grid.focus(); }
    } catch (err) { ctx.status(err.message, "error"); }
  }
  applySplit();
  reload().then(() => navigate(params));

  return {
    navigate,
    get keys() { return studioOpen ? [["↑↓", "Bill"], ["1–6", "Format"], ["E", "Expiry"], ["B", "B&W"], ["Ctrl+P", "Print"], ["Esc", "Detail"]] : keys.bar("sales"); },
    onKey(e, name) {
      if (studioOpen && studio.onKey(e, name)) return true;
      const act = {
        "sales.search": () => { q.focus(); q.select(); }, "sales.period": () => $(".f-start", root).focus(),
        "sales.refresh": () => { cache.clear(); reload(); }, "sales.return": () => doReturn(), "sales.exchange": () => doReturn(true),
        "sales.reprint": reprint, "sales.void": voidBill, "sales.edit": editBill, "sales.whatsapp": whatsapp,
      };
      for (const [id, fn] of Object.entries(act)) if (keys.matches(id, name)) { fn(); return true; }
      return false;
    },
    onDataChanged(areas) { if (areas.some((a) => a === "sales" || a === "customers")) { cache.clear(); reload(); } },
    onShow({ focus }) { cache.clear(); reload(); if (focus) setTimeout(() => grid.focus(), 0); },
    onHide: hideTip,
  };
}
