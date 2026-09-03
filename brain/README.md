# `brain/`

Brain-tumour MRI, mounted at `/brain/`. The second domain, and the proof that
the shared machinery generalises: it reuses the same patient / folder / job /
export workflow as `maxillo/` under its own namespace and its own tables.

## What it owns

- **Its own domain tables**: `Patient`, `Folder`, `FolderAccess`, `Dataset`,
  `Tag`, `VoiceCaption`, `Export` — mostly subclasses of the abstract bases in
  `common/base_models.py`, with `Patient` written for this domain.
- **The brain modalities**: the MRI sequences T1, T1c, T2 and FLAIR plus the
  segmentation output, registered by `manage.py setup_brain_modalities`.
- **Multi-sequence review**: the volume-grid surfaces that put the sequences side
  by side, and the domain's upload forms and patient pages.
- **Export configuration** (`export_config.py`) — which artifacts a brain export
  offers, expressed against the shared export catalog.
- **Voice captioning wiring** for this domain (the transcription relay itself is
  shared).

## What it must NOT own

- **Anything generic.** This app is the smallest of the three on purpose. A
  helper here that has nothing to do with MRI is a sign it should have gone into
  `common/`.
- **The annotation record.** Segmentations and measurements are written through
  `annotations/services/`.
- **A private copy of the pipeline.** Jobs are `common.Job` rows; dispatch is the
  shared `common/signals.py` signal. There is no brain-specific dispatch path.

## The boundary with `common/`

`brain` imports `common`; `common` never imports `brain`. Its domain slug is
registered once in `common/domains.py`, which is also where its per-domain FK
column names (`brain_patient`, `brain_voice_caption`) live — everything that
needs to reach a brain patient from shared code goes through that registry
rather than importing this app.

`brain.UserPreference` is superseded by `common.UserPreference`; prefer the
shared one for anything new.

Adding a comparable app is documented in
[docs/new-project-type.md](../docs/new-project-type.md).
