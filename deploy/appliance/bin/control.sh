#!/usr/bin/env bash
# faheem-erp menu — the Control Center: a keyboard menu (whiptail) over every appliance operation.
SCRIPT_NAME=control
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root
here="$(dirname "$(readlink -f "$0")")"
command -v whiptail >/dev/null || die "whiptail is missing: apt-get install whiptail"
export TERM="${TERM:-xterm-256color}" NEWT_COLORS="${NEWT_COLORS:-root=,blue}"
set +e                                   # a failed action returns to the menu instead of closing it
TITLE="Faheem Pharmacy ERP — Control Center"

state_line() {
  local ready="not answering"; curl -fsS -m 3 "$(ready_url)" >/dev/null 2>&1 && ready="running"
  echo "Version $(current_version) · ERP $ready · $(date '+%d-%b-%Y %H:%M')"
}
W() { whiptail --title "$TITLE" --backtitle "$(state_line)" "$@" 3>&1 1>&2 2>&3; }
run() {   # run "heading" command… — full-screen output, then back to the menu
  local h="$1"; shift
  clear; printf '\e[1m== %s\e[0m\n\n' "$h"
  "$@"; local rc=$?
  echo; [ $rc -eq 0 ] && echo "Done." || echo "Finished with problems (code $rc)."
  read -rp "Press Enter to return to the menu… " _
  return $rc
}
confirm() { W --yesno "$1" 12 70; }
typed() {  # typed WORD "message"
  local a; a="$(W --inputbox "$2\n\nType $1 to continue:" 13 70)" || return 1
  [ "$a" = "$1" ] || { W --msgbox "Cancelled." 8 40; return 1; }
}

doctor_menu() {
  run "Doctor — checking the whole appliance" "$here/doctor.sh"
  local items
  mapfile -t items < <("$here/doctor.sh" --json 2>/dev/null | python3 -c '
import json, sys
seen = set()
for f in json.load(sys.stdin):
    if f["severity"] != "PASS" and f["fix"] and f["fix"] not in seen:
        seen.add(f["fix"])
        print(f["fix"]); print((f["severity"] + "  " + f["title"])[:58]); print("OFF" if f["fix"] == "update" else "ON")
')
  [ ${#items[@]} -gt 0 ] || { W --msgbox "Nothing to fix." 8 40; return; }
  local chosen; chosen="$(W --checklist "Proposed fixes for what was found (Space to select):" 20 78 10 "${items[@]}")" || return
  chosen="$(tr -d '"' <<<"$chosen")"; [ -n "$chosen" ] || return
  # shellcheck disable=SC2086
  run "Applying fixes: $chosen" "$here/doctor.sh" --fix $chosen
}

update_menu() {
  local avail; avail="$("$here/update.sh" --check 2>&1 | tail -1)"
  case "$(W --menu "$avail" 15 70 4 \
      update "Update now (backup, rehearsal and automatic rollback included)" \
      version "Install a specific version" \
      rollback "Go back to the previous version" \
      history "Update history")" in
    update) confirm "Update the ERP now?\n\nThe counter is unavailable for a few minutes. If anything fails, the current version comes back automatically." && run "Update" "$here/update.sh" --yes ;;
    version) local v; v="$(W --inputbox "Version (x.y.z):" 9 50)" && [ -n "$v" ] && run "Update to $v" "$here/update.sh" "$v" ;;
    rollback) confirm "Go back to version $(env_get FAHEEM_PREVIOUS_VERSION)?" && run "Rollback" "$here/rollback.sh" ;;
    history) grep -E '\[(update|rollback)\]' "$DEPLOY_LOG" 2>/dev/null | tail -200 > /tmp/faheem-history.txt; W --textbox /tmp/faheem-history.txt 22 100 --scrolltext; rm -f /tmp/faheem-history.txt ;;
  esac
}

backup_menu() {
  case "$(W --menu "Backups are verified whole-ERP snapshots in $FAHEEM_BACKUPS/snapshots" 15 76 4 \
      now "Back up now" list "List backups" restore "Restore a backup…" drill "Prove the newest backup restores (live data untouched)")" in
    now) local n; n="$(W --inputbox "Note (optional):" 9 60)"; run "Backup" "$here/backup.sh" --reason manual --note "$n" ;;
    list) run "Backups" dc run --rm --no-TTY tools python -m app.snapshot list ;;
    restore)
      local ids=() id
      while read -r id; do ids+=("$id" ""); done < <(find "$FAHEEM_BACKUPS/snapshots" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}-[0-9]{6}-.*' -printf '%f\n' | sort -r | head -40)
      [ ${#ids[@]} -gt 0 ] || { W --msgbox "No backups yet." 8 40; return; }
      id="$(W --menu "Restore which backup? ALL data entered after it is replaced." 22 76 14 "${ids[@]}")" || return
      typed RESTORE "Restore $id over the current data? The current state is backed up first." && run "Restore $id" "$here/restore.sh" "$id" --yes ;;
    drill) run "Restore drill" dc run --rm --no-TTY tools python -m app.snapshot drill ;;
  esac
}

