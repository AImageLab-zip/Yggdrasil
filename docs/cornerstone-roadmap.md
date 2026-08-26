# Yggdrasil 3.0 — Cornerstone3D v5 migration: roadmap & status

Working branch: `release/3.0` (cut from `release/2.0` after `v2.0.0` is tagged).
Each phase ships as 1–3 PRs, independently deployable. **All migrations must be additive:**
no table renames, no destructive schema changes — see the risk register.

Companion documents:
- [cornerstone-future-work.md](cornerstone-future-work.md) — everything deliberately **out**
  of scope, with the reasoning.
- [modernization-roadmap.md](modernization-roadmap.md) — the 2.0 program, whose conventions
  this document follows.

## Why

Four unrelated frontend stacks render and annotate medical images, glued together by
`<script>` tag ordering and `window.*` globals: **NiiVue** (0.69.0 at
`templates/common/patient_detail.html:38`, 0.67.0 at
`templates/brain/patient_detail_content.html:151` — two versions), **Three.js r128**
(`templates/base.html:36-39`), **Konva** (two CDN specs), and plain `<img>`. There is no
`package.json`, no bundler, no npm.

| Problem | Evidence |
|---|---|
| One measurement tool exists, and it is never saved | `static/js/viewer_grid.js:88-91` (`toolRegistry` has one entry), `:96-100` (`measurementState` is module-local; nothing POSTs it) |
| No HU/real-value windowing — everything is percent-of-data-range | `static/js/modality_viewers/windowing.js`, `niivue_viewer.js:553-607`, slider `min=1 max=100` at `templates/maxillo/patient_detail_content.html:75-77` |
| No angle, area, ROI statistics, or calibration | absent; `LaparoscopyAnnotatorUtils.polygonArea` is dead code |
| Zero DICOM interoperability in or out | no `pydicom`, no `dcmjs`; DICOM is converted away in the browser and never stored |
| Undo/redo inconsistent: full (intraoral), snapshot (IOS), **none** (laparoscopy — the heaviest surface) | `intraoral_segmentation.js:644-711` vs `ios.js:628-631` vs `_deleteLastShape` defined and unbound at `laparoscopy_annotator_shapes.js:314` |
| Annotation API failures are silent by design | `laparoscopy_annotator_api.js:33-43` catches everything, resolves `null` |
| Five coordinate systems, no shared conversion layer | video px / normalized / mesh-local 3D / image px / world mm |
| Annotation UIs have zero test coverage; no E2E | 7 hand-run `node:test` files, none in CI |
| Third-party CDN is a runtime SPOF for the clinical viewer | `intraoral_segmentation.js:3` silently no-ops without Konva; contradicts the GDPR rationale at `templates/base.html:42-44` |

Cornerstone3D v5.8.2 (June 2026, MIT) replaces all four and closes every row.

## The governing architectural rule

> Yggdrasil owns semantics, provenance, identity, revisioning and canonical annotation data.
> Cornerstone owns interactive visualization and editing. DICOM/NIfTI/BIDS are storage and
> interchange formats. **No viewer-specific or interchange-specific format defines the
> Yggdrasil domain model.**

Consequences that constrain every phase:

- A new `annotations/` Django app is the durable model, reached only through an adapter layer.
- Cornerstone runtime ids (`imageId`, `volumeId`, `segmentationId`, `annotationUID`) are
  session-scoped and **never persisted**. Persistent identity is Yggdrasil ids,
  `SourceResource`, DICOM UIDs and file checksums.
- Cornerstone's serialized state is stored as an *editable* payload, never as canonical.
- The REST API is domain-oriented. There is no `POST /cornerstone/save`.
- Dense segmentation is a file artifact in object storage (canonical `nifti_labelmap`,
  exchange `dicom_seg`) — never JSON voxels.

## Decisions already made with the maintainer

| # | Decision |
|---|---|
| 1 | **npm + esbuild, dev-only; bundle committed** under `static/vendor/cornerstone/`. Nothing needed at deploy time. Removes every runtime CDN. |
| 2 | **Native DICOM ingestion in scope**, plus SEG/RTSTRUCT/SR import *and* export. |
| 3 | **Replace outright, per surface** — no feature flags. Each PR replaces one viewer and deletes the old code in the same commit. |
| 4 | **Cornerstone must not shape the database** (the rule above). |
| 5 | **Real modality-value windowing only — clean break.** Percent-of-range is deleted. |
| 6 | **Legacy annotations converted in place**; legacy tables readable one release as a cross-check, then dropped. |
| 7 | **IOS meshes migrate — Three.js removed entirely.** |
| 8 | **Panoramic becomes live CPR**; baked MIP/ray-sum PNGs retained as derived artifacts and **must stay exportable**. |
| 9 | **Magic Tool (SAM2) unchanged** — the WebSocket GPU worker stays. `@cornerstonejs/ai` deferred (see future-work). |
| 10 | **Laparoscopy video migrates last**; its *data* migrates in Phase 2 regardless. |
| 11 | First release also wires ROI stats + calibration + DICOM SR export; segmentation accelerators; polySEG labelmap↔contour↔surface; multi-volume fusion + advanced 3D. |
| 12 | **Yggdrasil 3.0 on `release/3.0`**, cut after `v2.0.0`. The additive-only rule re-anchors on the **v2.0.0** dump. |
| 13 | **WebGL2 required** (explicit unsupported-browser message), **WebGPU opportunistic**, **touch supported**. |
| 14 | **Labelmap editing is destructive** — brush and eraser mutate voxels; only the mask is canonical. Revisions are the audit trail, not stroke replay. |
| 15 | **Laparoscopy NPZ export stays byte-compatible**, but is regenerated from labelmaps instead of replaying strokes through PIL. |
| 16 | **Per-modality presets + per-volume auto-VOI.** Absolute HU presets only where meaningful; CBCT/MRI derive from robust percentiles. Presets configured per `Modality` in admin. |
| 17 | **Affine rewrite refuses when annotations exist (409).** |
| 18 | **The annotation lock becomes monotonic** — once annotated, always locked. |
| 19 | **ACL fix shipped first**, standalone. ✅ done on `release/2.0` (`232b40e`). |

## Critical findings

Established by reading the shipped `@cornerstonejs/*@5.8.2` packages and this repo — not from
documentation. Each one invalidates an assumption the migration would otherwise rest on.

**F1 — `modalityScaleNifti` has an inverted operator; HU would be silently wrong.**
✅ **Confirmed verbatim against the shipped 5.8.2 package** (Phase 1). Note the gate is
*not* near the top of the file — the header fields are normalised correctly first
(`scl_slope` of 0/NaN → 1), and the defect is the loop guard at the end.
`nifti-volume-loader/dist/esm/helpers/modalityScaleNifti.js` gates rescaling on
`if (slope !== 1 && inter !== 0)` — must be `||`. A NIfTI with `scl_slope = 1,
scl_inter = -1024` (the common uint16-plus-intercept CT/CBCT encoding) gets **no rescale at
all**; every voxel is off by 1024 HU. `slope = 2, inter = 0` is skipped too. **Decision #5 is
unimplementable on top of this.** Mitigation: derive `modalityLutModule` from the raw header
in our own metadata provider and apply it explicitly in the ROI/probe/stat layer; assert in
the harness and in CI fixtures across all four branches. File upstream.

