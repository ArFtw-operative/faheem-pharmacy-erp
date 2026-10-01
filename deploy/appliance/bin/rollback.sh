#!/usr/bin/env bash
# faheem-erp rollback [<version>] [--yes]
#
# Go back to the previous version (FAHEEM_PREVIOUS_VERSION) or a given installed one.
# - If that version's schema matches the database, only the version is switched: no data is lost.
# - If the database was migrated since, its data must come from the pre-upgrade snapshot: bills
#   written after that update are lost. This needs typed confirmation (or --yes).
SCRIPT_NAME=rollback
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

target="" yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1; shift ;;
    -*) die "unknown option $1" ;;
    *) target="$1"; shift ;;
  esac
done
take_lock rollback

current="$(current_version)"
target="${target:-$(env_get FAHEEM_PREVIOUS_VERSION)}"
[ -n "$target" ] || die "no previous version is recorded; give one: faheem-erp rollback <version>"
[ "$target" != "$current" ] || die "$target is already running"
rel="$(release_dir "$target")"
docker image inspect "$(image):$target" >/dev/null 2>&1 || docker pull --quiet "$(image):$target" >/dev/null \
  || die "image $target is not available"
extract_release "$target"

log "Rollback $current → $target"
if FAHEEM_VERSION="$target" dc_rel "$rel" run --rm --no-TTY tools python -m app.production schema | grep -q '"state": "current"'; then
  restore_id=""
else
  restore_id="$(cat "$STATE_DIR/pre-upgrade-snapshot.$current" 2>/dev/null || true)"
  [ -n "$restore_id" ] || die "the database is newer than $target and no pre-upgrade snapshot for $current is recorded; restore one by hand: faheem-erp restore <id>"
  echo "The database has been migrated since $target. Rolling back restores snapshot $restore_id:"
  echo "everything written after that update will be LOST."
  if [ "$yes" != 1 ]; then
    printf 'Type ROLLBACK to continue: ' > /dev/tty; read -r a < /dev/tty; [ "$a" = ROLLBACK ] || die "cancelled"
  fi
fi

"$FAHEEM_HOME/current/bin/backup.sh" --reason pre-restore --note "before rollback to $target" --no-prune --locked >/dev/null \
  || die "safety backup failed — nothing was changed"
dc stop web worker >/dev/null 2>&1 || true
env_set FAHEEM_PREVIOUS_VERSION "$current"
env_set FAHEEM_VERSION "$target"
switch_current "$target"
if [ -n "$restore_id" ]; then
  dc_rel "$rel" run --rm --no-TTY tools python -m app.snapshot restore "$restore_id" --yes || die "restore of $restore_id failed — run: sudo faheem-erp doctor"
fi
dc_rel "$rel" up -d --remove-orphans $(services_up)
wait_ready 300 || die "$target did not become ready — run: sudo faheem-erp doctor"
write_state version "$target" previous_version "$current" last_rollback_at "$(now_iso)"
ok "Rolled back to $target"
