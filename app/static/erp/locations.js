// Rack / box locations in the browser: the location picker, the bulk move and how a
// location is written. Every change goes to the server in one request (never one per product);
// the server validates the rack, the box and each product and reports what it did.
import { api, esc, h, modal } from "erp/core";

/** "R-A03 · Fever & Pain · B02" — the code first and always whole; the name may be cut. */
export function locationHtml(l, { names = true } = {}) {
  if (!l || !(l.rack || l.rack_code)) return '<span class="muted">Unassigned</span>';
  const rack = l.rack || l.rack_code, name = l.rack_name || "", box = l.box || l.box_code || "";
  return `<span class="loc"><b class="loc-code">${esc(rack)}</b>${names && name && name !== rack ? `<span class="loc-name">${esc(name)}</span>` : ""}${box ? `<b class="loc-box">${esc(box)}</b>` : ""}</span>`;
}
export const locationShort = (l) => (l && l.rack ? l.rack + (l.box ? " / " + l.box : "") : "");

/**
 * Choose a rack (and box). ``verb(target)`` builds the submit label, e.g. "Move 18 products".
 * ``apply({rack_id, box_id})`` runs on submit; an error keeps the dialog open.
 * Resolves with ``{rack_id, box_id, label, result}`` or null.
 */
export async function pickLocation({ title = "Move to rack", intro = "", verb = () => "Move", current = null, apply, allowClear = false, clearLabel = "", exclude = null }) {
  const { racks: all, config } = await api("/api/erp/locations/options");
  const racks = all.filter((r) => r.id !== exclude);
  if (!racks.length) throw new Error("No active racks yet — create one in Racks (F3)");
  let shown = racks, at = Math.max(0, racks.findIndex((r) => current && r.id === current.rack_id)), chosen = null;
  const body = h(`<div class="locpick">
    ${intro ? `<p>${intro}</p>` : ""}
    <label class="full">Rack<input class="lp-q" placeholder="Type a rack code or name (↑↓ choose, Enter)" autocomplete="off" spellcheck="false"></label>
    <div class="lp-list" role="listbox" tabindex="-1"></div>
    <label class="lp-boxl" ${config.location_boxes_enabled ? "" : "hidden"}>Box ${config.location_require_box ? "(required)" : "(optional)"}<select class="lp-box"></select></label>
    ${allowClear ? `<label class="chk lp-clearl"><input type="checkbox" class="lp-clear"> ${esc(clearLabel || "Instead, take it out of its rack (Unassigned)")}</label>` : ""}
    <p class="hint lp-target"></p></div>`);
  const q = body.querySelector(".lp-q"), list = body.querySelector(".lp-list"), boxSel = body.querySelector(".lp-box");
  const clear = body.querySelector(".lp-clear");
  const target = () => (clear && clear.checked ? null : chosen);
  const label = () => {
    const t = target();
    if (!t) return clear && clear.checked ? "Unassigned" : "";
    const b = t.boxes.find((x) => String(x.id) === boxSel.value);
    return t.code + (b ? " / " + b.code : "");
  };
  const refresh = () => {
    const submit = body.closest("form")?.querySelector("[type=submit]");
    if (submit) submit.firstChild.textContent = verb(label()) + " ";
    body.querySelector(".lp-target").textContent = label() ? "→ " + label() : "Choose a rack";
  };
  const paint = () => {
    list.innerHTML = shown.length ? shown.map((r, i) => `<div class="lp-opt${i === at ? " on" : ""}${chosen && chosen.id === r.id ? " picked" : ""}" role="option" data-i="${i}">
      <b>${esc(r.code)}</b><span>${esc(r.name || "")}</span>${r.boxes.length && config.location_boxes_enabled ? `<small>${r.boxes.length} box${r.boxes.length === 1 ? "" : "es"}</small>` : ""}</div>`).join("")
      : '<div class="lp-none">No rack matches</div>';
    list.querySelector(".lp-opt.on")?.scrollIntoView({ block: "nearest" });
  };
  const choose = (r) => {
    chosen = r;
    boxSel.innerHTML = `<option value="">${config.location_require_box ? "— choose a box —" : "— no box —"}</option>` +
      r.boxes.map((b) => `<option value="${b.id}">${esc(b.code)}${b.name ? " · " + esc(b.name) : ""}</option>`).join("");
    if (current && current.rack_id === r.id && current.box_id) boxSel.value = String(current.box_id);
    paint(); refresh();
  };
  q.addEventListener("input", () => {
    const t = q.value.trim().toLowerCase();
    shown = racks.filter((r) => !t || r.code.toLowerCase().includes(t) || (r.name || "").toLowerCase().includes(t));
    at = 0;
    const exact = shown.find((r) => r.code.toLowerCase() === t);
    if (exact) at = shown.indexOf(exact);
    paint();
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      at = Math.max(0, Math.min(shown.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)));
      paint();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!shown[at]) return;
      const already = chosen && chosen.id === shown[at].id;
      choose(shown[at]);
      // a rack with boxes: go on to the box; otherwise (or on a second Enter) apply
      if (config.location_boxes_enabled && chosen.boxes.length && !already) boxSel.focus();
      else body.closest("form").requestSubmit();
    }
  });
  list.addEventListener("click", (e) => { const o = e.target.closest("[data-i]"); if (o) { at = Number(o.dataset.i); choose(shown[at]); q.focus(); } });
  boxSel.addEventListener("change", refresh);
  clear?.addEventListener("change", () => { q.disabled = clear.checked; boxSel.disabled = clear.checked; refresh(); });
  if (current) { const r = racks.find((x) => x.id === current.rack_id); if (r) choose(r); }
  paint();
  let picked = null;
  const out = await modal({
    title, body, wide: true, submitLabel: verb(""),
    onOpen: () => { q.focus(); refresh(); },
    onSubmit: () => {
      const t = target();
      if (!t && !(clear && clear.checked)) throw new Error("Choose a rack (type its code, Enter)");
      if (t && config.location_boxes_enabled && config.location_require_box && !boxSel.value) throw new Error("Choose a box — a box is required");
      picked = { rack_id: t ? t.id : null, box_id: t && boxSel.value ? Number(boxSel.value) : null, label: label() };
      return apply ? apply(picked) : true;
    },
  });
  return out ? { ...picked, result: out } : null;
}

