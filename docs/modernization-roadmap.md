# Yggdrasil 2.0 Modernization — Roadmap & Status

Working branch: `release/2.0`. Each phase ships as 1–3 PRs, independently deployable.
All migrations must be additive: no table renames, no destructive schema changes on the production DB.

Decisions already made with the maintainer:
- One phased roadmap covering all requested items.
- Daily mysqldump → existing Garage object storage (retention + status page).
- CI on **self-hosted** GitHub Actions runners.
- Public guest view = anonymous read-only demo of curated folders (no login).
- Laparoscopy open-folder behavior was a bug, not intentional.
- gunicorn/prod hardening in scope.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Prod hardening + ACL fixes | ✅ done (`release/2.0`, commits `4ee1cc6`, `61a23f0`) |
| 1 | Versioning, CI, test baseline, dev bootstrap | ✅ done (`release/2.0`, tag `v1.9.0`, commits `5373b93`, `23d72e6`, `e9a60d3` + bootstrap commit) |
| 2 | Automated backup + status dashboard | ⬜ next |
| 3 | Export share expiry | ⬜ |
| 4 | Admin-driven worker/modality config | ⬜ |
| 5 | `common/` consolidation | ⬜ |
| 6 | Branding, landing, favicons, footer | ⬜ |
| 7 | Public guest demo | ⬜ |

## Phase 0 — DONE

**Commit `4ee1cc6` — ACL fixes:**
- `common/permissions.py:15` `_namespace()` returned the wrong variable, so every request-object call resolved to `maxillo`; brain/laparoscopy permission checks ran against the wrong domain. Fixed.
- Laparoscopy had no `FolderAccess` model; every project member saw all folders/patients. Added `laparoscopy.FolderAccess` (mirrors brain), removed the unfiltered special-cases in `filter_folders_for_user`/`filter_patients_for_user`.
- Migration `laparoscopy/0006` grants `standard` FolderAccess on all laparoscopy folders to existing laparoscopy project members — current effective visibility is preserved and becomes revocable. **After deploy: review laparoscopy folder grants in admin and revoke what shouldn't be there.**
- `maxillo/views/folders_tags.py` hardcoded maxillo's FolderAccess for all domains (brain folder-permission edits silently wrote maxillo rows with the same folder id). Now resolves per-namespace.
- `user_can_view_caption_content` handled only a `folder` FK; brain patients use a `folders` M2M. Now checks all patient folders.
- New tests: `common/tests.py` (namespace resolution), `laparoscopy/tests.py::LaparoscopyFolderAccessTests` (6 ACL tests), fixed stale `brain/tests_permissions.py`.
- Moved root `test_external_api.py` (manual script importing `requests`, broke test discovery) to `scripts/manual_test_external_api.py`.

**Commit `61a23f0` — prod server:**
- `entrypoint.sh`: `collectstatic` → `migrate --noinput` (opt out `AUTO_MIGRATE=0`) → gunicorn (`GUNICORN_WORKERS`/`GUNICORN_TIMEOUT`); `RUN_DEV_SERVER=1` keeps the dev server. `gunicorn==23.0.0` pinned; `docs/setup.md` + `.env.example` updated.

**Verification done:** full Django suite green (40 tests, MySQL 8 + Redis 7 in Docker); container boots under gunicorn, auto-migrates (incl. new laparoscopy migrations), serves HTTP 200.
**Verification still owed (needs prod-like env):** large export download streaming under gunicorn; runner claim/complete callbacks with a real runner.

How the suite was run (no compose stack needed):

```bash
docker network create ygg-test-net
docker run -d --rm --name ygg-test-db --network ygg-test-net \
  -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=ygg mysql:8.0
docker run -d --rm --name redis --network ygg-test-net \
  redis:7-alpine redis-server --requirepass testpass
docker build -t ygg-test .
docker run --rm --network ygg-test-net --env-file <test.env> \
  -v "$PWD":/app -u $(id -u):$(id -g) ygg-test python manage.py test
```

