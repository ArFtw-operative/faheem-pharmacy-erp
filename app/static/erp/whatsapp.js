// WhatsApp invoices — shared by POS, Sales History and Settings.
// Sending is queued on the server and delivered in the background: nothing here waits
// for WhatsApp, and a sale never depends on it.
import { api, esc, fmtDateTime, h } from "erp/core";

// ERP-style WhatsApp mark (speech bubble + handset), drawn inline so it follows the theme
export const WA_ICON = `<svg class="wa-ico" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path fill="currentColor" d="M16 3C8.8 3 3 8.7 3 15.8c0 2.5.7 4.9 2.1 7L3.3 29l6.4-1.7c2 1.1 4.1 1.6 6.3 1.6 7.2 0 13-5.7 13-12.8S23.2 3 16 3zm0 23.5c-2 0-3.9-.5-5.6-1.5l-.4-.2-3.8 1 1-3.7-.3-.4a10.5 10.5 0 0 1-1.6-5.9C5.3 10 10.1 5.3 16 5.3S26.7 10 26.7 15.8 21.9 26.5 16 26.5zm5.9-7.9c-.3-.2-1.9-.9-2.2-1s-.5-.2-.7.2-.8 1-1 1.2-.4.2-.7.1a8.7 8.7 0 0 1-4.3-3.7c-.3-.6.3-.5.9-1.7.1-.2 0-.4 0-.5l-1-2.4c-.3-.6-.5-.5-.7-.5h-.6a1.2 1.2 0 0 0-.9.4 3.6 3.6 0 0 0-1.1 2.7 6.3 6.3 0 0 0 1.3 3.3 14.4 14.4 0 0 0 5.5 4.8c2 .9 2.8 1 3.8.8a3.3 3.3 0 0 0 2.2-1.5 2.7 2.7 0 0 0 .2-1.5c-.1-.1-.3-.2-.7-.4z"/></svg>`;

let cached = null, cachedAt = 0;
/** {state, connected, message} — never the QR or credentials. */
export async function waStatus(fresh = false) {
  if (!fresh && cached && Date.now() - cachedAt < 10000) return cached;
  try { cached = await api("/api/erp/whatsapp/status" + (fresh ? "?fresh=1" : "")); }
  catch (err) { cached = { state: "ERROR", connected: false, message: err.message }; }
  cachedAt = Date.now();
  return cached;
}

export function normalizePhone(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.length === 12 && d.startsWith("91")) d = d.slice(2);
  else if (d.length === 11 && d.startsWith("0")) d = d.slice(1);
  return d.length === 10 && /[6-9]/.test(d[0]) ? "91" + d : "";
}
export const prettyPhone = (p) => (p && p.length === 12 ? `+${p.slice(0, 2)} ${p.slice(2, 7)} ${p.slice(7)}` : p || "");

/** Queue the invoice and follow its delivery in the status bar (non-blocking). */
export async function sendInvoice(ctx, sale, phone = "", { onUpdate } = {}) {
  const d = await api(`/api/erp/sales/${sale.id}/whatsapp`, { method: "POST", body: phone ? { phone } : {} });
  const m = d.message;
  ctx.status(`${m.is_resend ? "Resending" : "Sending"} ${sale.invoice_no} on WhatsApp to ${m.customer_phone}…`, "info");
  track(ctx, m, onUpdate);
  return m;
}

export function track(ctx, m, onUpdate) {
  let tries = 0;
  const tick = async () => {
    tries++;
    try {
      const d = await api(`/api/erp/whatsapp/messages/${m.id}`);
      const s = d.message;
      if (onUpdate) onUpdate(s);
      if (s.whatsapp_status === "sent") { ctx.status(`✓ ${s.invoice_no} sent on WhatsApp to ${s.customer_phone}`, "ok"); return; }
      if (s.whatsapp_status === "failed") { ctx.status(`WhatsApp: ${s.invoice_no} not sent — ${s.last_error} (retry from Sales)`, "error"); return; }
      if (s.whatsapp_status === "queued" && s.attempt_count > 0 && tries === 4) ctx.status(`WhatsApp: ${s.invoice_no} will retry — ${s.last_error}`, "warn");
    } catch { /* keep polling quietly */ }
    if (tries < 60) setTimeout(tick, tries < 10 ? 2000 : 6000);      // about 5 minutes, covers the retries
  };
  setTimeout(tick, 1500);
}

const STATUS_LABEL = { queued: "Queued", sending: "Sending", sent: "Sent", failed: "Failed" };
/** Delivery list for a bill (Sales History). */
export function deliveriesHtml(list) {
  if (!list.length) return '<p class="muted wa-none">Not sent on WhatsApp yet.</p>';
  return `<table class="kvtab wa-log">${list.map((m) => `<tr data-msg="${m.id}">
    <td><span class="wa-st wa-${m.whatsapp_status}">${STATUS_LABEL[m.whatsapp_status] || m.whatsapp_status}</span>${m.is_resend ? ' <small class="muted">resend</small>' : ""}</td>
    <td class="mono">${esc(m.customer_phone)}</td>
    <td class="muted">${esc(fmtDateTime(m.sent_at || m.queued_at || ""))}${m.attempt_count > 1 ? ` · ${m.attempt_count} tries` : ""}</td>
    <td>${m.whatsapp_status === "failed" ? `<button type="button" class="btn wa-retry" data-retry="${m.id}">Retry</button>` : ""}</td></tr>
    ${m.last_error && m.whatsapp_status !== "sent" ? `<tr><td colspan="4" class="warn small">${esc(m.last_error)}</td></tr>` : ""}`).join("")}</table>`;
}

/** Ask for the number the customer wants the invoice on (prefilled with the saved mobile). */
export function askPhone({ anchor, initial = "", invoiceNo = "" }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const el = h(`<form class="wa-ask" role="dialog" aria-label="WhatsApp number">
      <header>${WA_ICON}<b>Send ${esc(invoiceNo)} on WhatsApp</b></header>
      <label>Customer's WhatsApp number<input name="phone" inputmode="tel" maxlength="16" value="${esc(initial)}" placeholder="98765 43210" autocomplete="off"></label>
      <p class="fu-err" role="alert"></p>
      <footer><button type="button" class="btn" data-x>Cancel <kbd>Esc</kbd></button><button class="btn primary wa-btn" type="submit">${WA_ICON} Send <kbd>Enter</kbd></button></footer></form>`);
    const close = (v) => { el.remove(); if (prev && prev.focus) prev.focus(); resolve(v); };
    el.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") { e.preventDefault(); close(null); } });
    el.querySelector("[data-x]").onclick = () => close(null);
    el.addEventListener("submit", (e) => {
      e.preventDefault();
      const p = normalizePhone(el.phone.value);
      if (!p) { el.querySelector(".fu-err").textContent = "Enter a 10-digit Indian mobile number (starting 6–9)"; return; }
      close(p);
    });
    document.body.append(el);
    const box = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: innerWidth / 2 - 170, bottom: 160 };
    el.style.left = Math.max(8, Math.min(box.left, innerWidth - 360)) + "px";
    el.style.top = Math.max(8, Math.min(box.bottom + 6, innerHeight - 220)) + "px";
    el.phone.focus(); el.phone.select();
  });
}
