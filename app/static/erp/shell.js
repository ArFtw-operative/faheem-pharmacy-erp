import * as keys from "erp/keys";
import { openShortcuts } from "erp/shortcuts";
// ERP shell: module bar, workspace tabs (state kept alive while switching),
// global keyboard map, command palette (Ctrl+K), quick product lookup (Ctrl+F),
// status bar. Screens are ES modules exporting create(ctx, params, saved).
import { $, BOOT, api, esc, fmtExpShort, h, keyName, money, store, unitName } from "erp/core";

const MODULES = {
  pos: { label: "POS", key: "Alt+P", load: () => import("erp/pos"), multi: true },
  inventory: { label: "Inventory", key: "Alt+I", load: () => import("erp/inventory") },
  history: { label: "Stock History", load: () => import("erp/history"), multi: true },
  adjustments: { label: "Adjustments", load: () => import("erp/adjustments") },
  ledger: { label: "Ledger", load: () => import("erp/ledger"), hidden: true, multi: true },
  purchases: { label: "Purchases", key: "Alt+U", load: () => import("erp/purchases") },
  purchase: { label: "Purchase", load: () => import("erp/purchase"), hidden: true, multi: true },
  sales: { label: "Sales", load: () => import("erp/sales") },
  customers: { label: "Customers", load: () => import("erp/customers") },
  reports: { label: "Reports", load: () => import("erp/reports") },
  racks: { label: "Racks", key: "Alt+K", load: () => import("erp/racks") },
  masters: { label: "Categories & Forms", load: () => import("erp/masters") },
  settings: { label: "Settings", load: () => import("erp/settings") },
};
const CAN_OPEN = new Set(BOOT.modules || []);
CAN_OPEN.add("ledger");
if (CAN_OPEN.has("purchases")) CAN_OPEN.add("purchase");

const tabs = [];           // {id, module, params, title, el, screen, dirty}
let active = null;
let seq = 0;
const workspace = $("#workspace");
const tabBar = $("#wtabs");
const tabList = $('#tabs-list');
function tabOverflow() {
  $('#tabs-prev').disabled=tabBar.scrollLeft<=1;
  $('#tabs-next').disabled=tabBar.scrollLeft+tabBar.clientWidth>=tabBar.scrollWidth-1;
}
function revealActiveTab() {
  const button=tabBar.querySelector('.wtab.on');
  if(button) {
    const bounds=tabBar.getBoundingClientRect(),rect=button.getBoundingClientRect();
    if(rect.left<bounds.left)tabBar.scrollLeft-=bounds.left-rect.left;
    else if(rect.right>bounds.right)tabBar.scrollLeft+=rect.right-bounds.right;
  }
  tabOverflow();
}
$('#tabs-prev').onclick=()=>tabBar.scrollBy({left:-Math.max(160,tabBar.clientWidth*.7),behavior:'smooth'});
$('#tabs-next').onclick=()=>tabBar.scrollBy({left:Math.max(160,tabBar.clientWidth*.7),behavior:'smooth'});
tabBar.addEventListener('scroll',tabOverflow,{passive:true});
new ResizeObserver(revealActiveTab).observe(tabBar);
tabList.onchange=()=>{const tab=tabs.find(t=>t.id===tabList.value);if(tab)activate(tab);tabList.value='';};

// ------------------------------------------------------------------ status bar
let stTimer;
function status(msg, kind = "info") {
  const el = $("#st-msg");
  el.textContent = msg;
  el.className = "st-msg " + kind;
  clearTimeout(stTimer);
  if (kind !== "error") stTimer = setTimeout(() => { el.textContent = "Ready"; el.className = "st-msg"; }, 7000);
}
function renderKeys() {
  const hints = [...((active && active.screen.keys) || []), ...["app.newBill", "app.closeTab"].filter((id) => keys.keyFor(id)).map((id) => [keys.keyFor(id), keys.short(id)])];
  $("#st-keys").innerHTML = hints.map(([k, label]) => `<span><kbd>${esc(k)}</kbd>${esc(label)}</span>`).join("");
}

