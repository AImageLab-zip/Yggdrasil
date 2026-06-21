#!/bin/bash
set -e

TABLES=("auth_user" "common_projectaccess" "common_project" "common_modality")
SQL_FILE="$1"

[ -z "$SQL_FILE" ] && echo "Usage: $0 <sql_file>" && exit 1
[ ! -f "$SQL_FILE" ] && echo "Error: File not found" && exit 1
[ ! -f .env ] && echo "Error: .env not found" && exit 1

while IFS='=' read -r key value; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    [[ "$key" == "UID" ]] && continue
    export "$key=$value"
done < .env
DB_CONTAINER="toothfairy4m-db${DOCKER_SUFFIX:+-${DOCKER_SUFFIX}}"

TEMP_SQL=$(mktemp)
trap "rm -f $TEMP_SQL" EXIT

echo "SET FOREIGN_KEY_CHECKS=0;" > "$TEMP_SQL"
for table in "${TABLES[@]}"; do
    awk "/DROP TABLE IF EXISTS \`$table\`/,/UNLOCK TABLES;/" "$SQL_FILE" >> "$TEMP_SQL"
done
echo "SET FOREIGN_KEY_CHECKS=1;" >> "$TEMP_SQL"

docker cp "$TEMP_SQL" "${DB_CONTAINER}:/tmp/import.sql"
docker exec "${DB_CONTAINER}" mysql -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" "${MYSQL_DATABASE}" -e "source /tmp/import.sql"
docker exec "${DB_CONTAINER}" rm /tmp/import.sql

echo "Import completed!"
