# Automatic purchase intake

The existing purchase import API prepares and optionally posts complete invoices.
It runs locally using invoice fields, the ERP master, the local reference catalogue
and reviewed supplier history. There are no model calls or supplier-name rules.
No schema migration is required.

The pharmacy GSTIN is optional. An unregistered pharmacy leaves `gst_number`
blank and sets `pharmacy_state_code` in Shop details for the supplier tax split.
Supplier GSTIN and invoice tax validation still apply. A purchase/stock reset
retains reviewed packaging facts, including contradictions, independently of
the deleted documents; it does not retain invoice copies.

## Configuration

Use the existing settings command:

```
python scripts/manage.py setting set purchase_automation post
python scripts/manage.py setting set purchase_min_shelf_days 30
```

Modes are `off` (legacy review), `prepare` (automatic decisions, operator posts),
and `post` (post complete verified imports automatically). Posting still requires
the importing account's `purchase.post` permission. Short-expiry stock meeting
the configured minimum retains an informational warning; expired stock is held.

`POST /api/erp/purchases/import` is the usual multipart upload endpoint.
`POST /api/erp/purchases/{id}/prepare` re-evaluates an existing draft, without
posting it. It does not erase operator corrections. The response's
`summary.automation` reports row exceptions, document blockers, evidence coverage
and readiness for automatic posting. Coverage is **not an accuracy measurement**.

## Decision evidence

* Clear named forms and pack counts establish new-product units automatically.
  Content volume/weight never multiplies physical stock. An unspecified `10S`
  counts ten generic units, without falsely declaring them tablets or capsules.
* Catalogue matches can corroborate form and retail count. Strengths and release
  modifiers are retained; conflicting or discontinued candidates cannot decide.
  Limited combination-name abbreviations can establish a unanimous form only:
  they never match an existing medicine or fill in missing strengths.
* Nested cartons require an explicit saved conversion, an identical reviewed
  product conversion, or at least three distinct reviewed posted products from
  that supplier with the same Qty/Pack/Rate/MRP layout and no contrary examples.
  Layout convention is inference from reviewed history, not independent proof of
  every future shipment. Automatic receipts cannot reinforce this inference by
  teaching themselves. Explicit quantity-unit labels take precedence.
* Each receipt retains the factor, physical units, financial paid/free split,
  evidence sources and history IDs. `2.5 + 0.5` of ten receives thirty units.
* Confirmed supplier mappings are reused. Changed product descriptions under an
  existing supplier code are held. Repeat batches of one new product create one
  product master.
* Cross-format duplicate identification requires the same invoice number, total
  and every line's name, batch, expiry, quantity, rate and MRP. It never relies on
  similar supplier names. A corroborated duplicate can supply missing seller
  identity; only a user with supplier-management permission may enrich a missing
  supplier GSTIN from its unchanged source file. It cannot overwrite an existing
  GSTIN or receive the invoice twice.

## Posting controls

Automatic posting requires supplier, invoice number/date, batch and expiry data,
resolved physical units, rate/MRP, verified tax inputs, valid configured GSTINs,
line values within ₹0.02 of a supported printed basis, invoice tax within ₹0.02,
and the invoice total within ₹0.01. Extraction problems, identity conflicts,
unresolved packs, expired stock and duplicate invoices hold the whole document.
No unattended partial receipts or automatic acceptance of financial differences
are performed. Existing manual posting remains available under its permissions.

Retries return the existing document and may re-evaluate an unposted draft after
its evidence is improved. Posted retries do not replay inventory. Receipts are
atomic: a failure leaves the draft and rolls back its products, batches and stock
movements. Supplier-level locking serializes concurrent invoice-number checks on
PostgreSQL; SQLite serializes writes and has a unique posted-invoice constraint.

## Validation and remaining data

The test suite covers fractional/free allocations, nested history and conflicting
history, changed columns and unit labels, catalogue disagreements, retry and
duplicate protection, permission checks, missing evidence, expiry policy and
rollback after the second receipt fails. Private invoice fixtures stay outside
the repository. Evaluate new supplier formats against independently counted
deliveries before interpreting coverage as an accuracy claim.

Unknown pack text and an unidentified sale unit are data exceptions. Correcting
them once and posting the receipt teaches the supplier mapping/conversion for
later imports; a percentage target never overrides contradictory evidence.
