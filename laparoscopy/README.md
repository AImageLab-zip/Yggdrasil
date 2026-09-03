# `laparoscopy/`

Surgical video annotation, mounted at `/laparoscopy/`. The domain that is not
volumetric — its unit of work is a frame, not a voxel.

## What it owns

- **Its own domain tables**: `Patient`, `Folder`, `FolderAccess`, `Dataset`,
  `Tag`, `VoiceCaption`, `Export`, `Classification`.
- **The video vocabulary**: `RegionType` and `QuadrantType` (the label schemes),
  `RegionTypeUserColor` / `QuadrantTypeUserColor` (per-project, per-user colour
  schemes), `QuadrantClassificationMarker` (time-stamped markers) and
  `RegionAnnotation`.
- **Video handling**: `video_probe.py` and `manage.py laparoscopy_probe_videos`
  (duration, frame rate, dimensions), `mask_raster.py` (rasterising region
  annotations into per-frame masks), `export_processor.py` (subsampled frames
  plus NPZ masks as a ZIP).
- **The AI-assist proxy**: point-prompt segmentation is forwarded to an external
  worker service (`WORKER_BASE_URL`); when it is not configured those endpoints
  fail closed and the rest of the app is unaffected.
- The `video` modality, registered by `manage.py setup_laparoscopy_modalities`.

## What it must NOT own

- **The durable annotation record.** Region and quadrant annotations are stored
  through `annotations/services/` in the same shape as every other domain; the
  legacy `RegionAnnotation` / marker tables are the historical form, adapted by
  `annotations/adapters/legacy_laparoscopy.py`, not a parallel system to extend.
- **Generic media or export machinery.** Uploads, object storage and the export
  catalog are `common/`.
- **Its own job dispatch.** Frame-processing work is `common.Job` rows on the
  shared signal path.

## The boundary with `common/`

`laparoscopy` imports `common`; `common` never imports `laparoscopy`. Its slug
and per-domain FK names (`laparoscopy_patient`, `laparoscopy_voice_caption`) are
registered in `common/domains.py`, and shared code reaches this app only through
that registry.

Video-shaped concepts — frame indices, time ranges in milliseconds, quadrants —
belong to this app or to the annotation selectors that model them
(`frame_index`, `start_time_ms` / `end_time_ms`), never to `common/`.
