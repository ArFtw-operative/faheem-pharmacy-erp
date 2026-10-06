# Upgrade rehearsal: 1.9.2 (production) → 1.10.0 (Udhaar, Counter Report, manual bills apart from sales)

Done 2026-10-06 before release, with the **real Docker images** (1.9.2 built from `origin/prod` ff68a2d, 1.10.0
built from the release commit) on **PostgreSQL 17.11** (`postgres:17.11-trixie`, the image pinned in
`compose.yaml`), with the appliance's environment (`PHARMACY_PRODUCTION=1`, external migrations).

No copy of the pharmacy's own data was on the release machine, so the data was built through 1.9.2's own code:
the frozen `tests/fixtures/releases/1.9.2.db` imported with 1.9.2's `python -m app.import_sqlite` (356 rows,
43 tables, 49 history figures identical), then shop activity through 1.9.2's services: 40 products with opening
stock, 30 customers, 400 sales (cash, UPI, card, discounts, 1–4 lines), **27 manual bills** (still rows in
`sales` in 1.9.2), 15 returns, 8 voids. Totals: 431 rows in `sales` (28 of them manual), 1,015 sale lines.

## Steps (the same as the appliance's update)

1. 1.9.2 code: history fingerprint saved; `pg_dump` kept.
2. 1.10.0 image: `python -m app.production schema` → pending `f6b8d0a2c4e7` → `a8c0e2f4b6d8`.
3. `scripts/manage.py upgrade --check` → "Rehearsed 1 migration(s) on a copy: a8c0e2f4b6d8 · OK — 49 history
   figures identical after the migrations. Safe to upgrade."
4. `python -m app.production migrate` (exit 0), `schema` → current `a8c0e2f4b6d8`, `smoke` → database,
   write/read, products, invoice: ok.
5. Fingerprint again; compared with `upgrade_service.compare` and the migration's declared
   `RECONCILE_EXEMPT` / `RECONCILE_MOVED`: **no problems**.

## Result

| Figure | Before (1.9.2) | After (1.10.0) | |
|---|---|---|---|
| `sales` rows | 431 | 403 | −28 |
| `manual_bills` / `manual_bill_items` | — | 28 / 28 | +28 / +28 |
| `sale_items` rows | 1,015 | 987 | −28 |
| `sales.total` | 52,375.00 | 48,900.00 | −3,475.00 |
| `manual_bills.total` | — | 3,475.00 | **= the drop in sales** |
| `sale_items.quantity` | 12,526 | 12,471 | −55 = `manual_bill_items.quantity` 55 |
| stock ledger, batches, returns, customers, purchases, audit | | | **identical** |
| new | | | `udhaar_*`, `counter_day_closes` (empty) |

## Day one on the upgraded data (1.10.0 web on the migrated database, over HTTP)

`/health/ready` ready; owner sign-in → two-step setup → `/app`; wrong password 401. Then, 12/12 passed:
POS search finds a 1.9.2 product · inventory list with stock totals · sales history · Manual Bills screen
shows the 28 moved bills · new cash sale · return on it · Udhaar sale for a customer created in 1.9.2 ·
Udhaar part payment (25.00 → 15.00 outstanding) · Udhaar ledger · new manual bill (`MB-20261006-0028`,
numbering continues after the moved bills) · invoice of a 1.9.2 sale renders · Counter Report.

Afterwards `stock_ledger.reconcile()` → 0 problems; a restart comes back ready; the migration job run again
is a no-op.

## The Force Update path

*Force Update ERP* (no published release) takes a verified backup, then runs the newest `prod` commit's
installer with `--build-from-source`: the existing database is kept and the migration job above runs on start.
Between 1.9.2 and 1.10.0 the installer differs only in the default owner name shown at a **first** install
(`owner` / `Owner`); an existing database never gets a new owner account.

## Not covered here

The pharmacy's own data was not available; the migration reads only manual-bill rows (copied and reconciled
in the same transaction, which fails as a whole on any difference) and PostgreSQL runs it transactionally.
The force update's backup is the way back (`faheem-erp rollback` / restore, docs/UPDATE.md).
