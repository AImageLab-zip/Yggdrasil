#!/usr/bin/env bash
# Rehearse `manage.py migrate` on a throwaway copy of the database.
#
# A migration that only fails on real data (a PROTECT nobody's fixture hits, a NOT NULL
# over rows the dev database doesn't have) should fail here, on a scratch copy, rather
# than in the web container's entrypoint with production down. scripts/deploy_prod.sh
# runs this between building the new images and restarting anything.
#
# Run from the repo root (the compose file decides which MySQL image to use):
#   scripts/rehearse_migrations.sh <dump.sql.gz> <web-image>
#   scripts/rehearse_migrations.sh backups/prod_backup_20261010_031500.sql.gz yggdrasil-prod-web:latest
#
# What it does, in order (stops at the first failure):
#   1. preflight: dump is valid gzip, web image exists, MySQL image from `docker compose config`
#   2. scratch MySQL on its own --internal network: no published ports, no named volume
#   3. wait for it, check there is room for the restored data
#   4. load the dump
#   5. `migrate --noinput` from <web-image>, then `migrate --check`
# Whatever happens (success, failure, Ctrl-C), the scratch container, its volume and the
# network are removed: the copy is a full copy of clinical patient data.
#
# Isolation: the network is --internal, so nothing on it can reach app-net, proxy-net,
# the host's published ports or the internet. On top of that the web container gets
# only the settings Django needs to boot (not .env), with Redis, the Celery broker and
# object storage pointed at addresses that do not exist.
#
# Optional env:
#   DB_SERVICE=db                   # compose service whose image the scratch DB uses
#   REHEARSAL_READY_TIMEOUT=300     # seconds to wait for the scratch MySQL
#   REHEARSAL_SPACE_FACTOR=10       # free space needed: dump size x factor + 2 GiB
#   REHEARSAL_LOG=<path>            # log file (default ./backups/migration_rehearsal_<ts>.log)
#
# Exit 0: every migration applied and none is left. Non-zero: see the log.
set -euo pipefail
cd "$(dirname "$0")/.."

DUMP_FILE="${1:-}"
WEB_IMAGE="${2:-}"
DB_SERVICE="${DB_SERVICE:-db}"
READY_TIMEOUT="${REHEARSAL_READY_TIMEOUT:-300}"
SPACE_FACTOR="${REHEARSAL_SPACE_FACTOR:-10}"
RID="$(date +%Y%m%d_%H%M%S)_$$"
LOG="${REHEARSAL_LOG:-./backups/migration_rehearsal_${RID}.log}"
LABEL="yggdrasil.rehearsal=${RID}"
NET="ygg-rehearsal-${RID}"
DB_CTR="ygg-rehearsal-db-${RID}"
WEB_CTR="ygg-rehearsal-web-${RID}"
DB_NAME=rehearsal
DB_USER=rehearsal
DB_VOLUMES=""

say() { printf '\n== %s\n' "$*" | tee -a "$LOG"; }
die() { printf 'ERROR: %s\n' "$*" | tee -a "$LOG" >&2; exit 1; }
random_hex() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

[[ -n "$DUMP_FILE" && -n "$WEB_IMAGE" ]] || { echo "Usage: $0 <dump.sql.gz> <web-image>" >&2; exit 2; }

mkdir -p "$(dirname "$LOG")"
(umask 077 && : >> "$LOG")
chmod 600 "$LOG"

