// Keyboard shortcuts window (Ctrl+/ or F1): every action, its key, and a way to
// assign a different one. Fully keyboard driven:
//   ↑ ↓ select · Enter assign a new key (then press it) · Backspace clear · R reset · Esc close
// Keys the browser keeps for itself, typing keys and keys already used on the
// same screen are refused on the spot (and again by the server).
import { $, esc, h, isModifierOnly, keyName } from "erp/core";
import * as keys from "erp/keys";

let openNow = false;
// categories in the order people meet them in the app; any other group follows in registry order
const GROUP_ORDER = ["Everywhere", "POS", "Sales history", "Customers", "Inventory", "Stock ledger", "Stock history",
  "Stock adjustments", "Purchases", "Purchase document", "Masters", "Reports", "Settings"];
const ACTION_AT = new Map(keys.ACTIONS.map((a, i) => [a.id, i]));
const groupAt = (a) => { const i = GROUP_ORDER.indexOf(a.group); return i < 0 ? GROUP_ORDER.length + ACTION_AT.get(a.id) : i; };

export function openShortcuts({ status, scope } = {}) {
  if (openNow) return;
  openNow = true;
  const prev = document.activeElement;
  const el = h(`<div class="modal-backdrop"><div class="modal shortcuts" tabindex="-1" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">
    <header><h2>Keyboard shortcuts</h2>
      <input class="sc-q" placeholder="Search actions or keys…" autocomplete="off" aria-label="Search shortcuts">
      <span class="hint">↑↓ select · <kbd>Enter</kbd> change · <kbd>Backspace</kbd> clear · <kbd>R</kbd> reset · <kbd>Esc</kbd> close</span></header>
    <div class="modal-body"><table class="sc-table"><colgroup><col><col style="width:180px"><col style="width:180px"><col style="width:80px"></colgroup>
      <thead><tr><th>Action</th><th>Shortcut</th><th>Default</th><th></th></tr></thead><tbody></tbody></table>
      <h3 class="sc-h">Fixed keys (always the same)</h3>
      <table class="sc-table fixed"><tbody>${keys.FIXED.map(([k, what]) => `<tr><td><kbd>${esc(k)}</kbd></td><td>${esc(what)}</td></tr>`).join("")}</tbody></table>
      <p class="hint">Your shortcuts are saved to your user and follow you to every counter. Keys the browser never hands to a web page
      (Ctrl+T, Ctrl+W, Ctrl+Tab, Ctrl+1–9, F11, Alt+←/→ …), plain typing keys and keys already used on the same screen are refused.</p>
    </div>
    <footer><span class="sc-msg" role="status"></span><button type="button" class="btn" data-reset-all>Reset all to defaults</button><button type="button" class="btn primary" data-close>Close <kbd>Esc</kbd></button></footer>
  </div></div>`);
  const body = $("tbody", el), qIn = $(".sc-q", el), msg = $(".sc-msg", el);
  let rows = [], at = 0, capturing = null, busy = false;

  function render() {
    const q = qIn.value.trim().toLowerCase();
    rows = keys.ACTIONS.filter((a) => !q || `${a.label} ${a.group} ${keys.keyFor(a.id)}`.toLowerCase().includes(q));
    // the current screen's group first, then everywhere, then the rest — each group kept together, in menu order
    const rank = (a) => (a.scope === scope ? 0 : a.scope === "global" ? 1 : 2);
    rows.sort((a, b) => rank(a) - rank(b) || groupAt(a) - groupAt(b) || ACTION_AT.get(a.id) - ACTION_AT.get(b.id));
    at = Math.min(at, Math.max(rows.length - 1, 0));
    let group = "";
    body.innerHTML = rows.map((a, i) => {
      const head = a.group !== group ? `<tr class="grp"><td colspan="4">${esc(a.group)}${a.scope === scope ? " · this screen" : ""}</td></tr>` : "";
      group = a.group;
      const key = keys.keyFor(a.id);
      const cur = capturing === a.id ? '<span class="sc-cap">Press the new keys…</span>' : key ? `<kbd>${esc(key)}</kbd>` : '<span class="muted">none</span>';
      return `${head}<tr data-i="${i}" class="${i === at ? "on" : ""}${keys.isDefault(a.id) ? "" : " changed"}">
        <td>${esc(a.label)}</td><td>${cur}</td><td>${a.key ? `<kbd class="dim">${esc(a.key)}</kbd>` : ""}</td>
        <td class="sc-note">${keys.isDefault(a.id) ? "" : "custom"}</td></tr>`;
    }).join("") || '<tr><td colspan="4" class="muted">No action matches.</td></tr>';
    const on = $("tr.on", body);
    if (on) on.scrollIntoView({ block: "nearest" });
  }
  const say = (text, kind = "") => { msg.textContent = text; msg.className = "sc-msg " + kind; };
  const selected = () => rows[at];

  async function apply(id, key) {
    if (busy) return;
    busy = true;
    try {
      await keys.assign(id, key);
      capturing = null;
      say(key === null ? `${keys.label(id)}: back to ${keys.keyFor(id)}` : key ? `${keys.label(id)} → ${key}` : `${keys.label(id)}: no shortcut`, "ok");
      render();
    } catch (err) {
      say(err.message, "bad");
    } finally { busy = false; }
  }

  function close() {
    document.removeEventListener("keydown", onKey, true);
    el.remove();
    openNow = false;
    if (prev && prev.focus) prev.focus();
  }

  function onKey(e) {
    if (!document.body.contains(el)) return;
    const dialogs = document.querySelectorAll(".modal-backdrop");
    if (dialogs[dialogs.length - 1] !== el) return;   // a dialog opened on top of this one gets its own keys
    e.stopPropagation();
    if (e.isComposing || e.repeat || busy) { e.preventDefault(); return; }
    if (e.key === "Tab" && !capturing) {
      const focusable = [...el.querySelectorAll("input, button")];
      const i = focusable.indexOf(document.activeElement);
      e.preventDefault(); focusable[(i + (e.shiftKey ? -1 : 1) + focusable.length) % focusable.length].focus(); return;
    }
    if (capturing) {
      e.preventDefault();
      if (isModifierOnly(e)) return;
      const name = keyName(e);
      if (name === "Escape") { capturing = null; say("Cancelled"); render(); return; }
      if (name === "Backspace" || name === "Delete") { apply(capturing, ""); return; }
      const why = keys.problem(name);
      if (why) { say(`Not accepted: ${why}`, "bad"); return; }
      apply(capturing, name);
      return;
    }
    const inSearch = document.activeElement === qIn;
    if (e.key === "Escape") { e.preventDefault(); if (inSearch && qIn.value) { qIn.value = ""; render(); } else close(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (inSearch) $(".modal", el).focus();   // leave the search box: R / Backspace now act on the row
      at = e.key === "ArrowDown" ? Math.min(at + 1, rows.length - 1) : Math.max(at - 1, 0);
      render();
      return;
    }
    if (e.key === "PageDown") { e.preventDefault(); at = Math.min(at + 10, rows.length - 1); render(); return; }
    if (e.key === "PageUp") { e.preventDefault(); at = Math.max(at - 10, 0); render(); return; }
    if (e.key === "Enter" && selected() && !e.target.closest("button")) {
      e.preventDefault();
      capturing = selected().id;
      say(`Press the new shortcut for “${selected().label}” · Backspace = none · Esc = cancel`);
      render();
      return;
    }
    if (!inSearch && selected() && (e.key === "Backspace" || e.key === "Delete")) { e.preventDefault(); apply(selected().id, ""); return; }
    if (!inSearch && selected() && (e.key === "r" || e.key === "R") && !e.ctrlKey && !e.altKey) { e.preventDefault(); apply(selected().id, null); return; }
    if (!inSearch && e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) qIn.focus();  // type to filter
  }

  qIn.addEventListener("input", () => { at = 0; render(); });
  body.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-i]");
    if (!tr) return;
    if (busy) return;
    $(".modal", el).focus();
    at = Number(tr.dataset.i);
    capturing = rows[at].id;
    say(`Press the new shortcut for “${rows[at].label}” · Backspace = none · Esc = cancel`);
    render();
  });
  $("[data-close]", el).onclick = close;
  $("[data-reset-all]", el).onclick = async () => {
    if (busy) return;
    busy = true; capturing = null;
    try { await keys.resetAll(); say("All shortcuts are back to their defaults", "ok"); render(); } catch (err) { say(err.message, "bad"); } finally { busy = false; }
  };
  el.addEventListener("mousedown", (e) => { if (e.target === el) close(); });
  document.addEventListener("keydown", onKey, true);
  document.body.append(el);
  render();
  qIn.focus();
  if (status) status("Keyboard shortcuts: ↑↓ select, Enter then press the new keys");
}
