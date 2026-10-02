// Shared helpers for the ERP workspace (no framework, no build step).

export const BOOT = window.ERP_BOOT || {};
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

export class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}

/** JSON fetch. Aborted requests reject with AbortError (callers ignore it). */
export async function api(url, { method = "GET", body, signal } = {}) {
  const res = await fetch(url, {
    method, signal, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 || (res.redirected && res.url.includes("/login"))) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new ApiError("Session expired", 401);
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : (d && d.message) || `Request failed (${res.status})`;
    throw new ApiError(msg, res.status, d);
  }
  return data;
}

export const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
export const r2 = (n) => Math.round((num(n) + Number.EPSILON) * 100) / 100;
export const money = (v) => num(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const rupees = (v) => "₹" + money(v);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2030-02-01" → "Feb-2030" (pharmacy expiry format). */
export const fmtExp = (iso) => (iso ? `${MONTHS[Number(iso.slice(5, 7)) - 1]}-${iso.slice(0, 4)}` : "—");
export const fmtExpShort = (iso) => (iso ? `${MONTHS[Number(iso.slice(5, 7)) - 1]}-${iso.slice(2, 4)}` : "—");
/** "29-Sep-2026 21:48" */
export function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}-${MONTHS[d.getMonth()]}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Month-level expiry, same rule as the server (sellable through the printed month). */
export function isExpired(iso, today = new Date()) {
  if (!iso) return false;
  const y = Number(iso.slice(0, 4)), m = Number(iso.slice(5, 7));
  return y * 12 + m < today.getFullYear() * 12 + today.getMonth() + 1;
}
export function daysUntil(iso, today = new Date()) {
  if (!iso) return Infinity;
  return Math.floor((new Date(iso + "T00:00:00") - new Date(today.toDateString())) / 86400000);
}

export function unitName(unit, n) {
  const w = String(unit || "unit").toLowerCase();
  if (n === 1) return w;
  if (w === "box") return "boxes";
  if (w === "piece" || w === "pair") return w + "s";
  return w + "s";
}

/** "3" → 3 · "1s" → one pack · "2s+3" → two packs and three units · NaN if not understood. */
export function parseQty(text, upp) {
  const t = String(text ?? "").trim();
  if (/^\d+$/.test(t)) return parseInt(t, 10);
  const m = /^(\d+)\s*[sp]\s*(?:\+\s*(\d+))?$/i.exec(t);
  return m ? parseInt(m[1], 10) * Math.max(1, upp || 1) + parseInt(m[2] || "0", 10) : NaN;
}

export function describe(qty, upp, base, pack) {
  upp = Math.max(1, upp || 1);
  if (upp === 1) return `${qty} ${unitName(base, qty)}`;
  const full = Math.floor(qty / upp), loose = qty % upp;
  const parts = [];
  if (full) parts.push(`${full} ${unitName(pack, full)}`);
  if (loose || !full) parts.push(`${loose} ${unitName(base, loose)}`);
  return parts.join(" + ");
}

export function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = (...a) => { clearTimeout(t); fn(...a); };
  d.cancel = () => clearTimeout(t);
  return d;
}

export const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));

