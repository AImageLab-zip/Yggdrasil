# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.0.0] - 2026-09-02

### Added
- **`migrate_dataset_to_object_storage --trust-storage`, for finishing its own
  interrupted run.** The command decides whether a legacy `/dataset` reference can be
  rewritten by asking the local filesystem, which is right when it is migrating a live
  dataset and useless afterwards: once the blobs are uploaded and the machine they came
  from is gone, every path reads as missing and nothing is rewritten. `--trust-storage`
  asks the object store instead -- an exact key, or a prefix for folder and DICOM
  bundles -- and never uploads. The check runs in dry-run too, because the point of the
  dry run is to prove every reference resolves before a row is touched.
  Run against the 1.9 production database it establishes that the `/dataset` migration
  had in fact completed: **no** `FileRegistry.file_path` still names one, and
  `metadata['files'][*]['path']` holds object keys. What remains is residue -- 810 rows
  whose `metadata['logs']` quotes old paths in job output, one row whose
  `metadata['files']` names a panoramic PNG that is genuinely absent from the store, and
  11 `Job` rows whose `input_files`/`output_files` name 14 objects that are likewise
  absent. None of it is on a serving path except that single panoramic, which already
  404s wherever it is read from, since every instance reads the same bucket.
- **A bucket-to-bucket clone, for moving the instance without moving the store.**
  `scripts/mirror_bucket.py` copies one S3/Garage bucket into another key for key, so a
  new deployment can be given its own bucket while the old instance keeps serving from
  the original. The copy is server-side -- `CopyObject` is a single request to the
  destination naming the source -- so no object bytes pass through the machine running
  it, and one credential with read on the source and write on the destination is the
  whole requirement. `ObjectStorage` gained `copy_from` (multipart above S3's 5 GiB
  single-copy limit) and `list_objects`, which answers sizes where `list_keys` answers
  only names.
  Three things the shape of a real 650 GiB clone forced. `CopyObject` is *synchronous*,
  so the store does not answer until the object is copied and botocore's 60s
  `read_timeout` expires mid-copy on a few hundred MB -- and then retries work the store
  is still doing, which is a feedback loop rather than a retry. `read_timeout` is
  therefore configurable and defaults to 900s here, while the app's own default is
  untouched. Work is issued **shuffled**, because keys sort by prefix and size
  correlates with prefix, so key order points every worker at the same size class at
  once and a run of multi-GB objects blocks the whole pool. And a heartbeat reports
  in-flight progress on a timer rather than on completions, so a pool blocked on slow
  objects is distinguishable from a hung process.
  Concurrency is not the lever it looks like: measured against a single-node Garage,
  4x the workers bought 1.35x the throughput, because the store's disk is the ceiling.
  `--skip-existing` resumes and catches drift; `--verify` compares the two buckets
  without writing and exits non-zero on any missing or size-mismatched key.
- **The laparoscopy video algorithm runs on the cluster, and annotation waits for it.**
  The algorithm arrived as a Docker/Celery worker speaking the old ToothFairy4M manifest
  contract; the cluster it has to run on dispatches with `sbatch` and has no Docker
  daemon, so its logic was ported to `algo/laparoscopy-video/` --
  `--input-dir`/`--output-dir`, `ygg-stage` for I/O, `uv` for its environment, and
  `imageio-ffmpeg` for the ffmpeg the compute nodes do not have. It writes
  `compressed.mp4` (what the page plays) and `subsampled.mp4`, the sharpest frame of each
  source second, losslessly encoded.
  The sampled track is now what the annotator opens, and its existence is the gate: a raw
  recording runs at 25-30 fps, the record is one labelmap per annotated frame, and the
  export reads the sampled track -- so strokes drawn on the raw video described frames
  nothing else could line up with. `video_state` gained a `processing` state saying so,
  and both write endpoints re-check it (409, not 403: the request is fine, the state has
  not arrived). Derivatives are probed on completion, because the sampled track's frame
  rate is its own and nothing else can read it.
- **IOP-Compass: intraoral view classification, then tooth segmentation.**
  `algo/iop-compass/` on the cluster, beside a pristine checkout of the benchmark it
  imports. The order is the method rather than a pipeline convenience -- SegmentAnyTooth
  keeps one detector per clinical view, so the ResNet18's label selects the model; a
  five-photograph series is resolved jointly (`constrained_assign`) rather than by five
  independent argmaxes. Segmentation runs on the full image with no ROI stage, and the
  contours -- not the rasters -- are scaled back to the uploaded photograph's pixels.
  Polygons land through the existing intraoral completion path as
  `AnnotationOrigin.PREDICTION`, so they never freeze a patient's raw data and never
  overwrite a confirmed segmentation. Each photograph's view is recorded on its
  `FileRegistry.subtype`.
  Outputs are keyed by object-storage key: a cluster job is handed `YGG_INPUT_KEYS` and
  never sees a `FileRegistry` id, and `file_path` *is* that key, so the completion path
  now resolves either.

### Changed
- **The CBCT export offers the uploaded volume and the segmentation, and nothing else.**
  "Processed volume" sat beside "Segmentation" with nothing telling a reader them apart,
  and the resampled volume is not what anyone leaves with. The inference statistics and
  the pipeline's own panoramic PNG went with it -- pipeline diagnostics, not the record,
  and the panoramic images are offered under the panoramic modality where someone looks
  for them. The panoramic Z-sweep, a bucket for pre-MIP sweep variants, is gone too.
  Saved exports naming a dropped artifact degrade rather than break: `resolve_artifacts`
  has always ignored keys it does not know.
- **The export form names one thing per checkbox, and no longer explains itself.** The
  DICOM interchange outputs (SEG, SR, RTSTRUCT) and the raw-archive artifacts are off the
  form: interchange is not what anybody leaves this screen with, and the raw archive is
  being retired as an input. "Predicted tooth landmarks" is gone as a separate box --
  predictions are written into the same annotation record with `origin=PREDICTION`, so
  "Tooth landmarks" already covers generated and hand-placed points and the second box
  only offered the stale file the pipeline left behind. "Occlusion classification"
  (patient level) and "Bite classification (pipeline output)" (IOS) were one fact under
  two names: they are now a single **Bite Classification** under IOS, exporting the
  pipeline's JSON and the manual / AI classes together, with the matching filter merged
  the same way. The per-checkbox descriptions are gone; the labels say it. Saved exports
  naming a merged key still resolve to what they asked for.
- **DICOM upload is switched off.** The CBCT control accepts NIfTI and MetaImage only,
  the DICOM-folder pane is gone, and the refusal is enforced in
  `save_cbct_to_dataset` -- on the DICM marker in the bytes, not on a filename -- so the
  upload page, the project API and replacing a patient's files are all closed to it.
  Native DICOM storage (`save_cbct_folder_to_dataset`, `common.dicom.ingest`) is
  untouched and still serves the series already stored; what is switched off is
  accepting new ones.
- **The modality tabs read `IOP` and `TR`.** `intraoral-photo` had been seeded with its
  own slug as its label and teleradiography with none, so beside `IOS`, `OPT` and `RAW`
  two tabs read a slug and a full word. Migration `0051` relabels existing rows, since
  `get_or_create` only ever applied the seeder's defaults at creation.

