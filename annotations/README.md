# `annotations/`

The durable, versioned record of everything an annotator produces: landmarks,
tooth and volume segmentation, occlusion classification, panoramic arches,
measurements, video regions and quadrant markers — all in one shape, for every
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
