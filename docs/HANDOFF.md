# Faheem Pharmacy ERP — product guide

Release **1.4.0**, database schema `f6b8d0e2a4c7`. Everything is the keyboard workspace at `/app`.

## Running it

On the pharmacy PC the ERP is an appliance: install, network access, updates, backups and recovery
are in [INSTALL.md](INSTALL.md), [OPERATIONS.md](OPERATIONS.md), [UPDATE.md](UPDATE.md),
[BACKUP-RESTORE.md](BACKUP-RESTORE.md) and [RECOVERY.md](RECOVERY.md). For development see the README.
Every API answers with `Cache-Control: no-store`.

Administrators have a **Settings** tab (WhatsApp Invoicing — see [WHATSAPP.md](WHATSAPP.md)). Everything else is configured through the CLI
(on the appliance: `sudo faheem-erp manage <same arguments>`, e.g. `sudo faheem-erp manage setting list`):

```bash
python scripts/manage.py user list|add|passwd|disable|enable|reset-2fa|unlock [username] [--role R] [--password P]
python scripts/manage.py role list
python scripts/manage.py setting list|get|set <key> [value]
python scripts/manage.py version | upgrade [--check] | deployments
python scripts/manage.py logo <path>
```

Useful settings:

| Key | Meaning |
|---|---|
| `max_discount_pct` | Maximum item and bill discount (default 20) |
| `invoice_vocabulary` | JSON of extra supplier-header abbreviations, e.g. `{"qtty": "qty", "purrt": "rate"}` |
| `invoice_ai_enabled` / `invoice_ai_key` / `invoice_ai_model` | Optional AI invoice reader (off by default; see Purchases) |
| `round_off_mode`, `expiry_threshold_days`, `low_stock_threshold` | As named |

## The workspace (`/app`)

