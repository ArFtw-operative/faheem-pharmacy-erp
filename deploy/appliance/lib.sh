# shellcheck shell=bash
# Shared by every Faheem Pharmacy ERP host script. Paths can be overridden for tests only.
set -Eeuo pipefail
umask 027

FAHEEM_HOME="${FAHEEM_HOME:-/opt/faheem-erp}"            # tooling: releases/<version>/, current -> releases/<version>
FAHEEM_ETC="${FAHEEM_ETC:-/etc/faheem-erp}"              # faheem.env (secrets, 0600), registry.token (0600)
FAHEEM_DATA="${FAHEEM_DATA:-/var/lib/faheem-erp}"        # postgres/, uploads/, application-data/, whatsapp/, state/
FAHEEM_LOGS="${FAHEEM_LOGS:-/var/log/faheem-erp}"
FAHEEM_BACKUPS="${FAHEEM_BACKUPS:-/var/backups/faheem-erp}"
FAHEEM_LOCK="${FAHEEM_LOCK:-/run/lock/faheem-erp.lock}"
ENV_FILE="$FAHEEM_ETC/faheem.env"
TOKEN_FILE="$FAHEEM_ETC/registry.token"
STATE_DIR="$FAHEEM_DATA/state"
DEPLOY_LOG="$FAHEEM_LOGS/deploy.log"
export FAHEEM_DATA FAHEEM_LOGS FAHEEM_BACKUPS

if [ -t 1 ]; then c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_bad=$'\e[31m'; c_dim=$'\e[2m'; c_0=$'\e[0m'
else c_ok=""; c_warn=""; c_bad=""; c_dim=""; c_0=""; fi

_stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
log()  { local m="$*"; echo "${c_dim}$(_stamp)${c_0} $m"; _logfile "INFO  $m"; }
ok()   { local m="$*"; echo "${c_ok}✔${c_0} $m"; _logfile "OK    $m"; }
warn() { local m="$*"; echo "${c_warn}!${c_0} $m" >&2; _logfile "WARN  $m"; }
die()  { local m="$*"; echo "${c_bad}✘ $m${c_0}" >&2; _logfile "ERROR $m"; exit 1; }
_logfile() { { [ -d "$FAHEEM_LOGS" ] && echo "$(_stamp) [${SCRIPT_NAME:-faheem-erp}] $*" >> "$DEPLOY_LOG"; } 2>/dev/null || true; }

require_root() {
  [ "${FAHEEM_TEST:-0}" = 1 ] && return 0
  [ "$(id -u)" -eq 0 ] || die "run as root: sudo faheem-erp ${SCRIPT_NAME:-}"
}

# One host operation at a time (update, backup, restore, maintenance…). Waits up to $2 seconds.
take_lock() {
  mkdir -p "$(dirname "$FAHEEM_LOCK")"
  exec 9>"$FAHEEM_LOCK"
  flock -w "${2:-600}" 9 || die "another Faheem ERP operation is running (${1:-}); try again later"
}

# --- faheem.env -----------------------------------------------------------------------------------
env_get() { [ -r "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed 's/^"\(.*\)"$/\1/'; }
env_set() {        # replace in place, atomically, keeping mode and owner
  local key="$1" value="$2" tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  if grep -q "^$key=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"; echo "$key=$value" >> "$tmp"
  fi
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  chown --reference="$ENV_FILE" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$ENV_FILE"
}
flag() { case "$(env_get "$1" | tr '[:upper:]' '[:lower:]')" in true|1|yes|on) return 0;; *) return 1;; esac; }

image()           { echo "$(env_get FAHEEM_IMAGE)"; }
current_version() { env_get FAHEEM_VERSION; }
release_dir()     { echo "$FAHEEM_HOME/releases/$1"; }

# --- docker compose -------------------------------------------------------------------------------
# dc_rel <release dir> <compose args…>: the stack as that release describes it. The image version comes
# from faheem.env unless FAHEEM_VERSION is set in the environment (used to try a release before switching).
dc_rel() {
  local rel="$1"; shift
  local profiles=""                        # "tools" jobs are started only when named (compose run tools …)
  flag WHATSAPP_ENABLED && profiles="whatsapp"
  flag LAN_ACCESS && profiles="${profiles:+$profiles,}lan"
  COMPOSE_PROFILES="${COMPOSE_PROFILES:-$profiles}" \
    docker compose --project-name faheem-erp --project-directory "$rel" --env-file "$ENV_FILE" \
    -f "$rel/compose.yaml" -f "$rel/compose.prod.yaml" "$@"
}
dc() { dc_rel "$FAHEEM_HOME/current" "$@"; }

