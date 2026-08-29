# Cornerstone3D — deferred work

Companion to [cornerstone-roadmap.md](cornerstone-roadmap.md), which tracks the migration
itself. Everything here was considered during the Cornerstone3D v5 migration design and
**deliberately left out of scope**, with the reasoning recorded so a later decision does not
have to re-derive it.

Nothing in this file is committed work. Each entry states what it is, why it was deferred,
what would have to be true to pick it up, and roughly what it would cost.

The governing rule from the migration still applies to every item below:

> Yggdrasil owns semantics, provenance, identity, revisioning and canonical annotation data.
> Cornerstone owns interactive visualization and editing. DICOM/NIfTI/BIDS are storage and
> interchange formats. No viewer-specific or interchange-specific format defines the
> Yggdrasil domain model.

---

## 1. Client-side AI segmentation (`@cornerstonejs/ai`)

**What.** `@cornerstonejs/ai` ships `ONNXSegmentationController` (SAM / SAM2 via
`onnxruntime-web` with a WebGPU backend), `MarkerLabelmapTool` and
`LabelmapSlicePropagationTool`. Prompt points are turned into masks entirely in the browser,
written straight into Cornerstone labelmap state, with `IslandRemoval` for cleanup.

**Why deferred.** The platform already has working prompt-driven segmentation: the
laparoscopy Magic Tool talks over a WebSocket to a GPU worker (`WORKER_BASE_URL`,
`laparoscopy_annotator_worker.js`, the Django proxies at `laparoscopy/views.py`). That
server-side path does something the client-side controller does not: **temporal propagation
and track identity across a video window**. Replacing it would be a capability regression on
the one surface that uses it most.

The migration therefore keeps the WebSocket contract untouched and only changes what consumes
the returned mask — Cornerstone labelmap state instead of Konva polygons.

**What would have to be true to pick it up.**

- WebGPU available on the clinicians' actual machines. Without it, `onnxruntime-web` falls
  back to WASM and interactive prompting is too slow to be worth the round-trip it saves.
  The migration targets WebGL2-required / WebGPU-opportunistic, so this needs measurement,
  not assumption.
- Several hundred MB of ONNX weights self-hosted. `.gitignore` already reserves
  `static/vendor/transformers/models/` and `static/vendor/transformers/onnxruntime*/` for
  exactly this, so the storage decision was anticipated — but the fetch-and-verify step, the
  cache strategy, and the first-use download experience are all unbuilt.
- A decision on temporal propagation: either reimplement tracking client-side, or accept a
  hybrid where single-frame prompting is local and multi-frame propagation stays remote.

**Shape if adopted.** The most defensible version is *both*: client-side for interactive
single-frame prompting (no network round-trip, works offline, removes the hardcoded
institutional GPU host as a hard dependency), server-side retained for propagation and for
clients without WebGPU. The adapter boundary the migration builds makes this a sink swap
rather than a rewrite.

**Adjacent, and cheaper:** the same controller is modality-agnostic. Prompt-driven
segmentation on CBCT / brain volumes and on intraoral photos is a larger capability gain than
re-doing laparoscopy, and it has no incumbent to regress.

---

## 2. Multi-user live collaborative editing

**What.** Two annotators in the same study, seeing each other's edits live.

**Why deferred.** The annotation model is built on **immutable revisions with optimistic
concurrency** — `UniqueConstraint(annotation_set, revision_number)` means the second writer
gets a 409 and has to rebase. That is the correct primitive for reviewed clinical annotation
work, and it is deliberately *not* a CRDT. Live co-editing is a different consistency model,
not a feature bolted onto this one.

**What would have to be true.** A concrete demand for it. Today's workflow is one annotator
per set with a review step (`draft → submitted → in_review → approved`), which is what the
lifecycle was designed for. If co-editing is genuinely wanted, the honest design is
per-segment or per-region ownership within a draft revision — soft locks — rather than
operational transform over voxels.

**Note.** The infrastructure for the presence half already exists: Channels, the ASGI stack,
and `common/presence.py` power the online-users dashboard. Showing *who else has this study
open* is a small, useful, low-risk subset that does not require solving concurrent editing at
all, and is worth considering on its own.

---

## 3. Automatic segmentation conflict merging

**What.** When two revisions diverge from a common base, merge the labelmaps automatically
instead of forcing a rebase.

