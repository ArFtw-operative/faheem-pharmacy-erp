# Project memory

**Read this first.** It lets a new developer or AI coding agent continue without rediscovering the project.
Last review: **2026-10-06**. Update it whenever a decision is made or the state changes.

Map: [README](README.md) · [PRD](PRD.md) · [ARCHITECTURE](ARCHITECTURE.md) · [RULES](RULES.md) · [DESIGN](DESIGN.md) · [TASKS](TASKS.md)

---

## 1. Project identity

| | |
|---|---|
| Product | **Faheem Pharmacy ERP**: keyboard-first pharmacy billing, stock, purchases, customers, reports |
| Customer | One retail pharmacy (Faheem Pharmacy, Hyderabad), on its own Linux PC (the *appliance*) |
| Repository | GitHub `ArFtw-operative/faheem-pharmacy-erp`; branches **`dev`** (work) and **`prod`** (what the PC installs) |
| Architecture | FastAPI + SQLAlchemy + Alembic backend; plain-JS ES-module single-page workspace; PostgreSQL 17 in Docker Compose on the appliance; SQLite for development and tests |
| Released | **1.9.2** (`ff68a2d`) on `prod` and the pharmacy PC |
| In progress | **1.10.0**: implemented, tested and pushed to `dev` (4cd8992); not yet released to `prod`; see [TASKS.md TASK-001](TASKS.md#task-001--release-1100) |
| Schema head | `a8c0e2f4b6d8` (dev) · `f6b8d0a2c4e7` (1.9.2) |

---

## 2. Important technical decisions

**DECISION-001 — Keep the stack; no frontend framework.**
Decision: Python/FastAPI backend, plain ES modules, no build step. Reason: it works, it is small, and the owner wants nothing from the old UI. Impact: screens are hand-written modules; helpers in `core.js`, `grid.js`. Files: `app/static/erp/`, `DECISIONS.md` D2.

**DECISION-002 — One keyboard-first workspace at `/app`.**
Decision: the old server-rendered UI and the counter/cash-drawer "day open" mechanism were deleted (2026-09-30); billing must never be blocked by a counter day. Reason: speed and simplicity at the counter. Impact: the 1.10.0 **Counter Report is a report, not a gate**: it never blocks billing. Files: `app/main.py`, `shell.js`.

**DECISION-003 — Native Linux appliance with Docker Compose + PostgreSQL + systemd.**
Decision: no Windows build, no WSL deployment. Reason: reliability and self-maintenance at the shop. Impact: install via `deploy/appliance/install.sh`; updates via `faheem-erp update` / *Force Update ERP*. Files: `compose.yaml`, `deploy/appliance/`.

**DECISION-004 — The API never migrates PostgreSQL.**
Decision: a separate `migrate` job (`python -m app.production migrate`) runs before the web app, which refuses a non-current schema. Reason: no half-migrated production database. Files: `app/production.py`, `app/database.py` `init_db`.

**DECISION-005 — Guarded upgrades with a history fingerprint.**
Decision: every upgrade compares counts and money/quantity totals of business history before and after; any difference restores the snapshot. Intentional changes must be declared: `RECONCILE_EXEMPT` (figure may change) or **`RECONCILE_MOVED`** (added in 1.10.0: a figure may drop only by exactly what arrived in another table). Files: `app/services/upgrade_service.py` (`compare`, `_exemptions`), `app/snapshot.py` (`HISTORY_TABLES`, `HISTORY_SUMS`), [UPGRADES.md](UPGRADES.md).

**DECISION-006 — Stock is a ledger.**
Decision: batch stock = sum of `inventory_movements`; corrections are reversals. Files: `app/services/stock_ledger.py`.

**DECISION-007 — Purchases: accuracy before automation.**
Decision: everything is read and counted automatically, but a person posts; fuzzy matches only suggest; automatic product creation and scan import are off by default; partial *automatic* posting is not enabled. Files: `purchasing.py`, `purchase_automation.py`, `DECISIONS.md` D4, D11–D24.

**DECISION-008 — Locations are dated events and never touch stock.** Files: `location_service.py`, `DECISIONS.md` D25–D30.

**DECISION-009 — Manual bills are separate documents (1.10.0).**
Decision: own tables `manual_bills` / `manual_bill_items`; the migration moved existing ones out of `sales` after a reconciling copy. The model reuses sale attribute names (`invoice_no`, `sale_date`, `items`, `payments`…) so the same invoice renderers print it (it exposes `invoice_type = "MANUAL"`). Files: `app/models.py` `ManualBill`, `manual_bill_service.py`, migration `a8c0e2f4b6d8`.

**DECISION-010 — Udhaar is a payment mode plus a ledger (1.10.0).**
Decision: payment row mode `UDHAAR`; one `udhaar_entries` row per bill; repayments in `udhaar_payments`; reminders in `udhaar_reminders` delivered through the existing WhatsApp queue (`whatsapp_messages.kind = UDHAAR_REMINDER / UDHAAR_STATEMENT`). Files: `udhaar_service.py`, `whatsapp/service.py`.

**DECISION-011 — Counter days are frozen (1.10.0).**
Decision: a finished business day is stored in `counter_day_closes` with a SHA-256 digest, on first view after midnight or by the background job; later edits appear as differences. Files: `counter_service.py`.

**DECISION-012 — Invoices are rendered in the tab (Invoice Studio), never as pop-ups.** Files: `studio.js`.

---

## 3. Important business decisions (do not change without the owner)

- **"Udhaar" is the only name for store credit** (code `UDHAAR`, tables `udhaar_*`, screen "Udhaar Ledger"). Never call it "credit": staff confuse it with card payments.
- Udhaar needs a customer with mobile, a due date and a reminder date; cash is never refunded for unpaid Udhaar goods, **even with a manager override**.
- Manual bills never count anywhere: sales, reports, cash, Udhaar, stock.
- Discounts are percentages, capped at `max_discount_pct` (default 20%) per item, per bill and in total.
- Profit = sale value − purchase cost, counted only where cost is known. The owner **rejected** "missing purchase rate" buttons and Costed Sales / Cost Status / Missing Cost Lines columns; do not re-add them.
- The owner dislikes explanatory footnotes under reports ("feels AI-generated"): keep reports free of notes.
- WhatsApp is for customer-requested invoices and Udhaar reminders only, never bulk or promotional.
- Purchase stock cost includes GST by default.
- Customers is **not** a CRM: no dashboards or pipelines; one follow-up record shared by POS, Inbox and Calendar.
- Bill editing is wanted (F4 in Sales reopens the bill in its original POS tab).
- POS finish flow: F12 summary → Enter → completion banner → (follow-up) → Print / WhatsApp / No invoice. No automatic printing.
- Default remote-support device name `HYD-FAHEEM-PHARMACY`.

---

## 4. Critical files

| File / folder | Why it matters |
|---|---|
| `app/services/stock_ledger.py` | The only code allowed to change stock; FEFO allocation, expiry, reconcile |
| `app/services/sales_service.py` | Sale creation, edit, void, discount and payment rules |
| `app/services/udhaar_service.py` | Udhaar ledger rules (1.10.0) |
| `app/services/purchasing.py` | Purchase review, posting, rollback |
| `app/services/upgrade_service.py`, `app/snapshot.py` | Upgrade guard, fingerprints, snapshots, restore |
| `app/production.py`, `app/database.py` | Migration job and start-up gates |
| `app/permissions.py`, `app/deps.py` | Permission catalogue, roles, the permission guard |
| `app/models.py` | All 49 tables |
| `alembic/versions/` | Schema history (additive; `downgrade()` raises) |
| `tests/fixtures/releases/*.db` | Frozen database of every release; never regenerate |
| `app/static/erp/pos.js` | The counter; most user-visible behaviour |
| `app/static/erp/shell.js` | Tabs, keys, live refresh, module registry |
| `compose.yaml`, `deploy/appliance/` | How the pharmacy PC runs |
| `CHANGELOG.md`, `DECISIONS.md`, `DEPENDENCIES.md` | Release notes, decision log, licences |

---

## 5. Data model summary

**Products** (`items`) are stocked in **batches** (number, expiry, MRP, cost). Every stock change is an
**inventory movement**. **Sales** have **lines** (one batch each) and **payment rows**. An Udhaar part creates an
**Udhaar entry** with **payments** and **reminders**. **Returns** are separate documents. **Purchases** have
**lines** that, when posted, create or top up batches. **Suppliers** accumulate learned mappings. **Customers** have
follow-ups. **Racks/boxes** and dated **item locations** say where things are. **Users** have **roles** holding
**permissions**; every change writes an **audit log** row. Diagrams: [ARCHITECTURE.md §8](ARCHITECTURE.md#8-database-architecture).

## 6. Important workflows

| Workflow | Path through the code |
|---|---|
| Sale | `pos.js` → `POST /api/sales` → `sales_service.create_sale` → `stock_ledger.allocate/post` → `udhaar_service.sync_sale` → `audit.record` |
| Purchase | `purchases.js` → `POST /api/erp/purchases/import` → `purchase_automation.import_file` → review screen → `POST …/post` → `purchasing.post` → ledger |
| Return | `sales.js` → `POST /api/sales/{id}/refund` → `refund_service.create_return` (+ `udhaar_service.apply_return`) |
| Udhaar payment | `udhaar.js` → `POST /api/erp/udhaar/customers/{id}/receive` → `udhaar_service.receive_payment` |
| Counter Report | `counter.js` → `GET /api/erp/counter` → `counter_service.report` (freezes past days) |
| WhatsApp | queue row in `whatsapp_messages` → `app/worker.py` → `whatsapp/service.process_due` → gateway |
| Upgrade | `faheem-erp update` (appliance) or app start (dev SQLite) → `upgrade_service.upgrade` |

## 7. Important dependencies

| Library | Why |
|---|---|
| FastAPI / Starlette / Uvicorn | Web framework and server |
| SQLAlchemy 2, Alembic, psycopg 3 | Database access, migrations, PostgreSQL driver |
| bcrypt, itsdangerous, cryptography | Password hashes, signed cookies, encrypted 2FA secrets |
| qrcode | 2FA QR and payment QR |
| openpyxl, xlrd | Excel import/export |
| pdfplumber, pypdfium2, pdfminer.six, **PyMuPDF (AGPL, TASK-003)** | PDF invoice reading and rendering |
| RapidFuzz | Name similarity for product suggestions |
| phonenumbers | WhatsApp number normalisation |
| httpx | WhatsApp gateway calls |
| pytest, hypothesis | Tests |
| Playwright (Node, dev only) | Browser test scripts |

## 8. Known problems

See [TASKS.md](TASKS.md). Most important: 1.10.0 not yet released (the POS item-pick bug is live in 1.9.2); CI
workflows not on `dev`/`prod`; PyMuPDF licence; remote support not activated; idle-lock settings without effect.

---

## 9. Things future agents must know

### Working on a development machine
- **Dev server:** `./launch.sh start` (port 8781, data in `./data`). `run.py --port 8000` is the older way. The dev database is migrated on start (snapshot first).
- **Tests:** `.venv/bin/python -m pytest -q tests` (SQLite, about 6 minutes). PostgreSQL: start a scratch PostgreSQL 18 cluster on TCP `127.0.0.1:55432` (user `postgres`, trust auth, database `faheem_test`) and set `PHARMACY_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/faheem_test`. Use TCP: Unix socket paths inside deep folders are too long.
- **Browser checks:** Playwright scripts in `scripts/test-*-browser.mjs` against a running server. Run them on a **copy** of the database with a scratch server (`PHARMACY_DATA_DIR`, `PHARMACY_SNAPSHOT_DIR`, `PHARMACY_ENV_FILE=/dev/null` pointed at a temp folder), never on the owner's live data. Do not close the owner's workspace tabs.
- **Stopping a server:** kill by port (`ss -ltnp | grep :<port>`), never `pkill -f <pattern>`: the pattern also matches your own shell command and kills it.
- **Dev login:** ask the owner. Never write credentials into the repository (it may be public).
- WhatsApp on a dev machine needs Docker and `deploy/whatsapp/setup.sh` (TASK-010).
- Untracked folders in the repository root (old design kits, zips, `faheem-*.html`) are not part of the app; they are excluded via `.git/info/exclude`.

### Release routine (the owner asked never to be asked for it again)
1. Bump `APP_VERSION` in `app/config.py`; date the CHANGELOG section.
2. `PHARMACY_ADMIN_PASSWORD=<pw> python scripts/make_release_fixture.py X.Y.Z tests/fixtures/releases/X.Y.Z.db`, then `git add -f` it (`*.db` is ignored). Never regenerate an old fixture.
3. Full suite on SQLite **and** PostgreSQL 18.
4. Rehearsal on a copy of the pharmacy's data: worktree of `origin/prod` → `python -m app.import_sqlite <snapshot>` into a fresh PostgreSQL database → fingerprint → new code `scripts/manage.py upgrade --check`, `python -m app.production migrate|schema|smoke` → business fingerprint identical (or declared and reconciled) → run the app and exercise POS / purchases / inventory. Write `docs/REHEARSAL-X.Y.Z.md`.
5. Commit with author `ArFtw-operative <arfwtw@users.noreply.github.com>`. Push `dev`, then `dev:prod` **only with the owner's explicit consent each time**. Never print tokens. On the original WSL machine pushes used a one-off credential helper reading the Linux `gh` token; on a new machine use its own `gh auth login`.
6. The pharmacy PC updates with *Force Update ERP* (desktop) or `sudo faheem-erp force-update`.

### On-site checklist
[ONSITE-PENDING.md](ONSITE-PENDING.md): support VPS and DNS, enrolment, `faheem-erp import-medicine-reference`,
`faheem-support install`, 12-step acceptance test.

### Adding things
- New screen: module file + `shell.js` `MODULES` + `erp.html` import map + `routers/erp.py` `MODULE_PERMS`.
- New shortcut: `keymap_service.ACTIONS` + `keys.js` short label; never take a key the owner already assigned personally (e.g. Alt+Q).
- New permission: `app/permissions.py` (catalogue + roles) **and** a migration that inserts it and grants it (pattern in `a8c0e2f4b6d8`).
- New table/column: model + Alembic migration (SQLite and PostgreSQL) + add history tables to `snapshot.HISTORY_TABLES/SUMS` if they hold business history + `data_reset.WIPE_TABLES` + `sync_service.TABLE_AREAS`.

---

## 10. Do not do

- Do not change stock except through `stock_ledger.py`.
- Do not edit or delete ledger movements, posted documents, audit rows or frozen counter days.
- Do not change the schema without an Alembic migration, and do not weaken the upgrade fingerprint check to make a migration pass: declare and reconcile instead.
- Do not regenerate old release fixtures.
- Do not add endpoints without `require_permission`.
- Do not push to `prod` or release without the owner's consent.
- Do not commit secrets, `.env` files, databases, uploads or real customer/supplier documents.
- Do not reintroduce counter "day open" gates, browser pop-ups, fixed supplier column lists, or report footnotes.
- Do not call Udhaar "credit".
- Do not add GPL/AGPL dependencies.
- Do not use `pkill -f` to stop servers.

---

## 11. Current state

| | |
|---|---|
| Last documentation review | 2026-10-06 |
| Architecture status | Stable: appliance in production since 1.4.0; guarded upgrades; 49 tables; 217 endpoints |
| Active development | 1.10.0 (Udhaar ledger, Counter Report, manual bills separation, stock totals, POS pick fix): implemented, tested and on `dev`; waiting for release |
| Next steps | TASK-001 release 1.10.0 → TASK-004 CI → TASK-002 on-site remote support |
| Moving to a new machine | Clone the repository and check out `dev`, create `.venv` from `requirements.txt`, install Node Playwright for browser checks, log in to GitHub with `gh`, and re-create any scratch PostgreSQL cluster. Paths in older notes (`/home/abdurftw/…`, WSL, `/mnt/c/…`) belong to the original development PC. |
