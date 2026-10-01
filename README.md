# Faheem Pharmacy ERP

```bash
curl -fsSL https://raw.githubusercontent.com/ArFtw-operative/faheem-pharmacy-erp/prod/deploy/appliance/install.sh | sudo bash
```

Run that on the pharmacy PC (Ubuntu 22.04 / 24.04 / 26.04 or Debian 12 / 13, installed natively —
not WSL). It asks for the owner account and a few choices, and leaves a
self-maintaining appliance: Docker Compose + PostgreSQL + systemd, the ERP full-screen on the counter
screen at boot, a 05:00 backup / update / reboot, and the **ERP Control Center** on the desktop.
Every option: **[docs/INSTALL.md](docs/INSTALL.md)**.

A keyboard-first pharmacy ERP: POS with loose-tablet billing, a ledger-based inventory,
supplier-invoice import and review with a GST engine, sales history with returns and exchanges,
stock adjustments, WhatsApp invoices and reports in ERP document format — all in the browser at `/app`.

| | |
|---|---|
| Install, tokens, other PCs and the store VPN | [docs/INSTALL.md](docs/INSTALL.md) |
| Control Center, desktop shortcuts, settings, doctor | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| Releases, updates and rollback | [docs/UPDATE.md](docs/UPDATE.md) |
| Backups and restore | [docs/BACKUP-RESTORE.md](docs/BACKUP-RESTORE.md) |
| When something goes wrong | [docs/RECOVERY.md](docs/RECOVERY.md) |
| How it is built | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [migration policy](docs/UPGRADES.md) |
| WhatsApp invoices | [docs/WHATSAPP.md](docs/WHATSAPP.md) |
| Modules | [Purchases](docs/PURCHASES.md) · [POS](docs/POS.md) · [Inventory](docs/INVENTORY.md) · [Reporting](docs/REPORTING.md) · [Cost model](docs/financial-cost-model.md) · [Keyboard shortcuts](docs/keyboard-shortcuts.md) |
| Development | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) · [Security](docs/SECURITY.md) · [Changelog](CHANGELOG.md) |

## Branches and releases

- `dev` — day-to-day work. Every push runs CI: the test suite on SQLite and on PostgreSQL,
  shellcheck, and a fresh install of the real Docker stack with a smoke test.
- `prod` — releases. A push publishes the version in `app/config.py` once, after the same tests:
  `ghcr.io/arftw-operative/faheem-pharmacy-erp:<x.y.z>` and `:sha-<commit>` (never `latest`), a `v<x.y.z>`
  tag and a GitHub release. Installed PCs pick it up at the next 05:00 maintenance.

## Development

```bash
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
PHARMACY_ADMIN_PASSWORD='choose-one-1' .venv/bin/python run.py --port 8000 --no-browser   # SQLite, ./data
.venv/bin/python -m pytest -q
# the appliance stack from this checkout:
cp deploy/appliance/dev.env .appliance.env
docker compose --env-file .appliance.env -f compose.yaml -f compose.dev.yaml up --build
```

## Principles

- Stock changes only through the inventory ledger, in base units (tablets, bottles, pieces).
- Documents are immutable. Corrections are new, linked documents (returns, reversals, voids).
- Nothing is guessed. Supplier data is either proven or sent to review, and unknown cost shows as "—".
- Migrations are additive; an update never recalculates history and rolls back by itself if anything fails.
- No credentials in Git. Secrets are generated on the PC into `/etc/faheem-erp/faheem.env` (0600).
