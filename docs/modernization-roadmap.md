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
| 2 | Automated backup + status dashboard | ✅ done (`release/2.0`) |
| 3 | Export share expiry | ✅ done (`release/2.0`) |
| 4 | Admin-driven worker/modality config | ✅ done (`release/2.0`) |
| 5 | `common/` consolidation | ✅ done (`release/2.0`, PRs 5.1/5.2/5.3) |
| 6 | Branding, landing, favicons, footer | ✅ done (`release/2.0`, PRs 6.1/6.2) |
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

## Phase 4 — Admin-driven worker/modality config — DONE

Shipped on `release/2.0`. New `common.ModalityProcessingConfig` (OneToOne → `common.Modality`), migrations `common/0032` (create) + `0033` (seed). Central accessors in `common/modality_config.py` (`modality_requires_processing`, `modality_is_enabled`, `queue_override_for`, `modality_is_blocking`, `dependent_slugs_of`) — every reader falls back to legacy hardcoded/env behavior when no row exists. Wired into `maxillo/file_utils.py` (requires_processing branch), `common/job_routing.py` (`is_runner_enabled_for_modality` delegates to config; `select_runner_queue` DB `queue_name` beats ALL env), and `_processing_status` in `maxillo/models.py` + `brain/models.py` (`is_blocking` gates the 'processing' display; mirrored edit — unify in Phase 5). PR 4.2: generalized ios→bite via `create_dependent_jobs` + `depends_on` M2M (legacy fallback preserves ios→bite), collapsed the duplicated single-file upload blocks in `maxillo/api_views/projects.py` + `maxillo/views/patient_upload.py`. Admin: editable `ModalityProcessingConfigAdmin` in `common/admin.py` + `StackedInline` on `ModalityAdmin`. Decisions: DB queue wins over all env; `is_blocking` = in-flight job shows 'processing'. Runner HTTP contract untouched. Verified: 160-test suite green (MySQL 8 + Redis 7 Docker), `makemigrations --check` clean, seed rehearsed on pre-existing modalities (7 modalities → 7 configs, panoramic non-processing/non-blocking, ios processing/blocking). **Still owed (needs prod-like env): risk-item-0 rehearsal — restore the actual v1.9.0 mysqldump → migrate 0032/0033 → suite green.**

Original design notes: New `ModalityProcessingConfig` (OneToOne → `common.Modality`); absent row = legacy fallback (zero-risk rollout).
- Fields: `requires_processing` (replaces hardcoded `no_processing_modalities` at `maxillo/file_utils.py:368`), `queue_name` (DB > `RUNNER_QUEUE_BY_*` env > default in `common/job_routing.py`), `is_blocking` (drives `Patient._processing_status` gating, `maxillo/models.py:262-289` + brain mirror), `depends_on` M2M (feeds existing `Job.dependencies` machinery — see `create_bite_classification_job`), `is_enabled` (absorbs `is_runner_enabled_for_modality`).
- Data migration seeds rows from the hardcoded list + env JSON + ios→bite dependency ⇒ behavior identical on deploy day.
- PR 4.2 replaces per-slug upload wiring (`maxillo/api_views/projects.py:220-254`, `maxillo/views/patient_upload.py:113-156`) with a Modality-driven loop. Runner HTTP contract (`maxillo/runner_api_service.py`, `maxillo/api_views/runner.py`) untouched.

## Phase 5 — common/ consolidation (biggest; requires Phases 1+2)