✅ **Mitigated in Phase 3** (`b90a7f9`), with one correction to the sentence above: the
LUT that must be applied is **not** the header's. Upstream skips two of the four branches
and *applies* the other two, so re-applying the header LUT unconditionally would double
the intercept on the branches it got right. The hazard is not "the rescale is skipped"
but "it is skipped sometimes, and nothing on the volume records which".
`residualModalityLut()` answers that question and is what the metadata provider
registers; identity from it means "already in modality units", never "no rescale
defined". `toStoredValue()` is the inverse, which keeps an absolute preset expressible
against an array that may still be raw. Tier 2 of the harness asserts the whole thing on
real bytes, and the unit tests drive all four branches through a transcription of
`modalityScaleNifti`'s own array selection and buggy gate. See also **F17**, a second
defect in the same code path found while building this.

**F2 — Cornerstone inherits the silent-mirroring hazard `volume_metadata.js` guards.**
`static/js/nifti-reader.js:701-704` fabricates a diagonal RAS affine from `pixDims` when
`qform_code < 1 && sform_code < 1`; `rasToLps()` then converts that fiction into a
confident-looking LPS direction and Cornerstone renders it without complaint — the same hazard
documented at `static/js/volume_metadata.js:6-9` for NiiVue. **`volume_metadata.js` must be
kept and re-wired, not deleted.**

**F3 — the NIfTI loader's `.gz` detection breaks on this repo's URLs, two ways.**
✅ **Confirmed verbatim** and fixed in Phase 1 — see also F14, a third way found while
fixing it.
`createNiftiImageIdsAndCacheMetadata.js` does `new URL(url)`, which **throws** on a relative
path (`/maxillo/api/processing/files/serve/123/`), and tests `pathname.endsWith('.gz')`, which
excludes query strings so `?ext=.gz` cannot help. Fixed by a filename-suffixed serve route plus
absolutising in the imageId builder — **the only server change the frontend migration needs.**

**F4 — esbuild cannot produce IIFE; the module format is forced.** `import.meta` is
unavailable in `--format=iife`, and three vendor packages resolve workers via
`new URL(..., import.meta.url)` at **three different relative depths** (`../workers/` for
tools-compute, `./workers/` for polyseg and interpolation). IIFE kills every worker. ESM output
+ `<script type="module">` is mandatory.

**F5 — `@itk-wasm/morphological-contour-interpolation` defaults to jsdelivr.** Shipping
`labelmap-interpolation` without `setPipelinesBaseUrl()` silently reintroduces a runtime CDN.
Guarded by an automated no-CDN assertion over the emitted bundle.

**F6 — ContextPool is already the v5 default** (`webGlContextCount: 7`). Choose it explicitly
for the grid (4 viewports ≤ 7, so the pool never recycles; per-viewport real canvases match the
DOM/CSS at `viewer_grid.js:1291-1315`). Keep `isMobile` false — it clamps the count to 1.

**F7 — `amip` has no Cornerstone equivalent, and it is the default clinicians look at.**
`niivue_render_modes.js:62-91` is a bespoke Beer-Lambert extinction heuristic with a by-eye
constant, `selected` at `templates/maxillo/patient_detail_content.html:88`. `mip` →
`MAXIMUM_INTENSITY_BLEND` and `shaded` → native vtk.js gradient shading are clean equivalents;
`amip` is not. **Requires sign-off on real studies before deletion.**

**F8 — ~3,000 lines are already dead, and a further ~700 are conditionally dead.**

*Unconditionally dead — no template references them at all, verified by grep:*
`volume_viewer.js` (472, and it defines a **duplicate `window.CBCTViewer`**),
`slice_renderer.js` (487), `volume_interaction.js` (439), `windowing.js` (128),
`maxillo_niivue_viewer.js` (475), `nifti-reader-min.js`. Safe to delete outright.

*Loaded on every page but never invoked:* `static/js/volume_renderer.js` (434) — loaded at
`templates/common/patient_detail.html:29`, but `window.VolumeRenderer` has **zero call
sites** anywhere; it also does a runtime `fetch('/static/shaders/volume_fragment.glsl')`.
That shader file has no other consumer. Safe to delete.

*Conditionally dead — **verify before deleting***:
`static/js/modality_viewers/volume_loader.js` (496) and its only consumer
`static/js/worker/volume_worker.js`. Loaded at `templates/common/patient_detail.html:32`,
**outside** the `{% if ns == 'maxillo' %}` gate, so all three namespaces load it. Its call
site (`static/js/patient_detail.js:863-864`) is
`useLegacyVolumePreload && window.hasCBCT && window.isCBCTProcessed`:

- **maxillo** — `maxillo/views/patient_detail.py:659` sets `'fixedMode': True` and
  `templates/maxillo/patient_detail_content.html:4` renders `viewerGridData`, so
  `useLegacyVolumePreload` flips false. Unreachable.
- **brain** — `brain/views.py:190` sets `"hasCBCT": False`. Unreachable.
- **laparoscopy** — ⚠️ **reachable in principle.** Laparoscopy renders through the same shell
  but `templates/laparoscopy/patient_detail_content.html` does **not** render
  `viewerGridData`, so `useLegacyVolumePreload` stays `true`. `laparoscopy.Patient` defines
  `is_cbct_processed` (`laparoscopy/models.py:252`) and carries a legacy `cbct` FileField
  (`:143`), and `has_cbct` resolves via `patient.get_cbct_raw_file()` +
  `artifact_exists(...)` (`maxillo/views/patient_detail.py:262-268`).

  In practice `setup_laparoscopy_modalities` only creates the `video` modality, so no
  laparoscopy patient should own a `cbct_raw` `FileRegistry` row — but that is a claim about
  *data*, not code. **Gate the deletion on confirming it against the live database:**

  ```python
  from common.models import FileRegistry
  FileRegistry.objects.filter(domain="laparoscopy", file_type="cbct_raw").count()  # expect 0
  ```

  Zero ⇒ delete both files with the rest. Non-zero ⇒ keep them, or drop the load to
  `{% if ns == 'maxillo' %}`-style gating first and re-check.

  **Status: run against production, count is 0, so both files were deleted in Phase 1.**
  The gating logic went with them: with `window.VolumeLoader` gone, the
  `useLegacyVolumePreload` branch in `static/js/patient_detail.js` could never fire, and
  the `viewerGridData` parse there existed only to feed that gate. The analysis in this
  finding is what made the deletion safe rather than hopeful — record the count if it is
  ever re-checked.

**F9 — the file-serving ACL defect.** ✅ **Fixed** on `release/2.0` (`232b40e`). Recorded here
because the reachability analysis matters for Phase 8: `ActiveProfileMiddleware` only inspects
`maxillo`/`brain`/`laparoscopy` path prefixes, so the **global `api` namespace**
(`yggdrasil/urls.py`, used by `templates/common/sections/file_management_section.html`) skips
it and a view's own check is the only gate there. Any new endpoint family must not assume
middleware coverage.

**F10 — the demo is no longer a separate surface.** `common/demo.py:150-171` `demo_index`
**logs the anonymous visitor in as a real user**; writes are blocked by
`DemoGuestReadOnlyMiddleware` and reads narrowed by `folder.is_demo`. **Every new
`@login_required` endpoint is instantly anonymous-public for demo folders.** The 2.0 roadmap's
"fully separate, read-only surface" claim (Phase 7) is stale.

**F11 — two pre-existing holes in the annotation lock.** ✅ **Fixed** in Phase 2
(`47ede3b`), shipped alone as planned.
`maxillo/views/metadata.py:228` `update_nifti_metadata` rewrites a raw CBCT's qform/sform in
place and overwrites `FileRegistry.file_hash`/`file_size`
(`_update_file_identities:143-170`) without consulting `common/annotation_lock.py` — silently
re-basing every landmark and spline drawn on that volume, and the #1 `source_fingerprint`
invalidation vector. And `maxillo/views/classification.py:19` `update_classification` never
calls `project_allows_annotation`, unlike every other annotation write. The affine rewrite now
refuses with 409 *before any object-storage work*, and the classification endpoint accepts
either the `classification` or `bite_classification` slug, matching the form-post path in
`patient_detail`.