**Why deferred.** For dense voxel data there is no safe generic merge. Union, intersection
and last-writer-wins are all clinically wrong in some case, and a silent wrong merge is worse
than an explicit conflict. The 409 is the honest answer.

**What would have to be true.** A defined per-project policy stated by the people whose data
it is — e.g. "merge disjoint segments, conflict on overlap" — plus a diff UI so a human can
see what changed before accepting. The revision model already stores enough (base revision,
both canonical payloads) to compute and display such a diff; it is the *policy*, not the
plumbing, that is missing.

---

## 4. 4D segmentation editing

**What.** Editing segmentations on time-varying volumes (`segmentation_4d`, `roi_4d`).

**Why deferred.** No modality in the platform is 4D today. The kinds are present in
`AnnotationSet.KINDS` and `AnnotationSelector` carries `volume_index`, so the model does not
have to change to accommodate it — the work is entirely viewer and tooling.

**What would have to be true.** A 4D modality actually being ingested. Cornerstone has the
pieces (`dynamicVolume`, `cornerstoneStreamingDynamicImageVolumeLoader`, `dynamicCINETool`,
`generateImageFromTimeData`), so this is a "when the data arrives" item, not a research item.

---

## 5. RTSTRUCT-first workflows

**What.** Treating DICOM RTSTRUCT as a first-class editable representation — importing an
RT structure set, editing contours as contours, and writing back — rather than as an export
format.

**Why deferred.** The migration makes the canonical representation a **NIfTI labelmap** for
dense segmentation, with RTSTRUCT as an *exchange* payload written server-side. RTSTRUCT is
a per-slice planar contour format; round-tripping labelmap → contours → labelmap is lossy in
both directions, and making it canonical would mean either accepting that loss or maintaining
two canonical forms.

There was also a dependency caveat recorded in the migration risk register: that
`highdicom`'s RTSTRUCT *writer* is newer and less exercised than its SEG writer. **That was
wrong, and Phase 9 found out by looking.** `highdicom` 0.28.1 has `seg`, `sr`, `pm`, `ann`,
`ko`, `pr`, `sc` and `legacy` and **no RTSTRUCT writer at all**. The exchange writer this
repository ships is built directly on `pydicom` in `common/interop/rtstruct.py`, and its
tests read every object back and re-derive the contours.

That changes the shape of this entry rather than its conclusion. Making RTSTRUCT canonical
would still mean owning a per-slice planar contour format end to end — but the *reader*
would now have to be written here too, not merely adopted.

**What would have to be true.** A radiotherapy workflow with RTSTRUCT as the source of truth
— i.e. structure sets arriving from a treatment planning system that must be edited and
returned without a labelmap ever being authoritative. At that point `Contour` segmentation
representation plus `PlanarFreehandContourSegmentation` / `SplineContourSegmentation` /
`LivewireContourSegmentation` are the right tools, and the canonical payload for *that*
annotation kind would be contours, not voxels. The `AnnotationPayload` model already supports
a different canonical format per annotation kind, so this does not require a schema change.

---

## 6. Whole-slide imaging (`WSIViewport`)

**What.** Cornerstone v5 ships a whole-slide imaging viewport and `wsiAnnotationTools`.

**Why deferred.** No histopathology modality exists in the platform. This is listed only so
it is on the record that the viewer stack chosen can serve it without another migration — the
same `annotations/` model, the same adapter layer, a new `SourceResource` geometry and a new
viewport type.

**Cost if adopted.** Real, and mostly *backend*: WSI means pyramidal tiled serving, which the
current whole-file `streaming_response` model does not do. The DICOMweb subset added for
DICOM ingestion is the natural place to grow `/tiles`, and the byte-range work done there is
the prerequisite.

---

## 7. Surface segmentation persistence and automatic representation conversion

**What.** `@cornerstonejs/polymorphic-segmentation` converts between labelmap, contour and
surface representations (`PolySegWasm*` — nine documented directions). The migration uses it
*in one direction, in memory* (mask → contour for the Magic Tool sink, and labelmap → surface
for 3D display).

**Why deferred as a storage contract.** Persisting a surface as canonical means committing to
a mesh format (`gifti_surface` is reserved in `AnnotationPayload.FORMATS` for this) and to the
conversion being stable across library versions. The migration's position is that **labelmap
is the one fully-supported canonical segmentation path**, and contour/surface round-trips
should be validated independently before anything depends on them at rest.

