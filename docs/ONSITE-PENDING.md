# Pending — to finish on site (after 1.9.0)

1.9.0 (racks & boxes, purchase review filters, purchase counting engine, keyboard: tab switcher Alt+Z) is
pushed to `prod`; the pharmacy PC gets it with **Force Update ERP** (or `sudo faheem-erp force-update`).
Rehearsed on a copy of the shop's data: docs/REHEARSAL-1.9.0.md.

Remote support is in 1.9.0 but **inactive**: an ERP update never installs it. It needs the support
server and one command on the PC. Everything below is in order.

## A. Before going to the pharmacy (once, from anywhere)

1. **Support server**: a small Ubuntu 24.04 VPS (1 vCPU, 1–2 GB RAM, 20 GB disk). Note its public IP.
2. **DNS**: record `A  remote.faheemerp.in → <VPS IP>`. Check: `dig +short remote.faheemerp.in` shows the IP.
3. **Install the server** (on the VPS):
   ```
   git clone https://github.com/ArFtw-operative/faheem-pharmacy-erp
   cd faheem-pharmacy-erp/deploy/support-server
   sudo ./install-server.sh --domain remote.faheemerp.in --email <your email> --owner <your user name>
   ```
   Type the owner password (12+ characters, upper, lower, digit, symbol).
4. **Sign in** at https://remote.faheemerp.in → set up two-step sign-in in the authenticator app.
5. **Group and link** (on the VPS):
   ```
   faheem-mc group add Hyderabad
   faheem-mc enroll-url Hyderabad
   ```
   Keep the printed `https://remote.faheemerp.in/meshsettings?id=…` **private** (password manager); it lets a
   PC join the group. Never paste it in Git, WhatsApp groups or a shortcut.
6. Copy `/opt/meshcentral/.faheem-secrets` (backup password) somewhere safe off the VPS.

## B. At the pharmacy PC

1. **Update the ERP**: desktop *Force Update ERP* (or `sudo faheem-erp force-update`). Then `sudo faheem-erp health`
   → Faheem ERP OK (1.9.0), Database OK.
2. **Medicine reference prices** (not in Git; once per PC — gives purchase lines their price evidence):
   copy `Extensive_A_Z_medicines_dataset_of_India.xlsx` to the PC, then
   `sudo faheem-erp import-medicine-reference <path>/Extensive_A_Z_medicines_dataset_of_India.xlsx`.
3. **Remote support** (one command, with the link from A5):
   ```
   sudo faheem-support install --enroll-url '<link>' --name HYD-FAHEEM-PHARMACY
   ```
   It prints the `faheem-support` terminal password location (`/etc/faheem-support/support-account`) —
   copy the password into the support vault. Then **log out and in once** (or restart) so the desktop runs
   on Xorg (needed for remote desktop).
4. `sudo faheem-health` → Remote Support Agent INSTALLED, Remote Support DISABLED, MeshCentral Server REACHABLE.

## C. Acceptance test with the pharmacy (≈ 30 min)

From docs/REMOTE-SUPPORT.md §9:

| # | Do | Expect |
|---|---|---|
| 1 | Restart the PC | ERP opens by itself; support stays off |
| 2 | Double-click **Faheem Remote Support** | window "Connected", 60 min; device green in the portal within ~10 s |
| 3 | Portal → device → Desktop | the ERP screen visible and controllable (no black screen) |
| 4 | Terminal → log in `faheem-support` → `sudo faheem-support-admin status all` | works; `sudo bash` refused |
| 5 | Files → download `/var/log/faheem-erp/support/support.log` | arrives |
| 6 | Click **End Remote Support** | device grey within seconds |
| 7 | `sudo faheem-support enable --duration 5`, wait 5 min | ends by itself |
| 8 | Enable, unplug network 2 min, plug back | online again by itself |
| 9 | Enable on the phone's hotspot | online (no router setup needed) |
| 10 | Enable, `sudo reboot` | online again after boot for the time left |
| 11 | `sudo faheem-support repair` | all ✔ |
| 12 | Create technician accounts: `faheem-mc tech add NAME --role technician --groups Hyderabad`; later `faheem-mc tech audit` | everyone has two-step sign-in |

If something fails: `sudo faheem-support repair`, `/var/log/faheem-erp/support/support.log`,
`journalctl -u faheem-support-agent`; server side `journalctl -u meshcentral`.

## D. Other open items (not blocking)

* **PyMuPDF licence (AGPL)** used by the older PDF readers / invoice PDF: decide on a commercial licence or a port
  (DEPENDENCIES.md, DECISIONS.md D5).
* **CI workflow files** (`.github/workflows`) are still local only: the push token lacks the `workflow` scope —
  push them yourself with a token that has it.
* Drafts on the PC: after the update, new products prepared from invoices wait in the purchase review for the
  client's one-click confirmation (*Confirm N new products…*); racks can be set on the same screen (Alt+L).