// ------------------------------------------------------------------ durable workspace (crash recovery)
// Open tabs and each tab's unfinished state (POS bills…) are written to the server a moment after every
// change, every 15 s and when the page closes — committed to the database, so a crash, power cut or
// reboot loses nothing: the next login at this counter reopens the same work.
const TERMINAL = (() => {
  let t = store.get("terminal", "");
  if (!t) {
    t = (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("terminal", t);
  }
  return t;
})();
const tabStates = {};
let flushTimer = null, lastSent = "", restoring = false;
function snapshotData() {
  return {
    tabs: tabs.map((t) => ({ id: t.id, module: t.module, params: t.params, title: t.title })),
    active: active && active.id,
    states: Object.fromEntries(tabs.filter((t) => tabStates[t.id] !== undefined).map((t) => [t.id, tabStates[t.id]])),
  };
}
function scheduleFlush(delay = 800) {
  if (restoring) return;
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flush, delay);
}
async function flush() {
  flushTimer = null;
  if (restoring) return;
  const body = JSON.stringify({ terminal: TERMINAL, data: snapshotData() });
  if (body === lastSent) return;
  try {
    const r = await fetch("/api/erp/workspace", { method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" },
                                                  body, keepalive: body.length < 60000 });
    if (r.ok) lastSent = body; else if (r.status !== 401) scheduleFlush(5000);
  } catch { scheduleFlush(5000); }                          // server restarting: try again shortly
}
function flushNow() {                                         // the page is closing: a beacon outlives it
  if (restoring) return;
  const body = JSON.stringify({ terminal: TERMINAL, data: snapshotData() });
  if (body !== lastSent && navigator.sendBeacon && navigator.sendBeacon("/api/erp/workspace", new Blob([body], { type: "application/json" }))) lastSent = body;
}
setInterval(flush, 15000);
window.addEventListener("pagehide", flushNow);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushNow(); });

// ------------------------------------------------------------------ tabs
function saveTabs() {
  scheduleFlush();
  try {
    sessionStorage.setItem("erp:tabs", JSON.stringify({
      tabs: tabs.map((t) => ({ module: t.module, params: t.params, id: t.id })),
      active: active && active.id,
    }));
  } catch { /* private mode */ }
}
function saveTabState(tab, state) {
  tabStates[tab.id] = state === undefined ? null : JSON.parse(JSON.stringify(state));   // a copy of this moment
  scheduleFlush();
  try { sessionStorage.setItem("erp:tab:" + tab.id, JSON.stringify(state)); } catch { /* ignore */ }
}
function loadTabState(id) {
  try { return JSON.parse(sessionStorage.getItem("erp:tab:" + id) || "null"); } catch { return null; }
}

function renderTabs() {
  tabBar.innerHTML = tabs.map((t, i) => `<button type="button" role="tab" class="wtab${t === active ? " on" : ""}" data-tab="${t.id}"
    aria-selected="${t === active}" title="${esc(t.title)}${i < 9 ? " (Alt+" + (i + 1) + ")" : ""}">
    <span>${esc(t.title)}</span>${t.dirty ? '<b class="dot" title="Unsaved">•</b>' : ""}<i data-close="${t.id}" title="Close (${esc(keys.keyFor("app.closeTab"))})">×</i></button>`).join("");
  $("#modules").querySelectorAll("[data-module]").forEach((b) => b.classList.toggle("on", !!active && active.module === b.dataset.module));
  if (typeof fitModules === "function" && $("#modules").querySelector("[data-module][hidden].on")) fitModules();
  tabList.innerHTML=`<option value="">All tabs (${tabs.length})</option>`+tabs.map(t=>`<option value="${esc(t.id)}">${t===active?'● ':''}${esc(t.title)}${t.dirty?' •':''}</option>`).join('');
  requestAnimationFrame(revealActiveTab);
}

function paramsKey(p) { return JSON.stringify(p || {}); }

async function open(module, params = {}, { id, focus = true, fresh = false } = {}) {
  const def = MODULES[module];
  if (!def || !CAN_OPEN.has(module)) { status("You do not have access to " + (def ? def.label : module), "warn"); return null; }
  if (def.href) { window.open(def.href, "_blank"); status(`${def.label} opened in the classic screen (new browser tab)`); return null; }
  if (!fresh && !id) {
    const existing = tabs.find((t) => t.module === module && (def.multi ? paramsKey(t.params) === paramsKey(params) && module !== "pos" : true));
    const firstPos = module === "pos" && tabs.find((t) => t.module === "pos");
    const reuse = existing || firstPos;
    if (reuse) {
      activate(reuse, focus);
      if (params && Object.keys(params).length && reuse.screen.navigate) reuse.screen.navigate(params);   // e.g. a bill from a report
      return reuse;
    }
  }
  const mod = await def.load();
  const tab = { id: id || `t${Date.now().toString(36)}${seq++}`, module, params, title: def.label, dirty: false };
  tab.el = h(`<section class="screen" data-screen="${module}" hidden></section>`);
  workspace.append(tab.el);
  const ctx = {
    boot: BOOT,
    status,
    open,
    tabId: null,
    /** Bring an existing tab forward and hand it a target; false if that tab is gone or declines. */
    goto: (id, target) => {
      const t = tabs.find((x) => x.id === id);
      if (!t) return false;
      if (t.screen.navigate && t.screen.navigate(target) === false) return false;
      activate(t);
      return true;
    },
    close: () => closeTab(tab),
    setTitle: (t) => {
      if (tab.title === t) return;
      tab.title = t; renderTabs(); saveTabs();
      if (tab === active) document.title = t + " · Faheem Pharmacy";
    },
    setDirty: (d) => { if (tab.dirty !== !!d) { tab.dirty = !!d; renderTabs(); } },
    setKeys: () => { if (tab === active) renderKeys(); },
    save: (state) => saveTabState(tab, state),
    isActive: () => tab === active,
  };
  ctx.tabId = tab.id;
  const initial = loadTabState(tab.id);
  if (initial !== null) tabStates[tab.id] = initial;
  tab.screen = mod.create(ctx, params, tab.el, initial);
  tabs.push(tab);
  activate(tab, focus);
  saveTabs();
  return tab;
}

