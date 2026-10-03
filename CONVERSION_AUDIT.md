# Conversion audit

Every place that converts quantities, units or per-unit prices, and what calls what after
the consolidation (October 2026).

## The conversion service

The PackagingConversionService has two layers that together form one service. Nothing
outside them multiplies or divides by a pack size.

| Layer | Module | Owns |
|---|---|---|
| Arithmetic | `app/services/units.py` | base units ⇄ packs (`to_base`, `base_quantity`, `split_packs`), cashier expressions (`parse_qty_expression`), sale-quantity rules, MRP per base unit (`unit_price`, `display_unit_price`, `line_amount`), cost per base unit (`unit_cost`), printed-pack reading (`parse_pack`), expiry month policy |
| Packaging | `app/services/packaging_parser.py` | the structured pack hierarchy (purchase → retail → base, content) used by the resolver |
| Display / receipt | `app/services/packaging_conversion.py` | received stock (billed + free in invoice units), stock equivalent, content notes (2 × 60 mL), per-unit costs of a purchase line |

The browser keeps a live-preview mirror of four `units.py` functions in
`app/static/erp/core.js` (`parseQty`, `describe`, `toBase`, `lineAmount`) so typing a
quantity shows its effect without a round trip. The server recalculates and decides every
stored value. `tests/test_conversion_parity.py` runs the browser functions under Node and
fails if they disagree with `units.py`.

## Call sites

| Area | Location | Conversion | Status |
|---|---|---|---|
| Purchase import / review | `routers/purchases.py` `_line_view` | received stock, stock equivalent, per-unit costs | `packaging_conversion.receipt_view`, `unit_costs` (was: JS `equivalent()` and JS cost maths) |
| Purchase review (screen) | `static/erp/purchase.js` | Received stock / Stock equivalent / cost per pack, per tablet | displays server values; local maths removed |
| Purchase review (package editor) | `static/erp/physical-units.js` | preview of (paid + free) × count | `core.toBase` (was inline maths); `equivalent()` removed |
| Receiving (receipt decision) | `services/receipt_decision.py` `decide` | invoice units × units per invoice unit → base units | the receipt rule itself; the factor comes from evidence, the multiplication is exact `Decimal` |
| Receiving (posting) | `services/purchasing.py` `post` → `inventory_service.add_or_update_batch` → `stock_ledger.receive` | packs → base units | `units.to_base` |
| Purchase validation messages | `services/purchasing.py` `_normalise` | part packs → units | `units.base_quantity` (was `received * upp`) |
| Purchase cost per pack | `services/purchasing.py` `_cost_per_pack`, `gst.line_breakdown` | value ÷ received packs | money division, packs from the receipt decision |
| Inventory | `routers/erp.py`, `routers/inventory.py`, `inventory_service.describe_stock` | stock → strips + tablets; unit MRP | `units.describe_stock`, `units.display_unit_price` |
| Inventory (packaging editor) | `static/erp/inventory.js` | stock after a pack change | `core.toBase` (was inline) |
| Masters (UOM editor) | `static/erp/masters.js` | stock after a pack change | `core.toBase` (was inline) |
| POS | `services/sales_service.py` | cashier quantity, sale rules, line MRP | `units.parse_qty_expression`, `check_sale_quantity`, `line_amount`, `display_unit_price` |
| POS (screen) | `static/erp/pos.js` | FEFO preview amount | `core.lineAmount` (was inline `pack_mrp * qty / upp`) |
| Sales returns | `services/refund_service.py` | refund value ÷ sold qty | value apportionment (money), quantities stay in base units |
| Purchase returns | `services/purchase_service.py`, `routers/purchases.py` | return value of base units | `units.line_amount` |
| Stock adjustment | `routers/adjustments.py`, `adjustment_service.py` | cashier expression → base units | `units.parse_qty_expression` |
| Repack (UOM change) | `services/packaging_service.convert` | packs → base units through the ledger | REPACK_OUT/IN movements of `q × upp` |
| Batch tracking | `stock_ledger.sync_unit_prices` | pack MRP / cost → per base unit | `units.unit_price`, `units.unit_cost` |
| Profit / financials | `services/financials.py` | cost per base unit, gross value | `units.unit_cost`, `units.line_amount` (was local formulas) |
| Reports | `report_extra.py` non-moving MRP value, item-wise packs | MRP of stock, packs | `units.line_amount` (was inline), `units.describe_stock` |
| Invoice preview sample | `invoice_premium.py` | demo line amounts | `units.line_amount` (was inline) |
| Expiry tracking | `units.is_expired` | month policy | single definition |
| Supplier / product history | `purchase_audit_report.py` | received units ÷ retail packs | reads the stored receipt decision |
| Medicine reference evidence | `medicine_reference.py` | candidate counts of a printed pack | uses `units.parse_pack` levels |

## Ledger

`inventory_movements` stores the signed base quantity plus the document's own
quantity and unit (`txn_quantity`, `txn_unit`) and the pack snapshot (`units_per_pack`).
Purchase receipts also record the purchase, retail and base levels (see DECISIONS.md D9).
