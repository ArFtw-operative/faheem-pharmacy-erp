#!/usr/bin/env bash
# faheem-health — one screen: is everything on this PC as it should be?
#
#   Faheem ERP             OK
#   Database               OK
#   Kiosk                  OK
#   Backup                 OK (last 2026-10-04 05:00)
#   Updater                OK
#   Remote Support Agent   INSTALLED (HYD-FAHEEM-PHARMACY, package 1.0.0)
#   Remote Support         DISABLED
#   MeshCentral Server     REACHABLE
#
# Exit status 0 when nothing is wrong (remote support being off is normal).
SCRIPT_NAME=health
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
set +e

bad=0
row() { printf '%-22s %s\n' "$1" "$2"; }
st() { python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$STATE_DIR/status.json" "$1"; }

if curl -fsS -m 5 "$(ready_url)" >/dev/null 2>&1; then row "Faheem ERP" "OK ($(current_version))"; else row "Faheem ERP" "NOT READY — sudo faheem-erp doctor"; bad=1; fi

pg="$(dc ps --format '{{.Service}} {{.State}} {{.Health}}' postgres 2>/dev/null | head -1)"
case "$pg" in *running*healthy*|*"running "*) row "Database" "OK" ;; "") row "Database" "UNKNOWN (Docker not answering)"; bad=1 ;; *) row "Database" "PROBLEM ($pg)"; bad=1 ;; esac

desk="$(env_get FAHEEM_DESK_USER)"
if [ -z "$desk" ]; then row "Kiosk" "OFF (no desktop account)"
else
  home="$(getent passwd "$desk" | cut -d: -f6)"
  if [ -f "$home/.config/autostart/faheem-erp-app.desktop" ] && ! grep -q '^Hidden=true' "$home/.config/autostart/faheem-erp-app.desktop"; then row "Kiosk" "OK ($desk)"
  else row "Kiosk" "OFF (ERP does not open at login of $desk)"; fi
fi

last="$(st last_backup_at)"; lstat="$(st last_backup_status)"
if [ "$lstat" = ok ] && [ -n "$last" ] && [ $(( $(date +%s) - $(date -d "$last" +%s 2>/dev/null || echo 0) )) -lt 172800 ]; then row "Backup" "OK (last $(date -d "$last" '+%Y-%m-%d %H:%M'))"
elif [ -n "$last" ]; then row "Backup" "OLD or FAILED (last $last, $lstat) — sudo faheem-erp backup"; bad=1
else row "Backup" "NONE YET — sudo faheem-erp backup"; bad=1; fi

if systemctl is-enabled --quiet faheem-erp-maintenance.timer 2>/dev/null && systemctl is-active --quiet faheem-erp-maintenance.timer 2>/dev/null; then
  u="$(st last_update_status)"; row "Updater" "OK${u:+ (last update: $u)}"
else row "Updater" "TIMER OFF — sudo systemctl enable --now faheem-erp-maintenance.timer"; bad=1; fi

if [ -x /usr/local/bin/faheem-support ]; then
  json="$(/usr/local/bin/faheem-support status --json 2>/dev/null)"
  sf() { python3 -c 'import json,sys
try: print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
except Exception: print("")' "$json" "$1"; }
  s="$(sf state)"
  if [ "$s" = NOT_INSTALLED ] || [ -z "$s" ]; then row "Remote Support Agent" "NOT INSTALLED (sudo faheem-support install …)"
  else
    row "Remote Support Agent" "INSTALLED ($(sf reference), package $(sf package))"
    case "$s" in
      CONNECTED) row "Remote Support" "ENABLED — connected, $(( $(sf seconds_left) / 60 )) min left" ;;
      CONNECTING) row "Remote Support" "ENABLED — connecting" ;;
      DISABLED|EXPIRED) row "Remote Support" "DISABLED" ;;
      *) row "Remote Support" "$(sf label) — sudo faheem-support repair"; bad=1 ;;
    esac
    host="$(sf server)"
    if [ -n "$host" ] && curl -sS -o /dev/null -m 8 "https://$host/" 2>/dev/null; then row "MeshCentral Server" "REACHABLE ($host)"
    else row "MeshCentral Server" "UNREACHABLE ($host) — internet, DNS or server"; fi
  fi
else
  row "Remote Support Agent" "NOT INSTALLED"
fi
exit "$bad"