### Fixed
- **One classification per patient per classifier, which production already enforced.**
  A `UniqueConstraint(patient, classifier)` reached production out of band, ahead of this
  line, together with a pass that collapsed existing duplicates. The invariant is the
  right one -- there is the manual classification a human entered and the pipeline one
  the model produced, every write site treats it that way, and a duplicate only ever
  shadowed the newer row in `-timestamp` ordering, so the UI showed a stale
  classification -- but the model never declared it, so a fresh database did not have it
  and Django's recorded state did not know about it either.
  Now declared on `Classification.Meta`, with the migration written to converge from
  both starting points: the dedup only touches groups that are actually duplicated, and
  the constraint is added through `SeparateDatabaseAndState` only if the table does not
  already carry it, since a bare `AddConstraint` succeeds on an empty database and fails
  on a restored one with a duplicate key name. `patient_detail`'s accept-AI path moved
  from `create` to `update_or_create`: accepting the AI result twice, or accepting it
  after classifying by hand, is an ordinary thing to do rather than an error to raise.
- **The empty region panel named the wrong role.** With no Add button the laparoscopy
  region panel offered "An annotator can add one" while its test asserted
  "administrator" and its own comment claimed the button was administrator-only. The
  server is unambiguous -- naming what a project draws is annotation work, gated on
  `profile.is_annotator()`, and the template gates the button the same way -- so the
  sentence was right and the test and the comment were both stale.
- **A prediction locked the panoramic arch.** `annotation_lock` asks `annotations` first
  and then the legacy per-domain tables, and takes the union -- a cross-check kept for
  one release. But the legacy half is a bare `.exists()` on a row, and whether a *human*
  produced it is the actual question, which is exactly what `AnnotationSet.ever_annotated`
  carries. So an `ios_landmarks` file written by the landmark predictor locked a patient
  nobody had annotated, and the editor answered "Arch locked" over an arch that had never
  been touched. Where the conversion has produced a set for a kind, that set is now the
  answer and the table under it is skipped; where it has not, the old check still speaks.
- **A patient carrying several modalities lost its WebGL contexts, and two viewers went
  dark with it.** `webGlContextCount` is allocated **per rendering engine** and the pool
  fills eagerly, so 7 was 7 contexts for each of the four engines a maxillo patient page
  builds -- 28 allocated against a browser that keeps around 16, on a page that uses
  four. The oldest were dropped, which is the "Too many active WebGL contexts" storm, the
  `vtkPolyDataVS` compile failure, the `isUniformUsed` of null crash, the black intraoral
  viewport and the missing IOS mesh. The count only ever separates multiple *stack*
  viewports inside one engine -- every other type lands on context 0 -- and no engine here
  has more than one, so it is 1, with `renderingBudget.test.js` pinning that claim against
  the surfaces rather than against a preference.
- **`No imagePlaneModule found for imageId: ...front.jpg`, and a black intraoral stack.**
  `metaData.addProvider` is process-wide so the photo entry registers once -- over
  whichever surface's registry mounted first. A maxillo patient mounts two, and the second
  one's imageIds had no provider; Cornerstone's `buildMetadata` destructures
  `imagePlaneModule` with no null check, so the miss threw instead of degrading. One
  provider now reads a composite over every mounted surface's live registry, and a surface
  that unmounts drops out of it.
- **A CBCT patient with no panoramic logged a failed request on every visit.** The pane is
  offered for every CBCT because one can be generated from the volume, so it opened by
  asking `?meta=1` for patients that had nothing yet -- which the server can only answer
  404, and which the browser logs before any handler runs. The page is told at render time
  whether a panoramic file exists and shows the pane's own empty state instead.
- **The laparoscopy region chips changed size when one was selected.** They are a wrapped
  row of buttons, and the selected one gained a bolder label and a check mark that existed
  only while selected -- so picking a region resized two chips and shuffled the rest. The
  box is fixed and the mark is always laid out; selection changes colour and nothing else.
- **Video masks were too faint to see.** No labelmap style was set, so every region
  rendered at Cornerstone's default half-transparent fill -- and all but the selected one
  at the lower `*Inactive` alphas -- over saturated tissue under a specular highlight.

- **The CBCT panoramic arch editor drew no control points, and its axial background
  ignored the Z control.** Two independent throws, and neither could be seen from the
  suite because it drives fakes.
  - `ArchSpline extends CubicSpline`, but `getTransformMatrix` is abstract on `Spline`
    and implemented only by `CardinalSpline`, `BSpline` and `QuadraticBezier` --
    `CubicSpline` *calls* it and does not define it. So every render of the arch threw
    `this.getTransformMatrix is not a function` inside `renderAnnotationInstance`,
    before a single handle was drawn. The base is `CardinalSpline` now, the way
    `annotations/tensionSpline.js` already had it, and the annotation names its spline
    type and lets the tool build the instance rather than constructing one by hand
    (`Spline`'s constructor does not read `controlPoints`, so the hand-built one was
    born empty anyway). `archSpline.test.js` reached for the method as
    `getTransformMatrix?.()`, which is exactly what hid this; it is a required method
    now, and the suite exercises `getPolylinePoints()` -- the entry point the tool
    actually calls, and the one nothing had ever run.
  - `cprViewport.setVolume` handed `vtkImageCPRMapper` the Cornerstone volume's own
    `imageData`. A 5.8.2 `ImageVolume` attaches a `voxelManager` and states
    `hasScalarVolume: false`; it never calls `pointData.setScalars()`. The CPR mapper is
    the one mapper in this stack with no Cornerstone-patched counterpart, so it read
    null scalars, returned early from `buildBufferObjects`, and dereferenced a null
    `volumeTexture`. **That throw froze the whole page**:
    `ContextPoolRenderingEngine._renderFlaggedViewports` maps over the flagged viewports
    with no `try` and clears `_animationFrameSet` only after the loop, so one throwing
    viewport stops every viewport on the shared engine from ever repainting again -- the
    axial slice stood still while the mask overlay, which `setMask` paints directly,
    followed the Z control. The mapper is given a `vtkImageData` carrying the voxels in
    its point data now, with the volume's own geometry and no copy of the array.
  - The Z slider, prev/next and Reset auto were bound before the editor was mounted, so
    touching one on a patient who already had a panoramic threw on a null descriptor.
    They are no-ops until Edit is clicked, which is what they mean.
  - The arch spline config carried an invented `allowOpen`/`allowClosed`/`allowOpenEdit`
    triple that upstream merges in and never reads. The switch that exists is the
    tool-level `allowOpenSplines`, which was on its `false` default -- so the tool closed
    the arch on edit, drawing a loop through the tongue.
- **The laparoscopy video annotator lost every brush stroke on mouse-up, and its polygon
  tool could never draw at all.**
  - `Viewport.addActor` guards its renderer with `renderer?.addActor(actor)`;
    `Viewport._removeActor` does not. A `VideoViewport` has no VTK renderer -- it draws
    on a 2D canvas and its labelmaps are `CanvasActor`s -- so every labelmap actor
    removal was `Cannot read properties of undefined (reading 'removeActor')`, on every
    frame change and after every stroke. The stale actor was then never removed, so the
    next pass threw in the same place and the mask had no actor left to draw it. Both
    upstream callers skip that line for a viewport exposing `removeData`, so the video
    viewport now has one -- the missing half of the `declareCpuImageRendering` shim that
    already patched the *add* side of the same asymmetry.
  - The polygon button activated `PlanarFreehandContourSegmentationTool`, whose
    `createAnnotation` throws unless a Contour segmentation is active. This surface has
    only ever created labelmaps, so every stroke threw and was swallowed by
    `mouseDownActivate` -- visible while the mouse was down, gone on release. It draws
    with a plain `PlanarFreehandROI` now and the finished outline is rasterised into the
    region's labelmap and dropped (`imaging/video/polygonFill.js`), which keeps the
    labelmap the only record rather than adding a second representation of the same fact
    for something to keep in step.
  - The active tool was invisible. `selectTool` has always toggled `.active`, but
    Bootstrap's CSS was removed and nothing in `static/css/` styled it, so the armed tool
    rendered identically to the rest. The template also ships the brush pre-marked and
    nothing ever armed it, so the toolbar opened asserting a selection Cornerstone did not
    have -- harmless while the class had no styling, a false statement once it is filled
    blue. It is armed on mount now, or the mark is dropped when it cannot be.
  - At mount the brush painted into the **last** region while the panel and
    `editor.region` said the **first**: Cornerstone marks the most recently added
    representation active, and nothing re-asserted the selection afterwards.
  - `saveMarkers` POSTed to a view that accepts `GET`/`PUT`, so every quadrant marker add
    and every removal answered 405.
  - The dead `point` toolbar button mapped to no tool and reported "Pick a region before
    drawing on one" -- a true-sounding sentence with no action behind it. It is gone, and
    an unknown tool is now told apart from a missing region.