# The running stack's services (tools is a one-off job, never "up").
SERVICES_UP=(postgres migrate web worker)
services_up() {
  local s=("${SERVICES_UP[@]}")
  flag WHATSAPP_ENABLED && s+=(whatsapp)
  flag LAN_ACCESS && s+=(proxy)
  echo "${s[@]}"
}

port() { env_get FAHEEM_PORT || true; }
ready_url() { echo "http://127.0.0.1:$(port | sed 's/^$/8000/')/health/ready"; }

# wait_ready [seconds]: the web process answers /health/ready with 200
wait_ready() {
  local limit="${FAHEEM_READY_TIMEOUT:-${1:-240}}" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if curl -fsS -m 4 "$(ready_url)" >/dev/null 2>&1; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

# Appliance status for the ERP's System Health page (read-only inside the containers).
write_state() {   # write_state <key> <value> …
  mkdir -p "$STATE_DIR"
  local f="$STATE_DIR/status.json" tmp; tmp="$(mktemp "$f.XXXXXX")"
  python3 - "$f" "$@" > "$tmp" <<'PY'
import json, sys
path, *kv = sys.argv[1:]
try:
    data = json.load(open(path))
except Exception:
    data = {}
for k, v in zip(kv[::2], kv[1::2]):
    data[k] = v
print(json.dumps(data, indent=1, sort_keys=True))
PY
  chmod 644 "$tmp"; mv -f "$tmp" "$f"
}
now_iso() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# --- versions & the registry ----------------------------------------------------------------------
version_gt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }

# Release versions published in GHCR (x.y.z tags only). With a stored token when the images are private,
# anonymously when they are public. The token never appears on a command line.
registry_versions() {
  local repo bearer url
  repo="$(image)"; repo="${repo#ghcr.io/}"
  url="https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io"
  if [ -s "$TOKEN_FILE" ]; then
    bearer="$( { printf 'user = "faheem-appliance:'; tr -d '\n' < "$TOKEN_FILE"; printf '"\n'; } \
               | curl -fsS -m 20 -K - "$url" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  else
    bearer="$(curl -fsS -m 20 "$url" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  fi
  [ -n "$bearer" ] || die "GHCR refused access to $(image) (private images need a registry token: run the installer again)"
  printf 'header = "Authorization: Bearer %s"\n' "$bearer" \
    | curl -fsS -m 20 -K - "https://ghcr.io/v2/${repo}/tags/list?n=1000" \
    | grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' | tr -d '"' | sort -V
}
latest_version() { registry_versions | tail -1; }

