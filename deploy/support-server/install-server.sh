#!/usr/bin/env bash
# =================================================================================================
#  Faheem Remote Support server — MeshCentral (official, Apache-2.0) on a fresh Ubuntu 22.04/24.04 VPS
#
#    sudo ./install-server.sh --domain remote.faheemerp.in --email ops@faheemerp.in --owner faheem.owner
#
#  Before running: the domain's DNS A record points at this VPS, and ports 80 + 443 reach it.
#  It installs Node.js and MeshCentral (pinned), the hardened configuration (forced two-step sign-in,
#  no self sign-up, login prompt for terminals, lock-outs), Let's Encrypt, daily encrypted backups,
#  the firewall (22, 80, 443 only), fail2ban, log rotation and the owner account.
#  Running it again repairs the installation and never touches devices, accounts or backups.
#
#    --meshcentral-version X.Y.Z   default 1.2.5 (upgrades are explicit: run again with a newer version)
#    --ssh-from CIDR               restrict SSH to this address range (default: anywhere, rate-limited)
# =================================================================================================
set -Eeuo pipefail
umask 027
here="$(dirname "$(readlink -f "$0")")"
MC_HOME=/opt/meshcentral MC_DATA=/opt/meshcentral/meshcentral-data MC_BACKUPS=/var/backups/meshcentral MC_LOGS=/var/log/meshcentral
domain="" email="" owner="" version="1.2.5" ssh_from=""
step() { printf '\n\e[1m== %s\e[0m\n' "$*"; }
ok()   { printf '\e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '\e[33m!\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[31m✘ %s\e[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="$2"; shift 2 ;; --email) email="$2"; shift 2 ;; --owner) owner="$2"; shift 2 ;;
    --meshcentral-version) version="$2"; shift 2 ;; --ssh-from) ssh_from="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run with sudo"
[[ "$domain" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "--domain remote.faheemerp.in"
[[ "$email" == *@* ]] || die "--email: the address Let's Encrypt sends certificate notices to"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--meshcentral-version X.Y.Z"

step "Checking the server"
. /etc/os-release
case "$ID:$VERSION_ID" in ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ok "$PRETTY_NAME" ;; *) die "unsupported: $PRETTY_NAME" ;; esac
public_ip="$(curl -fsS -m 10 https://api.ipify.org 2>/dev/null || true)"
dns_ip="$(getent ahostsv4 "$domain" | awk 'NR==1 {print $1}')"
if [ -n "$public_ip" ] && [ "$dns_ip" != "$public_ip" ]; then
  warn "$domain resolves to ${dns_ip:-nothing}, this server is $public_ip — Let's Encrypt will fail until DNS points here"
else ok "$domain → ${dns_ip:-?}"; fi

step "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl ufw fail2ban logrotate >/dev/null
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 18 ]; then
  apt-get install -y -qq nodejs npm >/dev/null
fi
[ "$(node -p 'process.versions.node.split(".")[0]')" -ge 18 ] || die "Node.js 18 or newer is needed (found $(node -v))"
ok "Node.js $(node -v)"

step "MeshCentral $version (official package, Apache-2.0)"
id meshcentral >/dev/null 2>&1 || useradd --system --home-dir "$MC_HOME" --shell /usr/sbin/nologin meshcentral
install -d -m 750 -o meshcentral -g meshcentral "$MC_HOME" "$MC_DATA" "$MC_HOME/meshcentral-files"
install -d -m 750 -o meshcentral -g meshcentral "$MC_BACKUPS" "$MC_LOGS"
installed="$(node -p "require('$MC_HOME/node_modules/meshcentral/package.json').version" 2>/dev/null || true)"
if [ "$installed" != "$version" ]; then
  [ -z "$installed" ] || { systemctl stop meshcentral 2>/dev/null || true; warn "upgrading MeshCentral $installed → $version (backup first)"
    tar -C "$MC_HOME" -czf "$MC_BACKUPS/before-upgrade-$installed-$(date +%Y%m%d-%H%M%S).tar.gz" meshcentral-data meshcentral-files; }
  runuser -u meshcentral -- bash -c "cd '$MC_HOME' && npm install --no-audit --no-fund --loglevel=error meshcentral@$version" >/dev/null
fi
ok "MeshCentral $(node -p "require('$MC_HOME/node_modules/meshcentral/package.json').version")"

step "Configuration"
secrets="$MC_HOME/.faheem-secrets"
if [ ! -f "$secrets" ]; then
  (umask 077; printf 'BACKUP_ZIP_PASSWORD=%s\n' "$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" > "$secrets")
fi
chown root:root "$secrets"; chmod 600 "$secrets"
zip_pw="$(sed -n 's/^BACKUP_ZIP_PASSWORD=//p' "$secrets")"
cfg="$MC_DATA/config.json"
[ -f "$cfg" ] && cp -a "$cfg" "$cfg.$(date +%Y%m%d-%H%M%S).bak"
sed -e "s|{{DOMAIN}}|$domain|g" -e "s|{{ACME_EMAIL}}|$email|g" -e "s|{{BACKUP_ZIP_PASSWORD}}|$zip_pw|g" \
  "$here/config.template.json" > "$cfg.new"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$cfg.new" || die "rendered configuration is not valid JSON"
