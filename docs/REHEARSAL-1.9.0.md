# Upgrade rehearsal: 1.8.0 (production) → next release (racks, purchase engine, remote support)

Done 2026-10-04 before release (timings: medians on a development laptop), on **PostgreSQL 18** (as on the pharmacy PC), with client-like data:
the snapshot taken just before the purchase-engine deploy (schema `c9e2a5b8d1f4` = production 1.8.0):
1,006 products · 494 batches in stock · 7,555 units · 3,150 ledger movements · 15 sales · 6 users ·
6 purchases (4 drafts, 2 partly received) with 311 open draft lines.

## Steps (the same as the appliance's update)

1. Production code (`origin/prod` b7142ab, 1.8.0) imported the snapshot into an empty PostgreSQL database with
   `python -m app.import_sqlite` — 10,759 rows, 32 tables, 50 history figures identical, every relationship intact.
2. A raw-SQL fingerprint (independent of either code version) of products, batches, the stock ledger, purchases,
   every draft line including its counting decision and the person's corrections, sales, users and settings.
3. New code: `scripts/manage.py upgrade --check` (rehearses on a copy: "50 history figures identical after the
   migrations. Safe to upgrade"), `python -m app.production migrate` (2 migrations, 0.6 s), `schema` (current
   `e3a5c7e9b1d4`), `smoke` (database, write/read, products, invoice: ok).
4. The fingerprint again.

## Result

| Checked | Before | After |
|---|---|---|
| products, batches, ledger, purchases, draft lines + decisions + corrections, sales, users, settings | hash | **identical** |
| stock units / ledger sum / batches disagreeing with the ledger | 7,555 / 7,555 / 0 | **7,555 / 7,555 / 0** |
| draft lines by status (Ready 188 · Needs review 110 · Product match 10 · Corrected 3 · Posted 495) | | **identical** |
| new | | permissions +10 (rack.*, box.manage) with role grants; 36 built-in item forms; empty rack / location tables |

No product got an invented rack: every product starts Unassigned.

## Day one on the upgraded data (new app on the upgraded database)

| Action | Time | Outcome |
|---|---|---|
| Inventory list (sorted by name / by rack) | 57 / 56 ms | 1,006 products, all Unassigned at first |
| POS search "dolo" / by rack code "R-B01" | 16 / 15 ms | |
| Open each draft / partly received purchase (208, 204, 350, 43 lines) | 21–318 ms | all lines there; Rack column present |
| Create 5 racks | 13–21 ms each | |
| Move 147 products (Starts with A) to R-A01 — one request | 133 ms | 147 moved, 0 failed; POS shows `R-A01 · Antibiotics` |
| Move 78 syrups (search "syp") to R-B01 — one request | 101 ms | POS search `R-B01` lists them |
| Rack Inventory vs Current Stock | 98 / 151 ms | 7,555 = 7,555 |
| Draft NR03897: rack on its ready lines — one request | 423 ms | 146 lines set |
| Post it (after choosing the supplier, as the client does) | 10.1 s for 206 lines | posted 206; stock 7,555 → 10,086 (+2,531 = the lines' received units); 146 products placed in R-A02 by the posting |
| Afterwards | | ledger sum 10,086 = stock; 0 batches disagreeing; `stock_ledger.reconcile()` 0 problems |

The browser (Inventory sorted by rack, POS search by rack code, the purchase review with filters and the
"Accept suggested racks" chip) showed no errors.

## Remote support on update

The ERP update does not install, upgrade or start remote support (tests/test_remote_support.py
`test_erp_updates_never_touch_remote_support`). It is set up once with `faheem-support install …` and
upgraded only with `faheem-support upgrade`.

## Not covered here

The pharmacy PC's own database may hold drafts or sales made after the snapshot; the migrations are additive
and do not read business rows, so they behave the same. Before the release goes to the PC: back up
(`faheem-erp backup`, automatic before every update), and the updater rolls back by itself if the new version
is not healthy (docs/UPGRADES.md).
