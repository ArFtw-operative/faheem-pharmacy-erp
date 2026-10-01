#!/usr/bin/env bash
# Whole-ERP snapshots from the shell (database, uploads, config), stored outside the app.
#   scripts/snapshot.sh create [--note "..."] | list | verify <id> | restore <id> | drill [<id>] | where
# Stop the app before `restore` (faheemctl.sh restore <id> does stop → restore → start for you).
set -euo pipefail
APP="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ENV_FILE="${PHARMACY_ENV_FILE:-$APP/faheem.env}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
PY="$APP/.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$APP" && exec "$PY" -m app.snapshot "$@"