# Copy the host tooling shipped inside an image into releases/<version>/ (never over a running one).
extract_release() {
  local version="$1" rel tmp cid
  rel="$(release_dir "$version")"
  [ -f "$rel/.complete" ] && return 0
  tmp="$(mktemp -d "$FAHEEM_HOME/releases/.tmp-$version.XXXXXX")"
  cid="$(docker create "$(image):$version")"
  docker cp "$cid:/app/compose.yaml" "$tmp/compose.yaml" >/dev/null
  docker cp "$cid:/app/compose.prod.yaml" "$tmp/compose.prod.yaml" >/dev/null
  docker cp "$cid:/app/deploy/appliance/." "$tmp/" >/dev/null
  docker rm "$cid" >/dev/null
  chmod -R a+rX,go-w "$tmp"; chmod 755 "$tmp"/bin/* 2>/dev/null || true     # the desktop user runs the kiosk from here
  echo "$version" > "$tmp/VERSION"; touch "$tmp/.complete"
  rm -rf "$rel"; mv "$tmp" "$rel"
}
switch_current() { ln -sfn "releases/$1" "$FAHEEM_HOME/current.new" && mv -Tf "$FAHEEM_HOME/current.new" "$FAHEEM_HOME/current"; }

# Last line of a snapshot run: "Snapshot <id> written and verified …"
snapshot_id_from() { sed -n 's/^Snapshot \([^ ]*\) written.*/\1/p' | tail -1; }

# --- host integration (systemd units, CLI, polkit, menu entries, desktop shortcuts) -----------------
# Installed from the release in use, so an update that changes them takes effect too.
desktop_dir() {   # desktop_dir <user> → that user's Desktop folder (created)
  local u="$1" home d
  home="$(getent passwd "$u" | cut -d: -f6 || true)"; [ -n "$home" ] || return 1
  d="$(runuser -u "$u" -- xdg-user-dir DESKTOP 2>/dev/null)"
  [ -n "$d" ] && [ "$d" != "$home" ] || d="$home/Desktop"
  install -d -o "$u" -g "$(id -gn "$u")" "$d"; echo "$d"
}
install_host_files() {
  [ "${FAHEEM_TEST:-0}" = 1 ] && return 0
  local rel="$FAHEEM_HOME/current" u d f uid
  for f in "$rel"/systemd/*; do install -m 644 "$f" /etc/systemd/system/; done
  systemctl daemon-reload
  install -m 644 "$rel/logrotate/faheem-erp" /etc/logrotate.d/faheem-erp
  ln -sfn "$rel/bin/faheem-erp" /usr/local/bin/faheem-erp
  install -d /usr/share/polkit-1/actions
  install -m 644 "$rel/polkit-com.faheem.erp.policy" /usr/share/polkit-1/actions/com.faheem.erp.policy
  for f in "$rel"/desktop/*.desktop; do install -m 644 "$f" /usr/share/applications/; done
  # follow a new network address (the PC moved to another Wi-Fi / network)
  [ -d /etc/NetworkManager/dispatcher.d ] && install -m 755 -o root -g root "$rel/networkmanager/90-faheem-erp" /etc/NetworkManager/dispatcher.d/90-faheem-erp
  # the app may start the stack without a password, for these users only, and only that command
  local users="" sudoers=/etc/sudoers.d/faheem-erp tmp
  local people
  people="$(printf '%s\n' "$(env_get FAHEEM_DESK_USER)" "${FAHEEM_ADMIN_USER:-$(env_get FAHEEM_ADMIN_USER)}" | grep . | sort -u | tr '\n' ' ')"
  for u in $people; do id "$u" >/dev/null 2>&1 && users+="${users:+, }$u"; done
  if [ -n "$users" ]; then
    tmp="$(mktemp)"
    printf '# Faheem Pharmacy ERP: the desktop app starts the ERP if it is not running
%s ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block faheem-erp.service
' "$users" > "$tmp"
    if visudo -cf "$tmp" >/dev/null 2>&1; then install -m 440 -o root -g root "$tmp" "$sudoers"; fi
    rm -f "$tmp"
  fi
  # the app opens full screen at login of the desktop account (your own unless a counter account was chosen)
  for u in $(env_get FAHEEM_DESK_USER); do
    id "$u" >/dev/null 2>&1 || continue
    d="$(getent passwd "$u" | cut -d: -f6 || true)/.config/autostart"
    install -d -o "$u" -g "$(id -gn "$u")" "$d"
    [ -f "$d/faheem-erp-app.desktop" ] || install -m 644 -o "$u" -g "$(id -gn "$u")" "$rel/kiosk/faheem-erp-app-autostart.desktop" "$d/faheem-erp-app.desktop"
  done
  # shortcuts on the desktop of the desktop account and of the administrator who installed
  for u in $people; do
    id "$u" >/dev/null 2>&1 || continue
    d="$(desktop_dir "$u")" || continue
    uid="$(id -u "$u")"
    for f in "$rel"/desktop/*.desktop; do
      # "Switch to administrator" only makes sense when a separate counter account logs in automatically
      [ "$(basename "$f")" = faheem-erp-switch-user.desktop ] && [ -z "$(env_get FAHEEM_COUNTER_USER)" ] && continue
      install -m 755 -o "$u" -g "$(id -gn "$u")" "$f" "$d/$(basename "$f")"
      # GNOME shows a launcher as runnable only when it is marked trusted (needs the user's session)
      [ -S "/run/user/$uid/bus" ] && runuser -u "$u" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        gio set "$d/$(basename "$f")" metadata::trusted true 2>/dev/null || true
    done
  done
}
