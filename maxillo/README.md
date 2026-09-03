# `maxillo/`

Dental and maxillofacial imaging, mounted at `/maxillo/`. The oldest domain app,
and the one the shared machinery was extracted out of.

## What it owns

- **Its own domain tables**: `Patient`, `Folder`, `FolderAccess`, `Dataset`,
  `Tag`, `VoiceCaption`, `Export`, `Classification` — most of them subclasses of
  the abstract bases in `common/base_models.py`, `Patient` written from scratch.
- **The maxillo modalities**: CBCT, IOS (intraoral scans), intraoral photos,
  teleradiography, panoramic, raw archives — registered by
  `manage.py create_maxillo_modalities`.
- **Domain workflows**: bite/occlusion classification, IOS mesh handling
  (`ios_meshes.py`), intraoral tooth work (`intraoral_teeth.py`), panoramic state
  and warm-up, per-modality upload and viewer pages under `views/`.
- **Historical shared surfaces.** Authentication (`views/auth.py`), the admin
  screens in `views/admin.py`, and — importantly — **the runner HTTP API**
  (`api_views/runner.py`, `runner_api_service.py`) live here rather than in
  `common/` for historical reasons. They are not maxillo-specific. Treat them as
  shared code that happens to have this address: the runner API in particular is
  **frozen**, pinned by `tests_runner_api.py`, and speaks to runners deployed
  outside this repository.

## What it must NOT own

- **New cross-domain infrastructure.** The fact that auth and the runner API sit
  here is legacy, not a pattern to extend. Anything a second domain would need
  belongs in `common/`.
- **The annotation record.** Landmarks, tooth segmentation, panoramic arches and
  measurements are stored through `annotations/services/`, not in maxillo tables.
- **The panoramic reconstruction mathematics.** That is
  `static/js/seg2pano_core.js` and its worker, reached through the
  `Seg2PanoCore` global and never vendored.

## The boundary with `common/`

`maxillo` imports `common` freely; `common` never imports `maxillo`. Uploads go
through `common/uploads.py` and land as `common.FileRegistry` rows; processing is
`common.Job` rows created for the modality's enabled `ProcessingStep`s;
authorization is `common.ProjectAccess`, not `FolderAccess` (those tables still
carry rows but are not read for authorization).

When something here starts looking generic, the move is to lift it into
`common/` **without a domain branch** — if it cannot be written without naming
maxillo, it stays here.
