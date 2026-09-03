#!/usr/bin/env bash
set -euo pipefail

# Create a compressed, read-only MySQL backup from the running Docker db service.
#
# Usage:
#   scripts/backup_prod.sh [output_dir]
#
# Example:
#   scripts/backup_prod.sh ./backups
#
# Optional env:
#   DB_SERVICE=db                   # Docker Compose db service name
#   CHECK_SCHEMA=1                 # 1=verify the core 2.0 tables exist in the dump

OUTPUT_DIR="${1:-./backups}"
DB_SERVICE="${DB_SERVICE:-db}"
CHECK_SCHEMA="${CHECK_SCHEMA:-${CHECK_LEGACY_TABLES:-1}}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_FILE="${OUTPUT_DIR%/}/prod_backup_${TIMESTAMP}.sql.gz"

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker is not installed or not in PATH"
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Error: docker compose is not available"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

if [[ ! -w "$OUTPUT_DIR" ]]; then
    echo "Error: output directory is not writable: $OUTPUT_DIR"
    exit 1
fi

echo "[1/4] Creating compressed backup: $OUTPUT_FILE"
docker compose exec -T "$DB_SERVICE" sh -lc \
    'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers "$MYSQL_DATABASE"' \
    | gzip -1 > "$OUTPUT_FILE"

if [[ ! -s "$OUTPUT_FILE" ]]; then
    echo "Error: backup file is empty: $OUTPUT_FILE"
    exit 1
fi

echo "[2/4] Verifying gzip integrity"
gzip -t "$OUTPUT_FILE"

echo "[3/4] Locking backup file permissions"
chmod 600 "$OUTPUT_FILE"

if [[ "$CHECK_SCHEMA" == "1" ]]; then
    echo "[4/4] Checking for core 2.0 schema tables in dump"
    missing_tables=()
    # backup_prod.sh dumps the live 2.0 database, so the sanity check asserts the core
    # 2.0 tables are present (guards against an empty/truncated dump). Legacy scans_*
    # tables were renamed in 2.0 and are intentionally NOT expected here.
    required_tables=(
        "django_migrations"
        "common_project"
        "maxillo_fileregistry"
    )

    for table in "${required_tables[@]}"; do
        if ! zgrep -Eq "(CREATE TABLE|INSERT INTO)[[:space:]]+.*${table}" "$OUTPUT_FILE"; then
            missing_tables+=("$table")
        fi
    done

    if [[ ${#missing_tables[@]} -gt 0 ]]; then
        echo "Warning: backup created, but missing core 2.0 tables: ${missing_tables[*]}"
        echo "         The dump may be empty or truncated — verify before relying on it."
    else
        echo "Core schema check passed."
    fi
else
    echo "[4/4] Core schema check skipped (CHECK_SCHEMA=$CHECK_SCHEMA)"
fi

echo "Backup completed: $OUTPUT_FILE"
echo "Next step (restore into a fresh stack — see docs/admin-tasks.md):"
echo "  ./scripts/restore_prod.sh $OUTPUT_FILE"
