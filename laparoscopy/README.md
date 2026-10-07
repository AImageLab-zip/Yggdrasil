# `laparoscopy/`

Surgical video, mounted at `/laparoscopy/`. The domain that is not volumetric — its
unit of work is a frame, not a voxel.

**Annotation Mode** is the only annotation surface. It freezes the frame the video is paused
on and opens it for pixel-accurate label masks (brush, eraser, lasso, rectangle, circle, undo,
per-label colour/visibility/opacity, autosave). The button appears only when the project
enables the `image_segmentation` annotation method **and** the subsampled video derivative has
a recorded `ffprobe` result -- the surface will not guess a frame rate (`manage.py
laparoscopy_probe_videos` backfills it). Playback remains the compressed derivative; Annotation
Mode snaps its paused time to the nearest frame of the subsampled derivative. Frontend:
`frontend/imaging/segment/`, entry `image-segment`. Storage and API
are generic, in `annotations/services/image_segmentation.py`; nothing here is laparoscopy
specific except the page that mounts it.

The earlier annotator and its tables were removed (`annotations` migration 0010 purged the
stored sets; `manage.py laparoscopy_purge_annotation_masks` removes their archives from object
storage).

## What it owns

- **Its own domain tables**: `Patient`, `Folder`, `FolderAccess`, `Tag`, `VoiceCaption`,
  `Export`, `Classification`.
- **Video handling**: `video_probe.py` and `manage.py laparoscopy_probe_videos`
  (duration, frame rate, dimensions), recorded as the file arrives; `file_utils.py` for
  the upload path.
- The `video` modality, registered by `manage.py setup_laparoscopy_modalities`.

## What it must NOT own

- **The durable annotation record.** Anything an annotator produces is stored through
  `annotations/services/`, in the same shape as every other domain.
- **Generic media or export machinery.** Uploads, object storage and the export catalog
  are `common/`.
- **Its own job dispatch.** Frame-processing work is `common.Job` rows on the shared
  signal path.

## The boundary with `common/`

`laparoscopy` imports `common`; `common` never imports `laparoscopy`. Its slug and
per-domain FK names (`laparoscopy_patient`, `laparoscopy_voice_caption`) are registered in
`common/domains.py`, and shared code reaches this app only through that registry.

Video-shaped concepts — frame indices, time ranges in milliseconds — belong to this app or
to the annotation selectors that model them (`frame_index`, `start_time_ms` /
`end_time_ms`), never to `common/`.
