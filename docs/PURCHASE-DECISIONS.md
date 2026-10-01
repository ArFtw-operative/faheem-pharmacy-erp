# Local purchase decisions

The purchase API interprets supplier files locally. The decision engine calls no
external AI service. Its answers are based on source fields, product definitions,
exact decimal arithmetic, and supplier conventions confirmed during review.
Uncertain interpretations stop at review. An arbitrary PDF or an incomplete
invoice cannot be guaranteed correct by software alone.

## Quantities and inventory

Each purchase line exposes a `receipt` object in the existing purchase-detail API.

| Invoice | Confirmed unit | Inventory effect |
|---|---|---|
| 2.5 paid + 0.5 free | strip of 10 tablets | 30 tablets |
| 2.5 paid + 0.5 free | strip of 15 tablets | 45 tablets |
| 2.5 paid + 0.5 free | bottle of 200 mL | 3 bottles |
| 2.5 paid + 1 free | strip of 10 tablets | 35 tablets |
| 1.5 paid | indivisible bottle | review; no stock |
| Qty 2, pack 20X10 | unknown box/strip basis | review; no stock |
| Qty 2, pack 20X10 | 200 tablets per invoiced box | 400 tablets |

Paid/free quantities remain exact decimals. Only the physical total must be a
whole number of base units. For a 15-tablet strip, 37.5 billed-equivalent tablets
and 7.5 free-equivalent tablets describe financial allocation, not broken tablets.
Such a receipt writes 45 physical units in one ledger entry and preserves the
financial split in its decision snapshot. When both allocations are whole units,
paid and free movements are recorded separately.

Content such as 200 mL or 30 g is metadata. It does not multiply stock. A change
from a 100 mL to a 200 mL variant requires the correct product, even if both are
counted as bottles. Negative quantities are held for the existing purchase-return
workflow; they are never converted into positive receipts.

## API and review screen

Existing upload, manual JSON line entry, correction, partial receipt and posting
endpoints continue to work. Purchase detail includes `receipt` on every line.
`GET /api/erp/purchases/{id}/decisions` returns decisions and validation findings.

Verify an ambiguous transaction unit through the review-screen **Verify invoice
unit** button or:

```http
PUT /api/erp/purchases/123/lines/456/invoice-unit
Content-Type: application/json

{
  "units_per_invoice_unit": 200,
  "mrp_basis": "INVOICE_UNIT",
  "reason": "Supplier confirmed one box has 20 strips of 10 tablets"
}
```

`mrp_basis` is `MASTER_PACK` (retail strip/pack), `INVOICE_UNIT` (the box or unit
being billed), or `BASE` (one tablet/piece). It is explicit because an invoice
can bill boxes while printing strip MRP. Purchase value is spread over actual
received units and converted back to the master's pack for existing batch costs.
The supplier's original rate and MRP remain on the purchase line.

Verification uses the existing `purchase.create` permission. Posting still
requires `purchase.post`, a supplier and invoice number, resolved lines, and the
existing total reconciliation. Read endpoints use `purchase.view`. Source cells
are untrusted data, not executable instructions.

After a verified line posts, the convention is remembered for its supplier,
product, supplier description/code, manufacturer, literal pack text, mapped
layout, master base unit and master conversion. A change invalidates reuse.
Deleting the existing supplier mapping also deletes its conversion memory.
Changing financial values clears previously accepted warnings. Posted decisions
are snapshots and are not recalculated by later edits to the product master.

## Medicine reference data

Load the supplied reference files on the ERP host:

```bash
python scripts/import_medicine_reference.py \
  --file /path/updated_indian_medicine_data.csv \
  --file /path/medicine.csv
```

This creates `PHARMACY_DATA_DIR/medicine-reference.sqlite`. It does not create ERP
products, stock, or purchases. It stores name, manufacturer, packaging,
discontinued flag and source location only; clinical descriptions, indications,
substitutes and dosing instructions are not used for inventory identity.
Reference rows with the same name but different packs/manufacturers remain distinct.
Reimporting a source filename atomically replaces that source's older entries.
Keep source filenames unique and retain the printed SHA-256 import receipts.
The reference file must be included in the host's data-directory backups.

`GET /api/erp/medicine-reference?q=...` and the **Medicine reference** button return
ranked candidates with source locations. Fuzzy candidates never bind products.
An exact normalized name with one agreed manufacturer/pack can supply a missing
pack when the operator confirms a new product. Conflicting, discontinued or
unparseable references cannot supply it. Existing invoice packs take precedence;
reference data is not proof that a supplier delivered a particular pack variant.

## Input handling and limits

- CSV, TSV, XLS/XLSX and text PDFs continue through the shared column mapper.
- Every row retains its original source columns, including unused PTR/PTS/HSN
  columns. Supplier names and separate invoice identities are retained.
- Malformed/negative/nonfinite quantities, duplicated embedded and separate free
  quantities, nonintegral physical totals, invalid numeric values, invalid HSN
  shape and conflicting packs cannot silently post.
- Equal product names with multiple variants do not select the first database row.
- PDF printed line counts are checked when available. A mismatch cannot be
  bypassed by accepting a monetary difference. Original PDF invoice suffixes are
  preserved. Printed discount amounts are evidence, not assumed percentages.
- Scanned/image-only PDFs still require verified text/OCR extraction or manual
  entry. Unsupported layouts fail for review; arbitrary PDF extraction and OCR
  are not claimed to have universal accuracy.

The existing legacy advisor module remains in the codebase, but purchase imports
do not invoke it. Existing vocabulary and supplier column corrections provide
local customization. Supplier-specific names are not embedded in the engine.

## Validation and deployment

Run `python -m pytest -q`. The added tests cover fractional schemes, box/strip
conversions, unit costs, MRP basis, pack conflicts, scoped memory, stale matches,
input corruption, catalogue references, permissions and repeated posting.
Migration `c9e2a5b8d1f4` adds nullable JSON evidence columns only. It does not
recalculate historical stock or prices. Apply the normal ERP migration and
deployment process to a staging installation before promoting `dev` to `prod`.

To audit private supplier files without creating purchases:

```bash
python scripts/audit_supplier_inputs.py --file /path/invoice.csv \
  --file /path/invoice.pdf --output supplier-audit.json
```

The API uses the existing FastAPI upload and authorization flow; framework upload
behavior is documented at https://fastapi.tiangolo.com/tutorial/request-files/.
