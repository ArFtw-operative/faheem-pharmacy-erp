"""Keyboard shortcuts: the action registry, browser-safety rules and per-user overrides.

Every command in the ERP workspace is an *action* with a default key. Users
may assign a different key; the result is validated here (the browser applies
the same rules for instant feedback) and stored per user, so an operator's
shortcuts follow them to any counter PC.

A key is refused when:
* the browser never hands it to a web page (Ctrl+T, Ctrl+W, Ctrl+Tab, F11 …);
* it is needed for typing or grid navigation (plain letters, Enter, arrows …);
* it is already used by another action in the same screen or globally.
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.models import Setting, User

# scope: "global" works everywhere; a module scope only on that screen
ACTIONS: list[dict] = [
    # ---- everywhere
    {"id": "app.shortcuts", "scope": "global", "group": "Everywhere", "label": "Keyboard shortcuts (this window)", "key": "Ctrl+/"},
    {"id": "app.palette", "scope": "global", "group": "Everywhere", "label": "Command palette", "key": "Ctrl+K"},
    {"id": "app.lookup", "scope": "global", "group": "Everywhere", "label": "Quick product lookup", "key": "Ctrl+F"},
    {"id": "app.newBill", "scope": "global", "group": "Everywhere", "label": "New POS bill tab (next customer)", "key": "Alt+N"},
    {"id": "app.closeTab", "scope": "global", "group": "Everywhere", "label": "Close tab / bill", "key": "Alt+W"},
    {"id": "app.nextTab", "scope": "global", "group": "Everywhere", "label": "Next tab", "key": "Alt+PageDown"},
    {"id": "app.tabSwitcher", "scope": "global", "group": "Everywhere", "label": "Tab switcher: open tabs, most recent first (hold Alt, tap Z to step, release to switch)", "key": "Alt+Z"},
    {"id": "app.prevTab", "scope": "global", "group": "Everywhere", "label": "Previous tab", "key": "Alt+PageUp"},
    {"id": "app.pos", "scope": "global", "group": "Everywhere", "label": "Go to POS", "key": "Alt+P"},
    {"id": "app.inventory", "scope": "global", "group": "Everywhere", "label": "Go to Inventory", "key": "Alt+I"},
    {"id": "app.purchases", "scope": "global", "group": "Everywhere", "label": "Go to Purchases", "key": "Alt+U"},
    {"id": "app.returns", "scope": "global", "group": "Everywhere", "label": "Go to Sales history & returns", "key": "Alt+Shift+R"},
    {"id": "app.history", "scope": "global", "group": "Everywhere", "label": "Go to Stock History", "key": "Alt+Shift+H"},
    {"id": "app.adjustments", "scope": "global", "group": "Everywhere", "label": "Go to Stock adjustments", "key": "Alt+Shift+A"},
    {"id": "app.customers", "scope": "global", "group": "Everywhere", "label": "Go to Customers", "key": "Alt+Shift+C"},
    {"id": "app.masters", "scope": "global", "group": "Everywhere", "label": "Go to Categories & Forms", "key": "Alt+M"},
    {"id": "app.racks", "scope": "global", "group": "Everywhere", "label": "Go to Racks", "key": "Alt+Shift+K"},
    {"id": "app.reports", "scope": "global", "group": "Everywhere", "label": "Go to Reports", "key": "Alt+R"},
    # ---- POS
    {"id": "pos.search", "scope": "pos", "group": "POS", "label": "Item search", "key": "F2", "bar": 1},
    {"id": "pos.qty", "scope": "pos", "group": "POS", "label": "Edit quantity", "key": "F4", "bar": 2},
    {"id": "pos.batch", "scope": "pos", "group": "POS", "label": "Choose batch", "key": "F5", "bar": 3},
    {"id": "pos.remove", "scope": "pos", "group": "POS", "label": "Remove line", "key": "F6", "bar": 4},
    {"id": "pos.itemDisc", "scope": "pos", "group": "POS", "label": "Item discount %", "key": "F7", "bar": 5},
    {"id": "pos.billDisc", "scope": "pos", "group": "POS", "label": "Bill discount %", "key": "Shift+F7", "bar": 6},
    {"id": "pos.upi", "scope": "pos", "group": "POS", "label": "Pay by UPI", "key": "F8", "bar": 8},
    {"id": "pos.cash", "scope": "pos", "group": "POS", "label": "Pay by cash", "key": "F9", "bar": 7},
    {"id": "pos.card", "scope": "pos", "group": "POS", "label": "Pay by card", "key": "F10", "bar": 9},
    {"id": "pos.split", "scope": "pos", "group": "POS", "label": "Split payment", "key": "Alt+S", "bar": 10},
    {"id": "pos.udhaar", "scope": "pos", "group": "POS", "label": "Udhaar — customer pays later (customer, due date and reminder required)", "key": "Shift+F9", "bar": 10},
    {"id": "pos.save", "scope": "pos", "group": "POS", "label": "Complete sale (summary, then invoice Y/N)", "key": "F12", "bar": 11},
    {"id": "pos.hold", "scope": "pos", "group": "POS", "label": "Hold bill", "key": "Ctrl+H", "bar": 12},
    {"id": "pos.resume", "scope": "pos", "group": "POS", "label": "Resume held bill", "key": "Ctrl+R", "bar": 13},
    {"id": "pos.customer", "scope": "pos", "group": "POS", "label": "Customer search (#)", "key": "Alt+C", "bar": 14},
    {"id": "pos.walkin", "scope": "pos", "group": "POS", "label": "Back to walk-in customer", "key": "Alt+Backspace"},
    {"id": "pos.manual", "scope": "pos", "group": "POS", "label": "Manual bill on/off (any item, stock not checked or changed)", "key": "Alt+L", "bar": 16},
    {"id": "pos.followup", "scope": "pos", "group": "POS", "label": "Follow up with this customer", "key": "Alt+O", "bar": 17},
    {"id": "pos.saleType", "scope": "pos", "group": "POS", "label": "Switch sale type (Walk-in / Home delivery)", "key": "Alt+Y", "bar": 15},
    # ---- Inventory
    {"id": "inv.search", "scope": "inventory", "group": "Inventory", "label": "Search", "key": "F2", "bar": 1},
    {"id": "inv.new", "scope": "inventory", "group": "Inventory", "label": "New product", "key": "F3", "bar": 2},
    {"id": "inv.adjust", "scope": "inventory", "group": "Inventory", "label": "Stock adjustment", "key": "F5", "bar": 3},
    {"id": "inv.ledger", "scope": "inventory", "group": "Inventory", "label": "Stock ledger", "key": "F6", "bar": 4},
    {"id": "inv.edit", "scope": "inventory", "group": "Inventory", "label": "Edit product", "key": "Ctrl+E", "bar": 5},
    {"id": "inv.columns", "scope": "inventory", "group": "Inventory", "label": "Choose inventory columns", "key": "Alt+C", "bar": 7},
    {"id": "inv.export", "scope": "inventory", "group": "Inventory", "label": "Export to Excel", "key": "F9", "bar": 6},
    # ---- Reports (browser-friendly alternatives to Alt+D / Ctrl+Shift+C)
    {"id": "reports.period", "scope": "reports", "group": "Reports", "label": "Focus period / date", "key": "Alt+T", "bar": 1},
    {"id": "reports.generate", "scope": "reports", "group": "Reports", "label": "Generate report", "key": "Alt+G", "bar": 2},
    {"id": "reports.columns", "scope": "reports", "group": "Reports", "label": "Choose report columns", "key": "Alt+C", "bar": 3},
    {"id": "reports.print", "scope": "reports", "group": "Reports", "label": "Print generated report", "key": "Ctrl+P", "bar": 4},
    {"id": "reports.export", "scope": "reports", "group": "Reports", "label": "Export report to Excel", "key": "Ctrl+E", "bar": 5},
    {"id": "reports.view", "scope": "reports", "group": "Reports", "label": "Switch grid / document view", "key": "F6", "bar": 7},
    {"id": "reports.refresh", "scope": "reports", "group": "Reports", "label": "Refresh generated report", "key": "F5", "bar": 6},
    # ---- Racks
    {"id": "racks.search", "scope": "racks", "group": "Racks", "label": "Search racks", "key": "F2", "bar": 1},
    {"id": "racks.new", "scope": "racks", "group": "Racks", "label": "New rack", "key": "F3", "bar": 2},
    {"id": "racks.addProducts", "scope": "racks", "group": "Racks", "label": "Add products to the rack", "key": "F4", "bar": 3},
    {"id": "racks.refresh", "scope": "racks", "group": "Racks", "label": "Refresh", "key": "F5", "bar": 4},
    {"id": "racks.tab", "scope": "racks", "group": "Racks", "label": "Next tab (Products / Boxes / History / Snapshots / Settings)", "key": "F6", "bar": 5},
    {"id": "racks.move", "scope": "racks", "group": "Racks", "label": "Move selected products", "key": "F8", "bar": 6},
    {"id": "racks.edit", "scope": "racks", "group": "Racks", "label": "Edit rack", "key": "Ctrl+E", "bar": 7},
    {"id": "racks.newBox", "scope": "racks", "group": "Racks", "label": "New box", "key": "Alt+B"},
    # ---- Categories & Forms
    {"id": "masters.search", "scope": "masters", "group": "Categories & Forms", "label": "Search", "key": "F2", "bar": 1},
    {"id": "masters.refresh", "scope": "masters", "group": "Categories & Forms", "label": "Refresh", "key": "F5", "bar": 2},
    {"id": "masters.panel", "scope": "masters", "group": "Categories & Forms", "label": "Switch list (Categories / Item forms)", "key": "F6", "bar": 3},
    {"id": "masters.new", "scope": "masters", "group": "Categories & Forms", "label": "New category / item form", "key": "F3", "bar": 4},
    {"id": "masters.merge", "scope": "masters", "group": "Categories & Forms", "label": "Merge category into another", "key": "F4", "bar": 5},
    {"id": "masters.up", "scope": "masters", "group": "Categories & Forms", "label": "Move category up", "key": "Alt+ArrowUp"},
    {"id": "masters.down", "scope": "masters", "group": "Categories & Forms", "label": "Move category down", "key": "Alt+ArrowDown"},
    # ---- Sales history & returns
    {"id": "sales.search", "scope": "sales", "group": "Sales history", "label": "Search bill, customer, product or batch", "key": "F2", "bar": 1},
    {"id": "sales.period", "scope": "sales", "group": "Sales history", "label": "Focus date range", "key": "Alt+T", "bar": 2},
    {"id": "sales.refresh", "scope": "sales", "group": "Sales history", "label": "Refresh", "key": "F5", "bar": 3},
    {"id": "sales.return", "scope": "sales", "group": "Sales history", "label": "Return / refund items of the bill", "key": "F6", "bar": 4},
    {"id": "sales.exchange", "scope": "sales", "group": "Sales history", "label": "Exchange: return items, then a new bill for the customer", "key": "F7", "bar": 5},
    {"id": "sales.edit", "scope": "sales", "group": "Sales history", "label": "Edit the bill (opens it in a POS tab; stock re-posted on save)", "key": "F4", "bar": 5},
    {"id": "sales.reprint", "scope": "sales", "group": "Sales history", "label": "Reprint the bill", "key": "Ctrl+P", "bar": 6},
    {"id": "sales.void", "scope": "sales", "group": "Sales history", "label": "Void the bill (returns its stock)", "key": "Alt+X", "bar": 7},
    {"id": "settings.refresh", "scope": "settings", "group": "Settings", "label": "Refresh (WhatsApp status, activity)", "key": "F5", "bar": 1},
    {"id": "sales.whatsapp", "scope": "sales", "group": "Sales history", "label": "WhatsApp / resend the bill's invoice to the customer", "key": "F9", "bar": 8},
    # ---- Customers (directory, inbox, calendar)
    {"id": "cust.search", "scope": "customers", "group": "Customers", "label": "Search name, mobile, ID or area", "key": "F2", "bar": 1},
    {"id": "cust.newSale", "scope": "customers", "group": "Customers", "label": "New sale for the customer", "key": "F3", "bar": 2},
    {"id": "cust.followup", "scope": "customers", "group": "Customers", "label": "Follow up with the customer", "key": "F4", "bar": 3},
    {"id": "cust.refresh", "scope": "customers", "group": "Customers", "label": "Refresh", "key": "F5", "bar": 4},
    {"id": "cust.panel", "scope": "customers", "group": "Customers", "label": "Switch Directory / Inbox / Calendar", "key": "F6", "bar": 5},
    {"id": "cust.note", "scope": "customers", "group": "Customers", "label": "Add a note", "key": "F7", "bar": 6},
    {"id": "cust.edit", "scope": "customers", "group": "Customers", "label": "Edit customer details", "key": "Ctrl+E", "bar": 7},
    # ---- Stock adjustments
    {"id": "adj.search", "scope": "adjustments", "group": "Stock adjustments", "label": "Search product, batch, ADJ no. or reason", "key": "F2", "bar": 1},
    {"id": "adj.new", "scope": "adjustments", "group": "Stock adjustments", "label": "New stock adjustment", "key": "F3", "bar": 2},
    {"id": "adj.period", "scope": "adjustments", "group": "Stock adjustments", "label": "Focus date range", "key": "Alt+T", "bar": 3},
    {"id": "adj.refresh", "scope": "adjustments", "group": "Stock adjustments", "label": "Refresh", "key": "F5", "bar": 4},
    {"id": "adj.reverse", "scope": "adjustments", "group": "Stock adjustments", "label": "Reverse the adjustment (a new, linked document)", "key": "Alt+X", "bar": 5},
    # ---- Stock history (all products)
    {"id": "history.search", "scope": "history", "group": "Stock history", "label": "Search product / batch / reference", "key": "F2", "bar": 1},
    {"id": "history.period", "scope": "history", "group": "Stock history", "label": "Focus date range", "key": "Alt+T", "bar": 2},
    {"id": "history.type", "scope": "history", "group": "Stock history", "label": "Focus movement type", "key": "F6", "bar": 3},
    {"id": "history.refresh", "scope": "history", "group": "Stock history", "label": "Refresh", "key": "F5", "bar": 4},
    {"id": "history.export", "scope": "history", "group": "Stock history", "label": "Export to Excel (CSV)", "key": "F9", "bar": 5},
    {"id": "inv.history", "scope": "inventory", "group": "Inventory", "label": "Stock history of the product", "key": "F7", "bar": 8},
    {"id": "inv.moveRack", "scope": "inventory", "group": "Inventory", "label": "Move selected products to a rack / box", "key": "F8", "bar": 9},
    {"id": "inv.category", "scope": "inventory", "group": "Inventory", "label": "Change category of selected products", "key": "Alt+G"},
    # ---- Purchases (register, suppliers, returns)
    {"id": "purchases.search", "scope": "purchases", "group": "Purchases", "label": "Search", "key": "F2", "bar": 1},
    {"id": "purchases.import", "scope": "purchases", "group": "Purchases", "label": "Import supplier invoice (CSV / Excel / PDF)", "key": "F3", "bar": 2},
    {"id": "purchases.manual", "scope": "purchases", "group": "Purchases", "label": "Enter purchase manually", "key": "F4", "bar": 3},
    {"id": "purchases.refresh", "scope": "purchases", "group": "Purchases", "label": "Refresh", "key": "F5", "bar": 4},
    {"id": "purchases.panel", "scope": "purchases", "group": "Purchases", "label": "Switch list (Register / Suppliers / Returns)", "key": "F6", "bar": 5},
    {"id": "purchases.supplier", "scope": "purchases", "group": "Purchases", "label": "New supplier", "key": "F7", "bar": 6},
    {"id": "purchases.return", "scope": "purchases", "group": "Purchases", "label": "Purchase return to supplier", "key": "F8", "bar": 7},
    {"id": "purchases.delete", "scope": "purchases", "group": "Purchases", "label": "Delete unreceived draft", "key": "Alt+Delete", "bar": 8},
    {"id": "purchases.cancel", "scope": "purchases", "group": "Purchases", "label": "Cancel draft", "key": "Alt+X"},
    # ---- Purchase document (import review)
    {"id": "purchase.filter", "scope": "purchase", "group": "Purchase document", "label": "Filter lines (find, category, rack, form)", "key": "Alt+S"},
    {"id": "purchase.rack", "scope": "purchase", "group": "Purchase document", "label": "Set rack / box for selected lines", "key": "Alt+L"},
    {"id": "purchase.rackSuggested", "scope": "purchase", "group": "Purchase document", "label": "Accept suggested racks (selected or all lines)", "key": "Alt+Shift+L"},
    {"id": "purchase.header", "scope": "purchase", "group": "Purchase document", "label": "Invoice header (supplier, number, date, total)", "key": "F2", "bar": 1},
    {"id": "purchase.add", "scope": "purchase", "group": "Purchase document", "label": "Add line", "key": "F3", "bar": 2},
    {"id": "purchase.product", "scope": "purchase", "group": "Purchase document", "label": "Match product for the line", "key": "F4", "bar": 3},
    {"id": "purchase.newProduct", "scope": "purchase", "group": "Purchase document", "label": "Create as new product", "key": "Shift+F4", "bar": 4},
    {"id": "purchase.refresh", "scope": "purchase", "group": "Purchase document", "label": "Refresh", "key": "F5"},
    {"id": "purchase.accept", "scope": "purchase", "group": "Purchase document", "label": "Accept the line's warnings", "key": "F6", "bar": 5},
    {"id": "purchase.next", "scope": "purchase", "group": "Purchase document", "label": "Next line needing review", "key": "F8", "bar": 6},
    {"id": "purchase.post", "scope": "purchase", "group": "Purchase document", "label": "Post to stock", "key": "F12", "bar": 7},
    {"id": "purchase.columns", "scope": "purchase", "group": "Purchase document", "label": "Columns: what each supplier column means (remembered per supplier)", "key": "F9", "bar": 8},
    {"id": "purchase.gst", "scope": "purchase", "group": "Purchase document", "label": "GST: per-slab table, CGST/SGST or IGST, what to fix", "key": "F10", "bar": 9},
    {"id": "purchase.source", "scope": "purchase", "group": "Purchase document", "label": "Open the supplier's file", "key": "Alt+O"},
    {"id": "purchase.cancel", "scope": "purchase", "group": "Purchase document", "label": "Cancel draft", "key": "Alt+X"},
    {"id": "purchase.delete", "scope": "purchase", "group": "Purchase document", "label": "Delete unreceived draft", "key": "Alt+Delete"},
    {"id": "purchase.removeLine", "scope": "purchase", "group": "Purchase document", "label": "Remove unreceived line", "key": "Ctrl+Delete"},
    {"id": "purchase.category", "scope": "purchase", "group": "Purchase document", "label": "Change category only (selected lines, one click)", "key": "F7", "bar": 10},
    {"id": "purchase.rollback", "scope": "purchase", "group": "Purchase document", "label": "Roll back posted lines to draft (stock taken out again)", "key": "Alt+B"},
    {"id": "purchase.packaging", "scope": "purchase", "group": "Purchase document", "label": "Correct packing: what one invoice Qty is (saved for invoice, product or supplier)", "key": "Alt+K"},
    {"id": "purchase.inspect", "scope": "purchase", "group": "Purchase document", "label": "Match inspector: why this product and pack", "key": "Alt+J"},
    {"id": "purchase.inbox", "scope": "purchase", "group": "Purchase document", "label": "Show only lines needing attention / all lines", "key": "Alt+V"},
    # ---- Ledger
    {"id": "ledger.refresh", "scope": "ledger", "group": "Stock ledger", "label": "Refresh", "key": "F5", "bar": 1},
]
ACTION_IDS = {a["id"] for a in ACTIONS}

# Keys that keep their meaning everywhere and cannot be reassigned (shown read-only).
FIXED = [
    ("F1", "Keyboard shortcuts (always available)"), ("Enter", "Confirm / add / next step"), ("Esc", "Close list or dialog, cancel an edit"),
    ("↑ ↓", "Move in lists and grids"), ("PgUp PgDn Home End", "Page / first / last row"),
    ("Tab", "Next field (Qty → Disc % in the bill)"), ("Alt+1 … Alt+9", "Switch to tab 1–9"),
    ("Delete", "Remove the selected bill line (asks first)"), ("+ / −", "Change quantity of the selected line"),
    ("1s · 2s+3", "Qty shorthand: one strip · two strips and three units"), ("#", "In the item box: jumps to the Customer field"),
    ("Shift+F10 / Menu key", "Row context menu"),
    # lists that can mark several rows (Inventory, Purchases, Racks, Categories & Forms …)
    ("Space", "Mark / unmark the selected row"), ("Shift+↑ ↓", "Mark a range of rows"),
    ("Ctrl+A", "Mark every row shown"), ("Ctrl+click", "Mark / unmark the clicked row"),
    ("Esc (rows marked)", "Clear the marks"),
    ("0–9 (bill line selected)", "Start typing the quantity of the selected bill line"),
    ("Delete (purchase line)", "Remove the selected unreceived purchase line"),
    ("M (Purchases list)", "The supplier's product mappings"),
    ("Alt+1 / 2 / 3 (product editor)", "General · Packaging · Price & control tabs"),
    ("Alt+Z (in the tab switcher)", "Next tab in the list · Shift goes back · Delete closes the highlighted tab"),
]

# The browser (or the OS) keeps these for itself: a web page can never use them.
BLOCKED = {
    "F1", "Shift+F10", "Ctrl+L", "Ctrl+Shift+L", "Ctrl+0", "Ctrl++", "Ctrl+-", "Ctrl+=",
    "Ctrl+T", "Ctrl+N", "Ctrl+W", "Ctrl+Shift+T", "Ctrl+Shift+N", "Ctrl+Shift+W", "Ctrl+Shift+Q",
    "Ctrl+Tab", "Ctrl+Shift+Tab", "Ctrl+PageUp", "Ctrl+PageDown", "Ctrl+Shift+PageUp", "Ctrl+Shift+PageDown",
    "Ctrl+F4", "Alt+F4", "Alt+Tab", "Alt+Shift+Tab", "F11", "Ctrl+Shift+I", "Ctrl+Shift+J", "Ctrl+Shift+C",
    "Ctrl+Shift+Delete", "Alt+Home", "Alt+ArrowLeft", "Alt+ArrowRight", "Alt+D", "Alt+E", "Alt+F",
    "Alt+Space", "Ctrl+Escape", "Ctrl+Alt+Delete",
    *{f"Ctrl+{n}" for n in range(1, 10)}, *{f"Alt+{n}" for n in range(1, 10)},  # browser tabs / ERP tabs
}
_NAMED = {"ESCAPE": "Escape", "ESC": "Escape", "ENTER": "Enter", "TAB": "Tab", "SPACE": "Space",
          "BACKSPACE": "Backspace", "DELETE": "Delete", "DEL": "Delete", "INSERT": "Insert", "HOME": "Home",
          "END": "End", "PAGEUP": "PageUp", "PAGEDOWN": "PageDown", "PGUP": "PageUp", "PGDN": "PageDown",
          "ARROWUP": "ArrowUp", "ARROWDOWN": "ArrowDown", "ARROWLEFT": "ArrowLeft", "ARROWRIGHT": "ArrowRight",
          "UP": "ArrowUp", "DOWN": "ArrowDown", "LEFT": "ArrowLeft", "RIGHT": "ArrowRight"}
_NAV = {"Enter", "Escape", "Tab", "Space", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Insert"}


def normalize(key: str) -> str:
    """Canonical form: ``Ctrl+Alt+Shift+Key`` (``ctrl+shift+h`` → ``Ctrl+Shift+H``)."""
    parts = [p.strip() for p in str(key or "").replace(" ", "").split("+")]
    if len(parts) > 1 and parts[-1] == "" and parts[-2] == "":  # "Ctrl++"
        parts = parts[:-2] + ["+"]
    mods = {p.upper() for p in parts[:-1]}
    if mods - {"CTRL", "CONTROL", "CMD", "META", "ALT", "OPTION", "SHIFT"}:
        return str(key)
    base = parts[-1] if parts else ""
    upper = base.upper()
    if re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", upper):
        base = upper
    elif upper in _NAMED:
        base = _NAMED[upper]
    elif len(base) == 1:
        base = base.upper()
    out = [m for m, names in (("Ctrl", {"CTRL", "CONTROL", "CMD", "META"}), ("Alt", {"ALT", "OPTION"}),
                               ("Shift", {"SHIFT"})) if mods & names]
    return "+".join(out + [base]) if base else ""


def problem(key: str) -> str:
    """Why ``key`` cannot be a shortcut, or "" when it can."""
    k = normalize(key)
    if not k:
        return "Press a key combination"
    parts = k.split("+")
    base, mods = parts[-1], set(parts[:-1])
    if k in BLOCKED:
        return f"{k} is kept by the browser or used for tab switching — choose another"
    if base in ("Ctrl", "Alt", "Shift"):
        return "Add a key to the modifier (for example Alt+Q)"
    is_fn = bool(re.fullmatch(r"F([1-9]|1[0-2])", base))
    if mods - {"Ctrl", "Alt", "Shift"} or not (is_fn or base in _NAV or re.fullmatch(r"[A-Z0-9/,.;'\[\]\\`=\-]", base)):
        return "Use a letter, digit, navigation key or F1–F12 with valid modifiers"
    if {"Ctrl", "Alt"} <= mods:
        return "Ctrl+Alt combinations are reserved for system and AltGr typing shortcuts"
    if not mods and not is_fn:
        return f"{base} is needed for typing and moving around — use it with Ctrl or Alt, or use an F-key"
    if mods == {"Shift"} and not is_fn:
        return f"Shift+{base} types a character — use Ctrl or Alt instead"
    if base in _NAV and "Ctrl" not in mods and "Alt" not in mods and not is_fn:
        return f"{k} is used for moving in lists"
    return ""


def _overlap(a: str, b: str) -> bool:
    return a == b or "global" in (a, b)


def effective(overrides: dict[str, str]) -> dict[str, str]:
    return {a["id"]: normalize(overrides[a["id"]]) if a["id"] in overrides else a["key"] for a in ACTIONS}


def check(overrides: dict[str, str]) -> dict[str, str]:
    """Errors per action id for a complete set of overrides (empty = valid)."""
    errors: dict[str, str] = {}
    keys = effective(overrides)
    by_id = {a["id"]: a for a in ACTIONS}
    for aid, raw in overrides.items():
        if aid not in ACTION_IDS:
            errors[aid] = "Unknown action"
            continue
        if raw == "":
            continue  # unassigned on purpose
        why = problem(raw)
        if why:
            errors[aid] = why
    for i, a in enumerate(ACTIONS):
        for b in ACTIONS[i + 1:]:
            ka, kb = keys[a["id"]], keys[b["id"]]
            if ka and ka == kb and _overlap(a["scope"], b["scope"]):
                victim = a["id"] if a["id"] in overrides else b["id"]
                other = by_id[b["id"] if victim == a["id"] else a["id"]]
                errors.setdefault(victim, f"{ka} is already used for “{other['label']}” ({other['group']})")
    return errors


def _key(user: User) -> str:
    return f"keymap:user:{user.id}"


def overrides_for(db: Session, user: User) -> dict[str, str]:
    row = db.get(Setting, _key(user))
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in ACTION_IDS and isinstance(v, str)}


def save(db: Session, user: User, overrides: dict[str, str]) -> dict[str, str]:
    """Validate and store; returns the normalised overrides (defaults are dropped)."""
    defaults = {a["id"]: a["key"] for a in ACTIONS}
    errors = check(overrides)
    if errors:
        raise ValueError(errors)
    clean = {k: normalize(v) if v else "" for k, v in overrides.items()}
    clean = {k: v for k, v in clean.items() if k in ACTION_IDS and v != defaults.get(k)}
    errors = check(clean)
    if errors:
        raise ValueError(errors)
    row = db.get(Setting, _key(user))
    if row is None:
        row = Setting(key=_key(user), value="", value_type="json", label=f"Keyboard shortcuts for {user.username}")
        db.add(row)
    row.value = json.dumps(clean, sort_keys=True)
    db.flush()
    return clean


def payload(db: Session, user: User) -> dict:
    return {"actions": ACTIONS, "fixed": FIXED, "blocked": sorted(BLOCKED), "overrides": overrides_for(db, user)}
