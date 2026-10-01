#!/usr/bin/env bash
# After boot (faheem-erp-boot-check.timer, 5 minutes in): if the PC was off at 05:00 and the last update
# check is more than 24 hours old, check for and apply an update now. Never reboots, never runs the
# missed maintenance otherwise.
SCRIPT_NAME=boot-check
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

flag AUTO_UPDATE || exit 0
last="$(cat "$STATE_DIR/last-update-check" 2>/dev/null || echo 0)"
age=$(( $(date +%s) - last ))
if [ "$age" -lt 86400 ]; then exit 0; fi
log "Last update check was $((age / 3600)) h ago — checking now"
exec "$(dirname "$(readlink -f "$0")")/update.sh" --yes
