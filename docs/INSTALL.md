# Installing the appliance

## 1. The PC

- Ubuntu 22.04 / 24.04 / 26.04 Desktop or Debian 12 / 13 with a desktop, **installed on the PC**
  (WSL is refused), x86_64, 4 GB RAM or more, 15 GB free on `/var` (SSD recommended).
- An administrator account for the owner / support (the one that runs `sudo`).
- Internet during installation and for updates.

The installer adds one system account, `faheem-erp`, which the ERP containers run as (it cannot log in).
Your own desktop account is used for everything else: the PC logs into it automatically at startup and
the ERP opens full screen. Your account and its password are never changed. (`--counter-user NAME`
would instead log in automatically as a separate password-less account; not the default.)

## 2. Tokens

None, when the release images are public (GitHub → the repository's *Packages* →
`faheem-pharmacy-erp` → Package settings → visibility **Public**). The installer checks and only asks
when they are private:

- **Registry token** — kept on the PC (`/etc/faheem-erp/registry.token`, root-only) to download
  release images. GitHub → Settings → Developer settings → *Tokens (classic)* → scope
  **`read:packages` only**. It cannot read or change anything else; revoking it stops updates on
  that PC.

Never paste tokens into chat, documents or Git.

## 3. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ArFtw-operative/faheem-pharmacy-erp/prod/deploy/appliance/install.sh | sudo bash
```

It asks for:

| Question | Default |
|---|---|
| Registry token (hidden) — only if the images are private | — |
| Owner user ID / full name / password (typed twice, hidden; 8+ characters, letters and numbers) | `syed.faheem` / `Syed Faheem` |
| WhatsApp invoices | no |
| Use the ERP from other PCs / phones on the shop network or store VPN | yes |
| Pin this PC's address | yes |

Only a hash of the owner password is stored. At the owner's first sign-in the ERP shows a QR code
for an authenticator app (two-step sign-in), then 10 recovery codes to print.

Unattended (all options): `sudo bash install.sh --help`. For example

```bash
sudo FAHEEM_OWNER_PASSWORD=… bash install.sh --yes --no-whatsapp --lan --static-ip
```

### Bringing the existing data

Copy the current `pharmacy.db` to the PC and add `--import-sqlite /path/pharmacy.db`. The import
brings a copy to the release's schema, copies every table, and is accepted only if every row count,
every business-history figure and every relationship match; the source file is never written.
Its user accounts come along (no owner prompt). Later, into an installation that has no business data
yet: `sudo faheem-erp import-sqlite /path/pharmacy.db`.

Authenticator secrets were sealed with the old installation's key, which this PC does not have: the
import resets two-step sign-in for those users, and each sets it up again at their next sign-in at the
counter (the password is still required).

## 4. What is installed

```
/opt/faheem-erp/releases/<version>/   host tools of each release (from its image); current -> the running one
/etc/faheem-erp/faheem.env            configuration and generated secrets (root, 0600)
/var/lib/faheem-erp/                  postgres/, uploads/, application-data/, whatsapp/, proxy/, state/
/var/log/faheem-erp/                  deploy.log (updates, backups, maintenance), app/ (ERP logs)
/var/backups/faheem-erp/              snapshots/ (verified backups), host-config/ (copy of faheem.env, root-only)
/usr/local/bin/faheem-erp             the command
```

systemd: `faheem-erp.service` (the stack at boot), `faheem-erp-maintenance.timer` (05:00 India time),
`faheem-erp-boot-check.timer`, `faheem-erp-firewall.service` (with LAN access). Logs rotate weekly;
container logs are capped at 5 × 10 MB.

Desktop shortcuts (counter user and the installing administrator) and menu entries: **Faheem
Pharmacy ERP**, **ERP Control Center**, **ERP Doctor**, **Update ERP**, **Back up ERP**, **Restart
ERP**, **Shut down ERP**, **Start ERP**, **ERP Maintenance** — see [OPERATIONS.md](OPERATIONS.md).
On GNOME, if a desktop icon shows as a file, right-click it → *Allow Launching* (once).

## 5. Using the ERP from another PC, a phone, or from home

The counter screen uses `http://127.0.0.1`. With network access on, other devices use HTTPS through
a small proxy (Caddy) on this PC:

- `https://<this PC's address>` (e.g. `https://192.168.1.50`) or `https://<pc-name>.local`
- `sudo faheem-erp lan status` shows the addresses.

**Trust the certificate once per device.** The PC has its own certificate authority
("Faheem Pharmacy ERP"). On each device open `http://<this PC's address>/faheem-erp-ca.crt`:
- Windows: open the file → Install certificate → Local machine → *Trusted Root Certification Authorities*.
- Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate.
- iPhone: install the profile, then Settings → General → About → Certificate Trust Settings → enable it.
- macOS: open in Keychain Access → *System* → set to *Always Trust*.

**A fixed address.** Best: on the shop router, reserve this PC's address for its MAC address
(`faheem-erp lan status` shows both). Or let the PC pin its current address
(`sudo faheem-erp network static`) — choose one outside the router's DHCP range.

**Safety.** Only the proxy listens on the network (ports 80 → certificate download / redirect, 443 →
ERP). PostgreSQL, WhatsApp and the ERP process itself are never published. A firewall rule on this PC
admits only private and VPN ranges (`FAHEEM_ALLOWED_NETWORKS`; default 10/8, 172.16/12, 192.168/16
and 100.64/10), so a router port-forward from the internet is dropped. Every sign-in from the network
asks for the authenticator code, not just the password.

**From home: the store VPN.** Do not forward ports on the router. Connect the home device to the
store's network with a VPN and open `https://<this PC's address>` as in the shop:
- *Tailscale* (simplest): install it on this PC (`curl -fsSL https://tailscale.com/install.sh | sh`,
  `sudo tailscale up`) and on the home device; use the PC's Tailscale address (100.x.y.z). Its range
  is already admitted. The certificate is issued for the LAN address, so on the Tailscale address
  the browser warns once unless you set `FAHEEM_LAN_IP` to it and run `sudo faheem-erp lan enable`.
- *WireGuard / router VPN*: route the VPN subnet to the shop LAN; add the VPN subnet to
  `FAHEEM_ALLOWED_NETWORKS` if it is not private, then `sudo systemctl restart faheem-erp-firewall`.

Turn it off any time: `sudo faheem-erp lan disable`.

**Moving the PC to another network (e.g. from a test setup to the store).** The counter screen is not
affected — it always uses the PC itself. A fixed address set with `network static` belongs to that one
Wi-Fi network: on the store's Wi-Fi the PC gets an automatic address, and the ERP follows it by itself
(the HTTPS address and certificate are refreshed when the network changes). At the store, then:
reserve the PC's new address on the store router (or `sudo faheem-erp network static` there), and
install the certificate on the store's devices. A *wired* profile pinned with `network static` is
used on every cable network — return it to automatic first (`sudo faheem-erp network dhcp`).

## 6. Re-running and removing

Running the installer again repairs an installation (tooling, services, shortcuts, permissions)
and never touches data, secrets or the installed version. `sudo faheem-erp uninstall` removes the
appliance after a final backup; data and backups are kept unless `--purge`.
