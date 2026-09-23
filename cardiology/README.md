# `cardiology/`

ECG review, mounted at `/cardiology/`: upload a raw ECG recording per patient,
read it on a clinical grid, classify its rhythm and write notes about it.

## What it owns

- **Domain tables**: `Patient`, `Folder` (+ legacy `FolderAccess`), `Tag`,
  `VoiceCaption`, `Export`, and the `CardiologyProject` proxy for the admin.
- **The `ecg` modality** and the `ecg_classification` annotation method, seeded by
  `python manage.py setup_cardiology_modalities`.
- **ECG upload** (`forms.py`, `file_utils.py`, single and bulk). A recording is a
  JSON document `{"frequency", "dataX", "data": [{"title", "values"}]}`, validated
  structurally before anything is stored: numbers only, time in increasing order,
  a size cap, and a duration limit so the plot fits a browser canvas. There is no
  processing pipeline and no server-side signal processing.
- **The plot** (`static/js/cardiology/ecg_plot.js`): drawn in the browser at the
  clinical 25 mm/s and 10 mm/mV scale onto an off-DOM canvas, and panned through a
  window of it, so a 12-lead strip stays usable on a phone. The first time anyone
  with write access opens a patient, the rendered PNG is posted back
  (`save_browser_ecg_plot`) and becomes the `ecg_processed` export artifact. The
  admin warmup page (`ecg_warmup`) forces that pass for a whole folder, the way
  maxillo's panoramic warmup does.
- **The rhythm call** (AF / NSR / Other / NI): the patient page and the list
  filters. The call itself is stored in `annotations/` (below).
- **The export collector** for `ecg.classification` (`exports.py`), registered with
  `common.export_catalog` from `CardiologyConfig.ready()`.

## What it must not own

- **The rhythm call's storage.** It is an `AnnotationSet` of kind
  `ecg_rhythm_classification`, written only through
  `annotations.services.ecg_rhythm` and read in bulk through `annotations.queries`.
  Every change is a revision, a stale save is refused with 409, and a call locks
  the raw ECG like any other human annotation.
- **Anything every domain has.** Folders, tags, captions, deletion, exports and
  the profile page are the shared views in `common/domain_views/`, routed from
  `app_urls.py`; file responses go through `common.file_access`.

## The boundary with `common/`

`common/` never imports this app (`lint-imports` enforces it). Where shared code
needs cardiology-specific behaviour it reads the registry (`common/domains.py`:
FK columns, storage prefix, caption-panel switches) or a hook this app registers
at startup (the export collector). Object access is always judged against the
patient's own project (`get_patient_for`).
