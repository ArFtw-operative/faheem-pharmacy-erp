#!/usr/bin/env bash
# faheem-erp force-update — install the newest fixes now (desktop: "Force Update ERP").
#   - a newer published release exists → the normal transactional update to it (rehearsal, backup, rollback)
#   - otherwise → a verified backup, then the installer of the newest prod commit rebuilds the ERP from
#     that commit (pinned by its id, so a cached older installer can never be used). Data, accounts and
#     every choice made at install (network access, WhatsApp, automatic login) are kept.
SCRIPT_NAME=force-update
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root
here="$(dirname "$(readlink -f "$0")")"
REPO="${FAHEEM_REPO:-ArFtw-operative/faheem-pharmacy-erp}"

log "Force update: looking for the newest version"
newest="$( (latest_version) 2>/dev/null || true)"
if [ -n "$newest" ] && version_gt "$newest" "$(current_version)"; then
  log "Published release $newest is newer than $(current_version) — updating to it"
  exec "$here/update.sh" "$newest" --yes
fi

sha="$(curl -fsS -m 20 -H "Accept: application/vnd.github.sha" "https://api.github.com/repos/$REPO/commits/prod")" \
  || die "GitHub could not be reached — check the internet connection"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "unexpected answer from GitHub"
log "Newest prod commit: ${sha:0:7}"
"$here/backup.sh" --reason pre-upgrade --note "before force update (prod ${sha:0:7})" --no-prune >/dev/null \
  || die "the backup failed — nothing was changed"
installer="$(mktemp)"
curl -fsSL -m 120 "https://raw.githubusercontent.com/$REPO/$sha/deploy/appliance/install.sh" -o "$installer" \
  || die "could not download the installer of ${sha:0:7}"
SUDO_USER="${SUDO_USER:-$(env_get FAHEEM_ADMIN_USER)}" FAHEEM_SOURCE_REF="$sha" bash "$installer" --yes --build-from-source
rc=$?
rm -f "$installer"
[ $rc -eq 0 ] && ok "Force update finished (prod ${sha:0:7})" || die "the force update did not finish (code $rc) — run: sudo faheem-erp doctor"
