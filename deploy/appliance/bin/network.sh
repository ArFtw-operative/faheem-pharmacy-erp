#!/usr/bin/env bash
# faheem-erp lan status|enable|disable|ca|refresh   (refresh: follow a new address — run by NetworkManager)
# faheem-erp network static [auto|ADDRESS/PREFIX] [--yes]   pin this PC's IPv4 address (NetworkManager)
# faheem-erp network dhcp [--yes]                            back to an automatic address
#
# LAN access publishes ONLY the HTTPS proxy (ports 80 → redirect / certificate download, 443 → ERP)
# on the shop network; the firewall admits private and VPN ranges only, and every sign-in from the
# network needs the authenticator code. The counter screen keeps using http://127.0.0.1.
#
# A fixed address: the best way is a DHCP reservation on the shop router for this PC's MAC address
# (shown by `faheem-erp lan status`). `network static` instead pins the current address, gateway and
# DNS on this PC — choose an address outside the router's DHCP pool to avoid a clash.
SCRIPT_NAME=network
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

default_dev() { ip -4 route show default 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="dev") {print $(i+1); exit}}'; }
dev_cidr()    { ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk '{print $4; exit}'; }
gateway()     { ip -4 route show default 2>/dev/null | awk '{print $3; exit}'; }
lan_ip()      { local d; d="$(default_dev)"; [ -n "$d" ] && dev_cidr "$d" | cut -d/ -f1; }
nm_conn()     { nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$1" '$2==d {print $1; exit}'; }

confirm() {
  [ "$yes" = 1 ] && return 0
  [ -r /dev/tty ] || die "confirmation needed: add --yes"
  printf '%s [y/N] ' "$1" > /dev/tty; read -r a < /dev/tty
  case "$a" in y|Y|yes) return 0 ;; *) die "cancelled" ;; esac
}

publish_ca() {
  local crt="$FAHEEM_DATA/proxy/data/caddy/pki/authorities/local/root.crt" i
  for i in $(seq 1 30); do [ -f "$crt" ] && break; sleep 2; done
  [ -f "$crt" ] || { warn "the proxy has not created its certificate yet"; return 0; }
  install -m 644 "$crt" "$FAHEEM_HOME/faheem-erp-ca.crt"
}

addresses() {
  local ip host; ip="$(env_get FAHEEM_LAN_IP)"; host="$(env_get FAHEEM_HOSTNAME)"
  echo "  https://$ip"
  [ -n "$host" ] && echo "  https://$host.local"
  echo "  certificate for other devices: http://$ip/faheem-erp-ca.crt"
}

yes=0 args=()
for a in "$@"; do [ "$a" = --yes ] && yes=1 || args+=("$a"); done
set -- "${args[@]}"
area="${1:-}"; cmd="${2:-status}"

