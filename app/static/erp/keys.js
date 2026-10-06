// Keyboard shortcut registry for the ERP workspace.
//
// Actions and their defaults come from the server (keymap_service); a user's
// own assignments are stored server-side per user. Screens never hard-code
// keys: they register handlers by action id and ask this module which action
// a key press means. The validation rules mirror the server so the shortcuts
// window can refuse a key the moment it is pressed.
import { BOOT, api } from "erp/core";

// Older/cached shell pages may not carry the registry. Recover from the API;
// never silently turn every action into an unassigned shortcut.
async function loadKeymap() {
  try {
    const value = BOOT.keymap || await api("/api/erp/keymap");
    if (!Array.isArray(value?.actions) || !value.actions.length || !Array.isArray(value.fixed) || !Array.isArray(value.blocked)) {
      throw new Error("Missing shortcut registry");
    }
    return value;
  } catch (error) {
    const message = "Keyboard setup could not load. Restart the pharmacy server, then refresh this page. Your saved bills are unchanged.";
    const workspace = document.querySelector("#workspace");
    if (workspace) { workspace.textContent = message; workspace.setAttribute("role", "alert"); }
    const status = document.querySelector("#st-msg");
    if (status) { status.textContent = message; status.className = "st-msg error"; }
    throw error;
  }
}
const K = await loadKeymap();
export const ACTIONS = K.actions;
export const FIXED = K.fixed;
const BLOCKED = new Set(K.blocked);
const BY_ID = Object.fromEntries(ACTIONS.map((a) => [a.id, a]));
let overrides = { ...(K.overrides || {}) };
const listeners = new Set();

// short labels for the status bar
const SHORT = {
  "pos.saleType": "Sale type", "pos.manual": "Manual bill", "pos.search": "Item", "pos.qty": "Qty", "pos.batch": "Batch", "pos.remove": "Remove", "pos.itemDisc": "Item disc",
  "pos.billDisc": "Bill disc", "pos.cash": "Cash", "pos.upi": "UPI", "pos.card": "Card", "pos.split": "Split", "pos.udhaar": "Udhaar",
  "pos.save": "Save", "pos.hold": "Hold", "pos.resume": "Resume", "pos.customer": "Customer",
  "inv.search": "Search", "inv.new": "New", "inv.adjust": "Adjust", "inv.ledger": "Ledger", "inv.edit": "Edit",
  "inv.export": "Export", "masters.search": "Search", "masters.refresh": "Refresh", "masters.panel": "Switch list", "masters.new": "New category", "masters.merge": "Merge", "ledger.refresh": "Refresh",
  "purchases.search": "Search", "purchases.import": "Import", "purchases.manual": "Manual", "purchases.refresh": "Refresh",
  "purchases.panel": "Switch list", "purchases.supplier": "New supplier", "purchases.return": "Return",
  "sales.search": "Search", "sales.period": "Dates", "sales.refresh": "Refresh", "sales.return": "Return", "sales.exchange": "Exchange",
  "sales.reprint": "Reprint", "sales.edit": "Edit", "sales.void": "Void",
  "reports.view": "Grid/Document",
  "cust.search": "Search", "cust.newSale": "New sale", "cust.followup": "Follow up", "cust.refresh": "Refresh", "cust.panel": "Switch",
  "cust.note": "Note", "cust.edit": "Edit", "pos.followup": "Follow up",
  "adj.search": "Search", "adj.new": "New", "adj.period": "Dates", "adj.refresh": "Refresh", "adj.reverse": "Reverse",
  "history.search": "Search", "history.period": "Dates", "history.type": "Type", "history.refresh": "Refresh", "history.export": "Export",
  "inv.history": "History",
  "purchase.header": "Header", "purchase.add": "Add line", "purchase.product": "Product", "purchase.newProduct": "New product",
  "purchase.accept": "Accept", "purchase.next": "Next issue", "purchase.post": "Post", "purchase.columns": "Columns",
  "app.newBill": "New bill", "app.closeTab": "Close",
};

export const keyFor = (id) => (id in overrides ? overrides[id] : (BY_ID[id] || {}).key || "");
export const isDefault = (id) => !(id in overrides);
export const label = (id) => (BY_ID[id] || {}).label || id;
export const short = (id) => SHORT[id] || label(id);

/** The action a key means on this screen (screen actions win over global ones). */
export function lookup(scope, name) {
  if (!name) return null;
  for (const a of ACTIONS) if (a.scope === scope && keyFor(a.id) === name) return a.id;
  for (const a of ACTIONS) if (a.scope === "global" && keyFor(a.id) === name) return a.id;
  return null;
}
export const matches = (id, name) => !!name && keyFor(id) === name;

/** Status-bar hints for a screen, most important first. */
export function bar(scope) {
  return ACTIONS.filter((a) => a.scope === scope && a.bar && keyFor(a.id))
    .sort((a, b) => a.bar - b.bar)
    .map((a) => [keyFor(a.id), short(a.id)]);
}

// ---------------------------------------------------------------- validation (same rules as the server)
const NAV = new Set(["Enter", "Escape", "Tab", "Space", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Insert"]);

export function problem(key) {
  if (!key) return "Press a key combination";
  if (BLOCKED.has(key)) return `${key} is kept by the browser or used for tab switching — choose another`;
  const parts = key.split("+"), base = parts[parts.length - 1], mods = new Set(parts.slice(0, -1));
  if (["Ctrl", "Alt", "Shift", "Control", "Meta"].includes(base)) return "Add a key to the modifier (for example Alt+Q)";
  const fn = /^F([1-9]|1[0-2])$/.test(base);
  if ([...mods].some((m) => !["Ctrl", "Alt", "Shift"].includes(m)) || !(fn || NAV.has(base) || /^[A-Z0-9/,.;'\[\]\\`=\-]$/.test(base))) return "Use a letter, digit, navigation key or F1–F12 with valid modifiers";
  if (mods.has("Ctrl") && mods.has("Alt")) return "Ctrl+Alt combinations are reserved for system and AltGr typing shortcuts";
  if (!mods.size && !fn) return `${base} is needed for typing and moving around — use it with Ctrl or Alt, or use an F-key`;
  if (mods.size === 1 && mods.has("Shift") && !fn) return `Shift+${base} types a character — use Ctrl or Alt instead`;
  if (NAV.has(base) && !mods.has("Ctrl") && !mods.has("Alt") && !fn) return `${key} is used for moving in lists`;
  return "";
}

export function conflict(id, key) {
  const me = BY_ID[id];
  for (const a of ACTIONS) {
    if (a.id === id || keyFor(a.id) !== key) continue;
    if (a.scope === me.scope || a.scope === "global" || me.scope === "global") return a;
  }
  return null;
}

async function push(next) {
  const d = await api("/api/erp/keymap", { method: "PUT", body: { overrides: next } });
  overrides = d.overrides;
  listeners.forEach((fn) => fn());
}
/** Assign a key ("" = none, null = back to default). Throws with a reason when refused. */
export async function assign(id, key) {
  const next = { ...overrides };
  if (key === null) delete next[id];
  else {
    if (key) {
      const why = problem(key);
      if (why) throw new Error(why);
      const other = conflict(id, key);
      if (other) throw new Error(`${key} is already used for “${other.label}” (${other.group})`);
    }
    next[id] = key;
  }
  await push(next);
}
export const resetAll = () => push({});
export const onChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
