# Faheem Remote Support

The pharmacy double-clicks **Faheem Remote Support**. Within seconds the PC appears **online** in our
portal (`https://remote.faheemerp.in`) under its readable name (`HYD-FAHEEM-PHARMACY`); a technician
signs in (password + two-step code), opens the device and uses Desktop, Terminal or Files. No session
code, no IP address, no port forwarding, no TeamViewer / AnyDesk, nothing exposed on the pharmacy PC.
After 60 minutes (configurable) — or when the pharmacy presses **End Remote Support** — the agent stops.

Built on the official MeshCentral (github.com/Ylianst/MeshCentral, Apache-2.0), pinned to 1.2.5.

```
Pharmacy PC (Ubuntu)                                     Support server (VPS)
  Faheem ERP, PostgreSQL, kiosk                            remote.faheemerp.in :443 (TLS, Let's Encrypt)
  faheem-support-agent.service  ── outbound wss:443 ──►    MeshCentral (systemd, user meshcentral)
     (off until the shortcut; ends by timer)                  forced 2FA · roles · audit · daily backups
  shortcut → support-window → pkexec faheem-support          ufw 22/80/443 · fail2ban · rate limit
```

## 1. Server (once)

1. **DNS**: an `A` record `remote.faheemerp.in` → the VPS's public IPv4 (and `AAAA` if it has IPv6).
   Check: `dig +short remote.faheemerp.in`.
2. **Install** on a fresh Ubuntu 22.04/24.04 VPS (1 vCPU, 1–2 GB RAM, 20 GB is plenty for hundreds of PCs):
   ```
   git clone https://github.com/ArFtw-operative/faheem-pharmacy-erp && cd faheem-pharmacy-erp/deploy/support-server
   sudo ./install-server.sh --domain remote.faheemerp.in --email ops@faheemerp.in --owner faheem.owner [--ssh-from 203.0.113.0/24]
   ```
   It installs Node.js + MeshCentral 1.2.5, renders `config.template.json` (§ Security), obtains the TLS
   certificate from Let's Encrypt (renewed automatically by MeshCentral), opens only 22 / 80 / 443,
   rate-limits new HTTPS connections per address, installs fail2ban, log rotation and `faheem-mc`, and
   creates the owner account (password typed twice, hidden).
3. **Sign in** at `https://remote.faheemerp.in` as the owner: you are made to set up two-step sign-in
   (authenticator app) before anything else.
4. **Groups**: `faheem-mc group add Hyderabad` (one group per city / client chain; MeshCentral groups are flat,
   the device name carries city-client-branch).

`faheem-mc` asks for your user, password and current two-step code each run.

## 2. Pharmacy PC

The appliance installer sets it up when given the group's enrollment link (a secret — it lets a PC join
the group; never put it in Git, a script, a shortcut or a chat):

```
faheem-mc enroll-url Hyderabad            # on the server → https://remote.faheemerp.in/meshsettings?id=…
curl -fsSL …/deploy/appliance/install.sh | sudo bash -s -- --support-enroll-url 'https://…' --support-name HYD-FAHEEM-PHARMACY
```

On an existing installation: `sudo faheem-support install --enroll-url 'https://…' --name HYD-FAHEEM-PHARMACY`.

It installs the support package to `/opt/faheem-erp/support` (own version, see § Updates), downloads the
Linux agent from our server (agent and server always match), writes the agent settings with the readable
name, creates the `faheem-support` terminal account, puts the shortcut on the desktop of the counter and
administrator accounts, switches the login session to Xorg (remote desktop, § Desktop) and checks the
server is reachable. **Support stays off.**

**Naming**: `<CITY>-<CLIENT>[-<BRANCH>][-<NUMBER>]`, upper case: `HYD-FAHEEM-PHARMACY`, `HYD-MODERN-PHARMA-01`.
The name is set by the PC when it first connects (`agentName`); `faheem-mc rename OLD NEW` changes it later.
MeshCentral also shows the hostname, OS and agent version; client, branch, ERP version and installation
date can be kept in the device's Notes.

### Layout on the PC