const recent = [];          // tab ids, most recently used first (tab switcher order)
// ------------------------------------------------------------------ other counters (live refresh)
// Every few seconds: what changed anywhere (GET /api/erp/sync, one tiny query). The screen on show
// refreshes the areas that moved; screens in other tabs refresh when shown. Never while a dialog is
// open (someone is in the middle of something) — that waits for the next round.
let syncSeen = null;
const pendingAreas = new Map();      // tab id → Set(areas) not yet shown to that tab
function deliver(tab, areas) {
  if (!areas.length || !tab.screen.onDataChanged) return;
  try { tab.screen.onDataChanged(areas); } catch (err) { console.error(err); }
}
async function syncTick() {
  if (document.hidden || !tabs.length) return;
  let now;
  try { now = (await api("/api/erp/sync")).versions; } catch { return; }
  if (syncSeen === null) { syncSeen = now; return; }
  const changed = Object.keys(now).filter((a) => now[a] !== syncSeen[a]);
  if (!changed.length) return;
  if (document.querySelector(".modal-backdrop, .ctx-menu")) return;          // try again next round
  syncSeen = now;
  for (const t of tabs) {
    const set = pendingAreas.get(t.id) || new Set();
    changed.forEach((a) => set.add(a));
    pendingAreas.set(t.id, set);
  }
  if (active) { deliver(active, [...pendingAreas.get(active.id)]); pendingAreas.delete(active.id); }
}
setInterval(syncTick, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) syncTick(); });

function activate(tab, focus = true) {
  if (pendingAreas.has(tab.id)) {     // changes made elsewhere while this tab was in the background
    const areas = [...pendingAreas.get(tab.id)]; pendingAreas.delete(tab.id);
    setTimeout(() => deliver(tab, areas), 0);
  }
  const r = recent.indexOf(tab.id);
  if (r >= 0) recent.splice(r, 1);
  recent.unshift(tab.id);
  if (active && active !== tab) {
    active.el.hidden = true;
    if (active.screen.onHide) active.screen.onHide();
  }
  active = tab;
  tab.el.hidden = false;
  renderTabs();
  renderKeys();
  history.replaceState(null, "", "/app/" + tab.module + (tab.params && tab.params.item ? "/" + tab.params.item : ""));
  document.title = tab.title + " · Faheem Pharmacy";
  if (tab.screen.onShow) tab.screen.onShow({ focus });
  saveTabs();
}

async function closeTab(tab = active) {
  if (!tab) return;
  if (tab.screen.beforeClose) {
    if (!(await tab.screen.beforeClose())) return;   // the screen asks its own question (POS: hold / discard)
  } else if (tab.dirty && !(await confirmBox(`"${tab.title}" has unsaved work. Close it anyway?`))) return;
  const i = tabs.indexOf(tab);
  tabs.splice(i, 1);
  if (tab.screen.destroy) tab.screen.destroy();
  tab.el.remove();
  try { sessionStorage.removeItem("erp:tab:" + tab.id); } catch { /* ignore */ }
  delete tabStates[tab.id];
  if (active === tab) {
    active = null;
    const next = tabs[Math.min(i, tabs.length - 1)];
    if (next) activate(next); else { renderTabs(); renderKeys(); }
  } else renderTabs();
  saveTabs();
}

/** A new POS bill in its own tab, so another customer can be billed while this one waits. */
async function newBill() {
  const count = tabs.filter((t) => t.module === "pos").length;
  const tab = await open("pos", {}, { fresh: true });
  if (tab) status(`New bill opened (${count + 1} bills open) · Alt+1…9 switches tabs`, "ok");
}

function cycle(dir) {
  if (!tabs.length) return;
  const i = tabs.indexOf(active);
  activate(tabs[(i + dir + tabs.length) % tabs.length]);
}

function confirmBox(message) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const el = h(`<div class="modal-backdrop"><div class="modal small" role="alertdialog" aria-modal="true">
      <div class="modal-body"><p>${esc(message)}</p></div>
      <footer><button type="button" class="btn" data-no>No <kbd>Esc</kbd></button><button type="button" class="btn danger" data-yes>Yes <kbd>Y</kbd></button></footer></div></div>`);
    const done = (v) => { el.remove(); if (prev && prev.focus) prev.focus(); resolve(v); };
    el.querySelector("[data-no]").onclick = () => done(false);
    el.querySelector("[data-yes]").onclick = () => done(true);
    el.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape" || e.key.toLowerCase() === "n") { e.preventDefault(); done(false); }
      if (e.key.toLowerCase() === "y") { e.preventDefault(); done(true); }
    });
    document.body.append(el);
    el.querySelector("[data-no]").focus();
  });
}
window.erpConfirm = confirmBox;