export const store = {
  get(key, fallback) { try { const v = localStorage.getItem("erp:" + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("erp:" + key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

/** Canonical key name, identical to the server's keymap_service.normalize():
 *  "F2", "Ctrl+H", "Ctrl+Shift+H", "Alt+N", "Shift+F7", "Ctrl+/". Letters and digits
 *  come from the physical key, so Ctrl+Shift+1 is not read as "Ctrl+!". */
export function keyName(e) {
  const parts = [];
  const mod = e.ctrlKey || e.metaKey || e.altKey;
  if (e.ctrlKey || e.metaKey) parts.push("Ctrl");
  if (e.altKey) parts.push("Alt");
  let k = e.key;
  if (/^Key[A-Z]$/.test(e.code || "")) k = e.code.slice(3);
  else if (mod && /^Digit[0-9]$/.test(e.code || "")) k = e.code.slice(5);
  else if (mod && ({ Slash: "/", Comma: ",", Period: ".", Semicolon: ";", Quote: "'", BracketLeft: "[", BracketRight: "]", Backslash: "\\", Backquote: "`", Minus: "-", Equal: "=" })[e.code])
    k = ({ Slash: "/", Comma: ",", Period: ".", Semicolon: ";", Quote: "'", BracketLeft: "[", BracketRight: "]", Backslash: "\\", Backquote: "`", Minus: "-", Equal: "=" })[e.code];
  else if (k === " ") k = "Space";
  else if (k === "Esc") k = "Escape";
  else if (k && k.length === 1) k = k.toUpperCase();
  if (e.shiftKey && (mod || (k && k.length > 1))) parts.push("Shift");
  parts.push(k);
  return parts.join("+");
}
export const isModifierOnly = (e) => ["Control", "Alt", "Shift", "Meta", "AltGraph", "CapsLock"].includes(e.key);

/** A small in-page modal (no browser dialogs). Resolves with the form result or null. */
// Esc always reaches the topmost pop-up, even when focus has slipped outside it
// (a click on blank space, a button that was removed). Each pop-up keeps its own
// Esc logic; this only hands the key back to it. Registered when core first loads,
// so it runs before any screen or pop-up listener.
const POPUPS = ".modal-backdrop, .fu-pop, .wa-ask, .ctx-menu";
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !e.isTrusted) return;
  const all = document.querySelectorAll(POPUPS);
  const top = all[all.length - 1];
  if (!top || top.contains(e.target)) return;
  e.preventDefault(); e.stopImmediatePropagation();
  const into = top.querySelector("input, select, textarea, button, [tabindex]") || top;
  if (into.focus) into.focus({ preventScroll: true });
  into.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true, cancelable: true }));
}, true);

export function modal({ title, body, onOpen, onSubmit, submitLabel = "Save", wide = false }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const el = h(`<div class="modal-backdrop"><form class="modal${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}" novalidate>
      <header><h2>${esc(title)}</h2><span class="hint">Enter saves · Esc cancels</span></header>
      <div class="modal-body"></div>
      <p class="modal-error" role="alert"></p>
      <footer><button type="button" class="btn" data-cancel>Cancel <kbd>Esc</kbd></button>
      <button type="submit" class="btn primary">${esc(submitLabel)} <kbd>Enter</kbd></button></footer></form></div>`);
    const form = el.querySelector("form");
    const bodyEl = el.querySelector(".modal-body");
    if (typeof body === "string") bodyEl.innerHTML = body; else bodyEl.append(body);
    const err = el.querySelector(".modal-error");
    let closed = false;
    const close = (value) => {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown',dialogKeys,true);
      el.remove(); if (prev && prev.isConnected && prev.focus) prev.focus(); resolve(value);
    };
    const dialogKeys = e => {
      const dialogs = document.querySelectorAll('.modal-backdrop');
      if (dialogs[dialogs.length - 1] !== el) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
      else if (e.key === 'Tab') {
        const controls = Array.from(form.querySelectorAll('input,select,textarea,button,[tabindex]'))
          .filter(control => !control.disabled && control.tabIndex >= 0 && control.getClientRects().length);
        if (!controls.length) return;
        const index = controls.indexOf(document.activeElement);
        if (index < 0 || (e.shiftKey ? index === 0 : index === controls.length - 1)) {
          e.preventDefault(); controls[e.shiftKey ? controls.length - 1 : 0].focus();
        }
      }
    };
    document.addEventListener('keydown',dialogKeys,true);
    el.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); close(null); }
      // keyboard-first: Enter on a dropdown submits like Enter in a text box
      else if (e.key === "Enter" && e.target.tagName === "SELECT" && !e.altKey) { e.preventDefault(); form.requestSubmit(); }
    });
    el.querySelector("[data-cancel]").onclick = () => close(null);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      const btn = form.querySelector("[type=submit]");
      btn.disabled = true;
      try {
        const out = await onSubmit(form, (m) => { err.textContent = m; });
        if (out !== undefined && out !== false) close(out);
      } catch (ex) { err.textContent = ex.message || String(ex); }
      btn.disabled = false;
    });
    document.body.append(el);
    if (onOpen) onOpen(form);
    const first = form.querySelector("[autofocus]") || form.querySelector("input,select,textarea");
    if (first) { first.focus(); if (first.select) first.select(); }
  });
}
