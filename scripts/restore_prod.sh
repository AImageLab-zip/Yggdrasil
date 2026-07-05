#!/usr/bin/env bash
set -euo pipefail

# Restore a full MySQL dump (as produced by scripts/backup_prod.sh) into the
# running Docker db service.
#
# Use this for the v1.9 -> 2.0 server migration: restore the 1.9 dump into a
# fresh, EMPTY 2.0 database, THEN run `manage.py migrate` so the additive 2.0
# migrations apply on top of the restored 1.9 schema. See
# docs/upgrade-1.9-to-2.0.md.
#
# Usage:
#   scripts/restore_prod.sh <dump.sql.gz | dump.sql>
#
# Optional env:
#   DB_SERVICE=db     # Docker Compose db service name
#   FORCE=1           # restore even if the target DB already has tables
#                     # (DESTRUCTIVE — the dump's DROP TABLE statements win)
#
# The script refuses by default if the target database is non-empty, because a
# restore on top of an already-migrated 2.0 schema corrupts django_migrations.

DUMP_FILE="${1:-}"
DB_SERVICE="${DB_SERVICE:-db}"
FORCE="${FORCE:-0}"

if [[ -z "$DUMP_FILE" ]]; then
    echo "Usage: $0 <dump.sql.gz | dump.sql>"
    exit 1
fi
if [[ ! -f "$DUMP_FILE" ]]; then
    echo "Error: dump file not found: $DUMP_FILE"
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH"
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "Error: docker compose is not available (run from the repo root, with .env present)"
    exit 1
fi

# Decompress on the fly if gzipped.
if [[ "$DUMP_FILE" == *.gz ]]; then
    if ! gzip -t "$DUMP_FILE" 2>/dev/null; then
        echo "Error: $DUMP_FILE is not a valid gzip file"
        exit 1
    fi
    READ_CMD=(gzip -dc "$DUMP_FILE")
else
    READ_CMD=(cat "$DUMP_FILE")
fi

# Guard: refuse to restore onto a non-empty database unless FORCE=1.
TABLE_COUNT="$(docker compose exec -T "$DB_SERVICE" sh -lc \
    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();" \
    "$MYSQL_DATABASE"' | tr -d '[:space:]')"

if [[ "$TABLE_COUNT" != "0" && "$FORCE" != "1" ]]; then
    echo "Error: target database is not empty (${TABLE_COUNT} tables)."
    echo "       A restore here overwrites tables and corrupts django_migrations if 2.0"
    echo "       migrations already ran. Restore into a FRESH database (bring up the db"
    echo "       service with an empty mysql_data volume, AUTO_MIGRATE=0), or set FORCE=1"
    echo "       to override deliberately."
    exit 1
fi

echo "[1/2] Restoring $DUMP_FILE into service '$DB_SERVICE' (database from container env)"
"${READ_CMD[@]}" | docker compose exec -T "$DB_SERVICE" sh -lc \
    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'

echo "[2/2] Restore completed."
echo "Next step: apply the additive 2.0 migrations on top of the restored 1.9 schema."
echo "  docker compose --env-file .env up -d --build web   # entrypoint runs migrate"
echo "  # or, if AUTO_MIGRATE=0:"
echo "  docker exec -it yggdrasil-web-\${DOCKER_SUFFIX} python manage.py migrate"
