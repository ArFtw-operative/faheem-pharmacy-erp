# Changelog

Release notes for Faheem Pharmacy. Versions follow semantic versioning; every release
lists its database migrations. Upgrades keep all business history (see docs/UPGRADES.md).

## 1.9.2 (2026-10-04)

- **Two counters at once (shop PC + a browser on another PC, same or different user):** every change bumps a
  per-area counter (`sync_versions`, migration f6b8d0a2c4e7, additive) in the same transaction; each open
  ERP checks it every 4 s and refreshes the screens whose data moved — Inventory, Racks, Purchases and the
  open purchase, POS search, Sales, Customers, Adjustments — keeping the selection and never during an
  open dialog. Tested with two browsers: a rack move / category change on one shows on the other in 3–4 s.
- A browser opened while another PC is in use starts with its own tabs (it no longer copies that PC's open
  bills); a PC quiet for 10 minutes still hands its work over (replaced / crashed PC).
- Resuming a parked bill is one guarded update: two counters resuming the same bill — exactly one gets it.
- **Top bar never cuts modules off:** it tightens its spacing on narrow screens / zoom and puts what still does
  not fit in **More ▾** (Settings, Racks … stay one click away; the open module is never hidden).

## 1.9.1 (2026-10-04)

- **Purchase drafts made before an update are counted by the new engine:** each draft records the engine
  version that counted it; an open draft counted by an older engine is counted again once, automatically,
  when the purchase list or the draft is opened (a person's corrections are kept, posted lines are never
  touched, a failure leaves the draft as it was). On a copy of the shop's data: NR03895 0 → 38 lines ready,
  invoice 1234 2 → 13; stock, ledger and sales unchanged.

## 1.9.0 (2026-10-04)

- **Faheem Remote Support (MeshCentral, Apache-2.0):** the pharmacy double-clicks *Faheem Remote Support*;
  the PC connects outbound to `remote.faheemerp.in` and shows online under its readable name
  (HYD-FAHEEM-PHARMACY) for 60 minutes (configurable), then the agent stops by a systemd timer; *End Remote
  Support* ends it at once. No codes, IPs or open ports. A separate, versioned support package
  (`deploy/support`, 1.0.0 in `/opt/faheem-erp/support`) never touched by ERP updates; installed by the
  appliance installer (`--support-enroll-url`, `--support-name`) or `faheem-support install`. `faheem-support
  enable|disable|status|repair|upgrade`, `faheem-health`, doctor checks, terminal login as `faheem-support` with
  one checked root helper (works with sudo-rs), restore after a reboot for the time left, Xorg for remote
  desktop. Server kit (`deploy/support-server`): installer, hardened config (forced 2FA, lock-outs, no sign-up,
  terminal login prompt, daily encrypted backups), fail2ban, firewall rate limit, `faheem-mc` (groups,
  enrollment links, technician roles, 2FA audit). docs/REMOTE-SUPPORT.md.

- **Racks and boxes (new Racks tab, Alt+Shift+K):** racks with optional boxes, every product's place,
  its history and rack inventory on any past day (docs/LOCATIONS.md, DECISIONS D25–D30). Inventory:
  Rack / Box columns, Location filter (unassigned, no box, any rack or box), sort by rack, F8 moves the
  marked products (one request, validated, recorded), side panel shows the location and its history.
  POS search, quick lookup and the bill show the rack code first (`R-A03 · Fever & Pain · B02`); a rack
  or box code typed in POS lists what is kept there. Purchase review: Rack column (set / current /
  suggested), filters (Find, Category, Rack, Form), Mark all shown, Set rack (Alt+L), Accept suggested
  racks (Alt+Shift+L); confirmed racks are applied when the purchase posts. Reports → Rack Inventory
  (as of any day, Σ racks + Unassigned = total stock) and Rack History. Inventory import reads Rack
  Code / Box Code (unknown codes are queued, never created); export includes them. Permissions
  `rack.*` and `box.manage`. Migration `e3a5c7e9b1d4` is additive: existing products start Unassigned.
- **Inventory:** Starts with filter (A–Z, 0–9); bulk-action bar for marked products that offers only
  what changes something ("Enable 3 disabled", "Disable 8 enabled", "Archive 4 with no stock").
- **Keyboard:** tab switcher (Alt+Z: most recent first; hold Alt and tap Z, release to switch); holding
  Alt+N / Alt+W opens / closes tabs one after another (stops at an unsaved bill and at 9 bills);
  every key a screen handles is listed in Keyboard shortcuts. "Remove unreceived line" moved from
  Alt+D (kept by the browser, never worked) to Ctrl+Delete.

