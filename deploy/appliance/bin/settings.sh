#!/usr/bin/env bash
# faheem-erp settings                      show every setting
# faheem-erp settings set KEY VALUE        change one
#
#   boot-start        on|off   start the ERP when the PC starts (faheem-erp.service)
#   app-at-login      on|off   open the ERP full screen when the desktop user logs in
#   auto-login        on|off   log the desktop user in automatically at boot
# The desktop user is your own account (FAHEEM_DESK_USER; set by the installer).
#   auto-update       on|off   install new releases during maintenance
#   daily-reboot      on|off   restart the PC after the morning maintenance
#   maintenance-time  HH:MM    time of the daily maintenance (India time), default 05:00
#   backup-days       N        keep backups this many days ...
#   backup-keep       N        ... but never fewer than N snapshots
SCRIPT_NAME=settings
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

desk="$(env_get FAHEEM_DESK_USER)"; desk="${desk:-$(env_get FAHEEM_ADMIN_USER)}"
desk_home="$( [ -n "$desk" ] && getent passwd "$desk" | cut -d: -f6 || true)"
APP_AUTOSTART="$desk_home/.config/autostart/faheem-erp-app.desktop"
TIMER_DROPIN=/etc/systemd/system/faheem-erp-maintenance.timer.d/time.conf
onoff() { case "$1" in on|true|yes|1) echo on ;; off|false|no|0) echo off ;; *) die "use on or off" ;; esac; }

gdm_conf() { local f; for f in /etc/gdm3/custom.conf /etc/gdm3/daemon.conf; do [ -f "$f" ] && { echo "$f"; return; }; done; }

get() {
  case "$1" in
    boot-start) systemctl is-enabled --quiet faheem-erp.service 2>/dev/null && echo on || echo off ;;
    app-at-login) [ -n "$desk_home" ] && [ -f "$APP_AUTOSTART" ] && ! grep -q '^Hidden=true' "$APP_AUTOSTART" && echo on || echo off ;;
    auto-login)
      if [ -n "$(gdm_conf)" ]; then grep -q '^AutomaticLoginEnable=true' "$(gdm_conf)" && echo on || echo off
      elif [ -f /etc/lightdm/lightdm.conf.d/50-faheem-erp.conf ] || [ -f /etc/sddm.conf.d/50-faheem-erp.conf ]; then echo on
      else echo off; fi ;;
    auto-update) flag AUTO_UPDATE && echo on || echo off ;;
    daily-reboot) flag DAILY_HOST_REBOOT && echo on || echo off ;;
    maintenance-time) sed -n 's/^OnCalendar=\*-\*-\* \([0-9:]*\):00 .*/\1/p' "$TIMER_DROPIN" 2>/dev/null | tail -1 | grep . || echo 05:00 ;;
    backup-days) v="$(env_get BACKUP_RETENTION_DAYS)"; echo "${v:-30}" ;;
    backup-keep) v="$(env_get BACKUP_KEEP_MIN)"; echo "${v:-14}" ;;
    *) die "unknown setting $1" ;;
  esac
}

set_() {
  local key="$1" val="${2:-}"
  case "$key" in
    boot-start)
      if [ "$(onoff "$val")" = on ]; then systemctl enable faheem-erp.service >/dev/null
      else systemctl disable faheem-erp.service >/dev/null; fi ;;
    app-at-login)
      [ -n "$desk_home" ] || die "no desktop account recorded (FAHEEM_DESK_USER) — run the installer from your desktop account"
      install -d -o "$desk" -g "$(id -gn "$desk")" "$desk_home/.config/autostart"
      install -m 644 -o "$desk" -g "$(id -gn "$desk")" "$FAHEEM_HOME/current/kiosk/faheem-erp-app-autostart.desktop" "$APP_AUTOSTART"
      [ "$(onoff "$val")" = on ] || printf 'X-GNOME-Autostart-enabled=false\nHidden=true\n' >> "$APP_AUTOSTART" ;;
    auto-login)
      [ "$(onoff "$val")" = off ] || [ -n "$desk" ] || die "no desktop account recorded (FAHEEM_DESK_USER)"
      local f; f="$(gdm_conf)"
      if [ -n "$f" ]; then
        sed -i '/^AutomaticLoginEnable *=/d; /^AutomaticLogin *=/d' "$f"
        [ "$(onoff "$val")" = on ] && sed -i "/^\\[daemon\\]/a AutomaticLoginEnable=true\\nAutomaticLogin=$desk" "$f"
      elif [ -d /etc/lightdm ]; then
        if [ "$(onoff "$val")" = on ]; then install -d /etc/lightdm/lightdm.conf.d; printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$desk" > /etc/lightdm/lightdm.conf.d/50-faheem-erp.conf
        else rm -f /etc/lightdm/lightdm.conf.d/50-faheem-erp.conf; fi
      elif [ -d /etc/sddm.conf.d ]; then
        if [ "$(onoff "$val")" = on ]; then printf '[Autologin]\nUser=%s\n' "$desk" > /etc/sddm.conf.d/50-faheem-erp.conf
        else rm -f /etc/sddm.conf.d/50-faheem-erp.conf; fi
      else die "no supported login manager"; fi ;;
    auto-update) env_set AUTO_UPDATE "$([ "$(onoff "$val")" = on ] && echo true || echo false)" ;;
    daily-reboot) env_set DAILY_HOST_REBOOT "$([ "$(onoff "$val")" = on ] && echo true || echo false)" ;;
    maintenance-time)
      [[ "$val" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "use HH:MM (24-hour), e.g. 05:00"
      install -d "$(dirname "$TIMER_DROPIN")"
      printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* %s:00 Asia/Kolkata\n' "$val" > "$TIMER_DROPIN"
      systemctl daemon-reload; systemctl restart faheem-erp-maintenance.timer ;;
    backup-days) [[ "$val" =~ ^[0-9]+$ ]] && [ "$val" -ge 7 ] || die "at least 7 days"; env_set BACKUP_RETENTION_DAYS "$val" ;;
    backup-keep) [[ "$val" =~ ^[0-9]+$ ]] && [ "$val" -ge 3 ] || die "at least 3"; env_set BACKUP_KEEP_MIN "$val" ;;
    *) die "unknown setting $key" ;;
  esac
  log "Setting $key = $(get "$key")"
  ok "$key: $(get "$key")"
}

KEYS=(boot-start app-at-login auto-login auto-update daily-reboot maintenance-time backup-days backup-keep)
case "${1:-show}" in
  show) for k in "${KEYS[@]}"; do printf '%-18s %s\n' "$k" "$(get "$k")"; done
        printf '%-18s %s\n' "next maintenance" "$(systemctl show faheem-erp-maintenance.timer -p NextElapseUSecRealtime --value 2>/dev/null)" ;;
  get) get "${2:?key}" ;;
  set) take_lock settings 60; set_ "${2:?key}" "${3:?value}" ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac
