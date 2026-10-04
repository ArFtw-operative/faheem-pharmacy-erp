# shellcheck shell=bash
# Faheem Remote Support — shared by the support scripts. A separate package from the ERP release:
# its own version (VERSION), its own folder (/opt/faheem-erp/support), never touched by ERP updates.
# Paths can be overridden for tests only.
set -Eeuo pipefail
umask 027

SUPPORT_HOME="${SUPPORT_HOME:-/opt/faheem-erp/support}"          # VERSION, bin/, meshagent/ (agent, .msh, its identity)
SUPPORT_ETC="${SUPPORT_ETC:-/etc/faheem-support}"                 # support.env (settings, readable), support-account (0600)
SUPPORT_STATE="${SUPPORT_STATE:-/var/lib/faheem-erp/support}"     # session.json (active window)
SUPPORT_LOGS="${SUPPORT_LOGS:-/var/log/faheem-erp/support}"       # support.log, sudo-io/
SUPPORT_SYSTEMD="${SUPPORT_SYSTEMD:-/etc/systemd/system}"
SUPPORT_BIN_DIR="${SUPPORT_BIN_DIR:-/usr/local/bin}"
SUPPORT_CONF="$SUPPORT_ETC/support.env"
AGENT_DIR="$SUPPORT_HOME/meshagent"
AGENT_BIN="$AGENT_DIR/meshagent"
AGENT_MSH="$AGENT_DIR/meshagent.msh"
AGENT_UNIT="faheem-support-agent.service"
EXPIRY_UNIT="faheem-support-expiry"            # transient timer + service made per session (systemd-run)
SESSION="$SUPPORT_STATE/session.json"
SUPPORT_LOG="$SUPPORT_LOGS/support.log"

_stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }
# events go to the support log; never credentials, tokens, keys or the enrollment file's identifiers
slog() { { [ -d "$SUPPORT_LOGS" ] && echo "$(_stamp) [${SCRIPT_NAME:-faheem-support}] $*" >> "$SUPPORT_LOG"; } 2>/dev/null || true; }
say()  { echo "$*"; }
fail() { echo "✘ $*" >&2; slog "ERROR $*"; exit 1; }

conf_get() {   # "" when unset (never a failure: callers run under set -e)
  [ -r "$SUPPORT_CONF" ] || return 0
  sed -n "s/^$1=//p" "$SUPPORT_CONF" | tail -1 | sed 's/^"\(.*\)"$/\1/'
}
conf_set() {
  local key="$1" value="$2" tmp
  install -d -m 755 "$SUPPORT_ETC" 2>/dev/null || true
  # no secrets in support.env (server address, name, duration): readable so the window can show status
  [ -f "$SUPPORT_CONF" ] || { : > "$SUPPORT_CONF"; chmod 644 "$SUPPORT_CONF"; }
  tmp="$(mktemp "$SUPPORT_CONF.XXXXXX")"
  if grep -q "^$key=" "$SUPPORT_CONF"; then
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$SUPPORT_CONF" > "$tmp"
  else
    cat "$SUPPORT_CONF" > "$tmp"; echo "$key=$value" >> "$tmp"
  fi
  chmod 644 "$tmp"; mv -f "$tmp" "$SUPPORT_CONF"
}
conf_flag() { case "$(conf_get "$1" | tr '[:upper:]' '[:lower:]')" in true|1|yes|on) return 0;; *) return 1;; esac; }

server_url()   { conf_get SUPPORT_SERVER; }                       # https://remote.faheemerp.in
server_host()  { server_url | sed -E 's#^[a-z]+://##; s#[:/].*$##'; }
server_port()  { local p; p="$(server_url | sed -nE 's#^[a-z]+://[^:/]+:([0-9]+).*#\1#p')"; echo "${p:-443}"; }
support_name() { conf_get SUPPORT_NAME; }
duration_default() { local d; d="$(conf_get SUPPORT_DURATION_MIN)"; [[ "$d" =~ ^[0-9]+$ ]] && echo "$d" || echo 60; }
package_version()  { cat "$SUPPORT_HOME/VERSION" 2>/dev/null || echo "not installed"; }
# readable by the desktop user (the agent folder itself is root-only: it holds the enrollment and identity)
installed() { [ -f "$SUPPORT_HOME/.enrolled" ]; }

