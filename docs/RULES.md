# Rules

**The source of truth for what Faheem Pharmacy ERP must always do and never do.** Every rule says where it is enforced,
so it can be checked in the code. *(U)* marks rules that exist only in the unreleased 1.10.0 development branch.

Related: [PRD.md](PRD.md) (what the product does) · [ARCHITECTURE.md](ARCHITECTURE.md) (how) · [MEMORY.md](MEMORY.md) (why).

---

## 1. Business rules

### Selling

**BR-001 — Never sell expired stock.**
Reason: illegal and unsafe. Enforced: `app/services/stock_ledger.py` (`sellable_batches`, `allocate` raises *"Batch … expired … cannot be sold"*); expiry months count as sellable to the month's last day (`app/services/units.py` `is_expired`).
Module: POS. Exceptions: none (an expired batch leaves stock only through an Expired adjustment or a purchase return).

**BR-002 — Never sell more than is in stock.**
Reason: stock must match the shelves. Enforced: `stock_ledger.allocate` (counts only unexpired batches); checked again on the server at save time, not only on screen. Module: POS. Exceptions: manual bills (1.10.0), which are not sales and do not touch stock.

**BR-003 — First expiry, first out (FEFO).**
Reason: sell what expires first. Enforced: `stock_ledger.sellable_batches` orders by expiry (no-expiry last); `allocate` takes them in that order. Exception: a cashier may choose a batch (F5); a non-FEFO choice is written to the audit log (`sales_service._add_lines`).

**BR-004 — The price is the batch MRP; the browser never sets prices.**
Reason: MRP (Maximum Retail Price) is the legal ceiling and must be consistent. Enforced: `sales_service._add_lines` (price from `batch.mrp`; a batch without MRP cannot be sold). Exception: manual bills use typed rates.

**BR-005 — Quantities follow the pack.** A product not sold loose is sold in whole packs; loose products are sold in base units (tablets), with `1s` = one strip. Enforced: `units.check_sale_quantity`, `pos.js` `problem()`.

**BR-006 — Discounts are percentages within the store limit.** Item discount ≤ `max_discount_pct` (default 20%), bill discount ≤ the same, and item + bill together ≤ the same share of the MRP value (one paisa tolerance per line). Enforced: `sales_service._line_discount`, `_apply_totals_and_payments`; `pos.js` mirrors it. Permission: `sales.discount` (without it, discounts sent by the browser are removed in `routers/sales.py`).

**BR-007 — Payments add up exactly to the bill.** Split parts must sum to the rounded total; cash received must cover the cash part; change is computed on the cash part only. Enforced: `sales_service._normalize_payments`, `_apply_totals_and_payments`.

**BR-008 — Rounding follows the store setting** (`round_off_mode`: `NEAREST_RUPEE` default, `NEAREST_HALF`, `NONE`). Enforced: `sales_service.round_off`.

**BR-009 — Disabled or recycle-bin products are not sold.** Exception: a bill that already contained the product can still be edited. Enforced: `sales_service._add_lines` (`already_billed`).

**BR-010 — No double billing.** A retried submission with the same `client_request_id` returns the existing bill. Enforced: `sales_service.create_sale`, unique column `sales.client_request_id`, `routers/sales.py` (IntegrityError path).

**BR-011 — A bill is corrected, never silently changed.**
- Edit keeps the number, puts old stock back with `SALE_CANCEL` movements and sells the new lines, fully audited (`sales_service.amend_sale`).
- Not editable when voided, when anything was returned, or *(U)* when Udhaar repayments were received (`sales_service.editable_problem`, `udhaar_service.edit_problem`).
- Void needs a reason and keeps the bill marked `CANCELLED` (`sales_service.void_sale`, `routers/sales_history.py`).

**BR-012 — Returns are their own documents** (`RFND-YYYYMMDD-NNNN`), never edits of the bill; refunds are the original paid value per unit; quantity cannot exceed what is left to return. Enforced: `app/services/refund_service.py`.

**BR-013 — Refund approval.** Above `refund_manager_threshold` (default ₹1,000), or to a payment method the bill was not paid with, a refund needs `sales.refund_override`. Enforced: `refund_service.create_return`, `routers/sales.py`.

### Udhaar (store credit, pay later) *(U)*

**BR-014 — Udhaar needs an identified customer with a mobile number.** Enforced: `udhaar_service._check_customer` (server), `pos.js` `udhaarProblem` (screen).

