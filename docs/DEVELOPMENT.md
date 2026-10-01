# Development

## Prerequisites

- Python 3.11+ (developed on 3.14)
- pip / venv

## Installation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Environment configuration

Configuration is read from environment variables with sensible defaults (see
`app/config.py`). Copy the example and adjust as needed:

A fresh database needs the first owner's password: `PHARMACY_ADMIN_PASSWORD=…` (never commit it).
`PHARMACY_DATABASE_URL=postgresql+psycopg://…` runs on PostgreSQL; there the schema is created by
`python -m app.production migrate` and the app is started with `PHARMACY_EXTERNAL_MIGRATIONS=1`.

Key variables: `PHARMACY_DB`, `PHARMACY_DATA_DIR`, `PHARMACY_UPLOAD_DIR`,
`PHARMACY_LOG_DIR`, `PHARMACY_HOST`, `PHARMACY_PORT`, `PHARMACY_SECRET_KEY`,
`PHARMACY_TIMEZONE`.

## Database

The database is initialised automatically on startup: Alembic migrations run to head
(or `create_all` is used when migrations are unavailable), FTS5 structures are created,
and baseline data (settings, permissions, roles, an admin user, a default counter and
workstation) is seeded.

```bash
.venv/bin/python -m alembic upgrade head     # apply migrations explicitly
.venv/bin/python -m alembic revision --autogenerate -m "message"
```

First-run login: **admin / admin123** (you are prompted to change it).

## Demo data

```bash
PHARMACY_DEMO_SEED=1 \
PHARMACY_DB=$(pwd)/demo/db/pharmacy-demo.db \
PHARMACY_DATA_DIR=$(pwd)/demo/db/data \
PHARMACY_UPLOAD_DIR=$(pwd)/demo/db/uploads \
PHARMACY_LOG_DIR=$(pwd)/demo/db/logs \
.venv/bin/python scripts/seed_demo.py
```

## Development command

```bash
.venv/bin/python run.py            # picks a free port and opens the browser
# or explicitly:
PHARMACY_PORT=8010 .venv/bin/python run.py --no-browser
```

## Test command

```bash
.venv/bin/python -m pytest -q
```

Tests run against an isolated temporary database. `tests/test_end_to_end.py` walks
the whole business flow through the HTTP APIs; `tests/test_invoice_corpus.py` imports
supplier files from many billing-software layouts.

`PHARMACY_TEST_DATABASE_URL=postgresql+psycopg://…` runs the whole suite on PostgreSQL;
`PHARMACY_TEST_PG_URL=…` (an empty database) adds the PostgreSQL snapshot / upgrade / import tests.
`tests/test_appliance.py` exercises the host scripts against a modelled Docker.

## Build

There is no front-end build step; assets are plain CSS/JS served from `app/static`. The release image
is built by `Dockerfile`; `compose.dev.yaml` runs the appliance stack from a checkout (see README).
