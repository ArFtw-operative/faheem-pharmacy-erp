#!/usr/bin/env bash
# faheem-erp update [<version>] [--check] [--yes]
#
# Transactional update to <version> (default: the newest release in GHCR). Every step must pass:
#   1. pull the image (pinned x.y.z tag) and unpack its host tooling into releases/<version>/
#   2. rehearse its migrations on a scratch copy of the live database (live data untouched)
#   3. verified pre-upgrade snapshot
#   4. stop web + worker → switch the version → migration job → web + worker
#   5. /health/ready and the deployment smoke test (database, search, invoice PDF, uploads)
# If 4 or 5 fails, the previous version is put back automatically; if the database had already been
# migrated, the pre-upgrade snapshot is restored first. History: /var/log/faheem-erp/deploy.log.
SCRIPT_NAME=update
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

target="" check=0 locked=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1; shift ;;
    --yes) shift ;;
    --locked) locked=1; shift ;;
    -*) die "unknown option $1" ;;
    *) target="$1"; shift ;;
  esac
done
[ "$locked" = 1 ] || take_lock update

current="$(current_version)"
[ -n "$target" ] || target="$(latest_version)" || die "could not read the release list from GHCR"
[ -n "$target" ] || die "GHCR lists no releases yet"
[[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a release version: $target"
write_state last_update_check "$(now_iso)" latest_available "$target"
date +%s > "$STATE_DIR/last-update-check"

if [ "$target" = "$current" ]; then ok "Up to date ($current)"; exit 0; fi
version_gt "$target" "$current" || die "$target is older than the installed $current — use: faheem-erp rollback"
if [ "$check" = 1 ]; then echo "Update available: $current → $target"; exit 0; fi

log "Update $current → $target"
free_kb="$(df -Pk "$FAHEEM_DATA" | awk 'NR==2 {print $4}')"
[ "$free_kb" -gt 3000000 ] || die "less than 3 GB free on $(df -P "$FAHEEM_DATA" | awk 'NR==2 {print $6}') — not updating"
docker info >/dev/null 2>&1 || die "Docker is not running"

# 1. image + tooling
docker pull --quiet "$(image):$target" >/dev/null || die "could not pull $(image):$target"
extract_release "$target"
newrel="$(release_dir "$target")"; oldrel="$(readlink -f "$FAHEEM_HOME/current")"
ok "Pulled $(image):$target"

# 2. rehearsal with the new image on a scratch copy (live data untouched)
FAHEEM_VERSION="$target" dc_rel "$newrel" run --rm --no-TTY tools python scripts/manage.py upgrade --check \
  || die "the migration rehearsal for $target failed — nothing was changed"
ok "Migrations rehearsed on a copy"

# 3. pre-upgrade snapshot (with the running version's tool)
snap="$("$oldrel/bin/backup.sh" --reason pre-upgrade --note "before $target" --no-prune --locked | tail -1)" \
  || die "the pre-upgrade backup failed — nothing was changed"
write_state operation "updating to $target" operation_at "$(now_iso)" pre_upgrade_snapshot "$snap"
echo "$snap" > "$STATE_DIR/pre-upgrade-snapshot.$target"     # what a later rollback from $target restores

rollback() {
  warn "Update to $target failed: $1 — returning to $current"
  dc_rel "$newrel" stop web worker >/dev/null 2>&1 || true
  env_set FAHEEM_VERSION "$current"
  switch_current "$current"
  if dc_rel "$oldrel" run --rm --no-TTY tools python -m app.production schema 2>/dev/null | grep -q '"state": "current"'; then
    log "The database was not changed; no restore needed"
  else
    log "Restoring the pre-upgrade snapshot $snap"
    dc_rel "$oldrel" run --rm --no-TTY tools python -m app.snapshot restore "$snap" --yes \
      || { write_state operation "" last_update_status "failed; restore failed"; die "ROLLBACK RESTORE FAILED — run: sudo faheem-erp doctor"; }
  fi
  dc_rel "$oldrel" up -d $(services_up) && wait_ready 300 \
    || { write_state operation "" last_update_status "failed; rollback unhealthy"; die "rolled back to $current but it is not ready — run: sudo faheem-erp doctor"; }
  write_state operation "" version "$current" last_update_status "failed: $1 (rolled back)" last_update_at "$(now_iso)"
  die "update to $target failed ($1); $current is running again with its data"
}
trap 'rollback "interrupted"' INT TERM

# 4. switch
dc stop web worker >/dev/null 2>&1 || true
env_set FAHEEM_PREVIOUS_VERSION "$current"
env_set FAHEEM_VERSION "$target"
switch_current "$target"
dc_rel "$newrel" up -d --remove-orphans $(services_up) || rollback "the migration job or a service did not start"

# 5. verification
wait_ready 300 || rollback "/health/ready did not answer"
dc_rel "$newrel" run --rm --no-TTY tools python -m app.production smoke >/dev/null || rollback "the smoke test failed"
trap - INT TERM
install_host_files || warn "could not refresh the host integration files"

write_state operation "" version "$target" previous_version "$current" last_update_status ok last_update_at "$(now_iso)"
ok "Updated to $target (previous: $current; pre-upgrade snapshot $snap)"