**BR-015 — Udhaar needs a due date and a reminder date.** Due date ≥ today; reminder ≥ today and ≤ due date. Defaults: due = today + customer's days (else store `udhaar_days`, default 15); reminder = day before due. Enforced: `udhaar_service.sync_sale`, `_check_dates`, `proposed_dates`.

**BR-016 — The Udhaar limit cannot be exceeded** (when the customer has one). Enforced: `udhaar_service._check_customer`.

**BR-017 — Repayments never change the bill.** Each is an `udhaar_payments` row; `entry.paid` is their sum in the same transaction; a payment cannot exceed what is owed; without a chosen bill, oldest dues are paid first. Enforced: `udhaar_service.receive_payment`.

**BR-018 — Cash is never paid out for goods not paid for.** A return on a bill with Udhaar still owed must be refunded by Udhaar (reducing what is owed), even with an override. Enforced: `refund_service.create_return`, `udhaar_service.apply_return`.

**BR-019 — Udhaar status is computed, not stored:** Paid · Partially Paid · Overdue · Due Today · Upcoming (Cancelled when the bill was voided before any payment). Enforced: `udhaar_service.status`.

**BR-020 — A voided bill cancels its Udhaar; a bill with Udhaar repayments cannot be voided.** Enforced: `udhaar_service.cancel_for_void`.

### Counter and manual bills *(U)*

**BR-021 — A business day ends at 11:59 PM store time.** Enforced: `counter_service.bounds` (store `timezone`).

**BR-022 — A finished day's Counter Report is frozen and never recomputed.** Later changes are shown as differences beside the frozen figures; a SHA-256 digest detects tampering. Enforced: `counter_service.close_day`, `report`.

**BR-023 — Manual bills are never sales.** Separate tables; no stock, no payments, no Udhaar, not in any report, the Counter Report or Sales History; no batch on a line; Cash / UPI / Card only (as written). Enforced: `app/services/manual_bill_service.py`, migration `a8c0e2f4b6d8`.

### Purchases

**BR-024 — Nothing enters stock until a person posts it.** Enforced: `purchasing.post` (needs `purchase.post`).

**BR-025 — Posting needs a supplier and the supplier's invoice number**, and a PDF whose extraction reconciles with its printed totals. Enforced: `purchasing.post`.

**BR-026 — One invoice, one receipt.** The same file cannot be imported twice (file fingerprint `source_sha256`); the same supplier invoice number can be posted only once. Enforced: `purchasing.py`, partial unique index on `purchases`.

**BR-027 — Suggestions are suggestions.** Fuzzy product matches, proposed counts and rack suggestions are shown, never applied without a person. A normalised-name match is applied only when unique. Enforced: `purchase_automation.py`, `product_matcher.py`, `receipt_proposer.py`, `location_service.py`. See `DECISIONS.md` D4, D11, D29.

**BR-028 — Automatic product creation is off by default** (`purchase_auto_create_products=off`); new products wait for one-click confirmation. **Scanned images are off by default** (`purchase_scan_import=off`). Enforced: `purchase_automation.py`, `purchase_import.py`.

**BR-029 — Rollback to draft is all or nothing:** refused if any of that stock was sold, returned or adjusted, or if a purchase return exists. Enforced: `purchasing.rollback`.

**BR-030 — Purchase cost includes GST by default** (`purchase_cost_includes_gst=true`); each batch records which basis it uses. Enforced: `app/services/gst.py`.

### Stock and history

**BR-031 — Every stock change is a ledger movement; batch stock equals the sum of its movements.** Enforced: `stock_ledger.post`, `reconcile`; checked in the upgrade fingerprint (`check:ledger_mismatched_batches`).

**BR-032 — Mistakes are reversed, never deleted.** Sales are voided, adjustments reversed by another numbered document, purchases rolled back with `RECEIPT_REVERSAL` movements. Enforced: `stock_ledger.reverse`, `adjustment_service`, `purchasing.rollback`.

**BR-033 — History is never recalculated.** Cost is snapshotted on each sale line at sale time; reports read stored documents. Enforced: `app/services/financials.py`, [UPGRADES.md](UPGRADES.md).

**BR-034 — Locations never touch stock.** Enforced: `app/services/location_service.py` (DECISIONS D30).

---

## 2. Data rules