chown meshcentral:meshcentral "$cfg.new"; chmod 640 "$cfg.new"; mv -f "$cfg.new" "$cfg"
unset zip_pw
ok "Forced two-step sign-in, no self sign-up, terminal login prompt, lock-outs, daily encrypted backups"

step "Service, firewall, fail2ban, logs"
install -m 644 "$here/systemd/meshcentral.service" /etc/systemd/system/meshcentral.service
install -m 644 "$here/fail2ban/filter-meshcentral.conf" /etc/fail2ban/filter.d/meshcentral.conf
install -m 644 "$here/fail2ban/jail-meshcentral.conf" /etc/fail2ban/jail.d/meshcentral.conf
install -m 644 "$here/logrotate-meshcentral" /etc/logrotate.d/meshcentral
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
if [ -n "$ssh_from" ]; then ufw allow from "$ssh_from" to any port 22 proto tcp >/dev/null; else ufw limit 22/tcp >/dev/null; fi
ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
# MeshCentral counts and logs failed sign-ins on the web form only (fail2ban acts on those); its API
# connection (control.ashx) does not. A per-address limit on new HTTPS connections throttles guessing
# there too. Generous for real use: a browser opening the portal, and agents (one connection each).
rules=/etc/ufw/before.rules
if ! grep -q 'faheem-mc443' "$rules"; then
  cp -a "$rules" "$rules.faheem-before"
  python3 - "$rules" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
rule = ("# faheem-mc443: at most 60 new HTTPS connections a minute per address (burst 120)\n"
        "-A ufw-before-input -p tcp --dport 443 -m conntrack --ctstate NEW -m hashlimit --hashlimit-name faheem-mc443 "
        "--hashlimit-mode srcip --hashlimit-above 60/minute --hashlimit-burst 120 -j DROP\n")
marker = "# don't delete the 'COMMIT' line or these rules won't be processed"
s = s.replace(marker, rule + marker, 1) if marker in s else s.replace("COMMIT", rule + "COMMIT", 1)
open(p, "w").write(s)
PY
fi
ufw --force enable >/dev/null; ufw reload >/dev/null
ok "Firewall: 22 (SSH${ssh_from:+ from $ssh_from}), 80 (certificate + redirect), 443 — nothing else"
systemctl daemon-reload
systemctl enable fail2ban >/dev/null; systemctl restart fail2ban

if [ -n "$owner" ] && ! grep -q "\"name\":\"$owner\"" "$MC_DATA/meshcentral.db" 2>/dev/null; then
  step "Owner account $owner"
  systemctl stop meshcentral 2>/dev/null || true
  while :; do
    printf 'Password for %s (12+ characters, upper, lower, digit, symbol; not shown): ' "$owner" > /dev/tty; IFS= read -rs p1 < /dev/tty; echo > /dev/tty
    printf 'Again: ' > /dev/tty; IFS= read -rs p2 < /dev/tty; echo > /dev/tty
    [ "$p1" = "$p2" ] && [ "${#p1}" -ge 12 ] && break; warn "the passwords differ or are shorter than 12 characters"
  done
  # (MeshCentral takes the password as an argument: visible to administrators of this VPS for a moment)
  runuser -u meshcentral -- env P="$p1" bash -c "cd '$MC_HOME' && node node_modules/meshcentral --createaccount '$owner' --pass \"\$P\" --email '$email'" >/dev/null \
    || die "could not create the owner account"
  unset p1 p2
  runuser -u meshcentral -- bash -c "cd '$MC_HOME' && node node_modules/meshcentral --adminaccount '$owner'" >/dev/null
  ok "Owner $owner created (site administrator). Two-step sign-in is set up at the first login."
fi
systemctl enable meshcentral >/dev/null
systemctl restart meshcentral
for i in $(seq 1 60); do curl -fsS -m 3 -o /dev/null "https://$domain/" 2>/dev/null && break; sleep 2; done
if curl -fsS -m 5 -o /dev/null "https://$domain/"; then ok "https://$domain answers with a valid certificate"
else warn "https://$domain not answering with a valid certificate yet (first Let's Encrypt issue can take a minute): journalctl -u meshcentral -n 50"; fi
install -m 755 "$here/faheem-mc" /usr/local/bin/faheem-mc
echo
ok "Faheem Remote Support server is ready."
echo "   Portal:        https://$domain   (sign in as $owner; set up two-step sign-in)"
echo "   Next:          faheem-mc group add Hyderabad"
echo "                  faheem-mc enroll-url Hyderabad      → give to the pharmacy installer (--support-enroll-url)"
echo "                  faheem-mc tech add NAME --role technician --groups Hyderabad"
echo "   Backups:       $MC_BACKUPS (daily, encrypted zip; password in $secrets — keep a copy off this server)"