`<test.env>` needs: `SECRET_KEY`, `DB_NAME=ygg DB_USER=root DB_PASSWORD=testpass DB_HOST=ygg-test-db`, `EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend` + dummy `EMAIL_*`, `REDIS_PASSWORD=testpass`. (Phase 1 turns this into CI + a committed env template.)

## Phase 1 — Versioning, CI, tests, dev bootstrap (NEXT)

- **PR 1.1**: plain `VERSION` file (`2.0.0`) read into `settings.APP_VERSION`, exposed via existing `common/context_processors.py`, shown in `templates/base.html` footer; `CHANGELOG.md` (Keep-a-Changelog); tag current prod HEAD `v1.x` as rollback ref before anything else lands.
- **PR 1.2**: `.github/workflows/ci.yml` on self-hosted runners — MySQL 8 + Redis 7 services, `migrate` then `test`; permissive `ruff check` (codebase mixes tabs/spaces — no formatting enforcement); baseline tests: runner claim/complete/fail API contract (external runners must never break), `select_runner_queue` routing, Job post_save enqueue (mock `send_task`), smoke test per app. `release.yml` on `v*` tags. Optional: pip-tools lockfile.
- **PR 1.3**: `common/management/commands/seed_dev.py` (superuser, 3 projects, existing modality commands, demo folder/patient per domain; refuses if `DEBUG=False`); `scripts/dev_bootstrap.sh`; optional local MinIO/Garage in a `docker-compose.dev.yml` override (object storage is the main local-dev blocker — it's fully external today); `CONTRIBUTING.md`. NOTE: `scripts/` is in `.gitignore` (line 196) — force-add or move bootstrap elsewhere.

## Phase 2 — Backup + status dashboard

Celery beat + local `maintenance`-queue worker (new compose services sharing the web image). **The `maintenance` queue must never overlap `RUNNER_DEFAULT_QUEUE`/`RUNNER_QUEUE_BY_*` in prod `.env` — external runners must not consume it.**
- PR 2.1: `default-mysql-client` in Dockerfile; `common/tasks.py::backup_database()` — `mysqldump --single-transaction` → gzip + integrity check (reuse `scripts/backup_prod.sh` logic) → upload via `common/object_storage.py` to `backups/mysql/`; prune `BACKUP_KEEP_DAILY=14`/`BACKUP_KEEP_WEEKLY=8`; beat schedule daily 03:00; `manage.py backup_now` wrapper.
- PR 2.2: `common.SystemCheck` model (name/status/ran_at/duration_ms/details JSON, one additive migration); staff-only `/status/` page extending the health checks already in `common/views.py` (`_database_health`, `_object_storage_health`); backup WARN if newest OK >26h; read-only admin; unauthenticated `/healthz` (200/503, no details).
- Verify: restore a produced dump into scratch MySQL.

## Phase 3 — Export share expiry

- Nullable `expires_at` on the three Export models (maxillo `~:756` next to `shared_at`, brain, laparoscopy); null = never (current behavior, no backfill).
- Enforce in `_shared_export_availability` (`maxillo/views/export.py:119`) → 410 page; brain/laparoscopy reuse these helpers — verify all three paths.
- Share modal + `export_share_update` (:776): presets 7/30/90d, custom, "never" — **"never" server-side allowed only for staff/project-admins**; default 30 days.

## Phase 4 — Admin-driven worker/modality config

New `ModalityProcessingConfig` (OneToOne → `common.Modality`); absent row = legacy fallback (zero-risk rollout).
- Fields: `requires_processing` (replaces hardcoded `no_processing_modalities` at `maxillo/file_utils.py:368`), `queue_name` (DB > `RUNNER_QUEUE_BY_*` env > default in `common/job_routing.py`), `is_blocking` (drives `Patient._processing_status` gating, `maxillo/models.py:262-289` + brain mirror), `depends_on` M2M (feeds existing `Job.dependencies` machinery — see `create_bite_classification_job`), `is_enabled` (absorbs `is_runner_enabled_for_modality`).
- Data migration seeds rows from the hardcoded list + env JSON + ios→bite dependency ⇒ behavior identical on deploy day.
- PR 4.2 replaces per-slug upload wiring (`maxillo/api_views/projects.py:220-254`, `maxillo/views/patient_upload.py:113-156`) with a Modality-driven loop. Runner HTTP contract (`maxillo/runner_api_service.py`, `maxillo/api_views/runner.py`) untouched.

## Phase 5 — common/ consolidation (biggest; requires Phases 1+2)

Abstract base models in `common/`, concrete per-domain subclasses pinning existing `db_table` names — **zero data migration**; NOT shared tables.
- PR 5.1: promote maxillo private helpers to public `common/uploads.py` + `common/export_processing.py`; update `laparoscopy/file_utils.py:7-11`, `brain/views.py:32-40`, `maxillo/management/commands/run_export.py`. Kills all private cross-imports.
- PR 5.2: `common/domains.py` registry (single `DOMAIN_CHOICES` source); registry-driven `permissions.py` + `job_routing.py`; keep Job/FileRegistry per-domain FK columns, wrap in `get_patient()`/`set_patient()` accessors.
- PR 5.3: `common/base_models.py` (PatientBase, FolderBase, FolderAccessBase, DatasetBase, TagBase, VoiceCaptionBase, ExportBase incl. expiry, ClassificationBase, ActivePatientManager); diff the three copies first — drift stays on subclasses; keep `related_name`s identical. **Acceptance gate: `makemigrations --check --dry-run` produces nothing.** Rewrite `docs/new-project-type.md`.
- Ship 5.1 → observe a week → 5.2 → 5.3. Rehearse `migrate --plan` on a prod clone restored from a Phase 2 backup.

## Phase 6 — Branding

- PR 6.1: Yggdrasil world-tree favicon/logo set in `static/icons/` + `<link>`s in `base.html`; footer (`base.html:225`): drop "developed by Luca Lumetti", keep GitHub link, add `v{{ app_version }}` + "Yggdrasil 2.0 is out" note.
- PR 6.2: self-hosted webfont (GDPR — no Google CDN), `static/css/theme.css` polish **scoped away from annotation viewers** (`static/js/` and patient-detail partials untouched — annotators keep their known UI); expand `templates/common/landing.html` (runes theme exists) with name explanation + domain cards + demo link.

## Phase 7 — Public guest demo

Explicit `is_demo` flag on folders + anonymous `/demo/<domain>/` namespace (GET/HEAD only), NOT a synthetic guest login.
- `@demo_view` decorator; querysets start from `Folder.objects.filter(is_demo=True)`; reuse patient templates with `demo_mode=True` hiding write controls; authed API endpoints stay `@login_required`; anonymous branch in `common/file_access.py` strictly for demo-folder assets; per-IP rate limit.
- **Demo folders must contain only anonymized/synthetic studies** — checklist item in the PR.

## Risk register

0. **CRUCIAL — v1.9 dump must restore into a fresh 2.0 VM.** The production upgrade path is: mysqldump the 1.x VM → restore onto a brand-new VM → start 2.0 (auto-migrate). Tag `v1.9.0` (commit `52d1557`) marks that schema. Never edit/squash migrations existing at that tag; every later migration must be additive and apply cleanly on a restored 1.9 dump. Rehearse before each risky phase: restore 1.9 dump into scratch MySQL → `manage.py migrate` → suite green.
1. Runner HTTP contract frozen (claim/complete/fail + token auth) — contract test lands in Phase 1.
2. Auto-migrate on deploy (Phase 0) — guarded by CI migrations (Phase 1) and daily backups (Phase 2) before the riskier migrations (Phases 3–5).
3. `maintenance` queue collision with runner queues — check prod `.env` before Phase 2 deploy.
4. Phase 5 gated on the zero-SQL migration check + prod-clone rehearsal.
5. Post-deploy of Phase 0: audit auto-granted laparoscopy FolderAccess rows; also note `_namespace()` fix means maxillo-only admins no longer get admin power on brain/laparoscopy pages (correct, but a visible behavior change).