**What would have to be true.** A voxel-equality round-trip test suite over
labelmap → surface → labelmap on real studies, at several resolutions, showing the loss is
bounded and understood. Until that exists, surfaces are a rendering artifact
(`role='derived'`), not a canonical record.

---

## 8. Retiring the in-browser DICOM parser

**What.** `static/js/cbct_convert.js` + `static/js/worker/cbct_convert_worker.js` implement a
hand-rolled DICOM parser that converts a series to `.nii.gz` before upload.

**Why deferred (partially).** Native DICOM ingestion is in scope for the migration, and once
it ships the DICOM branch of the converter has no job. But the same worker is also the only
path for **MetaImage (`.mha`)** and for **NIfTI orientation repair**, both of which stay.

**Plan of record.** One release after DICOM ingestion ships, delete
`convertDicomSeries` / `parseDicomHeader` / `dicomSliceArrayType` and the DICOM cases in
`static/js/tests/cbct_convert_worker.test.js`. Keep `isDicomBuffer` — it becomes the
client-side pre-filter that avoids uploading 400 MB of non-DICOM. Keep everything else.

**Status: done, and earlier than planned.** Phase 8 deleted the DICOM branch in the same
commit that made it redundant rather than waiting a release, because leaving two DICOM
readers in the tree — one of which threw every slice of a folder into a single volume
regardless of `SeriesInstanceUID`, and refused compressed pixel data outright — would have
meant an ordinary JPEG-Lossless CBCT taking the broken path for a release. `isDicomBuffer`
stayed, and so did the MetaImage and NIfTI-repair halves.

---

## 9. Remaining CDN dependencies — **withdrawn**

This entry proposed removing the last runtime CDNs (Chart.js and
`chartjs-adapter-date-fns` on `templates/common/user_activity_stats.html`) and extending
`scripts/check_bundle_assets.mjs` to scan templates so a no-CDN rule was machine-enforced.

**The rule it was enforcing is gone.** A CDN serves a static asset faster than this
deployment can and takes the bandwidth off it; the blanket ban was never actually held to
either — `templates/base.html` has loaded Three.js, an STL loader, trackball controls and
fflate from three different CDNs the whole time. So Chart.js stays where it is, and the
build-time and verify-time checks now *note* a CDN reference instead of failing on it.

Two narrower rules survive, and they are not this one:

- **Webfonts stay self-hosted.** A font CDN sees every page view of every visitor, which
  is a consent question a JavaScript library does not raise. That is why IBM Plex and Font
  Awesome are in `static/`, and it is a GDPR argument, not a performance one.
- **The itk-wasm pipelines stay vendored and aliased** (F5). Not policy: those are wasm
  blobs whose ABI is pinned to the package version, and the upstream default URL is a
  moving reference. The check that used to enforce the ban is what now reports it if the
  alias ever stops applying.

---

## 10. Upstream defects to report

Found while verifying `@cornerstonejs/*@5.8.2` against the shipped packages. Both are worked
around in this codebase; both should be filed upstream so the workarounds can eventually be
removed.

**`nifti-volume-loader/helpers/modalityScaleNifti.js` — inverted operator.**

```js
if (slope !== 1 && inter !== 0) {   // should be ||
```

A NIfTI with `scl_slope = 1, scl_inter = -1024` (the common uint16-plus-intercept CT/CBCT
encoding) receives no rescale at all — every voxel is off by 1024 HU, silently.
`slope = 2, inter = 0` is skipped for the same reason. Worked around by deriving
`modalityLutModule` from the raw header in `frontend/imaging/metadata/`, and guarded by CI
fixtures across all four slope/intercept branches.

**`modalityScaleNifti.js` `allocateScalarData` — `Int8Array` under-counts the cache
budget by 2×** (roadmap F16).

```js
case 'Int8Array':
    bitsAllocated = 8;                       // but...
    checkCacheAvailable(bitsAllocated, nVox);
    scalarData = new Int16Array(nVox);       // ...16 bits are allocated
```

`checkCacheAvailable` therefore reserves half the bytes actually taken, so a volume near
the cache ceiling can be admitted and then overrun it. Not worked around here — it only
affects the `NIFTI_TYPE_INT8` branch with a float rescale, and the honest fix is upstream.

