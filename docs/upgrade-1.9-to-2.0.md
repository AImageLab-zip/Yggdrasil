# Upgrading a server from v1.9 to 2.0

This is the end-to-end runbook for migrating a production Yggdrasil instance from
the legacy **v1.9** line to **2.0**. The supported path is *not* an in-place
upgrade of the old VM — it is:

> **mysqldump the old 1.9 VM → restore that dump into a brand-new 2.0 stack with an
> empty database → let 2.0 apply its additive migrations on top → re-point object
> storage → verify.**

This is the path frozen by tag `v1.9.0` (commit `52d1557`) and risk item #0 in
[modernization-roadmap.md](modernization-roadmap.md). Every migration added after
`v1.9.0` is strictly additive, so a restored 1.9 database migrates cleanly to 2.0.

> **The database dump is not the whole system.** `mysqldump` captures rows only.
> Uploaded files (CBCT, IOS, videos, exports) live in **object storage (Garage / any
> S3-compatible store)**, referenced by `FileRegistry` rows. If you restore the DB
> but do not also carry the object store over (Step 5), patients and jobs come back
> but every file download 404s. Redis holds only transient job/broker state and does
> **not** need migrating.

---

## Prerequisites

- Access to the running **1.9** VM (to produce the dump).
- A **fresh host** for 2.0 with Docker + Docker Compose, and the object store
  (Garage/MinIO) either reachable from it or ready to receive a copy.
- The 2.0 repo checked out on the new host (`release/2.0`, or the `v2.0.0` tag once
  cut). A completed [setup.md](setup.md) `.env` — same `MYSQL_DATABASE` name is fine;
  secrets (`SECRET_KEY`, DB/Redis passwords, `RUNNER_API_TOKENS`) can be new.

Throughout, `$DOCKER_SUFFIX` is the value from your 2.0 `.env` (e.g. `prod`). The
`docker exec` / `docker compose` commands below interpolate it (and `UID`/`GID`) from
your shell, so export them **once per session** before you start — otherwise container
names resolve to `yggdrasil-web-` (→ `No such container`) and compose warns
`UID variable is not set`:

```bash
export DOCKER_SUFFIX=prod           # match DOCKER_SUFFIX in your .env
export UID="$(id -u)" GID="$(id -g)"
# If your user isn't in the `docker` group, prefix docker commands with sudo.
```

---

## Step 1 — Dump the 1.9 database

On the **1.9 VM**. The 1.9 tag predates `scripts/backup_prod.sh`, so use a raw
`mysqldump` against its db container (adjust the container name / credentials to the
old stack):

```bash
# From the 1.9 VM. OLD_DB_CONTAINER = the 1.9 mysql container name.
docker exec -e MYSQL_PWD="$OLD_ROOT_PASSWORD" OLD_DB_CONTAINER \
    mysqldump -uroot --single-transaction --routines --triggers "$OLD_DB_NAME" \
    | gzip -1 > "ygg_1.9_$(date +%Y%m%d_%H%M%S).sql.gz"
```

`--single-transaction` gives a consistent snapshot without locking; `--routines
--triggers` carry stored routines. If the 1.9 checkout already has
`scripts/backup_prod.sh`, that script does the same thing plus a gzip-integrity and
legacy-table check — prefer it.

Copy the resulting `.sql.gz` to the new host. **Guard it** — it contains all patient
data; keep it `chmod 600` and delete scratch copies afterward.

## Step 2 — Bring up a fresh 2.0 database (empty), migrations OFF

On the **new host**, in the repo root. Do the one-time [setup.md](setup.md) items
first (`.env`, `docker network create app-net-$DOCKER_SUFFIX`, `proxy-net`).

The ordering that matters: the restore must land on an **empty** database, *before*
2.0 migrations run. If the `web` container boots first with `AUTO_MIGRATE=1` (the
default), it creates the 2.0 schema in the empty DB and the subsequent 1.9 restore
collides with it. So bring up **only the db service** first:

```bash
# Empty mysql_data volume + no web container yet.
docker compose --env-file .env up -d db
```

Wait for it to become healthy (`docker compose ps` → `db` healthy). Set
`AUTO_MIGRATE=0` in `.env` for now so an accidental `web` start can't migrate the
empty DB before the restore.

## Step 3 — Full restore of the 1.9 dump

Use the full-database restore script:

```bash
./scripts/restore_prod.sh ygg_1.9_YYYYMMDD_HHMMSS.sql.gz
```

It refuses if the target DB already has tables (guarding against a restore over an
already-migrated schema); pass `FORCE=1` only if you deliberately mean to overwrite.
On success the 2.0 DB now holds the exact 1.9 schema, data, and `django_migrations`
history up to `v1.9.0`.

## Step 4 — Apply the additive 2.0 migrations

Set `AUTO_MIGRATE=1` again (or run migrate by hand) and bring up the app. `migrate`
sees the restored `django_migrations` and applies only the migrations added after
`v1.9.0`:

```bash
docker compose --env-file .env up -d --build   # web entrypoint runs migrate --noinput
# or, with AUTO_MIGRATE=0, run it explicitly:
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py migrate
```

