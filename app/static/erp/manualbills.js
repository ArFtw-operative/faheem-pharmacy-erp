// Manual Bills — reference documents only. Not sales: never in Sales History, reports, cash,
// Udhaar, customer balances or stock. Create them from a POS tab in manual mode (Alt+L).
//
//   ↑↓ bills · Enter invoice studio · F4 edit · Alt+X delete (kept, marked Deleted) · Ctrl+P print
import { $, BOOT, api, debounce, esc, fmtDateTime, fmtExpShort, modal, money } from "erp/core";
import { Grid } from "erp/grid";
import { createStudio } from "erp/studio";

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const today = new Date();
  const F = { q: "", start: iso(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)), end: iso(today), status: "ACTIVE" };
  let rows = [], detail = null, ctrl = null, studioOpen = false;

  root.innerHTML = `<div class="sales-h mb">
    <div class="filters">
      <label>From<input type="date" class="f-start" value="${F.start}"></label>
      <label>To<input type="date" class="f-end" value="${F.end}"></label>
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="bill no., customer, mobile or item"></label>
      <label>Show<select class="f-status"><option value="ACTIVE">Kept</option><option value="DELETED">Deleted</option><option value="">All</option></select></label>
      <button type="button" class="btn primary mb-new">New manual bill</button>
      <span class="spacer"></span><span class="s-sum muted"></span>
    </div>
    <p class="mb-note">Manual bills are records only — not sales. They are not in Sales History, any report, cash, Udhaar or stock.</p>
    <div class="sales-split"><div class="sales-list"></div><aside class="sales-detail"><p class="hint">Select a bill.</p></aside></div>
  </div>`;
  const side = $(".sales-detail", root), split = $(".sales-split", root);
  const studio = createStudio({ ctx, closeLabel: "Bill detail", onClose: () => closeStudio() });
  const grid = new Grid({
    label: "Manual bills", storageKey: "manual-bills", empty: "No manual bills in this period.",
    columns: [
      { key: "date", label: "Date / time", width: 128, render: (r) => esc(fmtDateTime(r.date)) },
      { key: "invoice_no", label: "Bill no.", width: 142, render: (r) => `<span class="mono">${esc(r.invoice_no)}</span>` },
      { key: "customer", label: "Customer", width: 170, render: (r) => `${esc(r.customer)}${r.mobile ? ` <span class="muted">${esc(r.mobile)}</span>` : ""}` },
      { key: "items", label: "Items", width: 52, align: "num" },
      { key: "total", label: "Amount", width: 90, align: "num", render: (r) => `<b>${money(r.total)}</b>` },
      { key: "payment", label: "Written as paid by", width: 150, render: (r) => esc(r.payment) },
      { key: "status", label: "Status", width: 80, cellClass: (r) => (r.status === "CANCELLED" ? "st-out" : ""), render: (r) => (r.status === "CANCELLED" ? "Deleted" : "Kept") },
      { key: "user", label: "By", width: 90 },
    ],
    rowClass: (r) => (r.status === "CANCELLED" ? "dim" : ""),
    onSelect: (r) => (studioOpen ? studio.show({ ...r, kind: "manual" }) : showDetail(r)),
    onActivate: () => openStudio(),
    contextMenu: () => [
      { label: "Invoice studio", key: "Enter", action: () => openStudio() },
      { label: "Edit", key: "F4", action: () => edit() },
      { label: "Delete", key: "Alt+X", action: () => del() },
    ],
  });
  $(".sales-list", root).append(grid.el);

  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    try {
      const d = await api(`/api/erp/manual-bills?${new URLSearchParams(Object.entries({ ...F, limit: 500 }).filter(([, v]) => v !== ""))}`, { signal: ctrl.signal });
      rows = d.bills;
      grid.setRows(rows, { keep: true });
      $(".s-sum", root).textContent = `${d.total} manual bill${d.total === 1 ? "" : "s"}`;
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  const showDetail = debounce(async (r) => {
    if (!r) { side.innerHTML = '<p class="hint">Select a bill.</p>'; detail = null; return; }
    try { detail = await api(`/api/erp/manual-bills/${r.id}`); render(); } catch (err) { ctx.status(err.message, "error"); }
  }, 60);
  function render() {
    const d = detail;
    side.innerHTML = `<h3><span class="mono">${esc(d.invoice_no)}</span> <span class="doc-st ds-draft">MANUAL — RECORD ONLY</span>${d.deleted ? ' <span class="doc-st ds-cancelled">DELETED</span>' : ""}</h3>
      <p class="muted">${esc(fmtDateTime(d.date))} · ${esc(d.customer)}${d.mobile ? " · " + esc(d.mobile) : ""} · by ${esc(d.user || "—")}</p>
      <table class="rawtab bill"><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Disc</th><th class="num">Amount</th></tr></thead><tbody>
      ${d.lines.map((l) => `<tr><td>${esc(l.name)}<br><small class="muted mono">${esc(l.batch || "")}${l.expiry ? " · " + fmtExpShort(l.expiry) : ""}</small></td>
        <td class="num">${l.qty}</td><td class="num">${money(l.rate)}</td><td class="num">${Number(l.discount) ? "−" + money(l.discount) : ""}</td><td class="num"><b>${money(l.total)}</b></td></tr>`).join("")}</tbody></table>
      <table class="kvtab"><tr><th>Items total</th><td class="num">${money(d.subtotal)}</td></tr>
        ${Number(d.bill_discount) ? `<tr><th>Bill discount</th><td class="num">−${money(d.bill_discount)}</td></tr>` : ""}
        ${Number(d.round_off) ? `<tr><th>Round off</th><td class="num">${money(d.round_off)}</td></tr>` : ""}
        <tr><th>Bill amount</th><td class="num"><b>₹${money(d.total)}</b></td></tr><tr><th>Written as paid by</th><td class="num">${esc(d.payment)}</td></tr></table>
      ${d.notes ? `<p class="hint">${esc(d.notes)}</p>` : ""}
      ${d.deleted ? `<p class="warn">Deleted: ${esc(d.delete_reason)}</p>` : `<p class="hint">Enter invoice studio · F4 edit · Alt+X delete</p>`}`;
  }
  function openStudio() {
    const r = grid.selected;
    if (!r) return;
    studioOpen = true;
    split.classList.add("with-studio");
    side.replaceWith(studio.el);
    studio.show({ ...r, kind: "manual" });
    ctx.setKeys(); grid.focus();
  }
  function closeStudio() {
    studioOpen = false;
    split.classList.remove("with-studio");
    studio.el.replaceWith(side);
    showDetail(grid.selected);
    ctx.setKeys(); grid.focus();
  }
  function edit() {
    const r = grid.selected;
    if (!r) return;
    if (!CAN["sales.void"]) { ctx.status("Editing bills needs the void / edit right", "warn"); return; }
    if (r.status === "CANCELLED") { ctx.status("A deleted manual bill cannot be edited", "warn"); return; }
    ctx.open("pos", { editManual: r.id }, { fresh: true });
  }
  async function del() {
    const r = grid.selected;
    if (!r || r.status === "CANCELLED") return;
    if (!CAN["sales.void"]) { ctx.status("Deleting needs the void / edit right", "warn"); return; }
    const out = await modal({
      title: `Delete ${r.invoice_no}`, submitLabel: "Delete",
      body: `<div class="form-grid"><label class="full">Reason<input name="reason" required autofocus maxlength="200"></label>
        <p class="full hint">The record is kept, marked Deleted. Nothing else changes — a manual bill never had stock or money effect.</p></div>`,
      onSubmit: (form) => api(`/api/erp/manual-bills/${r.id}/delete`, { method: "POST", body: { reason: form.reason.value } }),
    });
    if (out) { ctx.status(`${r.invoice_no} deleted`, "ok"); await reload(); }
    grid.focus();
  }
  async function newBill() { const t = await ctx.open("pos", {}, { fresh: true }); if (t && t.screen.manual) t.screen.manual(); }

  const q = $(".f-q", root);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 200);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  for (const [cls, key] of [["f-start", "start"], ["f-end", "end"], ["f-status", "status"]]) {
    $("." + cls, root).addEventListener("change", (e) => { F[key] = e.target.value; reload(); });
  }
  $(".mb-new", root).onclick = newBill;
  ctx.setTitle("Manual Bills");
  reload();
  return {
    get keys() { return studioOpen ? [["↑↓", "Bill"], ["1–6", "Format"], ["Ctrl+P", "Print"], ["Esc", "Detail"]] : [["Enter", "Studio"], ["F4", "Edit"], ["Alt+X", "Delete"]]; },
    onKey(e, name) {
      if (studioOpen && studio.onKey(e, name)) return true;
      const act = { F4: edit, "Alt+X": del, "Ctrl+P": () => { if (!studioOpen) openStudio(); setTimeout(() => studio.print(), 400); } };
      if (act[name]) { act[name](); return true; }
      return false;
    },
    onDataChanged(areas) { if (areas.includes("sales")) reload(); },
    onShow({ focus }) { reload(); if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
