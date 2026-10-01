#!/usr/bin/env bash
# faheem-erp backup [--reason manual|scheduled|pre-upgrade|pre-restore] [--note TEXT] [--no-prune]
#
# A verified whole-ERP snapshot (database dump + fingerprint of the business history from the same
# moment, uploaded files), written by the application's own snapshot tool into
# /var/backups/faheem-erp/snapshots/<id>/ and made read-only. The host configuration (faheem.env, whose
# secret key also unlocks the stored two-step sign-in secrets) is copied root-only beside it.
# Retention: BACKUP_RETENTION_DAYS (default 30), never fewer than BACKUP_KEEP_MIN (default 14).
# Prints the snapshot id on the last line.
SCRIPT_NAME=backup
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

reason=manual note="" prune=1 locked=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reason) reason="$2"; shift 2 ;;
    --note) note="$2"; shift 2 ;;
    --no-prune) prune=0; shift ;;
    --locked) locked=1; shift ;;       # the caller already holds the operation lock
    *) die "unknown option $1" ;;
  esac
done
[ "$locked" = 1 ] || take_lock backup

store="$FAHEEM_BACKUPS/snapshots"
mkdir -p "$store" "$FAHEEM_BACKUPS/host-config"
chown "$(env_get FAHEEM_UID):$(env_get FAHEEM_GID)" "$store" 2>/dev/null || true
chmod 700 "$FAHEEM_BACKUPS/host-config"

log "Backup ($reason) starting"
out="$(dc run --rm --no-TTY tools python -m app.snapshot create --reason "$reason" --note "$note" 2>&1)" \
  || { echo "$out" >&2; write_state last_backup_status failed last_backup_at "$(now_iso)"; die "backup failed"; }
id="$(echo "$out" | snapshot_id_from)"
[ -n "$id" ] || { echo "$out" >&2; die "backup finished without a snapshot id"; }
for f in faheem.env "faheem.env.$id"; do      # root-only: the secret key also unlocks two-step sign-in secrets
  install -m 600 "$ENV_FILE" "$FAHEEM_BACKUPS/host-config/$f"
done
write_state last_backup "$id" last_backup_status ok last_backup_at "$(now_iso)"
ok "Backup $id written and verified"

if [ "$prune" = 1 ]; then
  days="$(env_get BACKUP_RETENTION_DAYS)"; days="${days:-30}"
  keep="$(env_get BACKUP_KEEP_MIN)"; keep="${keep:-14}"
  cutoff="$(date -d "-$days days" +%Y%m%d)"
  mapfile -t all < <(find "$store" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended \
                       -regex '.*/[0-9]{8}-[0-9]{6}-[a-z-]+.*' -printf '%f\n' | sort -r)
  removed=0
  for i in "${!all[@]}"; do
    [ "$i" -lt "$keep" ] && continue
    snap="${all[$i]}"
    [ "${snap:0:8}" -lt "$cutoff" ] || continue
    chmod -R u+w "$store/$snap" && rm -rf "${store:?}/$snap"
    rm -f "$FAHEEM_BACKUPS/host-config/faheem.env.$snap"
    removed=$((removed + 1))
  done
  [ "$removed" -gt 0 ] && log "Retention: removed $removed snapshot(s) older than $days days (kept at least $keep)"
fi
echo "$id"
