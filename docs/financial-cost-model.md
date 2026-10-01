# Financial cost pipeline and remediation

The live investigation found one ₹1,139 invoice, six allocations, and six zero cost snapshots. All six batches came from opening-stock imports without acquisition rates. The database contained no purchase invoices or invoice lines. Therefore their actual acquisition costs cannot be reconstructed safely from this database.

## Dependency map

```text
PurchaseItem.rate / verified opening-stock acquisition rate
  -> Batch.purchase_rate (purchase pack) + Batch.units_per_pack
  -> Batch.unit_cost (stock unit, six decimal places) + cost_status
  -> stock_ledger.allocate (FEFO or explicit selected batch)
  -> SaleItem (one row per actual batch allocation)
     cost_rate, pack_mrp, units_per_pack, quantity, sale UOM/factor
     financial_status, financial_cost_source, net_sale_value, line_cost
  -> InventoryMovement financial snapshot (signed cost_amount)
  -> financials.facts / financials.aggregate
  -> Profit & Margin, Item-wise Sales cost/profit (on request), exports
```

The legacy `SaleItem.rate` is a selling-price field and remains compatible with existing bills. Acquisition Rate is `Batch.purchase_rate`; sale acquisition cost is `SaleItem.cost_rate`. Reports never substitute current item or batch rates for historical snapshots. A cashier line spanning batches already becomes several SaleItem rows with a shared line number; no duplicate allocation table is needed.

## Units, decimals and rounding

Existing item/batch purchase-pack and base-unit configuration is authoritative. Product size text such as `200 ml` is not a conversion factor. Non-loose bottles/tubes remain indivisible; products configured in tablets can permit loose quantities or require full strips through the existing quantity guard.

Optional item UOM hierarchies are stored in `item_uoms`. `BOX -> 10 STRIP -> 10 TABLET` must agree with the product's purchase-pack/base configuration. Cycles, unknown parents and inconsistent purchase-pack factors are rejected. `/api/erp/inventory/{item_id}/uoms` accepts the hierarchy; sale inputs can include a configured `uom`. Every allocation retains its sale UOM, factor, stock quantity and sale quantity.

Cost conversion uses Decimal and ROUND_HALF_UP at six decimal places. Money rounds to paise through the existing `money` function. Each allocation captures rounded line cost and allocated final revenue, including bill discounts, vouchers and rounding. Partial return cost reversals use cumulative allocation of the rounded original line cost, so a full return reverses that exact cost. Margin is total profit divided by total revenue, never an average of percentages.

## Missing costs and verified zero

Zero/default acquisition rate is `COST_MISSING`. Genuine free acquisition requires explicit `COST_ZERO_CONFIRMED` verification with a reason. A sale can continue with an explicit missing-cost snapshot; COGS, profit and margin remain unavailable for affected reports. Set `require_sale_cost=true` to require known costs and roll back the whole sale if any allocation is unresolved. All sale creation, allocations, ledger postings and snapshots share the existing transaction.

Inventory's **Verify cost** action updates only future cost basis and records an audit event. Profit & Margin's **Review missing costs** action verifies a historical pack cost using the original sale conversion. It retains the raw original `cost_rate`, stores a reconstructed cost separately, records provenance, and repairs missing ledger financial fields. Resolved historical costs cannot be overwritten through this workflow.

## Profit basis and returns

The default is actual realized sale value. Profit & Margin provides an explicit **Profit basis** control for actual value or MRP margin. Backend setting `profit_basis` can be `realized` or `mrp`. Both concepts remain available in the domain facts.

Sales Summary includes eligible refunds on their processing date, so its net sales reconcile to realized Profit & Margin for identical period/filters. Bill Register retains original document values. Sale returns reverse the original sale's cost; unsellable returned goods produce a separate **Unsellable Return Loss**. **Profit after Return Loss** deducts that loss once. Standing stock write-offs (ADJ documents that were not reversed) are reported as stock loss. Purchase returns debit remaining stock without rewriting previous sales. Voided invoices are excluded and their stock/cost movements reverse transactionally.

## Backfill and diagnostic APIs

The migration adds quality/provenance and financial snapshot fields; it does not guess costs. The backfill first uses an existing positive sale snapshot or explicitly resolved zero snapshot, then original sale-ledger financial evidence, then a matching purchase receipt/invoice predating the sale. Conflicting evidence becomes ambiguous. Today's item/batch rates are never a historical fallback.

```sh
.venv/bin/python scripts/backfill-sale-costs.py --output /tmp/cost-review.json
.venv/bin/python scripts/backfill-sale-costs.py --apply --output /tmp/cost-applied.json
```

The command defaults to review only, is idempotent, preserves original cost_rate, and records reconstruction time/version/source separately. Applied runs create an audit entry. A pre-migration SQLite backup and isolated-copy migration validation protect the live database.

All financial endpoints require `reports.financials`:

- `GET /api/reports/profit-margin?from=YYYY-MM-DD&to=YYYY-MM-DD&groupBy=day`
- `GET /api/reports/item-profitability?from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /api/reports/cost-issues` (optional period/product/category/brand/batch filters)
- `POST /api/reports/cost-issues/{sale_line_id}/resolve` with original `purchase_rate`, verification `reason`, and explicit `confirm_zero` for free acquisition

Batch verification additionally requires inventory editing and purchase access:

- `POST /api/erp/inventory/{item_id}/batches/{batch_id}/cost`

The repair endpoints require verified operator input; they do not infer historical acquisition cost from selling price or MRP.

## Validation

`tests/test_financial_engine.py` covers packs, quantity, loose tablets, indivisible bottles, configured hierarchies, batch allocation, discounts, missing and genuine-zero cost, snapshot immutability, fractional partial returns, audited backfill, mandatory-cost transaction rollback, purchase returns, void reversals, financial API permissions and net sales reconciliation. `scripts/test-financials-browser.mjs` checks Inventory verification, missing-cost output, historical repair and unchanged prior COGS after later rate changes on an isolated database.
