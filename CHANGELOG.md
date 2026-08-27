# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`annotations`, a new Django app: Yggdrasil's durable annotation model.** Thirteen
  models behind a strict layering — `validators/` is pure (values in, `ValidationError`
  out, no database), `adapters/` is pure translation, and `services/` is the only writer.
  No Cornerstone runtime identifier is ever persisted: `SourceResource.identity_key` is
  Yggdrasil's own durable name for annotatable content. Nothing is wired to a view yet;
  the viewers are replaced one surface at a time in later phases.
  - Three invariants are enforced in DDL because they fail invisibly:
    `UniqueConstraint(annotation_set, revision_number)` *is* the optimistic-concurrency
    primitive (a stale writer gets an `IntegrityError` → 409, with no read-then-write
    window because the check is the write); "exactly one primary target" and "exactly one
    canonical payload" use nullable slot columns rather than conditional constraints,
    which MySQL compiles to nothing with no error; and a millimetre measurement requires
    `is_calibrated`, so a length taken on an uncalibrated photograph is reported in
    pixels rather than as a physical claim the image cannot support.
  - Coordinate frames are named on every selector and every geometry row, with LPS and
    RAS as separate values — they differ by two sign flips, so conflating them mirrors a
    landmark across two planes with nothing in the record to say so. IOS landmarks get
    `resource_local`, because they come from `worldToLocal` against a mesh and have no
    patient frame.
  - Times are integer milliseconds throughout.
- Four conversion commands. `annotations_convert_legacy` converts the MySQL-resident
  legacy annotations (idempotent, resumable, `--dry-run`);
  `annotations_materialize_landmarks` handles the one surface that lives in object
  storage; `annotations_normalize_coordinates` records each volume's grid facts from its
  NIfTI header and counts the volumes whose orientation is inferred rather than read;
  `annotations_crosscheck` is read-only and exits non-zero on any legacy row without a
  converted counterpart, or any resource whose bytes changed after being annotated.
- Frontend build toolchain for the Cornerstone3D v5 migration: npm + esbuild,
  **dev-only**, with the emitted bundle committed under
  `static/vendor/cornerstone/<build>/`. Deploys need no Node and make no network
  request. `scripts/build_frontend.sh` builds it, `scripts/check_bundle_assets.mjs`
  (`npm run verify`) asserts every web-worker and wasm URL resolves against its own
  emitting file, and a new CI job rebuilds and fails on any diff. `{% cornerstone_entry 'volume-grid' %}` loads a
  surface. Five per-surface entries exist; **no template loads one yet** — the viewers
  are replaced one at a time in later phases.
- `api_serve_file_named`: `…/processing/files/serve/<id>/<filename>` beside the existing
  route, in all four namespaces. Cornerstone's NIfTI loader decides whether to gunzip by
  testing the URL *pathname* for a `.gz` suffix, which a query parameter cannot carry.
  Same view, same ACL; the filename segment never takes part in resolving the file.

### Changed
- **CBCT annotation is a mode now, and it is off by default.** One switch reading
  `Annotations on` / `Annotations off` replaces the eye button: turning it on reveals the
  measurement tools *and* shows the measurements, turning it off hides both and puts the
  crosshair back on the left mouse button. A study being read shows six fewer controls
  than one being measured, and there is no longer a pair of states (mode vs. visibility)
  that could disagree. The state lives in the DOM (`aria-checked`) and is read back at
  click time, so the switch cannot invert.
- A trash button beside save clears every measurement drawn on the study. It asks first,
  and it clears the *viewer*: the server replaces the whole set on save, so the next save
  is what makes a clear permanent and a reload is what undoes it. Both the confirmation
  and the notification say so.
- A saved measurement set is confirmed by the platform's green toast
  (`window.appNotify`) instead of "Saved 3 measurements." in the toolbar; a failed save
  gets a red one rather than arriving in the same place as a success. The toolbar's status
  line is now only for failures that are about the toolbar itself.
- **Third-party CDNs are allowed.** The blanket no-CDN rule is withdrawn: a CDN serves a
  static asset faster than this deployment can and takes the bandwidth off it, and
  `templates/base.html` had been loading Three.js, an STL loader, trackball controls and
  fflate from three of them the whole time. `scripts/build_frontend.mjs` and
  `scripts/check_bundle_assets.mjs` now *note* a CDN reference in the emitted bundle
  instead of failing the build on it. Two narrower rules survive and are unaffected:
  webfonts stay self-hosted (a font CDN sees every page view of every visitor — a GDPR
  question a library does not raise), and the itk-wasm pipelines stay vendored and aliased
  because their ABI is pinned to the package version. `docs/cornerstone-future-work.md`
  §9 is withdrawn accordingly.
- **The raw-data lock reads `AnnotationSet.ever_annotated`.** `common/annotation_lock.py`
  keeps its module path and all five public signatures byte-identical, and gains
  `annotations` as its first source: one indexed query instead of up to five per-domain
  existence checks. The lock is now **monotonic** — deleting annotation work no longer
  thaws the scan it was drawn on. Machine output still never locks a case, but the rule
  now lives in the data (a prediction revision does not set the flag) rather than in
  hardcoded exemptions. The legacy per-domain checks are retained alongside the new
  source for one release as a cross-check, and go when those tables are dropped.
- The Cornerstone bundle derives modality rescale (HU) from the raw NIfTI header itself
  rather than trusting `@cornerstonejs/nifti-volume-loader`, whose
  `modalityScaleNifti` skips the rescale whenever either factor is already neutral — so
  the ordinary `scl_slope=1, scl_inter=-1024` CBCT encoding would have been silently off
  by 1024 HU.