| Data | Rule | Source |
|---|---|---|
| Unique identifiers | `users.username`, `users.employee_id`, `items.article_id`, `customers.customer_id`, `suppliers.code`, `racks.code`, `sales.invoice_no`, `manual_bills.invoice_no`, `purchases.reference_no`, `sale_returns.return_no`, `purchase_returns.reference_no`, `stock_adjustments.reference_no`, `parked_sales.park_reference` | `app/models.py` |
| Batch identity | (`item_id`, `batch_no`, `expiry_date`) unique | `models.Batch` |
| Customer | Name required; mobile unique per customer (lookup by last 10 digits); type `WALK_IN` or `HOME_DELIVERY` | `customer_service.create_customer`, `find_by_mobile` |
| Sale states | `payment_status`: `PAID` (completed) or `CANCELLED` (voided) | `models.Sale`, `sales_service.void_sale` |
| Purchase states | `DRAFT` → `PARTIAL` → `POSTED`; `CANCELLED`; rollback back to `DRAFT` | `purchasing.py` |
| Parked bill states | `PARKED`, `CLAIMED`, `COMPLETED`, `DISCARDED` | `models.ParkedSale` |
| Udhaar entry states *(U)* | `OPEN`, `PAID`, `CANCELLED` (stored); display status computed | `models.UdhaarEntry`, `udhaar_service.status` |
| Manual bill states *(U)* | `ACTIVE`, `DELETED` (kept) | `models.ManualBill` |
| Money | Decimal with 2 places; never floating point | `app/utils.py` `money` |
| Time | Stored in UTC; business dates in the store timezone | `app/utils.py`, `business_time.py` |
| Deletion | Products → recycle bin (`deleted_at`); customers → deactivated (`is_active=false`); bills → voided; manual bills → marked Deleted; adjustments → reversed. **No hard delete of business documents.** Exception: the test-data reset (`app/services/data_reset.py`), behind its own command. | services named |
| Retention | Snapshots are never pruned automatically; audit log is permanent | `app/snapshot.py`, `app/audit.py` |
| Settings | Typed key/value rows; keys documented in [HANDOFF.md](HANDOFF.md) and `app/seed.py` | `settings_service.py` |

---

## 3. User permission rules

Who may do what is decided only by permission codes (`app/permissions.py`), checked on the server for every request
(`app/deps.py` `require_permission`). The screen hides what a user cannot use (`BOOT.can`), but hiding is not the
protection. Some screens additionally limit **data**: without `sales.view_history` a user sees only their own bills
(`routers/sales_history.py` `_scope`); cost and profit report columns need `reports.financials`
(`report_generator.visible_columns`).

| Action | Permission(s) |
|---|---|
| View | `inventory.view`, `sales.view_own` / `sales.view_history`, `purchase.view`, `customers.view`, `rack.view`, `udhaar.view`, report permissions |
| Create | `sales.create`, `inventory.create`, `purchase.create`, `customers.create`, `adjustment.create`, `rack.create` |
| Edit | `inventory.edit`, `customers.edit`, `rack.edit`, `sales.void` (bill edit) |
| Delete / disable | `inventory.delete`, `rack.disable`, `sales.void` (void bill, delete manual bill) |
| Approve | `purchase.post` (stock in), `sales.refund_override` (large / other-method refunds), `expiry.settle` |
| Export | `inventory.export`, `reports.export`, `sales.export` |
| Configure | `settings.manage` (WhatsApp, invoice templates, system info), `udhaar.manage` *(U)*, `supplier.manage`, `box.manage` |

**Default role matrix** (generated from `DEFAULT_ROLES`; administrators can change roles at runtime):

