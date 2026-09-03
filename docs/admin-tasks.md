# Admin tasks

Production and day-to-day operational tasks. Most commands run inside the web
container and assume `DOCKER_SUFFIX` is exported in your shell, matching the
value in `.env`:

```bash
export DOCKER_SUFFIX=prod          # or dev-yourname
```

Throughout, `web` is shorthand for
`docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py …`.

## Users

### Create a superuser

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py createsuperuser
```

### Promote an existing user

Through the Django admin (`/admin/`), or:

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py shell -c \
  "from django.contrib.auth.models import User; u=User.objects.get(username='NAME'); u.is_staff=True; u.is_superuser=True; u.save()"
```

Note that being staff is not the same as having project access: authorization is
`Project` + `ProjectAccess` (viewer / annotator / admin). Staff bypass the
maintenance modes and are auto-provisioned access, which is convenient and easy
to mistake for a permission check working.

## Site maintenance and lockdown

`common.SiteMaintenance` is a singleton row with three access modes, edited in
the Django admin (`/admin/common/sitemaintenance/`) or from the admin control
panel at `/admin/control-panel/`:

| Mode | Effect |
|---|---|
| `normal` | Nothing special. |
| `read_only` | Write requests are refused with **423**; reads still work. |
| `lockdown` | Everything is refused with **503**. |

Staff bypass both. `SiteMaintenanceMiddleware` exempts `/static/`, `/login/`,
`/logout/`, `/maintenance/`, `/healthz` and **every `/api/runner/` path** —
runners must keep claiming and reporting jobs while the site is closed to users,
or a maintenance window turns into a pile of stuck jobs.

The same row carries a planned-maintenance banner: set `planned_message` and
tick `planned_message_enabled` to warn users ahead of a window without changing
the access mode.

Health and status: `/healthz` (liveness), `/status/` (the operator page), and
`/api/processing/health/` (job counts plus object-storage connectivity).

## Database backups

A nightly Celery beat task (`common.tasks.backup_database`, 03:00 UTC) dumps the
database with `mysqldump --single-transaction`, gzips it, verifies the gzip,
uploads it to object storage and prunes old backups, recording the outcome as a
`SystemCheck` row visible on the status page.

It runs on the **maintenance queue**, consumed only by the local
`maintenance-worker` compose service. `MAINTENANCE_QUEUE` must never collide with
`RUNNER_DEFAULT_QUEUE` or any `RUNNER_QUEUE_BY_*` value, or an external runner
would consume backup tasks; settings refuse to start on a collision.

Run one on demand, synchronously, through exactly the same code path:

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py backup_now
```

It prints the result dict and exits non-zero on failure.

### Retention

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_KEEP_DAILY` | `14` | Keep the newest N backups, whatever their dates. |
| `BACKUP_KEEP_WEEKLY` | `8` | Beyond those, keep at most one backup per ISO week, for N weeks. |
| `BACKUP_KEY_PREFIX` | `backups/mysql/` | Key prefix in the object store. |

Everything older is deleted. Keys are named
`<prefix><database>_<UTC timestamp>.sql.gz`, and only keys matching that pattern
are considered — an unrelated object under the prefix is neither kept nor pruned,
it is ignored.

### Manual dump and restore

`scripts/backup_prod.sh [output_dir]` writes a compressed dump from the running
`db` service to the host. `scripts/restore_prod.sh <dump.sql.gz>` restores one.

The restore **refuses a non-empty target database by default**, because
restoring on top of an already-migrated schema corrupts `django_migrations`.
`FORCE=1` overrides it, destructively. To move an instance to a new host:
restore into a fresh, empty database *first*, then run `migrate`.

## Jobs

### Re-run processing for existing patients

When a new or updated algorithm ships for a modality, `resubmit_jobs` walks the
existing patients of a domain and gives them work:

```bash
# See what would happen
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py resubmit_jobs \
    --domain maxillo --modality cbct --dry-run

# Create jobs for patients that have none
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py resubmit_jobs \
    --domain brain --modality braintumor_mri_seg

# Also re-pend patients that already have a job
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py resubmit_jobs \
    --domain maxillo --modality cbct --include-existing
```

Options: `--domain` (required), `--modality` (required), `--include-existing`,
`--folder-id`, `--limit`, `--dry-run`.

It creates or re-pends `Job` rows and lets the normal `post_save` signal enqueue
them, so routing, the modality kill switch and the queue tables all apply exactly
as they do on upload. **There is no separate submit path** — anything that wants
a job run creates a row.

## Object storage

### Migrating a legacy `/dataset` tree into object storage

`migrate_dataset_to_object_storage` uploads artifacts that still live on a local
dataset volume and rewrites the database references that point at them.

```bash
# Dry run (the default)
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py \
    migrate_dataset_to_object_storage

# Do it
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py \
    migrate_dataset_to_object_storage --apply
```

Options: `--dataset-root` (defaults to `settings.DATASET_PATH`, else `/dataset`), `--apply`,
`--limit N` (per model, `0` = no limit), `--fail-missing` (abort instead of
reporting missing paths), and `--trust-storage`.

`--trust-storage` is for **finishing an interrupted run**: it decides by what is
already in object storage rather than what is on local disk, and never uploads.
That is the case where the blobs went up, the database references did not, and
the filesystem they came from is gone.

### Cloning a bucket

`scripts/mirror_bucket.py` copies one S3/Garage bucket into another, key for
key, server-side — `CopyObject` is a single request naming the source, so no
object bytes pass through the script. Keys are preserved exactly, which is only
correct because the key layout is derived from the **domain** and did not change
across the upgrade; `OBJECT_STORAGE_KEY_PREFIX` is deliberately ignored.

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python scripts/mirror_bucket.py \
    --source old-bucket --dest new-bucket --dry-run

docker exec -it yggdrasil-web-$DOCKER_SUFFIX python scripts/mirror_bucket.py \
    --source old-bucket --dest new-bucket --skip-existing
```

Useful flags: `--prefix`, `--dry-run`, `--skip-existing`, `--verify`,
`--concurrency` (default 8). Connection settings come from the
`OBJECT_STORAGE_*` environment, so inside the web container it is already
configured; `--endpoint-url` / `--access-key-id` / `--secret-access-key` override
them when the two buckets do not share one endpoint.

Both buckets must be reachable through the same endpoint and credential for the
copy to stay server-side.

### Annotation corpus sweeps

Bulk work over stored annotations lives in `annotations/management/commands/`:
`annotations_normalize_coordinates`, `annotations_materialize_landmarks`,
`annotations_crosscheck`, `annotations_compute_roi_stats`,
`annotations_rasterize_video_masks`, `annotations_convert_legacy`.

They share one shape: idempotent, `--dry-run`, `--limit`, and one bad object
costs its own rows rather than the whole sweep. Run them with `--dry-run` first
and read the report; they download whole volumes and are not request-path work.

## Seeding a new deployment

Each domain ships an idempotent command that creates its `Project` row and
registers its modalities — uploads fail until it has been run:

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py create_maxillo_modalities
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py setup_brain_modalities
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py setup_laparoscopy_modalities
```

See [setup.md](setup.md) for the rest of first-time setup and [running.md](running.md)
for everyday commands.
