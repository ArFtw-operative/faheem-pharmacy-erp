// Keyboard-first spreadsheet grid: sticky header, row selection, resizable
// columns (widths remembered), ↑↓ PgUp PgDn Home End, Enter = default action,
// context menu. Rendering is plain HTML strings — fast for hundreds of rows.
// With ``multi: true`` rows can also be marked for bulk actions:
// Shift+↑↓ / Shift+Home/End extend a range, Space toggles a row, Ctrl+A marks
// all, Esc clears; Shift+click and Ctrl+click with the mouse.
import { esc, h, store } from "erp/core";

export class Grid {
  constructor({ columns, storageKey = "", empty = "No rows", onSelect, onActivate, contextMenu, rowClass, onNearEnd, label = "",
    multi = false, canMark, onMarks }) {
    this.columns = columns;
    this.storageKey = storageKey;
    this.empty = empty;
    this.onSelect = onSelect;
    this.onActivate = onActivate;
    this.contextMenu = contextMenu;
    this.rowClass = rowClass;
    this.onNearEnd = onNearEnd;
    this.rows = [];
    this.index = -1;
    this.multi = multi;
    this.canMark = canMark || (() => true);
    this.onMarks = onMarks;
    this.marked = new Set();      // marked row indices
    this.anchor = null;           // where a Shift range started
    this.base = null;             // marks that existed before the range
    const widths = storageKey ? store.get("cols:" + storageKey, {}) : {};
    this.el = h(`<div class="grid" tabindex="0" role="grid" aria-label="${esc(label)}">
      <table><colgroup>${columns.map((c) => `<col data-col="${c.key}" style="width:${widths[c.key] || c.width || 100}px">`).join("")}</colgroup>
      <thead><tr>${columns.map((c) => `<th class="${c.align || ""}" data-col="${c.key}" title="${esc(c.title || c.label)}">${esc(c.label)}<span class="rs" data-resize="${c.key}"></span></th>`).join("")}</tr></thead>
      <tbody></tbody></table><div class="grid-empty" hidden></div></div>`);
    this.body = this.el.querySelector("tbody");
    this.emptyEl = this.el.querySelector(".grid-empty");
    this.el.addEventListener("keydown", (e) => this._key(e));
    this.body.addEventListener("mousedown", (e) => {
      const tr = e.target.closest("tr[data-i]");
      if (!tr) return;
      const i = Number(tr.dataset.i);
      if (this.multi && e.shiftKey) { e.preventDefault(); this._range(i); return; }
      if (this.multi && (e.ctrlKey || e.metaKey)) { e.preventDefault(); this.select(i); this.toggleMark(i); return; }
      this.anchor = null;
      this.select(i);
    });
    this.body.addEventListener("dblclick", (e) => {
      const tr = e.target.closest("tr[data-i]");
      if (tr && this.onActivate) this.onActivate(this.rows[Number(tr.dataset.i)], Number(tr.dataset.i));
    });
    this.body.addEventListener("contextmenu", (e) => {
      const tr = e.target.closest("tr[data-i]");
      if (!tr || !this.contextMenu) return;
      e.preventDefault();
      this.select(Number(tr.dataset.i));
      this._menu(e.clientX, e.clientY);
    });
    this.el.addEventListener("scroll", () => {
      if (this.onNearEnd && this.el.scrollTop + this.el.clientHeight > this.el.scrollHeight - 200) this.onNearEnd();
    });
    this._resizable(widths);
  }

