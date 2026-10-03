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

## D6. Roll back a posted purchase to draft

Requested by the pharmacy: posted lines entered wrongly could only be corrected one product
at a time. `purchasing.rollback` (Alt+B, needs `purchase.post`) takes chosen posted lines — or
the whole purchase — out of stock with linked `RECEIPT_REVERSAL` movements (the ledger is
never edited or deleted) and returns them to review with every correction kept. It is all or
nothing: if any of that stock was already sold, returned or adjusted, nothing changes and the
message says to use a purchase return. A line with a purchase return is refused. The
reference number is kept, so posting again reuses it; products created by the receipt stay.

## D7. Change category only

F7 on selected lines (or the current one) changes only the category of their products; a
category is applied with one click or its number key. Matched products change in the product
master (needs `inventory.edit`); products still to be created take it when created. Stock is
not touched, so received lines can be re-categorised too. Categories must exist in the master
(Masters → Categories adds them).

## D8. Item forms are data

The hard-coded form list became the `item_forms` master: 36 built-in forms (softgels, IV
fluids, ampoules, syringes, needles, cannulas, test strips, respules …) plus the pharmacy's
own. Every form list ends with “Create new form…”. A custom form's code is accepted as a
dosage form wherever product packaging is validated (`units.register_forms`).

## D9. Ledger levels

Receipt movements keep purchase, retail and base quantities beside the signed base quantity
(`purchase_quantity`, `purchase_uom`, `retail_quantity`, `retail_uom`, `base_uom`). The base
quantity stays the transactional number every report already uses; the levels are additional
facts, nullable for older rows. A reversal mirrors the levels it undoes.

## D10. Stock unit versus content

The parser's “base” level is the unit stock is counted in (tablet, bottle, tube, piece).
Content (60 mL, 20 g, 200 doses) is reported separately and never becomes stock: an
OMNIGEL 20 GM receipt of 3 is 3 tubes, “3 × 20 g”. This keeps every existing product's
stock unit unchanged.

## D11. What is applied automatically, and what only suggests

Applied without a person: saved supplier aliases (unless overruled), supplier item code /
GS1 GTIN, an exact name, a normalised name (punctuation, spacing, TAB/TABLET, GM/G — never
strength or release markers) when exactly one product has it, a confirmed supplier packing
alias, reviewed supplier history. Only suggested, with an explanation: fuzzy name scores
(RapidFuzz with attribute signals). Rationale: zero wrong auto-accepts outranks automation.
On the dev database's 495 posted lines the normalised matcher chose the person's product 469
times, declined 26 and was never wrong.

## D12. Automatic product creation is off by default

The engine instructions set auto-creation of products OFF by default. The earlier automation
created new products from unmatched lines in `post` mode. The corpus showed why that is
unsafe: “OKACET SYRUP” (size in the pack column) was proposed as a new product although
“OKACET SYRUP 60ML” exists. Now an automatically prepared new product waits in the
new-product queue (`new_product_unconfirmed`, Shift+F4 confirms with the prepared units).
The previous behaviour stays available: `manage.py setting set purchase_auto_create_products on`.
The matcher also reads a size printed in the pack column as part of the name.

## D13. Supplier recognition needs two signals

GSTIN or the exact printed name identify a supplier. Otherwise a learned invoice profile
assigns the supplier only when the file layout *and* the invoice-number shape (`NR#####`)
both point to exactly one supplier — many distributors export the same billing-software
layout, so a layout alone only produces a suggestion. Assigned this way, the confidence is
0.96: accepted, shown with a warning.

## D14. Confidence thresholds

Starting thresholds (auto 0.97, warning 0.90, review 0.75) were kept after calibration on
the corpus: with them the corpus has zero wrong auto-accepts and recurring known-supplier
lines pass at 100%. They are settings (`purchase_gate_thresholds`), not constants. Missing
batch/expiry (common on FMCG receipts) is a warning (0.92), not a block; a damaged batch, an
invalid or past expiry, or an unreadable quantity blocks.

## D15. OCR without new heavy dependencies

Scanned PDFs and photos use the bundled Tesseract 5.5 CLI (Apache-2.0) with word boxes,
pypdfium2 (already installed) to render pages and Pillow to clean them. OpenCV, PaddleOCR and
Docling were not needed for the corpus. A PDF is a scan when its own text layer holds fewer
than `PHARMACY_PDF_TEXT_MIN_CHARS` characters per page (default 25); a text PDF is never
OCR'd. Each OCR row keeps its confidence and whether qty × rate = amount; a row that does
not reconcile goes to review until a person checks it. The real photographed receipt on the
development machine (git-ignored; curved paper, a cropped total column on one page) reconciles on 51 of 102 rows:
the others correctly wait for a person.

## D16. Partial automatic posting is not enabled

Accepted lines of an invoice that also has review lines are not posted on their own: the
existing whole-invoice unattended post (mode `post`, every row resolved and the invoice total
reconciled) is kept. A person posts the accepted lines with one key (select, F12) after
looking at the exceptions. Posting part of an invoice automatically would leave PARTIAL
invoices nobody opened.

## D17. Regression corpus is synthetic

The repository is public, so real supplier invoices are never committed. `tests/purchase_corpus.py`
models the real layouts (Marg CSV, Excel, computer PDF, photographed receipt) with expected
results per line and runs in the build (`tests/test_purchase_corpus.py`). Private files can be
staged locally with `PHARMACY_SUPPLIER_CORPUS_DIR=… python scripts/purchase_corpus.py --private`.
