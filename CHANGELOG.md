# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Native DICOM ingestion and serving (roadmap Phase 8): an uploaded DICOM stays
  DICOM.** Until now a DICOM folder was converted to a single `.nii.gz` in the browser
  and the original was **discarded** — the server had no DICOM reader, and
  `save_cbct_folder_to_dataset` existed only to say so. The series is now stored as it
  arrived, de-identified but not transformed, cataloged, served back to the viewer, and
  exported. Adds `pydicom` and `@cornerstonejs/dicom-image-loader`.
  - **De-identification is a whitelist, not a blacklist**, and that is the whole design.
    The stored dataset is *rebuilt* from an explicit keep-list of the ~35 attributes the
    geometry, the modality LUT and the decoder actually need; every other element, and
    every private block, is dropped. A blacklist cannot be shown to be complete —
    DICOM has thousands of standard attributes and vendors add their own freely — so an
    unknown element is dropped by default and `assert_no_phi()` is a real assertion
    rather than a spot check. It runs against the **exact byte buffer handed to object
    storage**, and a leak aborts the transaction. Burned-in annotation and Secondary
    Capture are *refused*, not stored-and-flagged, because header-only de-identification
    cannot make them safe; `deid_confidence` records that the pixels were never read,
    and nothing may report more than it says.
  - **No `DicomUidMap` table.** The roadmap planned one and its own risk register called
    it "a re-identification vector … safely droppable". A table that is safely droppable
    is not load-bearing, so UIDs are instead *derived* — `HMAC(DICOM_UID_HMAC_KEY,
    original)` under the ISO `2.25` arc. Re-ingesting a study is idempotent for free, a
    duplicate upload is detectable rather than silently forking the study, and the
    vector stops existing instead of being mitigated.
  - **One `FileRegistry` row per series with a prefix `file_path`** — the shape
    `save_generic_modality_folder` has always written — so every consumer of a prefix
    row handles a series unchanged. `DicomSeries` / `DicomInstance` catalog it (one
    additive migration).
  - **`sealed_at` closes a gap the annotation lock cannot see.** The lock guards
    `FileRegistry` *rows*: one row, one file. A series is one row holding hundreds of
    objects, so rewriting instance 137 in place would re-base every coordinate drawn on
    the volume without touching the row. A series is sealed at the same moment
    `ever_annotated` is set — and, for the same reason, a *prediction* does not seal it.
  - **The viewer renders DICOM through the same path as NIfTI.** `dicomSeriesHeader`
    states a series in the terms the grid's geometry and windowing layer already reads,
    so the volume cache, the VOI, the orientation overlay and every measurement tool are
    shared rather than duplicated; the branch is two lines in `loadVolumeIntoWindows`.
    Reading the shipped loader rather than assuming settled three things that fail
    silently: the transfer syntax comes from the **response Content-Type** (so the frame
    endpoint states it explicitly — without it a JPEG Lossless CBCT renders as noise
    with no error); `preScale` is on by default, so DICOM voxels arrive already in
    modality units and the residual LUT is correctly identity, with none of NIfTI's F1
    ambiguity; and decoding runs in a web worker that `register()` does **not** install,
    so the lighter call would have downloaded every series correctly and then never
    decoded it.
  - **`npm run verify` earned its keep again.** The decoders reference their wasm as
    `new URL('@cornerstonejs/codec-charls/decodewasm', import.meta.url)` — a *bare
    package specifier* inside `new URL`, which esbuild copies through untouched and which
    can never resolve at runtime. Caught by the asset checker, fixed through the loader's
    own `wasmBasePath` hook, and the checker now verifies the replacement exists rather
    than ignoring the reference.
  - **Fixes F13, a pre-existing export bug.** `ExportProcessor._file_entry` called
    `artifact_exists(prefix)`, which heads a key that does not exist, raises, and skips
    the artifact **with a warning and no error** — so folder uploads have always
    exported as nothing at all. A `series` entry type expands a prefix row into its
    members. A regression test asserts the panoramic MIP and ray-sum still reach the ZIP
    (decision #8).
  - **`discard_raw` is refused for a modality holding DICOM** (risk 10), at ingest and
    in `ProcessingStep.clean()`: for a DICOM CBCT the raw series may be the only volume
    there is, so the flag would blank the viewer for a whole cohort while the bytes sat
    in storage. A nightly `SystemCheck` re-reads a sample of stored instances and
    re-asserts the whitelist, recording the pseudonymous UID of any offender and never
    the offending value.
  - The DICOM half of `cbct_convert.js` and its worker is deleted (255 lines); the
    MetaImage and NIfTI-repair halves **stay**, because `.mha` and the `.nii.gz`
    orientation repair still need them. Two behaviours of the deleted converter are not
    preserved because neither was correct: it threw every slice in a folder into one
    volume regardless of `SeriesInstanceUID`, and it refused compressed pixel data
    outright — which made an ordinary JPEG-Lossless CBCT un-uploadable.
- **`annotations`, a new Django app: Yggdrasil's durable annotation model.** Thirteen
  models behind a strict layering — `validators/` is pure (values in, `ValidationError`
  out, no database), `adapters/` is pure translation, and `services/` is the only writer.
  No Cornerstone runtime identifier is ever persisted: `SourceResource.identity_key` is
  Yggdrasil's own durable name for annotatable content. Nothing is wired to a view yet;
  the viewers are replaced one surface at a time in later phases.
  - Three invariants are enforced in DDL because they fail invisibly:
    `UniqueConstraint(annotation_set, revision_number)` *is* the optimistic-concurrency
    primitive (a stale writer gets an `IntegrityError` → 409, with no read-then-write
    window because the check is the write); "exactly one primary target" and "exactly one
    canonical payload" use nullable slot columns rather than conditional constraints,
    which MySQL compiles to nothing with no error; and a millimetre measurement requires
    `is_calibrated`, so a length taken on an uncalibrated photograph is reported in
    pixels rather than as a physical claim the image cannot support.
  - Coordinate frames are named on every selector and every geometry row, with LPS and
    RAS as separate values — they differ by two sign flips, so conflating them mirrors a
    landmark across two planes with nothing in the record to say so. IOS landmarks get
    `resource_local`, because they come from `worldToLocal` against a mesh and have no
    patient frame.
  - Times are integer milliseconds throughout.
- Four conversion commands. `annotations_convert_legacy` converts the MySQL-resident
  legacy annotations (idempotent, resumable, `--dry-run`);
  `annotations_materialize_landmarks` handles the one surface that lives in object
  storage; `annotations_normalize_coordinates` records each volume's grid facts from its
  NIfTI header and counts the volumes whose orientation is inferred rather than read;
  `annotations_crosscheck` is read-only and exits non-zero on any legacy row without a
  converted counterpart, or any resource whose bytes changed after being annotated.
- Frontend build toolchain for the Cornerstone3D v5 migration: npm + esbuild,
  **dev-only**, with the emitted bundle committed under
  `static/vendor/cornerstone/<build>/`. Deploys need no Node and make no network
  request. `scripts/build_frontend.sh` builds it, `scripts/check_bundle_assets.mjs`
  (`npm run verify`) asserts every web-worker and wasm URL resolves against its own
  emitting file, and a new CI job rebuilds and fails on any diff. `{% cornerstone_entry 'volume-grid' %}` loads a
  surface. Five per-surface entries exist; **no template loads one yet** — the viewers
  are replaced one at a time in later phases.
- `api_serve_file_named`: `…/processing/files/serve/<id>/<filename>` beside the existing
  route, in all four namespaces. Cornerstone's NIfTI loader decides whether to gunzip by
  testing the URL *pathname* for a `.gz` suffix, which a query parameter cannot carry.
  Same view, same ACL; the filename segment never takes part in resolving the file.

- **The panoramic reconstructs live, on Cornerstone3D and vtk.js.** The arch is edited on
  a real axial viewport -- pan, zoom and draggable control points come with the tool
  instead of being rewritten -- and the strip reformats continuously through
  `ImageCPRMapper` as the arch moves, where it used to appear only after a full CPU pass.
  `static/js/modality_viewers/cbct_panorex_editor.js` (924 lines of Konva) is deleted, and
  with it the last consumer of the `window.ViewerGrid` bridge Phase 3 built to keep it
  alive; the panoramic reads the CBCT out of the Cornerstone cache the grid already filled,
  so there is no second fetch, no second decode and no interface between the two surfaces
  to fall out of step.
  - **The strips that are saved are the same bytes they always were.**
    `static/js/seg2pano_core.js` and `static/js/worker/seg2pano_worker.js` are untouched,
    and the reader approves the *baked* strip rather than the live one: the reformat
    follows the arch while it is being dragged, and the CPU bake replaces it the moment
    the arch settles. The two are not the same image -- the ray-sum is a clipped
    non-negative sum where vtk's nearest equivalent is an average -- so the difference is
    kept off the moment of decision instead of hidden.
  - **The drawn arch is the arch the projection follows.** Cornerstone's Catmull-Rom is
    uniform; the one the baker fits is centripetal, and an arch's control points are
    unevenly spaced by construction, so the two disagree along the whole curve. A small
    spline subclass reproduces the baker's, and its test compares against
    `seg2pano_core.js` itself and pins the upstream methods it stands on, so a Cornerstone
    bump that renames one fails the build rather than quietly redrawing every arch.
  - The mandible mask is drawn as an actor in the volume's own frame rather than as a
    canvas positioned over the viewport, so nothing has to keep two files' idea of where
    the slice is in agreement.

- **The panoramic arch writes through `annotations/`.** Its own set kind, a slice
  selector carrying the axial index -- a spline is a list of `[x, y]` pairs inside *one*
  slice, and without the index it is a curve nobody can place -- and the same conversion
  `annotations_convert_legacy` uses, as one function, so the converted and the live rows
  cannot drift apart and `annotations_crosscheck` keeps comparing like with like. The two
  baked strips ride along as `png_render` payloads on the revision, which is what connects
  the exported PNGs to the geometry they were produced from; neither is canonical, because
  an image is not the truth about a curve. `maxillo.PanoramicState` is no longer written.
  - **An automatic arch is machine output, and never freezes a case.** The warm-up page
    generates one for every patient in a folder without anybody looking. Filed as human
    work it would have locked the raw data of a whole cohort -- monotonically, so nothing
    would have thawed it. An `auto` arch is recorded with a prediction origin and an
    edited one is not, which is the distinction the converter already made from the same
    field.
  - **A replaced CBCT is noticed rather than cleaned up after.** Every revision is stamped
    with its targets' content hashes, so rewriting a volume's affine makes the stored arch
    stop describing it, all by itself; the editor is handed revision 0 and starts again.
    The arch is kept, because it is the record of what the exported strips were baked
    from. The arch also names the *segmentation* it was fitted to, so a re-run mask is
    visible the same way.
  - **One reader for "which panoramic is current".** Three view modules each compared the
    same seven source fields by hand -- the page payload, the strip-serving endpoint and
    the save. They now share `maxillo.views.panoramic_state`, and the comparison is one
    line of it.

- **Teleradiography renders through Cornerstone3D**, replacing an `<img>` that could not
  measure anything. Lengths are honest about their unit: `px` until somebody calibrates
  the image against a known distance, `mm User` afterwards, and never a fabricated
  millimetre -- the metadata provider *omits* `pixelSpacing` rather than defaulting it,
  which is what makes Cornerstone report pixels. A new
  `POST .../images/<file_id>/calibration/` records the scale, recomputed by the server
  from the two points the user drew and stored with its provenance in
  `FileRegistry.metadata['pixel_spacing_mm']` -- no migration.
  `modality_viewers/teleradiography.js` is deleted.
- **One annotation revision can span several resources.** `AnnotationSet` is keyed
  `(domain, patient, kind)` and a revision replaces the whole set, so a patient with two
  annotatable resources lost one when the other was saved. A save now replaces the
  resources it names and carries the rest forward, on one revision inside one
  transaction. Latent for a brain patient with two series; it would have been reachable
  on every save for a photo stack.
- **Tooth segmentation writes through `annotations/`.** Its own set kind, the FDI
  vocabulary seeded by `annotations/migrations/0002`, labels required rather than
  defaulted -- an FDI code decides a polygon's export segment -- and the same conversion
  `annotations_convert_legacy` uses, as one function, so the converted and the live rows
  cannot drift apart and `annotations_crosscheck` keeps comparing like with like.
- **The intraoral photographs and their tooth segmentation render through
  Cornerstone3D**, replacing a grid of `<img>` thumbnails and 1901 lines of Konva. The
  photographs are now the same photo stack teleradiography uses -- stack scroll, pan,
  zoom, the measurement tools and calibration all come with them -- and outlines are drawn
  on the image being looked at rather than in a sidebar tab beside it. Both
  `static/js/intraoral_segmentation.js` and `static/js/modality_viewers/intraoral.js` are
  deleted. Konva is not: the panoramic editor and the laparoscopy annotator still draw with
  it.
  - **Outlines render exactly as they always have.** The old editor drew each ring as a
    Konva line with `tension: 0.35`, so the shape on screen was never the shape in the
    database -- 5,491 stored segmentations were approved by looking at a spline through
    their vertices. Konva's tension is weighted by chord length and Cornerstone's own
    cardinal spline is uniform, which on a hand-drawn ring differs by up to ~4 px, so the
    weighting is reproduced in a spline subclass instead. **The stored polygon is
    unchanged** -- the control points *are* the vertices -- and a test asserts both the
    curve parity and the upstream methods the subclass depends on, so a Cornerstone bump
    that renames one fails the build rather than quietly redrawing every study.
  - **Tooth polygons have one writer.** The segmentation job wrote
    `maxillo.IntraoralToothSegmentation` and so did the editor, and the export read it;
    all three now go through `annotations/`, which is where the polygons live. The legacy
    table is read-only history for the cross-check release. The "has tooth segmentation"
    filter was also written twice, in the patient list and the export builder, and is now
    one function.
  - **Confirmation is per photograph again.** It always was in the legacy row, but
    `AnnotationSet.status` is per patient, so the conversion was collapsing six
    photographs' sign-offs into one flag with the last row winning. `AnnotationTarget`
    gains a nullable `status` (additive migration, no backfill -- the conversion has never
    run against production), and a save that does not mention confirmation leaves it
    alone: an autosave must not retract a claim it never made.
  - **A photograph edited after being segmented reads back on the anatomy.** The
    re-projection through `rgb_editor.js`'s crop, mirror and rotate now happens on the
    annotation read path, from `annotations/adapters/image_edit_replay.py`. It is not
    written back: a re-projection is derived, and the seven production studies whose
    rotations were never applied still want a person to look at them rather than a
    machine's guess filed as approved work.
  - An outline drawn with no tooth selected says so and is not saved, rather than being
    silently dropped: the FDI code decides which segment a polygon is exported under, so
    there is no safe default to pick.
  - The `Teeth` switch hides the outlines when it is off, matching the `Measure` switch.
    Outlines left drawn under a switch reading "off" made the switch look broken, and the
    visibility is re-applied on every redraw -- a freshly restored annotation is visible by
    default, so scrolling used to bring them back.
  - The FDI grid and its controls are visible again. They were inside a
    `:not(.has-selected-image)` CSS gate belonging to the sidebar editor this replaces, and
    nothing set that class any more, so `display: none` applied unconditionally: no tooth
    could be selected and no undo, redo or `Mark done` could be reached.
- **IOS dental landmarks are annotation records, not a JSON document in object storage**
  (roadmap Phase 6, decision #20). Landmarks now write through
  `annotations.services.ios_landmarks` as `SpatialAnnotation3DItem` rows, on the same
  machinery measurements and tooth polygons already use. **No migration**: `ios_landmarks`
  was already an `AnnotationSet.kind`, the 3D point/plane model already existed, and the
  FDI code already resolves against the `fdi-permanent` vocabulary seeded by
  `annotations/migrations/0002` -- the landmark *type* travels in `attributes`, because the
  single label slot is spent on the tooth, which is what decides the code a point is
  exported under.
  - **The mesh is named, and the model insists on it.** Landmark coordinates are
    `resource_local` -- one STL's own object space -- and `add_spatial_3d` refuses to write
    that frame without a resolved target resource. So each arch gets its own target
    (`mesh_upper` / `mesh_lower`) pointing at the scan row the viewer actually serves. This
    is the gap `annotations_materialize_landmarks` records against
    `role='landmark_document'`: the legacy artifact named the *patient* and never the mesh,
    which left every stored coordinate interpreted against geometry nobody had written
    down. A converted study still reads correctly -- the reader groups by the arch the FDI
    code implies, not by the target's role -- and its first live save re-anchors it, so
    there is no backfill command and no second read path. The save names the old anchor
    explicitly rather than leaving it untouched, because a target a save does not name has
    its items *carried forward*: a converted study would otherwise have ended up holding
    both copies, and while single-point types survive that, `cusps` and `planar` are lists
    the reader appends to and would have silently doubled on the first edit. A save naming
    one arch still leaves the other where it was, so a partial save cannot delete landmarks
    the client never sent.
  - **Which mesh a landmark was picked against is unknowable for the pre-Phase-6 corpus,
    and is left that way.** Whether the viewer serves the raw or the re-oriented STL is
    `prefer_processed_for_viewer`, an admin-editable flag; the two are different geometry,
    so flipping it re-frames every landmark stored against the other one. Recording the
    mesh fixes this from here on. Nothing repairs the existing corpus, and a best-effort
    re-anchor would file a guess as the anchor.
  - **Saves are concurrency-checked.** The legacy `PUT` replaced the whole document and
    handed back a `file_id` the client discarded, so two annotators on one patient
    silently clobbered each other; `expectedRevision` now makes the loser a 409. The client
    also no longer names the mesh -- it names the arch and the server resolves the row, so
    a stale viewer cannot file points against geometry nobody was looking at.
  - **The export renders the document from the record** rather than streaming a stored
    one. `ios.landmarks` became a collector; the bytes keep the legacy framing (bare
    document, `separators=(",", ":")`, the same `ios_landmarks_patient_<id>.json`
    filename). One honest difference: keys come out sorted where the legacy file preserved
    the browser's insertion order. `ios.landmarks_prediction` stays a file artifact,
    because that one really is model output arriving over the frozen runner API.
  - The "Has IOS landmarks" filter -- written twice, in `common/export_catalog.py` and
    `maxillo/views/patient_list.py` -- is now one function in `annotations/queries.py`. It
    also stopped answering the wrong question: `files__file_type='ios_landmarks'` is true
    for a patient whose every landmark has been deleted, because the file row survives as
    history.
  - The landmark prediction job writes through the service too, with
    `origin=PREDICTION`, so model output never sets the monotonic `ever_annotated` flag and
    a nightly re-run cannot overwrite work somebody placed by hand.
- **The IOS mesh viewer is Cornerstone3D, and Three.js is gone** (roadmap Phase 6).
  `static/js/modality_viewers/ios.js` (1539 lines, including its own hand-rolled STL
  parser) is deleted, and with it the three Three.js r128 CDN tags in `templates/base.html`
  -- that file was their only consumer, so **no page loads Three.js any more**. The scans
  render in a `VolumeViewport3D` through `cornerstoneMeshLoader`, `TrackballRotateTool`
  replaces `THREE.TrackballControls`, and surface picking is `vtkCellPicker` with the
  markers as vtk sphere actors, which is Yggdrasil's own state as decision #4 requires.
  This was the last of the four frontend stacks the migration set out to replace.
  - **The stored coordinates do not move, and that is asserted rather than assumed.** The
    legacy viewer rotated each jaw 180° about Y and translated both by the negated centre
    of their combined bounding box -- but it stored `mesh.worldToLocal(hit.point)`, which
    inverts the *full* world matrix, so the stored numbers were always raw STL vertex
    coordinates. Cornerstone's `Mesh` applies no transform to an STL actor, so leaving the
    actors untransformed makes a picked world position identical to the value to store.
    `frontend/tests/meshLandmarks.test.js` pins both halves of that identity *and* reads
    the shipped `Mesh.js` to assert it still applies no transform -- a version bump that
    started centring meshes would otherwise move every landmark on every historical study,
    silently, and look entirely plausible doing it.
  - **Landmarks have redo now**, over an action log rather than the previous stack of fifty
    full-document copies. The roadmap lists undo/redo inconsistency across the four
    surfaces as one of the defects this migration exists to close. The mechanics are shared
    with the intraoral editor (`imaging/annotations/actionLog.js`), because "clear redo on
    a new action" is a rule that dies quietly in a second copy -- both surfaces identify
    edits positionally, so a stale redo replays against indices that have moved.
  - **A crash that took the whole toolbar down is fixed.** `ios.js` bound
    `toggleLandmarkMode` without a guard while the element sat behind
    `{% if 'ios_landmarks' in allowed_annotations %}`, so a project with annotations
    enabled but landmarks switched off threw before binding reset, wireframe, grid and all
    seven camera buttons.
  - The four inline `onclick="IOSViewer.updateGridSize(N)"` handlers and the
    `window.IOSViewer` global they reached into are gone -- the same value-with-two-owners
    shape that produced three of Phase 5's four browser-check defects. The entry also now
    sits inside the `{% if ns == 'maxillo' %}` guard the other four Cornerstone surfaces
    use; `ios.js` was downloaded on every brain and laparoscopy patient page, where it ran
    and returned because `#scan-viewer` does not exist there.
  - The legacy `GET/PUT /{ns}/api/patient/<id>/ios/landmarks/` endpoint and its three
    normalisers are deleted (decision #3: the old path goes in the same commit). The
    viewer reads and writes through the `annotations/` endpoints added above, so a save is
    concurrency-checked instead of a whole-document overwrite.
  - `maxillo/tests_ios_surface.py` renders the patient page and asserts every element id
    the JavaScript resolves, that Three.js is absent, and that the entry loads only under
    maxillo. Phase 5's lesson, applied: a template id joining two files is an untested
    interface, and a rename leaves the JS holding `null` with nothing to say so.
  - **Viewing and annotating are separated.** Landmark visibility is a switch in the
    toolbar, outside the `ios_landmarks` gate: reading a study that has landmarks on it is
    not annotating it, and a reader without landmark rights previously could not see them
    at all. It is a `role="switch"` with a visible on/off word rather than an eye icon,
    for the reason the measurement toolbars already give -- "is this icon telling me the
    state or the action?". Marker size, the 3D axes, the white background and per-type
    landmark visibility moved with it, into a **Visualization** menu; all four change what
    a reader sees and none needs annotation rights. The annotation workbench is now gated
    as a whole, so a project without the method renders no annotation DOM.
  - **One FDI tooth selector in the application.** The workbench uses the intraoral
    segmentation editor's grid -- the tinted, icon-bearing one -- instead of a second
    32-button layout naming the same teeth differently. `toothGrid.js` gained two small
    seams for it: `countFor`, because a landmark badge counts landmarks rather than
    polygons, and an injected `documentRef`. The workbench stacks the tooth grid and the
    landmark types vertically, each running the full width: it sits above the scan, so its
    height is jaw the clinician cannot see, and side by side the type column forced the
    arch narrow while the section stayed as tall as the taller half.
  - **The white background never worked.** `viewport.setBackground?.()` was an optional
    call on a method **no Cornerstone viewport has**, so it did nothing at all, silently --
    `enableElement`'s `defaultOptions.background` is read once at creation. It goes through
    the vtk renderer now. The default also follows the page's `data-theme` instead of a
    hardcoded dark, and follows it live, so a scan no longer sits on a dark canvas inside a
    light page.
  - **Scroll wheel zooms.** `ZoomTool` was bound to the right button only -- copied from
    the volume grid's 3D viewport, where a wheel event at least scrolls a stack. Here it
    did nothing.
  - **The reference axes were several times larger than the scans.** They were scaled by
    half the *camera distance*, which is itself a multiple of the bounding diagonal; they
    are a tenth of the scans' own radius now.
  - **The jaws render in the colour they are given.** They came out scarlet whatever was
    asked for, and the property colour was not the problem: `vtkSTLReader` unconditionally
    writes cell scalars named "Attribute" from the binary STL's per-facet attribute-byte
    field, and `vtkMapper` ships with `scalarVisibility: true` -- so the mapper coloured
    the surface through its default blue-to-red lookup table and never consulted the
    colour `Mesh` had set. That field is a padding word almost every exporter writes as
    zero, so a whole jaw mapped to one end of the rainbow. Scalar colouring is off; a test
    reads the shipped reader so the line cannot later be removed as pointless.
  - **The arches are a rose upper and a deep blue lower** -- the same pairing the Three.js
    viewer used, so anybody who learned the old one reads them the same way round, but with
    its real flaw removed. The legacy light pink against light lilac differed by hue alone,
    at 1.1:1 in relative luminance, which is to say not at all once vtk shades them: two
    surfaces meeting at the occlusal plane are read through lighting that varies far more
    than that. This pair is 2.8:1, so it survives the shading, a greyscale screenshot and a
    red-green colour vision deficiency. Both stay desaturated, because the landmark palette
    already spends red, orange, blue and purple on landmark *types* and those markers are
    small.
- **The occlusion classification dropdowns open at a sensible width.** `min-width: 100%`
  stretched them to their field, and the bite-class field spans the whole grid row, so five
  short options opened as a panel-wide sheet of empty space.

### Changed
- **`Calibrate` is part of annotation mode now, on every 2D surface.** It only does
  anything once a `Length` line has been drawn, so on a study being read it was a button
  whose only possible answer was "draw a line first". One owner (`applyAnnotationMode`), so
  every toolbar built on `controlIds` inherits it.
- **The image counter and the calibration readout follow the mouse wheel.** `StackScroll`
  moves the stack without going through `scrollTo`, so everything hanging off the Prev/Next
  buttons -- the counter, the calibration text, and which image the tooth editor draws --
  stopped following the image the moment the user reached for the wheel. They now listen for
  Cornerstone's own `STACK_NEW_IMAGE`, which covers both paths.
- **CBCT annotation is a mode now, and it is off by default.** One switch reading
  `Annotations on` / `Annotations off` replaces the eye button: turning it on reveals the
  measurement tools *and* shows the measurements, turning it off hides both and puts the
  crosshair back on the left mouse button. A study being read shows six fewer controls
  than one being measured, and there is no longer a pair of states (mode vs. visibility)
  that could disagree. The state lives in the DOM (`aria-checked`) and is read back at
  click time, so the switch cannot invert.
- A trash button beside save clears every measurement drawn on the study. It asks first,
  and it clears the *viewer*: the server replaces the whole set on save, so the next save
  is what makes a clear permanent and a reload is what undoes it. Both the confirmation
  and the notification say so.
- A saved measurement set is confirmed by the platform's green toast
  (`window.appNotify`) instead of "Saved 3 measurements." in the toolbar; a failed save
  gets a red one rather than arriving in the same place as a success. The toolbar's status
  line is now only for failures that are about the toolbar itself.
- **Third-party CDNs are allowed.** The blanket no-CDN rule is withdrawn: a CDN serves a
  static asset faster than this deployment can and takes the bandwidth off it, and
  `templates/base.html` had been loading Three.js, an STL loader, trackball controls and
  fflate from three of them the whole time. `scripts/build_frontend.mjs` and
  `scripts/check_bundle_assets.mjs` now *note* a CDN reference in the emitted bundle
  instead of failing the build on it. Two narrower rules survive and are unaffected:
  webfonts stay self-hosted (a font CDN sees every page view of every visitor — a GDPR
  question a library does not raise), and the itk-wasm pipelines stay vendored and aliased
  because their ABI is pinned to the package version. `docs/cornerstone-future-work.md`
  §9 is withdrawn accordingly.
- **The Phase 3 validation harness is deleted**, along with the `/imaging-validation/`
  page and the `@niivue/niivue` dependency. It existed to clear one gate; Tier 1 and
  Tier 2 were green across the readable corpus and F7's `amip` sign-off is recorded, so
  it had nothing left to answer. That takes ~2.2 MB out of the committed bundle — 1.4 MB
  of it base64-inlined Blosc/Zstd/LZ4 wasm reached through `zarrita`, so a staff-only
  page could in principle read Zarr arrays it was never shown — and drops the bundle
  from 19 emitted files to 12. **NiiVue went with it, so the tree no longer contains a
  reference implementation**: re-running either tier now means reverting the commit.
- **The raw-data lock reads `AnnotationSet.ever_annotated`.** `common/annotation_lock.py`
  keeps its module path and all five public signatures byte-identical, and gains
  `annotations` as its first source: one indexed query instead of up to five per-domain
  existence checks. The lock is now **monotonic** — deleting annotation work no longer
  thaws the scan it was drawn on. Machine output still never locks a case, but the rule
  now lives in the data (a prediction revision does not set the flag) rather than in
  hardcoded exemptions. The legacy per-domain checks are retained alongside the new
  source for one release as a cross-check, and go when those tables are dropped.
- The Cornerstone bundle derives modality rescale (HU) from the raw NIfTI header itself
  rather than trusting `@cornerstonejs/nifti-volume-loader`, whose
  `modalityScaleNifti` skips the rescale whenever either factor is already neutral — so
  the ordinary `scl_slope=1, scl_inter=-1024` CBCT encoding would have been silently off
  by 1024 HU.

### Fixed
- **Tooth polygons silently detached from rotated photographs.** The image editor can
  crop, mirror and rotate an intraoral photo, and both the client and the server
  re-project the stored polygons through those operations -- except the server
  implemented `flip-h`, `flip-v` and `crop` and **neither rotate case**, so a rotated
  photograph read back polygons nothing had re-projected. The preview was right and the
  stored read was wrong, which is the worse way round: whoever drew them saw them in the
  right place. **7 of the 68 edited intraoral files in production carry a rotate and are
  affected.** The projection is correct from here on; those studies still need looking
  at, because nothing has re-derived what was displayed in the meantime. Both
  implementations are now driven by `common/fixtures/image_edit_replay.json` so they
  cannot drift again.
- **Restored measurements were not drawn until a tool button was clicked.** The switch
  showed them and nothing appeared; switching it off and on again "fixed" it only
  because a tool button had been clicked in between. `ToolGroup.addTool` instantiates a
  tool but writes no `toolOptions` entry, and a tool with no mode is skipped by
  `getToolsWithModesForElement` — so the annotation rendering engine never asked
  `LengthTool` to draw, whatever the annotations' visibility said. Clicking any
  measurement button gave every tool a mode as a side effect of `setPrimaryTool`
  passiving its neighbours, which is what made it look intermittent. Switching the mode
  on now puts every measurement tool in `Passive` deliberately (and `Disabled` on the way
  out), pinned by a test that needs no GPU.
- **Measurements that could not be made visible again.** Two independent bugs, both
  reported as "I switch annotations on and nothing appears":
  - Restoring a study wrote the stored `isVisible` flag back. An annotation saved while
    the measurements were hidden therefore came back invisible *and unreachable* —
    Cornerstone's `setAnnotationVisibility(uid, true)` only clears the flag for a UID in
    its own hidden set, which a freshly added annotation is never in, so no amount of
    toggling could show it. Visibility is session state now, not part of the record.
  - Hiding "all annotations" hid the crosshair too. `getAllAnnotations()` returns the
    state tools keep for themselves, and the navigation reticle is one of those, so
    switching measurements off took the reticle with it. The hide now uses the same
    measurement filter a save uses, and writes the flag as well as the hidden set, which
    makes it idempotent.
- **Two annotation gates that were missing.** `update_nifti_metadata` rewrote a raw
  CBCT's qform/sform in place and restamped `FileRegistry.file_hash` without consulting
  the annotation lock — every landmark, spline and polygon already drawn on that volume
  kept its coordinates while the volume moved in patient space, with nothing in the
  record to say so. It now refuses with 409 before any object-storage work.
  `update_classification` was the one annotation write that never called
  `project_allows_annotation`, so a project with occlusion classification switched off
  still accepted instant updates from the sidebar.

### Removed
- ~3,700 lines of dead viewer code: `volume_viewer.js` (which also shadowed the live
  `window.CBCTViewer` with a duplicate), `slice_renderer.js`, `volume_interaction.js`,
  `windowing.js`, `maxillo_niivue_viewer.js`, `nifti-reader-min.js`, and
  `volume_renderer.js` with `static/shaders/volume_fragment.glsl` — the latter loaded on
  every patient-detail page and never called.
- The legacy CBCT volume preload (`volume_loader.js`, `worker/volume_worker.js` and the
  `useLegacyVolumePreload` branch in `patient_detail.js`). Unreachable for maxillo and
  brain by code path, and confirmed unreachable for laparoscopy against production: no
  laparoscopy patient owns a `cbct_raw` file.
- The Konva tooth-segmentation editor and the intraoral thumbnail grid it hung off
  (`intraoral_segmentation.js`, `modality_viewers/intraoral.js`), their sidebar section
  template, and the two `intraoral-segmentation/` endpoints -- `maxillo` no longer has a
  view that writes tooth polygons. What survived that module is one payload-normalising
  helper, moved to `maxillo/intraoral_teeth.py` because a views module with no views is a
  misfiled one, and the image-edit replay, moved to
  `annotations/adapters/image_edit_replay.py` where the read path and the export can both
  reach it.

## [2.0.0] - 2026-08-26

### Added
- Folder-level access control for laparoscopy (`laparoscopy.FolderAccess`, mirroring brain).
  A data migration grants access on all laparoscopy folders to existing project members,
  preserving current visibility while making it revocable.
- `VERSION` file exposed as `settings.APP_VERSION` and shown in the site footer.
- This changelog.
- CI on self-hosted GitHub Actions runners (ruff + full suite against MySQL/Redis,
  `makemigrations --check` gate) and a release workflow on `v*` tags.
- Contract tests freezing the external runner HTTP API (claim/complete/fail).
- One-command local development bootstrap (`scripts/dev_bootstrap.sh`):
  standalone `docker-compose.dev.yml` with MySQL, Redis and a single-node
  Garage for object storage, plus an idempotent `manage.py seed_dev`.
- Automated daily database backups (03:00 UTC): celery beat + a dedicated
  `maintenance`-queue worker dump, verify and upload to object storage under
  `backups/mysql/`, with daily/weekly retention pruning and a
  `manage.py backup_now` wrapper. Settings refuse to start if the maintenance
  queue collides with a runner queue.
- `SystemCheck` model recording maintenance runs; staff-only `/status/` page
  (DB, object storage, backup freshness — warns when the newest successful
  backup is older than 26h) and unauthenticated `/healthz` (200/503, no
  details).
- Export share links can now expire: nullable `expires_at` on all three
  Export models (null = never, so pre-2.0 links are unaffected). New and
  updated shares default to 30 days; the share UI offers 7/30/90 days or
  never, where "never" is allowed only for staff/project admins. Expired
  links answer 410 Gone on both the landing page and the download.
- Yggdrasil world-tree branding: an original SVG logo and a full favicon set
  (`static/icons/`, regenerable via `scripts/make_icons.sh`) wired into every
  page. The footer drops the original single-tenant author credit, keeps the
  GitHub link and version, and flags "Yggdrasil 2.0 is out".
- Self-hosted webfonts (Cinzel for display, Inter for body; OFL, under
  `static/fonts/`) and a sitewide `theme.css` — no Google Fonts CDN, so no
  third-party request (GDPR). Annotation viewers are deliberately left
  unrestyled. The landing page gains a Cinzel title, the world-tree logo, an
  explanation of the Yggdrasil name, and a demo call-to-action that stays
  hidden until a `demo_url` is provided (Phase 7).
- Public guest demo (Phase 7): an anonymous, read-only, no-login window at
  `/demo/` onto curated folders flagged `is_demo=True` (new `FolderBase`
  field, editable per folder in the admin). GET/HEAD only, per-IP rate
  limited; a self-contained set of views/templates that never touch the
  authenticated app or its `@login_required` file endpoints and can only
  reach a patient (and its files) that lives in a demo folder. The landing
  "Explore the public demo" CTA appears only once at least one demo folder
  exists. Only anonymized or synthetic studies may be flagged.
- `manage.py resubmit_jobs --domain <d> --modality <slug>`: bulk-create pending
  processing jobs for existing patients that have the modality's raw file but no
  job yet (e.g. after shipping a new algorithm for a modality), reusing the
  normal enqueue signal. `--include-existing` also re-pends patients that
  already have a job; `--folder-id`, `--limit`, `--dry-run` supported.

### Changed
- The web container now serves with gunicorn and runs `migrate` on start
  (`AUTO_MIGRATE=0` to opt out, `RUN_DEV_SERVER=1` for the dev server).
- The Django/Celery project package and runtime entrypoints are now named
  `yggdrasil` instead of the pre-2.0 project name; Django app labels and tables
  are unchanged so the v1.9 dump-restore migration path remains additive-only.
- The SLURM runner worker is intentionally off `app-net-$DOCKER_SUFFIX` and uses
  externally routable Redis/API URLs from `.env.worker`.

### Security
- File serving is now authorized against the file's own domain. `serve_file`
  resolved the patient with an `if laparoscopy / else .patient` branch (so a
  brain row consulted the maxillo FK) and then authorized **every** domain
  against a hardcoded `Project.objects.filter(slug='maxillo')`, passing the
  literal `'maxillo'` into `user_is_project_admin`/`user_can_read_folder`.
  Both directions were wrong: a maxillo member could read brain and
  laparoscopy files, and a laparoscopy-only member was refused their own.
  The namespaced routes were shielded by `ActiveProfileMiddleware`, but the
  global `/api/processing/files/serve/<id>/` route (used by the file-management
  UI) skips that middleware, so the view's own check was the only gate there.
  Authorization now runs through one helper, `common.file_access
  .authorize_file_read`, which resolves the patient via the domain registry and
  defers to `patient.project`. `brain.api_views.serve_file` uses it too and
  thereby gains the `raw_file_hidden` backstop it previously lacked.
  **Behavior change**: maxillo project admins no longer receive brain or
  laparoscopy files. Audit `ProjectAccess` rows across domains after deploying
  and grant per-domain access where it is genuinely intended.
- Patient-viewer and activity-stats pages now inject server data via Django's
  `json_script` filter instead of `|safe` JSON interpolation, removing a
  script-breakout XSS vector. The rendered `<script type="application/json">`
  elements keep their ids, so viewer JavaScript is unchanged.
- Removed `csrf_exempt` from all session-authenticated state-changing views
  (classification updates, patient tags, laparoscopy Magic Tool worker
  proxies, and the external project API). **Breaking for external API
  clients**: POSTs to `/api/<project>/upload/` and `/api/<project>/patients/`
  must now send the `csrftoken` cookie value in an `X-CSRFToken` header
  (standard `requests.Session` flow); unauthenticated POSTs without a token
  answer 403 instead of 401. The token-authenticated runner API under
  `/api/runner/...` is unchanged.
- Runner bearer-token comparison now uses `hmac.compare_digest` (constant
  time); same request/response contract.
- Brain processing API was fully anonymous: `serve_file` let anyone fetch any
  brain file by id, and `get_file_registry`/`get_job_status`/the job list
  leaked file paths, patient ids and job state. These now require login (file
  serving enforces the brain folder ACL; the monitoring endpoints are
  staff-only). The unauthenticated per-domain runner callbacks
  (`runner_claim/complete/fail`) that also mutated job state were removed —
  external runners use the single token-authenticated contract under
  `/api/runner/...` (domain-agnostic, unchanged).

### Fixed
- `common.uploads.domain_for_patient()` returned `maxillo` for brain patients
  (anything that wasn't laparoscopy), so registry-driven Job/FileRegistry FK
  helpers misfiled brain entities; it now maps every domain app label correctly.
- `common.permissions._namespace()` resolved every request-object call to `maxillo`,
  so brain and laparoscopy permission checks ran against the wrong domain.
- Folder permission edits from brain pages silently wrote maxillo `FolderAccess` rows;
  folder-tag views now resolve the model per namespace.
- `user_can_view_caption_content` ignored brain's `folders` M2M relation.
- `patient_volume_data` always returned 500: a dead filter block referenced an
  undefined `domain` variable and its NameError short-circuited the endpoint.
- `/admin/control-panel/` was reachable without authentication; it now
  requires a staff login like its sibling admin views.

## [1.9.0]

Last pre-modernization production state (tag `v1.9.0`, commit `52d1557`).
No changelog was kept before this point.