- **Inventory — complete product control in one place:** right-click (or Shift+F10) a product, or several
  marked with Ctrl+click / Shift+↑↓ / Space, to **change category** (drop-down of the live list, with
  "＋ New category…"; Purchases uses the same picker), **disable**
  (kept in stock and history, not sold), **enable**, **move to the recycle bin** (only at zero stock) or
  **restore**. New filters: **Packaging** (counted as plain packs / corrected by a user / set automatically)
  replaces the Units of measure tab, and **Show** (active / disabled / recycle bin). "View batches" left the
  menu (the side panel already shows them).
- **POS:** a disabled product still appears in search, struck through, marked "Disabled — not for sale",
  and cannot be added; the server refuses it too. Editing an old bill that already carried it still works.
- **Masters renamed Categories & Forms**, holding only Categories and Item forms.
- **Grids:** grey header, white / off-white rows, light grey rules and right-aligned figures; the blue
  focus border that showed as stray lines on the right and bottom edges is replaced by an accent under
  the header.

- **Purchases — received stock always shown:** billed + free is shown for every line, in the unit the
  supplier billed ("3 purchase packs" until the pack is known, then "3 strips", "2 bottles", "6 boxes").
  "Quantity not confirmed" is gone; only the **Stock equivalent** column waits for the pack conversion
  ("20 strips, 200 capsules", "2 × 60 mL", or "Pack conversion unresolved").
- **Purchases — exception inbox:** a document opens on the lines that need attention; auto-accepted and
  warning lines are one click away (Alt+V). A Confidence column shows each line's decision (Auto / Warning /
  Review / Blocked) with per-field confidence and where each value came from.
- **Purchases — Roll back to draft (Alt+B):** posted lines (selected, or the whole invoice) are taken out of
  stock with linked reversal movements and return to review with every correction kept. Refused, with the
  reason, if the stock was already sold, returned or adjusted.
- **Purchases — Change category only (F7):** one click (or a number key) sets the category of the selected
  lines' products, on open or received lines; stock is not touched.
- **Item forms:** 36 built-in forms (softgels, IV fluids, ampoules, vials, syringes, needles, cannulas, test
  strips, respules, suppositories …) and the pharmacy's own: every form list ends with "Create new form…";
  Masters → Item forms lists and hides them.
- **Packing correction (Alt+K):** say what one invoice Qty is (retail pack, outer box or base unit) and save it
  for this invoice, as product packaging, or as a supplier alias — the next invoice resolves on its own.
- **Match inspector (Alt+J):** why a line got its product and pack: signals, reasons, other candidates.
- **Automatic intake:** supplier recognised from its learned layout + invoice-number shape; names that
  differ only superficially match (strengths and SR/DS/… markers never ignored); supplier packing aliases,
  product packaging store and a mapping store with trust and history; GS1 barcodes fill a blank batch/expiry.
  New products the automation prepares wait in a **new-product queue** ("Confirm N new products…", one
  click for the category); `purchase_auto_create_products=on` restores automatic creation.