Confirm the plan is additive-only if you want to eyeball it first:

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py migrate --plan
```

## Step 5 — Carry over object storage

The DB now references files by key in `FileRegistry`, but the blobs live in the
object store. Two options:

- **Re-point (simplest):** if the existing Garage/MinIO is still running and reachable
  from the new host, set `OBJECT_STORAGE_ENDPOINT_URL` (and bucket/credentials) in the
  2.0 `.env` to that same store. No data copy — the new stack reads the old blobs.
- **Copy:** stand up a fresh object store and mirror the bucket
  (`rclone`/`mc mirror`/`aws s3 sync`) from old to new, then point `.env` at the new one.

Either way the keys in `FileRegistry` must resolve against whatever store `.env`
names, or downloads 404.

## Step 6 — Refresh modalities and run post-deploy tasks

Modality registration is idempotent — re-run so any modality rows added in 2.0 exist
(see [setup.md](setup.md) step 6):

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py create_maxillo_modalities
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py setup_brain_modalities
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py setup_laparoscopy_modalities
```

Then the two behaviour-change follow-ups from the roadmap risk register:

- **Laparoscopy folder grants (risk #5).** Phase 0's migration granted `standard`
  `FolderAccess` on *all* laparoscopy folders to existing project members to preserve
  1.9 visibility. In the admin, review and revoke any grant that shouldn't be there.
- **Maintenance queue (risk #3).** Confirm `MAINTENANCE_QUEUE` in `.env` does not
  collide with `RUNNER_DEFAULT_QUEUE` / any `RUNNER_QUEUE_BY_*` — settings refuse to
  start if it does, but check before deploy so external runners never consume backup
  jobs.

---

## Verification — the 2.0 pre-release checklist

These are the items the modernization phases deferred to a prod-like environment
("Road to 2.0 release" in [modernization-roadmap.md](modernization-roadmap.md)). Run
them to sign off the release. Items 1–2 are DB-level and reproducible on any scratch
host; 3–4 need the app serving and a runner; 5 is the tag.

### 1. Risk-item-0 rehearsal (release blocker)

Prove a real 1.9 dump migrates clean on top of a restored database. This is
Steps 1→4 above against a **scratch** stack, then confirm no migrations are pending:

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py migrate --check   # no pending
```

You do not need object storage for this rehearsal — it exercises schema + ORM only.

> **Do not run `manage.py test` against this stack.** The prod/app DB user is scoped to
> the one application database and lacks `CREATE` privilege for the `test_*` database
> Django builds, so the suite fails with `(1044, "Access denied ... to database
> 'test_yggdrasil'")`. Run the full suite via the dev/CI path, which is already
> configured with a root DB user: `docker compose -f docker-compose.dev.yml run --rm web
> python manage.py test` (or let CI run it). "Full suite green" is a CI gate, not a
> prod-container step.

### 2. Backup restore test (Phase 2)

Prove the automated backup path round-trips: produce a dump with the 2.0 tooling,
then restore it into a throwaway DB.

```bash
docker exec -it yggdrasil-web-$DOCKER_SUFFIX python manage.py backup_now   # writes to object storage
./scripts/backup_prod.sh ./backups                                            # or a local dump
# restore into a scratch stack and confirm it loads:
FORCE=1 ./scripts/restore_prod.sh ./backups/prod_backup_*.sql.gz
```

`backup_prod.sh` ends with a schema sanity check that confirms the dump holds the core
2.0 tables — it prints `Core schema check passed.` on a healthy 2.0 backup (it does
**not** warn about absent legacy `scans_*` tables; those were renamed in 2.0).

Also check `/status/` (staff-only) shows the backup freshness check green (it WARNs
when the newest successful backup is older than 26h), and `/healthz` returns 200.

### 3. Large export streaming under gunicorn (Phase 0)

The suite runs under the dev server; confirm a **large** export download streams
correctly under gunicorn (the production server). With the stack up
(`RUN_DEV_SERVER` unset, so gunicorn is serving), trigger an export of a big patient
in the UI and download the share link — confirm the full file arrives, not truncated,
and memory stays flat (`docker stats yggdrasil-web-$DOCKER_SUFFIX` during the
download).

### 4. Real-runner callback (Phase 0 / risk #1)

The external-runner HTTP contract is frozen and token-authed at `/api/runner/...`.
With a real runner pointed at the new stack (`RUNNER_API_TOKENS` matching), submit a
job and confirm the runner can **claim → complete/fail** it and the patient's
processing status updates. (Brain's old per-domain unauthenticated runner routes were
removed in Phase 8 — runners must use the single global contract.)

### 5. Cut the release

Once 1–4 pass: set the `[2.0.0]` date in [CHANGELOG.md](../CHANGELOG.md) (currently
`TBD`), fold in `[Unreleased]`, then tag:

```bash
git tag -a v2.0.0 -m "Yggdrasil 2.0.0"
git push origin v2.0.0   # fires .github/workflows/release.yml
```

---

## Rollback

The old 1.9 VM is untouched by this procedure (you only *read* its DB in Step 1), so
rollback is: point DNS / the reverse proxy back at the 1.9 VM. Keep the 1.9 VM running
until the 2.0 instance is verified in production. The `v1.9.0` tag is the code
rollback reference.
