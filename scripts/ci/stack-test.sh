#!/usr/bin/env bash
# Fresh install of the real Docker stack from an image, as the appliance runs it, then the deployment
# smoke test and an owner sign-in. Used by CI (dev) and before every release push (prod).
#   scripts/ci/stack-test.sh <image:tag>
set -Eeuo pipefail
img="${1:?image:tag}"
work="$(mktemp -d)"; trap 'docker compose -p faheem-ci --env-file "$work/env" -f compose.yaml down -v --remove-orphans >/dev/null 2>&1 || true; sudo rm -rf "$work"' EXIT
mkdir -p "$work"/{data/uploads,data/application-data,data/state,data/postgres,logs/app,backups/snapshots}
sudo chown -R 10001:10001 "$work"/data/uploads "$work"/data/application-data "$work"/logs/app "$work"/backups/snapshots
cat > "$work/env" <<ENV
FAHEEM_IMAGE=${img%:*}
FAHEEM_VERSION=${img##*:}
FAHEEM_DATA=$work/data
FAHEEM_LOGS=$work/logs
FAHEEM_BACKUPS=$work/backups
FAHEEM_PORT=18000
FAHEEM_PROXY_DIR=$PWD/deploy/appliance/proxy
POSTGRES_PASSWORD=$(openssl rand -hex 16)
PHARMACY_SECRET_KEY=$(openssl rand -hex 32)
PHARMACY_WPP_SECRET=$(openssl rand -hex 16)
ENV
dc() { docker compose -p faheem-ci --env-file "$work/env" -f compose.yaml "$@"; }
dc config -q
FAHEEM_OWNER_USERNAME=ci.owner FAHEEM_OWNER_FULL_NAME="CI Owner" FAHEEM_OWNER_PASSWORD="CiOwner2026" \
  dc run --rm -e FAHEEM_OWNER_USERNAME -e FAHEEM_OWNER_FULL_NAME -e FAHEEM_OWNER_PASSWORD migrate
dc up -d postgres migrate web worker
for _ in $(seq 60); do curl -fsS http://127.0.0.1:18000/health/ready && break; sleep 3; done
curl -fsS http://127.0.0.1:18000/health/ready
dc run --rm --no-TTY tools python -m app.production smoke
dc run --rm --no-TTY tools python -m app.snapshot create --reason manual --note ci
dc run --rm --no-TTY tools python -m app.snapshot drill
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -d 'username=ci.owner&password=CiOwner2026' http://127.0.0.1:18000/login)"
[ "$code" = 303 ] || { echo "owner sign-in answered $code"; exit 1; }
# a restart runs the migration job again (no-op) and comes back ready
dc restart web worker; for _ in $(seq 40); do curl -fsS http://127.0.0.1:18000/health/ready >/dev/null && break; sleep 3; done
curl -fsS http://127.0.0.1:18000/health/ready >/dev/null
echo "stack test OK"
