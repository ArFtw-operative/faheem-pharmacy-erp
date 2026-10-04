# Rack and box locations

Where every product is kept: racks, optional boxes inside racks, the product's current place,
its full history, and rack inventory on any past day. Design decisions: DECISIONS.md D25–D30.

## For the pharmacy

| Task | Where | Keys |
|---|---|---|
| Create a rack | Racks → New rack | F3 |
| Add boxes to a rack | Racks → rack → Boxes → New box (boxes must be on: Racks → Settings) | Alt+B |
| Put many products in a rack | Inventory: filter (e.g. Category, Location = Unassigned), mark (Ctrl+A / Space / Shift+↑↓), Move to rack | F8 |
| …or from the rack | Racks → rack → Add products (search, category, show Unassigned only, mark, Enter) | F4 |
| Move one product | Inventory → right-click → Move to rack / box, or the side panel's Move… | F8 |
| Take products out of a rack | the Move dialog's "Instead, take it out of …" (only offered when they are in a rack), or Racks → Products → right-click | |
| Rack for received stock | Purchase review: filters (Find, Category, Rack = Needs a rack, Form) → Mark all shown → Set rack… | Alt+S, Ctrl+A, Alt+L |
| Accept suggested racks | Purchase review | Alt+Shift+L |
| Find where a product is | POS search, Quick lookup (Ctrl+F), Inventory Rack / Box columns, side panel | |
| What is in a rack | POS / Inventory search for the rack code (`R-A03`, `R-A03/B02`); Racks → Products | |
| A rack on a past day | Racks → Snapshots (each day's close; or open any date), Reports → Rack Inventory (As of) | |
| Location history | Inventory side panel → Location history; Racks → Movement history | |
| Disable a rack | Racks → Settings → Disable rack… (asks where its products go first) | |

The rack code is what staff read on the shelf label and what POS shows first; the name may be cut
on a narrow screen, the code never.

## Settings (Racks → Settings, `rack.edit`)

| Setting | Default | Effect |
|---|---|---|
| Box / bin locations inside racks | off | boxes can be created and chosen |
| A box is required when boxes are on | off | every assignment must name a box |
| Received stock must have a rack before a purchase posts | off | posting refuses lines whose product has no rack and none was set on the line |
| Show rack and box in POS search and the bill | on | |
| POS search finds products by rack / box code | on | `R-A03` lists that rack's products after the name matches |

Each rack can be the preferred rack for one or more categories (Settings tab, drop-down): a
suggestion when stock of those categories arrives, never applied by itself.

## Permissions

`rack.view` (see locations; POS staff have it) · `rack.create` · `rack.edit` · `rack.disable` ·
`rack.assign` (move one product) · `rack.bulk_move` (move several) · `rack.history.view` ·
`rack.report.view` · `rack.snapshot.view` · `box.manage`. Administrator and Manager have all;
Pharmacist: view, assign, bulk move, history, reports, snapshots; Accountant: view, history,
reports, snapshots; Sales Staff and Counter Manager: view.

## Data model

```
racks (id, code unique, name, description, is_active, sort_order, zone, shelf_count, capacity, created/updated by/at)
rack_boxes (id, rack_id → racks RESTRICT, code unique per rack, name, …)  unique (rack_id, id) for composite keys
item_locations (item_id, batch_id NULL, rack_id, box_id, quantity NULL, valid_from, valid_to NULL = current, event_id)
    (rack_id, box_id) → rack_boxes (rack_id, id)        one open row per product (partial unique index)
location_events (operation_id, event_type ASSIGNED|MOVED|BOX_CHANGED|UNASSIGNED, source MANUAL|BULK|PURCHASE|IMPORT|RACK|MIGRATION,
    item_id, batch_id, from/to rack and box ids, from/to labels of the time, stock_snapshot, reason, reference, user, created_at)
categories.default_rack_id · purchase_items.rack_id / box_id (confirmed only)
```

Service: `app/services/location_service.py` (every write goes through it). API: `app/routers/locations.py`
(`/api/erp/racks…`, `/api/erp/boxes…`, `/api/erp/locations/assign` — one request for any number of
products, returns requested / processed / skipped / failed with reasons). Reports:
`app/services/location_report.py` (Rack Inventory, Rack History). Screens: `static/erp/racks.js`,
`static/erp/locations.js` (picker, move), Inventory, POS, Purchase review.

Import: inventory sheets may carry `Rack Code` / `Box Code`. An exact, active code is applied; an
unknown one (a typo such as `R-AO1`) is never created — the row imports, the location is left for
review with the closest existing code offered. Export: `rack_code, rack_name, box_code, box_name`.

## Reconciliation

Rack Inventory (any day) = per batch, the ledger sum up to that moment, grouped by the location
valid then; stock without a location is "Unassigned". Σ racks + Unassigned = Current Stock total
(tests/test_locations_api.py::test_rack_report_reconciles_with_current_stock).

## Performance (scripts/perf_locations.py, SQLite, one laptop core)

10,000 products · 50,000 batches · 100 racks · 500 boxes, every product located:

| Measure | Median |
|---|---|
| POS search (per query) without / with locations | 21 / 23 ms |
| Bulk move 500 products, one request | 247 ms |
| Inventory list, location filter, 200 rows | 56 ms |
| Inventory list sorted by rack | 61 ms |
| Rack report, one rack, today / as of a past day | 193 / 13 ms |
| Rack report, all racks (50,000 rows) | 1.7 s |
| Rack timeline, 30 days | 25 ms |
| Racks list with statistics (100 racks) | 148 ms |

No cache is used: the rack master is a primary-key join and the current location is one indexed
query per screen (DECISIONS D28).

## Upgrade

Migration `e3a5c7e9b1d4` is additive: new tables, two nullable columns, permissions. Existing
products start Unassigned (or keep a legacy typed rack as a real one); stock, purchases, drafts
and history are untouched. Downgrade drops only the new structures. Rehearsed on SQLite and on
PostgreSQL 18 (upgrade → downgrade → upgrade).
