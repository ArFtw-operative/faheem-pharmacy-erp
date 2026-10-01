# Migration policy

A pharmacy must go from one release to any later one and keep its whole business history, with
nothing re-entered. These rules make that true; the test suite enforces them.

- **Versioned migrations only.** Every schema change is an Alembic migration in `alembic/versions/`
  and must run on PostgreSQL and SQLite. Nothing resets or recreates a production database. (A brand-new
  PostgreSQL database is created from the models and stamped at head; the oldest migrations were
  written for SQLite databases already in the field.)
- **Additive by default.** Add tables and columns; keep old columns readable. `drop_table`,
  `drop_column`, `rename_table`, and `DELETE` / `UPDATE` of existing rows fail
  `test_new_migrations_are_not_destructive`, unless a `DESTRUCTIVE_APPROVED` comment explains why and a
  `RECONCILE_EXEMPT` entry names each history figure that changes on purpose.
- **History is never recalculated.** A new business rule applies to new documents only. Invoices,
  purchases, batches, stock movements, cost snapshots, refunds and the audit trail keep the values
  they were written with.
- **Proved on every release's data.** `tests/fixtures/releases/<version>.db` freezes a synthetic shop
  database at each release's schema (never regenerated). `tests/test_upgrades.py` upgrades every one to
  head and requires the history fingerprint to be identical; `tests/test_snapshot_pg.py` does the same
  through the SQLite → PostgreSQL import.
- **No downgrades.** `downgrade()` raises. Going back means restoring the pre-upgrade snapshot, which
  every update takes ([UPDATE.md](UPDATE.md)).

## Where the guard runs

| | |
|---|---|
| Appliance (PostgreSQL) | `faheem-erp update` rehearses the migrations on a scratch copy (`manage.py upgrade --check`), takes a verified snapshot, runs the migration job, checks health and the smoke test, and rolls back on any failure. |
| Development (SQLite) | start-up runs `upgrade_service.upgrade()`: snapshot → fingerprint → migrate → reconcile → automatic restore on a difference. Outcomes are recorded in the `deployment_log` table. |
