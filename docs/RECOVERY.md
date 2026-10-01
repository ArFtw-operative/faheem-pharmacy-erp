# When something goes wrong

Start with **ERP Doctor** on the desktop (or `sudo faheem-erp doctor`). It reads the live state and
the recent logs, names the cause, and offers the matching fix.

| Situation | What to do |
|---|---|
| Counter screen stays on "Starting services…" | wait 3 minutes after a boot; then ERP Doctor. Common causes it reports: Docker not running, database not ready, disk full. |
| The ERP stopped working after an update | updates roll back by themselves; Control Center → Status shows the last update's result. If the new version runs but misbehaves: `sudo faheem-erp rollback` ([UPDATE.md](UPDATE.md)). |
| Data entered wrongly / deleted by mistake | documents are corrected with returns, voids and adjustments in the ERP. Only if that is impossible: restore a backup from before the mistake ([BACKUP-RESTORE.md](BACKUP-RESTORE.md)) — later entries are lost. |
| Disk full | doctor → fix `cleanup` (old images, journal, caches; backups are not touched). Then copy old backups off the PC and lower `backup-days`. |
| Slow | doctor shows memory, load, long queries and dead rows; Control Center → Maintenance → optimise database / clear caches. |
| Forgotten password | login page → *Forgot password?* with the authenticator code or a recovery code. Or an administrator: `sudo faheem-erp user passwd <user>`. |
| Lost phone (authenticator) | sign in with a recovery code; or an administrator runs `sudo faheem-erp user reset-2fa <user>` — the user sets it up again at the next sign-in. |
| Account locked (5 wrong passwords) | wait 15 minutes, or `sudo faheem-erp user unlock <user>`. |
| Other devices cannot connect | `sudo faheem-erp lan status`. The PC's address changed → `sudo faheem-erp lan enable` (new certificate address) and fix the address ([INSTALL.md §5](INSTALL.md)). Browser warning → install the certificate on that device. |
| WhatsApp invoices not sent | ERP → Settings → WhatsApp shows the session state; re-link the phone if it says so. Messages wait in the queue meanwhile. |
| Updates stopped arriving | doctor: "Could not reach GHCR" → internet; for private images also the registry token (revoked/expired → run the installer again with a new one). |
| PC dead or replaced | new PC + backups: [BACKUP-RESTORE.md → Moving to a new PC](BACKUP-RESTORE.md). |
| Need help | Control Center → Support report: writes `/var/log/faheem-erp/support-<time>.txt` (no secrets) to send to support. |

Logs: Control Center → Logs, or `sudo faheem-erp logs web -f`; host operations in
`/var/log/faheem-erp/deploy.log`.

The commands above never delete business data. The only ones that replace data are `restore` and a
`rollback` across a migration, and both ask for a typed confirmation and back up first.
