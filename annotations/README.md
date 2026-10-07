# `annotations/`

The durable, versioned record of everything an annotator produces: landmarks,
tooth and volume segmentation, occlusion classification, panoramic arches,
measurements — all in one shape, for every
domain.

## What it owns

- **The model** (`models/`): `SourceResource` (the thing annotated, addressed by
  a stable `identity_key`) → `AnnotationSet` → `AnnotationTarget` →
  `AnnotationSelector`, with `AnnotationRevision` (a snapshot, never a delta),
  `AnnotationPayload` and the `AnnotationItem` subclasses.
- **Identity** (`identity.py`) — pure construction of `identity_key`.
- **Four layers**, and the boundaries between them are the point:
  - `validators/` — **pure**. Values in, `ValidationError` out. No database, no
    object storage, no model instances.
  - `adapters/` — **pure translation**. A legacy row or an interchange document
    in, descriptor dicts out. Never queries, never saves.
  - `services/` — **the only writer.** Every write allocates the revision number
    against the unique constraint, refreshes `ever_annotated`, fingerprints the
    targets and validates the items in one transaction.
  - `serializers/` — builds the canonical JSON document.
- **Corpus sweeps** (`management/commands/`) — the only place that reads bytes
  out of object storage in bulk.

## What it must NOT own

- **Bytes.** A dense labelmap is a `common.FileRegistry` row addressed by an
  `AnnotationPayload`; a sparse annotation is rows. Nothing binary lives in an
  annotation table.
- **Cornerstone runtime identifiers.** `annotationUID`, `imageId`, `volumeId`,
  `segmentationId` and `cachedStats` are session-scoped and must never be
  persisted or appear in a canonical document.
- **Domain UI.** Viewers, upload forms and patient pages live in the domain apps;
  this app is called by them.
- **Writes from outside `services/`.** A view that imports an annotation model
  and calls `.save()` is a review failure.

## The boundary with `common/`

`annotations` may import `common`; **`common` may not import `annotations`.**
That direction is the whole reason this is a separate app rather than a package
inside `common/`: annotations depend on `common` (patients, `FileRegistry`,
projects) while `common` grew subsystems that want to ask questions *about*
annotations. As one app that would be a cycle with nothing to stop it; as two,
the direction is checkable.

Where `common` needs the answer to "is this annotated?", it goes through the one
narrow module `common/annotation_lock.py`, not through these models.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the concurrency and schema rules
(`record_revision`, conditional constraints on MySQL, `is_calibrated`, the
255-character identity-key cap).

## Image segmentation (`kind = image_segmentation`)

Dense 2D labelmaps for a still image or the frames of a video; the pixels are the record.
Per revision, one non-canonical `npz_mask` payload **per annotated frame** (`variant`
`f<fileId>t<timeMs>`; one binary plane per label, so labels may overlap) plus one canonical
inline manifest listing every frame and a per-label `{area, bbox}` summary. A save names only
the frames it edited; the rest are carried forward as new payload rows over the *same* files,
so no bytes are copied. A frame sent with every plane empty is a deletion. Frames are keyed by
**milliseconds**, so re-encoding a video does not move a mask. The vocabulary is a per-project
`LabelSchema` (`image-segmentation-project-<pk>`): `value` and `code` never change, a rename or
recolour cannot move a mask, and labels are retired, never deleted. The browser sends planes as
`base64(gzip(bytes))`; the server owns the archive format. Routes:
`api/patients/<id>/image-segmentation/{,state/,frame/,labels/,labels/<code>/}`.

## Video quadrants (`kind = video_quadrants`)

The timeline classification of a surgical video: "from here on, the camera is in quadrant X".
One `EventAnnotationItem` per marker (`event_type="quadrant"`, `time_ms`, `label` FK); a marker
is an instant and the span it covers is derived by the reader (to the next marker, or the end),
so editing one never rewrites its neighbour. The vocabulary is its own per-project
`LabelSchema` (`quadrant-project-<pk>`), separate from the segmentation labels, with the same
retire-never-delete rule. A `PUT` replaces the whole marker list as a new revision; it is gated
by the `image_segmentation` method. Routes: `api/patients/<id>/quadrants/{,labels/,labels/<code>/}`.