cleanup() {
    local status=$?
    # A second Ctrl-C must not cut the cleanup short.
    trap '' INT TERM
    if [[ $status != 0 ]]; then
        printf '\nRehearsal FAILED (exit %s). Last lines of %s:\n' "$status" "$LOG" >&2
        tail -n 40 "$LOG" >&2 || true
    fi

    printf '\n-- cleanup\n' >>"$LOG"
    docker rm -f -v "$WEB_CTR" "$DB_CTR" >>"$LOG" 2>&1 || true
    # rm -v takes the anonymous volume with the container; this is the belt to those braces.
    for volume in $DB_VOLUMES; do
        docker volume rm -f "$volume" >>"$LOG" 2>&1 || true
    done
    docker network rm "$NET" >>"$LOG" 2>&1 || true

    local left=""
    left+=$(docker ps -aq --filter "label=$LABEL" 2>/dev/null || true)
    left+=$(docker network ls -q --filter "label=$LABEL" 2>/dev/null || true)
    for volume in $DB_VOLUMES; do
        if docker volume inspect "$volume" >/dev/null 2>&1; then left+=" $volume"; fi
    done
    if [[ -n "${left// /}" ]]; then
        printf '\nWARNING: rehearsal leftovers (a copy of patient data), remove them by hand:\n' >&2
        printf '  docker rm -f -v %s; docker volume rm %s; docker network rm %s\n' \
            "$DB_CTR" "${DB_VOLUMES:-<none>}" "$NET" >&2
        [[ $status != 0 ]] || status=1
    else
        echo "Scratch database, its volume and network removed."
    fi

    if [[ $status == 0 ]]; then
        printf 'Rehearsal passed. Log: %s\n' "$LOG"
    else
        printf 'Rehearsal failed. Log: %s\n' "$LOG" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# -- 1. preflight -------------------------------------------------------------
say "Preflight (log: $LOG)"
[[ -f "$DUMP_FILE" && -r "$DUMP_FILE" ]] || die "dump not found or not readable: $DUMP_FILE"
gzip -t "$DUMP_FILE" 2>>"$LOG" || die "$DUMP_FILE is not a valid gzip file"
docker image inspect "$WEB_IMAGE" >/dev/null 2>&1 || die "web image not found: $WEB_IMAGE"
DB_IMAGE=$(docker compose config --images "$DB_SERVICE" 2>>"$LOG") \
    || die "docker compose config failed (run from the repo root, with .env present)"
[[ -n "$DB_IMAGE" && "$DB_IMAGE" != *$'\n'* ]] || die "no single image for compose service '$DB_SERVICE'"
docker image inspect "$DB_IMAGE" >/dev/null 2>&1 || docker pull "$DB_IMAGE" >>"$LOG" 2>&1 \
    || die "cannot pull $DB_IMAGE"
DUMP_BYTES=$(wc -c < "$DUMP_FILE" | tr -d ' ')
echo "  dump:  $DUMP_FILE ($(( DUMP_BYTES / 1024 / 1024 )) MiB compressed)" | tee -a "$LOG"
echo "  web:   $WEB_IMAGE" | tee -a "$LOG"
echo "  mysql: $DB_IMAGE" | tee -a "$LOG"

# -- 2. scratch database ----------------------------------------------------------
say "Starting scratch MySQL on an isolated network"
docker network create --internal --label "$LABEL" "$NET" >>"$LOG" 2>&1 \
    || die "cannot create network $NET"
# Passed by name (-e VAR), so the passwords never appear in a process list.
# --skip-log-bin: the binary log would double the disk the load needs, for nothing.
MYSQL_ROOT_PASSWORD=$(random_hex) MYSQL_PASSWORD=$(random_hex) \
MYSQL_DATABASE=$DB_NAME MYSQL_USER=$DB_USER \
    docker run -d --name "$DB_CTR" --network "$NET" --label "$LABEL" \
        -e MYSQL_ROOT_PASSWORD -e MYSQL_PASSWORD -e MYSQL_DATABASE -e MYSQL_USER \
        "$DB_IMAGE" --skip-log-bin >>"$LOG" 2>&1 \
    || die "cannot start the scratch MySQL container"
DB_VOLUMES=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$DB_CTR")

# -- 3. ready + disk ----------------------------------------------------------------
say "Waiting for scratch MySQL (up to ${READY_TIMEOUT}s)"
# Over TCP: the image's init phase runs a socket-only server that would answer a
# socket ping before the real server (and the app user) exist.
deadline=$(( SECONDS + READY_TIMEOUT ))
until docker exec "$DB_CTR" sh -c \
        'mysqladmin ping --protocol=tcp -h127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" --silent' \
        >>"$LOG" 2>&1; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$DB_CTR" 2>/dev/null)" != true ]]; then
        docker logs --tail 50 "$DB_CTR" >>"$LOG" 2>&1 || true
        die "scratch MySQL exited during startup"
    fi
    (( SECONDS < deadline )) || die "scratch MySQL not ready after ${READY_TIMEOUT}s"
    sleep 2
