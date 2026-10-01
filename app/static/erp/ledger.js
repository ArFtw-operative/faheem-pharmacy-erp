import * as keys from "erp/keys";
// Stock ledger for one product (or one batch): every movement, newest first.
// Entries are immutable; corrections appear as new lines marked "rev".
import { api, esc, fmtDateTime } from "erp/core";
import { Grid } from "erp/grid";

export function create(ctx, params, root) {
  root.innerHTML = `<div class="ledger"><div class="filters"><b class="l-title">Stock ledger</b><span class="spacer"></span><span class="muted l-meta"></span></div></div>`;
  const grid = new Grid({
    label: "Stock ledger", storageKey: "ledger", empty: "No stock movements yet.",
    columns: [
      { key: "at", label: "Date/Time", width: 140, render: (r) => esc(fmtDateTime(r.at)) },
      { key: "label", label: "Type", width: 130, render: (r) => `${esc(r.label)}${r.reversal_of ? ' <span class="tag">rev</span>' : ""}` },
      { key: "reference", label: "Reference", width: 150, render: (r) => `<span class="mono">${esc(r.reference || "—")}</span>` },
      { key: "in", label: "In", width: 64, align: "num", render: (r) => (r.in ? `<b class="in">${r.in}</b>` : "") },
      { key: "out", label: "Out", width: 64, align: "num", render: (r) => (r.out ? `<b class="out">${r.out}</b>` : "") },
      { key: "balance", label: "Balance", width: 72, align: "num", title: "Batch balance after the movement" },
      { key: "batch_no", label: "Batch", width: 100, render: (r) => `<span class="mono">${esc(r.batch_no || "—")}</span>` },
      { key: "reason", label: "Reason", width: 280, render: (r) => `${r.txn ? `<span class="muted">${esc(r.txn)} · </span>` : ""}${esc(r.reason)}` },
      { key: "user", label: "User", width: 100 },
    ],
  });
  root.querySelector(".ledger").append(grid.el);

  async function load() {
    try {
      const [item, data] = await Promise.all([
        api("/api/erp/inventory/" + params.item),
        api(`/api/items/${params.item}/ledger?limit=1000${params.batch ? "&batch_id=" + params.batch : ""}`),
      ]);
      const batch = params.batch ? item.batches.find((b) => b.id === params.batch) : null;
      const name = item.name + (batch ? " · " + (batch.batch_no || "no batch") : "");
      ctx.setTitle("Ledger — " + name);
      root.querySelector(".l-title").textContent = "Stock ledger — " + name;
      root.querySelector(".l-meta").textContent = `${data.movements.length} movements · quantities in ${String(item.base_unit).toLowerCase()}s · now ${batch ? batch.stock : item.stock}`;
      grid.setRows(data.movements, { keep: true });
    } catch (err) { ctx.status(err.message, "error"); }
  }
  load();
  return {
    get keys() { return keys.bar("ledger"); },
    onKey(e, name) {
      if (keys.matches("ledger.refresh", name)) { load(); return true; }
      if (name === "Escape") { ctx.close(); return true; }
      return false;
    },
    onShow({ focus }) { if (focus) setTimeout(() => grid.focus(), 0); },
  };
}