// Right-click a tab: new bill (POS), close, close the others.
tabBar.addEventListener("contextmenu", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  e.preventDefault();
  const tab = tabs.find((t) => t.id === b.dataset.tab);
  if (!tab) return;
  document.querySelectorAll(".ctx-menu").forEach((m) => m.remove());
  const items = [
    ...(tab.module === "pos" || CAN_OPEN.has("pos") ? [
      { label: "New POS tab (new bill)", key: keys.keyFor("app.newBill"), run: newBill },
      { label: "New manual bill tab", key: "", run: async () => { const t = await open("pos", {}, { fresh: true }); t && t.screen.manual && t.screen.manual(); } },
    ] : []),
    { label: "Close tab", key: keys.keyFor("app.closeTab"), run: () => closeTab(tab) },
    ...(tabs.length > 1 ? [{ label: "Close other tabs", key: "", run: async () => { for (const t of tabs.filter((x) => x !== tab)) await closeTab(t); } }] : []),
  ];
  const prev = document.activeElement;
  const menu = h(`<div class="ctx-menu" role="menu">${items.map((it, i) => `<button type="button" role="menuitem" data-i="${i}">${esc(it.label)}${it.key ? `<kbd>${esc(it.key)}</kbd>` : ""}</button>`).join("")}</div>`);
  const close = (restore = true) => { menu.remove(); document.removeEventListener("mousedown", outside, true); if (restore && prev && prev.focus) prev.focus(); };
  const outside = (ev) => { if (!menu.contains(ev.target)) close(); };
  menu.addEventListener("click", (ev) => { const x = ev.target.closest("[data-i]"); if (x) { close(false); items[Number(x.dataset.i)].run(); } });
  menu.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    const btns = [...menu.querySelectorAll("button")], at = btns.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); btns[(at + (ev.key === "ArrowDown" ? 1 : -1) + btns.length) % btns.length].focus(); }
    else if (ev.key === "Escape") { ev.preventDefault(); close(); }
  });
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(e.clientX, innerWidth - r.width - 6) + "px";
  menu.style.top = Math.min(e.clientY, innerHeight - r.height - 6) + "px";
  document.addEventListener("mousedown", outside, true);
  setTimeout(() => menu.isConnected && menu.querySelector("button").focus(), 60);   // after the tab's own onShow focus
});

tabBar.addEventListener("mousedown", (e) => {
  if (e.button === 2) { const b = e.target.closest("[data-tab]"); if (b) { e.preventDefault(); activate(tabs.find((t) => t.id === b.dataset.tab)); } return; }
  const x = e.target.closest("[data-close]");
  if (x) { e.preventDefault(); closeTab(tabs.find((t) => t.id === x.dataset.close)); return; }
  const b = e.target.closest("[data-tab]");
  if (b) { e.preventDefault(); activate(tabs.find((t) => t.id === b.dataset.tab)); }
});

// ------------------------------------------------------------------ module bar
$("#modules").innerHTML = Object.entries(MODULES).filter(([m, d]) => !d.hidden && CAN_OPEN.has(m))
  .map(([m, d]) => `<button type="button" data-module="${m}" title="${esc(d.label)}${d.key ? " (" + d.key + ")" : ""}${d.href ? " — classic screen" : ""}">${esc(d.label)}</button>`).join("");
$("#modules").addEventListener("click", (e) => {
  const more = e.target.closest(".mod-more");
  if (more) { toggleMoreMenu(more); return; }
  const b = e.target.closest("[data-module]");
  if (b) { closeMoreMenu(); open(b.dataset.module); }
});