| Path | What |
|---|---|
| `/opt/faheem-erp/support/` | the support package: `VERSION`, `bin/`, `systemd/`, `polkit/`, `desktop/`, `icons/` |
| `/opt/faheem-erp/support/bin/` | `faheem-support` (also `/usr/local/bin`), `enable-support.sh`, `disable-support.sh`, `support-status.sh`, `install-agent.sh`, `repair-agent.sh`, `faheem-support-admin`, `support-window` |
| `/opt/faheem-erp/support/meshagent/` (0700) | the agent, its settings (`.msh`, the enrollment — 0600) and its identity (`meshagent.db`) |
| `/etc/faheem-support/support.env` | server, name, duration, resume-after-restart (no secrets) |
| `/etc/faheem-support/support-account` (0600) | the `faheem-support` terminal password (record it in the support vault) |
| `/var/lib/faheem-erp/support/session.json` | the open support window (start, end) |
| `/var/log/faheem-erp/support/` | `support.log` (events), `agent.log`; rotated weekly, 12 kept |

(The ERP's own layout is `/opt/faheem-erp`, `/etc/faheem-erp`, `/var/log/faheem-erp`; support lives beside it,
outside the ERP releases.)

### systemd

| Unit | Role |
|---|---|
| `faheem-support-agent.service` | the agent. **No `[Install]`: never starts at boot.** Restarts itself if it crashes. |
| `faheem-support-expiry.timer` / `.service` | made for each support window (`systemd-run`); runs `faheem-support disable --reason expired` |
| `faheem-support-resume.service` | at boot: if a window was open and time remains (and `SUPPORT_RESUME_AFTER_REBOOT=true`), restarts the agent for the time left; otherwise makes sure support is off |

ERP, database, kiosk, updater and backups start at boot as before; remote support does not.

## 3. Using it

**The pharmacy**: double-click **Faheem Remote Support**. A small window says *Remote assistance is
ENABLED · Status: Connected · Time remaining 00:59:32 · Support reference HYD-FAHEEM-PHARMACY* with
**[End Remote Support]**. No password is asked (polkit allows exactly this — enable for the configured
time, disable, status — for the person at the PC). Closing the window ends support; the timer ends it
anyway.

**Us**: `https://remote.faheemerp.in` → sign in (password + code) → My Devices → the green (online)
`HYD-FAHEEM-PHARMACY` → **Desktop** / **Terminal** / **Files**. The pharmacy sees a notification and the
privacy bar while we are connected.

**Terminal** opens a login prompt (`terminal.linuxshell = login`): sign in as `faheem-support`
(password in the vault / `/etc/faheem-support/support-account`). Root actions go through one checked command:

```
sudo faheem-support-admin status all            sudo faheem-support-admin journal faheem-erp 300
sudo faheem-support-admin logs web 500          sudo faheem-support-admin ps
sudo faheem-support-admin restart erp|docker|network|proxy
sudo faheem-support-admin erp doctor|health|backup|update --check|version
sudo faheem-support-admin health | disk | memory | reboot
sudo faheem-health
```

It works with sudo and sudo-rs (Ubuntu 26.04), never offers a shell, editor, pager or `docker exec/run`, and logs
every use with the technician's name (`support.log`, journal). If full administration is needed for a
deployment: `sudo faheem-support install … --sudo full` gives `faheem-support` full sudo (password per use,
journal-logged) — a deliberate, documented choice. Note: the agent itself runs as root (desktop capture and
file access need it); the portal's per-technician rights, two-step sign-in and MeshCentral's own event log are
what control and record access to it.

**Administrator emergency mode** (on the PC, root):

```
sudo faheem-support enable [--duration 120]     sudo faheem-support disable
sudo faheem-support status [--json]             sudo faheem-support repair
sudo faheem-erp support …   (same)             sudo faheem-erp health   (= sudo faheem-health)
```

## 4. Health, logs, repair

`sudo faheem-health`:

```
Faheem ERP             OK (1.9.0)
Database               OK
Kiosk                  OK (counter)
Backup                 OK (last 2026-10-04 05:00)
Updater                OK
Remote Support Agent   INSTALLED (HYD-FAHEEM-PHARMACY, package 1.0.0)
Remote Support         DISABLED
MeshCentral Server     REACHABLE (remote.faheemerp.in)
```