case "$area:$cmd" in
  lan:status)
    d="$(default_dev)"
    echo "Interface:   ${d:-none}  $(dev_cidr "${d:-lo}")  MAC $(cat "/sys/class/net/${d:-lo}/address" 2>/dev/null)"
    echo "Gateway:     $(gateway)"
    if [ -n "$d" ] && command -v nmcli >/dev/null; then
      echo "Addressing:  $(nmcli -g ipv4.method connection show "$(nm_conn "$d")" 2>/dev/null | sed 's/auto/automatic (DHCP)/; s/manual/fixed (static)/')"
    fi
    if flag LAN_ACCESS; then echo "LAN access:  ON"; addresses; else echo "LAN access:  off (enable: sudo faheem-erp lan enable)"; fi
    ;;
  lan:enable)
    ip="$(lan_ip)"; [ -n "$ip" ] || die "this PC has no network address"
    host="$(hostname -s)"
    take_lock lan
    env_set LAN_ACCESS true; env_set FAHEEM_LAN_IP "$ip"; env_set FAHEEM_HOSTNAME "$host"
    mkdir -p "$FAHEEM_DATA/proxy/data" "$FAHEEM_DATA/proxy/config"
    systemctl enable --now faheem-erp-firewall.service >/dev/null 2>&1 || "$(dirname "$(readlink -f "$0")")/firewall.sh"
    dc up -d proxy || die "the HTTPS proxy did not start"
    publish_ca
    ok "LAN access on. From other PCs / phones on the shop network or store VPN:"; addresses
    [ "$(nmcli -g ipv4.method connection show "$(nm_conn "$(default_dev)")" 2>/dev/null)" = manual ] \
      || warn "this PC's address is automatic (DHCP) and may change: reserve $ip for it on the router, or run: sudo faheem-erp network static"
    ;;
  lan:refresh)          # the PC joined another network (e.g. moved to the store): follow its new address
    flag LAN_ACCESS || exit 0
    ip="$(lan_ip)"; [ -n "$ip" ] && [ "$ip" != "$(env_get FAHEEM_LAN_IP)" ] || exit 0
    take_lock lan 120
    log "Network address changed $(env_get FAHEEM_LAN_IP) → $ip; refreshing the HTTPS address"
    env_set FAHEEM_LAN_IP "$ip"
    dc up -d --force-recreate proxy >/dev/null 2>&1 || warn "the HTTPS proxy did not restart"
    publish_ca
    ;;
  lan:disable)
    take_lock lan
    dc stop proxy >/dev/null 2>&1 || true; dc rm -f proxy >/dev/null 2>&1 || true
    env_set LAN_ACCESS false
    ok "LAN access off — the ERP is reachable only on this PC"
    ;;
  lan:ca)
    [ -f "$FAHEEM_HOME/faheem-erp-ca.crt" ] || publish_ca
    echo "$FAHEEM_HOME/faheem-erp-ca.crt"
    ;;
  network:static)
    command -v nmcli >/dev/null || die "NetworkManager (nmcli) is not available — set a DHCP reservation on the router instead"
    d="$(default_dev)"; [ -n "$d" ] || die "no network connection"
    conn="$(nm_conn "$d")"; [ -n "$conn" ] || die "interface $d is not managed by NetworkManager"
    want="${3:-auto}"; [ "$want" = auto ] && want="$(dev_cidr "$d")"
    [[ "$want" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || die "give the address as ADDRESS/PREFIX, e.g. 192.168.1.50/24"
    gw="$(gateway)"; dns="$(nmcli -g IP4.DNS device show "$d" 2>/dev/null | tr '|' ' ' | xargs)"; dns="${dns:-$gw}"
    echo "Pin '$conn' ($d): address $want, gateway $gw, DNS $dns"
    [ "$(nmcli -g connection.type connection show "$conn" 2>/dev/null)" = 802-3-ethernet ] \
      && warn "'$conn' is a wired profile: it is used on ANY cable network this PC is plugged into. Prefer a reservation on the router."
    echo "(Wi-Fi: this applies only to network '$conn'; another Wi-Fi network gets its own automatic address.)"
    confirm "Apply this fixed address?"
    nmcli connection modify "$conn" ipv4.method manual ipv4.addresses "$want" ipv4.gateway "$gw" ipv4.dns "$dns"
    nmcli connection up "$conn" >/dev/null
    env_set FAHEEM_LAN_IP "${want%/*}"
    flag LAN_ACCESS && dc up -d --force-recreate proxy >/dev/null
    ok "Fixed address ${want%/*} on $d (undo: sudo faheem-erp network dhcp)"
    ;;
  network:dhcp)
    d="$(default_dev)"; conn="$(nm_conn "$d")"; [ -n "$conn" ] || die "no NetworkManager connection"
    confirm "Return '$conn' to an automatic (DHCP) address?"
    nmcli connection modify "$conn" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
    nmcli connection up "$conn" >/dev/null
    sleep 3; env_set FAHEEM_LAN_IP "$(lan_ip)"
    flag LAN_ACCESS && dc up -d --force-recreate proxy >/dev/null
    ok "Automatic address: $(lan_ip)"
    ;;
  *) die "usage: faheem-erp lan status|enable|disable|ca  ·  faheem-erp network static [auto|ADDRESS/PREFIX] | dhcp" ;;
esac
