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
#   CHECK_LEGACY_TABLES=1          # 1=verify scans_* tables exist in dump

OUTPUT_DIR="${1:-./backups}"
DB_SERVICE="${DB_SERVICE:-db}"
CHECK_LEGACY_TABLES="${CHECK_LEGACY_TABLES:-1}"
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

if [[ "$CHECK_LEGACY_TABLES" == "1" ]]; then
    echo "[4/4] Checking for legacy scans_* tables in dump"
    missing_tables=()
    required_tables=(
        "scans_patient"
        "scans_job"
        "scans_fileregistry"
        "scans_export"
        "common_project"
    )

    for table in "${required_tables[@]}"; do
        if ! zgrep -Eq "(CREATE TABLE|INSERT INTO)[[:space:]]+.*${table}" "$OUTPUT_FILE"; then
            missing_tables+=("$table")
        fi
    done

    if [[ ${#missing_tables[@]} -gt 0 ]]; then
        echo "Warning: backup created, but missing expected tables: ${missing_tables[*]}"
        echo "         If this dump is not from legacy schema, reimport may not be applicable."
    else
        echo "Legacy table check passed."
    fi
else
    echo "[4/4] Legacy table check skipped (CHECK_LEGACY_TABLES=$CHECK_LEGACY_TABLES)"
fi

echo "Backup completed: $OUTPUT_FILE"
echo "Next step (restore into a fresh 2.0 stack — see docs/upgrade-1.9-to-2.0.md):"
echo "  ./scripts/restore_prod.sh $OUTPUT_FILE"