// Every module stays reachable whatever the screen width or zoom: the bar tightens its spacing, then
// moves the buttons that do not fit (never the open one) into "More ▾". Nothing hides off-screen.
function fitModules() {
  const nav = $("#modules"), bar = nav.closest(".appbar");
  nav.querySelector(".mod-more")?.remove();
  closeMoreMenu();
  const buttons = [...nav.querySelectorAll("[data-module]")];
  buttons.forEach((b) => { b.hidden = false; });
  bar.classList.remove("compact", "tight");
  const fits = () => nav.scrollWidth <= nav.clientWidth + 1;
  if (fits()) return;
  bar.classList.add("compact");
  if (fits()) return;
  bar.classList.add("tight");
  if (fits()) return;
  const more = h('<button type="button" class="mod-more" aria-haspopup="menu" title="More modules">More ▾</button>');
  nav.append(more);
  for (const b of [...buttons].reverse()) {
    if (fits()) break;
    if (b.classList.contains("on")) continue;
    b.hidden = true;
  }
  more.classList.toggle("on", buttons.some((b) => b.hidden && b.classList.contains("on")));
}
function closeMoreMenu() { document.querySelector(".mod-menu")?.remove(); }
function toggleMoreMenu(btn) {
  if (document.querySelector(".mod-menu")) { closeMoreMenu(); return; }
  const hidden = [...$("#modules").querySelectorAll("[data-module]")].filter((b) => b.hidden);
  const menu = h(`<div class="mod-menu" role="menu">${hidden.map((b) => `<button type="button" role="menuitem" data-module="${b.dataset.module}">${esc(b.textContent)}</button>`).join("")}</div>`);
  const r = btn.getBoundingClientRect();
  menu.style.top = r.bottom + "px"; menu.style.right = Math.max(4, window.innerWidth - r.right) + "px";
  menu.addEventListener("click", (e) => { const b = e.target.closest("[data-module]"); if (b) { closeMoreMenu(); open(b.dataset.module); } });
  document.body.append(menu);
  menu.querySelector("button")?.focus();
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".mod-menu, .mod-more")) closeMoreMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMoreMenu(); });
new ResizeObserver(() => fitModules()).observe(document.querySelector(".appbar"));
fitModules();
$("#operator-label").textContent = (BOOT.user && BOOT.user.name) || "";
$("#st-right").textContent = [BOOT.pharmacy, (BOOT.user || {}).name].filter(Boolean).join(" · ");
$("#st-right").classList.toggle("day-open", !!BOOT.session && BOOT.session.status !== "CLOSED");
function tick() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
  $("#clock").textContent = `${p(d.getDate())}-${m}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
tick();
setInterval(tick, 1000);

// ------------------------------------------------------------------ command palette (Ctrl+K)
const COMMANDS = [
  { label: "Keyboard shortcuts", get key() { return keys.keyFor("app.shortcuts"); }, run: () => showShortcuts() },
  { label: "New POS bill tab (another customer)", get key() { return keys.keyFor("app.newBill"); }, run: () => newBill() },
  { label: "Switch tab (open tabs, most recent first)", get key() { return keys.keyFor("app.tabSwitcher"); }, run: () => tabSwitcher() },
  { label: "Open POS", get key() { return keys.keyFor("app.pos"); }, run: () => open("pos") },
  { label: "New manual bill (items not in stock)", run: async () => { const t = await open("pos", {}, { fresh: true }); t && t.screen.manual && t.screen.manual(); } },
  { label: "Open Inventory", get key() { return keys.keyFor("app.inventory"); }, run: () => open("inventory") },
  { label: "Find product (quick lookup)", get key() { return keys.keyFor("app.lookup"); }, run: () => lookup() },
  { label: "Stock adjustment", get key() { return keys.keyFor("inv.adjust"); }, run: async () => { const t = await open("adjustments"); t && t.screen.create && t.screen.create(); } },
  { label: "Stock adjustments register", get key() { return keys.keyFor("app.adjustments"); }, run: () => open("adjustments") },
  { label: "New product", get key() { return keys.keyFor("inv.new"); }, run: async () => { const t = await open("inventory"); t && t.screen.newProduct && t.screen.newProduct(); } },
  { label: "Import opening stock (sheet)", run: async () => { const t = await open("inventory"); t && t.screen.importSheet && t.screen.importSheet(); } },
  { label: "Units of measure (Inventory → Packaging filter)", run: () => open("inventory") },
  { label: "Stock history (all movements)", get key() { return keys.keyFor("app.history"); }, run: () => open("history") },
  { label: "Purchases", get key() { return keys.keyFor("app.purchases"); }, run: () => open("purchases") },
  { label: "Customers", get key() { return keys.keyFor("app.customers"); }, run: () => open("customers") },
  { label: "Customer follow-up inbox", run: () => open("customers", { panel: "inbox" }) },
  { label: "Customer follow-up calendar", run: () => open("customers", { panel: "calendar" }) },
  { label: "Import purchase invoice", get key() { return keys.keyFor("purchases.import"); }, run: async () => { const t = await open("purchases"); t && t.screen.importInvoice && t.screen.importInvoice(); } },
  { label: "Enter purchase manually", run: async () => { const t = await open("purchases"); t && t.screen.manual && t.screen.manual(); } },
  { label: "Suppliers", run: async () => { const t = await open("purchases", {}); t && t.screen.show && t.screen.show("suppliers"); } },
  { label: "Purchase return to supplier", run: async () => { const t = await open("purchases"); t && t.screen.newReturn && t.screen.newReturn(); } },
  { label: "Sales history, returns & exchanges", get key() { return keys.keyFor("app.returns"); }, run: () => open("sales") },
  { label: "Reports", get key() { return keys.keyFor("app.reports"); }, run: () => open("reports") },
  { label: "Close workspace tab", get key() { return keys.keyFor("app.closeTab"); }, run: () => closeTab() },
];
const palette = $("#palette"), pInput = $("#palette-input"), pList = $("#palette-list");
let pItems = [], pAt = 0, pPrev = null;
function paletteRender() {
  const q = pInput.value.trim().toLowerCase();
  pItems = COMMANDS.filter((c) => !q || q.split(/\s+/).every((w) => c.label.toLowerCase().includes(w)));
  pAt = Math.min(pAt, Math.max(pItems.length - 1, 0));
  pList.innerHTML = pItems.map((c, i) => `<div class="pal-item${i === pAt ? " on" : ""}" data-i="${i}" role="option">${esc(c.label)}${c.key ? `<kbd>${esc(c.key)}</kbd>` : ""}</div>`).join("") || '<div class="pal-empty">No command</div>';
}
function paletteOpen() { pPrev = document.activeElement; palette.hidden = false; pInput.value = ""; pAt = 0; paletteRender(); pInput.focus(); }
function paletteClose() { palette.hidden = true; if (pPrev && pPrev.focus) pPrev.focus(); }
pInput.addEventListener("input", () => { pAt = 0; paletteRender(); });
pInput.addEventListener("keydown", (e) => {
  e.stopPropagation();
  if (e.key === "Escape") { e.preventDefault(); paletteClose(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); pAt = Math.min(pAt + 1, pItems.length - 1); paletteRender(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); pAt = Math.max(pAt - 1, 0); paletteRender(); }
  else if (e.key === "Enter") { e.preventDefault(); const c = pItems[pAt]; palette.hidden = true; if (c) c.run(); }
});
pList.addEventListener("mousedown", (e) => {
  const it = e.target.closest("[data-i]");
  if (it) { e.preventDefault(); palette.hidden = true; pItems[Number(it.dataset.i)].run(); }
});

// ------------------------------------------------------------------ quick product lookup (Ctrl+F)
function lookup() {
  const prev = document.activeElement;
  const el = h(`<div class="modal-backdrop"><div class="modal lookup" role="dialog" aria-label="Quick product lookup">
    <header><h2>Quick product lookup</h2><span class="hint">Esc returns you to where you were</span></header>
    <div class="modal-body"><input class="lk-q" placeholder="Name, generic, code or barcode" autocomplete="off"><div class="lk-res"></div></div></div></div>`);
  const q = el.querySelector(".lk-q"), res = el.querySelector(".lk-res");
  let ctrl;
  const run = async () => {
    if (ctrl) ctrl.abort();
    const term = q.value.trim();
    if (!term) { res.innerHTML = ""; return; }
    ctrl = new AbortController();
    try {
      const d = await api("/api/erp/pos/search?limit=6&q=" + encodeURIComponent(term), { signal: ctrl.signal });
      res.innerHTML = d.items.map((p) => {
        const b = p.batches[0];
        const off = p.active === false;
        return `<div class="lk-item${off ? " lk-off" : ""}"><b>${esc(p.name)}</b> <span class="muted">${esc(p.code)}${p.rack ? " · Rack " + esc(p.rack) : ""}</span>
          ${off ? '<span class="tag off">Disabled — not for sale</span>' : p.stock > 0 ? '<span class="tag ok">Active</span>' : '<span class="tag">Active · out of stock</span>'}
          <div>Stock <b>${p.stock}</b> ${esc(unitName(p.base_unit, p.stock))} · ${esc(p.stock_label)}${b ? ` · MRP ₹${money(b.pack_mrp)}${p.upp > 1 ? " / ₹" + money(b.unit_mrp) : ""} · Exp ${fmtExpShort(b.expiry)}` : " · no sellable batch"}</div></div>`;
      }).join("") || '<div class="muted">No product found</div>';
    } catch (err) { if (err.name !== "AbortError") res.textContent = err.message; }
  };
  let t;
  q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(run, 90); });
  el.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") { e.preventDefault(); el.remove(); if (prev && prev.focus) prev.focus(); } });
  el.addEventListener("mousedown", (e) => { if (e.target === el) { el.remove(); if (prev && prev.focus) prev.focus(); } });
  document.body.append(el);
  q.focus();
}

// ------------------------------------------------------------------ tab switcher (Alt+Z)
// Opens on the previous tab, most recently used first. Hold Alt and tap Z again to step down;
// releasing Alt switches (like Alt+Tab). Or type to filter, ↑↓ / 1–9 / Enter, Delete closes
// the highlighted tab, Esc cancels.
function tabSwitcher() {
  if (tabs.length < 2) { status(tabs.length ? "Only one tab is open" : "No tabs open"); return; }
  const prev = document.activeElement;
  const order = () => [...recent.map((id) => tabs.find((t) => t.id === id)).filter(Boolean), ...tabs.filter((t) => !recent.includes(t.id))];
  const label = (t) => (MODULES[t.module] || {}).label || t.module;
  const el = h(`<div class="modal-backdrop tabsw-back"><div class="palette tabsw" role="dialog" aria-modal="true" aria-label="Switch tab">
    <input class="tabsw-q" placeholder="Switch tab — type to filter · ↑↓ · 1–9 · Enter · Delete closes · Esc" autocomplete="off">
    <div class="palette-list tabsw-list" role="listbox"></div></div></div>`);
  const q = el.querySelector(".tabsw-q"), list = el.querySelector(".tabsw-list");
  let shown = [], at = 0, typed = false, closed = false;
  const altHeld = true;      // opened by Alt+Z: releasing Alt switches until the person types or clicks
  const paint = () => {
    const t = q.value.trim().toLowerCase();
    shown = order().filter((x) => !t || `${x.title} ${label(x)}`.toLowerCase().includes(t));
    at = Math.max(0, Math.min(at, shown.length - 1));
    list.innerHTML = shown.map((x, i) => `<div class="palette-item tabsw-item${i === at ? " on" : ""}${x === active ? " cur" : ""}" role="option" data-i="${i}">
      <span class="tabsw-n">${i < 9 ? i + 1 : ""}</span><span class="tabsw-t"><b>${esc(x.title)}</b>${x.dirty ? ' <span class="tag warn">unsaved</span>' : ""}</span>
      <span class="muted">${esc(label(x))}${x === active ? " · current" : ""}</span></div>`).join("") || '<div class="muted tabsw-none">No tab matches</div>';
    list.querySelector(".on")?.scrollIntoView({ block: "nearest" });
  };
  const close = () => { if (closed) return; closed = true; el.remove(); document.removeEventListener("keyup", up, true); };
  const go = (i = at) => { const t = shown[i]; close(); if (t) activate(t); else if (prev && prev.focus) prev.focus(); };
  const up = (e) => { if (e.key === "Alt" && altHeld && !typed) { e.preventDefault(); go(); } };
  el.addEventListener("keydown", async (e) => {
    e.stopPropagation();
    const name = keyName(e);
    if (keys.matches("app.tabSwitcher", name)) { e.preventDefault(); at = (at + (e.shiftKey ? -1 : 1) + shown.length) % shown.length; paint(); return; }
    if (e.key === "Escape") { e.preventDefault(); close(); if (prev && prev.focus) prev.focus(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); at = (at + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length; paint(); return; }
    if (e.key === "Enter") { e.preventDefault(); go(); return; }
    if (/^[1-9]$/.test(e.key) && !q.value) { e.preventDefault(); go(Number(e.key) - 1); return; }
    if (e.key === "Delete" || keys.matches("app.closeTab", name)) {
      e.preventDefault();
      const t = shown[at];
      if (!t) return;
      typed = true;                         // a tab was closed from here: releasing Alt no longer switches
      el.hidden = true;
      await closeTab(t);                    // asks first when the tab has unsaved work
      el.hidden = false;
      if (tabs.length < 2) { close(); return; }
      paint(); q.focus();
    }
  });
  q.addEventListener("input", () => { typed = true; at = 0; paint(); });
  list.addEventListener("click", (e) => { const o = e.target.closest("[data-i]"); if (o) go(Number(o.dataset.i)); });
  el.addEventListener("mousedown", (e) => { if (e.target === el) { close(); if (prev && prev.focus) prev.focus(); } });
  document.addEventListener("keyup", up, true);
  document.body.append(el);
  at = order()[0] === active ? 1 : 0;                            // start on the previous tab
  paint();
  q.focus();
}

// ------------------------------------------------------------------ global keys
const showShortcuts = () => openShortcuts({ status, scope: active?.module });
$("#st-shortcuts").onclick = showShortcuts;
const GLOBAL = {
  "app.shortcuts": showShortcuts, "app.palette": paletteOpen, "app.lookup": lookup,
  "app.newBill": newBill, "app.closeTab": () => closeTab(), "app.tabSwitcher": tabSwitcher,
  "app.nextTab": () => cycle(1), "app.prevTab": () => cycle(-1),
  ...Object.fromEntries(["pos", "inventory", "history", "adjustments", "purchases", "customers", "reports", "masters", "racks"].map((m) => ["app." + m, () => open(m)])),
  "app.returns": () => open("sales"),
};
function refreshHints() {
  renderKeys(); renderTabs();
  document.querySelectorAll("[data-shortcut]").forEach((el) => { el.textContent = keys.keyFor(el.dataset.shortcut); });
  document.querySelectorAll("[data-module]").forEach((el) => {
    const key = keys.keyFor("app." + (el.dataset.module === "sales" ? "returns" : el.dataset.module));
    el.title = MODULES[el.dataset.module].label + (key ? ` (${key})` : "");
  });
  $("#st-shortcuts").textContent = "Keyboard shortcuts" + (keys.keyFor("app.shortcuts") ? ` (${keys.keyFor("app.shortcuts")})` : "");
}
keys.onChange(refreshHints);
refreshHints();
// Holding these keys repeats them (closing tabs, opening bills, stepping through tabs): one at a
// time, each after the previous has finished, at a steady pace. Everything else ignores
// auto-repeat. A tab that asks before closing (unsaved bill) stops the run: its question opens
// a dialog, and while a dialog is open no shortcut — held or not — reaches the tabs.
const REPEATABLE = new Set(["app.closeTab", "app.newBill", "app.nextTab", "app.prevTab"]);
const REPEAT_MS = 140;
let repeatBusy = false, repeatAt = 0;
document.addEventListener("keydown", (e) => {
  if (e.isComposing || e.getModifierState("AltGraph") || !palette.hidden || document.querySelector(".modal-backdrop, .ctx-menu")) return;
  const name = keyName(e);
  const action = keys.lookup(active?.module, name);
  const tabKey = /^Alt\+[1-9]$/.test(name);
  if (!action && !tabKey && name !== "F1") return;
  e.preventDefault(); e.stopPropagation();
  if (REPEATABLE.has(action) && GLOBAL[action]) {
    const now = performance.now();
    if (repeatBusy || (e.repeat && now - repeatAt < REPEAT_MS)) return;
    // a held Alt+N stops at nine bills (what Alt+1…9 can reach); a deliberate single press still opens more
    if (e.repeat && action === "app.newBill" && tabs.filter((t) => t.module === "pos").length >= 9) {
      status("9 bills open — release the keys (another press opens one more)", "warn"); return;
    }
    repeatBusy = true; repeatAt = now;
    Promise.resolve(GLOBAL[action]()).finally(() => { repeatBusy = false; });
    return;
  }
  if (e.repeat) return;
  if (active?.screen.prepareShortcut && !active.screen.prepareShortcut()) return;
  if (name === "F1") { showShortcuts(); return; }
  if (tabKey) { const t = tabs[Number(name.slice(-1)) - 1]; if (t) activate(t); return; }
  if (GLOBAL[action]) GLOBAL[action]();
  else if (active?.screen.onKey) active.screen.onKey(e, name);
}, true);
// Unmodified navigation remains owned by the focused screen.
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || !palette.hidden || document.querySelector(".modal-backdrop")) return;
  if (active?.screen.onKey?.(e, keyName(e))) e.preventDefault();
});
window.addEventListener("beforeunload", (e) => { if (tabs.some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ start
(async function start() {
  let saved = null, recovered = null;
  try { saved = JSON.parse(sessionStorage.getItem("erp:tabs") || "null"); } catch { saved = null; }
  if (!(saved && saved.tabs && saved.tabs.length)) {
    // a fresh browser session (reboot, crash, new login): the last snapshot of this counter
    try {
      const snap = await api("/api/erp/workspace?terminal=" + encodeURIComponent(TERMINAL));
      const d = snap && snap.data;
      if (d && Array.isArray(d.tabs) && d.tabs.length) {
        for (const [id, state] of Object.entries(d.states || {})) {
          try { sessionStorage.setItem("erp:tab:" + id, JSON.stringify(state)); } catch { /* ignore */ }
        }
        saved = { tabs: d.tabs.filter((t) => MODULES[t.module] && CAN_OPEN.has(t.module)), active: d.active };
        recovered = snap;
      }
    } catch { /* no snapshot: start fresh */ }
  }
  restoring = true;
  const [, , pathMod, item] = location.pathname.split("/");
  const mod = BOOT.initial_module || pathMod;
  if (saved && saved.tabs && saved.tabs.length) {
    for (const t of saved.tabs) await open(t.module, t.params, { id: t.id, focus: false });
    const want = tabs.find((t) => t.id === saved.active);
    if (want) activate(want);
  }
  if (mod && MODULES[mod] && !MODULES[mod].href && !tabs.some((t) => t.module === mod)) {
    await open(mod, mod === "ledger" && item ? { item: Number(item) } : {});
  }
  if (BOOT.initial_module && tabs.some((t) => t.module === BOOT.initial_module)) activate(tabs.find((t) => t.module === BOOT.initial_module));
  if (!tabs.length) {
    const first = CAN_OPEN.has("pos") ? "pos" : CAN_OPEN.has("inventory") ? "inventory" : null;
    if (first) await open(first);
  }
  restoring = false;
  if (recovered) {
    const bills = (recovered.data.tabs || []).filter((t) => t.module === "pos" && recovered.data.states?.[t.id]?.lines?.length).length;
    const when = new Date(recovered.saved_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    status(`Restored your open work from ${when}: ${saved.tabs.length} tab${saved.tabs.length === 1 ? "" : "s"}`
      + (bills ? `, ${bills} unfinished bill${bills === 1 ? "" : "s"}` : "") + " — continue where you left off", "ok");
  }
  scheduleFlush(2000);
  if (!recovered && !store.get("seenHelp", false)) {
    status("Keyboard ERP: Ctrl+K commands · Alt+P POS · Alt+I Inventory · Ctrl+F product lookup · Alt+1…9 switch tabs");
    store.set("seenHelp", true);
  }
})();