**Fabricated affine when no qform/sform.** `nifti-reader-js` synthesises a diagonal RAS affine
from `pixDims` when `qform_code < 1 && sform_code < 1`; `rasToLps()` then converts that
fiction into a plausible LPS direction and the volume renders without complaint. This is the
same silent-mirroring hazard documented in `static/js/volume_metadata.js`, which is why that
module is retained and re-wired rather than deleted. Arguably an upstream *policy* question
rather than a bug — but the loader should at minimum surface that the orientation was
inferred, so a consumer can refuse to show measurements derived from it.


---

## 11. Browser-side interchange import (SEG / RTSTRUCT / SR)

**What.** Reading a DICOM SEG, RT Structure Set or SR produced elsewhere and turning it
into Yggdrasil annotations. Decision #2 asked for import *and* export; Phase 9 shipped
export only.

**Why deferred.** Not difficulty — scope, and the absence of a second half. Export has a
clear consumer: somebody wants this study's annotations in a form their own system reads.
Import has none yet, and importing without one means choosing, with no user to ask, how
several genuinely ambiguous cases resolve: a SEG whose frames do not line up with any
series held here; an SR whose measurements reference instances that were never ingested;
an RTSTRUCT whose contours fall outside every stored frame of reference. Each of those is
a policy question, and a policy invented to make a feature demo-able is the kind that
survives into production unexamined.

The roadmap put import in the browser deliberately — that is where the registered volume
is, so a labelmap can be checked against the grid it claims to describe before anything is
written. That reasoning still holds and is the right starting point.

**What would have to be true.** A stated source: a scanner, a planning system or a
collaborator actually sending these objects, so the ambiguous cases have someone to
resolve them. At that point risk 12 (two SEG writers disagreeing) comes back with it —
Phase 9 recorded it as not-applicable precisely because there is only one writer today,
and a browser-side importer paired with a browser-side writer is what would create the
second.

**Cost if adopted.** Moderate and mostly validation, not parsing: `dcmjs` reads all three,
and the adapter layer that turns them into descriptors is the same shape as the five that
exist. The work is in refusing well.

---

## 12. DICOM Parametric Map

**What.** `highdicom.pm`, for storing a derived per-voxel *measurement* map — an ADC map,
a perfusion map, a computed HU map — as DICOM rather than as a NIfTI.

**Why deferred.** Nothing in the platform produces one. A Parametric Map is not a
segmentation with real numbers in it: it is a map whose values carry a real-world quantity
and a unit, and DICOM requires that unit to be a coded concept. The CBCT pipeline emits a
label array, which is a SEG; the ROI statistics are numbers about regions, which are an
SR. Writing a Parametric Map today would mean inventing a quantity for values that do not
have one.

**What would have to be true.** A pipeline that emits a calibrated per-voxel quantity —
the obvious candidate is a bone-density map, which would have a real unit and a real
consumer. `highdicom.pm` is a small module and the export plumbing already exists, so this
is a "when the data arrives" item rather than a research one.

---

## 13. Re-wiring the Magic Tool onto the Cornerstone video surface

**What.** Restoring SAM2-assisted segmentation on the laparoscopy surface Phase 10 built.

**This is not deferred work — it is unfinished work**, listed here only so it is not lost.
It is a **release blocker** for 3.0. The distinction matters: everything else in this file
was considered and deliberately left out of scope, and this was not.

**Where it stands.** The WebSocket contract is untouched (decision #9) — the GPU worker,
the protocol and the Django proxies in `laparoscopy/views.py` are exactly as they were, so
there is no contract to renegotiate. The sink is built and unit-tested
(`frontend/imaging/video/magicSink.js`): a returned mask is written into the labelmap
directly, with none of the contour tracing and small-component filtering the old annotator
needed to make a mask look like a polygon.

What is missing is the host. `laparoscopy_annotator_worker.js` was a mixin on the deleted
annotator's prototype and read **58 members** off it — shape registration, the mask overlay
renderer, the pending-scope bookkeeping, the timeline's drag state. Roughly half of those
have no counterpart on the new surface because they existed to manage Konva nodes; the
other half — prompt collection, the frame key, scope signatures, the mask decision box —
are real and need somewhere to live.

**Shape if picked up.** A `frontend/imaging/video/magic/` module owning the session, the
prompts and the pending scopes, talking to the surface through the same narrow interface
the page glue uses. The prompt points already have a home in the record: Phase 10 stores
them as sparse `Geometry2DItem` points in normalised coordinates, which is what
`annotations_convert_legacy` has always written.
