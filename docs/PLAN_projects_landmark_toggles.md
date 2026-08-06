# Plan: IOS landmark visibility toggles + Project/Folder architecture

Date: 2026-08-06
Status: draft — pending review

Two work items:

1. **Small**: IOS landmark *visibility* toggles, split from the annotation tool.
2. **Large**: Projects become first-class containers above folders (upload
   gating, processing gating, annotation-method gating, ACL migration).

---

## 1. IOS landmark visibility toggles (maxillo patient detail)

### Current behavior

- `templates/maxillo/patient_detail_content.html` → toolbar group
  `#iosLandmarkControls` has ONE button `#toggleLandmarkMode`.
- `static/js/modality_viewers/ios.js`: `landmarkState1.active` gates **both**
  marker rendering (`renderLandmarks()` returns early when `!active`) and the
  workbench panel (`#iosLandmarkWorkbench`).
- Markers: per-type colored spheres; types
  `incisal outer bracket gingival mesial distal inner facial cusps planar`
  (`landmarkTypes1`), colors in `landmarkColors1`. `planar` is read-only
  (prediction).
- `syncLandmarkVisibility()` already hides markers of a hidden jaw (upper/lower
  mesh toggle).
- Workbench Display section already has size / 3D-axes / white-background
  toggles — natural home for per-type toggles.

### Changes

**Toolbar split (patient_detail_content.html + ios.js)**

```
#iosLandmarkControls
  [Landmarks label]
  [eye button #toggleLandmarkVisibility]   <- NEW: show/hide saved landmarks (view only)
  [location-dot #toggleLandmarkMode]       <- existing: open annotation workbench
  [chevron dropdown: per-type checkboxes]  <- NEW: quick visibility filter (popover)
```

