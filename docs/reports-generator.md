# SQL-backed ERP report generator

Reports is an ERP workspace tab and `/reports` opens the same selector. The landing page renders report definitions only. It does not call sales, purchase, inventory valuation or profit summary queries. Opening a card loads parameters; only Generate (or an explicit drill-down) queries report data.

Twenty reports cover sales, bill/item/category/brand registers and returns; purchase summaries/registers/suppliers/returns; current/batch/expiry/low stock and movements/adjustments; profit, discounts, collections and void invoices.

## Workflow and controls

Choose a card → choose dates and report-specific filters → Generate Report. Presets are calendar-based in the pharmacy timezone: Today, Yesterday, This Week, Last Week, This Month, Last Month and Custom Range. Custom Range enables From/To. Inventory balances use an As of Date. Parameter changes do not refresh an existing document until Generate is pressed.

Columns, Filter, Print, PDF, Excel, CSV and Refresh are on the generated document toolbar. Column choices are remembered per report/view and signed-in user in this workstation's browser. Applying a different column selection explicitly generates the requested document again. Reset returns to fresh parameters and removes the result. Dates support native browser date picking and keyboard input.

Sales Summary is the first card and defaults to a daily crosstab. Its category columns come from `items.category` in SQL, including custom categories; there is no Apollo channel split or fixed category list. The detail view is a bill register. Double-click or keyboard Enter on a summary row opens its date-range bills; invoice rows open the bill. Category/brand rows open filtered Item-wise Sales, and supplier rows open filtered Purchase Register.

## SQL and accounting rules

- Source records: `sales`, `sale_items`, `sale_payments`, `sale_returns`, `sale_return_items`, `purchases`, `purchase_items`, `purchase_returns`, `items`, `batches`, `suppliers`, `inventory_movements` and the cancellation audit trail.
- All date boundaries are `[start, end)` in UTC, converted from the selected pharmacy-local dates. The To date is inclusive to the user.
- Completed sales exclude cancelled invoices. Each invoice's total is allocated across its recorded line values, including bill discounts, voucher and rounding; the final line absorbs penny differences. Category totals therefore reconcile to invoice totals. Sales Returns is separate from original sales.
- Purchase category/item filters allocate the received invoice total over its recorded line values. Draft/unreceived documents are excluded. Purchase quantities are not confused with base stock quantities.
- Profit reads recorded sale-line cost, subtracts completed refunds from revenue and restores cost only for restocked returns. Margin is gross profit / revenue. It does not invent expenses or claim net profit.
- Payment Collection uses each actual `sale_payments` row, including split bills, and completed refund outflows in their actual method/date.
- Historical stock quantities come from signed `inventory_movements` up to the selected date, not today's batch projection. Products without a received batch appear in low/out-of-stock reports. Expiry is sellable through the end of its recorded month.
- Product descriptions, categories/manufacturers and stock MRP use the current product/batch master because historical versions of these fields are not stored. Inventory reports state this explicitly; they do not claim historical monetary valuation. Quantities are shown with their product's stock unit.
- Loss/Adjustment includes signed corrections and unsellable customer returns. Void reasons come from the recorded cancellation audit entry.

## Access and exports

The registry supplies a permission per report. Sales reports use `reports.sales`, purchase reports use `reports.purchase`, inventory reports use `inventory.view`, and Profit & Margin uses `reports.financials`. Unauthorized cards are absent and direct generation is rejected. Exports also require `reports.export` and re-check report permission.

Generation returns only the chosen fields plus applicable drill-down targets. The server retains a user-scoped document snapshot for exports, so CSV, numeric/styled Excel and paginated PDF contain the same selected fields and generated rows even if SQL data changes afterward. Snapshots expire after 30 minutes, server restart or eviction from the most recent 32 documents; generate again when an export expires. User-provided spreadsheet text is escaped to prevent formulas.

The legacy explicit `/reports/export/...` endpoints remain for compatibility; the ERP shell at `/app` is the only landing page.

## Keyboard

Alt+R opens Reports. Alt+T focuses date/period, Alt+G generates, Alt+C opens Columns, Ctrl+P prints, Ctrl+E exports Excel, F5 refreshes, Escape returns to the selector. Card arrows and Enter support selection; table Up/Down and Enter support drill-down. Returns now opens with Alt+Shift+R. Alt+D and Ctrl+Shift+C stay reserved for the browser. All report commands are in the customizable shortcut registry.

## Verification

`.venv/bin/python -m pytest tests/test_report_generator.py tests/test_reports.py tests/test_erp_api.py tests/test_keymap.py tests/test_pages.py tests/test_admin_rbac.py tests/test_ops_workspaces.py -q`

For UI verification, start `.venv/bin/python scripts/shortcut-test-server.py` against its automatically created isolated database, then `node scripts/test-reports-browser.mjs`. It checks a landing page without data queries, keyboard selection/generation, SQL filters, totals, drill-down, saved columns, all twenty report cards, CSV/Excel/PDF, and Alt+R. The POS browser regression remains `node scripts/test-shortcuts-browser.mjs`.

Screenshots produced during verification: `/tmp/pharmacy-reports-selector.png` and `/tmp/pharmacy-sales-summary.png`. Report tests verify database data, timezone boundaries, invoice reconciliation, void exclusion/reason, column projection, historical ledger balances, populated returns, profit and payment calculations, authorization and snapshot export integrity.

Inventory has a Columns control (Alt+C), with independent product and batch selections remembered per user on the workstation. MRP and purchase rates have explicit pack/unit labels. Current Stock, Batch-wise Stock, Expiry and Low Stock offer the same pricing columns in their Columns controls. Rates use the latest matching purchase invoice line linked through a received stock-ledger movement, respecting the report as-of date. Opening stock without a purchase invoice uses its recorded batch rate. Differing batch prices show Varies in Inventory and blank in product-level reports; use Batch-wise Stock for exact prices. Purchase rates and source invoice columns require purchase.view, including server-side report selection checks.