- **Every line counted:** when nothing certain decides a pack, the count is proposed from weighed evidence
  (supplier history, the invoice's other lines, product and reference MRP, typical unit prices) with its
  reasons; otherwise whole packs / strips are counted. The client reviews ("Proposed — check") and posts;
  posting teaches the supplier. Implausibly priced counts and wrong catalogue packs are flagged.
- **Smart restock:** a batch restocked at another MRP or expiry is reconciled by rule (lower printed MRP
  while stock remains), never an error; routine warnings are accepted when posting; a different size of a
  product becomes its own product; "Post N ready" posts what is ready while the rest waits.
- **Scans and photos:** an OCR route exists but is switched off (`purchase_scan_import`); this pharmacy
  imports text PDF, CSV and Excel.
- **Ledger:** purchase receipts record purchase, retail and base quantities beside base units.
- **Regression corpus** in the build (zero wrong auto-accepts), `scripts/purchase_corpus.py` report,
  `manage.py purchases bootstrap|metrics`.

**Migrations:** `d4f6a8c0e2b5` adds `item_forms`, `product_packagings`, `supplier_packaging_aliases`,
`supplier_invoice_profiles`, `mapping_history`, `import_metrics`, nullable trust columns on
`supplier_product_maps` and level columns on `inventory_movements` (additive only).

## 1.8.0 — 2026-10-02

- **Purchases:** receipt decisions and supplier unit verification are kept per line and learned per
  supplier; intake is automated from reviewed supplier and packaging evidence. Fixed-width PDF invoices
  are recovered in full and printed counts enforced. Unregistered-pharmacy purchases are supported and
  GSTINs are informational only (no posting gate). Physical quantity adjustment counts received strips
  without changing what the invoice billed; stock equivalents, form and base unit are shown per line.
- **Purchases → Add line:** a line can be typed in full by hand ("Enter manually" in the product picker),
  then created as a new product or matched later with F4.
- **Purchase audit reports:** selected supplier invoices, browseable without date filters, with
  complete exports; accounting charges kept concise; recorded receipt conversion shown.
- **Inventory:** item and all-batch report replaces the detail strip; specifications and the supplier of
  each batch are in the right-hand batch panel; equivalents shown.
- **Stock adjustments:** product selection and dialog keys fixed; strip adjustment prefilled with the
  resolved received quantity.
- **POS:** the Customer field has its own search and list (no longer the item box); "#" in the item box
  jumps to it.
- **Keyboard:** the shortcuts window groups each category once, current screen first, with a fixed
  header. Esc always closes the topmost pop-up, even when focus has left it.
- **Workspace:** no clipping; overflow tabs are reachable with arrows and the "All tabs" list.
- **Desktop → ERP Maintenance** now opens a menu: **Users** (reset 2FA / two-step sign-in, reset password,
  unlock, enable / disable, change name — the list shows each user's 2FA state) and the optimisation tasks.
  Resetting 2FA also unlocks the account. CLI: `faheem-erp menu maintenance`.

**Migrations:** `c9e2a5b8d1f4` adds `purchase_items.receipt_decision` and
`supplier_product_maps.receipt_conventions` (additive). **Fixtures:** `tests/fixtures/releases/1.8.0.db`.

## 1.7.0 — 2026-10-01

- **Purchases:** delete unreceived drafts from the register or document (Alt+Delete, reassignable),
  with confirmation and an audit record; right-click also opens, cancels and refreshes. Received
  purchases remain protected. Removing a line is mapped to Alt+D (Delete also works in the grid).
- **General items:** batch, expiry, purchase rate, MRP and category are optional. Missing values
  remain missing; unknown cost is never counted as free stock. New unclassified goods default to
  General / one unit per pack. Malformed supplied values and ambiguous conversions still need review.
  Unlabelled, undated receipts retain their own purchase cost and price.
- **Inventory:** Loose sale is editable with stock present. Automatic defaults use pack conversion,
  sale unit and dosage form (including single tablets); explicit Yes/No choices are respected.
- **POS:** after completing a customer's sale, Y/N offers the existing follow-up screen before
  invoice / WhatsApp choices. Saving or closing the follow-up continues to the invoice options.
- **WhatsApp:** Indian numbers need no country prefix; select a country for international numbers,
  or enter +country code. Country metadata validates national prefixes and number lengths; saved
  international alternate numbers retain their country code.

**Migrations:** none. **Fixtures:** `tests/fixtures/releases/1.7.0.db`.

## 1.6.1 — 2026-10-01

- **Fix:** the business-data reset failed on PostgreSQL once stock adjustments existed (deleted in the
  wrong order). It now deletes in the order the schema's own foreign keys require; tested on both
  databases with every kind of record.
- **Control Center → Reset business data** (main menu, and the desktop entry "Reset ERP Business Data"):
  inventory, sales, purchases, returns, held bills, adjustments and the WhatsApp log in one step;
  customers, users, settings, WhatsApp setup and invoice designs kept; typed RESET, backup first.
- **Control Center → User control:** reset a user's password (typed twice, hidden, never echoed),
  change their name, reset two-step sign-in, unlock. CLI: `faheem-erp user passwd|rename|reset-2fa|unlock`.

**Migrations:** none. **Fixtures:** `tests/fixtures/releases/1.6.1.db`.

## 1.6.0 — 2026-10-01

- **Crash recovery ("graceful snaps"):** open tabs and every unfinished POS bill (lines, customer,
  discounts, payment entered, manual-bill fields) are saved to the database a moment after each change,
  every 15 s and when the page closes — per user and counter. After a crash, power cut or reboot the
  next login reopens the same tabs and bills and says so in the status bar. A bill that was completed
  just before the crash is recognised and not billed twice.

**Migrations:** `b8d0f2a4c6e9` adds `workspace_snapshots` (additive). **Fixtures:** `tests/fixtures/releases/1.6.0.db`.

## 1.5.2 — 2026-10-01

- **WhatsApp number check:** before an invoice is queued the customer's primary mobile is checked with
  WhatsApp, then the alternate; the status bar says which was used. When neither is on WhatsApp the
  number box opens listing what was checked; a typed number is checked too and can be saved as the
  alternate mobile. At delivery the number is checked again and the other number used if needed;
  failures name the numbers — nothing fails silently.

**Migrations:** none. **Fixtures:** `tests/fixtures/releases/1.5.2.db`.

## 1.5.1 — 2026-10-01

- **Manual bill:** every column of a line is a field — code, item, batch, expiry, pack, quantity, rate,
  discount and amount (typing the amount sets the rate, after the line's discount). The code is stored
  on the line (defaults to the product's code when chosen from inventory).

**Migrations:** `a7c9e1f3b5d8` adds `sale_items.item_code` (additive). **Fixtures:** `tests/fixtures/releases/1.5.1.db`.

## 1.5.0 — 2026-10-01

- **Settings → Invoice Store:** premium A4 invoice templates for WhatsApp (with / without stamp; printing
  keeps the classic invoice). 5 lines per page (configurable), no line split across pages, summary and
  stamp on the last page; shop details, optional GSTIN and drug licence (printed only when on), invoice
  texts and stamp upload; live preview. Inter font (OFL) bundled for the PDF.
- WhatsApp invoice files are regenerated when the template or shop details change.

**Migrations:** none (schema `f6b8d0e2a4c7`). **Fixtures:** `tests/fixtures/releases/1.5.0.db`.

## 1.4.1 — 2026-10-01

- **Desktop app:** "Faheem Pharmacy ERP" opens full screen like dedicated software (kiosk: no address
  bar, tabs, menus or top-edge exit button; Super / Alt+Tab / Alt+F4 still work), with its own profile,
  and starts the ERP first if it is not running — no password, the only command allowed.
  It opens at login (full-screen for the counter user, windowed for the administrator; Settings →
  `app-at-login`), and **Start ERP** / **Restart ERP** open it afterwards.
- **WhatsApp:** the gateway's access token is sent the way WPPConnect 2.10 expects (the bare token —
  before, every call was refused and no QR appeared); a refusal now shows as an error.
- **Your own account, no extra user:** the PC logs into the account that ran the installer and opens
  the ERP there. No separate "faheem" account is created any more (`--counter-user NAME` makes it opt-in);
  a re-run moves the automatic login back to you and tells you how to remove the old account.
- **Network moves:** the ERP follows a new network address by itself (NetworkManager hook), so moving
  the PC to the store's Wi-Fi keeps shop-network access working; `network static` warns for wired profiles.
- **WhatsApp → Connect** no longer appears to do nothing while the gateway starts its browser: the
  session is started without waiting, the page shows "Starting…" and keeps checking until the QR code
  appears; gateway errors show in the status bar.
- **After a power cut:** the boot catch-up also takes the missed daily backup (never a reboot) and
  records an interrupted update / restore for Status and the doctor; the app opens at login without delay.
- Installer: builds the image on the PC from `prod` while no release is published (and again on every
  re-run until then); normal system account for `faheem-erp`; image downloads shown before the first
  start; clear message instead of a token prompt when nothing is published.

- **Manual bill:** search and choose any product regardless of stock (MRP as the rate, product kept as a
  reference) or add a typed item not in inventory; quantity → Tab → discount → Tab → rate, editable in
  the cart; stored, returned and reported like any bill (item-wise sales show "· manual bill" rows);
  never checks or changes stock. Every cell of a manual line (name, batch, expiry, quantity, rate,
  discount) is edited with a click; a saved manual bill reopens from Sales (F4) as a manual bill.
- **Settings → WhatsApp** rebuilt in the ERP's own style (sub-tab, form sections, plain buttons,
  standard grid for activity).
- **Control Center → Maintenance → Reset test data** (`faheem-erp reset-test-data`): removes stock,
  products, suppliers, purchases and sales; keeps customers, users, settings and WhatsApp; backup first.
- **Force Update ERP** desktop shortcut (`faheem-erp force-update`): newest release, or a rebuild of the
  newest prod commit, with a backup first — no commands to copy. Installer re-runs keep the choices
  made at install (network access, WhatsApp, automatic login, account).

**Migrations:** none (schema `f6b8d0e2a4c7`). **Fixtures:** `tests/fixtures/releases/1.4.1.db`.

## 1.4.0 — 2026-10-01

**The appliance**
- Installs on a native Ubuntu / Debian PC with one command (`deploy/appliance/install.sh`):
  Docker Compose stack (PostgreSQL 17, migration job, web, worker, optional WhatsApp gateway and HTTPS
  proxy), systemd units, generated secrets in `/etc/faheem-erp/faheem.env` (0600), owner account typed
  at install (only a hash stored), counter user with auto-login and a full-screen ERP behind a
  "Starting services…" page.
- `faheem-erp` command and the **ERP Control Center** (keyboard menu): start / restart / shut down,
  updates and rollback, backups and restore, doctor, maintenance and performance (database, disk,
  caches, counter browser cache and cookies), settings (boot start, counter screen, auto-login,
  auto-update, daily reboot, maintenance time, backup retention), network access, logs, power.
  Desktop shortcuts for each, behind an administrator password (polkit).
- **Doctor** diagnoses from live state and recent logs and proposes the matching fix; `--fix` applies them.
- **Transactional updates** from GHCR (pinned `x.y.z` images): rehearsal on a scratch copy, verified
  snapshot, health + smoke test, automatic rollback with snapshot restore. Manual rollback.
- **Daily maintenance** at 05:00 India time: backup with retention, update, cleanup, weekly VACUUM,
  optional reboot; no catch-up after a PC was off — only an update check after boot.
- **Network access** for office PCs, phones and the store VPN: HTTPS through a local Caddy proxy with
  its own certificate authority, firewall limited to private / VPN ranges, fixed address helper,
  `Secure` cookies, and the authenticator code at every network sign-in.
- Health endpoints `/health/live`, `/health/ready`; `/api/v1/system/version`; separate WhatsApp worker.
- `python -m app.import_sqlite` moves an existing SQLite database into PostgreSQL, verified (row
  counts, history fingerprint, every relationship); two-step secrets sealed with another
  installation's key are reset for re-enrolment.
- CI: tests on SQLite and PostgreSQL, shellcheck, fresh install of the real stack; releases from `prod`.

**Migrations:** none (schema `f6b8d0e2a4c7`).

**Fixtures:** `tests/fixtures/releases/1.4.0.db` (schema `f6b8d0e2a4c7`).

## 1.3.0 — 2026-10-01

**Login and two-step sign-in**
- New login page in the ERP's own design (app bar, dialog panel, status bar): User ID + password,
  no sign-up — accounts come from the administrator.
- First login: scan a QR with an authenticator app (Google / Microsoft Authenticator…), confirm a
  6-digit code, receive 10 one-time recovery codes, then continue. Later logins: user ID + password
  (`mfa_at_login=true` also asks for a code every time).
- Forgot password: user ID + authenticator code (or a recovery code) + new password; every other
  session is logged out. 5 wrong tries lock the account for 15 minutes.
- TOTP per RFC 6238 (verified against its test vectors), secret encrypted at rest, replay guard,
  recovery codes stored as hashes. Ended sessions are rejected server-side.
- `manage.py user add … --keep-password`, `user reset-2fa`, `user unlock`.

**PostgreSQL**
- The application runs on PostgreSQL as well as SQLite (search, triggers, portable SQL); the test
  suite runs on both.

**Migrations**
- `f6b8d0e2a4c7` adds two-factor and lock-out columns to `users` (additive).

**Fixtures:** `tests/fixtures/releases/1.3.0.db` (schema `f6b8d0e2a4c7`).

## 1.2.0 — 2026-10-01

**WhatsApp invoices** (docs/WHATSAPP.md)
- POS, after Complete sale: **Print invoice (P)** or **WhatsApp invoice (W)** (or N). WhatsApp is queued
  and delivered in the background; the next bill starts at once and a sale never depends on WhatsApp.
- Sales History: **WhatsApp / Resend WhatsApp (F9)**, the bill's delivery log and Retry. A resend sends
  the same stored invoice PDF — never a new sale or invoice.
- New **Settings** tab (administrators only) → **WhatsApp Invoicing**: Connected / Disconnected / QR
  required, QR pairing, Reconnect, Log out, invoice message with placeholders and preview, an image
  sent with invoices (replaceable), test send, delivery activity.
- Database queue with retries (30 s, 2 min) and permanent-failure detection; reconnects a paired
  session after a reboot. Provider interface (WPPConnect now; the official Business API can replace it).
- Gateway kit: `deploy/whatsapp/` (Docker Compose bound to 127.0.0.1, setup script, `faheemctl.sh whatsapp`).
- Permissions: `whatsapp.send` (billing roles), `settings.manage` (Administrator only).
- POS customer box is clickable; Purchases: Amount incl. GST column.

**Migrations**
- `e5a7c9d1f3b6` adds the `whatsapp_messages` table (additive).

**Fixtures:** `tests/fixtures/releases/1.2.0.db` (schema `e5a7c9d1f3b6`).

## 1.1.0 — 2026-10-01

**GST on purchases**
- Every purchase line carries its GST: taxable value after item discount, scheme and bill discount,
  GST at the line's rate, CGST + SGST (intra-state) or IGST (inter-state, from the supplier's and the
  pharmacy's GSTIN), value incl. GST and **rate per pack incl. GST** (new grid column; side panel shows
  invoice rate → taxable → GST → rate incl. GST).
- Stock received from now on is costed at the rate **including** GST paid to the supplier
  (`purchase_cost_includes_gst`, default on; switch off if the shop claims input-tax credit).
  Batches record their basis; stock received earlier is never re-costed.
- GST checks with the exact fix in each message: missing GST % (never silently 0), GST % worked out from
  a GST-amount column, withdrawn slabs after the 22-Sep-2025 rate change (12% / 28%), rate changed since
  the product's last purchase, one HSN at two rates, line GST amount vs %, invoice GST total (e.g.
  Marg SumGst) vs the lines, pharmacy / supplier GSTIN check digit. Slabs and the change date are
  settings (`gst_rates`, `gst_rates_before`, `gst_rates_changed_on`).
