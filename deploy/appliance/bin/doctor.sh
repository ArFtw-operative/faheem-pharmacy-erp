#!/usr/bin/env bash
# faheem-erp doctor [--fix [all|ID…]] [--json] [--report]
#
# Diagnoses the appliance from its live state — service health and restart loops, what the recent logs
# actually say, database statistics, memory, disk, network, backups, updates — and proposes the fix
# that matches the cause it found. Nothing is changed unless --fix is given:
#   --fix            apply every proposed fix (only safe, reversible actions: never a restore or rollback)
#   --fix ID…        apply only those fixes (IDs are printed next to each finding)
#   --fix-prompt     diagnose, then ask before applying the proposed fixes (desktop shortcut)
#   --json           findings as JSON (the Control Center uses this)
#   --report         also write a support file without secrets
# Exit status 1 when a FAIL remains.
SCRIPT_NAME=doctor
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root
here="$(dirname "$(readlink -f "$0")")"
set +o pipefail          # a diagnosis keeps going when one probe (du, find, grep…) finds nothing

json=0 report=0 fix_mode="" fix_ids=() prompt=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) json=1; shift ;;
    --report) report=1; shift ;;
    --fix-prompt) prompt=1; shift ;;
    --fix) fix_mode=selected; shift
           while [ $# -gt 0 ] && [[ "$1" != --* ]]; do fix_ids+=("$1"); shift; done
           [ ${#fix_ids[@]} -eq 0 ] || [ "${fix_ids[0]}" = all ] && { fix_mode=all; fix_ids=(); } ;;
    *) die "unknown option $1" ;;
  esac
done

F_SEV=() F_TITLE=() F_DETAIL=() F_FIX=()
finding() {   # finding PASS|WARN|FAIL "title" "detail" [fix-id]
  F_SEV+=("$1"); F_TITLE+=("$2"); F_DETAIL+=("${3:-}"); F_FIX+=("${4:-}")
  [ "$json" = 1 ] && return 0
  local c="$c_ok"; [ "$1" = WARN ] && c="$c_warn"; [ "$1" = FAIL ] && c="$c_bad"
  printf '%s%-4s%s  %s' "$c" "$1" "$c_0" "$2"
  [ -n "${3:-}" ] && [ "$1" != PASS ] && printf '\n        %s%s%s' "$c_dim" "$3" "$c_0"
  [ -n "${4:-}" ] && printf '\n        fix: %s' "$4"
  printf '\n'
}

# What a service's recent log says is wrong — the cause decides the fix.
diagnose_logs() {   # diagnose_logs <service> → "cause|fix-id|evidence line"
  local svc="$1" logs l
  logs="$(dc logs --no-color --tail 300 "$svc" 2>&1 || true)"
  line() { grep -iE "$1" <<<"$logs" | tail -1 | sed 's/^[^|]*| //' | cut -c1-160; }
  if l="$(line 'No space left on device')"; [ -n "$l" ]; then echo "the disk is full|cleanup|$l"
  elif l="$(line 'could not connect to server|connection refused|connection to server at|password authentication failed|the database system is (starting up|shutting down)')"; [ -n "$l" ]; then echo "cannot reach PostgreSQL|restart-postgres|$l"
  elif l="$(line 'Permission denied|PermissionError|Read-only file system')"; [ -n "$l" ]; then echo "a data folder has the wrong owner|permissions|$l"
  elif l="$(line 'schema is not current|migrations must complete|Migration head|alembic|UpgradeError')"; [ -n "$l" ]; then echo "the database schema does not match this release|migrate|$l"
  elif l="$(line 'MemoryError|Killed|out of memory|OOMKilled')"; [ -n "$l" ]; then echo "the process ran out of memory|restart-$svc|$l"
  elif l="$(line 'address already in use')"; [ -n "$l" ]; then echo "another program uses the ERP's port|port|$l"
  elif l="$(line 'Traceback|ERROR|Exception|FATAL|panic')"; [ -n "$l" ]; then echo "an error in the $svc process|restart-$svc|$l"
  else echo "no error in the recent log|restart-$svc|"; fi
}

# ---- host -----------------------------------------------------------------------------------------
. /etc/os-release 2>/dev/null || true
if grep -qi microsoft /proc/version 2>/dev/null; then finding FAIL "Host is WSL" "the appliance needs Ubuntu/Debian installed on the PC"
else finding PASS "Host: ${PRETTY_NAME:-Linux}, kernel $(uname -r), up $(uptime -p 2>/dev/null | sed 's/^up //')"; fi
[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && finding PASS "Clock synchronised" \
  || finding WARN "Clock not synchronised" "bill times and two-step codes depend on the clock" ntp

read -r used_pct avail_kb mount <<<"$(df -Pk "$FAHEEM_DATA" | awk 'NR==2 {gsub("%","",$5); print $5, $4, $6}')"
biggest="$(du -xsh "$FAHEEM_DATA"/* "$FAHEEM_BACKUPS" "$FAHEEM_LOGS" /var/lib/docker 2>/dev/null | sort -rh | head -3 | awk '{printf "%s %s; ", $2, $1}')"
if [ "$used_pct" -ge 92 ]; then finding FAIL "Disk ${used_pct}% full on $mount" "largest: $biggest" cleanup
elif [ "$used_pct" -ge 80 ]; then finding WARN "Disk ${used_pct}% full on $mount" "largest: $biggest" cleanup
else finding PASS "Disk ${used_pct}% used on $mount ($(( avail_kb / 1048576 )) GB free)"; fi

mem_total="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"; mem_avail="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
mem_pct=$(( mem_avail * 100 / mem_total ))
hogs="$(ps -eo rss,comm --sort=-rss | awk 'NR>1 && NR<=4 {printf "%s %d MB; ", $2, $1/1024}')"
if [ "$mem_pct" -lt 8 ]; then finding FAIL "Memory almost exhausted (${mem_pct}% free)" "largest: $hogs" restart-erp
elif [ "$mem_pct" -lt 15 ]; then finding WARN "Memory low (${mem_pct}% free)" "largest: $hogs"
else finding PASS "Memory ${mem_pct}% free of $(( mem_total / 1048576 )) GB"; fi
cores="$(nproc)"; load="$(cut -d' ' -f1 /proc/loadavg)"
if awk -v l="$load" -v c="$cores" 'BEGIN {exit !(l > c * 2)}'; then
  finding WARN "High load ($load on $cores cores)" "busiest: $(ps -eo pcpu,comm --sort=-pcpu | awk 'NR>1 && NR<=4 {printf "%s %s%%; ", $2, $1}')"
fi

# ---- configuration --------------------------------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  mode="$(stat -c %a "$ENV_FILE")"
  [ "$mode" = 600 ] && finding PASS "Configuration protected (600)" || finding FAIL "faheem.env is readable by others ($mode)" "it holds the database password and keys" permissions
  missing=""; for k in FAHEEM_VERSION POSTGRES_PASSWORD PHARMACY_SECRET_KEY PHARMACY_WPP_SECRET; do [ -n "$(env_get "$k")" ] || missing+="$k "; done
  [ -z "$missing" ] || finding FAIL "Configuration incomplete" "missing: $missing— run the installer again (it keeps existing values)"
else finding FAIL "No configuration ($ENV_FILE)" "run the installer again"; fi

# ---- docker and the stack -------------------------------------------------------------------------
if ! docker info >/dev/null 2>&1; then
  finding FAIL "Docker is not running" "$(systemctl is-failed docker 2>/dev/null) $(journalctl -u docker -n 3 --no-pager -o cat 2>/dev/null | tail -1 | cut -c1-140)" docker
else
  finding PASS "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  for s in $(services_up); do
    [ "$s" = migrate ] && continue
    read -r state health restarts <<<"$(dc ps -a --format '{{.State}} {{.Health}}' "$s" 2>/dev/null | head -1) $(docker inspect -f '{{.RestartCount}}' "faheem-erp-$s-1" 2>/dev/null || echo 0)"
    [ -z "${restarts:-}" ] && { restarts="$health"; health=""; }
    if [ "${state:-}" = running ] && { [ "${health:-}" = healthy ] || [ -z "${health:-}" ]; } && [ "${restarts:-0}" -lt 3 ]; then
      finding PASS "Service $s running${health:+ ($health)}"
    else
      IFS='|' read -r cause fix evidence <<<"$(diagnose_logs "$s")"
      what="${state:-not created}${health:+, $health}"; [ "${restarts:-0}" -ge 3 ] && what+=", restarted $restarts times"
      [ "${state:-}" = running ] || fix="${fix/restart-$s/start}"
      finding FAIL "Service $s: $what" "cause: $cause${evidence:+ — \"$evidence\"}" "$fix"
    fi
  done
  read -r mstate mcode <<<"$(dc ps -a --format '{{.State}} {{.ExitCode}}' migrate 2>/dev/null | head -1)"
  if [ "${mstate:-}" = exited ] && [ "${mcode:-}" != 0 ]; then
    IFS='|' read -r cause fix evidence <<<"$(diagnose_logs migrate)"
    finding FAIL "Migration job failed (exit $mcode)" "cause: $cause${evidence:+ — \"$evidence\"}" "$fix"
  fi
  if dc ps --format '{{.Service}} {{.Ports}}' 2>/dev/null | grep -qE '^postgres .*->'; then finding FAIL "PostgreSQL port is published" "it must never be reachable from the network"
  else finding PASS "PostgreSQL reachable only inside the stack"; fi

  if dc ps --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
    pq() { dc exec -T postgres psql -U faheem -d faheem -At -c "$1" 2>/dev/null; }
    size="$(pq "select pg_size_pretty(pg_database_size('faheem'))")"
    conns="$(pq "select count(*) from pg_stat_activity where datname='faheem'")"
    long="$(pq "select count(*) from pg_stat_activity where datname='faheem' and state <> 'idle' and now() - query_start > interval '5 minutes'")"
    bloat="$(pq "select coalesce(round(100.0 * sum(n_dead_tup) / nullif(sum(n_live_tup + n_dead_tup), 0)), 0) from pg_stat_user_tables")"
    lastvac="$(pq "select coalesce(to_char(max(greatest(last_vacuum, last_autovacuum)), 'YYYY-MM-DD'), 'never') from pg_stat_user_tables")"
    [ -n "$size" ] && finding PASS "Database $size, $conns connection(s), last vacuum $lastvac"
    [ "${long:-0}" -gt 0 ] && finding WARN "$long database query(ies) running over 5 minutes" "usually a stuck report; restarting the ERP ends them" restart-erp
    [ "${bloat:-0}" -ge 20 ] && finding WARN "Database ${bloat}% dead rows" "space from edited/deleted rows not reclaimed yet" optimize-db
  fi
fi

body="$(curl -sS -m 5 "$(ready_url)" 2>&1 || true)"
if curl -fsS -m 5 "$(ready_url)" >/dev/null 2>&1; then finding PASS "ERP answering on $(ready_url | sed 's#/health/ready##')"
else
  why="$(sed -n 's/.*"database": *"\([^"]*\)".*/\1/p' <<<"$body")"
  case "$why" in
    "schema mismatch") finding FAIL "ERP not ready: database schema mismatch" "the migration job has not completed for this release" migrate ;;
    unavailable) finding FAIL "ERP not ready: database unavailable" "PostgreSQL is down or refusing connections" restart-postgres ;;
    *) finding FAIL "ERP not answering" "${body:0:140}" start ;;
  esac
fi
errs="$(dc logs --no-color --since 24h web 2>/dev/null | grep -cE 'Traceback|ERROR' || true)"
if [ "${errs:-0}" -gt 20 ]; then
  top="$(dc logs --no-color --since 24h web 2>/dev/null | grep -E 'ERROR' | sed 's/^[^|]*| //; s/[0-9]\{2,\}/N/g' | sort | uniq -c | sort -rn | head -1 | cut -c1-150)"
  finding WARN "$errs errors in the ERP log in 24 h" "most frequent: $top"
fi

# ---- backups, updates, schedule -------------------------------------------------------------------
last="$(find "$FAHEEM_BACKUPS/snapshots" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}-[0-9]{6}-.*' -printf '%T@ %f\n' 2>/dev/null | sort -n | tail -1)"
st_json="$STATE_DIR/status.json"; sget() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$st_json" "$1" 2>/dev/null; }
if [ -z "$last" ]; then finding FAIL "No backup yet" "" backup
else
  age_h=$(( ($(date +%s) - ${last%%.*}) / 3600 ))
  if [ "$(sget last_backup_status)" = failed ]; then finding FAIL "The last backup attempt failed ($(sget last_backup_at))" "newest good one: ${last#* }, ${age_h} h old" backup
  elif [ "$age_h" -gt 26 ]; then finding WARN "Newest backup is ${age_h} h old" "${last#* } — the 05:00 maintenance may not have run (PC off?)" backup
  else finding PASS "Newest backup ${last#* } (${age_h} h old)"; fi
fi
if [ -n "$(sget interrupted_operation)" ]; then
  finding WARN "The PC stopped during: $(sget interrupted_operation)" "noticed at boot $(sget interrupted_at); check the ERP's data, or restore the backup taken before it (faheem-erp backups)"
fi
case "$(sget last_update_status)" in
  ""|ok) ;;
  *) finding WARN "Last update: $(sget last_update_status)" "at $(sget last_update_at); the previous version kept running — see: faheem-erp logs, $DEPLOY_LOG" ;;
