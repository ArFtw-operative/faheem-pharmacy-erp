#!/usr/bin/env bash
# faheem-erp uninstall [--purge]
# Removes the services, timers, kiosk autostart, CLI and tooling. Business data
# (/var/lib/faheem-erp), backups (/var/backups/faheem-erp) and faheem.env are KEPT unless --purge,
# which asks for the typed word PURGE and still keeps /var/backups/faheem-erp.
SCRIPT_NAME=uninstall
# shellcheck source=../lib.sh
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
require_root

purge=0; [ "${1:-}" = --purge ] && purge=1
printf 'Uninstall Faheem Pharmacy ERP from this PC? Type UNINSTALL: ' > /dev/tty
read -r a < /dev/tty; [ "$a" = UNINSTALL ] || die "cancelled"
take_lock uninstall

"$FAHEEM_HOME/current/bin/backup.sh" --reason manual --note "before uninstall" --no-prune --locked >/dev/null \
  || warn "final backup failed"
systemctl disable --now faheem-erp-maintenance.timer faheem-erp-boot-check.timer faheem-erp.service 2>/dev/null || true
dc down --remove-orphans 2>/dev/null || true
rm -f /etc/systemd/system/faheem-erp*.service /etc/systemd/system/faheem-erp*.timer
systemctl daemon-reload
rm -f /usr/local/bin/faheem-erp /etc/logrotate.d/faheem-erp /usr/share/applications/faheem-erp*.desktop \
      /usr/share/polkit-1/actions/com.faheem.erp.policy
rm -rf /etc/systemd/system/faheem-erp-maintenance.timer.d
for u in faheem $(env_get FAHEEM_ADMIN_USER); do
  d="$(getent passwd "$u" | cut -d: -f6 || true)"; [ -n "$d" ] || continue
  rm -f "$d"/.config/autostart/faheem-erp-kiosk.desktop "$d"/Desktop/faheem-erp*.desktop "$(runuser -u "$u" -- xdg-user-dir DESKTOP 2>/dev/null)"/faheem-erp*.desktop
done
rm -rf "$FAHEEM_HOME"
if [ "$purge" = 1 ]; then
  printf 'Delete ALL ERP data in %s (backups in %s are kept)? Type PURGE: ' "$FAHEEM_DATA" "$FAHEEM_BACKUPS" > /dev/tty
  read -r a < /dev/tty
  if [ "$a" = PURGE ]; then rm -rf "$FAHEEM_DATA" "$FAHEEM_ETC" "$FAHEEM_LOGS"; ok "Data removed"; fi
fi
ok "Uninstalled. Kept: $FAHEEM_BACKUPS$([ "$purge" = 1 ] || echo ", $FAHEEM_DATA, $FAHEEM_ETC")"