- GST panel on the purchase document (F10): per-slab table, CGST / SGST / IGST and why, what to fix.
- New report **Purchase GST** (Purchase Reports): by line, invoice, product, GST rate, HSN, supplier or
  month; returns to suppliers reverse their GST.
- Fractional scheme quantities (2.5 + 0.5) received exactly; `manage.py purchases revalidate`.
- Purchase status chips filter lines; right-click tab menu.

**Migrations**
- `d4f6b8c0e2a5` adds the GST snapshot columns to purchase lines, `supply_type` to purchases and
  `rate_basis` to batches (additive).

**Fixtures:** `tests/fixtures/releases/1.1.0.db` (schema `d4f6b8c0e2a5`).

## 1.0.0 — 2026-09-30

First versioned release of the keyboard-first ERP at `/app`.

**Features**
- POS: loose-tablet billing, FEFO batches, discounts, split payment, hold/resume, manual
  bills, bill edit in its original POS tab. Complete sale shows the sale summary, then
  asks "Generate invoice? Y/N".
- Invoice studio inside the POS / Sales tab (A4, A5, Letter, 80 mm, 58 mm, ERP text;
  PDF / Excel / CSV).
- Purchases: supplier invoice import from any layout (CSV, Excel, digital PDF), review,
  partial posting, purchase returns.
- Inventory ledger in base units, stock history, adjustment documents.
- Sales history with returns, exchanges and voids.
- Customers: directory, record, follow-ups (Inbox / Calendar).
- Reports in ERP document format, including Item-wise Sales (Inventory), movement,
  customer and profit reports, with live customer and item search.

**Upgrades and data safety**
- Guarded upgrades: snapshot → migrate → reconcile the business history → automatic
  restore on failure. No silent `create_all` fallback.
- Whole-ERP snapshots (database, uploads, config) in an isolated store with a verified
  mirror on the Windows drive, a daily restore drill and a standalone restore tool.
- Release layout (`releases/<version>`, `current`, `shared/`), rehearsed installs with
  automatic rollback, `faheemctl.sh rollback`, and a deployment log.
- Linux (WSL) only: the Windows build, desktop launcher and Windows printing backend
  were removed.

**Migrations**
- `c3e5a7b9d1f3` adds the `deployment_log` table (additive).
- Earlier revisions up to `b2d4f6a8c0e1` are the pre-1.0 schema history.

**Fixtures:** `tests/fixtures/releases/0.9-pre-release.db` (schema `b2d4f6a8c0e1`) and
`tests/fixtures/releases/1.0.0.db` (schema `c3e5a7b9d1f3`).