esac
newest="$( (latest_version) 2>/dev/null || true)"     # subshell: a refused registry must not end the diagnosis
if [ -n "$newest" ] && version_gt "$newest" "$(current_version)"; then finding WARN "Update available: $(current_version) → $newest" "" update
elif [ -n "$newest" ]; then finding PASS "Version $(current_version) is the newest"
else finding WARN "Could not reach GHCR to check for updates" "internet connection — or, if the images are private, the registry token"; fi
for t in faheem-erp-maintenance.timer faheem-erp-boot-check.timer; do
  systemctl is-enabled --quiet "$t" 2>/dev/null || finding WARN "$t is off" "no daily backup/maintenance" enable-timers
done
systemctl is-enabled --quiet faheem-erp.service 2>/dev/null || finding WARN "ERP does not start at boot" "turn on in Control Center → Settings if intended" enable-boot
logs_mb=$(( $(du -sk "$FAHEEM_LOGS" 2>/dev/null | cut -f1) / 1024 ))
[ "$logs_mb" -gt 1024 ] && finding WARN "Logs use $logs_mb MB" "" cleanup

# ---- network --------------------------------------------------------------------------------------
if flag LAN_ACCESS; then
  iptables -L FAHEEM-ERP -n >/dev/null 2>&1 && finding PASS "Firewall limits network access to private/VPN ranges" \
    || finding FAIL "Firewall rules missing" "the HTTPS port is open to any address" firewall
  ip_now="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')"
  [ "$ip_now" = "$(env_get FAHEEM_LAN_IP)" ] && finding PASS "Network address $ip_now (https://$ip_now)" \
    || finding WARN "Network address changed: ${ip_now:-none} (ERP certificate is for $(env_get FAHEEM_LAN_IP))" "other devices cannot connect at the old address" lan-address
