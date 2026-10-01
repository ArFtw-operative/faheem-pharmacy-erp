# ERP keyboard shortcuts

After updating the Python backend, restart the running pharmacy server on its existing port, then refresh the ERP page. A browser refresh alone does not reload Python routes. Cached pages without embedded shortcut data fetch the registry from the API; if unavailable, the workspace shows a restart message instead of silently dropping all defaults.

Open **Keyboard shortcuts** from the status bar, **Ctrl+/**, or **F1**. F1 is fixed and remains available even when the configurable shortcut is cleared.

Search by action, key, or screen. Click an action, or select it with the arrow keys and press Enter, then press its new shortcut. Backspace clears an assignment; R resets the selected action when focus is outside the search field. The window also has a Reset all to defaults button. Escape cancels key capture or clears a search; Escape again closes the window.

Assignments save automatically for the signed-in user and load on other counters at the next page load. Changes update the current page immediately, including open workspace tabs, the status bar, POS button hints, context menus and the command palette. Separate browser windows load updates when refreshed.

Customer search defaults to **Alt+C**; Ctrl+L is reserved for the browser address bar. New bill and close bill remain Alt+N and Alt+W. The old F11, Ctrl+W and Ctrl+Tab aliases are removed because those keys belong to the browser.

## Sale type

The POS header has **Walk-in** and **Home delivery** buttons. **Alt+Y** switches between them; reassign it by searching for **Switch sale type** in the shortcuts window. Selecting a customer initially uses their saved category unless a sale type has already been explicitly chosen for this bill. Changing the sale type keeps the customer selected and does not modify their profile.

The choice is saved in the tab draft, included in held-bill payloads, restored when resumed, and sent as `customer_type` when saving the sale. The existing `sales.customer_type` database column stores it for invoices and reports. A fresh bill starts as Walk-in. No database migration is required.

## Reports

Reports opens in an ERP workspace tab with **Alt+R**. Returns now uses **Alt+Shift+R**. Within Reports: **Alt+T** focuses period/date, **Alt+G** generates, **Alt+C** chooses columns, **Ctrl+P** prints, **Ctrl+E** exports Excel and **F5** refreshes. Escape returns to the report selector. Arrow keys move between cards; Enter opens one. Generated table rows support Up/Down and Enter for drill-down, or double-click with a mouse.

Alt+D and Ctrl+Shift+C remain blocked because the browser reserves them. All report commands can be reassigned in the existing shortcuts window.

## Validation and scope

The server and browser reject reserved browser/system shortcuts, plain typing/navigation keys, Ctrl+Alt combinations used by AltGr, unknown key names and conflicts. A global action cannot share a key with any screen action. Different screens may reuse a key (for example F2 searches both POS and Inventory). Resetting an individual action is also rejected if its default is now assigned to another overlapping action.

Interceptable shortcuts such as Ctrl+F and Ctrl+R retain their ERP meanings while assigned. Unassigned keys are not intercepted by the shell. Native dialogs and operating-system shortcuts are outside the ERP's control. Browser verification currently covers Chromium.

## Implementation

- `app/services/keymap_service.py`: action IDs, defaults, scope and validation; settings stored under `keymap:user:<id>`.
- `GET/PUT /api/erp/keymap`: authenticated per-user settings. PUT replaces the complete overrides object, validates before writing, and returns normalized assignments.
- `app/static/erp/keys.js`: effective assignments, conflict checking and change notifications.
- `app/static/erp/shortcuts.js`: searchable keyboard-driven dialog.
- `app/static/erp/shell.js`: dispatches registered commands before editable cells consume events. POS commits valid quantity/discount edits before executing a command; invalid edits keep focus.

To add a command, register its action and scope in the service, then bind its action ID in the screen or shell. Do not add another hard-coded key handler. Mark inline key hints with `data-shortcut="action.id"`.

## Verification

Run `.venv/bin/python -m pytest tests/test_keymap.py tests/test_erp_api.py tests/test_discount_limits.py tests/test_parking.py -q`.

For browser checks, start `.venv/bin/python scripts/shortcut-test-server.py` in one terminal, then run `node scripts/test-shortcuts-browser.mjs` in another. The server creates an isolated temporary database and test account; do not point this browser script at a live database. Stop the test server after use.

Verified: 45 Python tests; Chromium dialog, conflict feedback, remapping, persistence after reload, reset, POS tabs, Inventory/Masters integration, quantity commit, remapped bill-close confirmation, cached-shell default recovery, all payment buttons and an actual completed test sale. Screenshot: `/tmp/pharmacy-shortcuts.png`.

Inventory: Alt+C opens Columns for both the product and batch panes. This binding is configurable as `inv.columns` in Keyboard Shortcuts.