### Changed
- `toolDecision` is gone from `imaging/video/bootstrap.js`. It was a pure, exported and
  tested copy of the "which tool may be armed" rule with no caller anywhere;
  `editor.setActiveTool` is the one that runs, and it now answers
  `'ok' | 'unknown' | 'needs-region'` so the binder can tell the two refusals apart. Two
  implementations of one rule, in two shapes, is how they drift.

### Added
- **The laparoscopy region panel manages region types, and lists what is drawn in them.**
  Rename, recolour and delete have existed at `/laparoscopy/api/region-types/<pk>/` since
  Phase 10 and the page called none of them; each row now offers all three, plus a
  per-region show/hide (view state, deliberately not stored -- a persisted flag would
  follow a reader to another workstation and read there as a missing annotation). A new
  region type is born in the next unused colour from a shared palette instead of every
  one being the same blue. A recolour writes the LUT entry directly
  (`config.color.setSegmentIndexColor`): re-registering the representation is what colours
  a *new* region and short-circuits for one already on screen, so the swatch would have
  moved and the mask would not.
- **An annotation list, with the tool that drew each mask.** One row per (region, frame)
  -- the addressable unit of this record -- naming the tool, the region and the instant,
  with go-to-frame, move-to-another-region and clear. The tool is carried through the save
  body and the NPZ archive under a new self-describing `mask_tools` key; an archive
  written before it reads back unchanged, and its masks show no tool rather than an
  invented one, because the tool was never recorded anywhere.
- **Quadrants can be created in the page.** `#quadrant-types-panel`,
  `#timeline-add-class-btn` and `#timeline-class-admin-list` were authored in Phase 10
  with their own chip CSS and appeared in no JavaScript file at all, so the panel kept
  `d-none` for its whole life, `activeQuadrantId` could never leave null, and "Add
  Marker" could only ever answer "Pick a quadrant before adding a marker." The panel is
  bound now: add, rename, recolour and delete, with the reassignment the delete endpoint
  demands when markers still use the type.

