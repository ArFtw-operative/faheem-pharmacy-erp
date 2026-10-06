# Tasks — current priorities, issues and technical debt

**Reviewed:** 2026-10-06. Nothing in this list was changed while writing the documentation; each item is recorded to be
picked up deliberately. A code search found **no TODO / FIXME / HACK markers** in the maintained code, so items come
from the code itself, the existing docs (`docs/ONSITE-PENDING.md`, `DECISIONS.md`, `DEPENDENCIES.md`) and the state
of the repository.

Labels: **Confirmed** = verified in the code or repository. **Needs investigation** = plausible, not proven.

---

## Current priorities

### P0 — Critical

### TASK-001 — Release 1.10.0
Status: Open · Priority: P0 · Area: Release
Relevant files: `CHANGELOG.md` (section *Unreleased (next: 1.10.0)*), `app/config.py` (`APP_VERSION`), `alembic/versions/a8c0e2f4b6d8_udhaar_manual_bills_counter.py`, `scripts/make_release_fixture.py`

Problem: the Udhaar ledger, Counter Report, manual bills separation, stock totals and the **POS item-pick fix**
are finished and tested (872 tests on SQLite and PostgreSQL 18, browser scripts) but exist only as **uncommitted
changes on the development machine**. The pharmacy still runs 1.9.2, which has the POS bug where the 4th item picked
with the arrow keys is not added or replaces another line.

Expected result: 1.10.0 committed, released to `prod`, installed on the pharmacy PC.

