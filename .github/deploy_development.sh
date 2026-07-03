#!/usr/bin/env bash
set -euo pipefail

# Pull the latest development branch and redeploy the dev Docker Compose
# stack, then run migrations. Scoped to a single DOCKER_SUFFIX so it never
# touches prod or another dev instance's database (see docs/setup.md).
#
# Intended to run on the VPS, invoked over SSH by
# .github/workflows/deploy-development.yml on every push to `development`.
#
# Usage:
#   scripts/deploy_development.sh
#
# Optional env:
#   REPO_DIR=/srv/Yggdrasil-dev   # checkout to pull/redeploy
#   DOCKER_SUFFIX=dev             # stack suffix (containers/network/project name)

REPO_DIR="${REPO_DIR:-/srv/Yggdrasil-dev}"
DOCKER_SUFFIX="${DOCKER_SUFFIX:-dev}"

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH"
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Error: docker compose is not available"
    exit 1
fi

cd "$REPO_DIR"

echo "[1/3] Pulling latest development branch"
git fetch origin development
git checkout development
git reset --hard origin/development

echo "[2/3] Rebuilding and restarting the $DOCKER_SUFFIX stack"
DOCKER_SUFFIX="$DOCKER_SUFFIX" docker compose --env-file .env up -d --build

echo "[3/3] Running migrations"
docker exec "toothfairy4m-web-${DOCKER_SUFFIX}" python manage.py migrate --noinput

echo "Deploy complete: $(git rev-parse --short HEAD)"