maintenance_menu() {
  local c; c="$(W --menu "Maintenance and performance" 19 78 9 \
      full "Run the daily maintenance now (backup, update if on, cleanup) — no reboot" \
      database "Optimise the database (VACUUM ANALYZE)" \
      cleanup "Free disk space (old images, journal, caches)" \
      app-cache "Clear the ERP's memory caches (restarts it; nobody signed out)" \
      browser-cache "Clear the counter browser's cache" \
      browser-cookies "Clear the counter browser's cache AND cookies (sign in again)" \
      all "All of the above except cookies" \
      usage "Disk usage")" || return
  case "$c" in
    full) run "Maintenance" env FAHEEM_NO_REBOOT=1 "$here/maintenance.sh" ;;
    browser-cookies) confirm "Clear cookies? The counter must sign in again." && run "Browser cache and cookies" "$here/optimize.sh" browser-cookies ;;
    *) run "Optimise: $c" "$here/optimize.sh" "$c" ;;
  esac
}

settings_menu() {
  while :; do
    local items=() k v
    for k in boot-start app-at-login auto-login auto-update daily-reboot maintenance-time backup-days backup-keep; do
      v="$("$here/settings.sh" get "$k" 2>/dev/null)"; items+=("$k" "$v")
    done
    k="$(W --menu "Select a setting to change it:" 20 70 10 "${items[@]}" \
          lan "network access — $(flag LAN_ACCESS && echo on || echo off)" \
          whatsapp "WhatsApp invoices — $(flag WHATSAPP_ENABLED && echo on || echo off)")" || return
    case "$k" in
      maintenance-time) v="$(W --inputbox "Daily maintenance time (HH:MM, India time):" 9 60 "$("$here/settings.sh" get maintenance-time)")" && run "Setting" "$here/settings.sh" set "$k" "$v" ;;
      backup-days|backup-keep) v="$(W --inputbox "$k:" 9 50 "$("$here/settings.sh" get "$k")")" && run "Setting" "$here/settings.sh" set "$k" "$v" ;;
      lan) network_menu ;;
      whatsapp) if flag WHATSAPP_ENABLED; then confirm "Turn WhatsApp invoices off? (the phone stays paired)" && run "WhatsApp" "$here/faheem-erp" whatsapp disable
                else confirm "Turn WhatsApp invoices on? Pair the phone afterwards in ERP → Settings → WhatsApp." && run "WhatsApp" "$here/faheem-erp" whatsapp enable; fi ;;
      *) v="$("$here/settings.sh" get "$k")"; [ "$v" = on ] && v=off || v=on
         confirm "Turn $k $v?" && run "Setting" "$here/settings.sh" set "$k" "$v" ;;
    esac
  done
}

network_menu() {
  case "$(W --menu "Use the ERP from other PCs / phones on the shop network or the store VPN (HTTPS)" 16 78 5 \
      status "Addresses and status" enable "Turn network access on" disable "Turn network access off" \
      static "Fix this PC's address" dhcp "Automatic address (DHCP)")" in
    status) run "Network" "$here/network.sh" lan status ;;
    enable) run "Network access" "$here/network.sh" lan enable --yes ;;
    disable) confirm "Other devices will lose access. Continue?" && run "Network access" "$here/network.sh" lan disable ;;
    static) local a; a="$(W --inputbox "Address/prefix to fix (empty = keep the current one), e.g. 192.168.1.50/24.\nPick an address outside the router's DHCP pool — or reserve it on the router instead." 12 76)" || return
            run "Fixed address" "$here/network.sh" network static "${a:-auto}" --yes ;;
    dhcp) confirm "Return to an automatic address? It may change." && run "Automatic address" "$here/network.sh" network dhcp --yes ;;
  esac
}