| Permission | Area | Allows | Admin | Mgr | Pharm | Ctr Mgr | Staff | Acct |
|---|---|---|---|---|---|---|---|---|
| `inventory.view` | inventory | View item master and stock | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `inventory.create` | inventory | Add items to the master | ✓ | ✓ | ✓ |  |  |  |
| `inventory.edit` | inventory | Edit item master / batches | ✓ | ✓ | ✓ |  |  |  |
| `inventory.delete` | inventory | Deactivate or delete items | ✓ | ✓ |  |  |  |  |
| `inventory.export` | inventory | Export inventory to Excel/CSV | ✓ | ✓ | ✓ |  |  | ✓ |
| `sales.create` | sales | Create a sale / bill | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `sales.view_own` | sales | View own sales | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `sales.view_history` | sales | View all sales history | ✓ | ✓ | ✓ | ✓ |  | ✓ |
| `sales.view_profit` | sales | View profit/loss figures | ✓ | ✓ |  |  |  | ✓ |
| `sales.discount` | sales | Apply discounts and vouchers | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `sales.void` | sales | Void or cancel a sale | ✓ | ✓ |  |  |  |  |
| `sales.refund` | sales | Process a return / refund against an invoice | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `sales.refund_override` | sales | Approve refunds above threshold or to a different method | ✓ | ✓ |  |  |  |  |
| `sales.export` | sales | Export sales data | ✓ | ✓ |  |  |  | ✓ |
| `billing.print` | billing | Print or reprint invoices | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `whatsapp.send` | billing | Send / resend a sale's invoice on WhatsApp (customer-requested) | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `settings.manage` | settings | Settings: pair or log out WhatsApp, invoice message and image | ✓ |  |  |  |  |  |
| `purchase.view` | purchase | View purchases | ✓ | ✓ | ✓ |  |  | ✓ |
| `purchase.create` | purchase | Import, enter and correct purchase invoices (drafts) | ✓ | ✓ | ✓ |  |  |  |
| `purchase.post` | purchase | Post a reviewed purchase invoice into stock | ✓ | ✓ | ✓ |  |  |  |
| `supplier.manage` | purchase | Add and edit suppliers and their product mappings | ✓ | ✓ | ✓ |  |  | ✓ |
| `purchase.return` | purchase | Create purchase returns | ✓ | ✓ | ✓ |  |  | ✓ |
| `customers.create` | customers | Add a customer while billing | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `customers.view` | customers | Customer directory, invoices, activity and customer reports | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `customers.edit` | customers | Edit customer details and notes | ✓ | ✓ | ✓ |  |  |  |
| `followups.manage` | customers | Create, complete and reschedule customer follow-ups | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `reports.sales` | reports | View sales reports | ✓ | ✓ | ✓ |  |  | ✓ |
| `reports.purchase` | reports | View purchase reports | ✓ | ✓ |  |  |  | ✓ |
| `reports.expiry` | reports | View expiry reports | ✓ | ✓ | ✓ |  |  | ✓ |
| `reports.financials` | reports | View financial / profit reports | ✓ | ✓ |  |  |  | ✓ |
| `reports.export` | reports | Export reports | ✓ | ✓ | ✓ |  |  | ✓ |
| `reports.counter` | reports | Counter Report: the day's sales and collections by payment mode | ✓ | ✓ | ✓ | ✓ |  | ✓ |
| `udhaar.view` | udhaar | Udhaar Ledger: what customers owe, ledgers and statements | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `udhaar.receive` | udhaar | Receive Udhaar payments and send Udhaar reminders | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `udhaar.manage` | udhaar | Set customers' Udhaar limit and days, and opening balances | ✓ | ✓ | ✓ |  |  |  |
| `expiry.view` | expiry | View expiry alerts | ✓ | ✓ | ✓ |  |  |  |
| `expiry.snooze` | expiry | Snooze an expiry reminder | ✓ | ✓ | ✓ |  |  |  |
| `expiry.settle` | expiry | Settle an expiry (debits stock) | ✓ | ✓ | ✓ |  |  |  |
| `adjustment.create` | adjustment | Record loose/damage write-offs | ✓ | ✓ | ✓ |  |  |  |
| `rack.view` | rack | See racks, boxes and product locations | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `rack.create` | rack | Create racks | ✓ | ✓ |  |  |  |  |
| `rack.edit` | rack | Edit racks and location settings | ✓ | ✓ |  |  |  |  |
| `rack.disable` | rack | Disable and enable racks | ✓ | ✓ |  |  |  |  |
| `rack.assign` | rack | Assign or move a product's location | ✓ | ✓ | ✓ |  |  |  |
| `rack.bulk_move` | rack | Move many products to a rack at once | ✓ | ✓ | ✓ |  |  |  |
| `rack.history.view` | rack | See location history | ✓ | ✓ | ✓ |  |  | ✓ |
| `rack.report.view` | rack | Rack inventory reports | ✓ | ✓ | ✓ |  |  | ✓ |
| `rack.snapshot.view` | rack | Historical (as-of) rack inventory | ✓ | ✓ | ✓ |  |  | ✓ |
| `box.manage` | rack | Create, rename and disable boxes | ✓ | ✓ |  |  |  |  |

New permission codes reach existing installations through the migration that introduces them (example:
`alembic/versions/a8c0e2f4b6d8_udhaar_manual_bills_counter.py` `GRANTS`) and through `app/seed.py`, which adds a newly
introduced code to the roles whose template includes it.

---

## 4. Application rules

