#!/usr/bin/env bash
# Daily maintenance — run by faheem-erp-maintenance.timer at 05:00 Asia/Kolkata (never caught up later).
#   1. verified backup + retention
#   2. update to the newest release when AUTO_UPDATE=true (only after a good backup)
#   3. cleanup (old images/releases, journal, caches); Sundays also VACUUM ANALYZE the database
#   4. reboot the PC when DAILY_HOST_REBOOT=true
SCRIPT_NAME=maintenance
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root
here="$(dirname "$(readlink -f "$0")")"

exec 8>"$FAHEEM_LOCK.maintenance"
flock -n 8 || { warn "maintenance is already running"; exit 0; }
take_lock maintenance 1800
log "Maintenance starting"
write_state maintenance_started_at "$(now_iso)"
status=ok

if "$here/backup.sh" --reason scheduled --locked >/dev/null; then
  if flag AUTO_UPDATE; then
    "$here/update.sh" --yes --locked || status="update failed (previous version kept)"
  fi
else
  status="backup failed; update skipped"
  warn "$status"
fi

export FAHEEM_LOCKED=1                    # child scripts run under this maintenance's lock
"$here/optimize.sh" cleanup || warn "cleanup did not finish"
if [ "$(date +%u)" = 7 ]; then "$here/optimize.sh" database || warn "database optimisation did not finish"; fi

write_state maintenance_finished_at "$(now_iso)" maintenance_status "$status"
log "Maintenance finished: $status"

if flag DAILY_HOST_REBOOT && [ "${FAHEEM_NO_REBOOT:-0}" != 1 ]; then
  log "Daily reboot (DAILY_HOST_REBOOT=true)"
  flock -u 9 || true
  sync
  [ "${FAHEEM_TEST:-0}" = 1 ] && { echo "REBOOT"; exit 0; }
  systemctl reboot
fi