/** Plain-language result of a server-side move: "18 moved to R-A01 / B02 · 2 already there · 1 failed". */
export function resultText(out) {
  const parts = [`${out.processed} ${out.target_short === "Unassigned" ? "unassigned" : "moved to " + out.target_short}`];
  if (out.skipped.length) parts.push(`${out.skipped.length} already there`);
  if (out.failed.length) parts.push(`${out.failed.length} failed`);
  return parts.join(" · ");
}

export async function showFailures(out) {
  if (!out.failed.length && !out.skipped.length) return;
  const row = (x) => `<tr><td>${esc(x.name || "#" + x.id)}</td><td>${esc(x.reason)}</td></tr>`;
  await modal({ title: "Move details", submitLabel: "Close", wide: true, onSubmit: () => true,
    body: `<p>Requested ${out.requested} · moved ${out.processed} · skipped ${out.skipped.length} · failed ${out.failed.length}</p>
      <table class="rawtab"><thead><tr><th>Product</th><th>Why</th></tr></thead><tbody>
      ${out.failed.map(row).join("")}${out.skipped.map(row).join("")}</tbody></table>` });
}

/**
 * Move products (rows with ``id``, ``name``, ``rack_id``, ``box_id``) to a rack / box picked here.
 * What the screen showed is sent along, so a product another user moved meanwhile is reported
 * instead of being moved again from stale information.
 */
export async function moveProducts(ctx, rows, { title } = {}) {
  if (!rows.length) { ctx.status("Select products first", "warn"); return null; }
  const n = rows.length, one = n === 1 ? rows[0] : null;
  const expected = Object.fromEntries(rows.map((r) => [r.id, r.rack_id ? { rack_id: r.rack_id, box_id: r.box_id || null } : null]));
  const picked = await pickLocation({
    title: title || (one ? `Location — ${one.name}` : `Move ${n} products`),
    intro: one ? `Now: ${one.rack ? esc(one.rack + (one.box ? " / " + one.box : "")) : "Unassigned"}` : `${n} products selected.`,
    current: one && one.rack_id ? { rack_id: one.rack_id, box_id: one.box_id } : null,
    // taking products out of their rack only makes sense when some of them are in one
    allowClear: rows.some((r) => r.rack_id),
    clearLabel: one ? `Instead, take it out of ${one.rack}${one.box ? " / " + one.box : ""} (Unassigned)`
      : `Instead, take the ${rows.filter((r) => r.rack_id).length} that are in a rack out of it (Unassigned)`,
    verb: (t) => (t === "Unassigned" ? `Unassign ${n === 1 ? "" : n + " products"}`.trim() : `Move ${n === 1 ? "" : n + " products "}${t ? "to " + t : ""}`.trim()),
    apply: (t) => api("/api/erp/locations/assign", { method: "POST", body: { item_ids: rows.map((r) => r.id), rack_id: t.rack_id, box_id: t.box_id, expected } }),
  });
  if (!picked) return null;
  const out = picked.result;
  ctx.status(resultText(out), out.failed.length ? "warn" : "ok");
  if (out.failed.length) await showFailures(out);
  return out;
}
