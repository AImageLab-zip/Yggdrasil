# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - TBD

### Added
- Folder-level access control for laparoscopy (`laparoscopy.FolderAccess`, mirroring brain).
  A data migration grants access on all laparoscopy folders to existing project members,
  preserving current visibility while making it revocable.
- `VERSION` file exposed as `settings.APP_VERSION` and shown in the site footer.
- This changelog.
- CI on self-hosted GitHub Actions runners (ruff + full suite against MySQL/Redis,
  `makemigrations --check` gate) and a release workflow on `v*` tags.
- Contract tests freezing the external runner HTTP API (claim/complete/fail).
- One-command local development bootstrap (`scripts/dev_bootstrap.sh`):
  standalone `docker-compose.dev.yml` with MySQL, Redis and a single-node
  Garage for object storage, plus an idempotent `manage.py seed_dev`.
- Automated daily database backups (03:00 UTC): celery beat + a dedicated
  `maintenance`-queue worker dump, verify and upload to object storage under
  `backups/mysql/`, with daily/weekly retention pruning and a
  `manage.py backup_now` wrapper. Settings refuse to start if the maintenance
  queue collides with a runner queue.
- `SystemCheck` model recording maintenance runs; staff-only `/status/` page
  (DB, object storage, backup freshness — warns when the newest successful
  backup is older than 26h) and unauthenticated `/healthz` (200/503, no
  details).
- Export share links can now expire: nullable `expires_at` on all three
  Export models (null = never, so pre-2.0 links are unaffected). New and
  updated shares default to 30 days; the share UI offers 7/30/90 days or
  never, where "never" is allowed only for staff/project admins. Expired
  links answer 410 Gone on both the landing page and the download.
- Yggdrasil world-tree branding: an original SVG logo and a full favicon set
  (`static/icons/`, regenerable via `scripts/make_icons.sh`) wired into every
  page. The footer drops the original single-tenant author credit, keeps the
  GitHub link and version, and flags "Yggdrasil 2.0 is out".
- Self-hosted webfonts (Cinzel for display, Inter for body; OFL, under
  `static/fonts/`) and a sitewide `theme.css` — no Google Fonts CDN, so no
  third-party request (GDPR). Annotation viewers are deliberately left
  unrestyled. The landing page gains a Cinzel title, the world-tree logo, an
  explanation of the Yggdrasil name, and a demo call-to-action that stays
  hidden until a `demo_url` is provided (Phase 7).
- Public guest demo (Phase 7): an anonymous, read-only, no-login window at
  `/demo/` onto curated folders flagged `is_demo=True` (new `FolderBase`
  field, editable per folder in the admin). GET/HEAD only, per-IP rate
  limited; a self-contained set of views/templates that never touch the
  authenticated app or its `@login_required` file endpoints and can only
  reach a patient (and its files) that lives in a demo folder. The landing
  "Explore the public demo" CTA appears only once at least one demo folder
  exists. Only anonymized or synthetic studies may be flagged.
- `manage.py resubmit_jobs --domain <d> --modality <slug>`: bulk-create pending
  processing jobs for existing patients that have the modality's raw file but no
  job yet (e.g. after shipping a new algorithm for a modality), reusing the
  normal enqueue signal. `--include-existing` also re-pends patients that
  already have a job; `--folder-id`, `--limit`, `--dry-run` supported.

### Changed
- The web container now serves with gunicorn and runs `migrate` on start
  (`AUTO_MIGRATE=0` to opt out, `RUN_DEV_SERVER=1` for the dev server).

### Security
- Brain processing API was fully anonymous: `serve_file` let anyone fetch any
  brain file by id, and `get_file_registry`/`get_job_status`/the job list
  leaked file paths, patient ids and job state. These now require login (file
  serving enforces the brain folder ACL; the monitoring endpoints are
  staff-only). The unauthenticated per-domain runner callbacks
  (`runner_claim/complete/fail`) that also mutated job state were removed —
  external runners use the single token-authenticated contract under
  `/api/runner/...` (domain-agnostic, unchanged).

### Fixed
- `common.uploads.domain_for_patient()` returned `maxillo` for brain patients
  (anything that wasn't laparoscopy), so registry-driven Job/FileRegistry FK
  helpers misfiled brain entities; it now maps every domain app label correctly.
- `common.permissions._namespace()` resolved every request-object call to `maxillo`,
  so brain and laparoscopy permission checks ran against the wrong domain.
- Folder permission edits from brain pages silently wrote maxillo `FolderAccess` rows;
  folder-tag views now resolve the model per namespace.
- `user_can_view_caption_content` ignored brain's `folders` M2M relation.
- `patient_volume_data` always returned 500: a dead filter block referenced an
  undefined `domain` variable and its NameError short-circuited the endpoint.
- `/admin/control-panel/` was reachable without authentication; it now
  requires a staff login like its sibling admin views.

## [1.9.0]

Last pre-modernization production state (tag `v1.9.0`, commit `52d1557`).
No changelog was kept before this point.
