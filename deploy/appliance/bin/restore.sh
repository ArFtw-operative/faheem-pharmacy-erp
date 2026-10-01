#!/usr/bin/env bash
# faheem-erp restore [<snapshot-id> | --latest] [--yes]
#
# Puts a verified snapshot back: the web process and the worker are stopped (no bill can be written
# meanwhile), the current state is snapshotted first, then the database is replaced in one
# transaction and the uploads are restored. On start the migration job brings an older snapshot up to
# this release's schema. Without an id, lists the snapshots.
SCRIPT_NAME=restore
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

id="" yes=0 locked=0
while [ $# -gt 0 ]; do
  case "$1" in
    --latest) id=latest; shift ;;
    --yes) yes=1; shift ;;
    --locked) locked=1; shift ;;
    -*) die "unknown option $1" ;;
    *) id="$1"; shift ;;
  esac
done

if [ -z "$id" ]; then
  dc run --rm --no-TTY tools python -m app.snapshot list
  echo; echo "Restore one with:  sudo faheem-erp restore <id>"
  exit 0
fi
[ "$locked" = 1 ] || take_lock restore
if [ "$id" = latest ]; then
  id="$(find "$FAHEEM_BACKUPS/snapshots" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended \
         -regex '.*/[0-9]{8}-[0-9]{6}-[a-z-]+.*' -printf '%f\n' | sort | tail -1)"
  [ -n "$id" ] || die "no snapshots in $FAHEEM_BACKUPS/snapshots"
fi

dc run --rm --no-TTY tools python -m app.snapshot verify "$id" || die "snapshot $id did not verify — nothing was changed"
if [ "$yes" != 1 ]; then
  [ -r /dev/tty ] || die "confirmation needed: add --yes"
  echo "This replaces ALL current ERP data with snapshot $id."
  printf 'Type RESTORE to continue: ' > /dev/tty
  read -r answer < /dev/tty
  [ "$answer" = RESTORE ] || die "cancelled"
fi

log "Restoring $id"
write_state operation "restoring $id" operation_at "$(now_iso)"
dc stop web worker >/dev/null 2>&1 || true
if ! dc run --rm --no-TTY tools python -m app.snapshot restore "$id" --yes; then
  warn "restore failed — starting the ERP on the data it had"
  dc up -d $(services_up) || true
  write_state operation "" last_restore_status failed
  die "restore of $id failed; the previous state is kept as a pre-restore snapshot"
fi
dc up -d $(services_up)
wait_ready 300 || die "restored $id, but the ERP did not become ready — run: sudo faheem-erp doctor"
write_state operation "" last_restore "$id" last_restore_status ok last_restore_at "$(now_iso)"
ok "Restored $id; the ERP is ready"