- **A project could not be created from the Django admin, and could not be deleted at
  all.** Both defects came from the same place -- the admin was still treating a project
  as if it were a folder, which is what it used to be before the folder->project
  migration made folders a sub-organization *inside* a project.
  - **Create.** `Project.created_by` is a `null=True` audit column that was never
    `blank=True`, so every `ModelForm` built from it -- the admin's add page, which the
    control panel's "New project" button links straight to -- made it a required picker
    and refused the POST with `created_by: This field is required`. It is `blank=True`
    now and the admin fills it from the request. `Modality` and `AnnotationMethod`
    carried the identical defect on the identical column and are fixed with it.
  - **Delete.** `Project` CASCADEs to patients, which CASCADE to `FileRegistry`, which
    the annotation graph guards with `PROTECT` (`SourceResource.file`,
    `AnnotationPayload.file`, `AnnotationTarget.source_resource`). `PROTECT`, unlike
    `RESTRICT`, raises *even when the protecting row is part of the same cascade*, so the
    confirmation page rendered as "cannot be deleted" and the POST came back 200 with the
    project still there -- a silent no-op for any project whose patients had ever been
    annotated. The guards are deliberate: destroying annotation work has to be an
    explicit decision. `common/deletion.py` is now that decision, in one place
    and in dependency order -- the annotation items (which `PROTECT` the targets and
    selectors they are anchored to, and so cannot be left to the set's own cascade),
    then the annotation sets, then the source resources naming the
    project's files, then the project, then the objects in storage by the keys the
    `FileRegistry` rows recorded (a row's `file_path` is an object key *or* a bundle
    prefix; listing the prefix covers both). The confirmation page states real counts,
    annotation items and payloads included, and says the loss is permanent.
  - **Who may.** Creating and deleting a project is now a superuser act; editing one
    keeps the ordinary `change_project` permission, and folders stay creatable in the app
    by project admins. The control panel's button is hidden from staff who cannot use it.
  - **Folders read as folders.** The three copies of `FolderAdmin` are one
    `DomainFolderAdmin`, whose project picker is scoped to the admin's own domain -- it
    offered every project in the database, so a maxillo folder could be filed under a
    brain project: invisible to every project-scoped listing while pointing across
    domains. Folders also appear inline on their project, where the hierarchy is.
- **Two of the three domains could create folders they could never delete.** The
  endpoint existed for brain alone, and no UI in any domain called it -- the folder
  context menu offered statistics, rename and permissions, and no way to remove the
  folder. The rule is one function now (`common/deletion.py`, beside the project
  cascade, because the two rules are opposites worth reading together: deleting a
  project destroys everything below it, deleting a folder destroys nothing but the
  folder). Maxillo's view serves laparoscopy as well -- laparoscopy includes
  `maxillo.app_urls` under its own namespace -- and brain's copy now defers to the
  same function. Patients are never deleted: `Patient.folder` is `SET_NULL`, so they
  stay in their project and simply stop being filed, and the context menu says so
  before asking. Two defects fell out of the consolidation:
  - brain's copy counted only the folder's **direct** patients, so a folder whose
    sub-folders held patients read as empty and took them silently. `Folder.parent` is
    CASCADE, so the question is about the whole subtree; soft-deleted patients count
    too, being restorable rows the cascade would unfile.
  - brain's permission check passed the domain slug `"brain"`, which makes
    `_project_from_context` fall back to *the first active brain project by name* --
    so with more than one brain project it consulted the wrong one, refusing that
    folder's own admins and admitting another project's. Both views now check against
    the folder's own project, which is the only project that owns it.
- **Every project created from the control panel became a maxillo project.** "New
  project" was a single hardcoded link to `maxilloproject/add/`, and a project's domain
  is immutable and forced by the admin class that serves it -- so the choice of domain
  is the choice of button. There is now one per domain, reversed from the admin URLs
  (via `common.domains.project_admin_add_targets`, keeping "adding a domain is a
  one-line change" true) so a renamed proxy breaks a test rather than a user's link.
- **A seventh round: four surfaces, four defects that a data-path check cannot see.**
  - **The CBCT 3D segmentation had no depth in it.** Every tooth on the far side of the
    arch showed through the near side, and rotating changed which colours won rather than
    what occluded what. Last round put the labelmap actor on a composite through
    `setRenderMode`, and that correction could never arrive in time:
    `addSegmentationRepresentations` is **synchronous and returns `undefined`** -- it files
    the representation and leaves the actor to the segmentation render loop -- so the
    `await` at the call site resolves while the viewport still holds only the study, and
    the walk over its actors finds no labelmap to correct. Meanwhile Cornerstone had
    already built the actor with its own default, `config?.blendMode ??
    MAXIMUM_INTENSITY_BLEND` (`legacyVolumePlan.js`), set on the mapper by
    `createVolumeActor`; a MIP through a labelmap takes the largest *label value* along
    each ray, which is a flat map rather than structures in depth. The blend mode is now
    asked for at registration (`solidVoxelConfig`), so there is no race to lose. The
    re-application stays: a drop rebuilds the actors and it is what puts them back.
  - **The panoramic arch was drawn, filed, and then discarded on the way to the screen.**
    `setArch` built its `SplineROITool` annotation by hand and omitted `isVisible`.
    `filterAnnotationsWithinSlice` -- which every volume viewport's render *and* every hit
    test passes through -- does a bare `if (!isVisible) continue`, so the arch was never
    drawn and no handle was ever found under the pointer: the automatic spline appeared to
    work and its control points could not be moved. `isLocked` and `FrameOfReferenceUID`
    are stated too; upstream's own `hydrate` sets all three, and this is that object built
    by hand. **And the mandible under it was invisible for two separate reasons.**
    `setMask` drew through a window/level, which is a greyscale ramp -- so the blue was
    white, and the mask's *zero* voxels were painted as translucent black over the whole
    slice, darkening everything except the region meant to stand out. `MASK_COLOR` had
    been declared and used by nothing. It is now a colour transfer function plus a
    piecewise opacity, which is how a `vtkImageSlice` composites. The mask also carried no
    direction matrix, so on the usual CBCT affine -- positive diagonal in RAS, negative in
    LPS -- it was mirrored about the origin; its axes now come from `indexToWorldLps`, the
    same function that places an arch control point.
  - **The laparoscopy annotator worked on the first frame and no other.** `prepareFrame`
    called `addSegmentations` with a fixed per-region id for every frame it prepared, and
    `SegmentationStateManager.addSegmentation` **throws** on an id it already holds. The
    throw came out of `prepareFrame`, through `showFrame`, and into an unhandled
    rejection: masks unpainted, frame navigation half-applied, nothing reported. A region
    is now registered once and its labelmap layer *grown* per frame with
    `updateSegmentations`, which is also what lets Cornerstone resolve the labelmap
    belonging to the frame on screen. Two things around it that made the surface hard to
    read: every region drew in Cornerstone's default colour, because each is its own
    segmentation using its own segment 1 and none was given a LUT -- so the swatches in
    the region list described nothing; each region's `#rrggbb` is now its mask's colour.
    And the selected region was marked only by Bootstrap's `.active`, a faint grey fill
    that is the difference between selected and hovered; it is now filled with the
    region's own colour, in a foreground chosen for contrast, and says so through
    `aria-pressed`. The list also sat flush against the card border -- it had no
    `card-body`.
  - **Zooming the laparoscopy video did nothing at all.** The buttons called
    `viewport.setZoom?.(viewport.getZoom?.() * factor)`. Optional chaining reads as a
    guard and is not one: both methods exist, inherited from `Viewport`, and the inherited
    `getZoom` goes through `getVtkActiveCamera()`. `VideoViewport` sets
    `useCustomRenderingPipeline = true`, so the engine never makes it a vtk.js-driven
    viewport and never adds a renderer for its id -- `getZoom()` throws before `setZoom`
    is reached. Zoom now goes through the camera pair the class implements itself, via
    `zoomBy`, which owns the one piece of arithmetic that is easy to invert:
    `parallelScale` is half the world height on screen and therefore moves the *opposite*
    way to magnification.
  - **The IOS axis captions floated over the scan they were meant to orient.** The arrows
    are actors and the GPU occludes them correctly; the `x`/`y`/`z` captions are HTML over
    the canvas and were always on top, so a caption stayed legible in exactly the position
    that proves it is behind a jaw -- contradicting the arrow it names, on the one control
    a reader uses to work out which way round the scan is. Each caption is now depth-tested
    against the arches with the mesh picker the surface already owns, coalesced to one
    animation frame because `CAMERA_MODIFIED` fires on every step of a drag. A hidden arch
    cannot hide a caption: `vtkPicker` requires `getNestedVisibility()` before it will
    consider a prop, which is what keeps `viewUpper` honest.

- **A sixth round. The legacy conversion now runs from `migrate`, and it had never
  finished on real data.**
  - **`annotations/migrations/0005` runs `annotations_convert_legacy`.** Upgrading a 1.9
    or 2.0 deployment to 3.0 has to leave a working system without anybody remembering a
    second step, and it did not: 54 browser-generated panoramics sat in object storage
    while the viewer answered 404, because the conversion had never been run there. The
    command's own header argues against a `RunPython` -- unbounded rows, no resume, a
    blocked deploy -- and the migration answers each: it works patient by patient in its
    own transaction, it is idempotent so a re-run *is* the resume, and
    `continue_on_error` keeps one odd row from stranding an upgrade.
  - **Three bugs stood between that command and a complete run**, all found by running it:
    - **The region schema was keyed on the wrong project.** Region types predate the
      project registry, so a stored stroke can name a `RegionType` that now belongs to a
      different project than its patient does. `_resolve_label` refused it -- correctly,
      for a live save -- and the conversion aborted on `label code 'Tool' is not defined
      in schema 3`, taking every later surface with it. `region_label_schema` now takes
      `extra_codes`, so history is representable while the live UI still offers only the
      project's own types.
    - **`EventAnnotationItem.value` could not hold a transcript.** It is a
      `CharField(255)`; every domain stores `text_caption` as a `TextField`, and 4072 of
      4111 maxillo captions and all 6 brain ones are longer than 255 characters. MySQL
      answered `Data too long for column 'value' at row 1` and **no voice caption had
      ever converted**. Widened to `TextField` in `0004` -- the column is in no index, so
      there is no prefix-length question.
    - **The panoramic conversion dropped half of what it read.** `PanoramicState` carries
      `mip_file`, `raysum_file` and a segmentation source; the converter wrote only the
      arch. `panoramic_arch_state` reads the strips back as `png_render` payloads and
      `expected_fingerprint` covers the segmentation as well as the volume, so a study
      converted this way had an arch, no strips, a fingerprint that could never match,
      and a 404 over two PNGs sitting in storage -- "Panoramic image not available", as
      reported. It now calls `services.panoramic.save_panoramic_arch`, the same writer
      the live save uses, and `_panoramic_converted` treats a strip-less conversion as
      *not done* so a re-run repairs it. Nothing is deleted to do that: `AnnotationTarget`
      is `PROTECT`ed on purpose, and the repair lands as a new revision.
  - **The laparoscopy region panel was blank and said nothing.** Patient 12's project
    defines no `RegionType` rows, so every drawing tool answered "Pick a region before
    drawing on one" over a panel offering nothing to pick -- a true sentence with no
    action behind it. The panel now says the project defines none, and the **Add** button
    that has been in the template since Phase 10 bound to nothing is wired to
    `/laparoscopy/api/region-types/`. A region created that way is handed to the editor
    (`addRegion`), which clears its per-frame labelmap cache so the new region has
    buffers, rather than requiring a reload.
  - **The timeline stood still while the video played.** "Playing is for looking; the
    playhead catches up when it stops" is the right rule for *annotation* state -- running
    `showFrame` sixty times a second would rebuild a labelmap per frame -- and it was
    applied to the readouts too, so the clock and the playhead froze for the whole of a
    recording. `showTime` now takes an instant, and playback drives it from the video
    element's own `timeupdate`. No annotation state follows a playing video.
  - **The 3D segmentation rendered outlines, not voxels.** Two causes, both visible in the
    screenshots. Cornerstone's labelmap default is a 3px outline over a 50% fill, which is
    right on a slice and is exactly the translucent shells reported on the volume render;
    the 3D window now gets `solidVoxelStyle()` per viewport while the slices keep their
    outline. And the study's render mode was applied to *every* actor -- itself a fix for
    a real mismatch -- which put the labelmap on an attenuated **maximum-intensity**
    projection: that takes the largest *label value* along each ray, so the
    highest-numbered tooth wins wherever two overlap regardless of depth. A labelmap is
    not an intensity field; it is composited and shaded (`LABELMAP_RENDER_SPEC`), and
    `setRenderMode` tells the two apart by the window's own volume id.

- **A fifth round: five defects, three of them one missing signal each.**
  - **The laparoscopy recording was a black box, and always had been.** The viewport is
    built while `#video-annotate-viewport` still carries `d-none`, so `enableElement`
    sizes its canvas to 0x0; the page removes the class a moment later and nothing told
    Cornerstone. `[ygg-video] mounted` was reported over a canvas with no pixels in it.
    The volume grid solved this in Phase 3 and the photo stack had quietly grown its own
    private copy of the same helper, so `isMeasurable`/`observeSize` moved to
    `imaging/runtime/elementSize.js` and all three surfaces use the one. Moving the
    element into `#video-player-wrap` last round changed nothing about this: the box was
    black at the bottom of the page too.
  - **A slice change on the panoramic arch threw, and took the whole editor with it.**
    `showSlice` called `setViewReference({sliceIndex})`. That is not a view reference:
    `BaseVolumeViewport.setViewReference` takes its slice branch only when
    `viewRef.volumeId` matches the viewport's, every other branch is gated on the frame
    of reference, and with neither field present the last `else` **throws**
    `Incompatible view refs: undefined!==1.2.840.10008.1.4`. The throw happened inside
    the geometry worker's `onmessage`, so it was uncaught and abandoned the rest of
    `onGeometry` -- no arch, no CPR, no live pane, no bake. "Generated panoramic" stayed
    empty forever. It now passes `getViewReference({sliceIndex})`, which is the companion
    the library intends, and the worker bridge routes a throwing callback to `onError` so
    the next handler bug becomes a message on the panel rather than a stack trace.
  - **The brain grid put one sequence's window on all four.** Every window joined the VOI
    synchroniser at construction, and `voiSyncCallback` copies the source's **absolute**
    `voiRange` onto its targets. That is right on the CBCT grid -- four planes of one
    study -- and wrong on the brain grid, where the four windows hold FLAIR, T1, T1c and
    T2 on four different intensity scales. It fires on the opening `setProperties` too,
    so three of the four wore the first-loaded volume's window before anybody touched
    anything, and every later drag re-broke them. Membership now follows the volumes
    rather than the layout: `voiSyncGroup` puts windows in the synchroniser exactly when
    they are showing the same volume, and a grid whose windows disagree synchronises
    nothing. Each sequence keeps the `openingVoi` derived from its own data, which is
    what makes the four look like each other.
  - **An empty collection is no longer a 404.** The intraoral and teleradiography
    listings answered "this patient has none" with 404, so every CBCT page load logged
    two failed requests in the browser console and the photo stack said "These images
    could not be listed" over a study where nothing had gone wrong -- and a real failure
    was indistinguishable from an ordinary patient. They return `{"images": [], "count":
    0}` at 200, which is the shape `readImageRecords` already normalises both endpoints
    into, so the viewer reaches its own "There are no images on this study yet." message
    with no new branch. 404 still means a *named* thing is absent: an unknown patient, a
    panoramic variant asked for by name, the bytes of an image that is not there.

### Added
- `yggdrasil/settings_sqlite_test.py`, so the Django suite runs against a throwaway
  SQLite file instead of requiring the production MySQL server. `settings.py` hard-wires
  MySQL and validates its credentials at import; a suite that only ever touches a `test_`
  database should not need a database server to be reachable.

- **A fourth round. Two of the third round's fixes were the cause of two of these.**
  - **The crosshair drew clean lines and could not be moved.** Turning
    `getReferenceLineDraggableRotatable` off removed the rotation circles, as intended,
    and also removed every *translation* the tool performs: `_jump` — which is what a
    click on the image runs — filters the other viewports through
    `controllable && draggableRotatable && sameScene` and returns without moving anything
    when that leaves an empty list (`CrosshairsTool.js:942-952`); `_dragCallback`'s
    `OPERATION.DRAG` branch filters on the same flag, and `addNewAnnotation` builds
    `activeViewportIds` from it too. So neither a click nor a drag did anything, on the
    one control that navigates a CBCT. The handles now go through the switch that removes
    *only* handles — the tool's `minimal` profile, which forces both handle flags false in
    the drawing and hit-testing paths and never even computes the slab handle points —
    while `_jump` and `_dragCallback` read the raw callbacks, left at their default. The
    profile's own purpose is a 40px stub, so `lineLengthInPx` is set past any canvas and
    the tool's existing `liangBarksyClip` gives back the full-width lines. `mobile` stays
    off for the reason the third round found. The test now pins the *separation* — a bump
    that merged the drawing switch into the navigation one would otherwise bring back
    either the clutter or the frozen crosshair, with no build error.
  - **Three of the video annotator's tools were never registered.** `TOOL_PLAN` and
    `VIDEO_TOOL_NAMES` were written from the class names minus `Tool`, and three of
    Cornerstone's `toolName` statics do not follow that pattern:
    `RectangleScissorsTool.toolName` is `'RectangleScissor'`, `CircleScissorsTool.toolName`
    is `'CircleScissor'`, and `PlanarFreehandContourSegmentationTool.toolName` keeps its
    suffix. `ToolGroup.addTool` answers an unknown name with `console.warn` and a bare
    `return`, so the scissors and the polygon tool were absent from the group and their
    toolbar buttons did nothing — three warnings in a browser console and a green suite.
    The names are corrected, `VIDEO_TOOL_NAMES` is derived from `TOOL_PLAN` rather than
    typed a second time, the `LivewireContourSegmentationTool` registration that no group
    added and no button named is gone, and `frontend/tests/videoToolNames.test.js` reads
    the real statics out of `node_modules`.
  - **The laparoscopy recording rendered at the bottom of the page.**
    `#video-annotate-viewport` was emitted with the payload and the entry tag, after the
    back button and the saving indicator, so the annotation toolbar and the frame bar sat
    over an empty black box and drove a viewport below the whole record. It is now inside
    `#video-player-wrap`, beside the placeholder it replaces, which is where both the
    reader and `pageControls.js` already expect it.
  - **The IOS viewer lost the legacy 180-degree turn, so both arches were upside down.**
    `ios.js:368` and `:394` set `mesh.rotation.y = Math.PI` on **each arch**, and the
    camera vectors transcribed into `cameraPresets.js` were written against that rotated
    scene. The port kept the cameras and dropped the rotation — and it was right to: a
    landmark is stored as a raw STL vertex coordinate and `vtkCellPicker` reports world
    positions, so transforming the actors would move every historical landmark. The same
    half-turn is carried by the *cameras* instead, which is visually identical and touches
    no coordinate: `LEGACY_CAMERA_PRESETS` holds the untransformed numbers and
    `CAMERA_PRESETS` is derived from them by `Rᵧ(180)`, so the relationship is asserted
    rather than a table of hand-rotated vectors.
  - **The reference axes are labelled.** Three coloured arrows with nothing naming them
    read as decoration, and "which one is Y?" is the question they exist to answer — it is
    also the axis the orientation above is stated in terms of. `x`, `y` and `z` are HTML
    over the canvas, projected from each arrowhead through `worldToCanvas` on every camera
    change, in `vtkAxesActor`'s own three colours. Not `vtkVectorText` (extruded geometry
    the arches would occlude) and not `vtkTextActor` (the same projection, less legibly);
    `pointer-events: none`, so a caption cannot swallow a landmark placement.

- **A third round, and two of the second round's fixes were shipped and did not work.**
  Both had the same shape as everything else on this branch: the code was written, it was
  built into the bundle, and a library escape hatch or a load order meant nothing ever
  read it. A green suite saw none of it.
  - **The crosshair's square and circle were never gated on the switches that were
    turned off.** `crosshairLinesOnly` set `getReferenceLineDraggableRotatable` and
    `getReferenceLineSlabThicknessControlsOn` to false, correctly, and the handles stayed
    on screen — because `CrosshairsTool.js:533-538` does not read those callbacks alone:

        this._getReferenceLineDraggableRotatable(id) || this.configuration.mobile?.enabled

    `mobile` defaults to `{enabled: isMobile()}`, and `isMobile()` is
    `matchMedia('(any-pointer:coarse)').matches` — **true on any machine with a
    touchscreen attached**, which a clinical workstation frequently is. Mobile mode ORed
    past both flags, and its draw branch is `(lineActive || mobile.enabled) && …`, so the
    rotation circle and the slab-thickness square were drawn *permanently* rather than
    during a drag, at `handleRadius: 9` and hit-testable — which is also what swallowed
    the primary drag and left the lines standing still under the cursor. The
    configuration now turns the touch profile off, and a test reads the real
    `CrosshairsTool.js` so a version bump that moves that `||` fails the build instead of
    quietly bringing the clutter back.
  - **The overlay threw on every viewport that held nothing.** `refreshOverlay` read
    `viewport.getImageIds?.()?.length`, and `BaseVolumeViewport.getImageIds` *throws*
    when there is no volume actor — `?.` guards a missing method, not a throwing one. So
    every camera event on an empty window raised `No actor found for the given volumeId:
    undefined`: during `setVolumes` on the CBCT grid, and permanently on the brain grid,
    where the unguarded `refreshOverlays()` at the end of the mount took the entire grid
    down with it (`The volume grid failed to start`), losing the toolbar and the
    segmentation control. It reads `getNumberOfSlices()` now, which goes through the same
    `getImageSliceDataForVolumeViewport(this) || {}` the neighbouring `getSliceIndex`
    already relies on and answers `undefined` instead of throwing.
  - **The brain grid is four axial windows again, and has no crosshair.** The previous
    round bent the layout to suit the tool; the layout is the requirement. This surface
    compares *sequences* — FLAIR against T1 against T1c against T2 — and four axial
    windows is what that looks like. The consequence is handled rather than hidden:
    `supportsCrosshairs` asks whether a layout has two or more non-parallel slice planes,
    and a grid that fails it never registers the tool, so the left mouse button goes to
    window/level and the toolbar offers that button instead of a crosshair. A control
    that looks pressed and does nothing is the defect, not the fix. The CBCT grid is
    unchanged, and its "For crosshairs to operate, at least two viewports must be given"
    warning is gone too: the tool is now activated *after* the viewports join the group
    rather than before.
  - **The 3D segmentation was two volumes projected differently in one renderer.** The
    labelmap reaches the `volume3d` window correctly — Cornerstone adds it as a second
    `vtkVolume` — but it arrives under vtk's default composite blend while the study
    beneath it is an attenuated MIP, because `legacyVolumePlan` asks for a blend mode on
    the volume input and `VolumeViewport3D.setBlendMode()` is a no-op returning `null`.
    That mismatch is the haze that was reported as "a weird broken segmentation". The
    render mode now applies to *every* actor in that viewport rather than
    `getActors()[0]`, and the segmentation control re-asserts it after adding the
    representation, which is also what makes `reapply()` correct after a drop.
  - **The segmentation no longer freezes the tab while it loads.** The labelmap was
    filled one voxel at a time through `voxelManager.setAtIndex`, which re-resolves the
    owning slice and re-marks it dirty on every call — for a CBCT, 10⁸ of them on the
    main thread. `setCompleteScalarDataArray` is the library's own path: one typed-array
    write per slice, each marked once.
  - **The panoramic was never generated, for anyone, because of a load order.**
    `bootstrapPanoramic` gated on `window.canEdit`, which is assigned inside
    `patient_detail.js`'s `DOMContentLoaded` handler — while `{% cornerstone_entry %}`
    emits a *module* script, which is deferred and runs first, with `readyState` already
    `'interactive'`. The gate saw `undefined` and refused on every visit, so the
    unattended pass that produces a patient's default panoramic never ran and no export
    could contain one. `window.scanId` was undefined at the same moment, which is why
    every announcement carried `patientId: null` and the admin warm-up page saw a wall of
    indistinguishable skips. Both now come from `#django-data`, the same resolution
    `grid/bootstrap.js` already documents for `window.CBCTViewer` — the page was served
    with these facts and does not need to wait for a script to copy them onto a global.

- **The laparoscopy annotator: a 500, and a surface that was never wired.**
  - **Every GET of the video state was an HTTP 500.** `patient_video_annotations` called
    `_types_payload(...)["types"]`, and `_types_payload` returns the list itself — the
    `"types"` key belongs to `_handle_type_list`'s *response body*, one function below.
    `TypeError: list indices must be integers`, on every patient, since `f5387cb`. The
    annotator refuses to mount on a non-OK response, so this presented as a laparoscopy
    page with no video on it. It was invisible to the suite because nothing ever issued
    a GET to the view: the service beneath it is well covered and the surface test
    asserted only that the endpoint's *URL* appeared in the payload, so the one line that
    differs between the service and the wire had never been executed. It is now.
  - **A failure to annotate no longer costs the recording.** The surface returned `null`
    the moment that endpoint answered anything but 200, and the page then left the
    viewport hidden and the placeholder on screen — a placeholder reading "No video
    uploaded for this patient." over a file sitting in object storage exactly where it
    should be, which sends somebody looking for an upload that already happened. It now
    mounts either way: **full** when the state was read, and **degraded** when it was not
    — the video plays, the frame navigation works, nothing can be drawn or saved, and the
    page says why in a sentence. The frame-size disagreement keeps its meaning and joins
    the degraded case rather than blanking the page: a stored mask must never be painted
    over a differently-sized recording, and the recording is still watchable while that
    is sorted out. `ready` also gets its own placeholder sentence, because sharing one
    with `absent` is what let the false claim be made at all.
  - **The frame bar, the timeline, the region list and the save button do something.**
    All four were rendered by the template and connected to nothing; the page's inline
    glue polled for the surface every 50 ms and then wired the tool buttons alone.
    `surface.save()`, `goToInstant` and `editor.selectRegion` had **no callers** — and
    because every drawing tool carries `needsRegion: true` and nothing could select a
    region, *no drawing tool could be activated at all*. The glue is now
    `frontend/imaging/video/pageControls.js`, called by the entry as soon as the surface
    exists, so the frame arithmetic and the tool/region rules are unit tests rather than
    clicks. The Magic Tool panel and the shapes list are hidden rather than bound — the
    first is a known release blocker, the second describes per-stroke rows decision #14
    removed — because a control that is present and inert is worse than one that is
    absent.
  - **Every write from that page was going to be a bare 403.** `readCsrfToken` read the
    `csrftoken` *cookie*, and `CSRF_USE_SESSIONS = True` means this deployment sets none;
    the hidden input is the only source, and the page carried no `{% csrf_token %}`. Both
    fixed. Never observed, because the mount failed first.

- **A second round on the same two surfaces, and the first round's 3D fix was wrong.**
  - **The 3D segmentation never needed a surface.** The previous entry claimed a
    `volume3d` viewport "cannot render a labelmap" and built the overlay's 3D half on
    `@cornerstonejs/polymorphic-segmentation`. It can:
    `getViewportLabelmapRenderMode` returns `'volume'` for anything extending
    `BaseVolumeViewport`, and `VolumeViewport3D` does, so the labelmap becomes a second
    volume actor with its own transfer function — which is what NiiVue was doing all
    along. The surface route was also expensive in a way that would have bitten
    regardless: it extracts one mesh per label in a worker, and each extraction calls
    `getCompleteScalarDataArray()`, which allocates *a fresh copy of the whole volume*
    (`VoxelManager.js:649`) — thirty-odd copies of a 10⁸-voxel study for a CBCT's teeth.
    Every viewport now takes the same Labelmap representation and the polySeg add-on is
    no longer registered, because nothing asks it for a conversion.
  - **The per-class visibility list is gone.** All of it or none of it, on the
    maintainer's call. The `segments` map behind it stays and is not optional:
    Cornerstone hides a labelmap by marking every *segment* hidden, so a segmentation
    that declares only segment 1 — which is what a missing `segments` config produces —
    cannot be switched off past its first class.
  - **The crosshair draws lines and nothing else** — ⚠️ *this shipped and did not work;
    see the third round below.* `CrosshairsTool` decorates every reference line with
    rotation circles and slab-thickness squares; both were reported as clutter. Turned
    off through the tool's own `getReferenceLineDraggableRotatable` /
    `getReferenceLineSlabThicknessControlsOn` switches rather than hidden in CSS, so the
    handles and the drags they afford go together — a drag with no handle would be an
    invisible control.
  - **The brain grid had a crosshair and no reference lines, and the reason was the
    layout** — ⚠️ *reverted in the third round below; the layout was the requirement.*
    Its four windows were all axial, and the crosshair draws the intersection lines of
    the *other* viewports' planes: four parallel planes intersect nowhere. The first
    three windows became axial, sagittal and coronal, with the fourth a second axial.
  - **Both grids open darker.** The robust percentiles were 2/98, chosen to match
    NiiVue's `calMinMax` so replacing the viewer would not visibly change a study. Both
    grids were reported as opening too bright, and the old viewer's choice is not a
    reason to keep a window nobody likes; they are 0.5/99.5, so a narrower slice of the
    brightest voxels sets the white point.
  - **`video_state` could say `ready` over a page with no annotator.** It answered
    `ready` whenever the namespace was not `laparoscopy` — a way of saying "this surface
    is not here" — but the template reads it to choose its sentence, so a `ready` with a
    `null` payload left the "No video uploaded" placeholder on screen. It now means one
    thing: the payload is real and the annotator will mount from it.
  - **The video placeholder shows the server's working, for staff.** "No video uploaded
    for this patient." over a file somebody can see in the bucket is a claim, and the
    page had no way to show how it reached it. An administrator now sees which rows
    exist, which lack a probe, and — when none is found — what file types the patient
    *does* have, because a `FileRegistry` row attached to the wrong patient FK is
    invisible to this page while sitting in object storage exactly as expected.

- **Four bugs found by driving the migrated surfaces.** Three are regressions from
  `c03afa6` and `3999899`, and they share a shape worth naming: in each case **the
  server payload and the markup survived and the JavaScript that read them did not**.
  A green suite could not see any of them, which is the calibration the roadmap's
  release section already gives for Phases 4 and 5.
  - **The segmentation overlay is back, on the brain grid and on the CBCT one.**
    `viewer_grid_data.segmentationFile` has been emitted by both views throughout and
    read by nobody since NiiVue was deleted. `imaging/grid/segmentation.js` registers
    it as a Cornerstone labelmap over the loaded volume; the SEG button is a toggle
    plus a **per-class visibility list**, which the single on/off it replaces did not
    have. The palette is the old one value for value — the fixed green/red/blue for
    three or fewer classes, the golden-ratio hue walk above that — because a nicer
    palette would silently recolour every segmentation anyone has approved.
  - **A CBCT segmentation renders on the 3D window again.** NiiVue composited an
    overlay volume into every slice type for free; Cornerstone needs a **Surface**
    representation there, which no labelmap can supply. `@cornerstonejs/polymorphic-segmentation`
    had been bundled since Phase 3 and **its `init` was never called**, so the 3D half
    had nothing behind it. The slice windows get a labelmap and the `volume3d` window
    gets a surface, decided per viewport.
  - **A grid mismatch is refused, not resampled** — the posture `common/interop/seg.py`
    already takes for a SEG whose grid disagrees with its series. Dimensions *and*
    spacing, because two volumes can agree on voxel count and disagree on voxel size,
    and that failure drifts with distance rather than being uniformly offset, which
    reads as an overlay that slides rather than one that is wrong.
  - **Dragging a modality chip onto a window works again, and the brain grid opens
    empty.** The chips have carried `draggable="true"` and the windows a `.drop-hint`
    the whole time. With nothing bound, `primaryVolumeFrom` loaded
    `Object.keys(modalityFiles)[0]` — an arbitrary series, since brain sends no
    `defaultModality` — into all four windows, and no interaction could change any of
    them. Now nothing is fetched until a chip is dropped, and each window says which
    series it holds: four unlabelled greyscale MRIs are four pictures nobody can tell
    apart, which is why `viewer_grid.js` wrote a `.window-label` and why the overlay
    has one again. `enableDragDrop` — in the payload since before 3.0, read by nobody —
    is what tells the two surfaces apart, and `brain/views.py` now states it rather
    than relying on a client-side default.
  - **"No video uploaded for this patient." was said over a stored, playable
    recording.** The annotator refuses to mount without a recorded `ffprobe` result,
    correctly — a browser cannot read a frame rate, and guessing 30 for a 25 fps
    recording mis-files every mask while looking right. **Nothing recorded one.**
    `video_probe.probe_and_record` had exactly one caller,
    `annotations_rasterize_video_masks`, which visits only patients carrying *legacy
    stroke rows* and writes onto the `video_raw` row — while the page ranks a
    `video_processed`/`compressed` derivative first. So a study with a video and no
    legacy annotations could never mount the annotator, and one that had been
    rasterised was asked about the wrong row. Three fixes, because it was three bugs:
    the upload path records a probe as the file arrives; `laparoscopy_probe_videos`
    backfills every existing video row, raw and processed alike, and **reports when a
    patient's video files disagree on frame size** rather than papering over a mask
    that cannot describe the file being played; and the page picks the highest-ranked
    row it can actually *describe*, falling back to the top-ranked one so playback
    survives either way. The placeholder now distinguishes "no video" from "not yet
    analysed", because telling someone a stored file was never uploaded sends them
    looking for it instead of running a command.
  - **The widest `except` on the patient page no longer answers a bug with a claim
    about the data.** Any failure building the video context — a mistyped URL name
    included, which the code's own comment records having happened — became
    `has_video = False`. It still catches, so a failure cannot take the patient record
    down with it; what changed is that it logs, and reports `video_state = 'error'`
    instead of "this patient has no video".
  - **The admin offers a project only what its domain has.** Bite classification could
    be enabled on a brain or laparoscopy project. `AnnotationMethod.domain` has existed
    since migration 0043 and the admin never consulted it; `Modality` had no domain at
    all, so one is added mirroring it exactly, blank meaning available everywhere, and
    backfilled from the three per-domain seeder commands that were the de-facto map.
    A `ProcessingStep` needs no column of its own — it is its modality's. The three
    per-domain project admins were three copies of one class differing in a hardcoded
    string; they are now `DomainProjectAdmin` plus a `domain` attribute each.
    Filtering the *queryset* rather than hiding options client-side is what makes this
    hold on save as well as on render, and having the domain belong to the admin class
    rather than the object is what makes it work on the **add** form — the case a
    `get_object(request)` filter cannot serve, and the case reported.

### Added
- **The laparoscopy annotator is on Cornerstone3D, and Konva is gone from the whole
  repository (roadmap Phase 10).** `laparoscopy_annotator.js` and its six mixins —
  **4,919 lines** — are deleted along with both Konva CDN tags, so no page in Yggdrasil
  loads Konva. This was the last of the four original frontend stacks.
  - **The record is a labelmap per annotated frame** (decision #14), one plane per
    region, because regions overlap and a single-valued labelmap cannot say so. Keyed by
    milliseconds rather than frame index — the frame rate is a property of the file, so a
    record keyed by frame number stops meaning the same instant the moment a video is
    re-encoded — and by label code rather than class axis, because the export's axis is
    the project's region types in order and adding a category would otherwise re-label
    every historical study.
  - **The per-stroke API is gone with the strokes.** Once the eraser has mutated pixels
    there is no "the stroke with id 41" to `PATCH` or `DELETE`, so the two region routes
    became one whole-state `GET`/`PUT` with `expectedRevision` and a 409 — the shape
    every migrated surface uses.
  - **NPZ export is regenerated from labelmaps and keeps its bytes** (decision #15). The
    migration command and the export rasterise through the same function, replaying
    strokes in their recorded order because the eraser is destructive; the frozen tests
    are unchanged and a new one exports the same study down both paths and compares every
    frame. A patient the migration has not reached still exports from strokes, so it can
    be run at leisure rather than in the deploy window.
  - **The page refuses to mount the annotator for a video with no recorded probe.** A
    browser cannot read a video's frame rate; guessing 30 for a 25 fps recording puts
    every mask on the wrong frame and looks entirely correct. The rate is `ffprobe`'s,
    cached on the file's registry row.

### Known gaps
- **The Magic Tool (SAM2) is not wired on the new video surface.** Its client was a mixin
  on the deleted annotator's prototype and needed **58 members** from it — shape
  registration, the mask overlay renderer, the pending-scope bookkeeping, the timeline's
  drag state — so it could not survive that file's deletion, and shipping it as a mixin
  nothing applies would have been dead code that reads as live. The WebSocket contract is
  untouched (decision #9): the GPU worker, the protocol and the Django proxies are exactly
  as they were, and the labelmap sink it will write into is built and tested. Only the
  host is missing. **This is a capability regression and a release blocker**, recorded
  here rather than deferred quietly.
- **Finding F20 was wrong and is withdrawn.** The first draft of this phase concluded
  Cornerstone could not render a labelmap on a `VideoViewport` and built a bespoke frame
  decoder and a second image loader around it. `VideoViewport` defines both of the methods
  the labelmap path calls and wraps each labelmap in a `CanvasActor` for exactly that
  purpose; the error came from a truncated grep and from not re-reading the file. It was
  caught by writing the test meant to pin the finding, which failed. The roadmap keeps the
  entry, withdrawn, because the near miss is more useful than the fact.

### Added
- **Interchange export (roadmap Phase 9): the annotation record leaves as DICOM SEG,
  SR and RTSTRUCT.** Server-authoritative, because `common/export_processing.py` has
  no browser. Three new export artifacts under `interop/`, produced from the durable
  record rather than from a stored document, in the shape decision #20 established.
  Adds `highdicom`.
  - **Only for annotations anchored to a natively-stored DICOM series, and that is a
    design decision rather than a limitation to be worked around.** All three objects
    reference source SOP instances — a SEG carries `ReferencedSeriesSequence` and
    inherits the source's Frame of Reference, an RT Structure Set names one per ROI, an
    SR's evidence is a list of composite objects. A patient whose CBCT arrived as a
    `.nii.gz` has no DICOM identity to reference, and fabricating a Secondary Capture
    series so the export had something to point at would file an invention as
    provenance. Such a patient contributes no interop files, and the artifact
    descriptions say so before anyone selects them.
  - **An uncalibrated measurement stays uncalibrated, and the unit is what carries it.**
    A length taken with no known pixel spacing is written in UCUM `{pixels}`, never
    converted. The first draft also set a "not calibrated" code as the measurement's
    qualifier; that maps onto **Numeric Value Qualifier (0040,A301)**, whose defined
    terms are CID 42 — "value out of range", "measurement failure", "not a number". It
    qualifies a value as *unusable*, so that encoding discarded the measurement in the
    act of trying to caveat it, and a strict receiver rejects a code outside CID 42
    outright. There is no standard concept for "uncalibrated" and a private one would be
    a string only this repository can read, so the attribute stays absent and a test
    asserts its absence with the reasoning attached.
  - **Risk 13's premise was wrong.** The register says "highdicom's RTSTRUCT writer is
    newer and less exercised than its SEG writer, which is why SEG and SR ship first".
    highdicom 0.28.1 ships `seg`, `sr`, `pm`, `ann`, `ko`, `pr`, `sc` and `legacy` and
    **no RTSTRUCT writer at all**. The IOD is small and fully specified, so it is built
    attribute by attribute against `pydicom` and every object written in the tests is
    read back, its contours re-derived and compared to the rows they came from. A
    round trip is a stronger claim than "we used a library" would have been.
  - **A box and a sphere are omitted from an RTSTRUCT, not approximated.** DICOM has no
    such ROI primitive; tessellating a sphere into planar contours would file a
    rendering choice as clinical data. A RAS-stored shape is dropped for the same
    reason — LPS and RAS differ by two sign flips, so a silent conversion mirrors the
    ROI across two planes and the result looks plausible.
  - **A SEG whose grid disagrees with its series is refused rather than resampled.**
    Since Phase 8 the runner reads the series directly, so its labelmap is already on
    that grid; a mismatch means the two are not the same study and `highdicom` would
    not notice, because it matches frames to source images positionally. Slice
    direction is settled by comparing the stored `ImagePositionPatient` of the first and
    last instances against the affine, so a flipped export is a failed assertion rather
    than a segmentation of the other end of the jaw.
  - Every UID is derived — `HMAC(DICOM_UID_HMAC_KEY, ...)` under the ISO `2.25` arc,
    the same way Phase 8 derives its own — so re-exporting a study produces the same
    objects rather than a second set a receiving system files alongside the first.

### Fixed
- **F21: the DICOM series seal never fired on real work.** Phase 8 added `sealed_at` so
  that annotating a series freezes its instances — the annotation lock guards
  `FileRegistry` *rows*, and a series is one row holding hundreds of objects. The seal
  looked for targets with `kind=dicom_series`, which is the resource `common.dicom.ingest`
  registers and the resource **nothing else ever writes**: the volume grid saves through
  `annotations/views.py`, which registers a `logical_volume` against the `FileRegistry`
  row, because a grid knows a file id and nothing else. So on every study anyone had
  actually annotated the filter matched nothing and every instance stayed rewritable
  underneath its own coordinates. The suite did not see it because its test attached the
  ingest-side resource by hand — the one shape production never produces. Found while
  Phase 9 needed the same resolution, and fixed where it belongs: `series_for_resource`
  asks both ways round — the UID when the resource carries one, otherwise the
  `FileRegistry` row, which is the same row for both because `register_dicom_series` sets
  `file` deliberately — and it is asked of *every* target, because whether there is DICOM
  under an annotation is a fact about the bytes and not about which registrar named them.

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