is_root() { [ "${FAHEEM_TEST:-0}" = 1 ] || [ "$(id -u)" -eq 0 ]; }

now_epoch() { date +%s; }
iso() { date -u -d "@$1" '+%Y-%m-%dT%H:%M:%SZ'; }

# --- the support window ---------------------------------------------------------------------------
session_get() {   # session_get <key> → value from session.json ("" when there is no session)
  [ -r "$SESSION" ] || return 0
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$SESSION" "$1"
}
session_write() {  # session_write <started epoch> <expires epoch> <minutes> <by>
  install -d -m 755 "$SUPPORT_STATE"
  local tmp; tmp="$(mktemp "$SESSION.XXXXXX")"
  python3 - "$@" > "$tmp" <<'PY'
import json, sys
s, e, m, by = sys.argv[1:5]
print(json.dumps({"started": int(s), "expires": int(e), "minutes": int(m), "by": by}, indent=1))
PY
  chmod 644 "$tmp"; mv -f "$tmp" "$SESSION"
}
remaining() {      # seconds left in the current window (0 = none / expired)
  local e; e="$(session_get expires)"
  [[ "$e" =~ ^[0-9]+$ ]] || { echo 0; return; }
  local r=$(( e - $(now_epoch) )); [ "$r" -gt 0 ] && echo "$r" || echo 0
}

# --- connectivity checks (used by status, enable, repair) -------------------------------------------
dns_ok()    { getent hosts "$(server_host)" >/dev/null 2>&1; }
server_ok() {      # the server answers HTTPS with a valid certificate (curl verifies it)
  curl -fsS -o /dev/null -m 8 ${SUPPORT_CURL_INSECURE:+-k} "$(server_url)/" 2>/dev/null \
    || curl -sS -o /dev/null -m 8 -w '%{http_code}' ${SUPPORT_CURL_INSECURE:+-k} "$(server_url)/" 2>/dev/null | grep -qE '^[1-5][0-9][0-9]$'
}
internet_ok() {    # any well-known resolver answers, or the support server itself resolves
  dns_ok && return 0
  getent hosts one.one.one.one >/dev/null 2>&1 || getent hosts dns.google >/dev/null 2>&1
}
agent_active()  { systemctl is-active --quiet "$AGENT_UNIT"; }
agent_running() { pgrep -f "^$AGENT_BIN" >/dev/null 2>&1; }
agent_connected() {  # an established connection from the agent to the support server's port
  local ips port pids line peer ip
  port="$(server_port)"
  ips="$(getent ahosts "$(server_host)" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
  [ -n "$ips" ] || return 1
  pids=" $(pgrep -f "^$AGENT_BIN" 2>/dev/null | tr '\n' ' ') "
  [ "$pids" != "  " ] || return 1
  # as root, only the agent's own sockets count; otherwise (status for the desktop window) the remote end
  # of the connection must be the server (never our own listening side on a shared machine)
  local with_pid=0; is_root && [ "${FAHEEM_TEST:-0}" != 1 ] && with_pid=1
  while read -r line; do
    peer="$(echo "$line" | awk '{print $4}')"
    if [ "$with_pid" = 1 ]; then
      local owner; owner="$(echo "$line" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
      case "$pids" in *" $owner "*) ;; *) continue ;; esac
    fi
    for ip in $ips; do
      [ "$peer" = "$ip:$port" ] || [ "$peer" = "[$ip]:$port" ] || [ "$peer" = "[::ffff:$ip]:$port" ] && return 0
    done
  done < <(if [ "$with_pid" = 1 ]; then ss -Htnp state established 2>/dev/null; else ss -Htn state established 2>/dev/null; fi)
  return 1
}
