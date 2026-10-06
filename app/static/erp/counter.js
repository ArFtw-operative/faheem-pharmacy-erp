// Counter Report — one business day at the counter, payment mode by payment mode.
//
//   The day ends at 11:59 PM (store time). Today is live; a finished day is frozen at midnight and
//   always shows the figures it closed with — a bill of that day edited later shows as a difference.
//   Click (or Enter on) a figure to see the bills behind it; Enter on a bill opens it in Sales.
//   ← / → previous / next day · Ctrl+P print
import { $, api, esc, fmtDateTime, money, num } from "erp/core";

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const LONG = (s) => new Date(s + "T00:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
const FIELD = { sales_total: "Sales", refunds: "Returns", net: "Net", received: "Received", bills: "Bills", gross: "Gross (MRP)", discounts: "Discounts",
  udhaar_given: "Udhaar given", udhaar_collected: "Udhaar collected", udhaar_outstanding: "Udhaar outstanding", cash_in_hand: "Cash in hand" };

export function create(ctx, params, root) {
  let day = (params && params.day) || iso(new Date()), rep = null, drill = null;
  root.innerHTML = `<div class="counter">
    <div class="filters">
      <button type="button" class="btn c-prev" title="Previous day (←)">◀</button>
      <label>Business day<input type="date" class="c-day"></label>
      <button type="button" class="btn c-next" title="Next day (→)">▶</button>
      <button type="button" class="btn c-today">Today</button>
      <span class="c-state"></span>
      <span class="spacer"></span>
      <button type="button" class="btn c-print">Print <kbd>Ctrl+P</kbd></button>
    </div>
    <div class="c-body" tabindex="0"></div>
  </div>`;
  const body = $(".c-body", root), dayIn = $(".c-day", root);

  async function load() {
    dayIn.value = day; dayIn.max = iso(new Date());
    drill = null;
    try { rep = await api(`/api/erp/counter?day=${day}`); render(); }
    catch (err) { body.innerHTML = `<p class="warn">${esc(err.message)}</p>`; rep = null; }
    ctx.setTitle(`Counter · ${day === iso(new Date()) ? "Today" : day.split("-").reverse().join("-")}`);
  }
  const cell = (label, value, mode, cls = "") => `<button type="button" class="c-card ${cls}" ${mode ? `data-mode="${mode}"` : "disabled"}><span>${label}</span><b>${value}</b></button>`;
  function render() {
    const f = rep.figures;
    $(".c-state", root).innerHTML = rep.state === "OPEN"
      ? '<span class="tag ok">OPEN · live until midnight</span>'
      : `<span class="tag">CLOSED ${esc(fmtDateTime(rep.closed_at))}</span>${rep.intact ? "" : ' <b class="warn">digest mismatch</b>'}`;
    body.innerHTML = `
      <h2>${esc(LONG(rep.date))}</h2>
      <p class="muted">${esc(rep.note)}</p>
      ${rep.changes.length ? `<div class="c-changes"><b>Changed after this day closed</b> (a bill of this day was edited, voided or returned later — closed figures stay as they were):
        <ul>${rep.changes.map((c) => `<li>${esc(FIELD[c.field] || c.field)}: closed <b>${esc(String(c.closed))}</b> → now <b>${esc(String(c.now))}</b></li>`).join("")}</ul></div>` : ""}
      <table class="report-table c-modes"><thead><tr><th>Payment mode</th><th class="number">Bills</th><th class="money">Sales</th><th class="money">Udhaar collected</th>
        <th class="money">Refunds</th><th class="money">Received</th></tr></thead><tbody>
        ${f.modes.map((m) => `<tr data-mode="${m.mode}" class="${m.mode === "UDHAAR" ? "c-ud" : ""}" tabindex="0"><td><b>${esc(m.label)}</b>${m.mode === "UDHAAR" ? ' <small>(store credit — not received)</small>' : ""}</td>
          <td class="number">${m.bills}</td><td class="money">${money(m.sales)}</td><td class="money">${m.mode === "UDHAAR" ? "—" : money(m.collected)}</td>
          <td class="money">${num(m.refunds) ? "−" + money(m.refunds) : "0.00"}</td><td class="money"><b>${m.mode === "UDHAAR" ? "—" : money(m.received)}</b></td></tr>`).join("")}
      </tbody><tfoot><tr><td>TOTAL</td><td class="number">${f.bills}</td><td class="money">${money(f.sales_total)}</td><td class="money">${money(f.udhaar_collected)}</td>
        <td class="money">${num(f.refunds) ? "−" + money(f.refunds) : "0.00"}</td><td class="money">${money(f.received)}</td></tr></tfoot></table>
      <div class="c-cards">
        ${cell("Transactions", f.bills, "ALL")}
        ${cell("Gross sales (MRP)", "₹" + money(f.gross), "ALL")}
        ${cell("Discounts", "−₹" + money(f.discounts), "ALL")}
        ${num(f.round_off) ? cell("Round off", (num(f.round_off) > 0 ? "+" : "−") + "₹" + money(Math.abs(num(f.round_off))), null) : ""}
        ${cell("Returns / refunds", `−₹${money(f.refunds)} <small>${f.returns}</small>`, "RETURNS")}
        ${cell("Net sales", "₹" + money(f.net), "ALL", "strong")}
        ${cell("Received (all modes)", "₹" + money(f.received), "ALL", "strong")}
        ${cell("Cash in hand", "₹" + money(f.cash_in_hand), "CASH", "strong")}
        ${cell("Udhaar given today", "₹" + money(f.udhaar_given), "UDHAAR", "c-ud")}
        ${cell("Udhaar collected", "₹" + money(f.udhaar_collected), "COLLECTIONS")}
        ${cell("Udhaar outstanding (end of day)", "₹" + money(f.udhaar_outstanding), null, "c-ud")}
        ${cell("Voided bills", `${f.void.bills} · ₹${money(f.void.total)}`, "VOID", "muted")}
      </div>
      <p class="hint">Received = sales paid now (Cash / UPI / Card) + Udhaar collected − refunds paid out. Udhaar sales are owed, not received. Manual bills are not sales and are never counted.</p>
      <div class="c-drill"></div>`;
  }
  async function showDocs(mode) {
    const box = $(".c-drill", root);
    try {
      const d = await api(`/api/erp/counter/documents?day=${day}&mode=${mode}`);
      drill = d;
      const label = { ALL: "All bills", CASH: "Paid by cash", UPI: "Paid by UPI / Online", CARD: "Paid by card", UDHAAR: "Udhaar bills", VOID: "Voided bills",
        RETURNS: "Returns / refunds", COLLECTIONS: "Udhaar collected" }[mode] || mode;
      const head = d.kind === "returns" ? "<th>Return</th><th>Bill</th><th>Time</th><th>Method</th><th class=\"money\">Refund</th>"
        : d.kind === "collections" ? "<th>Customer</th><th>Bill</th><th>Time</th><th>Mode</th><th class=\"money\">Amount</th>"
        : "<th>Bill</th><th>Time</th><th>Customer</th><th>Paid by</th><th class=\"money\">This mode</th><th class=\"money\">Bill total</th>";
      const rows = d.rows.map((r, i) => d.kind === "returns"
        ? `<tr data-i="${i}" tabindex="0"><td class="mono">${esc(r.return_no)}</td><td class="mono">${esc(r.invoice_no)}</td><td>${esc(fmtDateTime(r.date))}</td><td>${esc(r.method)}</td><td class="money">${money(r.amount)}</td></tr>`
        : d.kind === "collections"
        ? `<tr data-i="${i}" tabindex="0"><td>${esc(r.customer)}</td><td class="mono">${esc(r.invoice_no)}</td><td>${esc(fmtDateTime(r.date))}</td><td>${esc(r.method)}</td><td class="money">${money(r.amount)}</td></tr>`
        : `<tr data-i="${i}" tabindex="0"><td class="mono">${esc(r.invoice_no)}</td><td>${esc(fmtDateTime(r.date))}</td><td>${esc(r.customer)}</td><td>${esc(r.payment)}</td><td class="money">${money(r.amount)}</td><td class="money">${money(r.total)}</td></tr>`).join("");
      box.innerHTML = `<h3>${esc(label)} <small class="muted">${d.rows.length} · Enter / double-click opens the bill</small></h3>
        <table class="report-table c-docs"><thead><tr>${head}</tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">None</td></tr>'}</tbody></table>`;
      const first = box.querySelector("tr[data-i]");
      if (first) first.focus();
      box.scrollIntoView({ block: "nearest" });
    } catch (err) { ctx.status(err.message, "error"); }
  }
  function openDoc(tr) {
    const r = drill && drill.rows[Number(tr.dataset.i)];
    if (r && r.id) ctx.open("sales", { bill: r.id });
  }
  body.addEventListener("click", (e) => {
    const m = e.target.closest("[data-mode]");
    if (m && !m.disabled) { showDocs(m.dataset.mode); return; }
  });
  body.addEventListener("dblclick", (e) => { const tr = e.target.closest(".c-docs tr[data-i]"); if (tr) openDoc(tr); });
  body.addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr[data-i], tr[data-mode]");
    if (!tr) return;
    if (e.key === "Enter") { e.preventDefault(); if (tr.dataset.mode) showDocs(tr.dataset.mode); else openDoc(tr); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const sib = e.key === "ArrowDown" ? tr.nextElementSibling : tr.previousElementSibling;
      if (sib) sib.focus();
    }
  });
  const shift = (n) => { const d = new Date(day + "T00:00:00"); d.setDate(d.getDate() + n); const next = iso(d); if (next <= iso(new Date())) { day = next; load(); } };
  $(".c-prev", root).onclick = () => shift(-1);
  $(".c-next", root).onclick = () => shift(1);
  $(".c-today", root).onclick = () => { day = iso(new Date()); load(); };
  dayIn.addEventListener("change", () => { if (dayIn.value) { day = dayIn.value; load(); } });
  function print() {
    if (!rep) return;
    const w = window.open("", "_blank");
    if (!w) { ctx.status("Allow pop-ups to print", "warn"); return; }
    w.document.write(`<!doctype html><html><head><title>Counter report ${esc(rep.date)}</title><style>body{font:12px Arial;margin:12mm}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd;padding:4px 6px}.money,.number{text-align:right}
      .c-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:10px}.c-card{border:1px solid #ccd;padding:6px;display:flex;justify-content:space-between;background:none;font:inherit}.c-drill,.hint{display:none}</style></head>
      <body><h1>${esc(ctx.boot.pharmacy || "Faheem Pharmacy")} — Counter Report</h1>${body.innerHTML}<script>window.onload=()=>window.print()<\/script></body></html>`);
    w.document.close();
  }
  $(".c-print", root).onclick = print;
  load();
  return {
    navigate(p) { if (p && p.day) { day = p.day; load(); } return true; },
    get keys() { return [["←/→", "Day"], ["Enter", "Bills"], ["Ctrl+P", "Print"]]; },
    onKey(e, name) {
      if (name === "Ctrl+P") { print(); return true; }
      if ((name === "ArrowLeft" || name === "ArrowRight") && !e.target.closest("input")) { shift(name === "ArrowLeft" ? -1 : 1); return true; }
      return false;
    },
    onDataChanged(areas) { if (areas.includes("sales") && rep && rep.state === "OPEN") load(); },
    onShow({ focus }) { if (rep && rep.state === "OPEN") load(); if (focus) setTimeout(() => body.focus(), 0); },
  };
}
