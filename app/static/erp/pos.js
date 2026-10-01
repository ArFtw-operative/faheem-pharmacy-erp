import * as keys from "erp/keys";
// POS — keyboard-first billing.
//
// Flow: type item → Enter → type Qty → Enter → next item … F9/F8/F10 payment →
// F12 Complete sale: the sale summary (Enter completes, Esc goes back to adjust),
// then "Generate invoice? Y/N" — Y opens the invoice studio inside this tab.
// Qty is always the product's base unit (tablets for a loose strip); "1s" = one
// strip, "2s+3" = two strips and three tablets. Stock is allocated FEFO across
// unexpired batches and each batch is charged its own MRP — exactly what the
// server does, so the amount shown is the amount billed. "#" in the item box
// searches customers; an unknown customer is created inline without leaving.
import { createStudio } from "erp/studio";
import { followUpPopover } from "erp/followup";
import { WA_ICON, askPhone, normalizePhone, prettyPhone, sendInvoice, waStatus } from "erp/whatsapp";
import { $, $$, ApiError, api, daysUntil, debounce, describe, esc, fmtExp, fmtExpShort, h, money, num, parseQty, r2, rupees, uid, unitName } from "erp/core";

// Bill numbers are slots among the POS tabs that are open right now: one tab is
// always Bill 1, a second Bill 2, and closing a tab frees its number again.
const openSlots = new Set();
function takeSlot(preferred) {
  if (preferred && !openSlots.has(preferred)) { openSlots.add(preferred); return preferred; }
  let n = 1;
  while (openSlots.has(n)) n++;
  openSlots.add(n);
  return n;
}
const SALE_TYPES = { WALK_IN: "Walk-in", HOME_DELIVERY: "Home delivery" };
const saleType = (value) => value === "HOME_DELIVERY" ? value : "WALK_IN";
const MODES = { CASH: "Cash", UPI: "UPI", CARD: "Card", SPLIT: "Split" };