  _resizable(widths) {
    this.el.querySelector("thead").addEventListener("mousedown", (e) => {
      const key = e.target.dataset && e.target.dataset.resize;
      if (!key) return;
      e.preventDefault();
      const col = this.el.querySelector(`col[data-col="${key}"]`);
      const start = e.clientX, w0 = col.getBoundingClientRect().width || parseInt(col.style.width, 10);
      const move = (ev) => { col.style.width = Math.max(40, w0 + ev.clientX - start) + "px"; };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        if (this.storageKey) { widths[key] = parseInt(col.style.width, 10); store.set("cols:" + this.storageKey, widths); }
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  }

  setColumns(columns) {
    if (!columns.length) return;
    const widths = this.storageKey ? store.get("cols:" + this.storageKey, {}) : {};
    this.columns = columns;
    this.el.querySelector("colgroup").innerHTML = columns.map(c => `<col data-col="${c.key}" style="width:${widths[c.key] || c.width || 100}px">`).join("");
    this.el.querySelector("thead").innerHTML = `<tr>${columns.map(c => `<th class="${c.align || ""}" data-col="${c.key}" title="${esc(c.title || c.label)}">${esc(c.label)}<span class="rs" data-resize="${c.key}"></span></th>`).join("")}</tr>`;
    this.body.innerHTML = this.rows.map((r, i) => this._rowHtml(r, i)).join("");
  }

  resetColumns() {
    if (this.storageKey) store.set("cols:" + this.storageKey, {});
    this.columns.forEach((c) => { this.el.querySelector(`col[data-col="${c.key}"]`).style.width = (c.width || 100) + "px"; });
  }

  _rowHtml(row, i) {
    const cls = ["" + (i === this.index ? "sel" : ""), this.marked.has(i) ? "mark" : "", this.rowClass ? this.rowClass(row) || "" : ""].join(" ").trim();
    return `<tr data-i="${i}" class="${cls}">${this.columns.map((c) => {
      const v = c.render ? c.render(row) : esc(row[c.key] ?? "");
      const tdc = [c.align || "", c.cellClass ? c.cellClass(row) || "" : ""].join(" ").trim();
      return `<td class="${tdc}">${v}</td>`;
    }).join("")}</tr>`;
  }

  setRows(rows, { keep = false } = {}) {
    const prevId = keep && this.rows[this.index] ? this.rows[this.index].id : undefined;
    const markedIds = keep ? new Set([...this.marked].map((i) => this.rows[i] && this.rows[i].id)) : new Set();
    this.rows = rows;
    this.marked = new Set();
    rows.forEach((r, i) => { if (markedIds.has(r.id) && this.canMark(r)) this.marked.add(i); });
    this.anchor = null;
    this.body.innerHTML = rows.map((r, i) => this._rowHtml(r, i)).join("");
    this.emptyEl.hidden = rows.length > 0;
    this.emptyEl.textContent = this.empty;
    let i = prevId !== undefined ? rows.findIndex((r) => r.id === prevId) : -1;
    if (i < 0) i = rows.length ? Math.min(Math.max(this.index, 0), rows.length - 1) : -1;
    this.index = -1;
    if (i >= 0) this.select(i, { silent: keep && prevId !== undefined && rows[i] && rows[i].id === prevId });
    else if (this.onSelect) this.onSelect(null, -1);
    this._marksChanged();
  }

  // ---------------------------------------------------------------- marking (multi)
  get markedRows() { return [...this.marked].sort((a, b) => a - b).map((i) => this.rows[i]).filter(Boolean); }

  _paint(i) {
    const tr = this.body.querySelector(`tr[data-i="${i}"]`);
    if (tr) tr.classList.toggle("mark", this.marked.has(i));
  }

  _marksChanged() { if (this.multi && this.onMarks) this.onMarks(this.markedRows); }

  setMarks(indices) {
    const old = this.marked;
    this.marked = new Set(indices.filter((i) => this.rows[i] && this.canMark(this.rows[i])));
    for (const i of new Set([...old, ...this.marked])) this._paint(i);
    this._marksChanged();
  }

  toggleMark(i = this.index) {
    if (!this.multi || !this.rows[i] || !this.canMark(this.rows[i])) return;
    const next = new Set(this.marked);
    next.has(i) ? next.delete(i) : next.add(i);
    this.anchor = null;
    this.setMarks([...next]);
  }

  clearMarks() { if (this.marked.size) { this.anchor = null; this.setMarks([]); return true; } return false; }

  markAll() { this.anchor = null; this.setMarks(this.rows.map((_, i) => i)); }

  _range(to) {
    if (this.anchor === null) { this.anchor = this.index < 0 ? to : this.index; this.base = new Set(this.marked); }
    const [a, b] = [Math.min(this.anchor, to), Math.max(this.anchor, to)];
    const range = [];
    for (let i = a; i <= b; i++) range.push(i);
    this.select(to);
    this.setMarks([...this.base, ...range]);
  }

  appendRows(more) {
    const start = this.rows.length;
    this.rows = this.rows.concat(more);
    this.body.insertAdjacentHTML("beforeend", more.map((r, k) => this._rowHtml(r, start + k)).join(""));
    this.emptyEl.hidden = this.rows.length > 0;
  }

  refreshRow(i) {
    const tr = this.body.querySelector(`tr[data-i="${i}"]`);
    if (tr) tr.outerHTML = this._rowHtml(this.rows[i], i);
  }

  get selected() { return this.rows[this.index] || null; }

  select(i, { silent = false } = {}) {
    if (!this.rows.length) return;
    i = Math.max(0, Math.min(this.rows.length - 1, i));
    const old = this.body.querySelector("tr.sel");
    if (old) old.classList.remove("sel");
    this.index = i;
    const tr = this.body.querySelector(`tr[data-i="${i}"]`);
    if (tr) {
      tr.classList.add("sel");
      const head = this.el.querySelector("thead").offsetHeight;
      if (tr.offsetTop - head < this.el.scrollTop) this.el.scrollTop = tr.offsetTop - head;
      else if (tr.offsetTop + tr.offsetHeight > this.el.scrollTop + this.el.clientHeight) this.el.scrollTop = tr.offsetTop + tr.offsetHeight - this.el.clientHeight;
    }
    if (!silent && this.onSelect) this.onSelect(this.rows[i], i);
    if (this.onNearEnd && i >= this.rows.length - 5) this.onNearEnd();
  }

  focus() { this.el.focus({ preventScroll: true }); if (this.index < 0 && this.rows.length) this.select(0); }

  _page() { return Math.max(1, Math.floor(this.el.clientHeight / 30) - 1); }

  _key(e) {
    if (e.target !== this.el) return;
    const k = e.key;
    const moves = { ArrowDown: 1, ArrowUp: -1, PageDown: this._page(), PageUp: -this._page() };
    if (this.multi && e.shiftKey && !e.altKey && !e.ctrlKey && (k in moves || k === "Home" || k === "End")) {
      e.preventDefault();
      const from = this.index < 0 ? 0 : this.index;
      const to = k === "Home" ? 0 : k === "End" ? this.rows.length - 1 : Math.max(0, Math.min(this.rows.length - 1, from + moves[k]));
      this._range(to);
      return;
    }
    if (this.multi && k === " " && !e.ctrlKey && !e.altKey) { e.preventDefault(); this.toggleMark(); return; }
    if (this.multi && (e.ctrlKey || e.metaKey) && (k === "a" || k === "A")) { e.preventDefault(); this.markAll(); return; }
    if (this.multi && k === "Escape" && this.marked.size) { e.preventDefault(); e.stopPropagation(); this.clearMarks(); return; }
    if (k in moves || k === "Home" || k === "End") this.anchor = null;
    if (k in moves && !e.altKey && !e.ctrlKey) { e.preventDefault(); this.select((this.index < 0 ? 0 : this.index) + moves[k]); }
    else if (k === "Home" && !e.ctrlKey) { e.preventDefault(); this.select(0); }
    else if (k === "End" && !e.ctrlKey) { e.preventDefault(); this.select(this.rows.length - 1); }
    else if (k === "Enter" && !e.shiftKey && this.selected && this.onActivate) { e.preventDefault(); this.onActivate(this.selected, this.index); }
    else if ((k === "ContextMenu" || (k === "F10" && e.shiftKey)) && this.contextMenu && this.selected) {
      e.preventDefault();
      const tr = this.body.querySelector("tr.sel").getBoundingClientRect();
      this._menu(tr.left + 40, tr.bottom);
    }
  }

  _menu(x, y) {
    document.querySelectorAll(".ctx-menu").forEach((m) => m.remove());
    const items = this.contextMenu(this.selected) || [];
    if (!items.length) return;
    const menu = h(`<div class="ctx-menu" role="menu" tabindex="-1">${items.map((it, i) =>
      `<button type="button" role="menuitem" data-i="${i}">${esc(it.label)}${it.key ? `<kbd>${esc(it.key)}</kbd>` : ""}</button>`).join("")}</div>`);
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    const close = () => { menu.remove(); document.removeEventListener("mousedown", outside, true); this.focus(); };
    const outside = (ev) => { if (!menu.contains(ev.target)) close(); };
    menu.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (b) { close(); items[Number(b.dataset.i)].action(); }
    });
    menu.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      const btns = Array.from(menu.querySelectorAll("button"));
      const at = btns.indexOf(document.activeElement);
      if (ev.key === "ArrowDown") { ev.preventDefault(); btns[(at + 1) % btns.length].focus(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); btns[(at - 1 + btns.length) % btns.length].focus(); }
      else if (ev.key === "Escape") { ev.preventDefault(); close(); }
    });
    document.body.append(menu);
    document.addEventListener("mousedown", outside, true);
    menu.querySelector("button").focus();
  }
}