users_menu() {
  local list users=() u
  list="$(dc run --rm --no-TTY tools python scripts/manage.py user list 2>/dev/null)" || { W --msgbox "Could not read the users (is the ERP running?)" 8 60; return; }
  while read -r emp name rest; do
    [ -n "$name" ] && users+=("$name" "$(echo "$rest" | sed 's/  */ /g' | cut -c1-48)")
  done <<<"$list"
  [ ${#users[@]} -gt 0 ] || { W --msgbox "No users found." 8 40; return; }
  u="$(W --menu "ERP users — choose one:" 20 76 10 "${users[@]}")" || return
  case "$(W --menu "User $u" 15 64 5 password "Reset password" name "Change name" twostep "Reset two-step sign-in (new QR at next login)" unlock "Unlock (after 5 wrong passwords)")" in
    password)
      local p1 p2
      p1="$(W --passwordbox "New password for $u (8+ characters, letters and numbers):" 10 64)" || return
      p2="$(W --passwordbox "Type it again:" 9 64)" || return
      [ "$p1" = "$p2" ] || { W --msgbox "The two passwords differ — nothing changed." 8 50; return; }
      FAHEEM_NEW_PASSWORD="$p1" run "Reset password of $u" dc run --rm --no-TTY -e FAHEEM_NEW_PASSWORD tools python scripts/manage.py user passwd "$u"
      unset p1 p2 ;;
    name)
      local n; n="$(W --inputbox "New full name for $u:" 9 64)" || return
      [ -n "$n" ] && run "Change name of $u" dc run --rm --no-TTY tools python scripts/manage.py user rename "$u" --name "$n" ;;
    twostep) confirm "Reset two-step sign-in for $u?\n\nUse this when the phone or authenticator app is lost. At the next login $u scans a new QR code." \
               && run "Reset two-step sign-in of $u" dc run --rm --no-TTY tools python scripts/manage.py user reset-2fa "$u" ;;
    unlock) run "Unlock $u" dc run --rm --no-TTY tools python scripts/manage.py user unlock "$u" ;;
  esac
}

reset_data() {
  typed RESET "Reset business data?\n\nRemoves ALL products, batches and stock, suppliers, purchases and purchase returns, sales, returns, held bills, adjustments and the WhatsApp message log.\n\nKept: customers, users and passwords, settings, the WhatsApp setup and the invoice designs.\nA backup is taken first — undo it from Backups → Restore." \
    && run "Reset business data" "$here/faheem-erp" reset-test-data --yes
}

logs_menu() {
  local s; s="$(W --menu "Show the log of:" 15 60 6 web "the ERP" worker "WhatsApp queue" migrate "migration job" postgres "database" proxy "network proxy" deploy "updates / backups / maintenance")" || return
  clear
  if [ "$s" = deploy ]; then less +G "$DEPLOY_LOG"; else dc logs --no-color --tail 1000 "$s" 2>&1 | less +G; fi
}

power_menu() {
  case "$(W --menu "Power" 13 64 4 stop "Shut down the ERP (PC stays on)" start "Start the ERP" reboot "Restart the PC" poweroff "Turn the PC off")" in
    stop) confirm "Shut down the ERP? Billing stops until it is started again." && run "Shut down ERP" "$here/faheem-erp" stop ;;
    start) run "Start ERP" "$here/faheem-erp" start ;;
    reboot) confirm "Restart the PC now?" && { "$here/faheem-erp" stop; systemctl reboot; } ;;
    poweroff) confirm "Turn the PC off now?" && { "$here/faheem-erp" stop; systemctl poweroff; } ;;
  esac
}

while :; do
  choice="$(W --menu "Choose with ↑ ↓ and Enter (Esc to leave):" 24 74 15 \
      status "Status" \
      restart "Restart ERP" \
      stop "Shut down ERP" \
      start "Start ERP" \
      update "Updates…" \
      force "Force update — install the newest fixes from prod now" \
      backup "Backups…" \
      doctor "Doctor — find and fix problems" \
      maintenance "Maintenance and performance…" \
      settings "Settings (boot, counter screen, updates, reboot…)" \
      network "Network access…" \
      logs "Logs…" \
      power "Power (restart / turn off the PC)…" \
      users "User control (password, name, two-step sign-in)…" \
      reset "Reset business data (inventory, sales, purchases…)" \
      support "Write a support report")" || { clear; exit 0; }
  case "$choice" in
    status) run "Status" "$here/faheem-erp" status ;;
    restart) confirm "Restart the ERP? Billing pauses for about a minute." && run "Restart ERP" "$here/faheem-erp" restart ;;
    stop) confirm "Shut down the ERP? Billing stops until it is started again." && run "Shut down ERP" "$here/faheem-erp" stop ;;
    start) run "Start ERP" "$here/faheem-erp" start ;;
    update) update_menu ;;
    force) confirm "Install the newest fixes from prod now?\n\nA backup is taken first. The ERP is unavailable for a few minutes while it rebuilds." && run "Force update" "$here/force-update.sh" ;;
    backup) backup_menu ;;
    doctor) doctor_menu ;;
    maintenance) maintenance_menu ;;
    settings) settings_menu ;;
    network) network_menu ;;
    logs) logs_menu ;;
    power) power_menu ;;
    users) users_menu ;;
    reset) reset_data ;;
    support) run "Support report" "$here/doctor.sh" --report ;;
  esac
done
