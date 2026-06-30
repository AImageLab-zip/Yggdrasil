# Setup

First-time setup for running Yggdrasil locally with Docker Compose.

## Prerequisites

- Docker and Docker Compose
- A Docker network shared by everyone running this stack on the host (see below)

## External dependencies

Two infrastructure pieces sit outside the Django app's own code and are easy to overlook:

- **Redis** — started as the `redis` service in [docker-compose.yml](../docker-compose.yml) (so `docker compose up` brings it up for you), but it isn't just an internal cache: it's the Celery broker that external distributed runners connect to directly to pick up and report on jobs (see [docs/runners.md](runners.md)). If you change `REDIS_PASSWORD` or `REDIS_EXTERNAL_PORT`, runner nodes need the matching values too.
- **Garage** (or any S3-compatible object store, e.g. MinIO) — **not** part of `docker-compose.yml` at all. It's a fully external service that must already be running and reachable from the `toothfairy4m-web-$DOCKER_SUFFIX` container at `OBJECT_STORAGE_ENDPOINT_URL` (`.env.example` defaults to `http://garage:3900`). Make sure that container can actually resolve/reach the `garage` host — either join the same Docker network Garage is on, or point `OBJECT_STORAGE_ENDPOINT_URL` at a routable address — otherwise uploads/exports will fail with object storage errors (check `/api/processing/health/`, see [docs/running.md](running.md)).

## 1. Pick a `DOCKER_SUFFIX`

Every container, network, and project name in [docker-compose.yml](../docker-compose.yml) is suffixed with `$DOCKER_SUFFIX`, e.g. `toothfairy4m-web-$DOCKER_SUFFIX`, `app-net-$DOCKER_SUFFIX`. This lets multiple stacks (your dev instance, a teammate's, production) run on the same host without colliding.

Pick something unique to you, e.g. `dev-yourname`, or `prod` for the production deployment.

## 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

- `DOCKER_SUFFIX` — the value you picked above
- `UID` / `GID` — your host user/group id (`id -u` / `id -g`), so files written by the container are owned by you
- `SECRET_KEY` — any random string for Django
- `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`
- `REDIS_PASSWORD`
- `RUNNER_API_TOKENS` — token(s) the external runners will use

`docker-compose.yml` always reads from `.env` in the repo root (`env_file: .env`), so make sure that's the file you edited.

## 3. Create the shared Docker network

The `app-net-$DOCKER_SUFFIX` network is declared as `external: true`, meaning Compose expects it to already exist — it won't create it for you:

```bash
docker network create app-net-$DOCKER_SUFFIX
```

You also need the `proxy-net` external network (shared reverse proxy network), if it doesn't already exist on the host:

```bash
docker network create proxy-net
```

## 4. Bring the stack up

See [running.md](running.md) for day-to-day commands. The short version:

```bash
docker compose --env-file .env up -d --build
```

This builds the web image, then starts `web` (Django), `db` (MySQL), and `redis`.

## 5. Run migrations

`entrypoint.sh` deliberately does **not** run `migrate` on container start (it's commented out, since auto-migrating on every restart is unsafe with multiple stacks sharing a DB). On first run — and after pulling any change with new migrations — run it yourself:

```bash
export DOCKER_SUFFIX=YOUR-DOCKER-SUFFIX
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py migrate
```

## 6. Seed projects and modalities

Each project app ships a management command that creates its `Project` row and registers its `Modality` rows (file types it accepts). The database is empty without this — uploads will fail until it's run, since `Patient.modalities` and upload forms validate against existing `Modality` records.

```bash
# Maxillo: CBCT, IOS, intraoral photos, teleradiography, panoramic, raw zip
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py create_maxillo_modalities

# Brain: reuses the same Patient/Modality model under its own project namespace
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py setup_brain_modalities

# Laparoscopy: video modality
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py setup_laparoscopy_modalities
```

These are idempotent (`get_or_create` + update) — safe to re-run after upgrades that add/change modalities.

## 7. Optional: laparoscopy AI worker

Laparoscopy's point-prompt segmentation proxies to an external worker service. If you're not running one, those endpoints will fail closed but the rest of the app works fine. To enable it, set in `.env`:

```
WORKER_BASE_URL=http://your-worker-host:port
```

(Per-endpoint overrides `WORKER_SESSION_READY_URL` / `WORKER_SESSION_PROMPT_URL` are also available — see `.env.example`.)
