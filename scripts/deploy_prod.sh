#!/usr/bin/env bash
# Deploy the checked-out branch's upstream to this production stack.
#
# Run from the production checkout (e.g. /srv/Yggdrasil-prod):
#   scripts/deploy_prod.sh            # backup, pull, build, fix volume ownership, restart
#   SKIP_PULL=1 scripts/deploy_prod.sh    # deploy the working tree as it is
#
# The previously deployed commit (for the rollback instructions) is read from
# .deployed-ref, which this script writes after each successful deploy. The first time,
# when the script itself arrives with the pull, pass it explicitly:
#   git pull --ff-only && PREVIOUS_REF=<commit that was running> scripts/deploy_prod.sh
#
# What it does, in order (stops at the first failure; nothing is restarted until
# the new images have built):
#   1. preflight: repo root, .env, UID/GID, clean tracked files, secret ownership
#   2. database backup (scripts/backup_prod.sh -> ./backups)
#   3. tag the running images :rollback, fast-forward to upstream
#   4. build the new images
#   5. chown the app volumes to UID:GID (the services no longer run as root)
#   6. docker compose up -d (web applies migrations on start), wait for /healthz
#   7. print the rollback commands
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# -- 1. preflight -------------------------------------------------------------
say "Preflight"
[[ -f docker-compose.yml && -f .env ]] || die "run from the production checkout (docker-compose.yml and .env)"
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
SUFFIX=$(env_get DOCKER_SUFFIX); APP_UID=$(env_get UID); APP_GID=$(env_get GID)
WEB_PORT=$(env_get WEB_EXTERNAL_PORT)
[[ -n "$SUFFIX" ]] || die "DOCKER_SUFFIX missing from .env"
[[ "$APP_UID" =~ ^[0-9]+$ && "$APP_GID" =~ ^[0-9]+$ ]] || die "UID and GID must be numeric in .env (the services run as UID:GID)"
[[ "$APP_UID" != 0 ]] || die "UID=0 in .env would run the services as root"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no
    die "tracked files are modified; commit or stash them first"
fi
# The runner now runs as UID: the key must be its own (ssh refuses group/world-readable
# keys), and the pinned host keys must be readable by it.
KEY_FILE=${SLURM_SSH_KEY_HOST:-./secrets/slurm_ssh_key}
KNOWN_HOSTS_FILE=${SLURM_KNOWN_HOSTS_HOST:-./secrets/slurm_known_hosts}
[[ -f "$KEY_FILE" && -f "$KNOWN_HOSTS_FILE" ]] || die "missing $KEY_FILE or $KNOWN_HOSTS_FILE (mounted into the runner worker)"
[[ "$(stat -c %u "$KEY_FILE")" == "$APP_UID" ]] || die "$KEY_FILE must be owned by uid $APP_UID: sudo chown $APP_UID $KEY_FILE && chmod 600 $KEY_FILE"
if [[ "$(stat -c %u "$KNOWN_HOSTS_FILE")" != "$APP_UID" && $(( 0$(stat -c %a "$KNOWN_HOSTS_FILE") & 4 )) == 0 ]]; then
    die "$KNOWN_HOSTS_FILE must be readable by uid $APP_UID"
fi
if ! grep -qE '^RUNNER_STAGE_MODE=credentials' .env; then
    echo "NOTE: jobs will use presigned storage grants (RUNNER_STAGE_MODE=presigned)."
    echo "      The cluster needs ygg-stage 0.2 BEFORE processing resumes:"
    echo "        cd /work/yggdrasil_workers/Yggdrasil && git pull && .venv/bin/python -m pip install -e slurm"
    echo "      Until then, set RUNNER_STAGE_MODE=credentials in .env (see slurm/README.md)."
fi
COMPOSE="docker compose"
$COMPOSE config --quiet || die "docker compose config is invalid"
OLD_REF=${PREVIOUS_REF:-$(cat .deployed-ref 2>/dev/null || git rev-parse HEAD)}
git cat-file -e "$OLD_REF^{commit}" 2>/dev/null || die "PREVIOUS_REF $OLD_REF is not a commit"

# -- 2. backup -----------------------------------------------------------------
say "Database backup"
scripts/backup_prod.sh ./backups

# -- 3. rollback tags + code ---------------------------------------------------
say "Tagging running images for rollback"
APP_SERVICES=(web beat maintenance-worker runner-worker)
for service in "${APP_SERVICES[@]}"; do
    image="yggdrasil-${SUFFIX}-${service}"
    if docker image inspect "$image:latest" >/dev/null 2>&1; then
        docker tag "$image:latest" "$image:rollback"
        echo "  $image:rollback"
    fi
done

if [[ "${SKIP_PULL:-0}" != 1 ]]; then
    say "Updating code (fast-forward only)"
    git pull --ff-only
fi
NEW_REF=$(git rev-parse HEAD)
echo "  $OLD_REF -> $NEW_REF"

# -- 4. build -------------------------------------------------------------------
say "Building images"
$COMPOSE build "${APP_SERVICES[@]}"

# -- 5. volume ownership ----------------------------------------------------------
# The services used to run as root, so existing volume contents are root-owned.
say "Handing app volumes to $APP_UID:$APP_GID"
chown_in() {  # service, paths...
    local service=$1; shift
    $COMPOSE run --rm --no-deps --user 0:0 --entrypoint chown "$service" -R "$APP_UID:$APP_GID" "$@"
}
chown_in web /app/logs /app/staticfiles /app/media /tmp/processing
chown_in maintenance-worker /app/backups
chown_in beat /tmp

# -- 6. restart + health ------------------------------------------------------------
say "Restarting app services"
$COMPOSE up -d --remove-orphans "${APP_SERVICES[@]}"

say "Waiting for web (migrations run on start)"
healthy=0
for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${WEB_PORT}/healthz"; then healthy=1; break; fi
    sleep 2
done
$COMPOSE ps
if [[ $healthy != 1 ]]; then
    $COMPOSE logs --tail 80 web || true
    die "web did not become healthy; roll back with the commands below"
fi
for service in beat maintenance-worker runner-worker; do
    state=$($COMPOSE ps --format '{{.State}}' "$service" 2>/dev/null || true)
    [[ "$state" == running ]] || echo "WARNING: $service is '$state' -- check: $COMPOSE logs $service"
done
echo "web is healthy on 127.0.0.1:${WEB_PORT}"
echo "$NEW_REF" > .deployed-ref

# -- 7. rollback / cleanup -------------------------------------------------------------
cat <<EOF

== Deployed $NEW_REF

Rollback (code and images; the new migrations only add tables/expiry dates and are safe
to leave applied):
  git checkout $OLD_REF
  for s in ${APP_SERVICES[*]}; do docker tag yggdrasil-${SUFFIX}-\$s:rollback yggdrasil-${SUFFIX}-\$s:latest; done
  docker compose up -d --no-build ${APP_SERVICES[*]}

Once satisfied, delete the rollback images. The pre-hardening images contain .env,
.env.worker, the SLURM key and SQL dumps (they were built without a .dockerignore):
  for s in ${APP_SERVICES[*]}; do docker rmi yggdrasil-${SUFFIX}-\$s:rollback; done
  docker image prune -f
EOF
