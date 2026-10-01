#!/usr/bin/env bash
# After every boot (faheem-erp-boot-check.timer, 5 minutes in) — including after a power cut:
#   - an operation the shutdown interrupted (update, restore…) is recorded for the doctor / Status;
#   - the missed part of the 05:00 maintenance is caught up: a backup when the newest is over 24 hours
#     old, and an update check when the last one is over 24 hours old (AUTO_UPDATE=true).
# It never reboots: a catch-up reboot during business hours is exactly what must not happen.
SCRIPT_NAME=boot-check
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root
here="$(dirname "$(readlink -f "$0")")"
sget() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$STATE_DIR/status.json" "$1" 2>/dev/null || true; }

op="$(sget operation)"
if [ -n "$op" ]; then
  warn "The PC stopped during: $op — the ERP starts on the data it has; run: sudo faheem-erp doctor"
  write_state operation "" interrupted_operation "$op" interrupted_at "$(now_iso)"
fi

wait_ready 600 || warn "the ERP is not ready yet; catching up anyway"

newest="$(find "$FAHEEM_BACKUPS/snapshots" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended \
          -regex '.*/[0-9]{8}-[0-9]{6}-.*' -printf '%T@\n' 2>/dev/null | sort -n | tail -1 || true)"
if [ -z "$newest" ] || [ $(( $(date +%s) - ${newest%%.*} )) -gt 86400 ]; then
  log "No backup in the last 24 hours (PC off at maintenance time?) — taking it now"
  "$here/backup.sh" --reason scheduled --note "catch-up after boot" >/dev/null || warn "catch-up backup failed"
fi

flag AUTO_UPDATE || exit 0
last="$(cat "$STATE_DIR/last-update-check" 2>/dev/null || echo 0)"
age=$(( $(date +%s) - last ))
[ "$age" -ge 86400 ] || exit 0
log "Last update check was $((age / 3600)) h ago — checking now"
exec "$here/update.sh" --yes