### Fixed
- **Restored measurements were not drawn until a tool button was clicked.** The switch
  showed them and nothing appeared; switching it off and on again "fixed" it only
  because a tool button had been clicked in between. `ToolGroup.addTool` instantiates a
  tool but writes no `toolOptions` entry, and a tool with no mode is skipped by
  `getToolsWithModesForElement` — so the annotation rendering engine never asked
  `LengthTool` to draw, whatever the annotations' visibility said. Clicking any
  measurement button gave every tool a mode as a side effect of `setPrimaryTool`
  passiving its neighbours, which is what made it look intermittent. Switching the mode
  on now puts every measurement tool in `Passive` deliberately (and `Disabled` on the way
  out), pinned by a test that needs no GPU.
- **Measurements that could not be made visible again.** Two independent bugs, both
  reported as "I switch annotations on and nothing appears":
  - Restoring a study wrote the stored `isVisible` flag back. An annotation saved while
    the measurements were hidden therefore came back invisible *and unreachable* —
    Cornerstone's `setAnnotationVisibility(uid, true)` only clears the flag for a UID in
    its own hidden set, which a freshly added annotation is never in, so no amount of
    toggling could show it. Visibility is session state now, not part of the record.
  - Hiding "all annotations" hid the crosshair too. `getAllAnnotations()` returns the
    state tools keep for themselves, and the navigation reticle is one of those, so
    switching measurements off took the reticle with it. The hide now uses the same
    measurement filter a save uses, and writes the flag as well as the hidden set, which
    makes it idempotent.
- **Two annotation gates that were missing.** `update_nifti_metadata` rewrote a raw
  CBCT's qform/sform in place and restamped `FileRegistry.file_hash` without consulting
  the annotation lock — every landmark, spline and polygon already drawn on that volume
  kept its coordinates while the volume moved in patient space, with nothing in the
  record to say so. It now refuses with 409 before any object-storage work.
  `update_classification` was the one annotation write that never called
  `project_allows_annotation`, so a project with occlusion classification switched off
  still accepted instant updates from the sidebar.

### Removed
- ~3,700 lines of dead viewer code: `volume_viewer.js` (which also shadowed the live
  `window.CBCTViewer` with a duplicate), `slice_renderer.js`, `volume_interaction.js`,
  `windowing.js`, `maxillo_niivue_viewer.js`, `nifti-reader-min.js`, and
  `volume_renderer.js` with `static/shaders/volume_fragment.glsl` — the latter loaded on
  every patient-detail page and never called.
- The legacy CBCT volume preload (`volume_loader.js`, `worker/volume_worker.js` and the
  `useLegacyVolumePreload` branch in `patient_detail.js`). Unreachable for maxillo and
  brain by code path, and confirmed unreachable for laparoscopy against production: no
  laparoscopy patient owns a `cbct_raw` file.

## [2.0.0] - 2026-08-26

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
- The Django/Celery project package and runtime entrypoints are now named
  `yggdrasil` instead of the pre-2.0 project name; Django app labels and tables
  are unchanged so the v1.9 dump-restore migration path remains additive-only.
- The SLURM runner worker is intentionally off `app-net-$DOCKER_SUFFIX` and uses
  externally routable Redis/API URLs from `.env.worker`.

### Security
- File serving is now authorized against the file's own domain. `serve_file`
  resolved the patient with an `if laparoscopy / else .patient` branch (so a
  brain row consulted the maxillo FK) and then authorized **every** domain
  against a hardcoded `Project.objects.filter(slug='maxillo')`, passing the
  literal `'maxillo'` into `user_is_project_admin`/`user_can_read_folder`.
  Both directions were wrong: a maxillo member could read brain and
  laparoscopy files, and a laparoscopy-only member was refused their own.
  The namespaced routes were shielded by `ActiveProfileMiddleware`, but the
  global `/api/processing/files/serve/<id>/` route (used by the file-management
  UI) skips that middleware, so the view's own check was the only gate there.
  Authorization now runs through one helper, `common.file_access
  .authorize_file_read`, which resolves the patient via the domain registry and
  defers to `patient.project`. `brain.api_views.serve_file` uses it too and
  thereby gains the `raw_file_hidden` backstop it previously lacked.
  **Behavior change**: maxillo project admins no longer receive brain or
  laparoscopy files. Audit `ProjectAccess` rows across domains after deploying
  and grant per-domain access where it is genuinely intended.
- Patient-viewer and activity-stats pages now inject server data via Django's
  `json_script` filter instead of `|safe` JSON interpolation, removing a
  script-breakout XSS vector. The rendered `<script type="application/json">`
  elements keep their ids, so viewer JavaScript is unchanged.
- Removed `csrf_exempt` from all session-authenticated state-changing views
  (classification updates, patient tags, laparoscopy Magic Tool worker
  proxies, and the external project API). **Breaking for external API
  clients**: POSTs to `/api/<project>/upload/` and `/api/<project>/patients/`
  must now send the `csrftoken` cookie value in an `X-CSRFToken` header
  (standard `requests.Session` flow); unauthenticated POSTs without a token
  answer 403 instead of 401. The token-authenticated runner API under
  `/api/runner/...` is unchanged.
- Runner bearer-token comparison now uses `hmac.compare_digest` (constant
  time); same request/response contract.
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
