// Invoice studio — a panel inside the current workspace tab (POS after a bill
// is saved, or the Sales tab). Never a browser pop-up: the invoice renders in a
// frame inside the tab and prints from there, so every other tab stays usable.
//
//   1 A4 · 2 A5 · 3 Letter · 4 80 mm · 5 58 mm · 6 ERP text document · E expiry · B black & white
//   Ctrl+P print · PDF / Excel / CSV export · Esc closes (when closable)
import { BOOT, api, esc, fmtDateTime, h, money, store } from "erp/core";

export const SIZES = [["A4", "A4"], ["A5", "A5"], ["LETTER", "Letter"], ["THERMAL80", "80 mm"], ["THERMAL58", "58 mm"], ["ERP", "ERP text"]];

export function createStudio({ ctx, onClose = null, closeLabel = "Close" } = {}) {
  const CAN = BOOT.can || {};
  let sale = null, frame = null;
  const pref = store.get("studio:prefs", { size: "A4", expiry: true, mono: false });
  const brand = `${BOOT.pharmacy || "Faheem Pharmacy"} Invoice Studio`;
  const el = h(`<section class="studio" tabindex="-1" aria-label="${esc(brand)}">
    <header class="studio-head">
      <div class="studio-brand"><span class="studio-mark">${esc((BOOT.pharmacy || "Faheem Pharmacy").toUpperCase())}</span><b>Invoice Studio</b></div>
      <div class="studio-facts"></div>
    </header>
    <div class="studio-bar">
      <div class="studio-sizes" role="group" aria-label="Invoice format">
        ${SIZES.map(([k, l], i) => `<button type="button" class="btn" data-size="${k}" title="${l} (${i + 1})">${l}</button>`).join("")}
      </div>
      <label class="chk"><input type="checkbox" class="st-expiry"> Expiry <kbd>E</kbd></label>
      <label class="chk"><input type="checkbox" class="st-mono"> B&amp;W <kbd>B</kbd></label>
      <span class="spacer"></span>
      <span class="studio-info muted"></span>
      <button type="button" class="btn primary st-print">Print <kbd>Ctrl+P</kbd></button>
      <button type="button" class="btn st-exp" data-fmt="pdf">PDF</button>
      <button type="button" class="btn st-exp" data-fmt="xlsx">Excel</button>
      <button type="button" class="btn st-exp" data-fmt="csv">CSV</button>
      ${onClose ? `<button type="button" class="btn st-close">${esc(closeLabel)} <kbd>Esc</kbd></button>` : ""}
    </div>
    <div class="studio-stage"><p class="hint studio-empty">Select a bill to see its invoice.</p></div>
  </section>`);
  const facts = el.querySelector(".studio-facts");
  function renderFacts(d) {
    if (!d) { facts.innerHTML = ""; return; }
    const kv = (k, v, cls = "") => `<div class="kv ${cls}"><span>${esc(k)}</span><b>${v}</b></div>`;
    facts.innerHTML = [
      kv("Bill", `<span class="mono">${esc(d.invoice_no)}</span>${d.type === "MANUAL" ? ' <span class="tag">manual</span>' : ""}${d.status === "CANCELLED" ? ' <span class="doc-st ds-cancelled">VOIDED</span>' : ""}`),
      kv("Date", esc(fmtDateTime(d.date))),
      kv("Billed to", `${esc(d.customer || "Walk-in")}${d.mobile ? ` <span class="muted">${esc(d.mobile)}</span>` : ""}`),
      kv("Sale type", d.customer_type === "HOME_DELIVERY" ? "Home delivery" : "Walk-in"),
      kv("Items", `${d.items ?? (d.lines || []).length}${d.units ? ` · ${d.units} units` : ""}`),
      kv("Amount", `₹${money(d.total)}`, "amt"),
      kv("Paid by", esc(d.payment || "")),
      d.tendered ? kv("Received / change", `₹${money(d.tendered)} / ₹${money(d.change || 0)}`) : "",
      Number(d.refunded) ? kv("Refunded", `−₹${money(d.refunded)}`, "warn") : "",
      d.user ? kv("Billed by", esc(d.user)) : "",
    ].join("");
  }
  const stage = el.querySelector(".studio-stage"), info = el.querySelector(".studio-info");
  const expBox = el.querySelector(".st-expiry"), monoBox = el.querySelector(".st-mono");
  expBox.checked = pref.expiry; monoBox.checked = pref.mono;

  const save = () => store.set("studio:prefs", pref);
  const call = (fn, ...a) => { try { return frame && frame.contentWindow.studio && frame.contentWindow.studio[fn](...a); } catch { return null; } };
  function paintSizes() { el.querySelectorAll("[data-size]").forEach((b) => b.classList.toggle("on", b.dataset.size === pref.size)); }

  function setSize(size) {
    const reload = (size === "ERP") !== (pref.size === "ERP");     // the ERP text view is a different page
    pref.size = size;
    if (size.startsWith("THERMAL")) { pref.mono = true; monoBox.checked = true; }
    save(); paintSizes();
    if (reload && sale) show(sale); else call("setSize", size);
  }
  let showing = 0;
  async function show(s, { autoprint = false } = {}) {
    sale = s;
    const mine = ++showing;
    renderFacts(s && s.invoice_no ? s : null);
    if (s && s.id && !s.lines) {              // full bill facts for the header
      api(`/api/erp/sales/${s.id}`).then((d) => { if (mine === showing) { sale = { ...s, ...d }; renderFacts(sale); } }).catch(() => {});
    }
    if (!s) { stage.innerHTML = '<p class="hint studio-empty">Select a bill to see its invoice.</p>'; frame = null; return; }
    if (s.status === "CANCELLED") { stage.innerHTML = `<p class="hint studio-empty">${esc(s.invoice_no)} was voided — its invoice cannot be printed.</p>`; frame = null; return; }
    const qs = new URLSearchParams({ size: pref.size, mono: pref.mono ? 1 : 0, expiry: pref.expiry ? 1 : 0, autoprint: autoprint ? 1 : 0 });
    frame = h(`<iframe class="studio-frame" title="Invoice ${esc(s.invoice_no)}" src="/sales/${s.id}/invoice?${qs}"></iframe>`);
    stage.replaceChildren(frame);
    info.textContent = "Rendering…";
  }
  function print() {
    if (!frame) { ctx.status("Select a bill first", "warn"); return; }
    if (!CAN["sales.create"] && CAN["billing.print"] === false) return;
    call("print");
  }
  function exportAs(fmt) {
    if (!sale) { ctx.status("Select a bill first", "warn"); return; }
    const url = fmt === "pdf" ? `/sales/${sale.id}/pdf` : `/api/erp/sales/${sale.id}/export.${fmt}`;
    const a = document.createElement("a"); a.href = url; a.download = ""; document.body.append(a); a.click(); a.remove();
    ctx.status(`Downloading ${sale.invoice_no} as ${fmt.toUpperCase()}`, "ok");
  }

  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data || e.data.source !== "invoice-studio" || !frame || e.source !== frame.contentWindow) return;
    if (e.data.type === "rendered") info.textContent = `${e.data.pages === 1 ? "1 page" : e.data.pages + " pages"} · ${e.data.label}`;
    else if (e.data.type === "error") info.textContent = e.data.message;
  });
  el.querySelector(".studio-sizes").addEventListener("click", (e) => { const b = e.target.closest("[data-size]"); if (b) setSize(b.dataset.size); });
  expBox.addEventListener("change", () => { pref.expiry = expBox.checked; save(); call("setExpiry", pref.expiry); });
  monoBox.addEventListener("change", () => { pref.mono = monoBox.checked; save(); call("setMono", pref.mono); });
  el.querySelector(".st-print").onclick = print;
  el.querySelectorAll(".st-exp").forEach((b) => { b.onclick = () => exportAs(b.dataset.fmt); });
  const close = el.querySelector(".st-close");
  if (close) close.onclick = () => onClose();
  paintSizes();

  /** Keys while the studio is showing; returns true when handled. */
  function onKey(e, name) {
    if (name === "Ctrl+P") { print(); return true; }
    if (/^[1-6]$/.test(name)) { setSize(SIZES[Number(name) - 1][0]); return true; }
    if (name === "E") { expBox.checked = !expBox.checked; expBox.dispatchEvent(new Event("change")); return true; }
    if (name === "B") { monoBox.checked = !monoBox.checked; monoBox.dispatchEvent(new Event("change")); return true; }
    if (name === "Escape" && onClose) { onClose(); return true; }
    return false;
  }
  return { el, show, print, exportAs, onKey, get sale() { return sale; } };
}