**F12 — MySQL silently drops conditional constraints.** Django compiles
`UniqueConstraint(condition=…)` to **nothing** on MySQL (no partial indexes, no error). Every
"exactly one primary/canonical" rule must use a nullable slot column (`primary_slot`,
`canonical_slot`) whose NULLs are distinct.

**F13 — a pre-existing export bug DICOM would inherit.** `ExportProcessor._file_entry`
(`common/export_processing.py:296-319`) calls `artifact_exists(row.file_path)` →
`storage.head(prefix)` → `FileNotFoundError` → the artifact is **silently skipped with a
warning**. This already happens for `save_generic_modality_folder` prefix rows; a DICOM series
export would silently produce nothing.

The four below were found while building against the real packages: F14–F16 in Phase 1,
F17 in Phase 3.

**F14 — the loader appends `?frame=N` with a literal `?`, so a `file_key` URL is
unusable.** Found in Phase 1. `createNiftiImageIdsAndCacheMetadata.js:174` builds each
slice id as ``` `nifti:${niftiURL}?frame=${i}` ``` unconditionally. A serve URL that
already carries a query string therefore yields
`nifti:/…/serve/123/v.nii.gz?file_key=segmentation_nifti?frame=0` — **two `?`**, so
`frame` parses as part of the `file_key` value and every slice resolves to frame 0.
This collides directly with the Phase 1 decision to keep `file_key` a query parameter,
and it matters concretely: a `cbct_processed` row with `file_hash == 'multi-file'` is
addressed as `?file_key=segmentation_nifti` (`maxillo/api_views/files.py:45-65`) and is
a volume Phase 3 must display. Phase 1 does **not** paper over it —
`frontend/imaging/ids/imageIds.js` `assertLoaderSafeUrl()` refuses such a URL with the
reason, and it is unit-tested.

✅ **Resolved in Phase 3** (`8758efa`), though not the way this finding guessed. Rather
than resolving the bundle key from the filename segment — which would have ended the
"`filename` is decorative" contract and would collide whenever two members share a
basename — the key moved into its own path segment:
`.../serve/<id>/key/<bundle_key>/<filename>`, registered in all three serving
namespaces. Additive, so no existing caller changes; `filename` stays decorative on
both forms; and `assertLoaderSafeUrl` is **unchanged** and still refuses a query string.
The constraint was never wrong — it simply had no alternative to point at until the
server grew one. `?file_key=` is kept for existing callers, and a request carrying both
a path key and a query key that disagree is a **400**, not a precedence rule.

**F15 — four `import.meta.url` worker resolutions, at four depths, not three.**
Found in Phase 1 while building. F4 counts three; `itk-wasm` adds a fourth, *nested*
one: `itk-wasm/dist/pipeline/create-web-worker.js:9` spawns
`new Worker(new URL('./web-workers/itk-wasm-pipeline.worker.js', import.meta.url))`
from **inside** the interpolation worker, so it resolves from `app/workers/`.

Worse, the compute-worker specifier resolves from **two** places at once. It is
`'../workers/computeWorker.js'` in both `app/chunk-*.js` (→ `<build>/workers/`, correct)
and `app/workers/polySegConverters.js` — because the polyseg worker imports the
`@cornerstonejs/tools` `utilities` barrel and so carries the string too, resolving to
`<build>/app/workers/computeWorker.js`, which did not exist. Fixed with a one-line ESM
re-export stub rather than a second 3.4 MB copy of the worker. `scripts/check_bundle_assets.mjs`
found this on its first run; it is exactly the class of failure it exists for — dead
worker, clinical viewer, no build error.

**F16 — `allocateScalarData`'s `Int8Array` case under-counts the cache budget by 2×.**
Minor, and ours to report upstream, not to work around.
`modalityScaleNifti.js` `allocateScalarData` sets `bitsAllocated = 8` for `'Int8Array'`
and then allocates `new Int16Array(nVox)`, so `checkCacheAvailable` reserves half the
bytes actually taken. Filed alongside F1.

**F17 — the rescale is applied *in place, into an integer array*, so it can wrap.**
Found in Phase 3, while building F1's mitigation. For the two branches
`modalityScaleNifti` does not skip, it writes `raw * slope + inter` back into the typed
array it chose from the datatype — and that array is often still integral.
`NIFTI_TYPE_INT16` with `scl_slope = 2` stays an `Int16Array`, so a study whose raw
maximum exceeds 16383 wraps silently; `NIFTI_TYPE_UINT16` with a positive integral
rescale stays a `Uint16Array` and wraps the same way. Unlike F1 this cannot be shown
from the header alone — it needs the data range — so it ships as a per-study predicate
(`upstreamRescaleMayOverflow` in `frontend/imaging/metadata/modalityLutModule.js`)
evaluated by the harness rather than as a fixture. The predicate has to mirror
upstream's own array selection and not merely the datatype: a fractional rescale takes
the `Float32Array` branch, and so does a negative one for both unsigned types, so a
datatype-only table would report overflow for volumes that are in fact promoted and
safe. Note that **F16's over-allocation is load-bearing here**: `'Int8Array'` allocates
an `Int16Array`, which is what gives a rescaled int8 volume room to grow. Fixing F16
without fixing F17 would introduce a wrap. File both upstream together.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0.1 | File-serving ACL fix (standalone security PR) | ✅ done (`release/2.0`, `232b40e`) |
| 0.2 | 2.0 close-out: rehearsals recorded, `[2.0.0]` dated | ✅ done (`release/2.0`, `c2e0ec7`); **`v2.0.0` tag still to push** |
| 0.4 | `release/3.0` cut from `release/2.0` @ `cda55df` | ✅ done |
| 0.3 | `docs/cornerstone-future-work.md` | ✅ done (`release/2.0`, `5dbb639`) |
| 1 | Build toolchain + vendored bundle + dead-code deletion | ✅ done (`release/3.0`, `d8ce0df`, `9dd212f`, this commit) |
| 2 | `annotations/` Django app | ✅ done (`release/3.0`, `47ede3b`…`032e639`) |
| 3 | CBCT + brain volume grid | 🟡 in progress (`release/3.0`, `8758efa`…`d7687c4`) — foundation, **the validation harness**, the replacement grid and measurement persistence are in; **nothing is wired to a template yet** and the deletions are gated on a green harness run across both corpora |
| 4 | Photo stacks (teleradiography + intraoral) | ⬜ not started |
| 5 | Intraoral tooth segmentation | ⬜ not started |
| 6 | IOS meshes + landmark tool (Three.js removed) | ⬜ not started |
| 7 | Panoramic live CPR | ⬜ not started |
| 8 | Native DICOM ingestion and serving | ⬜ not started |
| 9 | Interop: SEG / RTSTRUCT / SR / Parametric Map | ⬜ not started |
| 10 | Laparoscopy video (Konva removed) | ⬜ not started |

## Phase 1 — Build toolchain and vendored bundle