fi

# ---- fixes ----------------------------------------------------------------------------------------
apply_fix() {
  case "$1" in
    ntp) timedatectl set-ntp true ;;
    cleanup) "$here/optimize.sh" cleanup ;;
    permissions)
      chmod 600 "$ENV_FILE"; chown root:root "$ENV_FILE"
      chown -R "$(env_get FAHEEM_UID):$(env_get FAHEEM_GID)" "$FAHEEM_DATA/uploads" "$FAHEEM_DATA/application-data" "$FAHEEM_LOGS/app" ;;
    docker) systemctl restart docker ;;
    start) dc up -d $(services_up) && wait_ready 300 ;;
    restart-erp) dc restart web worker && wait_ready 300 ;;
    restart-postgres) dc restart postgres && dc up -d $(services_up) && wait_ready 300 ;;
    restart-*) dc restart "${1#restart-}" ;;
    migrate) dc up -d migrate && dc up -d $(services_up) && wait_ready 300 ;;
    optimize-db) "$here/optimize.sh" database ;;
    backup) "$here/backup.sh" --reason manual --note "doctor" --locked >/dev/null ;;
    update) "$here/update.sh" --yes ;;
    enable-timers) systemctl enable --now faheem-erp-maintenance.timer faheem-erp-boot-check.timer ;;
    enable-boot) systemctl enable faheem-erp.service ;;
    firewall) systemctl restart faheem-erp-firewall.service || "$here/firewall.sh" ;;
    lan-address) "$here/network.sh" lan enable --yes ;;
    port) echo "Another program uses port $(port): $(ss -ltnp 2>/dev/null | grep ":$(port) " | sed 's/.*users:((//' | cut -d, -f1)"; return 1 ;;
    *) echo "no automatic fix for $1"; return 1 ;;
  esac
}

