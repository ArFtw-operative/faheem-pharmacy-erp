#!/usr/bin/env bash
# faheem-erp-firewall.service: admit the ERP's network ports (80, 443 — the HTTPS proxy) only from
# private and VPN address ranges. Docker-published ports bypass ufw, so the rules live in Docker's
# DOCKER-USER chain. A router port-forward from the internet is therefore dropped at this PC.
#   FAHEEM_ALLOWED_NETWORKS (faheem.env) — space-separated CIDRs; default: RFC 1918 + 100.64.0.0/10
#   (CGNAT range used by Tailscale / many WireGuard setups).
SCRIPT_NAME=firewall
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

nets="$(env_get FAHEEM_ALLOWED_NETWORKS)"
nets="${nets:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10}"
chain=FAHEEM-ERP

if [ "${1:-}" = stop ]; then
  for p in 80 443; do
    while iptables -D DOCKER-USER -p tcp -m conntrack --ctorigdstport "$p" --ctdir ORIGINAL -j "$chain" 2>/dev/null; do :; done
  done
  iptables -F "$chain" 2>/dev/null || true; iptables -X "$chain" 2>/dev/null || true
  exit 0
fi

iptables -L DOCKER-USER -n >/dev/null 2>&1 || die "Docker's DOCKER-USER chain is missing (is Docker running with iptables?)"
iptables -N "$chain" 2>/dev/null || iptables -F "$chain"
iptables -A "$chain" -s 127.0.0.0/8 -j RETURN
for n in $nets; do iptables -A "$chain" -s "$n" -j RETURN; done
iptables -A "$chain" -j DROP
for p in 80 443; do
  iptables -C DOCKER-USER -p tcp -m conntrack --ctorigdstport "$p" --ctdir ORIGINAL -j "$chain" 2>/dev/null \
    || iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport "$p" --ctdir ORIGINAL -j "$chain"
done
log "Firewall: ERP network ports admitted from $nets"