done
echo "  ready" | tee -a "$LOG"

NEED_KB=$(( DUMP_BYTES * SPACE_FACTOR / 1024 + 2 * 1024 * 1024 ))
FREE_KB=$(docker exec "$DB_CTR" df -Pk /var/lib/mysql 2>>"$LOG" | awk 'NR==2 {print $4}')
[[ "$FREE_KB" =~ ^[0-9]+$ ]] || die "cannot measure free space for the scratch database"
echo "  free: $(( FREE_KB / 1024 )) MiB, needed (dump x ${SPACE_FACTOR} + 2 GiB): $(( NEED_KB / 1024 )) MiB" | tee -a "$LOG"
(( FREE_KB >= NEED_KB )) || die "not enough disk for the scratch database: $(( FREE_KB / 1024 )) MiB free, $(( NEED_KB / 1024 )) MiB needed. Free space first; filling this disk would also hurt the production database on it."

# -- 4. load ------------------------------------------------------------------------
say "Loading the dump"
started=$SECONDS
gzip -dc "$DUMP_FILE" | docker exec -i "$DB_CTR" sh -c \
    'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' >>"$LOG" 2>&1 \
    || die "loading the dump failed"
echo "  loaded in $(( SECONDS - started ))s" | tee -a "$LOG"

# -- 5. migrate -----------------------------------------------------------------------
# Only what settings.py refuses to boot without, plus overrides for everything that
# could reach a live service. memory:// is a broker that exists only in-process, so a
# stray .delay() goes nowhere instead of hanging on retries.
WEB_ENV=(
    "SECRET_KEY=rehearsal-$(random_hex)"
    "DEBUG=false"
    "DB_NAME=$DB_NAME"
    "DB_USER=$DB_USER"
    "DB_PASSWORD=$(docker exec "$DB_CTR" printenv MYSQL_PASSWORD)"
    "DB_HOST=$DB_CTR"
    "DB_PORT=3306"
    "EMAIL_BACKEND=django.core.mail.backends.dummy.EmailBackend"
    "EMAIL_HOST=localhost"
    "EMAIL_PORT=25"
    "EMAIL_HOST_USER="
    "EMAIL_HOST_PASSWORD="
    "EMAIL_USE_TLS=false"
    "EMAIL_USE_SSL=false"
    "DEFAULT_FROM_EMAIL=rehearsal@localhost"
    "REDIS_PASSWORD=rehearsal"
    "REDIS_HOST=rehearsal-has-no-redis.invalid"
    "CELERY_BROKER_URL=memory://"
    "CELERY_RESULT_BACKEND=cache+memory://"
    "OBJECT_STORAGE_ENDPOINT_URL=http://rehearsal-has-no-object-storage.invalid:9"
    "OBJECT_STORAGE_ACCESS_KEY_ID=rehearsal"
    "OBJECT_STORAGE_SECRET_ACCESS_KEY=rehearsal"
    "OBJECT_STORAGE_BUCKET=rehearsal-has-no-bucket"
    "WHISPER_WS_URL=ws://rehearsal-has-no-whisper.invalid:9"
)
WEB_ENV_NAMES=()
for kv in "${WEB_ENV[@]}"; do WEB_ENV_NAMES+=(-e "${kv%%=*}"); done
run_web() {
    env "${WEB_ENV[@]}" docker run --rm --name "$WEB_CTR" --network "$NET" --label "$LABEL" \
        "${WEB_ENV_NAMES[@]}" "$WEB_IMAGE" "$@"
}

say "Applying migrations from $WEB_IMAGE"
run_web python manage.py migrate --noinput 2>&1 | tee -a "$LOG" || die "migrate failed"

say "Checking nothing is left unapplied"
run_web python manage.py migrate --check --noinput >>"$LOG" 2>&1 \
    || die "migrate --check: migrations are still unapplied after migrate"
echo "  none left" | tee -a "$LOG"
