# Purchase processing decisions

Decisions taken while building the purchase processing engine (October 2026). Each entry
says what was decided and why. Where the engine instructions did not cover a case, the
safest option was chosen and is recorded here.

## D1. Root cause of "Quantity not confirmed" (Phase 1)

**Symptom.** After editable package quantity was added, many purchase lines showed
`Received stock: Quantity not confirmed` and `Equivalent: —`.

**Cause.** Commit `e894f7b` ("Add physical quantity adjustment and equivalent purchase
units") added a client-side `equivalent(receipt)` helper in `app/static/erp/physical-units.js`:

```js
return receipt?.resolved ? `${receipt.received_base_units} …` : 'Quantity not confirmed';
```

The Received stock column was fed only by `received_base_units`, which exists only after the
*pack conversion* is resolved. Any line whose pack was ambiguous (`10x1x10`, `10X1`, `10ML57`)
therefore lost its received quantity on screen, although billed and free were known.
Commit `2d61d3d` then added an `Equivalent` column fed by `physical.equivalent`, which is empty
unless the conversion is resolved *and* the pack holds more than one unit, giving `—`.
The server already kept billed (`paid`) and free on every decision; nothing on the server
blocked the quantity. The fault was the display tying two separate facts together.

**Fix.** Received quantity and pack interpretation are separate facts.
`app/services/packaging_conversion.py` (`receipt_view`) computes, on the server:

* Received stock = billed + free, in the unit the invoice counts (`3 purchase packs` until the
  conversion is known, `3 strips` / `2 bottles` / `6 boxes` after);
* Stock equivalent = the levels below it (`20 strips, 200 capsules`, `2 × 60 mL`), or
  `Pack conversion unresolved`.

The package editor (Adjust quantity / form) is kept; only the dependency that hid the received
quantity was removed. The column is renamed **Stock equivalent** and no longer uses tablet-only
wording. Regression tests: `tests/test_received_quantity_regression.py`.

## D2. Language and stack

The engine instructions assume a Python backend; the repository already is one (FastAPI,
SQLAlchemy, Alembic, SQLite in development and PostgreSQL on the appliance). The existing
stack is kept. The frontend is plain ES modules, not React; no UI framework is added.

## D3. Reuse the existing pipeline instead of a parallel engine

Most stages already exist and are tested (column mapper with value profiles, three PDF
readers, deterministic product matching, receipt decisions, reconciliation, idempotent import
by file hash). Building a second pipeline next to them would leave two systems running in
parallel, which the instructions forbid. The new services wrap and extend the existing code;
`PurchaseProcessingEngine` is the existing `purchase_automation.import_file` → `purchasing`
flow, with the stages named in ARCHITECTURE notes below.

## D4. Accuracy before automation

The existing rule "nothing is guessed" stays: a fuzzy product suggestion is never applied
without a person, and an ambiguous pack (`10x1x10` with no supplier history) is not converted
silently. Automation is raised only with evidence that cannot be wrong in the same way twice
(GSTIN, reviewed supplier history, identical normalised names with matching strength and form).

## D5. PyMuPDF licence (pre-existing)

`pymupdf` (AGPL-3.0) was already a dependency before this work, used for the PDF invoice
readers (`purchase_import._parse_pdf`, `pdf_invoice`) and for rendering the premium invoice
(`invoice_premium`). The instructions forbid adopting AGPL packages. Ripping it out mid-task
would put the PDF readers' accuracy at risk without a corpus to prove the replacement, so it
was **not removed** in this change. It is recorded in `DEPENDENCIES.md` as a licence conflict that
needs a decision by the owner: either confirm a commercial licence, or port the readers to
`pdfplumber`/`pypdfium2` (both already installed, MIT/Apache-2.0) behind the corpus tests.
No new code in this change imports PyMuPDF.