Acceptance criteria:
- `APP_VERSION = "1.10.0"`, CHANGELOG dated, release fixture `tests/fixtures/releases/1.10.0.db` added (never regenerated).
- Full suite green on SQLite and PostgreSQL; `tests/test_upgrades.py` green.
- Rehearsal on a copy of the pharmacy's PostgreSQL data: migration `a8c0e2f4b6d8` moves any manual bills and reconciles (`docs/REHEARSAL-1.10.0.md`).
- `dev` and `prod` pushed (prod only with the owner's consent); PC updated with *Force Update ERP*.

Dependencies: none.

### P1 — High

### TASK-002 — Activate remote support at the pharmacy
Status: Open · Priority: P1 · Area: Operations · Relevant files: `docs/ONSITE-PENDING.md`, `deploy/support-server/`, `deploy/support/`
Problem: remote support is shipped but inactive; the support server (VPS, DNS) and PC enrolment are not done.
Expected result: technician can help remotely with the pharmacy's consent.
Acceptance criteria: steps A–C of `docs/ONSITE-PENDING.md` completed, including the 12-step acceptance test.
Dependencies: a VPS and the `remote.faheemerp.in` DNS record.

### TASK-003 — Decide the PyMuPDF (AGPL) licence
Status: Open · Priority: P1 · Area: Legal / dependencies · Relevant files: `DEPENDENCIES.md`, `DECISIONS.md` (D5), `app/services/purchase_import.py`, `pdf_invoice.py`, `invoice_render.py`, `invoice_premium.py`
Problem: `pymupdf` is AGPL-3.0, against the project's permissive-only policy.
Expected result: either a commercial licence is confirmed, or the readers/renderers are ported to `pdfplumber` / `pypdfium2`.
Acceptance criteria: decision recorded; if ported, the invoice corpus tests (`tests/test_invoice_corpus.py`, `tests/test_purchase_corpus.py`) still pass.

### TASK-004 — Bring CI workflows into the main branches
Status: Open · Priority: P1 · Area: CI/CD · Relevant files: branch `dev-before-purchase-update-3e893ce` (`.github/workflows/ci.yml`, `release.yml`), `scripts/ci/stack-test.sh`
Problem: the GitHub workflow files exist only on a local branch. The push token lacked the `workflow` scope, so `dev` and `prod` have no automated CI.
Expected result: tests and the stack test run automatically on every push.
Acceptance criteria: workflows on `dev` and `prod`; a push shows a green run.
Dependencies: a GitHub token with `workflow` scope.

### TASK-005 — Idle-screen and lock settings have no confirmed effect
Status: **Needs investigation** · Priority: P1 · Area: Security / UX · Relevant files: `app/seed.py` (`idle_timeout_minutes`, `lock_timeout_minutes`), `app/services/settings_service.py`
Problem: the settings exist and are listed as store settings, but no screen code reads them (a search of `app/static/erp/` finds no use). An unattended counter stays signed in for the 12-hour session.
Expected result: either implement the idle screen / lock, or remove the settings.
Acceptance criteria: behaviour matches the settings, with a test.

### P2 — Medium

### TASK-006 — Decide the remaining old-UI routes
Status: Open · Priority: P2 · Area: Technical debt · Relevant files: `app/routers/sales.py`, `app/routers/reports.py`
Problem: routes from the removed server-rendered UI remain: `GET /sales` (redirect), `POST /sales/{sale_id}/print`, `GET /sales/returns/{return_id}/pdf`, `GET /sales/export/{fmt}`, `GET /reports` (page alias). Earlier removal was deferred to the owner.
Acceptance criteria: each kept route is documented as used, or removed with its tests.

### TASK-007 — Remove stale constants from `app/models.py`
Status: Confirmed · Priority: P2 · Area: Technical debt · Relevant file: `app/models.py` (top of file)
Problem: module constants are unused and partly wrong: `PAYMENT_MODES` lacks `UDHAAR`, `FOLLOWUP_STATUS` is defined twice with different values, `REFUND_METHODS`, `DRAFT_STATUS`, `EXPIRY_STATUS` are not used by the application.
Acceptance criteria: removed or made the single source used by services; tests green.

### TASK-008 — Clean up after manual bills left sales (1.10.0)
Status: Confirmed · Priority: P2 · Area: Technical debt · Relevant files: `app/models.py` (`Sale.invoice_type`), `app/services/report_extra.py` (`MANUAL_BILL` cost source branch)
Problem: `sales.invoice_type` is now always `INVENTORY`, and no sale line can carry the `MANUAL_BILL` cost source any more, so that branch in `report_extra.py` is dead. (The `invoice_type == "MANUAL"` checks in `report_document.py` and `invoice_kit.py` are **not** dead: they now serve `ManualBill` documents, which expose `invoice_type = "MANUAL"`.)
Acceptance criteria: dead branch removed (the column itself stays, per the additive-migration policy).

### TASK-009 — PDF invoice shows raw payment codes
Status: Confirmed · Priority: P2 · Area: UX · Relevant file: `app/services/invoice_render.py` (prints `sale.payment_mode`)
Problem: the A4 PDF renderer prints `UDHAAR`, `SPLIT` as codes, while the studio and text invoice print friendly labels and the Udhaar due date.
Acceptance criteria: the PDF shows "Udhaar", split parts and the due date like `invoice_kit.py`.

### TASK-010 — WhatsApp unavailable on development machines without Docker
Status: Confirmed · Priority: P2 · Area: Developer experience · Relevant files: `deploy/whatsapp/setup.sh` (local, not tracked), `compose.yaml` (`whatsapp` profile), `app/services/whatsapp/provider.py`
Problem: the Settings → Connect WhatsApp button is disabled when `PHARMACY_WPP_SECRET` is not set; the gateway needs Docker.
Acceptance criteria: documented dev setup, or a fake provider switch for development.

### TASK-011 — Udhaar return larger than what is owed needs two returns
Status: Confirmed limitation · Priority: P2 · Area: Udhaar *(1.10.0)* · Relevant file: `app/services/udhaar_service.py` (`apply_return`)
Problem: when a customer has partly repaid, a return worth more than the remaining balance is refused by Udhaar and refused by cash; the cashier must split it into two returns.
Expected result: one return refunds by Udhaar up to the balance and the rest by the original paid method.

### P3 — Low

### TASK-012 — Stale comments about Windows
Status: Confirmed · Priority: P3 · Relevant file: `app/config.py` (docstring and `APP_BUILD` comment)
Problem: comments say production runs on Windows; the product is a Linux appliance (Windows build removed).

### TASK-013 — "Total Quantity" adds different units
Status: Confirmed (by design) · Priority: P3 · Area: Reports *(1.10.0)* · Relevant files: `app/routers/erp.py` `_stock_totals`, `report_generator.py`
Problem: the total quantity adds tablets, bottles and packs as one number. Values (rate, MRP) are correct; the quantity total is a count of stock units only.
Expected result: label it "Total stock units", or show per-unit totals.

### TASK-014 — Inventory Excel export has no totals row
Status: Confirmed (deliberate) · Priority: P3 · Relevant file: `app/services/inventory_service.py` (`export_xlsx`)
Problem: the export doubles as the re-import template, so totals were not added there; totals are in the Current Stock / Batch-wise Stock report exports.

---

## Bugs

| ID | Bug | Status |
|---|---|---|
| TASK-001 | POS: item picked with arrow keys not added / replaces another line (fixed in 1.10.0, not yet released) | Fixed in dev |
| TASK-009 | PDF invoice shows raw payment codes | Open |
| TASK-011 | Udhaar return above the remaining balance cannot be done in one step | Open |

## Technical debt

TASK-006 (old routes), TASK-007 (stale constants), TASK-008 (manual-bill branches), TASK-012 (comments).
Also: `app/services/reports_service.py` (older period helper) is still used by `routers/sales.py` `_history_filters`
alongside `report_generator.py`. **Needs investigation** whether both are needed.

## Security improvements

| ID | Item | Status |
|---|---|---|
| SEC-1 | No anti-CSRF token; relies on SameSite=Lax cookies and JSON bodies. Review form-encoded POST endpoints (`/login`, `/logout`, file uploads). | Needs investigation |
| SEC-2 | Development default `PHARMACY_SECRET_KEY` in `app/config.py` is only rejected by the production migration job; a dev server started on a network address would use it. Refuse it whenever `PHARMACY_HOST` is not loopback. | Confirmed |
| SEC-3 | No general request rate limiting (only the per-account login lock). | Confirmed |
| SEC-4 | Idle lock not active (TASK-005). | Needs investigation |
| SEC-5 | `SECURITY.md` states the repository is private; verify the actual GitHub visibility and update the statement. | Needs investigation |

## Performance improvements

| Item | Status |
|---|---|
| No written performance targets (e.g. POS search response time) or automated timing checks except `scripts/perf_locations.py` and `scripts/benchmark_inventory.py` (manual) | Confirmed |
| Inventory totals (first page) load every in-stock batch of the filtered products in Python; fine at today's size (~1,000 products), re-check at 10,000+ | Recommendation |

## UX improvements

| Item | Status |
|---|---|
| Customers screen does not show a customer's Udhaar balance (it is in the Udhaar Ledger) | Recommendation |
| Users and roles can only be managed from the command line (`scripts/manage.py`), not a screen | Confirmed |
| Store settings (discount limit, rounding, Udhaar days…) mostly via command line; Settings screen covers WhatsApp, invoices, system info, Udhaar (1.10.0) | Confirmed |

## Testing gaps

| Gap | Status |
|---|---|
| Browser tests (`scripts/test-*-browser.mjs`) are run by hand against a running server; not part of the pytest suite or CI | Confirmed |
| No JavaScript unit tests for screen logic (POS totals, discount mirror) | Confirmed |
| Appliance scripts are tested with a fake Docker (`tests/test_appliance.py`); the real stack test needs Docker (`scripts/ci/stack-test.sh`) | Confirmed |
| WhatsApp is tested against a fake gateway only | Confirmed |

## Documentation gaps

| Gap | Status |
|---|---|
| `docs/HANDOFF.md` header still says release 1.4.0 and an old schema; this suite supersedes it for current state | Confirmed |
| Root `README.md` did not point to this documentation home (link added with this suite) | Done |
| User / role administration has no user-facing guide beyond `scripts/manage.py` help | Confirmed |

## Future enhancements (not requested; ideas only)

- GST on sales invoices, if the pharmacy's billing requirements change.
- Screen for users, roles and store settings.
- Per-unit stock totals; Udhaar balance on the customer record.