if [ "$json" = 1 ]; then
  python3 - "${#F_SEV[@]}" "${F_SEV[@]}" "${F_TITLE[@]}" "${F_DETAIL[@]}" "${F_FIX[@]}" <<'PY'
import json, sys
n = int(sys.argv[1]); a = sys.argv[2:]
print(json.dumps([{"severity": a[i], "title": a[n + i], "detail": a[2 * n + i], "fix": a[3 * n + i]} for i in range(n)], indent=1))
PY
fi

fails=0 warns=0
for i in "${!F_SEV[@]}"; do [ "${F_SEV[$i]}" = FAIL ] && fails=$((fails + 1)); [ "${F_SEV[$i]}" = WARN ] && warns=$((warns + 1)); done

if [ "$prompt" = 1 ] && [ -r /dev/tty ]; then
  fixable=0; for i in "${!F_SEV[@]}"; do [ "${F_SEV[$i]}" != PASS ] && [ -n "${F_FIX[$i]}" ] && [ "${F_FIX[$i]}" != update ] && fixable=1; done
  if [ "$fixable" = 1 ]; then
    printf '\nApply the proposed fixes now? [Y/n] ' > /dev/tty; read -r a < /dev/tty
    case "${a:-y}" in y|Y|yes) fix_mode=all ;; esac
  fi
