#!/usr/bin/env bash
# One-command local development bootstrap.
# Brings up MySQL, Redis and a single-node Garage (docker-compose.dev.yml),
# initializes the Garage layout/bucket/key, migrates and seeds the database,
# then starts the Django dev server on http://localhost:8000.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.dev.yml"

# Dev credentials - must match docker-compose.dev.yml (web service env).
GARAGE_KEY_ID="GK31c2f218a2e44f485b94239e"
GARAGE_KEY_SECRET="7f2e4d8c1ba9463f5e0d2c8a7b6e9f1d3c5a8e0b4d7f2a6c9e1b3d5f7a0c2e4d"
GARAGE_BUCKET="yggdrasil"

if [ ! -f .env ]; then
    cp .env.example .env
    if command -v python3 >/dev/null; then
        secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
        sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" .env
    fi
    echo "Created .env from .env.example"
fi

export GID="${GID:-$(id -g)}"
export UID 2>/dev/null || true

$COMPOSE up -d db redis garage

echo "Waiting for Garage..."
for _ in $(seq 1 60); do
    if $COMPOSE exec -T garage /garage status >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# One-time Garage init; every step is idempotent or tolerated on re-run.
node_id=$($COMPOSE exec -T garage /garage status \
    | awk '/^==== HEALTHY NODES/{f=1; next} f && $1 != "ID" && NF {print $1; exit}')
if [ -z "$node_id" ]; then
    echo "Could not determine Garage node id" >&2
    exit 1
fi
$COMPOSE exec -T garage /garage layout assign -z dc1 -c 1G "$node_id" || true
$COMPOSE exec -T garage /garage layout apply --version 1 || true
$COMPOSE exec -T garage /garage key import -n dev-key --yes \
    "$GARAGE_KEY_ID" "$GARAGE_KEY_SECRET" || true
$COMPOSE exec -T garage /garage bucket create "$GARAGE_BUCKET" || true
$COMPOSE exec -T garage /garage bucket allow --read --write --owner \
    "$GARAGE_BUCKET" --key dev-key || true

$COMPOSE run --rm web python manage.py migrate --noinput
$COMPOSE run --rm web python manage.py seed_dev

$COMPOSE up -d web
echo
echo "Yggdrasil dev is up: http://localhost:8000 (login: admin / admin)"
echo "Stop with: docker compose -f docker-compose.dev.yml down"
