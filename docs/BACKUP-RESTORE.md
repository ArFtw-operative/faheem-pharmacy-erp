# Backups and restore

## What a backup is

A **snapshot** of the whole ERP, written by the application's own tool into
`/var/backups/faheem-erp/snapshots/<date-time-reason>/`:

- `database.dump` — PostgreSQL dump taken from one exported snapshot of the database;
- a **fingerprint** of the business history from the same instant (counts, money and quantity
  totals, statuses, stock-ledger balance);
- `uploads.tar.gz` — logo, supplier bills and other uploaded files;
- `manifest.json` — SHA-256 of every file, app version, schema revision.

It counts only after it has been verified, and is then made read-only. Next to the snapshots,
`host-config/faheem.env` (root-only) keeps the PC's configuration: its secret key also unlocks the
stored two-step sign-in secrets, so it is needed to rebuild on a new PC.

## When

- every morning at the maintenance time (05:00 by default);
- before every update, rollback, restore and import;
- whenever you ask: desktop **Back up ERP**, Control Center → Backups, `sudo faheem-erp backup --note "…"`.

Retention: `backup-days` (30) but never fewer than `backup-keep` (14) snapshots — Control Center →
Settings. Only the daily store is pruned; nothing else deletes a snapshot.

## Copies off the PC

A backup on the same disk does not survive a dead disk or theft. Copy
`/var/backups/faheem-erp` regularly to a USB drive or another computer, e.g.

```bash
sudo rsync -a /var/backups/faheem-erp/ /media/<user>/<usb-drive>/faheem-erp-backups/
```

Keep the copy private: it holds customer data and the configuration secrets.

## Checking a backup restores

`sudo faheem-erp drill [ID]` (Control Center → Backups → Prove…) restores a snapshot into a scratch
database, compares the fingerprint and drops the scratch database. Live data is not touched.

## Restore

`sudo faheem-erp restore` lists the snapshots; `sudo faheem-erp restore <id>` (or Control Center →
Backups → Restore) then:

1. verifies the snapshot (checksums, fingerprint) — if it fails, nothing changes;
2. asks for the typed word `RESTORE`;
3. stops web and worker (no bill can be written meanwhile);
4. snapshots the current state (`pre-restore`) so the restore itself can be undone;
5. replaces the database in a single transaction and restores the uploads;
6. starts the ERP; the migration job brings an older snapshot up to this release's schema.

Everything entered after the snapshot was taken is replaced. A PostgreSQL snapshot is never restored
into SQLite or the other way round.

## Moving to a new PC

1. Install the appliance on the new PC ([INSTALL.md](INSTALL.md)).
2. Copy the old PC's `/var/backups/faheem-erp/` to the same place on the new PC.
3. Put the old `host-config/faheem.env` values `PHARMACY_SECRET_KEY` into the new
   `/etc/faheem-erp/faheem.env` (keeps two-step sign-in working), then
   `sudo faheem-erp restore <newest id>`.
