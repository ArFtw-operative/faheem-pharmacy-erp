# Operating the appliance

Everything is available three ways: desktop shortcuts, the **ERP Control Center** (a keyboard menu)
and the `faheem-erp` command. Each asks for an administrator password — the counter user `faheem`
has no administrator rights, so the owner or support types theirs (polkit).

## Desktop shortcuts

| Shortcut | Does |
|---|---|
| Faheem Pharmacy ERP | the ERP full screen, like dedicated software (no browser bars, no exit button at the top; leave with Super, Alt+Tab or Alt+F4); starts the ERP first if it is not running (no password) |
| ERP Control Center | the menu below |
| ERP Doctor | diagnoses the appliance and offers the fixes it found |
| Update ERP | installs the newest release (backup, rehearsal and automatic rollback included) |
| Back up ERP | verified backup now |
| Restart ERP / Shut down ERP / Start ERP | the ERP only; the PC stays on. Start / Restart open the app afterwards |
| ERP Maintenance | optimise the database, free disk space, clear caches |

## Control Center — `sudo faheem-erp menu`

↑ ↓ Enter, Esc to go back.

- **Status** — services, version, last backup / update / maintenance.
- **Restart / Shut down / Start ERP.**
- **Updates** — update now, a specific version, roll back, history.
- **Backups** — back up now, list, restore (typed confirmation; the current state is backed up
  first), prove the newest backup restores.
- **Doctor** — runs the diagnosis, then a checklist of the proposed fixes.
- **Maintenance and performance**
  - run the daily maintenance now (no reboot)
  - optimise the database (VACUUM ANALYZE)
  - free disk space (old images and releases, Docker build cache, journal, apt cache)
  - clear the ERP's memory caches (restarts it; nobody is signed out)
  - clear the counter browser's cache — or cache **and cookies** (the counter signs in again)
  - disk usage
- **Settings** — select one to change it:

  | Setting | Meaning | Default |
  |---|---|---|
  | boot-start | start the ERP when the PC starts | on |
  | counter-screen | open the ERP full-screen when the counter user logs in | on |
  | app-at-login | open the ERP app when the administrator logs in | on |
  | auto-login | log the counter user in automatically at boot | on |
  | auto-update | install new releases during the morning maintenance | on |
  | daily-reboot | restart the PC after the morning maintenance | on |
  | maintenance-time | time of the daily maintenance, India time | 05:00 |
  | backup-days / backup-keep | keep backups N days, never fewer than M snapshots | 30 / 14 |
  | network access | other PCs / phones / store VPN over HTTPS | asked at install |
  | WhatsApp invoices | the gateway on/off | off |

- **Network access** — addresses, on/off, fixed or automatic address.
- **Logs** — ERP, WhatsApp queue, migration job, database, proxy, updates/backups/maintenance.
- **Power** — restart or turn off the PC (the ERP is stopped cleanly first).
- **Support report** — a file in `/var/log/faheem-erp/` with the diagnosis, configuration names
  (secrets replaced by their length) and recent logs.

## The doctor

`sudo faheem-erp doctor` reads the live state rather than a fixed checklist:

- each service's state, health and restart count — and, when one is unhealthy, **what its recent log
  says**: disk full, PostgreSQL unreachable, a folder with the wrong owner, schema mismatch, out of
  memory, port taken, or the last error line — and proposes the fix that matches that cause;
- the readiness answer of the ERP (database unavailable vs. schema mismatch);
- database size, connections, queries running over 5 minutes, share of dead rows;
- memory (and the largest processes), load (and the busiest), disk (and the largest folders);
- the most frequent error in the last 24 h of the ERP log;
- backups (age and last attempt), the last update's outcome, whether a newer release exists;
- timers, boot start, firewall, whether the PC's address changed since the certificate was made.

`--fix` applies every proposed fix; `--fix backup restart-postgres` only those. Fixes are safe,
reversible actions (start / restart services, re-run the migration job, permissions, NTP, cleanup,
database optimisation, firewall, address refresh, a backup). Updating is offered but never applied by
`--fix` alone, and a restore or rollback is never automatic.

## Command reference

```
sudo faheem-erp status | start | stop | restart
sudo faheem-erp update [VERSION] | update --check | rollback [VERSION]
sudo faheem-erp backup [--note TEXT] | backups | restore ID|--latest | drill [ID]
sudo faheem-erp doctor [--fix [ID…]] [--report]
sudo faheem-erp optimize database|cleanup|app-cache|browser-cache|browser-cookies|all|usage
sudo faheem-erp settings | settings set KEY VALUE
sudo faheem-erp lan status|enable|disable|ca | network static [IP/PREFIX] | network dhcp
sudo faheem-erp whatsapp enable|disable | whatsapp-status
sudo faheem-erp user list|add|passwd|disable|enable|reset-2fa|unlock …
sudo faheem-erp logs [web|worker|migrate|postgres|proxy|whatsapp] [-f]
sudo faheem-erp maintenance | power reboot|poweroff | version | uninstall
```

## Daily maintenance (05:00 India time)

1. Verified backup; retention applied.
2. If **auto-update** is on and the backup succeeded: update to the newest release
   ([UPDATE.md](UPDATE.md)).
3. Cleanup (old images and releases, journal, caches); on Sundays also VACUUM ANALYZE.
4. If **daily-reboot** is on: restart the PC.

## After a power cut or a PC that was off

- The ERP starts with the PC (`faheem-erp.service`); its containers restart by themselves and
  PostgreSQL recovers its own state (data checksums on).
- The counter user is logged in automatically and gets the ERP full-screen; the administrator gets the
  app window as soon as they log in. Until the ERP is ready the app shows "Starting services…", then
  opens it by itself.
- Five minutes after boot the **catch-up** runs: a backup if the newest is more than 24 hours old, and
  an update check if the last one is more than 24 hours old. It never reboots — a missed 05:00 reboot
  is simply skipped.
- If the power went off in the middle of an update or restore, Status and the doctor say so. An update
  is safe either way: its database change is one transaction, and the pre-update backup exists.
