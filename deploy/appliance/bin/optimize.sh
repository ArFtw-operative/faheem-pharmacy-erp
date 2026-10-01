#!/usr/bin/env bash
# faheem-erp optimize database|cleanup|app-cache|browser-cache|browser-cookies|all|usage
#   database         VACUUM + ANALYZE: reclaim space from edited rows, refresh the planner's statistics
#   cleanup          old images and releases, Docker build cache, journal over 200 MB, apt cache, old support files
#   app-cache        restart web + worker (clears in-memory report documents and caches; nobody is signed out)
#   browser-cache    clear the counter browser's cache (closes it; the ERP reopens at next login / icon)
#   browser-cookies  also clear its cookies and site data (the counter must sign in again)
#   all              database + cleanup + app-cache + browser-cache
#   usage            where the disk space goes
SCRIPT_NAME=optimize
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

kiosk_user=faheem
kiosk_home="$(getent passwd "$kiosk_user" | cut -d: -f6 || true)"

database() {
  local before after
  before="$(dc exec -T postgres psql -U faheem -d faheem -At -c "select pg_database_size('faheem')")"
  log "Optimising the database (VACUUM ANALYZE)…"
  dc exec -T postgres psql -U faheem -d faheem -q -c "VACUUM (ANALYZE)" || die "VACUUM failed"
  after="$(dc exec -T postgres psql -U faheem -d faheem -At -c "select pg_database_size('faheem')")"
  before="${before:-0}"; after="${after:-0}"
  ok "Database optimised: $(( before / 1048576 )) MB → $(( after / 1048576 )) MB, statistics refreshed"
}

cleanup() {
  local keep_now keep_prev freed_before freed_after
  freed_before="$(df -Pk "$FAHEEM_DATA" | awk 'NR==2 {print $4}')"
  keep_now="$(current_version)"; keep_prev="$(env_get FAHEEM_PREVIOUS_VERSION)"
  docker image ls "$(image)" --format '{{.Tag}}' | while read -r tag; do
    [ "$tag" = "$keep_now" ] || [ "$tag" = "$keep_prev" ] || [ "$tag" = "<none>" ] && continue
    docker image rm "$(image):$tag" >/dev/null 2>&1 && log "removed image $tag" || true
  done
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f >/dev/null 2>&1 || true
  find "$FAHEEM_HOME/releases" -mindepth 1 -maxdepth 1 -type d ! -name "$keep_now" ! -name "$keep_prev" ! -name '.tmp-*' -exec rm -rf {} + 2>/dev/null || true
  find "$FAHEEM_LOGS" -name 'support-*.txt' -mtime +30 -delete 2>/dev/null || true
  find "$FAHEEM_LOGS" -name '*.gz' -mtime +120 -delete 2>/dev/null || true
  journalctl --vacuum-size=200M >/dev/null 2>&1 || true
  apt-get clean >/dev/null 2>&1 || true
  freed_after="$(df -Pk "$FAHEEM_DATA" | awk 'NR==2 {print $4}')"
  ok "Cleanup done: $(( (freed_after - freed_before) / 1024 )) MB freed (backups are never touched here)"
}

app_cache() {
  log "Restarting the ERP processes (in-memory caches cleared)…"
  dc restart web worker >/dev/null
  wait_ready 300 && ok "ERP restarted and ready" || die "the ERP did not come back — run: sudo faheem-erp doctor"
}

browser_profiles() {   # every Chromium/Chrome profile root of the counter user
  local d
  for d in "$kiosk_home/snap/chromium/common/chromium" "$kiosk_home/.config/chromium" "$kiosk_home/.config/google-chrome"; do
    [ -d "$d" ] && echo "$d"
  done
}

close_browser() {
  local n
  for n in chrome chromium chromium-browser google-chrome; do pkill -u "$kiosk_user" -x "$n" 2>/dev/null || true; done
  sleep 2
}

browser() {   # browser cache|cookies
  [ -n "$kiosk_home" ] || die "no counter user '$kiosk_user' on this PC"
  local what="$1" root p cleared=0
  close_browser
  for root in $(browser_profiles); do
    for p in "$root"/*/Cache "$root"/*/"Code Cache" "$root"/*/GPUCache "$root"/*/"Service Worker/CacheStorage" "$root"/ShaderCache "$root"/GrShaderCache; do
      [ -e "$p" ] && { rm -rf "$p"; cleared=$((cleared + 1)); }
    done
    if [ "$what" = cookies ]; then
      for p in "$root"/*/Cookies "$root"/*/Cookies-journal "$root"/*/Network/Cookies "$root"/*/Network/Cookies-journal \
               "$root"/*/"Local Storage" "$root"/*/"Session Storage" "$root"/*/IndexedDB; do
        [ -e "$p" ] && { rm -rf "$p"; cleared=$((cleared + 1)); }
      done
    fi
  done
  rm -rf "$kiosk_home/.cache/chromium" "$kiosk_home/.cache/google-chrome" "$kiosk_home/snap/chromium/common/.cache" 2>/dev/null || true
  ok "Counter browser $([ "$what" = cookies ] && echo "cache and cookies" || echo cache) cleared ($cleared item(s)); open the ERP again from its icon"
}

usage() {
  df -h "$FAHEEM_DATA" | tail -1 | awk '{print "Disk " $6 ": " $3 " used of " $2 " (" $5 "), " $4 " free"}'
  du -xsh "$FAHEEM_DATA"/* "$FAHEEM_BACKUPS" "$FAHEEM_LOGS" /var/lib/docker 2>/dev/null | sort -rh
  echo; docker system df 2>/dev/null || true
}

[ "${1:-}" = usage ] || [ "${FAHEEM_LOCKED:-0}" = 1 ] || take_lock optimize 120
case "${1:-}" in
  database) database ;;
  cleanup) cleanup ;;
  app-cache) app_cache ;;
  browser-cache) browser cache ;;
  browser-cookies) browser cookies ;;
  all) database; cleanup; app_cache; browser cache ;;
  usage) usage ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