export function create(ctx, params, root, saved) {
  const B = ctx.boot, CAN = B.can || {};
  const expiryDays = (B.settings && B.settings.expiry_threshold_days) || 90;
  const roundMode = (B.settings && B.settings.round_off_mode) || "NEAREST_RUPEE";
  const billNo = takeSlot(saved && saved.slot);   // a reloaded tab keeps its number
  // store policy: the most any line, or the bill, may be discounted — the server enforces the same
  const MAXD = Number((B.settings && B.settings.max_discount_pct) ?? 20);

  const blank = () => ({ slot: billNo, manual: false, lines: [], customer: null, saleType: "WALK_IN", saleTypeExplicit: false, discount: 0, discountPct: null, mode: "CASH", received: "", ref: "", split: { CASH: "", UPI: "", CARD: "" }, parked: null, requestId: uid() });
  let S = saved && saved.lines ? Object.assign(blank(), saved) : blank();
  // opened for an exchange: the returning customer is already on the new bill
  if (!(saved && saved.lines) && params && params.customer) { S.customer = params.customer; S.exchangeNote = params.note || ""; }
  S.saleType = saleType(saved?.saleType ?? saved?.customer?.customer_type ?? saved?.customer?.type);
  let sel = S.lines.length ? S.lines.length - 1 : -1;
  let editing = null;         // index of the line whose qty is being edited
  let discEditing = null;     // index of the line whose discount % is being edited
  let rateEditing = null;     // manual bill: index of the line whose rate is being typed
  let lastBill = null;
  let busy = false;

  root.innerHTML = `
  <div class="pos">
    <div class="pos-head">
      <span class="lbl">Bill</span><b class="bill-no"></b>
      <span class="lbl">Customer</span><button type="button" class="cust-chip" title="Click (or the Customer shortcut) to choose a customer"></button>
      <div class="sale-types" role="group" aria-label="Sale type">
        <span class="lbl">Sale type</span>
        <button type="button" class="btn" data-sale-type="WALK_IN" aria-pressed="false">Walk-in</button>
        <button type="button" class="btn" data-sale-type="HOME_DELIVERY" aria-pressed="false">Home delivery</button>
        <kbd data-shortcut="pos.saleType" title="Switch sale type">${esc(keys.keyFor("pos.saleType"))}</kbd>
      </div>
      <span class="edit-badge" hidden></span>
      <span class="manual-badge" hidden title="Any item, stock not checked or changed — own MB- number series">MANUAL BILL</span>
      <span class="spacer"></span>
    </div>
    <div class="pos-search">
      <label class="lbl" for="q-${billNo}">Item</label>
      <input id="q-${billNo}" class="q" autocomplete="off" spellcheck="false" placeholder="Search product name or code">
      <kbd data-shortcut="pos.search">${esc(keys.keyFor("pos.search"))}</kbd>
      <div class="drop" hidden></div>
    </div>
    <div class="pos-main">
      <div class="bill-wrap" tabindex="0" aria-label="Bill lines">
        <table class="bill">
          <colgroup><col style="width:38px"><col style="width:92px"><col><col style="width:112px"><col style="width:74px"><col style="width:60px"><col style="width:84px"><col style="width:78px"><col style="width:112px"><col style="width:96px"></colgroup>
          <thead><tr><th class="num">#</th><th>Code</th><th>Product</th><th>Batch</th><th>Expiry</th><th class="num" title="Units per pack">Pack</th><th class="num" title="In selling units">Qty</th><th class="num" title="MRP per selling unit">MRP</th><th class="num" title="Item discount % — max ${MAXD}%">Disc %</th><th class="num">Amount</th></tr></thead>
          <tbody></tbody>
        </table>
        <div class="bill-empty">Start typing a medicine name. <kbd>Enter</kbd> adds it, type the quantity, <kbd>Enter</kbd> again.</div>
      </div>
      <aside class="totals">
        <section class="bill-sum" aria-label="Bill summary">
          <header><span>BILL SUMMARY</span><span class="t-items">0 items</span></header>
          <div class="bs-row"><span>MRP value</span><b class="t-gross">0.00</b></div>
          <div class="bs-row less"><span title="Per item on a bill line (max ${MAXD}%)">Less: item discounts</span><span class="t-ldisc">0.00</span></div>
          <div class="bs-row sub"><span>Subtotal</span><b class="t-sub">0.00</b></div>
          <div class="bs-row less disc">
            <label for="bd-${billNo}" title="Percent of the subtotal, 0–${MAXD}">Less: bill disc.</label>
            <span class="bd-in"><input id="bd-${billNo}" class="t-disc num" type="number" min="0" max="${MAXD}" step="0.5" inputmode="decimal" placeholder="0" ${CAN["sales.discount"] ? "" : "disabled title=\"No discount permission\""} value="0"><i>%</i></span>
            <span class="t-bdisc">0.00</span>
          </div>
          <div class="bs-row note"><span class="t-dlim">Discount limit ${MAXD}% per item and per bill</span></div>
          <div class="bs-row small"><span>Round off</span><span class="t-round">0.00</span></div>
          <div class="bs-net"><span>NET PAYABLE</span><b class="t-netv">₹0.00</b></div>
          <div class="bs-save t-save" hidden></div>
        </section>
        <div class="pay-h">PAYMENT</div>
        <div class="paymodes">
          <button type="button" data-mode="CASH">Cash <kbd data-shortcut="pos.cash">${esc(keys.keyFor("pos.cash"))}</kbd></button>
          <button type="button" data-mode="UPI">UPI <kbd data-shortcut="pos.upi">${esc(keys.keyFor("pos.upi"))}</kbd></button>
          <button type="button" data-mode="CARD">Card <kbd data-shortcut="pos.card">${esc(keys.keyFor("pos.card"))}</kbd></button>
          <button type="button" data-mode="SPLIT">Split <kbd data-shortcut="pos.split">${esc(keys.keyFor("pos.split"))}</kbd></button>
        </div>
        <div class="pay pay-cash">
          <label>Received<input class="p-recv num" inputmode="decimal" placeholder="exact"></label>
          <div class="t-row"><span>Change to return</span><b class="p-change">0.00</b></div>
        </div>
        <div class="pay pay-ref" hidden><label>Reference<input class="p-ref" maxlength="80" placeholder="UTR / approval no. (optional)"></label></div>
        <div class="pay pay-split" hidden>
          <label>Cash<input class="s-amt num" data-split="CASH" inputmode="decimal"></label>
          <label>UPI<input class="s-amt num" data-split="UPI" inputmode="decimal"></label>
          <label>Card<input class="s-amt num" data-split="CARD" inputmode="decimal"></label>
          <label>Cash received<input class="s-recv num" inputmode="decimal" placeholder="exact"></label>
          <div class="t-row"><span class="s-left-l">Remaining</span><b class="s-left">0.00</b></div>
          <div class="t-row"><span>Change to return</span><b class="s-change">0.00</b></div>
        </div>
        <p class="pay-err" role="alert"></p>
        <button type="button" class="btn primary finalize">Complete sale <kbd data-shortcut="pos.save">${esc(keys.keyFor("pos.save"))}</kbd></button>
        <div class="last-bill" hidden></div>
      </aside>
    </div>
    <div class="pos-detail"></div>
  </div>`;

  const q = $(".q", root), drop = $(".drop", root), tbody = $("tbody", root), wrap = $(".bill-wrap", root);
  // the invoice studio lives inside this POS tab: other tabs stay usable while it is open
  const studio = createStudio({ ctx, closeLabel: "New bill", onClose: () => closeStudio() });
  const studioBox = h('<div class="pos-studio" hidden></div>');
  studioBox.append(studio.el);
  root.append(studioBox);
  let studioOpen = false;
  function openStudio(summary, opts = {}) {
    studioOpen = true;
    $(".pos", root).hidden = true;
    studioBox.hidden = false;
    studio.show(summary, opts);
    ctx.setKeys();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    setTimeout(() => studio.el.focus(), 0);
  }
  function closeStudio() {
    studioOpen = false;
    studioBox.hidden = true;
    $(".pos", root).hidden = false;
    ctx.setKeys();
    q.focus();
  }

  function renderSaleType() {
    $$("[data-sale-type]", root).forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.saleType === S.saleType));
      button.disabled = busy;
    });
  }
  function changeSaleType(value) {
    if (busy) return;
    S.saleType = saleType(value);
    S.saleTypeExplicit = true;
    renderSaleType();
    ctx.save(S);
    ctx.status(`Sale type: ${SALE_TYPES[S.saleType]}`, "ok");
  }
  // Switching type with the mouse must not cancel an in-progress quantity edit.
  $(".sale-types", root).addEventListener("mousedown", (event) => {
    if (event.target.closest("[data-sale-type]")) event.preventDefault();
  });
  $(".sale-types", root).addEventListener("click", (event) => {
    const button = event.target.closest("[data-sale-type]");
    if (button) changeSaleType(button.dataset.saleType);
  });

  // ---------------------------------------------------------------- line math (mirrors the server)
  const upp = (l) => (l.manual ? 1 : Math.max(1, l.upp || 1));
  const pool = (l) => (l.manual ? [] : l.batch_id ? l.batches.filter((b) => b.id === l.batch_id) : l.batches);
  const avail = (l) => (l.manual ? Infinity : pool(l).reduce((n, b) => n + b.stock, 0));
  function alloc(l) {
    let need = l.qty; const out = [];
    for (const b of pool(l)) {
      if (need <= 0) break;
      const take = Math.min(b.stock, need);
      if (take > 0) { out.push({ b, qty: take, amount: r2(num(b.pack_mrp) * take / (b.upp || upp(l))) }); need -= take; }
    }
    return out;
  }
  const amount = (l) => (l.manual ? r2(num(l.rate) * l.qty) : r2(alloc(l).reduce((n, a) => n + a.amount, 0)));   // gross at MRP
  const lineDisc = (l, pct = num(l.disc)) => r2(amount(l) * pct / 100);        // same rounding as the server
  const lineNet = (l) => r2(amount(l) - lineDisc(l));
  function discProblem(pct) {
    if (!Number.isFinite(pct) || pct < 0) return "Enter a discount between 0 and " + MAXD + "%";
    if (pct > MAXD) return `Maximum item discount is ${MAXD}% — ${pct}% is not allowed`;
    return "";
  }
  const step = (l) => (upp(l) > 1 && !l.loose ? upp(l) : 1);
  function problem(l, qty = l.qty) {
    if (l.manual) {
      if (!Number.isSafeInteger(qty) || qty < 1) return "Enter a whole quantity";
      if (!(num(l.rate) > 0)) return `Enter the rate for ${l.name}`;
      return "";
    }
    if (!Number.isSafeInteger(qty) || qty < 1) return "Enter a quantity (3, 1s or 2s+3)";
    const have = avail(l);
    if (qty > have) return `Only ${have} ${unitName(l.base_unit, have)} of ${l.name} available${l.batch_id ? " in this batch" : " across non-expired batches"}`;
    if (upp(l) > 1 && !l.loose && qty % upp(l)) return `${l.name} is sold as a full ${unitName(l.pack_unit, 1)} of ${upp(l)} — loose quantity is not allowed`;
    return "";
  }
  function roundBill(v) {
    if (roundMode === "NONE") return r2(v);
    if (roundMode === "NEAREST_HALF") return Math.round(v * 2) / 2;
    return Math.round(v);
  }
  function totals() {
    const gross = r2(S.lines.reduce((n, l) => n + amount(l), 0));
    const ldisc = r2(S.lines.reduce((n, l) => n + lineDisc(l), 0));
    const sub = r2(gross - ldisc);
    // the bill discount is always a percentage (0 – limit) of the bill after item discounts
    const discPct = num(S.discountPct);
    const disc = r2(sub * discPct / 100);
    const pre = r2(sub - disc);
    const net = roundBill(pre);
    return { gross, ldisc, sub, disc, discPct, net, round: r2(net - pre), units: S.lines.reduce((n, l) => n + l.qty, 0),
      billMax: r2(sub * MAXD / 100), totalMax: r2(gross * MAXD / 100) };
  }
  function billDiscProblem(t = totals()) {
    if (t.discPct < 0) return "Bill discount cannot be negative";
    if (t.discPct > MAXD) return `Bill discount is ${t.discPct}% — the maximum is ${MAXD}%`;
    if (r2(t.ldisc + t.disc) > r2(t.totalMax + 0.01 * S.lines.length)) return `Item + bill discounts ₹${money(t.ldisc + t.disc)} exceed ${MAXD}% of the MRP value — maximum ₹${money(t.totalMax)}`;
    return "";
  }

  // ---------------------------------------------------------------- render
  function manualLineHtml(l, i) {
    const bad = problem(l);
    const qtyCell = editing === i ? `<input class="qty-in num" value="${l.qty}" aria-label="Quantity">` : `<b>${l.qty}</b>`;
    const rateCell = rateEditing === i ? `<input class="rate-in num" value="${num(l.rate) || ""}" placeholder="rate" aria-label="Rate per unit">` : num(l.rate) ? money(l.rate) : '<span class="bad">rate?</span>';
    return `<tr data-i="${i}" class="${i === sel ? "sel" : ""}${bad ? " bad" : ""}" title="${esc(bad)}">
      <td class="num muted">${i + 1}</td><td class="mono${l.code ? "" : " muted"}">${esc(l.code || "TYPED")}</td>
      <td class="prod">${esc(l.name)}</td><td class="muted">—</td><td class="muted">—</td><td class="num">1</td>
      <td class="num qty">${qtyCell}</td><td class="num">${rateCell}</td>
      <td class="num disc">${discEditing === i ? `<input class="disc-in num" value="${num(l.disc) || ""}" placeholder="0" aria-label="Discount percent, max ${MAXD}">` : num(l.disc) ? `${num(l.disc)}% <small>−${money(lineDisc(l))}</small>` : '<span class="muted">—</span>'}</td>
      <td class="num strong">${money(lineNet(l))}</td></tr>`;
  }

  function lineHtml(l, i) {
    if (l.manual) return manualLineHtml(l, i);
    const a = alloc(l), first = (a[0] || {}).b || pool(l)[0] || {};
    const near = first.expiry && daysUntil(first.expiry) <= expiryDays;
    const bad = problem(l);
    const unitMrp = first.unit_mrp !== undefined ? first.unit_mrp : 0;
    const qtyCell = editing === i
      ? `<input class="qty-in num" value="${l.qty}" aria-label="Quantity in ${unitName(l.base_unit)}">`
      : `<b>${l.qty}</b>`;
    return `<tr data-i="${i}" class="${i === sel ? "sel" : ""}${bad ? " bad" : ""}" title="${esc(bad)}">
      <td class="num muted">${i + 1}</td><td class="mono">${esc(l.code)}</td>
      <td class="prod">${esc(l.name)}${l.upp > 1 && l.qty >= l.upp ? `<small>= ${esc(describe(l.qty, l.upp, l.base_unit, l.pack_unit))}</small>` : ""}</td>
      <td class="mono">${esc(first.batch_no || "—")}${a.length > 1 ? ` <span class="tag">+${a.length - 1}</span>` : ""}${l.batch_id ? ' <span class="tag" title="Chosen by cashier">M</span>' : ""}</td>
      <td class="${near ? "warn" : ""}">${fmtExpShort(first.expiry)}</td>
      <td class="num">${upp(l)}</td>
      <td class="num qty">${qtyCell}</td>
      <td class="num">${money(unitMrp)}</td>
      <td class="num disc">${discEditing === i ? `<input class="disc-in num" value="${num(l.disc) || ""}" placeholder="0" aria-label="Discount percent, max ${MAXD}">` : num(l.disc) ? `${num(l.disc)}% <small>−${money(lineDisc(l))}</small>` : '<span class="muted">—</span>'}</td>
      <td class="num strong">${money(lineNet(l))}</td></tr>`;
  }

  function render() {
    renderSaleType();
    $(".manual-badge", root).hidden = !S.manual;
    const eb = $(".edit-badge", root);
    eb.hidden = !S.editing;
    if (S.editing) eb.textContent = `EDITING ${S.editing.invoice_no}`;
    q.placeholder = S.manual ? "Manual bill: search any item (stock not checked) or type a name not in inventory"
      : "Search product name or code";
    tbody.innerHTML = S.lines.map(lineHtml).join("");
    $(".bill-empty", root).hidden = S.lines.length > 0;
    $(".bill-no", root).textContent = S.parked ? S.parked.ref : `NEW-${String(billNo).padStart(2, "0")}`;
    const c = S.customer;
    const hint = `<kbd class="cc-key" data-shortcut="pos.customer">${esc(keys.keyFor("pos.customer"))}</kbd>`;
    $(".cust-chip", root).innerHTML = c
      ? `${hint}<b>${esc(c.name)}</b> <span>${esc(c.mobile || "")}</span> <span class="muted">${esc(c.customer_id || "")}</span><span class="cc-clear" role="button" title="Back to walk-in (${esc(keys.keyFor("pos.walkin"))})">×</span>`
      : `${hint}<span class="cc-empty">Walk-in — click to choose a customer</span>`;
    renderTotals();
    renderDetail();
    if (rateEditing !== null) {
      const inp = $(".rate-in", tbody);
      if (inp) { inp.focus(); inp.select(); bindRate(inp); }
    } else if (editing !== null) {
      const inp = $(".qty-in", tbody);
      if (inp) { inp.focus(); inp.select(); bindQty(inp); }
    } else if (discEditing !== null) {
      const inp = $(".disc-in", tbody);
      if (inp) { inp.focus(); inp.select(); bindDisc(inp); }
    }
    ctx.setDirty(S.lines.length > 0);
    // the tab is named after the customer as soon as one is on the bill; otherwise the bill number
    ctx.setTitle((S.manual ? "Manual · " : "POS · ") + (S.customer && S.customer.name ? S.customer.name
      : S.parked ? S.parked.ref : `Bill ${billNo}`));
    ctx.save(S);
  }

  function renderTotals() {
    const t = totals();
    $(".t-items", root).textContent = `${S.lines.length} item${S.lines.length === 1 ? "" : "s"} · ${t.units} units`;
    $(".t-gross", root).textContent = money(t.gross);
    $(".t-ldisc", root).textContent = t.ldisc ? "−" + money(t.ldisc) : "0.00";
    $(".t-sub", root).textContent = money(t.sub);
    $(".t-bdisc", root).textContent = t.disc ? "−" + money(t.disc) : "0.00";
    const used = t.gross ? r2(((t.ldisc + t.disc) / t.gross) * 100) : 0;
    $(".t-dlim", root).textContent = `Limit ${MAXD}% · used ${used}%`;
    $(".t-dlim", root).classList.toggle("warn", used > MAXD);
    $(".t-round", root).textContent = (t.round >= 0 ? "+" : "−") + money(Math.abs(t.round));
    $(".t-netv", root).textContent = rupees(t.net);
    const saved = r2(t.gross - t.net);
    const save = $(".t-save", root);
    save.hidden = !(saved > 0.009 && S.lines.length);
    save.textContent = `Customer saves ₹${money(saved)} on MRP`;
    const disc = $(".t-disc", root);
    if (document.activeElement !== disc) disc.value = num(S.discountPct) ? String(num(S.discountPct)) : "0";
    disc.classList.toggle("bad", !!billDiscProblem(t));
    $$(".paymodes button", root).forEach((b) => b.classList.toggle("on", b.dataset.mode === S.mode));
    $(".pay-cash", root).hidden = S.mode !== "CASH";
    $(".pay-ref", root).hidden = !(S.mode === "UPI" || S.mode === "CARD");
    $(".pay-split", root).hidden = S.mode !== "SPLIT";
    const recv = $(".p-recv", root);
    if (document.activeElement !== recv) recv.value = S.received;
    const change = S.received === "" ? 0 : r2(num(S.received) - t.net);
    $(".p-change", root).textContent = money(Math.max(change, 0));
    $(".p-change", root).classList.toggle("neg", change < 0);
    if (S.mode === "SPLIT") {
      $$(".s-amt", root).forEach((inp) => { if (document.activeElement !== inp) inp.value = S.split[inp.dataset.split]; });
      const paid = r2(["CASH", "UPI", "CARD"].reduce((n, m) => n + num(S.split[m]), 0));
      const left = r2(t.net - paid);
      $(".s-left-l", root).textContent = left < 0 ? "Over by" : "Remaining";
      $(".s-left", root).textContent = money(Math.abs(left));
      $(".s-left", root).classList.toggle("neg", left !== 0);
      const sr = $(".s-recv", root);
      if (document.activeElement !== sr) sr.value = S.splitRecv || "";
      const cashPart = num(S.split.CASH);
      $(".s-change", root).textContent = money(Math.max(r2(num(S.splitRecv || cashPart) - cashPart), 0));
    }
    $(".p-ref", root).value = S.ref || "";
    $(".pay-err", root).textContent = payProblem(t) || "";
    $(".finalize", root).disabled = !S.lines.length || busy;
  }

  function renderDetail() {
    const l = S.lines[sel];
    const d = $(".pos-detail", root);
    if (!l) { d.innerHTML = S.manual ? '<span class="muted">Manual bill · search any item or type one not in inventory · quantity, Tab → discount, Tab → rate · the bill never changes stock</span>'
      : '<span class="muted">No line selected · ↑↓ in the bill to select · see the shortcut bar for quantity, batch and remove</span>'; return; }
    if (l.manual) {
      d.innerHTML = `<b>${esc(l.name)}</b><span>${l.item_id ? "From inventory — manual bill: stock not checked or changed" : "Typed item — not in inventory"}</span><span>Rate <b>₹${money(l.rate)}</b> <kbd>Enter</kbd> on the line: rate → quantity · Tab → discount</span>
        <span>Discount <b>${num(l.disc) ? `${num(l.disc)}% (−₹${money(lineDisc(l))})` : "none"}</b></span>`;
      return;
    }
    const a = alloc(l), first = (a[0] || {}).b || pool(l)[0] || {};
    d.innerHTML = `<b>${esc(l.name)}</b>
      <span>${esc(l.form || "")}</span>
      <span>Pack <b>${upp(l) > 1 ? `${upp(l)} ${unitName(l.base_unit, upp(l))} / ${unitName(l.pack_unit, 1)}` : esc(l.pack_raw || "1")}</b></span>
      <span>Selling unit <b>${esc(unitName(l.base_unit, 1))}</b></span>
      <span>Loose sale <b>${l.loose ? "Yes" : "No"}</b></span>
      <span>Pack MRP <b>₹${money(first.pack_mrp)}</b></span>
      ${upp(l) > 1 ? `<span>Unit MRP <b>₹${money(first.unit_mrp)}</b></span>` : ""}
      <span>Discount <b>${num(l.disc) ? `${num(l.disc)}% (−₹${money(lineDisc(l))})` : "none"}</b> <kbd data-shortcut="pos.itemDisc">${esc(keys.keyFor("pos.itemDisc"))}</kbd></span>
      <span>Available <b>${avail(l)}</b> (${esc(describe(avail(l), l.upp, l.base_unit, l.pack_unit))})</span>
      ${a.length ? `<span>From ${a.map((x) => `${esc(x.b.batch_no || "—")} ×${x.qty} @₹${money(x.b.unit_mrp)} · Exp ${fmtExp(x.b.expiry)}`).join(" · ")}</span>` : ""}
      ${l.content ? `<span>Content <b>${esc(l.content)}</b></span>` : ""}
      ${l.rack ? `<span>Rack <b>${esc(l.rack)}</b></span>` : ""}`;
  }

  function payProblem(t = totals()) {
    if (!S.lines.length) return "";
    const bad = S.lines.map((l) => problem(l) || discProblem(num(l.disc))).find(Boolean);
    if (bad) return bad;
    const dp = billDiscProblem(t);
    if (dp) return dp;
    if (S.mode === "CASH" && S.received !== "" && num(S.received) < t.net) return `Received is ₹${money(t.net - num(S.received))} short`;
    if (S.mode === "SPLIT") {
      const paid = r2(["CASH", "UPI", "CARD"].reduce((n, m) => n + num(S.split[m]), 0));
      if (paid !== t.net) return `Split must add up to ₹${money(t.net)} (now ₹${money(paid)})`;
      if (S.splitRecv && num(S.splitRecv) < num(S.split.CASH)) return "Cash received is less than the cash part";
    }
    return "";
  }

  // ---------------------------------------------------------------- selection & editing
  function select(i) {
    if (!S.lines.length) { sel = -1; renderDetail(); return; }
    sel = Math.max(0, Math.min(S.lines.length - 1, i));
    $$("tr", tbody).forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.i) === sel));
    const tr = $(`tr[data-i="${sel}"]`, tbody);
    if (tr) tr.scrollIntoView({ block: "nearest" });
    renderDetail();
  }
  function editQty(i = sel, seed = null) {
    if (i < 0 || !S.lines[i]) return;
    sel = i; editing = i; discEditing = null;
    render();
    const inp = $(".qty-in", tbody);
    if (inp && seed !== null) { inp.value = seed; inp.setSelectionRange(seed.length, seed.length); }
  }
  function editRate(i = sel) {
    if (i < 0 || !S.lines[i] || !S.lines[i].manual) return;
    sel = i; rateEditing = i; editing = null; discEditing = null;
    render();
  }
  function bindRate(inp) {
    inp.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const i = rateEditing, l = S.lines[i];
        const v = Number(String(inp.value).replace(/,/g, ""));
        if (!(v > 0)) { ctx.status("Enter the rate per unit (more than 0)", "error"); inp.classList.add("bad"); inp.select(); return; }
        l.rate = r2(v); rateEditing = null;
        editQty(i);                                    // name → rate → quantity
      } else if (e.key === "Escape") {
        e.preventDefault(); rateEditing = null;
        const l = S.lines[sel];
        if (l && l.manual && !(num(l.rate) > 0)) { S.lines.splice(sel, 1); sel = S.lines.length - 1; }
        render(); q.focus();
      }
    };
  }
  function addManualProduct(p) {
    const b = p.batches[0];
    const rate = b ? num(b.pack_mrp) : 0;
    S.lines.push({ manual: true, name: String(p.name).slice(0, 250), code: p.code, item_id: p.id, qty: 1, rate, disc: 0,
      batch_id: null, batches: [], base_unit: "UNIT", pack_unit: "UNIT", upp: 1, pack: p.pack_raw || "" });
    q.value = ""; closeDrop();
    const i = S.lines.length - 1;
    if (rate > 0) {
      editQty(i);
      ctx.status(`${p.name}: quantity, Tab → discount · rate ₹${money(rate)} (MRP) — Enter on the line changes it`);
    } else {
      editRate(i);
      ctx.status(`${p.name} has no MRP on record — type the rate, Enter, then the quantity`);
    }
  }
  function addManual(name) {
    if (!name) return;
    S.lines.push({ manual: true, name: name.slice(0, 250), qty: 1, rate: 0, disc: 0, item_id: null, batch_id: null, batches: [], base_unit: "UNIT", pack_unit: "UNIT", upp: 1 });
    q.value = "";
    closeDrop();
    editRate(S.lines.length - 1);
    ctx.status(`${name}: type the rate per unit, Enter, then the quantity`);
  }
  // follow-up for the customer on this bill (or the bill just saved, when the studio is showing it)
  async function followUp() {
    const saved = studioOpen && studio.sale && studio.sale.customer_id ? studio.sale : null;
    const cust = saved ? { id: saved.customer_id, name: saved.customer, mobile: saved.mobile } : S.customer;
    if (!cust || !cust.id) { ctx.status("Select a customer first (# in the item box or " + keys.keyFor("pos.customer") + ")", "warn"); return; }
    if (!ctx.boot.can || !ctx.boot.can["followups.manage"]) { ctx.status("Follow-ups need the follow-up right", "warn"); return; }
    const sale = saved ? { id: saved.id, invoice_no: saved.invoice_no } : (lastBill && lastBill.customer_id === cust.id ? { id: lastBill.id, invoice_no: lastBill.no } : null);
    await followUpPopover({ anchor: studioOpen ? studio.el.querySelector(".studio-head") : $(".cust-chip", root), customer: cust, sale, ctx });
  }
  function toggleManual() {
    if (S.lines.length) { ctx.status(`Finish, hold or clear this bill before switching to ${S.manual ? "a stock bill" : "a manual bill"} (Alt+N opens another bill)`, "warn"); return; }
    S.manual = !S.manual;
    render(); q.focus();
    ctx.status(S.manual ? "Manual bill: items not kept in stock · no inventory effect · numbered MB-…" : "Back to a normal stock bill", "ok");
  }

  function bindQty(inp) {
    inp.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const i = editing, l = S.lines[i];
        const v = parseQty(inp.value, l.upp);
        const err = problem(l, v);
        if (err) { ctx.status(err, "error"); inp.classList.add("bad"); inp.select(); return; }
        l.qty = v; editing = null;
        if (l.manual && e.key === "Enter") { render(); q.focus(); ctx.status(`${l.name}: ${v} × ₹${money(l.rate)} = ₹${money(lineNet(l))}`, "ok"); return; }
        if (e.key === "Tab" && !e.shiftKey && CAN["sales.discount"]) { editDisc(i); return; }  // Qty → Tab → Disc %
        render(); q.focus();
        ctx.status(`${l.name}: ${v} ${unitName(l.base_unit, v)} · ₹${money(lineNet(l))}`, "ok");
      } else if (e.key === "Escape") { e.preventDefault(); editing = null; render(); q.focus(); }
      else if (/^F\d+$/.test(e.key)) { e.preventDefault(); inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); }
    };
    inp.oninput = () => {
      const l = S.lines[editing];
      const v = parseQty(inp.value, l.upp);
      inp.classList.toggle("bad", !!problem(l, v));
      const hint = Number.isFinite(v) && v > 0 ? `${v} ${unitName(l.base_unit, v)}${l.upp > 1 && v >= l.upp ? " = " + describe(v, l.upp, l.base_unit, l.pack_unit) : ""} · ₹${money(amount({ ...l, qty: v }))}` : "";
      ctx.status(hint || "Type 3, 1s (one " + unitName(l.pack_unit, 1) + ") or 2s+3");
    };
    inp.onblur = () => { if (editing !== null) setTimeout(() => { if (editing !== null && document.activeElement !== inp) { editing = null; render(); } }, 0); };
  }
  // ---- item discount (F7, or Tab from Qty): percent, never above the store limit
  function editDisc(i = sel) {
    if (i < 0 || !S.lines[i]) { ctx.status("Select a bill line first (↑↓), then use Item discount", "warn"); return; }
    if (!CAN["sales.discount"]) { ctx.status("You do not have discount permission", "warn"); return; }
    sel = i; editing = null; discEditing = i;
    render();
    ctx.status(`Item discount for ${S.lines[i].name}: 0–${MAXD}% · Enter saves · Esc cancels`);
  }
  function bindDisc(inp) {
    const i = discEditing, l = S.lines[i];
    const commit = () => {
      const raw = inp.value.trim().replace("%", "");
      const v = raw === "" ? 0 : Number(raw);
      const err = discProblem(v);
      if (err) { ctx.status(err, "error"); inp.classList.add("bad"); inp.select(); return false; }
      l.disc = r2(v); discEditing = null;
      const t = totals(), dp = billDiscProblem(t);
      render(); q.focus();
      ctx.status(dp || (v ? `${l.name}: ${v}% off · −₹${money(lineDisc(l))} · now ₹${money(lineNet(l))}` : `${l.name}: no discount`), dp ? "warn" : "ok");
      return true;
    };
    inp.onkeydown = (e) => {
      e.stopPropagation();
      if (e.key === "Tab" && l.manual && !e.shiftKey) { e.preventDefault(); if (commit()) editRate(i); }
      else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { e.preventDefault(); discEditing = null; render(); q.focus(); }
      else if (/^F\d+$/.test(e.key)) { e.preventDefault(); commit(); }
    };
    inp.oninput = () => {
      const raw = inp.value.trim().replace("%", "");
      const v = raw === "" ? 0 : Number(raw);
      const err = discProblem(v);
      inp.classList.toggle("bad", !!err);
      ctx.status(err || `${v}% off ₹${money(amount(l))} = −₹${money(lineDisc(l, v))} → ₹${money(r2(amount(l) - lineDisc(l, v)))}`, err ? "error" : "info");
    };
    inp.onblur = () => { setTimeout(() => { if (discEditing !== null && document.activeElement !== inp) { discEditing = null; render(); } }, 0); };
  }
  function removeLine(i = sel) {
    if (!S.lines[i]) return;
    const [l] = S.lines.splice(i, 1);
    editing = null; discEditing = null;
    sel = Math.min(i, S.lines.length - 1);
    render();
    ctx.status(`Removed ${l.name}`);
  }

  // ---------------------------------------------------------------- adding products
  function lineFrom(p) {
    return { item_id: p.id, code: p.code, name: p.name, pack_raw: p.pack_raw, upp: p.upp || 1, loose: !!p.loose,
      base_unit: p.base_unit, pack_unit: p.pack_unit, form: p.form, rack: p.rack, batches: p.batches, batch_id: null, qty: 0,
      content: p.content || "", disc: 0 };
  }
  function addProduct(p) {
    if (!p.batches.length) { ctx.status(`${p.name} has no sellable stock (out of stock or only expired batches)`, "error"); return; }
    let i = S.lines.findIndex((l) => l.item_id === p.id && !l.batch_id);
    if (i >= 0) {
      S.lines[i].batches = p.batches;           // fresh stock figures
    } else {
      const l = lineFrom(p);
      l.qty = Math.min(step(l), avail(l));
      if (problem(l)) { ctx.status(problem(l), "error"); return; }
      S.lines.push(l);
      i = S.lines.length - 1;
    }
    closeDrop();
    q.value = "";
    editQty(i);
    const l = S.lines[i];
    const b = l.batches[0];
    if (b) ctx.status(l.loose
      ? `${l.name}: qty in ${unitName(l.base_unit)} · 1 ${unitName(l.pack_unit, 1)} = ${l.upp} · ₹${money(b.unit_mrp)} per ${unitName(l.base_unit, 1)} (strip MRP ₹${money(b.pack_mrp)} ÷ ${l.upp}) · 1s = full ${unitName(l.pack_unit, 1)}`
      : `${l.name}: sold per ${unitName(l.base_unit, 1)}${l.content ? " (" + l.content + ")" : ""} · ₹${money(b.pack_mrp)}`);
  }

  // ---------------------------------------------------------------- search box: products / #customers / parked
  let mode = null;            // "items" | "customers" | "newcust" | "parked" | "batch"
  let results = [], at = 0, ctrl = null, lastTerm = "";
  const cache = new Map();

  function closeDrop() { drop.hidden = true; drop.innerHTML = ""; mode = null; results = []; }

  function renderItems() {
    drop.hidden = false;
    drop.innerHTML = results.length ? `<table class="res"><thead><tr><th>Code</th><th>Product</th><th class="num">Pack</th><th class="num">Available</th><th>Expiry</th><th class="num">MRP</th><th>Rack</th></tr></thead><tbody>${
      results.map((p, i) => {
        const b = p.batches[0];
        const inBill = S.lines.filter((l) => l.item_id === p.id).reduce((n, l) => n + l.qty, 0);
        return `<tr data-i="${i}" class="${i === at ? "on" : ""}${p.batches.length || S.manual ? "" : " off"}">
          <td class="mono">${esc(p.code)}</td><td><b>${esc(p.name)}</b>${p.generic ? `<small>${esc(p.generic)}</small>` : ""}</td>
          <td class="num">${p.upp > 1 ? p.upp : esc(p.pack_raw || "1")}</td>
          <td class="num">${p.batches.length ? `${p.stock} ${esc(unitName(p.base_unit, p.stock))}<small>${esc(p.stock_label)}${inBill ? " · " + inBill + " in bill" : ""}</small>` : '<span class="bad-t">Out of stock</span>'}</td>
          <td class="${b && daysUntil(b.expiry) <= expiryDays ? "warn" : ""}">${b ? fmtExpShort(b.expiry) : "—"}</td>
          <td class="num">${b ? `₹${money(b.unit_mrp)}<small>${p.upp > 1 ? "/" + esc(unitName(p.base_unit, 1)) + " · ₹" + money(b.pack_mrp) + "/" + esc(unitName(p.pack_unit, 1)) : ""}</small>` : "—"}</td>
          <td>${esc(p.rack || "")}</td></tr>`;
      }).join("")}${S.manual ? typedRow(results.length) : ""}</tbody></table><div class="drop-foot">${S.manual
        ? "Manual bill · ↑↓ choose · Enter add (stock is not checked or changed) · last row adds the typed name · Esc close"
        : "↑↓ choose · Enter add · Esc close · # customer"}</div>`
      : S.manual ? `<table class="res"><tbody>${typedRow(0)}</tbody></table><div class="drop-foot">Not in inventory · Enter adds it as a typed item</div>`
      : `<div class="drop-empty">No product matches “${esc(lastTerm)}”.</div>`;
  }

  const typedRow = (i) => `<tr data-i="${i}" class="new${i === at ? " on" : ""}"><td colspan="7">＋ Add “${esc(lastTerm)}” as a typed item (not in inventory)</td></tr>`;

  function renderCustomers() {
    drop.hidden = false;
    const term = q.value.replace(/^#/, "").trim();
    const rows = results.map((c, i) => `<tr data-i="${i}" class="${i === at ? "on" : ""}"><td class="mono">${esc(c.customer_id)}</td><td><b>${esc(c.name)}</b></td><td class="mono">${esc(c.mobile)}</td><td>${esc(c.doctor || "")}</td></tr>`);
    const newIdx = results.length;
    rows.push(`<tr data-i="${newIdx}" class="new${at === newIdx ? " on" : ""}"><td colspan="4">＋ New customer${term ? ` “${esc(term)}”` : ""} — create here without leaving the bill</td></tr>`);
    drop.innerHTML = `<table class="res"><thead><tr><th>ID</th><th>Customer</th><th>Mobile</th><th>Doctor</th></tr></thead><tbody>${rows.join("")}</tbody></table>
      <div class="drop-foot">↑↓ choose · Enter select · Esc walk-in</div>`;
  }

  function renderNewCustomer(term) {
    mode = "newcust";
    drop.hidden = false;
    const digits = term.replace(/\D/g, "");
    const isPhone = digits.length >= 5 && digits.length === term.replace(/[\s+-]/g, "").length;
    drop.innerHTML = `<form class="newcust" autocomplete="off">
      <b>New customer</b>
      <label>Mobile<input name="mobile" inputmode="tel" maxlength="15" value="${esc(isPhone ? digits : "")}" required></label>
      <label>Name<input name="name" maxlength="150" value="${esc(isPhone ? "" : term)}" required></label>
      <label>Doctor<input name="doctor_name" maxlength="150" placeholder="optional"></label>
      <span class="nc-err" role="alert"></span>
      <span class="hint">Enter = next / save · Esc = cancel</span></form>`;
    const form = $("form", drop);
    const inputs = $$("input", form);
    (isPhone ? inputs[1] : inputs[0]).focus();
    form.addEventListener("keydown", async (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); closeDrop(); q.value = ""; q.focus(); return; }
      if (e.key !== "Enter") return;
      e.preventDefault();
      const mobile = form.mobile.value.trim(), name = form.name.value.trim();
      // Enter walks Mobile → Name, then saves (Doctor is optional)
      if (document.activeElement === form.mobile && !name) { form.name.focus(); return; }
      if (!mobile || mobile.replace(/\D/g, "").length !== 10) { $(".nc-err", form).textContent = "Enter a 10-digit mobile number"; form.mobile.focus(); return; }
      if (!name) { $(".nc-err", form).textContent = "Enter the customer's name"; form.name.focus(); return; }
      if (!CAN["customers.create"]) { $(".nc-err", form).textContent = "You are not allowed to add customers"; return; }
      try {
        const d = await api("/api/customers", { method: "POST", body: { mobile, name, doctor_name: form.doctor_name.value.trim() } });
        setCustomer(d.customer);
        ctx.status(`Customer ${d.customer.name} created and added to the bill`, "ok");
      } catch (err) {
        if (err.status === 409 && err.detail && err.detail.customer) { setCustomer(err.detail.customer); ctx.status("That mobile already belongs to " + err.detail.customer.name + " — selected", "warn"); }
        else $(".nc-err", form).textContent = err.message;
      }
    });
  }

  // the same picker for the shortcut and the mouse: customer search in the item box, results clickable
  function chooseCustomer() {
    q.value = "#"; q.focus();
    mode = "customers"; results = []; at = 0; renderCustomers();
    drop.insertAdjacentHTML("afterbegin", '<div class="drop-hint">Type a name, mobile or customer ID — or click a result</div>');
  }
  $(".cust-chip", root).addEventListener("mousedown", (e) => e.preventDefault());      // keep an open qty edit
  $(".cust-chip", root).addEventListener("click", (e) => {
    if (e.target.closest(".cc-clear")) { if (S.customer) { S.customer = null; render(); ctx.status("Walk-in customer"); q.focus(); } return; }
    chooseCustomer();
  });

  function setCustomer(c) {
    S.customer = c ? { id: c.id, name: c.name, mobile: c.mobile, customer_id: c.customer_id, customer_type: saleType(c.customer_type ?? c.type) } : null;
    if (!S.saleTypeExplicit) S.saleType = saleType(c?.customer_type ?? c?.type);
    closeDrop(); q.value = ""; render(); q.focus();
  }

  async function runSearch() {
    const raw = q.value;
    const term = raw.trim();
    if (ctrl) ctrl.abort();
    if (!term) { closeDrop(); return; }
    ctrl = new AbortController();
    const key = term.toLowerCase();
    try {
      if (term.startsWith("#")) {
        mode = "customers";
        const t = term.slice(1).trim();
        results = t ? (await api("/api/erp/customers?q=" + encodeURIComponent(t), { signal: ctrl.signal })).customers : [];
        at = 0; renderCustomers();
        return;
      }
      mode = "items";
      lastTerm = term;
      const hit = cache.get(key);
      if (hit && Date.now() - hit.t < 20000) results = hit.items;
      else {
        results = (await api("/api/erp/pos/search?q=" + encodeURIComponent(term), { signal: ctrl.signal })).items;
        cache.set(key, { t: Date.now(), items: results });
        if (cache.size > 200) cache.delete(cache.keys().next().value);
      }
      if (q.value.trim() !== term) return;   // a newer keystroke is in flight
      if (S.manual) {                        // manual bill: exact name first, else the first match, else the typed row
        at = results.findIndex((p) => p.name.toLowerCase() === key);
        if (at < 0) at = 0;
      } else {
        at = results.findIndex((p) => p.batches.length);
        if (at < 0) at = 0;
      }
      renderItems();
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  const search = debounce(runSearch, 70);

  function moveDrop(d) {
    const n = mode === "customers" || (mode === "items" && S.manual) ? results.length + 1 : results.length;
    if (!n) return;
    at = (at + d + n) % n;
    if (mode === "customers") renderCustomers(); else if (mode === "items") renderItems(); else if (mode === "parked") renderParked(); else if (mode === "batch") renderBatch();
    const on = $("tr.on", drop);
    if (on) on.scrollIntoView({ block: "nearest" });
  }

  async function chooseDrop() {
    if (mode === "items") {
      const p = results[at];
      if (S.manual) { if (p) addManualProduct(p); else addManual(q.value.trim()); return; }
      if (p) addProduct(p);
    } else if (mode === "customers") {
      if (at < results.length) { const c = results[at]; setCustomer(c); ctx.status(`Customer: ${c.name}`, "ok"); }
      else renderNewCustomer(q.value.replace(/^#/, "").trim());
    } else if (mode === "parked") resumeParked(results[at]);
    else if (mode === "batch") chooseBatch();
  }

  q.addEventListener("input", () => {
    if (q.value.trim().startsWith("#")) search.flush(); else search();
  });
  q.addEventListener("keydown", async (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!drop.hidden && results.length + (mode === "customers" ? 1 : 0)) moveDrop(1);
      else if (S.lines.length) { wrap.focus(); select(sel < 0 ? 0 : sel); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!drop.hidden) moveDrop(-1); else if (S.lines.length) { wrap.focus(); select(sel < 0 ? S.lines.length - 1 : sel); }
    } else if (e.key === "Enter") {
      e.preventDefault();
      // held bills (Ctrl+R) and the batch picker (F5) open with an empty item box: Enter picks the highlighted row
      if (!drop.hidden && (mode === "parked" || mode === "batch")) { await chooseDrop(); return; }
      const term = q.value.trim();
      if (!term) { if (S.lines.length) { $(".p-recv", root).focus(); } return; }
      if (S.manual && !term.startsWith("#")) { search.cancel(); await runSearch(); await chooseDrop(); return; }
      // scanner / fast typist: search now, then add an exact single match
      if (mode !== "customers" && mode !== "parked") { search.cancel(); await runSearch(); }
      if (mode === "customers" && !results.length) { renderNewCustomer(term.slice(1).trim()); return; }
      if (mode === "items" && results.length === 1 && results[0].batches.length) { addProduct(results[0]); return; }
      await chooseDrop();
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (!drop.hidden) closeDrop(); else q.value = "";
    }
  });
  drop.addEventListener("mousedown", (e) => {
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    e.preventDefault();
    at = Number(tr.dataset.i);
    chooseDrop();
  });

  // ---------------------------------------------------------------- batch selector (F5)
  let batchLine = null;
  function renderBatch() {
    const l = S.lines[batchLine];
    drop.hidden = false;
    const rows = [{ id: null, label: "Auto — FEFO (earliest expiry first)" }].concat(l.batches);
    results = rows;
    drop.innerHTML = `<div class="drop-title">Batch for <b>${esc(l.name)}</b></div><table class="res"><thead><tr><th>Batch</th><th>Expiry</th><th class="num">Pack MRP</th><th class="num">Unit MRP</th><th class="num">Available</th></tr></thead><tbody>${
      rows.map((b, i) => b.id === null
        ? `<tr data-i="${i}" class="${i === at ? "on" : ""}"><td colspan="5"><b>${b.label}</b></td></tr>`
        : `<tr data-i="${i}" class="${i === at ? "on" : ""}"><td class="mono">${esc(b.batch_no || "—")}</td><td class="${daysUntil(b.expiry) <= expiryDays ? "warn" : ""}">${fmtExp(b.expiry)}</td><td class="num">${money(b.pack_mrp)}</td><td class="num">${money(b.unit_mrp)}</td><td class="num">${b.stock}</td></tr>`).join("")}</tbody></table>
      <div class="drop-foot">↑↓ choose · Enter apply · Esc cancel · a chosen batch is recorded as a manual override</div>`;
  }
  function openBatch() {
    const l = S.lines[sel];
    if (!l) return;
    if (l.manual) { ctx.status("A manual line has no batch — it is not from stock", "warn"); return; }
    batchLine = sel; mode = "batch";
    at = l.batch_id ? l.batches.findIndex((b) => b.id === l.batch_id) + 1 : 0;
    renderBatch();
    q.focus();
  }
  function chooseBatch() {
    const l = S.lines[batchLine], choice = results[at];
    l.batch_id = choice ? choice.id : null;
    closeDrop();
    const err = problem(l);
    if (err) ctx.status(err, "warn"); else ctx.status(l.batch_id ? `Batch ${choice.batch_no} chosen for ${l.name}` : `${l.name}: automatic FEFO`, "ok");
    render(); q.focus();
  }

  // ---------------------------------------------------------------- hold / resume
  async function hold() {
    if (S.editing) { ctx.status(`You are editing ${S.editing.invoice_no} — save it (F12) or close the tab to leave it unchanged`, "warn"); return false; }
    if (!S.lines.length) { ctx.status("Nothing to hold", "warn"); return false; }
    try {
      const cart = S.lines.map((l) => (l.manual ? { ...l, quantity: l.qty } : { ...l, quantity: l.qty, rate: l.qty ? r2(lineNet(l) / l.qty) : 0 }));
      const d = await api("/api/pos/park", { method: "POST", body: { payload: { cart, manual: !!S.manual, customer: S.customer, customer_type: S.saleType, pos: "erp" }, customer_id: S.customer && S.customer.id } });
      ctx.status(`Bill held as ${d.park_reference} · use Resume held bill to return`, "ok");
      S = blank(); sel = -1; render(); q.focus();
      return true;
    } catch (err) { ctx.status(err.message, "error"); return false; }
  }

  // Alt+W: an empty bill closes at once; a bill with items asks Hold / Discard / Keep
  function beforeClose() {
    if (!S.lines.length) return Promise.resolve(true);
    const t = totals();
    return new Promise((resolve) => {
      const prev = document.activeElement;
      const el = h(`<div class="modal-backdrop"><div class="modal small" role="alertdialog" aria-modal="true" aria-label="Close bill">
        <header><h2>Close ${esc(S.customer ? S.customer.name + "'s bill" : "Bill " + billNo)}?</h2></header>
        <div class="modal-body"><p>${S.lines.length} item${S.lines.length === 1 ? "" : "s"} · ₹${money(t.net)} not yet billed.</p></div>
        <footer><button type="button" class="btn" data-a="keep">Keep open <kbd>Esc</kbd></button>
        <button type="button" class="btn danger" data-a="discard">Discard <kbd>D</kbd></button>
        <button type="button" class="btn primary" data-a="hold">Hold &amp; close <kbd>H</kbd></button></footer></div></div>`);
      const done = async (a) => {
        el.remove();
        if (a === "hold") {
          const ok = await hold();
          resolve(ok);
          if (!ok && prev && prev.focus) prev.focus();
          return;
        }
        if (a === "discard") { S = blank(); ctx.save(S); resolve(true); return; }
        if (prev && prev.focus) prev.focus();
        resolve(false);
      };
      el.addEventListener("click", (e) => { const b = e.target.closest("[data-a]"); if (b) done(b.dataset.a); });
      el.addEventListener("keydown", (e) => {
        e.stopPropagation(); e.preventDefault();
        const key = e.key.toLowerCase();
        if (key === "escape" || key === "n") done("keep");
        else if (key === "h" || key === "enter") done("hold");
        else if (key === "d") done("discard");
      });
      document.body.append(el);
      el.querySelector('[data-a="hold"]').focus();
    });
  }
  function renderParked() {
    drop.hidden = false;
    drop.innerHTML = results.length ? `<div class="drop-title">Held bills</div><table class="res"><thead><tr><th>Ref</th><th>Customer</th><th class="num">Items</th><th class="num">Total</th><th>Held at</th><th>By</th></tr></thead><tbody>${
      results.map((p, i) => `<tr data-i="${i}" class="${i === at ? "on" : ""}"><td class="mono">${esc(p.park_reference)}</td><td>${esc(p.customer ? p.customer.name : "Walk-in")}</td><td class="num">${p.items}</td><td class="num">${money(p.total)}</td><td>${esc(p.parked_at || "")}</td><td>${esc(p.parked_by || "")}</td></tr>`).join("")}</tbody></table><div class="drop-foot">Enter resume · Esc close</div>`
      : '<div class="drop-empty">No held bills.</div>';
  }
  async function showParked() {
    try {
      results = (await api("/api/erp/pos/parked")).parked; at = 0; mode = "parked"; renderParked(); q.focus();
    } catch (err) { ctx.status(err.message, "error"); }
  }
  async function resumeParked(p) {
    if (!p) return;
    if (S.lines.length) { ctx.status("Finish or hold the current bill first (use the New bill shortcut to open another tab)", "warn"); return; }
    try {
      await api(`/api/pos/parked/${p.id}/resume`, { method: "POST" });
      const cart = (p.payload && p.payload.cart) || [];
      const lines = [];
      const manual = !!(p.payload && p.payload.manual);
      for (const c of cart) {
        if (manual) { lines.push({ manual: true, name: c.name, code: c.code || "", pack: c.pack || "", qty: Number(c.qty || c.quantity) || 1, rate: num(c.rate), disc: Math.min(num(c.disc), MAXD), item_id: c.item_id || null, batch_id: null, batches: [], base_unit: "UNIT", pack_unit: "UNIT", upp: 1 }); continue; }
        if (!c.item_id) continue;
        const fresh = await api(`/api/items/${c.item_id}/batches`);
        const batches = (fresh.batches || []).map((b) => ({ id: b.id, batch_no: b.batch_no, expiry: b.expiry, stock: b.quantity, pack_mrp: b.mrp, upp: b.units_per_pack, unit_mrp: b.unit_mrp }));
        lines.push({ ...lineFrom({ id: c.item_id, code: fresh.article_id, name: fresh.name, pack_raw: fresh.pack_size, upp: fresh.units_per_pack,
          loose: fresh.loose_sale, base_unit: fresh.base_unit, pack_unit: fresh.pack_unit, form: fresh.dosage_form, rack: c.rack, batches }),
        batch_id: batches.some((b) => b.id === c.batch_id) ? c.batch_id : null, qty: Number(c.qty || c.quantity) || 1,
        disc: Math.min(num(c.disc), MAXD) });
      }
      S = blank();
      S.manual = manual;
      S.lines = lines;
      S.customer = (p.payload && p.payload.customer) || (p.customer ? { id: p.customer.id, name: p.customer.name, mobile: p.customer.mobile, customer_id: p.customer.customer_id } : null);
      S.saleType = saleType(p.payload?.customer_type ?? S.customer?.customer_type ?? S.customer?.type);
      S.saleTypeExplicit = !!p.payload?.customer_type;
      S.parked = { id: p.id, ref: p.park_reference };
      sel = lines.length - 1;
      closeDrop(); render(); q.focus();
      ctx.status(`Resumed ${p.park_reference}` + (lines.some((l) => problem(l)) ? " — check highlighted lines (stock changed)" : ""), "ok");
    } catch (err) { ctx.status(err.message, "error"); }
  }

  // ---------------------------------------------------------------- payment & finalize
  function setMode(m) {
    S.mode = m;
    render();
    if (m === "CASH") { const r = $(".p-recv", root); r.focus(); r.select(); }
    else if (m === "SPLIT") { const t = totals(); if (!["CASH", "UPI", "CARD"].some((k) => num(S.split[k]))) S.split = { CASH: "", UPI: "", CARD: "" }; renderTotals(); $(".s-amt", root).focus(); ctx.status(`Split ₹${money(t.net)} across Cash / UPI / Card`); }
    else { const r = $(".p-ref", root); r.focus(); }
  }
  $(".paymodes", root).addEventListener("click", (e) => { const b = e.target.closest("[data-mode]"); if (b) setMode(b.dataset.mode); });
  const recv = $(".p-recv", root);
  recv.addEventListener("input", () => { S.received = recv.value.trim(); renderTotals(); ctx.save(S); });
  recv.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); finalize(); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); q.focus(); }
  });
  $(".p-ref", root).addEventListener("input", (e) => { S.ref = e.target.value; ctx.save(S); });
  $(".p-ref", root).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); finalize(); } if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); q.focus(); } });
  $$(".s-amt", root).forEach((inp, i, all) => {
    inp.addEventListener("input", () => { S.split[inp.dataset.split] = inp.value.trim(); renderTotals(); ctx.save(S); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault(); e.stopPropagation();
        const t = totals();
        const others = r2(["CASH", "UPI", "CARD"].filter((m) => m !== inp.dataset.split).reduce((n, m) => n + num(S.split[m]), 0));
        if (!inp.value.trim() && others < t.net) { S.split[inp.dataset.split] = String(r2(t.net - others)); renderTotals(); }
        (all[i + 1] || $(".s-recv", root)).focus();
      } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); q.focus(); }
    });
    inp.addEventListener("focus", () => inp.select());
  });
  const sRecv = $(".s-recv", root);
  sRecv.addEventListener("input", () => { S.splitRecv = sRecv.value.trim(); renderTotals(); });
  sRecv.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); finalize(); } if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); q.focus(); } });
  const discIn = $(".t-disc", root);
  // bill discount: always a percentage, 0 – limit ("10" = 10%); anything else is refused
  // a refused entry goes back to the value the field had before editing started
  let lastGoodDisc = { discount: 0, discountPct: num(S.discountPct) };
  discIn.addEventListener("focus", () => { lastGoodDisc = { discount: 0, discountPct: num(S.discountPct) }; });
  discIn.addEventListener("blur", () => {
    if (!Number.isFinite(S.discountPct) || billDiscProblem()) {
      Object.assign(S, lastGoodDisc);
      ctx.status(`Bill discount refused — enter 0 to ${MAXD}%`, "error");
      renderTotals(); ctx.save(S);
    }
  });
  function readBillDisc() {
    const raw = discIn.value.trim().replace("%", "");
    return { discount: 0, discountPct: raw === "" ? 0 : Number(raw) };
  }
  discIn.addEventListener("input", () => {
    Object.assign(S, readBillDisc());
    const t = totals(), err = !Number.isFinite(S.discountPct) ? `Enter a percentage from 0 to ${MAXD}` : billDiscProblem(t);
    renderTotals();
    ctx.status(err || (t.disc ? `Bill discount −₹${money(t.disc)} · net ₹${money(t.net)}` : "No bill discount"), err ? "error" : "info");
    if (!err) ctx.save(S);
  });
  discIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      const bad = !Number.isFinite(S.discountPct) || billDiscProblem();
      if (bad) {  // not accepted: back to the last valid discount
        Object.assign(S, lastGoodDisc);
        ctx.status(`Bill discount refused — enter 0 to ${MAXD}% (item + bill together also stay within ${MAXD}%)`, "error");
        discIn.blur(); renderTotals();
      }
      q.focus();
    }
  });
  $(".finalize", root).onclick = () => finalize();

  // ---------------------------------------------------------------- sale summary → complete → invoice Y/N
  // A panel over this POS tab only (other tabs stay usable). Keys arrive through
  // onKey below: review = Enter completes / Esc back; done = Y invoice / N no.
  let overlay = null;
  const PAD = (v, n, right) => { const t = String(v); return right ? t.padStart(n) : t.padEnd(n); };
  function wrapText(text, n) {
    const out = []; let cur = "";
    for (const w of String(text).split(/\s+/).filter(Boolean)) {
      for (let piece = w; piece;) {
        const room = cur ? n - cur.length - 1 : n;
        if (piece.length <= room) { cur = cur ? cur + " " + piece : piece; piece = ""; }
        else if (!cur) { out.push(piece.slice(0, n)); piece = piece.slice(n); }
        else { out.push(cur); cur = ""; }
      }
    }
    if (cur || !out.length) out.push(cur);
    return out;
  }
  function stamp(d = new Date()) {
    const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const hr = d.getHours() % 12 || 12;
    return `${String(d.getDate()).padStart(2, "0")}-${M[d.getMonth()]}-${d.getFullYear()} ${String(hr).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "AM" : "PM"}`;
  }
  // the same plain ERP layout as the report documents
  function summaryText(t) {
    const W = 78, rule = "-".repeat(W), center = (x) => " ".repeat(Math.max(0, Math.floor((W - x.length) / 2))) + x;
    const both = (a, b) => a + " ".repeat(Math.max(2, W - a.length - b.length)) + b;
    const COLS = [["SNo", 4], ["Item", 25], ["Qty", 5, 1], ["MRP", 9, 1], ["Dis%", 5, 1], ["Dis", 8, 1], ["Amount", 10, 1]];
    const row = (cells) => COLS.map(([, n, r], i) => PAD(cells[i] ?? "", n, r)).join("  ").trimEnd();
    const cust = S.customer ? `${S.customer.name}${S.customer.mobile ? " · " + S.customer.mobile : ""}` : "Walk-in";
    const out = [center((B.pharmacy || "Faheem Pharmacy").toUpperCase()), "", center(S.editing ? `SALE SUMMARY — EDIT OF ${S.editing.invoice_no}` : "SALE SUMMARY"), "",
      both(`Bill: ${S.editing ? S.editing.invoice_no : S.parked ? S.parked.ref : "NEW-" + String(billNo).padStart(2, "0")}${S.manual ? " (manual)" : ""}`, `Date: ${stamp()}`),
      both(`Customer: ${cust}`, `Sale type: ${SALE_TYPES[S.saleType]}`),
      rule, row(COLS.map((c) => c[0])), rule];
    S.lines.forEach((l, i) => {
      const a = alloc(l), first = (a[0] || {}).b || pool(l)[0] || {};
      const mrp = l.manual ? num(l.rate) : first.unit_mrp ?? 0;
      const name = wrapText(l.name, 25);
      out.push(row([i + 1, name[0], l.qty, money(mrp), num(l.disc) ? num(l.disc) : "-", lineDisc(l) ? money(lineDisc(l)) : "-", money(lineNet(l))]));
      for (const more of name.slice(1)) out.push(row(["", more]));
      if (!l.manual && a.length > 1) out.push(row(["", `(${a.length} batches)`]));
    });
    out.push(rule, "TOTAL" + row(["", "", t.units, "", "", t.ldisc ? money(t.ldisc) : "-", money(t.sub)]).slice(5), rule, "");
    const kv = (k, v) => out.push(" ".repeat(W - 44) + PAD(k, 30) + PAD(v, 14, true));
    kv("MRP value", money(t.gross));
    if (t.ldisc) kv("Less: item discounts", "-" + money(t.ldisc));
    kv("Subtotal", money(t.sub));
    if (t.disc) kv(`Less: bill discount (${t.discPct}%)`, "-" + money(t.disc));
    if (t.round) kv("Round off", (t.round > 0 ? "+" : "") + money(t.round));
    out.push(" ".repeat(W - 44) + "-".repeat(44));
    kv("NET PAYABLE", money(t.net));
    out.push(" ".repeat(W - 44) + "-".repeat(44));
    if (S.mode === "SPLIT") {
      for (const m of ["CASH", "UPI", "CARD"]) if (num(S.split[m]) > 0) kv(`Paid by ${MODES[m]}`, money(S.split[m]));
      if (S.splitRecv && num(S.splitRecv) > num(S.split.CASH)) { kv("Cash received", money(S.splitRecv)); kv("Change to return", money(num(S.splitRecv) - num(S.split.CASH))); }
    } else {
      kv("Paid by", MODES[S.mode]);
      if (S.mode === "CASH") {
        const recv = S.received === "" ? t.net : r2(num(S.received));
        kv("Received", money(recv));
        kv("Change to return", money(Math.max(0, r2(recv - t.net))));
      } else if (S.ref) kv("Reference", S.ref.slice(0, 14));
    }
    if (t.gross - t.net > 0.009) out.push("", `Customer saves Rs. ${money(t.gross - t.net)} on MRP.`);
    return out.join("\n");
  }
  function showOverlay(html) {
    closeOverlay();
    overlay = h(`<div class="pos-overlay"><section class="pos-sheet" tabindex="-1" role="dialog" aria-modal="false">${html}</section></div>`);
    $(".pos", root).append(overlay);
    ctx.setKeys();
    const sheet = overlay.querySelector(".pos-sheet");
    setTimeout(() => sheet.focus(), 0);
    return sheet;
  }
  function closeOverlay() { if (overlay) { overlay.remove(); overlay = null; ctx.setKeys(); } }
  function review(t) {
    return new Promise((resolve) => {
      const sheet = showOverlay(`<header class="pos-sheet-h"><b>${S.editing ? "Update sale" : "Complete sale"}</b><span class="muted">check the bill with the customer</span></header>
        <pre class="doc-text pos-summary"></pre>
        <footer><button type="button" class="btn" data-a="back">Back to adjust sale <kbd>Esc</kbd></button>
        <button type="button" class="btn primary" data-a="ok">${S.editing ? "Update sale" : "Complete sale"} <kbd>Enter</kbd></button></footer>`);
      sheet.querySelector(".pos-summary").textContent = summaryText(t);
      const done = (ok) => { closeOverlay(); resolve(ok); };
      sheet.onclick = (e) => { const b = e.target.closest("[data-a]"); if (b) done(b.dataset.a === "ok"); };
      overlay.keyFn = (name) => {
        if (name === "Enter" || keys.matches("pos.save", name)) { done(true); return true; }
        if (name === "Escape") { done(false); return true; }
        return false;
      };
    });
  }
  // after the sale: Print invoice or WhatsApp invoice (or neither). WhatsApp is queued and
  // delivered in the background — the next bill starts at once, the sale never waits for it.
  function askInvoice(d, summary, wasEdit) {
    const change = num(d.change);
    const canWa = !!CAN["whatsapp.send"];
    const saved = normalizePhone(summary.mobile);
    const sheet = showOverlay(`<header class="pos-sheet-h"><b>✓ ${esc(d.invoice_no)} ${wasEdit ? "updated" : "completed"}</b><span class="muted">₹${money(d.total)} · ${esc(summary.payment)} · ${esc(summary.customer)}</span></header>
      ${change > 0 ? `<p class="pos-change">Return change <b>₹${money(change)}</b></p>` : ""}
      <p class="pos-ask">Invoice for the customer</p>
      <div class="pos-choices">
        <button type="button" class="choice" data-a="print"><span class="choice-ico">🖨</span><b>Print invoice</b><small>opens the invoice studio here</small><kbd>P</kbd></button>
        ${canWa ? `<button type="button" class="choice wa" data-a="wa">${WA_ICON}<b>WhatsApp invoice</b><small class="wa-to">${saved ? "to " + esc(prettyPhone(saved)) : "type the customer's number"}</small><kbd>W</kbd></button>` : ""}
      </div>
      ${canWa ? '<p class="wa-note muted">Checking WhatsApp…</p>' : ""}
      <footer><button type="button" class="btn" data-a="no">No invoice <kbd>N</kbd></button></footer>`);
    let waOk = false;
    if (canWa) waStatus(true).then((st) => {
      if (!overlay || !sheet.isConnected) return;
      waOk = st.connected;
      const note = sheet.querySelector(".wa-note"), btn = sheet.querySelector(".choice.wa");
      note.textContent = st.connected ? "" : st.message;
      note.className = "wa-note " + (st.connected ? "muted" : "warn");
      btn.classList.toggle("off", !st.connected);
      btn.title = st.connected ? "" : st.message;
    });
    const finish = (msg) => { closeOverlay(); if (msg) ctx.status(msg); q.focus(); };
    const whatsapp = async () => {
      if (!waOk) { ctx.status((await waStatus(true)).message, "warn"); return; }
      let phone = saved;
      if (!phone) {
        phone = await askPhone({ anchor: sheet.querySelector(".choice.wa"), initial: summary.mobile || "", invoiceNo: d.invoice_no });
        if (!phone) { sheet.focus(); return; }
      }
      closeOverlay(); q.focus();
      try { await sendInvoice(ctx, { id: d.sale_id, invoice_no: d.invoice_no }, phone); }
      catch (err) { ctx.status(`WhatsApp: ${err.message}`, "error"); }
    };
    const choose = (a) => (a === "print" ? (closeOverlay(), openStudio(summary)) : a === "wa" ? whatsapp()
      : finish(`${d.invoice_no} saved without an invoice — Sales history (or "invoice / reprint" below) opens it any time`));
    sheet.onclick = (e) => { const b = e.target.closest("[data-a]"); if (b) choose(b.dataset.a); };
    overlay.keyFn = (name) => {
      if (name === "P" || name === "Enter") { choose("print"); return true; }
      if (name === "W" && canWa) { choose("wa"); return true; }
      if (name === "N" || name === "Escape") { choose("no"); return true; }
      return false;
    };
  }

  async function finalize() {
    if (busy) return;
    if (editing !== null) { const inp = $(".qty-in", tbody); if (inp) inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); if (editing !== null) return; }
    if (discEditing !== null) { const inp = $(".disc-in", tbody); if (inp) inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); if (discEditing !== null) return; }
    if (!S.lines.length) { ctx.status("The bill is empty", "warn"); q.focus(); return; }
    const t = totals();
    const err = payProblem(t);
    if (err) { ctx.status(err, "error"); return; }
    const body = {
      invoice_type: S.manual ? "MANUAL" : "INVENTORY",
      lines: S.lines.map((l) => (l.manual
        ? { name: l.name, item_id: l.item_id || null, pack: l.pack || "", quantity: l.qty, rate: num(l.rate), discount_pct: CAN["sales.discount"] ? num(l.disc) : 0 }
        : { item_id: l.item_id, batch_id: l.batch_id || null, quantity: l.qty, discount_pct: CAN["sales.discount"] ? num(l.disc) : 0 })),
      customer_id: S.customer ? S.customer.id : null,
      customer_type: S.saleType,
      discount: 0,
      discount_pct: CAN["sales.discount"] ? t.discPct : 0,
      client_request_id: S.requestId,
      parked_id: S.parked ? S.parked.id : null,
      notes: S.ref ? `Ref: ${S.ref}` : "",
    };
    if (S.mode === "SPLIT") {
      body.payment_mode = "SPLIT";
      body.payments = ["CASH", "UPI", "CARD"].filter((m) => num(S.split[m]) > 0).map((m) => ({ mode: m, amount: r2(num(S.split[m])), reference: m !== "CASH" ? S.ref || "" : "" }));
      if (S.splitRecv) body.cash_received = r2(num(S.splitRecv));
    } else {
      body.payment_mode = S.mode;
      body.payments = [{ mode: S.mode, amount: t.net, reference: S.mode !== "CASH" ? S.ref || "" : "" }];
      if (S.mode === "CASH") body.cash_received = S.received === "" ? t.net : r2(num(S.received));
    }
    if (!(await review(t))) { ctx.status("Back to the bill — adjust it and press Complete sale again"); q.focus(); return; }
    busy = true; renderTotals(); renderSaleType();
    ctx.status("Completing sale…");
    const billed = { customer: S.customer ? S.customer.name : "Walk-in", mobile: S.customer ? S.customer.mobile || "" : "",
      customer_type: S.saleType, type: S.manual ? "MANUAL" : "INVENTORY", items: S.lines.length,
      units: S.lines.reduce((n, l) => n + l.qty, 0), payment: S.mode === "SPLIT" ? "Split" : MODES[S.mode] };
    try {
      const d = S.editing
        ? await api(`/api/sales/${S.editing.id}`, { method: "PUT", body: { ...body, reason: "Edited from Sales" } })
        : await api("/api/sales", { method: "POST", body });
      const wasEdit = S.editing;
      try { const m = JSON.parse(sessionStorage.getItem("erp:saleTabs") || "{}"); m[d.sale_id] = ctx.tabId; sessionStorage.setItem("erp:saleTabs", JSON.stringify(m)); } catch { /* private mode */ }
      lastBill = { no: d.invoice_no, total: d.total, change: d.change, mode: S.mode, id: d.sale_id, customer_id: S.customer ? S.customer.id : null };
      const box = $(".last-bill", root);
      box.hidden = false;
      box.innerHTML = `Last bill <b>${esc(d.invoice_no)}</b> · ₹${money(d.total)} ${esc(MODES[S.mode])}${d.change && num(d.change) > 0 ? ` · <b class="change">Return ₹${money(d.change)}</b>` : ""} · <a href="#" class="reprint">invoice / reprint</a>`;
      const summary = { ...billed, customer_id: S.customer ? S.customer.id : null, id: d.sale_id, invoice_no: d.invoice_no, total: d.total, tendered: d.tendered, change: d.change,
        date: new Date().toISOString(), status: "PAID" };
      box.querySelector(".reprint").onclick = (e) => { e.preventDefault(); openStudio(summary); };
      ctx.status(`✓ Bill ${d.invoice_no} ${wasEdit ? "updated (stock re-posted to its batches)" : "completed"} · ₹${money(d.total)}${d.change && num(d.change) > 0 ? ` · return ₹${money(d.change)}` : ""}`, "ok");
      cache.clear();
      const wasManual = S.manual && !wasEdit;
      S = blank(); S.manual = wasManual; sel = -1; editing = null; discEditing = null; rateEditing = null;
      render();
      askInvoice(d, summary, wasEdit);
    } catch (ex) {
      ctx.status(ex instanceof ApiError ? ex.message : "Could not save the bill: " + ex.message, "error");
    } finally { busy = false; renderTotals(); renderSaleType(); }
  }

  // ---------------------------------------------------------------- bill grid keyboard
  wrap.addEventListener("keydown", (e) => {
    if (e.target !== wrap) return;
    if (e.key === "ArrowDown") { e.preventDefault(); select(sel + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (sel <= 0) q.focus(); else select(sel - 1); }
    else if (e.key === "Home") { e.preventDefault(); select(0); }
    else if (e.key === "End") { e.preventDefault(); select(S.lines.length - 1); }
    else if (e.key === "Enter") { e.preventDefault(); if (S.lines[sel] && S.lines[sel].manual) editRate(); else editQty(); }
    else if (e.key === "Escape") { e.preventDefault(); q.focus(); }
    else if (e.key === "Delete") {
      e.preventDefault();
      const l = S.lines[sel];
      if (l) window.erpConfirm(`Remove ${l.name} from the bill?`).then((ok) => { if (ok) removeLine(); wrap.focus(); });
    } else if (/^[0-9]$/.test(e.key) && !e.ctrlKey && !e.altKey) { e.preventDefault(); editQty(sel, e.key); }
    else if (e.key === "+" || e.key === "=") { e.preventDefault(); bump(1); }
    else if (e.key === "-") { e.preventDefault(); bump(-1); }
  });
  function bump(d) {
    const l = S.lines[sel];
    if (!l) return;
    const v = l.qty + d * step(l);
    if (v < 1) return;
    const err = problem(l, v);
    if (err) { ctx.status(err, "warn"); return; }
    l.qty = v; render(); wrap.focus();
  }
  tbody.addEventListener("mousedown", (e) => {
    const tr = e.target.closest("tr[data-i]");
    if (!tr || e.target.classList.contains("qty-in") || e.target.classList.contains("disc-in")) return;
    select(Number(tr.dataset.i));
    if (e.target.closest("td.qty")) { e.preventDefault(); editQty(); }
    else if (e.target.closest("td.disc")) { e.preventDefault(); editDisc(); }
    else wrap.focus();
  });
  tbody.addEventListener("contextmenu", (e) => {
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    e.preventDefault();
    select(Number(tr.dataset.i));
    document.querySelectorAll(".ctx-menu").forEach((m) => m.remove());
    const menu = h(`<div class="ctx-menu"><button data-a="qty">Edit quantity<kbd data-shortcut="pos.qty">${esc(keys.keyFor("pos.qty"))}</kbd></button><button data-a="disc">Item discount %<kbd data-shortcut="pos.itemDisc">${esc(keys.keyFor("pos.itemDisc"))}</kbd></button><button data-a="batch">Change batch<kbd data-shortcut="pos.batch">${esc(keys.keyFor("pos.batch"))}</kbd></button><button data-a="rm">Remove item<kbd data-shortcut="pos.remove">${esc(keys.keyFor("pos.remove"))}</kbd></button></div>`);
    menu.style.left = e.clientX + "px"; menu.style.top = e.clientY + "px";
    menu.onclick = (ev) => { const a = ev.target.closest("button"); menu.remove(); if (!a) return; ({ qty: () => editQty(), disc: () => editDisc(), batch: openBatch, rm: () => removeLine() })[a.dataset.a](); };
    document.body.append(menu);
    setTimeout(() => document.addEventListener("mousedown", function off(ev) { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener("mousedown", off); } }), 0);
  });

  // ---------------------------------------------------------------- screen API
  const KEYS = {
    "pos.saleType": () => changeSaleType(S.saleType === "WALK_IN" ? "HOME_DELIVERY" : "WALK_IN"),
    "pos.search": () => { q.focus(); q.select(); },
    "pos.qty": () => editQty(),
    "pos.followup": () => followUp(),
    "pos.manual": toggleManual,
    "pos.batch": openBatch,
    "pos.remove": () => removeLine(),
    "pos.itemDisc": () => editDisc(),
    "pos.billDisc": () => { if (CAN["sales.discount"]) { discIn.focus(); discIn.select(); } else ctx.status("You do not have discount permission", "warn"); },
    "pos.upi": () => setMode("UPI"),
    "pos.cash": () => setMode("CASH"),
    "pos.card": () => setMode("CARD"),
    "pos.split": () => setMode("SPLIT"),
    "pos.save": finalize,
    "pos.hold": hold,
    "pos.resume": showParked,
    "pos.customer": () => chooseCustomer(),
    "pos.walkin": () => { if (S.customer) { S.customer = null; render(); ctx.status("Walk-in customer"); } },
  };
  async function loadForEdit(id) {
    try {
      const d = await api(`/api/erp/sales/${id}/edit`);
      S = blank();
      S.editing = { id: d.sale_id, invoice_no: d.invoice_no };
      S.manual = d.invoice_type === "MANUAL";
      S.lines = d.lines.map((l) => (l.manual
        ? { manual: true, name: l.name, code: l.code || "", qty: l.qty, rate: l.rate, disc: Math.min(num(l.disc), MAXD), item_id: l.item_id || null, batch_id: null, batches: [], base_unit: "UNIT", pack_unit: "UNIT", upp: 1 }
        : { ...lineFrom({ id: l.item_id, code: l.code, name: l.name, pack_raw: l.pack_raw, upp: l.upp, loose: l.loose, base_unit: l.base_unit,
            pack_unit: l.pack_unit, form: l.form, rack: l.rack, content: l.content, batches: l.batches }),
          batch_id: l.batch_id, qty: l.qty, disc: Math.min(num(l.disc), MAXD) }));
      S.customer = d.customer ? { id: d.customer.id, name: d.customer.name, mobile: d.customer.mobile, customer_id: d.customer.customer_id } : null;
      S.saleType = saleType(d.customer_type); S.saleTypeExplicit = true;
      S.discountPct = d.discount_pct || null;
      if (d.payment_mode === "SPLIT") { S.mode = "SPLIT"; for (const p of d.payments) S.split[p.mode] = String(p.amount); }
      else { S.mode = ["CASH", "UPI", "CARD"].includes(d.payment_mode) ? d.payment_mode : "CASH"; if (S.mode === "CASH" && d.tendered) S.received = String(d.tendered); }
      sel = S.lines.length - 1;
      render(); q.focus();
      ctx.status(`Editing ${d.invoice_no}: change lines, quantity, discount or payment, then F12 saves it under the same number`, "ok");
    } catch (err) { ctx.status(err.message, "error"); }
  }
  render();
  if (!(saved && saved.lines) && params && params.edit) loadForEdit(params.edit);
  if (S.exchangeNote) setTimeout(() => ctx.status(S.exchangeNote, "ok"), 0);
  return {
    navigate(target) {
      if (!target || !target.edit) return true;
      if (S.lines.length && !(S.editing && S.editing.id === target.edit)) return false;   // busy with another bill
      if (studioOpen) closeStudio();
      if (!(S.editing && S.editing.id === target.edit)) loadForEdit(target.edit);
      return true;
    },
    manual: () => { if (!S.manual) toggleManual(); },
    get keys() {
      if (overlay) return overlay.querySelector("[data-a=print]") ? [["P", "Print invoice"], ...(CAN["whatsapp.send"] ? [["W", "WhatsApp invoice"]] : []), ["N", "No invoice"]] : [["Enter", "Complete sale"], ["Esc", "Back to adjust sale"]];
      return studioOpen ? [["1–6", "Format"], ["E", "Expiry"], ["B", "B&W"], ["Ctrl+P", "Print"], ["Esc", "New bill"]] : keys.bar("pos");
    },
    onKey(e, name) {
      if (overlay) return overlay.keyFn(name) || !/^(Alt|Ctrl)\+/.test(name);   // the summary owns the keys; tab keys still work
      if (studioOpen) {
        if (keys.matches("pos.followup", name)) { followUp(); return true; }
        return studio.onKey(e, name) || name === "Ctrl+P";
      }
      const fn = KEYS[keys.lookup("pos", name)];
      if (!fn) return false;
      fn();
      return true;
    },
    onShow({ focus }) { if (focus) setTimeout(() => (overlay ? overlay.querySelector(".pos-sheet") : q).focus(), 0); },
    prepareShortcut() {
      const inp = $(".qty-in, .disc-in", tbody);
      if (inp) inp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
      return editing === null && discEditing === null;
    },
    beforeClose,
    destroy() { openSlots.delete(billNo); },
  };
}