| Rule | Where |
|---|---|
| The workspace is the only interface: `/` redirects to `/app`; screens open as tabs, several POS tabs at once | `app/main.py`, `shell.js` |
| Work is never lost: open tabs and unfinished bills are saved continuously per user and counter and restored at the next login | `routers/workspace.py`, `shell.js` |
| Two counters stay in sync: change counters are bumped in the same transaction as the change; screens refresh every 4 s, never while a dialog is open | `services/sync_service.py`, `shell.js` |
| Billing never waits for WhatsApp or printing: messages are queued | `whatsapp/service.py` |
| Invoices open inside the tab (Invoice Studio), never a browser pop-up | `studio.js` |
| Every action has a keyboard shortcut; each user may reassign them; keys the browser keeps are refused | `keymap_service.py` (`ACTIONS`, `BLOCKED`) |
| API answers are never cached | `app/main.py` middleware |
| Long work never blocks other screens (worker threads) | `app/routing.py` |

---

## 5. Coding rules (as practised in this repository)

| Topic | Convention |
|---|---|
| Language / stack | Python 3 + FastAPI + SQLAlchemy 2; plain JavaScript ES modules; no frontend framework, no build step (DECISIONS D2) |
| Layering | Routers are thin (parse, permission, call service, commit/rollback, shape JSON); rules live in `app/services/`; the stock ledger is the only code that changes stock |
| Naming | Python `snake_case`; services named `<area>_service.py`; routers per area; screens `app/static/erp/<module>.js`; tables plural `snake_case`; permission codes `area.action` |
| Errors | Each service has its own exception (`SaleError`, `PurchaseError`, `UdhaarError`…) with a message a cashier can act on; routers turn them into HTTP 400 `{"detail": …}` |
| Audit | Every data-changing service calls `app.audit.record(...)` |
| Money and time | `Decimal` via `app.utils.money`; UTC in the database, local time via `business_time` |
| Database access | SQLAlchemy ORM / Core only, parameterised; no string-built SQL with user input |
| Migrations | One Alembic migration per schema change, must run on SQLite **and** PostgreSQL; additive by default; destructive steps need `DESTRUCTIVE_APPROVED` + `RECONCILE_EXEMPT`/`RECONCILE_MOVED` ([UPGRADES.md](UPGRADES.md)); `downgrade()` raises; one head only |
| Release fixtures | Each release adds `tests/fixtures/releases/<version>.db` (never regenerated); every fixture must upgrade to head with history intact (`tests/test_upgrades.py`) |
| Tests | pytest in `tests/`, run on SQLite and PostgreSQL (`PHARMACY_TEST_DATABASE_URL`); browser flows in `scripts/test-*-browser.mjs` |
| New screen keys | Register in `keymap_service.ACTIONS`, short label in `keys.js`, capability in `routers/erp.py` `CAPABILITIES` |
| New module | `shell.js` `MODULES`, `erp.html` import map, `routers/erp.py` `MODULE_PERMS` |
| Configuration | Store behaviour in the `settings` table; bootstrap/secrets in environment variables; never secrets in Git |
| Dependencies | Permissive licences only (MIT, BSD, Apache-2.0, MPL-2.0); see `DEPENDENCIES.md` |
| Naming in the UI | Store credit is **Udhaar** everywhere (code `UDHAAR`); "Card" is only for card payments |
| Commits | Author identity `ArFtw-operative <arfwtw@users.noreply.github.com>`; release push to `prod` only with the owner's consent |

---

## 6. Critical "never break" rules

These protect money, stock and trust. Each is backed by code and tests; breaking one is a serious defect.

1. **Never change stock outside `app/services/stock_ledger.py`.** No direct updates of `batches.quantity`.
2. **Never sell expired or unavailable stock, and never take the price from the browser** (BR-001, BR-002, BR-004).
3. **Never edit or delete a ledger movement, a posted document or an audit record.** Correct by reversal or a new document (BR-032).
4. **Never recalculate history** in a migration or a report (BR-033). A migration that must move data declares it and reconciles it (`RECONCILE_MOVED`).
5. **Never change the schema without an Alembic migration**, and never start the app on a half-migrated database (`database.init_db` refuses).
6. **Never bypass `require_permission`** on a new endpoint.
7. **Never let manual bills enter sales, money, Udhaar or stock** *(U)* (BR-023).
8. **Never pay out cash for unpaid Udhaar goods; never modify an Udhaar bill to record a repayment** *(U)* (BR-017, BR-018).
9. **Never reopen a frozen counter day** *(U)* (BR-022).
10. **Never apply a guess as a fact** in purchases or locations (BR-027).
11. **Never block billing** on WhatsApp, printing or a slow report.
12. **Never commit secrets, real customer data or production databases.**