`faheem-erp doctor` also reports remote support (and fixes with `--fix support-repair`).

Status words: **Connected** · **Connecting…** · **Offline** (no internet) · **Support server unavailable** ·
**Agent error** · **Support expired** · **Disabled**. They come from the service state, the agent process,
DNS, the server's TLS answer and the agent's own established connection to the server — not assumed.

`support.log` records: install / enrollment, enabled (by whom, until when), connected, disabled (manual /
expired / after restart / uninstall), restored after restart, repair runs, admin-helper use, upgrades. Never
passwords, tokens, keys or the enrollment identifiers.

`sudo /opt/faheem-erp/support/bin/repair-agent.sh` (= `faheem-support repair`): agent program, settings and
permissions, service registration (restored from the package, never enabled at boot), command links, internet,
DNS, TLS to the server, the connection when a window is open; restarts the agent when needed. It never wipes or
re-enrolls the device (re-enrolling: `faheem-support install --enroll-url …`, identity kept).

## 5. Updates

* **ERP updates never touch remote support.** `faheem-erp update` swaps the ERP release; each release *carries*
  the support package (`releases/<v>/support-package/`) but nothing installs it. If an ERP update fails, the
  agent still works and we can repair the PC remotely.
* **Support package** (`deploy/support/VERSION`, e.g. 1.0.3) is upgraded only on purpose:
  `sudo faheem-support upgrade` (from the installed ERP release's copy) or `--from DIR`. The agent's identity and
  enrollment are kept; refuses to go back to an older version.
* **The agent binary** follows the server: when the server is upgraded (`install-server.sh --meshcentral-version X`),
  MeshCentral updates connected agents itself.

## 6. Security

| Requirement | How |
|---|---|
| HTTPS only, valid certificate | MeshCentral on 443 with Let's Encrypt (`letsencrypt.production`); 80 only redirects / answers the certificate challenge |
| Strong passwords, MFA | `passwordRequirements`: 12+ chars, upper/lower/digit/symbol, common passwords banned, **force2factor** |
| No anonymous access / sign-up | `newAccounts: false`; no shared links are created; agent invite codes off |
| Lock-out, fail2ban | web sign-in: 5 failures in 10 min (password or 2FA) → 30 min lock-out, logged to `/var/log/meshcentral/auth.log`; fail2ban bans the address 1 h |
| API brute force | MeshCentral 1.2.5 does not count / log failed sign-ins on its API connection (`control.ashx`); the firewall limits new HTTPS connections to 60/min per address |
| Minimal ports | VPS: 22 (optionally from one range), 80, 443. Pharmacy PC: **nothing inbound** (no SSH, RDP, VNC, database or ERP ports) — the agent only dials out |
| Secrets | enrollment link / `.msh` given at install time, stored root-only on the PC; backup zip password root-only on the VPS; nothing in Git |
| Audit | MeshCentral events per user and device (sessions, files, terminal); `support.log` on the PC; sudo / helper use in the journal |
| Consent / visibility | the pharmacy turns support on; notifications + privacy bar while connected; ends by timer |

**Two-step sign-in gap**: MeshCentral makes an account set up two-step sign-in at its *first web sign-in*;
until then a password alone works (also through the API). Create accounts with a temporary password
(`faheem-mc tech add` forces a change), have the person sign in right away, and run `faheem-mc tech audit`
— it lists accounts without two-step sign-in (exit 1) so they can be fixed or removed.

### Roles

`faheem-mc tech add NAME --role … --groups G1,G2` (temporary password; changed and 2FA set at first sign-in):

| Role | Server | Device groups |
|---|---|---|
| owner | site administrator (all server rights) | full |
| admin (Support Admin) | no new groups | full rights on the listed groups |
| technician | no new groups, no tools | remote control (desktop, terminal, files), notes, chat; sees only own events |
| readonly | no new groups, no tools | sees devices, no control / terminal / files |

Nobody shares the owner password.

## 7. Remote desktop on Linux

MeshCentral's Linux remote desktop captures **X11 (Xorg)** sessions; Wayland gives a black screen. The support
installer sets `WaylandEnable=false` in GDM (backup `custom.conf.faheem-before-support`; takes effect at the next
login) so the counter session — and the Chromium kiosk in it — runs on Xorg. `--keep-wayland` skips this (then
Desktop will not work). The login screen itself is GDM's own session; it is visible once the counter account is
logged in (automatic login).

## 8. Backup, restore, moving the server

* MeshCentral backs itself up daily (`autoBackup`) to `/var/backups/meshcentral` as an encrypted zip (password in
  `/opt/meshcentral/.faheem-secrets`); 30 days kept. **Copy them off the VPS** (e.g. a nightly `rclone`/`rsync`
  to object storage) together with a copy of `.faheem-secrets` kept elsewhere.
* Restore: install a server with `install-server.sh` (same domain), stop it, unzip the backup into
  `/opt/meshcentral/meshcentral-data` (and `meshcentral-files`), `chown -R meshcentral:`, start.
* **Moving to another VPS without touching the pharmacies**: the PCs trust the server by its *agent certificate*
  (in `meshcentral-data`: `agentserver-cert-*`, `root-cert-*`, plus the database). Copy the whole
  `meshcentral-data` folder to the new VPS (install-server.sh first, then stop and replace), then point the
  `remote.faheemerp.in` DNS record at the new address. Agents reconnect by name to the same certificate; nothing
  is reinstalled. Keep the old VPS until every device shows online on the new one.

## 9. Verification

Done in development against the **real MeshCentral 1.2.5 server and the real Linux x86-64 agent** it serves
(Ubuntu 26.04, local server with a self-signed certificate):

* enrollment from the group's link; the device appears as **HYD-FAHEEM-PHARMACY** (not a random id), online
  seconds after `enable`, offline after `disable` and after the expiry timer fires;
* commands run on the device and their output returned (`meshctrl runcommand`); a file uploaded and downloaded
  byte-identical (`meshctrl upload/download`) — the same channels Terminal and Files use;
* support server stopped during a window → status *Support server unavailable*; restarted → the agent
  reconnected by itself (≈ 6 s), status *Connected*;
* restart during a window → `faheem-support-resume` restored it for the time left; with
  `SUPPORT_RESUME_AFTER_REBOOT=false` support stayed off;
* the hardened configuration starts; web sign-in lock-out after 5 failures (even the right password refused);
  the auth log lines match the fail2ban filter (5 of 5); technician / read-only rights land as intended
  (rights masks 24712 / 9728); sudo rules accepted by sudo-rs `visudo`.
* automated: `tests/test_remote_support.py` (11) and `tests/test_appliance.py` (19).

**Still to be done on the real target before go-live** (needs the pharmacy's Ubuntu desktop PC and the VPS,
not available in development): Desktop on the Xorg counter session with the Chromium kiosk; Terminal login as
`faheem-support` and the `faheem-support-admin` commands; the shortcut window (zenity) and password-less pkexec
under the counter account; Let's Encrypt issue and renewal on `remote.faheemerp.in`; and the network matrix from
outside the LAN — mobile hotspot (CGNAT), router restart, public IP change, internet cut and restore. The
checklist:

| # | Test | Expected |
|---|---|---|
| 1 | fresh install with `--support-enroll-url`, reboot | ERP opens; `faheem-health`: agent INSTALLED, support DISABLED, server REACHABLE |
| 2 | double-click the shortcut | window *Connected*, device online in the portal within ~10 s |
| 3 | Desktop | the kiosk visible and controllable (no black screen) |
| 4 | Terminal → `faheem-support` → `sudo faheem-support-admin status all` | works; `sudo bash` refused |
| 5 | Files: download `/var/log/faheem-erp/support/support.log` | arrives |
| 6 | End Remote Support | offline within seconds |
| 7 | enable, wait 60 min (or `--duration 5` as root) | offline at the end; *Support expired* |
| 8 | enable, pull the network cable 2 min, reconnect | back online by itself |
| 9 | enable on a phone hotspot | online (outbound only) |
| 10 | enable, `sudo reboot` | online again after boot for the time left |
| 11 | `sudo faheem-erp update` | support still works; `faheem-support version` unchanged |
| 12 | `sudo faheem-support repair` | all ✔ |