fi

if [ -n "$fix_mode" ]; then
  [ "$json" = 1 ] || echo
  done_fixes=" "
  for i in "${!F_SEV[@]}"; do
    f="${F_FIX[$i]}"; [ -n "$f" ] && [ "${F_SEV[$i]}" != PASS ] || continue
    [[ "$done_fixes" == *" $f "* ]] && continue
    if [ "$fix_mode" = selected ] && [[ " ${fix_ids[*]} " != *" $f "* ]]; then continue; fi
    [ "$f" = update ] && [ "$fix_mode" = all ] && continue          # updating is a decision, never a side effect
    log "Fixing: ${F_TITLE[$i]} ($f)"
    if apply_fix "$f"; then ok "fixed: $f"; else warn "fix $f did not succeed"; fi
    done_fixes+="$f "
  done
  [ "$json" = 1 ] || { echo; echo "Re-checking…"; exec "$0"; }
fi

if [ "$json" != 1 ]; then
  echo
  echo "Version $(current_version) · $fails failure(s), $warns warning(s)"
  [ $((fails + warns)) -gt 0 ] && echo "Apply the proposed fixes: sudo faheem-erp doctor --fix   (or pick in: sudo faheem-erp menu)"
fi
if [ "$report" = 1 ]; then
  f="$FAHEEM_LOGS/support-$(date +%Y%m%d-%H%M%S).txt"
  {
    echo "Faheem Pharmacy ERP support report $(now_iso)"
    for i in "${!F_SEV[@]}"; do echo "${F_SEV[$i]}  ${F_TITLE[$i]}  ${F_DETAIL[$i]}  ${F_FIX[$i]:+[fix ${F_FIX[$i]}]}"; done
    echo; echo "== faheem.env (secrets redacted)"
    awk -F= '/^[A-Z_]+=/ {k=$1; v=substr($0, length(k)+2); if (k ~ /PASSWORD|SECRET|TOKEN|KEY/) print k "=<" length(v) " chars>"; else print}' "$ENV_FILE"
    echo; echo "== services"; dc ps -a 2>&1
    echo; echo "== deploy log (last 80)"; tail -80 "$DEPLOY_LOG" 2>/dev/null
    for s in web worker migrate postgres; do echo; echo "== $s (last 60)"; dc logs --no-color --tail 60 "$s" 2>&1; done
  } > "$f"
  chmod 600 "$f"; [ "$json" = 1 ] || echo "Support report: $f"
fi
[ "$fails" -eq 0 ]
