# Faheem Pharmacy ERP — instructions for Claude Code

Before any work, read **[docs/MEMORY.md](docs/MEMORY.md)**: project state, decisions, the release routine, and what
not to do. The full documentation home is [docs/README.md](docs/README.md); the rules to keep are in
[docs/RULES.md](docs/RULES.md); open work is in [docs/TASKS.md](docs/TASKS.md).

Essentials:
- Store credit is **Udhaar** everywhere (code `UDHAAR`); never "credit".
- Stock changes only through `app/services/stock_ledger.py`; schema changes only through Alembic migrations that pass the upgrade fingerprint guard.
- Run `.venv/bin/python -m pytest -q tests` before claiming work is done; PostgreSQL too for releases.
- Push to `prod` and release only with the owner's explicit consent.
- Never commit credentials; this repository may be public.
- Keep `docs/` up to date when architecture, rules, the database, major features or deployment change.