- New state in `landmarkState1`:
  - `showLandmarks: false` — markers visible without entering annotation mode.
  - `visibleTypes: {incisal: true, outer: true, ... planar: true}` — all true
    by default (matches "all enabled by default when entering the landmark
    annotations view").
- `renderLandmarks()`: render markers when `showLandmarks || active` (currently
  only when `active`).
- `syncLandmarkVisibility()`: also hide markers whose `type` is not in
  `visibleTypes[type]`; AND when neither `showLandmarks` nor `active`, hide all.
- Eye button `aria-pressed` mirrors `showLandmarks`; no workbench interaction.
- Annotation button unchanged (`toggleLandmarkMode` toggles `active` +
  workbench). Opening the workbench does NOT reset visibility toggles (user's
  per-type choices persist for the page session; initial default is all on).
- Per-type toggles in **two places** (same handler, `data-landmark-type`):
  1. Workbench Display section: "Landmark visibility" group — checkbox per type
     with color dot + label.
  2. Toolbar dropdown next to the eye button — compact, so a read-only user can
     filter types without ever opening the workbench.
- Read-only visitors (`canEditLandmarks()` false) must still see saved
  landmarks via the eye button — already supported by the "Viewing saved
  landmarks" status path; only edit buttons are disabled.
- CSS: reuse `.ios-landmark-type` color-dot styling; add `.ios-landmark-vis`
  classes in `static/css/patient_detail.css`.

**Server side**: none required — visibility is purely client-side. Landmarks
load/save endpoints unchanged.

---

## 2. Project-first architecture

### 2.1 What exists today (inventory)

- `common.Project`: name/slug/description/icon/is_active/created_by + `modalities`
  M2M. **Currently one row per domain** (`maxillo`, `brain`, `laparoscopy`),
  created as a side effect on URL import (`maxillo/urls.py` `Project.objects.create`
  — a hack to remove).
- `common.ProjectAccess`: user→project, roles `standard`/`admin`. Grants app
  entry (middleware) + upload right. Invitations grant ProjectAccess.
- Session `current_project_id`; `ProjectSessionMiddleware` (URL namespace →
  project by slug); `ActiveProfileMiddleware` (`user.profile` ← ProjectAccess by
  namespace slug).
- Upload already limits `allowed_modalities` to the current project's
  `modalities` (`patient_upload.py`, `upload.html` render loop).
- Folders per domain (`FolderBase`): nested (`parent` self-FK), patients in
  `Patient.folder` FK (maxillo/laparoscopy) or `Patient.folders` M2M (brain).
  `FolderAccess` user→folder roles `standard`/`annotator`/`project_manager` is
  the **actual patient-level ACL** today.
- `common/permissions.py`: folder-based checks (`user_can_read_folder`,
  `user_can_write_annotations`, `filter_patients_for_user` ...).
- Processing: `ProcessingStep` DAG per modality (global), dispatch at upload via
  `common/uploads.create_step_jobs`; rerun via `ensure_step_jobs_for_patient`.
  Queue routing per project slug (`job_routing._project_slug_for_job` uses the
  fake `patient.project`).
- `maxillo.Patient.project` is a **property** that fake-looks-up
  `Project(slug='maxillo')` — must be replaced by a real FK.
- Annotation tools present today: IOS landmarks, bite classification,
  intraoral segmentation, sagittal/vertical/transverse classification, voice
  captions (maxillo); region/quadrant markers (laparoscopy); MRI captioning +
  segmentation review (brain).

### 2.2 Target model

```
Project (common)
  ├─ domain (slug: maxillo|brain|laparoscopy)          NEW
  ├─ modalities M2M                                    (exists — gates upload input)
  ├─ annotation_methods M2M -> AnnotationMethod        NEW
  ├─ is_active / description / icon / created_by       (exists)
  └─ folders  -> Folder.project FK                     NEW (mandatory)
         └─ patients -> Patient.project FK (NEW, mandatory) + Patient.folder FK (mandatory)
```

- `AnnotationMethod` (new common model): slug/name/description/icon +
  applicable domain/modalities; admin-managed. Examples:
  `ios_landmarks`, `bite_classification`, `intraoral_segmentation`,
  `classification`, `voice_caption`, `video_regions`, `mri_caption`.
- Project gates:
  - **Input modalities**: upload UI + server-side save guards use
    `project.modalities` (UI exists; server guard missing).
  - **Processing dispatch**: step DAG filtered to steps whose modality ∈
    project.modalities (upload-time `create_step_jobs`, rerun
    `ensure_step_jobs_for_patient`, rerunnable-step picker,
    `_processing_status`/viewer availability).
  - **Annotation methods**: patient detail UI + write endpoints gated by
    `project.annotation_methods`.

### 2.3 Permission layer

`common/permissions.py` migrates from folder-scoped to project-scoped checks:

- `user_can_read_patient(user, patient)` — any ProjectAccess row on
  `patient.project` (or project admin / staff / demo guest rules).
- `user_can_write_annotations(user, patient)` — ProjectAccess role
  `annotator`|`admin` on `patient.project`.
- `filter_patients_for_user(qs)` — `project_id__in` user's ProjectAccess set.
- `filter_folders_for_user(qs)` — folders whose `project_id` ∈ set.
- Keep `user_is_project_admin`; project resolution from a namespace becomes
  "projects with `domain == namespace`" instead of `slug == namespace`.
- `ActiveProfileMiddleware`: resolve profile from session `current_project_id`
  (fallback: first active project of the namespace's domain). No more
  namespace-slug lookup.
- `user.profile` compatibility preserved (ProjectAccess has the same method
  surface).

### 2.4 Processing gating

- `create_step_jobs(source_job, allowed_modality_slugs=None)`: skip any step
  whose `step.modality.slug` not in allowed set; root handling unchanged.
- `ensure_step_jobs_for_patient(patient, slugs)`: same filter, from
  `patient.project.modalities`.
- `modality_config._available_steps_for_files` /
  `rerunnable_steps_for_patient`: filter by patient's project modalities.
- `job_routing._project_slug_for_job`: read real `patient.project.slug`.
  **Compat note**: existing `RUNNER_QUEUE_BY_PROJECT` env keys are domain slugs;
  keep domain-slug projects as-is or add per-project env keys.
- Bite-classification auto-dispatch (`save_ios_to_dataset`) only when project
  allows it (or modality in project.modalities).

### 2.5 Annotation-method gating

- Registry list per project (`annotation_methods`).
- Patient detail view computes `allowed_annotations` into context; templates
  `{% if 'ios_landmarks' in allowed_annotations %}` wrap:
  - `#iosLandmarkControls` (and landmarks endpoints → 403 server-side),
  - bite classification section, intraoral segmentation section,
    classification panel,
  - laparoscopy annotation mode button/panels,
  - brain caption/segmentation tools.
- Server-side guard in `patient_data.py` landmarks endpoint + captions +
  classification writes (defense in depth; UI hiding is cosmetic).

### 2.6 Upload flow

- `PatientUploadForm`: add `project` (required) + keep `folder` but make
  **required**. Folder queryset = folders of selected project (JS cascade from
  project select; existing `get_project_folders` API repurposed, project-scoped).
- Server-side: reject folder whose `project` != selected project; reject
  modality not in `project.modalities` (hard guard in `save_*` helpers or
  view).
- Namespace still scopes uploads (project select shows only projects with
  matching `domain`).

### 2.7 Admin tooling

- Django admin (`maxillo/admin.py`): enable Project create/edit (modalities +
  annotation_methods + domain + active), register `AnnotationMethod`,
  `ProjectAccess` per-project user management (already exists).
- In-app admin control panel: add Projects section (list, create, edit,
  grant/revoke user access) — replaces per-folder permission UI in
  `folders_tags.py` (`folder_permissions`, `upsert_folder_permission`,
  `delete_folder_permission` deprecated; create/rename folder stays, now
  project-scoped).

### 2.8 Migration — folders become Projects

Three ordered migrations; all reversible (except final NOT NULL enforcement
which requires backfill).

**M1 — schema, nullable additions**

- `common.Project.domain` (nullable; backfilled from slug).
- `common.AnnotationMethod` + `Project.annotation_methods` M2M.
- `common.Folder`... no — folder tables are per-domain:
  - maxillo/brain/laparoscopy `Folder.project` FK (nullable).
  - maxillo/laparoscopy `Patient.project` FK (nullable).
- Brain: add `Patient.project` FK + single `Patient.folder` FK (nullable) —
  keep `folders` M2M until data migration, then drop.

**M2 — data migration (the core "folders → projects")**

Recommended rule — **top-level folders become Projects; subfolders become
folders inside those projects**:

1. For each root folder `F` (parent IS NULL) in domain `D`:
   - Create `Project P`: name=`F.name`, slug=unique slug (dedupe with `-2`,
     `-3`), `domain=D`, icon/description copied, `modalities` = all active
     modalities of domain `D`, `annotation_methods` = all methods applicable to
     `D`.
   - For each subfolder `S` under `F`: migrate to `Folder S'` with
     `project=P`, `parent=NULL` (flatten), name kept, created_by kept.
   - Patients directly in `F`: `patient.project=P`, `patient.folder` = new
     default folder in `P` (create `Folder` named `F.name` or "Default" under
     `P`; deterministic single folder).
   - Patients in subfolder `S`: `patient.project=P`, `patient.folder=S'`.
2. **ACL**: for each `FolderAccess(folder=F)` row → `ProjectAccess(user,
   project=P)` with role mapping:
   - `standard` → `standard`
   - `annotator` → `annotator`
   - `project_manager` → `admin` (project managers manage the project)
   - Subfolder `FolderAccess` rows fold into `P` (union; highest role wins).
   - Dedupe on `(user, project)`; keep existing domain-level ProjectAccess rows.
3. Domain catch-all projects (`maxillo`/`brain`/`laparoscopy` slug rows): keep
   as real projects (become the "unassigned/default" bucket if needed).
4. Patients with `folder IS NULL` (or brain M2M empty): `patient.project` =
   domain catch-all project, `patient.folder` = its default folder.
5. Brain patients with multiple folders: pick first folder (deterministic),
   log others in migration output.
6. Backfill `Folder.project` for the catch-all projects' folders.

**M3 — enforce + cleanup**

- `Project.domain` NOT NULL.
- `Folder.project` NOT NULL (all three apps).
- `Patient.project` NOT NULL; `Patient.folder` NOT NULL (maxillo/laparoscopy);
  brain: drop `folders` M2M, keep single FK.
- Remove `Patient.project` fake property (would clash with FK; maxillo only).
- Remove URL-import project creation hacks (`maxillo/urls.py`,
  `laparoscopy/urls.py`).
- `FolderAccess` tables: keep rows (no data loss), stop reading; optional
  archive/rename later. `Folder.parent` column kept nullable (legacy), no
  longer used by UI.
- Object-storage key prefixes: switch `project_slug_from_patient` to real
  project slug **or** keep domain prefix (decision 4) — changing the prefix
  changes object keys for new uploads only; existing objects untouched.

### 2.9 UI overview

- Landing: project cards grouped/ordered by domain; per-project patient count
  (real FK now).
- Navbar project switcher: unchanged (session `current_project_id`), now
  reflects real multi-project world; shows user's accessible projects.
- Patient list: folder filter lists only folders of the current project;
  patient rows scoped by project (filter_patients_for_user now project-based).
- Patient detail: breadcrumb Project › Folder › Patient; modality tabs filtered
  by project.modalities; annotation sections filtered by
  project.annotation_methods.
- RecentlyViewed `project_label`: real project name.

### 2.10 Rollout / risk

- Migrations run in deploy order M1→M2→M3; M2 is a data migration (use
  `RunPython` with reversible function + dry-run script).
- Tests to update: `tests_permissions.py`, `tests_demo.py`,
  `tests_live_transcription.py`, `tests_phase8.py`, `tests_upload.py`,
  `tests_patient_list_upload.py`, seed_dev, demo guest.
- `.env`/`RUNNER_QUEUE_BY_PROJECT` keys keep working for domain-slug projects;
  new projects can add their own keys.
- Feature-flag not required — the migration is deterministic; recommend staging
  dry-run on a DB copy (there is a prod dump: `ygg_1.9_20260704_155137.sql.gz`).

---

## Open decisions (need user input)

1. **Folder nesting**: flatten (top-level folders → projects, subfolders →
   folders) — recommended. Or every folder (any depth) → its own project?
2. **Role mapping**: `project_manager` (folder role) → `admin` (project role)?
   Or keep a `project_manager` role on ProjectAccess?
3. **Brain M2M**: OK to collapse `Patient.folders` M2M → single `folder` FK
   (first folder wins)?
4. **Object storage prefix**: keep domain prefix (`maxillo/raw/...`) or switch
   to project slug (`<project_slug>/raw/...`)?
5. **Domain catch-all projects**: keep `maxillo`/`brain`/`laparoscopy` slug
   projects as real, usable projects after migration?
6. **Landmark toggle placement**: both toolbar dropdown + workbench Display
   section (recommended), or one?
7. **ProjectAccess roles**: extend to `standard`/`annotator`/`admin` (adds
   read-only vs annotate distinction — needed to preserve today's
   `FolderAccess.standard` read-only semantics)?
