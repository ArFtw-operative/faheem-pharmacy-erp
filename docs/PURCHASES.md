# Purchases

The Purchases module (Alt+U) is the only way stock is received. The full design is in
[HANDOFF.md](HANDOFF.md#purchases-how-supplier-files-are-understood).

**Flow:** upload a supplier file (CSV, XLS, XLSX or computer-generated PDF) → raw lines are staged
exactly as written → normalised and validated → products matched deterministically
(supplier mapping → product code → exact name) → review and correction → **post** (atomic,
PUR-000001) → batches and `PURCHASE_RECEIPT` ledger movements.

**Line statuses:** Ready · Corrected · Needs review · Product? · Invalid · Posted · Not received.

**Invoice statuses:** Draft · Partly posted (selected lines received) · Posted · Cancelled.

**Suppliers** carry a code, GSTIN, contact, payment terms and credit days, plus two learned
things: product mappings (from confirmed matches) and a column layout (from F9 corrections).

**Purchase returns** (PR-000001) take stock out of a chosen batch; their value defaults to that
batch's purchase rate.

## GST

One engine (`app/services/gst.py`) handles every supplier and file layout. Sales never add GST
(MRP includes it); purchases carry it.

| Step | Calculation |
|---|---|
| Taxable (line) | qty × rate − item discount − scheme discount |
| Taxable (net) | × (1 − bill discount %) — bill discount is before tax |
| GST | taxable (net) × GST %, rounded per line; **CGST + SGST** (halves that add up exactly) when supplier and pharmacy GSTINs share a state, **IGST** otherwise |
| Rate incl. GST | (taxable + GST) ÷ packs received (paid + free; scheme splits like 2.5 + 0.5 included) |

**Stock cost.** Stock received is costed at the rate *including* GST (`purchase_cost_includes_gst`,
default `true`). A shop that claims input-tax credit sets it to `false` and stock is costed before GST.
Each batch records its basis (`INCL_GST` / `EXCL_GST`); batches received before 1.1.0 keep an empty
basis and are never re-costed.

**Where the GST % comes from.** The file's GST % column → worked out from a GST-amount column
(“Tax Amt”, “GST Amount”) → otherwise the line needs review (never silently 0). A file with no GST at
all is taken as 0% with a note on each line.

**Checks** (each message states the figures and how to fix it):

| Check | Level |
|---|---|
| GST % missing on a line while other lines show GST | Needs review |
| GST value that is not a percentage | Needs review |
| Withdrawn slab (12% / 28%) on an invoice after `gst_rates_changed_on` | Warning (F6 accepts) |
| Rate differs from the product's last posted purchase | Warning (F6) |
| One HSN at two rates on the same invoice | Warning (F6) |
| Printed line GST amount ≠ GST % × taxable | Warning (F6) |
| Invoice GST total (e.g. Marg `SumGst`) ≠ sum of lines | GST panel (F10) |
| Pharmacy / supplier GSTIN missing or failing its check digit | GST panel (F10) |

**Settings:** `gst_number` (the pharmacy's GSTIN), `gst_rates` (default `0,0.25,3,5,18,40`),
`gst_rates_before` (`0,0.25,3,5,12,18,28`), `gst_rates_changed_on` (`2025-09-22`),
`purchase_cost_includes_gst`.

**Report:** Reports → Purchase Reports → **Purchase GST** — by invoice line, invoice, product, GST
rate, HSN, supplier or month; returns to suppliers reverse their GST.
