import * as keys from "erp/keys";
// Stock History — every ledger movement across all products (or one product
// when opened from Inventory with F7). Read-only: the ledger is immutable and
// corrections show up as reversal lines. Enter opens the source document
// (purchase) or the product's stock ledger.
import { $, BOOT, api, debounce, esc, fmtDateTime } from "erp/core";
import { Grid } from "erp/grid";

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

export function create(ctx, params, root) {
  const CAN = BOOT.can || {};
  const today = new Date();
  const weekAgo = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6);
  const F = { start: params.item ? iso(new Date(today.getFullYear() - 1, today.getMonth(), today.getDate())) : iso(weekAgo),
    end: iso(today), type: "", direction: "", q: "", user: "", item: params.item || "", batch: params.batch || "" };
  let rows = [], total = 0, loading = false, ctrl = null;

  root.innerHTML = `<div class="history">
    <div class="filters">
      <label>From<input type="date" class="f-start" value="${F.start}"></label>
      <label>To<input type="date" class="f-end" value="${F.end}"></label>
      <label>Search<input class="f-q" autocomplete="off" spellcheck="false" placeholder="product, code, batch, reference, reason"></label>
      <label>Movement<select class="f-type"><option value="">All types</option></select></label>
      <label>Direction<select class="f-dir"><option value="">In and out</option><option value="in">Stock in</option><option value="out">Stock out</option></select></label>
      <label>User<select class="f-user"><option value="">Everyone</option></select></label>
      ${F.item ? '<button type="button" class="btn f-all" title="Show every product">All products</button>' : ""}
      <span class="spacer"></span>
      ${CAN["inventory.export"] ? `<button type="button" class="btn f-export">Export <kbd>${esc(keys.keyFor("history.export"))}</kbd></button>` : ""}
    </div>
    <div class="h-sum"></div>
  </div>`;
  const sum = $(".h-sum", root);
  const grid = new Grid({
    label: "Stock history", storageKey: "stock-history", empty: "No stock movements in this period.",
    columns: [
      { key: "at", label: "Date / time", width: 136, render: (r) => esc(fmtDateTime(r.at)) },
      { key: "item", label: "Product", width: 250, render: (r) => `<b>${esc(r.item)}</b> <span class="muted mono">${esc(r.code)}</span>` },
      { key: "batch_no", label: "Batch", width: 96, render: (r) => `<span class="mono">${esc(r.batch_no || "—")}</span>` },
      { key: "label", label: "Movement", width: 130, render: (r) => `${esc(r.label)}${r.reversal_of ? ' <span class="tag">rev</span>' : ""}` },
      { key: "in", label: "In", width: 62, align: "num", render: (r) => (r.in ? `<b class="in">${r.in}</b>` : "") },
      { key: "out", label: "Out", width: 62, align: "num", render: (r) => (r.out ? `<b class="out">${r.out}</b>` : "") },
      { key: "unit", label: "Unit", width: 70, render: (r) => esc(r.unit) + (r.txn ? ` <span class="muted">(${esc(r.txn)})</span>` : "") },
      { key: "balance", label: "Batch bal.", width: 74, align: "num", title: "Batch balance after the movement" },
      { key: "reference", label: "Reference", width: 130, render: (r) => `<span class="mono">${esc(r.reference || "—")}</span>` },
      { key: "reason", label: "Reason", width: 280, render: (r) => esc(r.reason) },
      { key: "user", label: "User", width: 96 },
    ],
    onActivate: (r) => drill(r),
    onNearEnd: () => more(),
    contextMenu: (r) => [
      ...(r.reference_type === "PURCHASE" && r.reference_id && CAN["purchase.view"] ? [{ label: `Open purchase ${r.reference}`, key: "Enter", action: () => drill(r) }] : []),
      { label: "Product stock ledger", key: r.reference_type === "PURCHASE" ? "" : "Enter", action: () => ctx.open("ledger", { item: r.item_id }) },
      { label: "Only this product", action: () => { F.item = r.item_id; F.batch = ""; reload(); } },
      { label: "Only this batch", action: () => { F.item = r.item_id; F.batch = r.batch_id; reload(); } },
    ],
  });
  root.querySelector(".history").append(grid.el);

  function drill(r) {
    if (!r) return;
    if (r.reference_type === "PURCHASE" && r.reference_id && CAN["purchase.view"]) ctx.open("purchase", { id: r.reference_id });
    else ctx.open("ledger", { item: r.item_id, ...(r.batch_id ? { batch: r.batch_id } : {}) });
  }
  const qs = (extra = {}) => new URLSearchParams(Object.entries({ ...F, ...extra }).filter(([, v]) => v !== "" && v != null)).toString();

  async function reload() {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    try {
      const d = await api(`/api/erp/stock-history?${qs({ limit: 300 })}`, { signal: ctrl.signal });
      rows = d.movements; total = d.total;
      grid.setRows(rows);
      if (d.types) {
        const t = $(".f-type", root);
        t.innerHTML = '<option value="">All types</option>' + d.types.map((x) => `<option value="${x.code}">${esc(x.label)}</option>`).join("");
        t.value = F.type;
        const u = $(".f-user", root);
        u.innerHTML = '<option value="">Everyone</option>' + d.users.map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
        u.value = F.user;
      }
      const scope = F.item && rows[0] ? ` · ${rows[0].item}${F.batch && rows[0] ? " batch " + rows[0].batch_no : ""}` : "";
      ctx.setTitle(F.item && rows[0] ? `History — ${rows[0].item}` : "Stock History");
      sum.innerHTML = `<span class="chip">${d.total} movements${esc(scope)}</span><span class="chip">${d.products} products</span>
        <span class="chip ok">In ${d.in}</span><span class="chip bad">Out ${d.out}</span>
        <span class="hint">Quantities are in each product's sale unit (tablets, bottles…). Enter opens the purchase or the product ledger.</span>`;
    } catch (err) { if (err.name !== "AbortError") ctx.status(err.message, "error"); }
  }
  async function more() {
    if (loading || rows.length >= total) return;
    loading = true;
    try {
      const d = await api(`/api/erp/stock-history?${qs({ limit: 300, offset: rows.length })}`);
      rows = rows.concat(d.movements);
      grid.appendRows(d.movements);
    } catch (err) { ctx.status(err.message, "error"); }
    loading = false;
  }
  const q = $(".f-q", root);
  const later = debounce(() => { F.q = q.value.trim(); reload(); }, 200);
  q.addEventListener("input", later);
  q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); later.flush(); grid.focus(); } });
  for (const [cls, key] of [["f-start", "start"], ["f-end", "end"], ["f-type", "type"], ["f-dir", "direction"], ["f-user", "user"]]) {
    $("." + cls, root).addEventListener("change", (e) => { F[key] = e.target.value; reload(); });
  }
  const all = $(".f-all", root);
  if (all) all.onclick = () => { F.item = ""; F.batch = ""; all.remove(); reload(); };
  const exportCsv = () => {
    if (!CAN["inventory.export"]) { ctx.status("Exporting needs inventory export rights", "warn"); return; }
    window.open(`/api/erp/stock-history.csv?${qs()}`, "_blank");
    ctx.status(`Exporting ${total} movements`, "ok");
  };
  const ex = $(".f-export", root);
  if (ex) ex.onclick = exportCsv;
  reload();

  return {
    get keys() { return keys.bar("history"); },
    onKey(e, name) {
      if (keys.matches("history.search", name)) { q.focus(); q.select(); return true; }
      if (keys.matches("history.period", name)) { $(".f-start", root).focus(); return true; }
      if (keys.matches("history.type", name)) { $(".f-type", root).focus(); return true; }
      if (keys.matches("history.refresh", name)) { reload(); return true; }
      if (keys.matches("history.export", name)) { exportCsv(); return true; }
      return false;
    },
    onShow({ focus }) { if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