Abstract base models in `common/`, concrete per-domain subclasses pinning existing `db_table` names — **zero data migration**; NOT shared tables.
- PR 5.1 — DONE: promoted maxillo private helpers to public `common/uploads.py` (8 upload helpers: `get_patient`, `domain_for_patient`, `entity_fk_kwargs`, `project_slug_from_patient`, `raw_key_prefix_for`, `processed_key_prefix_for`, `sanitize_relpath`, `upload_uploaded_file_to_storage`) and `common/export_processing.py` (whole engine moved from `maxillo/utils/export_processor.py` — `ExportProcessor`, `start_export_processing`, `build_patient_classification_blob` — plus the 6 shared view helpers `coerce_bool`, `resolve_content_selection`, `build_shared_download_url`, `recover_stuck_export`, `kill_export_processes`, `format_file_size`). `maxillo.file_utils`/`maxillo.views.export` keep back-compat aliases to old private names (minimal internal diff); `laparoscopy/file_utils.py` and `brain/views.py` import the public names from `common`; `run_export.py` imports the engine from `common`. `_domain_models` relative `..models` imports rewritten absolute (`maxillo.models`); `build_shared_download_url` inlines the namespace read (no `common`→maxillo dep). All private cross-imports killed. Verified: `makemigrations --check` clean (no model changes), 160-test suite green (MySQL 8 + Redis 7 Docker), import smoke + per-domain `ExportProcessor._domain_models` resolution. Kept out of scope: brain's own divergent upload helper copy, the public `save_video_to_dataset`/`LaparoscopyExportProcessor` cross-imports, `laparoscopy/export_processor.py`.
- PR 5.2 — DONE: `common/domains.py` registry is the single source for `DOMAIN_CHOICES`/`DOMAINS`/`DEFAULT_DOMAIN`/`DOMAIN_FK_FIELDS` (+ `normalize_domain`/`fk_fields_for`). `common/models.py` drops the 3 duplicated inline `DOMAIN_CHOICES` and adds `DomainFKAccessorMixin` (registry-driven `get_patient`/`set_patient`/`get_voice_caption`/`set_voice_caption` over the parallel per-domain FK columns — methods only, no migration). `permissions.py`, `presence.py`, `job_routing.py`, `uploads.py` all route through the registry (no per-domain if/elif). Commit `2670c8a`.
- PR 5.3 — DONE: `common/base_models.py` with `ActivePatientManager` + abstract `VoiceCaptionBase`, `DatasetBase`, `FolderBase`, `FolderAccessBase`, `TagBase`, `ExportBase` (incl. expiry + `mark_*`/`ensure_share_token`), `ClassificationBase` (maxillo+laparoscopy). Diffed the three copies first: only byte-identical fields lifted; per-app drift (`related_name`, `db_table`, `help_text`, `default`) stays overridden on subclasses; all shared **methods/properties** lifted (registry-driven where domain-specific, e.g. `files`/`processing_jobs`). This fixed maxillo's `VoiceCaption` which had lacked the explicit `files`/`processing_jobs`/`__str__` that brain/laparoscopy had — all three now share brain's complete method set. `Patient` intentionally NOT base-extracted (most domain-specific model — divergent fields + helper methods; only its `ActivePatientManager` was shared and is now in common). **Acceptance gate met: `makemigrations --check --dry-run` produces nothing** (zero data migration; `db_table`s pinned, tables untouched). `docs/new-project-type.md` rewritten for the registry + base-model workflow. 160-test suite green.
- Shipped 5.1 → 5.2 → 5.3 in sequence. **Still owed (needs prod-like env): rehearse `migrate --plan` on a prod clone restored from a Phase 2 backup — 5.x adds no migrations, but confirm the restored 1.9 dump still `migrate`s clean and the suite is green (risk-item-0).**

## Phase 6 — Branding — DONE

Shipped on `release/2.0` (PRs 6.1 `07aa016`, 6.2 `5fb28d7`).
- PR 6.1: original hand-drawn Yggdrasil world-tree master `static/icons/ygg-logo.svg` + `favicon.svg`; raster set (`favicon-16/32.png`, `apple-touch-icon.png` 180, `favicon.ico`) derived by `scripts/make_icons.sh` (rsvg/ImageMagick, or cairosvg+Pillow fallback; `git add -f` since `scripts/` is gitignored). `<link>`s wired in `base.html` `<head>`. Footer rewritten: dropped author credit, kept GitHub + `v{{ app_version }}`, added "Yggdrasil 2.0 is out" badge.
- PR 6.2: self-hosted Cinzel (display) + Inter (body) woff2 under `static/fonts/` (fontsource @5, latin subset, OFL licenses committed — no Google CDN). New sitewide `static/css/theme.css` (loaded in `base.html` after Bootstrap): `@font-face`, base type, navbar-brand/heading display font, footer polish — **scope-guarded away from viewers** (no image_viewer/intraoral/viewer_grid/patient_detail selectors). Landing (`common/landing.html` + `landing.css`): Cinzel title, tree-SVG logo swap, Yggdrasil name-explanation block, runes easter-egg kept.

**Phase 7 hook**: the landing "public demo" CTA is already in `common/landing.html`, gated on a `demo_url` context var (hidden until set). Phase 7 just needs its landing views (`maxillo/views/patient_list.py`, `brain/views.py`, laparoscopy) to pass `demo_url` (e.g. `reverse('demo_index')`).

Original plan:
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
