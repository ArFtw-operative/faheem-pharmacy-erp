import * as keys from "erp/keys";
// Stock adjustments — every manual stock correction as a numbered, immutable
// ADJ document over the ledger.
//
//   Decrease: Loose / missing · Damage · Expired · Count shortfall · Other
//   Increase: Count surplus · Other (into an existing batch or a new one)
//
// F3 new adjustment · Alt+X reverse (a new linked document; nothing is edited
// or deleted) · Enter opens the product's stock ledger.
import { $, BOOT, api, debounce, esc, fmtDateTime, fmtExpShort, modal, money } from "erp/core";
import { Grid } from "erp/grid";
import { pickProduct } from "erp/purchase";

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const t = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase() : "");

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const today = new Date();
  const F = { start: iso(new Date(today.getFullYear(), today.getMonth() - 1, today.getDate())), end: iso(today), category: "", direction: "", q: "" };
  let rows = [], types = [], total = 0, ctrl = null;

  root.innerHTML = `<div class="history">
    <div class="filters">
      <label>From<input type="date" class="f-start" value="${F.start}"></label>
      <label>To<input type="date" class="f-end" value="${F.end}"></label>
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="product, batch, ADJ no. or reason"></label>
      <label>Type<select class="f-cat"><option value="">All types</option></select></label>
      <label>Direction<select class="f-dir"><option value="">Both</option><option value="OUT">Decrease</option><option value="IN">Increase</option></select></label>
      <span class="spacer"></span>
      ${CAN["adjustment.create"] ? `<button type="button" class="btn primary f-new">New adjustment <kbd>${esc(keys.keyFor("adj.new"))}</kbd></button>` : ""}
    </div>
    <div class="h-sum"></div>
  </div>`;
  const grid = new Grid({
    label: "Stock adjustments", storageKey: "adjustments", empty: "No stock adjustments in this period. F3 records one.",
    columns: [
      { key: "reference_no", label: "ADJ no.", width: 100, render: (r) => `<span class="mono">${esc(r.reference_no)}</span>` },
      { key: "date", label: "Date / time", width: 130, render: (r) => esc(fmtDateTime(r.date)) },
      { key: "item", label: "Product", width: 230, render: (r) => `<b>${esc(r.item)}</b> <span class="muted mono">${esc(r.code)}</span>` },
      { key: "batch", label: "Batch", width: 96, render: (r) => `<span class="mono">${esc(r.batch || "—")}</span>` },
      { key: "label", label: "Type", width: 120 },
      { key: "quantity", label: "Qty", width: 90, align: "num",
        render: (r) => `<b class="${r.direction === "IN" ? "in" : "out"}">${r.direction === "IN" ? "+" : "−"}${r.quantity}</b> <small>${esc(r.unit)}</small>` },
      { key: "value", label: "Cost value", width: 86, align: "num", render: (r) => money(r.value) },
      { key: "reason", label: "Reason", width: 260, render: (r) => esc(r.reason) },
      { key: "status", label: "Status", width: 110, render: (r) => r.reversal_of ? `<span class="muted">reverses ${esc(r.reversal_of)}</span>`
        : r.reversed ? '<span class="warn">Reversed</span>' : '<span class="ok">Posted</span>' },
      { key: "user", label: "User", width: 90 },
    ],
    rowClass: (r) => (r.reversed ? "dim" : ""),
    onActivate: (r) => ctx.open("ledger", { item: r.item_id, ...(r.batch_id ? { batch: r.batch_id } : {}) }),
    contextMenu: () => [
      { label: "Product stock ledger", key: "Enter", action: () => { const r = grid.selected; if (r) ctx.open("ledger", { item: r.item_id }); } },
      ...(CAN["adjustment.create"] ? [{ label: "Reverse this adjustment", key: keys.keyFor("adj.reverse"), action: () => reverse() }] : []),
    ],
  });
  root.querySelector(".history").append(grid.el);

  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    const qs = new URLSearchParams(Object.entries(F).filter(([, v]) => v !== "")).toString();
    try {
      const d = await api(`/api/erp/adjustments?${qs}`, { signal: ctrl.signal });
      rows = d.adjustments; total = d.total; types = d.types;
      grid.setRows(rows, { keep: true });
      const cat = $(".f-cat", root);
      if (cat.options.length <= 1) cat.innerHTML = '<option value="">All types</option>' + types.map((x) => `<option value="${x.code}">${esc(x.label)}</option>`).join("");
      $(".h-sum", root).innerHTML = `<span class="chip">${d.total} documents</span><span class="chip bad">Out ${d.out}</span><span class="chip ok">In ${d.in}</span>
        <span class="chip">Loss at cost ₹${money(d.loss_value)}</span><span class="hint">Reversed documents are excluded from the totals · quantities in sale units</span>`;
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }

  async function newAdjustment() {
    if (!CAN["adjustment.create"]) { ctx.status("Stock adjustments need the adjustment right", "warn"); return; }
    const item = await pickProduct({ title: "Stock adjustment — choose product", searchUrl: "/api/erp/products" });
    if (!item) return;
    let d;
    try { d = await api(`/api/erp/inventory/${item.id}`); } catch (err) { ctx.status(err.message, "error"); return; }
    const batches = d.batches;
    const out = types.filter((x) => x.out), inc = types.filter((x) => x.in);
    const res = await modal({
      title: `Adjust ${d.name}`, wide: true, submitLabel: "Post adjustment",
      body: `<div class="form-grid">
        <label>Direction<select name="direction" autofocus><option value="OUT">Decrease (loss)</option><option value="IN">Increase (surplus)</option></select></label>
        <label>Type<select name="category"></select></label>
        <label class="full">Batch<select name="batch"></select></label>
        <fieldset class="full form-grid three nb" hidden><legend>New batch</legend>
          <label>Batch no.<input name="batch_no" maxlength="60"></label><label>Expiry (MM/YYYY)<input name="expiry"></label>
          <label>Pack MRP ₹<input name="mrp" inputmode="decimal"></label><label>Cost / pack ₹<input name="cost" inputmode="decimal"></label></fieldset>
        <label>Quantity (${esc(t(d.base_unit || "unit"))}s · 1s = one ${esc(t(d.pack_unit || "pack").toLowerCase())})<input name="quantity" autocomplete="off" required></label>
        <label>Reason<input name="reason" maxlength="300" required placeholder="what happened"></label>
        <p class="full hint">Current stock <b>${d.stock}</b> ${esc(t(d.base_unit || "unit").toLowerCase())}s. The adjustment is a permanent document; a mistake is corrected by reversing it (Alt+X).</p></div>`,
      onOpen: (form) => {
        const fill = () => {
          const dir = form.direction.value;
          form.category.innerHTML = (dir === "OUT" ? out : inc).map((x) => `<option value="${x.code}">${esc(x.label)}</option>`).join("");
          const opts = batches.filter((b) => dir === "IN" || b.stock > 0)
            .map((b) => `<option value="${b.id}">${esc(b.batch_no || "no batch")} · exp ${fmtExpShort(b.expiry)} · ${b.stock} in stock · MRP ${money(b.pack_mrp)}</option>`);
          if (dir === "IN") opts.push('<option value="new">New batch…</option>');
          form.batch.innerHTML = opts.join("") || '<option value="">No batch with stock</option>';
          toggle();
        };
        const toggle = () => { form.querySelector(".nb").hidden = form.batch.value !== "new"; };
        form.direction.addEventListener("change", fill);
        form.batch.addEventListener("change", toggle);
        fill();
      },
      onSubmit: (form) => {
        const body = { item_id: d.id, direction: form.direction.value, category: form.category.value, quantity: form.quantity.value, reason: form.reason.value };
        if (form.batch.value === "new") Object.assign(body, { batch_no: form.batch_no.value, expiry: form.expiry.value, mrp: form.mrp.value, cost: form.cost.value });
        else if (form.batch.value) body.batch_id = Number(form.batch.value);
        return api("/api/erp/adjustments", { method: "POST", body });
      },
    });
    if (res) {
      const a = res.adjustment;
      ctx.status(`${a.reference_no}: ${a.label} ${a.direction === "IN" ? "+" : "−"}${a.quantity} ${a.item} · stock now ${res.stock}`, "ok");
      await reload();
      grid.select(rows.findIndex((r) => r.id === a.id));
    }
    grid.focus();
  }

  async function reverse() {
    const r = grid.selected;
    if (!r) return;
    if (!CAN["adjustment.create"]) { ctx.status("Stock adjustments need the adjustment right", "warn"); return; }
    if (r.reversal_of) { ctx.status(`${r.reference_no} is itself a reversal — post a new adjustment instead`, "warn"); return; }
    if (r.reversed) { ctx.status(`${r.reference_no} was already reversed`, "warn"); return; }
    const res = await modal({
      title: `Reverse ${r.reference_no}`, submitLabel: "Post reversal",
      body: `<div class="form-grid"><p class="full">${esc(r.label)} ${r.direction === "IN" ? "+" : "−"}${r.quantity} ${esc(r.item)} (${esc(r.batch || "—")}) will be undone by a new document; ${esc(r.reference_no)} stays on record, marked reversed.</p>
        <label class="full">Reason<input name="reason" required autofocus maxlength="300" placeholder="why it was wrong"></label></div>`,
      onSubmit: (form) => api(`/api/erp/adjustments/${r.id}/reverse`, { method: "POST", body: { reason: form.reason.value } }),
    });
    if (res) { ctx.status(`${res.adjustment.reference_no} reverses ${r.reference_no}`, "ok"); await reload(); }
    grid.focus();
  }

  const q = $(".f-q", root);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 200);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  for (const [cls, key] of [["f-start", "start"], ["f-end", "end"], ["f-cat", "category"], ["f-dir", "direction"]]) {
    $("." + cls, root).addEventListener("change", (e) => { F[key] = e.target.value; reload(); });
  }
  const nb = $(".f-new", root);
  if (nb) nb.onclick = newAdjustment;
  reload();

  return {
    create: newAdjustment,
    get keys() { return keys.bar("adjustments"); },
    onKey(e, name) {
      const act = { "adj.search": () => { q.focus(); q.select(); }, "adj.new": newAdjustment, "adj.period": () => $(".f-start", root).focus(),
        "adj.refresh": reload, "adj.reverse": reverse };
      for (const [id, fn] of Object.entries(act)) if (keys.matches(id, name)) { fn(); return true; }
      return false;
    },
    onDataChanged(areas) { if (areas.includes("inventory")) reload(); },
    onShow({ focus }) { reload(); if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