One keyboard-first screen with modules as tabs; state survives switching tabs. **Ctrl+/**
lists every shortcut, and each one can be reassigned per user (browser-reserved keys are
refused). **Ctrl+K** opens the command palette.

| Module | Key | What it does |
|---|---|---|
| POS | Alt+P · Alt+N new bill | FEFO billing in sale units (strips and loose tablets), cash/UPI/card/split, received/change, `#` customer search with inline create, hold/resume, item and bill discount (capped), **Alt+L manual bill** |
| Inventory | Alt+I | Products, batches (supplier, supplier invoice, PUR no., received date), F5 adjust, F6 ledger, F7 history |
| Stock History | Alt+Shift+H | Every ledger movement across products; filters, totals, CSV |
| Adjustments | Alt+Shift+A | ADJ documents: loose, damage, expired, count shortfall/surplus, other; Alt+X reversal |
| Purchases | Alt+U | Register, supplier invoice import and review, suppliers, purchase returns |
| Sales | Alt+Shift+R | Bill register, detail, hover preview, **Enter: Invoice Studio**, F4 edit (in its original POS tab), F6 return/refund, F7 exchange, Ctrl+P print, void |
| Customers | Alt+Shift+C | Directory (bills, net sales, last visit), customer record (invoices with hover preview, returns, follow-ups, activity, notes), Inbox and Calendar of follow-ups |
| Reports | Alt+R | 20+ reports; Document view in the ERP text format; PDF/Excel/CSV/Text |
| Masters | Alt+M | Pharmacy-managed categories, units of measure |

## Rules the system enforces

- **Stock changes only through the inventory ledger** (`inventory_movements`), in base units.
  `batches.quantity` is a projection; `stock_ledger.reconcile()` must return `[]`.
- **Documents are immutable.** Corrections are new documents: returns (RFND-), purchase
  returns (PR-), adjustment reversals (ADJ-), voids (stock reversed, bill kept). Bills are never edited.

## Customers and follow-ups

Customers is an operational module, not a CRM. Every figure comes from finalized bills: voided bills are excluded
and refunds are subtracted. A follow-up is one `customer_followups` record, which POS (**Alt+O**), the customer
record (F4), the Inbox (C complete, R reschedule) and the Calendar all read and change. Invoice links always open the
canonical bill in the Sales tab.

Customer reports (Reports → Customer Reports): Frequent Customers, Purchase History, Recovery (inactive customers),
Top Customers, New Customers, Returns / Refunds, Follow-up Due and Follow-up Performance. Inventory reports add
Non-Moving Items, Top Moving Items and Item-wise Sales (per product or batch, split by day/week/month; sold, returned,
net qty, MRP value, discount, sales, refunds, net sales, bills, stock, cost/profit) — base units, net of returns.
All reports share the ranges Today … Last 7/30/90 days, quarters, financial year (Apr–Mar) and custom.

## Faheem Pharmacy Invoice Studio

Opens **inside the tab**, never as a browser pop-up. In POS, F12 **Complete sale** shows the sale summary (plain ERP
layout: items, MRP, Dis%, discount, totals, payment; Enter completes, Esc goes back to adjust), then asks
**Generate invoice? Y/N** — Y opens the studio in that POS tab (it never auto-prints). In the Sales tab, Enter opens it beside the bill list and ↑/↓ choose the invoice.
The header shows the bill's facts. Keys: 1–6 choose A4 / A5 / Letter / 80 mm / 58 mm / plain ERP text document, E toggles expiry, B toggles B&W,
Ctrl+P prints and Esc closes. It also exports PDF, Excel and CSV. Report rows open their bill in the Sales tab.
- **Only a posted purchase changes stock.** Posting is atomic; the same supplier invoice
  posts once (DB-enforced); a total difference must be acknowledged, and that is audited.
- **Nothing is guessed.** Batch, expiry, product, pack conversion and cost are either
  proven or sent to review. Fuzzy/AI suggestions are never applied without a person.
- **Unknown cost is shown as "—"**, never as zero (manual bills, legacy stock without cost).

## Purchases: how supplier files are understood

No supplier layout is hard-coded. For CSV, XLS and XLSX files:

1. Headers are tokenised (camelCase, dotted abbreviations, `IDisPer` → item · discount · %) and
   scored against a field vocabulary. Look-alike columns are vetoed (`Mrp_Old`, `RetPrice`,
   `GstVal1`, `MfgDate`).
2. Each guess is checked against the column's values. An unnamed column full of `04/28`
   values is recognised as expiry.
3. Invoice-level columns that repeat on every row (invoice no./date, net amount, round off,
   freight, credit/debit, bill discount) are separated from line columns. A file holding
   several invoices becomes one draft per invoice.
4. A letterhead above the table supplies GSTIN, date and supplier name. An exact **GSTIN**
   match selects the supplier; a name alone is only reported.
5. **F9 Columns** shows what each supplier column was read as. A correction is remembered
   for that supplier only.

Computer-generated PDFs go through three readers: ruled tables, a word-geometry reader
(columns found from data gutters, order-preserving header alignment, wrapped names,
repeated page headers) and the legacy estimate reader. The most complete, arithmetically
consistent reading wins. Photos and scanned PDFs are refused.

**Reconciliation** is taxable value − item discount − scheme − bill discount + GST + charges ± round off.

**Exception inbox.** A purchase opens on the lines that need a person (Review / Blocked). The confidence
gate (`confidence_gate.py`) scores product, quantity, batch, expiry and the pack conversion separately; a
weak pack never hides the received quantity. Auto-accepted and warning lines are one click away (Alt+V).
Thresholds: `manage.py setting set purchase_gate_thresholds '{"auto":0.97,"warn":0.90,"review":0.75}'`.
New products prepared from the invoice wait in the queue ("Confirm N new products…") unless
`purchase_auto_create_products` is `on`.

**What is learned** (mapping store, `mapping_history` keeps every change): supplier product aliases with
trust (an overruled alias is only suggested), supplier packing aliases (Alt+K, "Save supplier alias"),
product packs (`product_packagings`), supplier invoice profiles (layout + invoice-number shape recognise
the supplier when the file has no GSTIN), column roles (F9). `manage.py purchases bootstrap` rebuilds
them from history; `manage.py purchases metrics` prints straight-through per supplier.

**Scans and photos** go through the bundled OCR (`tools/tesseract`; on Windows set `PHARMACY_TESSERACT`);
rows whose qty × rate ≠ amount wait for a person.

**Review screen keys:**
- **Enter** corrects a line; the supplier's value is kept, with who and when.
- **F4** matches a product; **Shift+F4** creates it as new.
- **Shift+↑/↓**, Space or Ctrl+A select lines. Then **F12** posts only those lines (partial receipt, status PARTIAL); **Shift+F4** creates them all as new products with one category (units detected per pack; ambiguous packs pre-filled, confirmed with **F6**); **F4** confirms matches from ranked suggestions.
- **Alt+X** cancels a draft, or on a partly received invoice closes the remaining lines as not received.
- **F7** changes only the category of the selected lines (one click per category); works on received lines.
- **Alt+B** rolls posted lines (selected, or all) back to draft: stock is reversed through the ledger.
- **Alt+K** packing correction; **Alt+J** match inspector; **Alt+V** inbox / all lines.

Optional **AI invoice reader**: when the built-in reader cannot find the core columns,
and only if `invoice_ai_enabled=1` with a key, the column headers and at most 8 sample
rows are sent to the configured Claude model. Its column mapping is still checked against
the values and flagged on the draft.

## Code map

```
app/services/  stock_ledger (ledger engine) · inventory_service · units / packaging_service / uom_service
               purchasing (staging, review, posting) · purchase_import · column_mapper · pdf_invoice · invoice_agent
               sales_service · refund_service · adjustment_service · purchase_service (returns)
               report_generator · report_document · financials · category_service · keymap_service
app/routers/   erp (shell, inventory, masters) · purchases · sales · sales_history · stock_history
               adjustments · reports · inventory (import, export, ledger APIs)
app/static/erp/ shell · core · grid (multi-select) · keys · pos · inventory · purchases · purchase
               sales · studio · history · adjustments · reports · masters · ledger · shortcuts
alembic/versions/  migrations (applied by the migration job / upgrade guard; additive only)
tests/         460+ tests incl. test_end_to_end.py, test_invoice_corpus.py, test_upgrades.py, test_appliance.py
```

## Remaining work and notes

- **Expiry** is handled through Inventory (Expiring / Expired filters), Adjustments (Expired write-off) and
  purchase returns.
- **Legacy stock has no purchase cost.** Profit & Margin counts profit only on sold lines whose batch has a
  purchase rate; the rest add to Net Revenue but not to cost or profit. Rates can be set per batch in Inventory.
- **Product disable/delete** is not in the workspace yet.