> **Shipped.** What follows is the plan as designed; the corrections below record where
> the shipped implementation differs, and why. The whole F8 set is gone — including the
> conditionally-dead pair, whose production `cbct_raw` count came back 0 (see the end of
> this section).
>
> - **Actual emitted layout** (see `scripts/build_frontend.mjs` for the authority):
>   the ICRPolySeg wasm lands in `app/workers/assets/`, not `<build>/assets/`, because
>   it is imported by the polyseg *worker* and esbuild writes a file-loader asset
>   relative to the file that emitted it. `app/workers/computeWorker.js` also exists as a
>   one-line re-export stub — F15.
> - **`<build>` hashes the inputs, not the outputs** (`frontend/**` minus `frontend/tests/`,
>   plus `package-lock.json` and `scripts/build_frontend.mjs`). An output hash would be
>   circular: the app bundle's `publicPath` and the vendored itk pipelines URL both name
>   the build directory. `frontend/tests/` is excluded on purpose — a test edit must not
>   restamp ~20 MB of byte-identical committed output.
> - **`build_frontend.sh` does *not* regenerate `static/js/nifti-reader.js` or
>   `static/js/vendor/fflate-0.8.2.min.js`, and the "freshness gate becomes total" claim
>   below is withdrawn.** fflate is not esbuild output at all — its own header reads
>   `Original file: /npm/fflate@0.8.2/umd/index.js`, i.e. a jsdelivr copy of the prebuilt
>   UMD file. `nifti-reader.js` *is* esbuild output, but of a **custom entry that assigns
>   `window.nifti = {...}`** and was never committed, built from an unrecorded source
>   version. Re-authoring that entry would swap the header parser underneath
>   `volume_metadata.js`, `cbct_convert.js` and the panorex path — a behavioural change to
>   a load-bearing parser, for no benefit, in exactly the silent-hazard class this
>   migration exists to remove. Both files are already self-hosted. Phase 3 revisits the
>   parser when it re-wires `volume_metadata.js` (F2), and can settle the version
>   question then; `@cornerstonejs/nifti-volume-loader@5.8.2` itself depends on
>   `nifti-reader-js@0.6.9`, which is a useful corroboration of the Phase-3 preflight
>   hypothesis but not proof about this file.
> - **Two browser shims were required**: `events@3.3.0` and `url@0.11.4`.
>   `@cornerstonejs/core`'s `Mesh` class imports vtk.js's `XMLPolyDataReader`, which uses
>   `xmlbuilder2` — CommonJS, node builtins, on the actual read path. Real shims rather
>   than throwing stubs: guessing at unreachability inside a mesh reader that Phase 6
>   depends on is the wrong trade for a clinical viewer.
> - **The no-CDN assertion is enforced by *aliasing*, not by calling
>   `setPipelinesBaseUrl()`.** The setter stops the fetch but leaves the jsdelivr literal
>   in the emitted bytes, which would force `check_bundle_assets.mjs` to allowlist a CDN
>   host and gut risk #4's mitigation. `frontend/vendor-shims/itk-pipelines-base-url.js`
>   replaces the vendor module at build time, so the fallback is unreachable by
>   construction and the assertion stays absolute. The URL is still set at runtime — from
>   `import.meta.url` **inside the worker**, since the worker `peerImport`s its own module
>   instance and a main-thread setter would not reach it.
> - **`splitting` cannot separate core+tools.** All five surfaces need them, so the shared
>   chunk is ~4.1 MB and every page pays it. What splitting *does* buy is the intended
>   thing: the NIfTI loader, polySeg, itk-wasm and the CPR mapper stay out of it, so a
>   laparoscopy page does not download the volume/CPR/mesh stack.
> - **`application/wasm` resolves correctly** from Python 3.11's `mimetypes` in the app
>   container, so whitenoise serves the wasm with the right type. One fewer unknown for
>   the "needs a live environment" list.
> - `npm test` runs `node --test <glob>`, not `node --test <dir>`: directory discovery
>   does not work in this Node build.

**`package.json`** (root, dev-only, **no `"type"` field** — this keeps `static/js/**` in the
default CommonJS scope so five of the seven existing `node:test` files survive unedited).
`frontend/package.json` = `{"type": "module"}` scopes the new ESM source. Every Cornerstone
package pinned to `5.8.2` **exactly** — the lockstep peer deps (`core` peers on
`metadata@5.8.2`/`utils@5.8.2`) make ranges dangerous. `package-lock.json` committed.

**Output layout — version-stamped directory, not hashed filenames.** `yggdrasil/settings.py:216-219`
uses non-manifest static storage (the comment at `:214-215` says manifest hashing "would 404
those refs"), so per-file hashes are out. Nothing in a template references the workers, chunks
or wasm — only the bundle does. So hash the *directory*:

```
static/vendor/cornerstone/
  manifest.json                      # {"build": "a1b2c3d4"}  (committed)
  a1b2c3d4/
    workers/computeWorker.js         # resolves ../workers/... from app/
    app/{volume-grid,photo-stack,mesh-landmarks,panoramic-cpr,video-annotate}.js
    app/chunk-*.js                   # FLAT — a nested chunk moves import.meta.url
    app/workers/{polySegConverters,interpolationWorker}.js
    itk/pipelines/                   # vendored — kills the jsdelivr default (F5)
    assets/ICRPolySeg-<hash>.wasm
```

Five entries with `--splitting` (per-surface, not one mega-bundle:
`templates/common/patient_detail.html:37-43` already gates viewer scripts per namespace, and a
laparoscopy page must not download the volume/CPR/mesh stack).

**`scripts/build_frontend.sh`** — added to the `.gitignore` allowlist, together with
`scripts/build_frontend.mjs` (esbuild's JS API is needed for the F5 alias and the F15
worker layout) and `scripts/check_bundle_assets.mjs`. ~~It also regenerates
`static/js/nifti-reader.js` and `static/js/vendor/fflate-0.8.2.min.js`~~ — **withdrawn,
see the note at the top of this section.**

**`scripts/check_bundle_assets.mjs`** — resolves every `new URL(..., import.meta.url)` and
public-path asset string against its emitting file and fails if the target is missing; also
asserts no emitted file contains `cdn.jsdelivr.net` / `unpkg.com` / `cdnjs` (the guard for F5).

**CI** gains a third job (`container: node:22-slim`): `npm ci` → `npm run build` →
`git diff --exit-code` (byte-reproducible via the exact esbuild pin, committed lockfile, no
sourcemaps) → `npm run verify` → `node --test frontend/ static/js/tests/`.
**`npm ci` requires registry egress on the self-hosted runner** — document it beside the
existing `gh` requirement in `CONTRIBUTING.md`.

**Django side:** `common/cornerstone_assets.py` reads `manifest.json` at import (like
`_read_app_version()` at `settings.py:22-30`); `common/templatetags/cornerstone.py` exposes
`{% cornerstone_entry 'volume-grid' %}`, mirroring `common/templatetags/icons.py` +
`common/icons.py`. A missing manifest degrades to a console error, never a 500.

**Server change for F3** — one named route beside `maxillo/api_urls.py:40-44`:
`processing/files/serve/<int:file_id>/<str:filename>`. `file_key` stays a query param; as a
path segment it would break `.gz` detection again.

**Auth:** volume traffic is same-origin `GET`; `fetch` defaults to `credentials:
'same-origin'`, satisfying `SESSION_COOKIE_SAMESITE = "Strict"`. **No CSRF for reads.** Writes
keep the DOM-input pattern (`static/js/modality_viewers/ios.js:608-609`) because
`CSRF_USE_SESSIONS` + `CSRF_COOKIE_HTTPONLY` (`settings.py:248-250`) make the cookie
unreadable — the comment at `static/js/nifti_metadata.js:82` already says so.

**Delete the F8 dead set in this phase** — ~3,000 lines at zero risk, which also removes the
duplicate `window.CBCTViewer` definition before Phase 3 redefines that global. The
conditionally-dead pair (`volume_loader.js`, `worker/volume_worker.js`) needs the
laparoscopy `cbct_raw` count checked against the live database first; see F8.

## Phase 2 — `annotations/` Django app

> **Shipped** (`47ede3b`, `6b6405d`, `8e70c6a`, `accbd09`, `e4edf84`, `156d2f5`,
> `032e639`). What follows is the plan as designed; the corrections below record where
> the shipped implementation differs, and why. Nothing is wired to a view — the surfaces
> are replaced one at a time from Phase 3 on.
>
> - **The coordinate systems, enumerated.** The plan said "the nine, plus
>   `resource_local`" without listing them. Shipped, in `annotations/constants.py`:
>   `patient_lps_mm`, `patient_ras_mm`, `volume_voxel`, `image_pixel`,
>   `image_normalized`, `slice_pixel`, `video_pixel`, `video_normalized`, `none`, plus
>   `resource_local`. **LPS and RAS are separate values**, which is the one that matters:
>   DICOM/Cornerstone is LPS and NIfTI's world frame is RAS, they differ by two sign
>   flips, and a value stored in one and read as the other lands mirrored across the
>   sagittal and coronal planes — F2's hazard, in the data model. `none` is for
>   annotations that genuinely have no geometry (a classification, a caption); a blank
>   would be indistinguishable from an omission.
> - **The legacy conversion is a management command, not a `RunPython`.** The plan's
>   "migrations touch MySQL only" reads as though the MySQL-resident conversion belongs
>   in a migration. It does not: the row counts are unbounded, a migration converting
>   them blocks the deploy and cannot resume after failing halfway.
>   `annotations_convert_legacy` runs row by row in its own transaction, is idempotent on
>   a `legacy:<app>.<table>:<pk>` marker stored in the revision note, and takes
>   `--dry-run`/`--limit`/`--domain`/`--surface`. The schema half stays in migrations,
>   where the additive-only rule and the rehearsal's `migrate --plan` check actually
>   apply; the command emits no DDL. `annotations/migrations/0002` is the one data
>   migration — it seeds the FDI permanent-dentition vocabulary, and `sqlmigrate` prints
>   no DDL for it.
> - **The lock unions both sources for one release**, rather than "the predicate becomes
>   a query on `ever_annotated`" outright. Replacing the legacy checks in the same commit
>   would unlock every patient between `migrate` and the conversion finishing, and would
>   unlock permanently any surface the conversion turns out to miss. Decision #6 already
>   keeps the legacy tables readable for one release as a cross-check; the union is what
>   makes that window safe, and it costs one query. `_legacy_reasons` — and with it the
>   last `from maxillo.models import PanoramicState` in `common` — goes in the release
>   that drops those tables, gated on a clean production `annotations_crosscheck`
>   (risk #19). **The five public signatures are byte-identical as specified**; all nine
>   call sites are untouched.
> - **`AnnotationItemBase.target` is nullable.** An occlusion classification or a voice
>   caption is a statement about the study, and a patient may own no file for it to point
>   at. Geometry and measurements still require one, enforced in
>   `annotations/services/items.py` — coordinates with no resource behind them are
>   numbers.
> - **`MeasurementItem` also carries a nullable `spatial_3d_item` FK.** The plan named
>   only `geometry_2d_item`. A volume ROI statistic in patient space attaches to a 3D
>   shape, and Phase 3 produces those; adding the column now avoids a schema change then.
>   Naming both on one row is refused as ambiguous.
> - **`AnnotationPayload` gained a `variant` column.** "One payload per format per
>   revision" is wrong for the panoramic, which bakes a MIP *and* a ray-sum strip from one
>   arch. Uniqueness is on `(revision, format, variant)`.
> - **A `study_notes` set kind was added.** `laparoscopy.Classification` shares a table
>   name with maxillo's and nothing else — it holds free-text `notes` and no occlusion
>   facets — so filing it under `occlusion_classification` would misdescribe a surgeon's
>   remark in every export. During the cross-check release a laparoscopy patient can
>   report both "study notes" (new) and "an occlusion classification" (the legacy
>   branch's pre-existing mislabel); the second goes with that branch.
> - **`annotations_normalize_coordinates` converts knowledge, not data.** It reads NIfTI
>   headers and records shape, spacing, affine, orientation and the `scl_slope`/`scl_inter`
>   pair on `SourceResource.descriptor`. It never moves a stored coordinate between
>   frames: that is lossy, needs a decision per surface, and doing it silently inside a
>   maintenance command is how a landmark ends up somewhere nobody chose. It also counts
>   the volumes with neither `qform_code` nor `sform_code` set — the F2 population, whose
>   orientation is inferred from pixel dimensions — so Phase 3 starts with a number
>   instead of a hypothesis.
> - **`serializers/` is the canonical JSON document**, with
>   `assert_no_viewer_identifiers` walking it for `annotationUID`, `imageId`, `volumeId`,
>   `segmentationId`, `cachedStats` and friends. The realistic way one gets in is a tool
>   payload stored wholesale into `attributes`, so the check is a walk rather than a
>   top-level key test.
> - **Test count: 680, 0 failures** (from 462 after Phase 1), on MySQL 8 with live
>   object storage. The DDL-level cases in `annotations/tests_model_constraints.py` exist
>   because they would pass vacuously on a backend that ignores `CHECK` — `sqlmigrate`
>   confirms every constraint is emitted, and the tests confirm MySQL enforces it.

**Layout.** `models/ validators/ services/ adapters/ serializers/ management/commands/`.
`validators/` is pure (dict in, `ValidationError` out — no DB, no I/O); `adapters/` is pure
translation; **`services/` is the only writer.** A view that imports a model and calls
`.save()` is a review failure. No DRF — every API in this repo is a plain function view.

**Models** (all `db_table='annotations_*'`): `LabelSchema` / `LabelDefinition`
(`UniqueConstraint(schema, value)` is "an integer `2` in an old labelmap must never change
meaning", in DDL); `SourceResource` (file | dicom_series | dicom_instance | derived_resource |
logical_volume, one unique `identity_key` because of F12, `file` FK **`PROTECT`**);
`AnnotationSet` (`annotation_method` FK is the gating hook; `ever_annotated` is the monotonic
lock flag); `AnnotationTarget` (`primary_slot`); `AnnotationSelector` (every selector declares
`coordinate_system`; times are **integer milliseconds**); `AnnotationRevision`
(`UniqueConstraint(annotation_set, revision_number)` **is** the optimistic-concurrency
primitive — the loser gets `IntegrityError` → 409); `AnnotationPayload` (**one-to-many**,
`canonical_slot`, plus a `png_render` format for the panoramic previews);
`Geometry2DItem` and `SpatialAnnotation3DItem` (**kept separate** — disjoint invariants);
`MeasurementItem` (nullable `geometry_2d_item` FK; `is_calibrated` so a pixel length is never
reported as millimetres); `TemporalAnnotationItem`; `EventAnnotationItem`.

**Coordinate systems** — the nine, plus **`resource_local`**: IOS landmarks come from
`worldToLocal` against a mesh and have no patient frame; calling them `patient_world` would be
false.

**Migrations touch MySQL only** — object-storage I/O is unrunnable in CI and unresumable on
failure, so all bytes-reading work is a management command
(`annotations_materialize_landmarks`, `annotations_normalize_coordinates`,
`annotations_crosscheck`).

**Rewritten `annotation_lock.py`** — keep the module path and all five public signatures
**byte-identical** (nine call sites across six files). The predicate becomes a query on
`ever_annotated`, replacing the hardcoded exemptions at `:88-90` and **removing
`from maxillo.models import PanoramicState`**, so `common` stops importing a domain app.

**Fix F11 in this phase, shipped alone.** ✅ done (`47ede3b`).

## Phase 3 — CBCT + brain volume grid (largest, riskiest)

> **In progress** (`8758efa`, `b90a7f9`, `831f9a2`). The foundation and the validation
> harness are shipped; the viewer replacement and the deletions are not, and are gated
> on a green harness run across the maxillo *and* brain corpora. Nothing is wired to a
> template yet — `templates/common/patient_detail.html` still loads NiiVue and
> `viewer_grid.js`. What follows is the plan as designed; the corrections below record
> where the shipped part differs, and why.
>
> - **Preflight passed.** `static/js/nifti-reader.js` *is* built from
>   `nifti-reader-js@0.6.9`: the `NIFTI1.readHeader` bodies are byte-identical to the
>   released bundle apart from the module-namespace prefix a different bundler emits
>   (`Utils.` vs `utilities_1.Utils.`). Tier 1 therefore compares one header parser
>   against itself, not two parsers against each other. Re-run the diff if the pin moves.
> - **F14 is resolved by a new route, not by re-reading the filename.** The roadmap
>   guessed "resolve the bundle key from the filename segment server-side, which would
>   change the `filename` is decorative contract". Shipped instead:
>   `.../serve/<id>/key/<bundle_key>/<filename>`, registered in all three serving
>   namespaces. It is additive, so every existing caller keeps its contract; it cannot
>   collide when two bundle members share a basename, which a basename lookup would; and
>   `filename` stays decorative on both forms rather than becoming load-bearing on one.
>   `?file_key=` is untouched, and when both are present and disagree the request is a
>   **400** rather than a precedence rule — the two names point at different volumes, and
>   the failure being prevented is a viewer rendering the segmentation while every label
>   says it is showing the volume.
> - **F1's mitigation needed a second half the roadmap does not name.** "Derive
>   `modalityLutModule` from the raw header and apply it explicitly" is only right for
>   the two branches upstream skips. For the other two, upstream *does* apply the
>   rescale, and applying the header LUT again would double the intercept. The operative
>   hazard is therefore not "the rescale is skipped" but "it is skipped *sometimes*,
>   with nothing on the volume recording which". `residualModalityLut()` is what a
>   consumer of real values must ask; identity means "already in modality units", not
>   "no rescale defined".
> - **A new finding, F17, fell out of that.** `modalityScaleNifti` applies its rescale
>   **in place, into the integer array it just allocated**, so `NIFTI_TYPE_INT16` with
>   `scl_slope = 2` and a raw maximum above 16383 wraps silently. Unlike F1 it needs the
>   data range to demonstrate, so it ships as a per-study predicate
>   (`upstreamRescaleMayOverflow`) rather than a fixture. The predicate mirrors
>   upstream's own array-selection branches, not just the datatype — a datatype-only
>   table reports overflow for volumes that are in fact promoted to `Float32Array` and
>   safe. F16's over-allocation turns out to be **load-bearing** for the INT8 case and
>   is deliberately not "corrected".
> - **The harness is three-legged, not a viewer-versus-viewer diff.** The roadmap says
>   "NiiVue `frac2mm` (RAS) vs Cornerstone `indexToWorld` (LPS)". Taken literally that
>   is a pairwise comparison, which reports agreement when both stacks are wrong the
>   same way — precisely the F2 population, where both consume the same fabricated
>   affine. Shipped with the file's own affine as the **reference** leg and both viewers
>   measured against it, so an F2 study reports *agreement plus a warning* instead of a
>   plain green. The roadmap's own "pure affine maths cannot catch a mirroring bug
>   introduced downstream" is the same observation from the other end.
> - **NiiVue's index space is not the file's, and the roadmap does not mention it.**
>   NiiVue reorients every volume to RAS on load. Feeding the same `(i, j, k)` to
>   `frac2mm` and to `indexToWorld` therefore compares two different voxels — and
>   agrees by accident on any volume already stored RAS, which is most of them. The
>   permutation is undone from `nvImage.permRAS`, whose convention was read off the
>   shipped 0.69.0 (`dimsRAS = [dims[0], dims[perm[0]], …]`, so output axis `j` reads
>   input axis `perm[j] - 1`, negated when flipped). The flip must use the **source**
>   axis length: using the output axis's is silent on a cube and wrong on every real
>   CBCT. `frac2mm` is called with `isForceSliceMM = true`, or it uses `frac2mmOrtho` —
>   the orthogonalised slice matrix, which is not an oblique volume's true world mapping.
> - **Sampling is seeded.** The roadmap says "~10⁴ pseudo-random voxel indices" without
>   saying reproducible. A gate whose samples cannot be reproduced reports "green once,
>   on voxels nobody can name": a failure cannot be re-examined at the index that failed,
>   and two runs' numbers cannot be compared. `mulberry32` with a committed default seed,
>   and the eight corners plus the centre are pinned before the random fill — an
>   off-by-one at the far edge is exactly what an interior draw misses.
> - **`volumeRange`'s inputs could not be ported, only its logic.** The roadmap says
>   "reuse `niivue_render_modes.volumeRange`'s robust min/max logic (and port its test)".
>   Its `robust_min`/`robust_max` were computed by *NiiVue*, inside the library being
>   deleted; only the fallback chain is ours to keep. That chain is reproduced decision
>   for decision — including the widening that stops a 99%-air volume opening on a window
>   which clips the anatomy entirely — and `robustRange()` computes the percentiles here,
>   by histogram rather than sort because a CBCT is 10⁸ voxels. The ported test asserts
>   the real-value answer *and* re-derives the percentages the old one asserted, so a
>   drift between them shows up as a failure rather than as a rewrite.
> - **Presets are CT-only, deliberately.** Decision #16 says "absolute HU presets only
>   where meaningful". Shipped: `MODALITY_PRESETS` has a `ct` table and nothing else.
>   CBCT greyscale is not calibrated Hounsfield — the same anatomy reads differently
>   between vendors and between fields of view on one machine — so a CBCT preset would
>   be a number that looks authoritative and is not. The per-`Modality` admin
>   configuration half of #16 lands with the viewer, since it needs a migration.
> - **The harness page is staff-only, not project-admin.** `panoramic_warmup` — the
>   pattern it follows — gates on project admin. That is not enough here: F10 records
>   that `demo_index` logs anonymous visitors in as a real user, and this page
>   enumerates raw volume URLs across domains. The demo guest is a non-staff user, so
>   `is_staff` is a gate the demo path cannot reach at all, and the test asserts it
>   against the actual seeded guest rather than reasoning about it.
> - **Two bugs were found by writing the harness's own tests**, both in code written
>   minutes earlier, and both of the class the harness exists to catch:
>   `checkAnalyticLengths` walked its 64-voxel run off the end of any axis shorter than
>   66, skipped every check, and reported failure for a *correct* viewer; and the
>   fixture for "catches a transposed direction matrix" was accidentally symmetric — a
>   rotation purely about x comes out of `rasToLps` as a symmetric matrix whose
>   transpose is itself — so that test passed vacuously. The second one is why the
>   fixture now asserts its own asymmetry first.
> - **Measurements persist, and the number the store keeps is not the number the
>   viewer reported.** The "Why" table opens with "one measurement tool exists, and it
>   is never saved"; `annotations/adapters/cornerstone.py` closes it. Every value is
>   recomputed from the handles, because `cachedStats` is Cornerstone's own cache and
>   is stale between edits *by design* — a store that trusted it would record a number
>   that disagrees with the shape beside it, and the serializer already refuses a
>   document containing one. Two handle conventions would have been wrong by guess:
>   `Angle`'s vertex is the **middle** handle (`AngleTool.js:411-418`), and
>   `RectangleROI`'s corners are stored (BL, BR, TL, TR), so a shoelace walked in index
>   order traces a bow-tie whose signed area cancels **exactly** — a 4×3 ROI would be
>   recorded as 0 mm² with nothing about the shape looking wrong.
> - **Intensity statistics are refused rather than accepted from the client.** A
>   probe's Hounsfield reading is not derivable from geometry; it needs the voxels.
>   Decision #11 puts ROI statistics in the first release, and they belong to a
>   server-side pass that reads the volume — **still outstanding**, and the one part of
>   #11 this phase does not deliver. The shape is stored; the number is not invented.
> - **Saving is replace-the-set, not a diff.** A revision *is* the state of the work at
>   a moment. Diffing would need a stable per-annotation identity, and the only
>   candidate is the `annotationUID` — the identifier the governing rule says is never
>   persisted. Deleting a measurement is an empty save, which records the deletion
>   instead of erasing the work.
> - **A Frame of Reference UID is dropped outside patient space.** Cornerstone attaches
>   it to every annotation; in `volume_voxel` or `resource_local` it is a false claim of
>   comparability with any other series carrying the same UID, and a later fusion would
>   trust it. Found because Phase 2's validator refused the row — the adapter now agrees
>   with the validator rather than working around it.
> - **Everything in the harness is temporary** and is deleted with the viewer
>   replacement: `frontend/entries/volume-validation.js`,
>   `frontend/imaging/validation/`, `common/imaging_validation.py`,
>   `templates/common/imaging_validation.html` and the `@niivue/niivue` devDependency.
>   The bundle entry is the only place in the tree that vendors NiiVue.
> - **Test count: 197 frontend (`node --test`, from 97) and 16 new Django tests**, 0
>   failures. The fixtures are real NIfTI-1 files written byte by byte and round-tripped
>   through `nifti-reader-js`: a mock header object would skip the parser, and the parser
>   is part of what is being validated.

**Gate: the validation harness must be green across the maxillo *and* brain corpora before
this merges.** With no feature flags, this pre-merge gate *is* the safety net.

Deletes `viewer_grid.js` (2267), `niivue_viewer.js` (760), `niivue_render_modes.js` (544 — and
with it the `VERSION = '0.69.0'` pin and the three shader anchor strings),
`maxillo_cbct_grid_adapter.js` (417), two test files, both NiiVue CDN tags, and the percent
Level/Window sliders.

Windowing per decision #16: **reuse `niivue_render_modes.volumeRange`'s robust min/max logic**
(and port its test) for the per-volume auto-VOI.

### Validation harness — `frontend/imaging/validation/`

Follows `maxillo/views/panoramic_warmup.py:1-14`: an admin-gated page driving real studies
through the real code path. NiiVue 0.69 temporarily **vendored** for the comparison.

- **Tier 1 — geometry (exact; blocks deletion).** Axcodes from both paths through the same
  function, including the `hasMetadata: false` case. ~10⁴ pseudo-random voxel indices: NiiVue
  `frac2mm` (RAS) vs Cornerstone `indexToWorld` (LPS) after the `rasToLps` x/y negation,
  `max|Δ| < 1e-4 mm`. Dims, spacing, direction cosines orthonormal to 1e-6. **Analytic length
  check** between two voxel centres whose separation is computable from the affine, to 1e-6.
  **Chirality** — a synthetic blob at a known RAS position plus real studies with a known-side
  finding (pure affine maths cannot catch a mirroring bug introduced downstream of the affine).
  A deliberately-broken `qform_code = sform_code = 0` fixture.
- **Tier 2 — intensity (catches F1).** Histogram and percentiles of the cached scalar data vs
  values computed independently with `raw*scl_slope + scl_inter` applied unconditionally.
  Fixtures for `(1,-1024)`, `(2,0)`, `(1,0)`, `(0.5,-100)`.
- **Tier 3 — appearance (human-reviewed, not a gate).** Side-by-side at matched camera and VOI;
  a contact sheet for mip/amip/shaded vs replacements.

*Preflight:* confirm `static/js/nifti-reader.js` is built from `nifti-reader-js@0.6.9`, or
Tier 1 compares two different header parsers.

## Phases 4–10 (summary)

- **4 — Photo stacks.** `webImageLoader.js` (~60 lines, scheme `ygg-web:`). **No
  `pixelSpacing` unless known** — Cornerstone then reports `px` and labels it uncalibrated;
  never fabricate 1 mm/px. Calibration persisted in `FileRegistry.metadata['pixel_spacing_mm']`
  (a JSONField already used for per-file data — **no migration**).
- **5 — Intraoral tooth segmentation.** FDI-keyed polygons ↔ `Contour` representation. The
  `labelMapper` FDI↔segmentIndex table is load-bearing and must be exhaustively unit-tested.
  Preserve the operation-based undo/redo — implemented over the *Yggdrasil* representation, not
  Cornerstone state — and the edit-operation replay at `intraoral_segmentation.js:465-495`.
  Segmentation accelerators land here.
- **6 — IOS meshes.** `cornerstoneMeshLoader` + `MeshType.STL` in a `VolumeViewport3D`;
  `TrackballRotateTool` replaces `THREE.TrackballControls`. Deletes `ios.js` (1539) and
  `templates/base.html:36-39`. **Sitewide change** — grep for `THREE.` in the same PR.
- **7 — Panoramic live CPR.** Interactive layer → Cornerstone + vtk.js `ImageCPRMapper`.
  **Baking layer unchanged**: `seg2pano_core.js` and `worker/seg2pano_worker.js` survive
  verbatim, so the exported PNGs (`common/export_catalog.py:232-241`) keep their bytes.
  **Port `cbct_panorex_editor.test.js`, do not delete it** — it locks the never-on-load and
  unattended-`autoMode` behaviours that `panoramic_warmup.js` depends on.
- **8 — Native DICOM.** Catalog in `common/` (not `annotations/`, which would be a cycle).
  **One `FileRegistry` row per series, `file_path` = a prefix** — already the house pattern at
  `maxillo/file_utils.py:369-435`. `DicomSeries.sealed_at` because the annotation lock guards
  rows, not instances. `pydicom` with `stop_before_pixels=True`. De-identification at ingest,
  verified by a sentinel test, a runtime `assert_no_phi` that aborts the transaction, and a
  nightly `SystemCheck`. Serving is a **minimal DICOMweb WADO-RS subset** (`wadors:`), because
  the v5 adapter API assumes per-frame imageIds. Demo gate per F10.
- **9 — Interop.** Browser for import (it has the registered volume); **server authoritative**
  for export (`common/export_processing.py` has no browser). Adds `highdicom`. **Fix F13** and
  add a regression test asserting a default export still contains
  `panoramic/generated/panoramic_mip.png` (decision #8).
- **10 — Laparoscopy video.** `VideoViewport` + video segmentation tools. Keeps
  `_timeline.js`, `_api.js`, `_worker.js`, `_magic.js`. Eraser becomes destructive
  (decision #14); NPZ export stays byte-compatible but is regenerated from labelmaps
  (decision #15) — **prove byte-equivalence on existing studies during migration.**

## Verification

**CI:** `ruff check .` → `makemigrations --check --dry-run` **empty** → `migrate` → full
Django suite on MySQL 8 + Redis 7 → **new frontend job** (`npm ci`, build,
`git diff --exit-code`, `npm run verify`, `npm test`). Shipped in Phase 1; the frontend
job installs `git` *before* `actions/checkout`, because without it checkout falls back to
a tarball and there is no repository for `git diff` to compare against.

**JS tests.** Five of the seven existing files survive with **zero edits** (root
`package.json` has no `"type"` field). `niivue_render_modes.test.js` and
`maxillo_cbct_grid_adapter.test.js` die with their modules — port the robust min/max coverage
into the preset code. `cbct_panorex_editor.test.js` is **ported, not deleted**.

**New JS coverage — Tier A** (pure logic, `node --test`, no DOM): `coordinateMapper`,
`labelMapper`, `toolMapper`, `from/toYggdrasil*` round-trips asserting `annotationUID` and
`cachedStats` never appear in output, **`imageIds.js` `.gz` and absolute-URL invariants**
(highest value per line — both failure modes surface far from their cause),
`modalityLutModule` across all four slope/intercept branches (so F1 cannot regress in),
`history.js` undo/redo.

**Tier B** (Playwright, **numeric not pixel** — screenshot diffs on headless SwiftShader are
driver-sensitive and a flaky required gate gets disabled): `LengthTool` matching the analytic
answer to 1e-6; `worldToCanvas` round-trip to sub-pixel; `imagePlaneModule` values and slice
index at a known crosshair world position; VOI after a preset; `getPixelData()` at a known
voxel. `workflow_dispatch` + nightly, promoted to required after weeks green.

**The frozen runner contract** (`maxillo/tests_runner_api.py`) must pass **untouched** at every
phase. If it fails, the change is wrong.

### Running the suite where object storage is live

Five tests exercise object storage and **fail in any environment without it** (they attempt
`http://garage:3900` and error). On a machine with live Garage/MinIO configured via
`OBJECT_STORAGE_*` they should **pass** — treat a failure there as a real signal, not
environmental noise:

```
brain.tests_export_catalog.BrainFolderRelationTests.test_collect_files_uses_the_brain_patient_fk
maxillo.tests_export_catalog.ExportProcessorSelectionTests.test_only_the_selected_panoramic_variant_is_collected
maxillo.tests_cbct_upload_contract.CBCTUploadContractTests.test_save_cbct_to_dataset_records_orientation_metadata
maxillo.tests_bulk_upload.BulkUploadViewTests.test_a_rejected_file_leaves_no_patient_and_does_not_abort_the_batch
maxillo.tests_bulk_upload.BulkUploadViewTests.test_each_file_becomes_one_patient_named_after_it
```

Baseline for comparison, measured on `release/2.0` at `232b40e` **without** object storage:
425 tests, these 5 failing; with the ACL fix's tests, 444 tests and the same 5. See
[running.md](running.md) and [setup.md](setup.md) for bringing the stack up.

**Measured on `release/3.0` at `cda55df` *with* live object storage** (the dev stack's
Garage, via `docker compose -f docker-compose.dev.yml`): **444 tests, 0 failures** — all
five of the above pass, confirming they are environmental and not latent bugs. After
Phase 1: **462 tests, 0 failures** (+9 `common.tests_cornerstone_assets`,
+9 `common.tests_file_serve_acl`), and 87 JS tests in one `npm test` invocation. After
Phase 2: **680 tests, 0 failures** (+6 `maxillo.tests_annotation_gates`, +9
`common.tests_raw_data_lock`, +203 across the eight `annotations.tests_*` modules), in
~5.5 minutes.

**Prod-clone rehearsal** (before Phases 2 and 8): restore the dump → read `migrate --plan` and
confirm every operation is `CreateModel`/`AddField`/`AddIndex`/`AddConstraint`/`RunPython` →
`sqlmigrate annotations 0002` **must print no DDL** → migrate → `annotations_crosscheck`
reports the pre-conversion gap → `annotations_convert_legacy` → `annotations_materialize_landmarks`
→ `annotations_crosscheck` exits 0 → full suite green → **lock-state diff**: snapshot
`raw_data_is_locked(p)` for every patient before and after, and require every newly-locked
patient to be explained by an annotation that already existed. *That diff is the single most
important check in the rehearsal.* Note that the Phase-2 lock unions both sources, so the
expected diff is **empty** — a patient that gains a lock has been locked by the conversion,
which means the conversion assigned a human origin to something that is not human work.

**Still to run against a production clone.** The rehearsal above has not been performed;
Phase 2 was verified against the dev stack only. The conversion commands have never seen
production row counts, and the F2 population size that `annotations_normalize_coordinates`
reports is unknown.

**Needs a live environment:** a real 400–800 instance DICOM series through ingest (wall clock,
peak RSS, gunicorn headroom); Cornerstone in a real browser against real nginx (wasm MIME,
same-origin worker loading, cookie propagation, 600-slice time-to-first-image); export
streaming of a bundle containing a full DICOM series under gunicorn.

## Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | **F1 — silently wrong HU** | Own `modalityLutModule` + Tier-2 harness + CI fixtures across all four branches; file upstream |
| 2 | **F7 — `amip` has no equivalent**, and it is the default | Explicit sign-off on real studies before deleting `niivue_render_modes.js` |
| 3 | **Worker/wasm URL resolution** at ~~three~~ **four** depths, un-rewritten by esbuild | ✅ discharged in Phase 1 — fixed layout + `npm run verify`, which caught a real broken path on its first run (F15) |
| 4 | **F5 — itk-wasm's jsdelivr default** | ✅ discharged in Phase 1 — build-time **alias** (not just `setPipelinesBaseUrl`), so the no-CDN assertion can stay absolute |
| 5 | **Bundle-freshness gate needs byte-reproducible esbuild** | ✅ discharged in Phase 1 — verified: consecutive builds are byte-identical (exact pin, committed lockfile, no sourcemaps) |
| 6 | **`npm ci` needs registry egress** on the self-hosted runner | Documented in `CONTRIBUTING.md` beside the existing `gh` requirement; **still to be provisioned on the runner** |
| 7 | **No feature flags** makes the harness gate load-bearing | If the corpus is too large, sweep every folder with confirmed annotations plus a random sample |
| 8 | **F10 — demo guests reach every new endpoint** | `is_demo_guest → 404` behind `DICOM_DEMO_ENABLED`; nightly verification green before flipping |
| 9 | **De-identification is header-only** | Refuse burned-in / Secondary-Capture; record `deid_confidence`; never claim more in the UI |
| 10 | **`discard_raw` bricks the DICOM viewer** (the raw row *is* the viewer source) | Guarded at ingest *and* in `ProcessingStep.clean()` |
| 11 | **The lock is blind to per-instance mutation** | `sealed_at` + `DicomInstance.save()` refusal + `_lock_reasons` learning about `annotations` |
| 12 | **Two SEG writers (dcmjs + highdicom) can disagree** | Cross-writer mask/geometry equivalence test + a committed browser-generated fixture |
| 13 | **highdicom RTSTRUCT is less exercised than its SEG writer** | Verify before shipping; SEG/SR first |
| 14 | **`validate_labelmap` is the only server-side voxel read** | Budget check *before* reading, from the already-validated header |
| 15 | **F13 — prefix rows silently export nothing** | `series` entry type + the panoramic-PNG regression test |
| 16 | **`DicomUidMap` is a re-identification vector** | Never in admin, never exported, HMAC key in env, documented as safely droppable |
| 17 | **Decision #18 removes an escape hatch** (delete no longer unlocks) | Superuser override exists; stamp `metadata['lock_override']` on bypass so a later fingerprint mismatch is explainable |
| 18 | **Decision #15 must preserve NPZ bytes** | Prove byte-equivalence on existing studies during migration; both frozen tests unchanged |
| 19 | **Dropping legacy tables is non-additive** | Its own release, gated on a clean prod `annotations_crosscheck`; rollback is the Phase-2 backup |
| 20 | **Additive-only re-anchors on v2.0.0** | Once `v2.0.0` is tagged it replaces `v1.9.0` as the rollback reference and as the dump every later migration must apply onto |
