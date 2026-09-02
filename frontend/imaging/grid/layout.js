/**
 * The volume grid's shape: four windows, what each one shows, and what that means to
 * Cornerstone.
 *
 * Pure. No Cornerstone import -- the enum values are inlined as the string literals
 * they are (`@cornerstonejs/core/enums/OrientationAxis.js` and `ViewportType.js`,
 * verified against the shipped 5.8.2) so that this module, and the state machine next
 * to it, can be reasoned about and tested without a GPU. {@link assertEnumsMatch} is
 * how that inlining is kept honest: the entry calls it once with the real enums, and a
 * version bump that renames a value fails loudly at start-up rather than by silently
 * producing a viewport that renders nothing.
 *
 * Two things this module deliberately does not do:
 *
 *   - **It does not persist anything.** `viewportId` and `volumeId` are Cornerstone
 *     runtime identifiers, session-scoped by the governing rule in
 *     docs/cornerstone-roadmap.md, and nothing downstream may treat one as an identity.
 *     They are derived here so they are derived *once*, not so they can be stored.
 *   - **It does not decide what a window shows.** That is `windowState.js`. This module
 *     answers "given that window 2 is showing a coronal slice, what does Cornerstone
 *     need to be told?" and nothing else.
 */

/** The grid is four windows. `viewer_grid.js:1291-1315` sizes four real canvases. */
export const GRID_WINDOWS = 4;

/** What a window can be showing. `render` is the optional 3D view. */
export const ORIENTATIONS = Object.freeze({
    AXIAL: 'axial',
    SAGITTAL: 'sagittal',
    CORONAL: 'coronal',
    RENDER: 'render',
});

/** The three that are 2D slices through the volume, and therefore synchronisable. */
export const SLICE_ORIENTATIONS = Object.freeze([
    ORIENTATIONS.AXIAL,
    ORIENTATIONS.SAGITTAL,
    ORIENTATIONS.CORONAL,
]);

/** Cornerstone `ViewportType` values, inlined -- see {@link assertEnumsMatch}. */
export const VIEWPORT_TYPES = Object.freeze({
    ORTHOGRAPHIC: 'orthographic',
    VOLUME_3D: 'volume3d',
});

/** Cornerstone `OrientationAxis` values, inlined -- see {@link assertEnumsMatch}. */
export const ORIENTATION_AXES = Object.freeze({
    AXIAL: 'axial',
    SAGITTAL: 'sagittal',
    CORONAL: 'coronal',
});

/**
 * Translate one of our orientations into the viewport Cornerstone should build.
 *
 * @param {string} orientation one of {@link ORIENTATIONS}.
 * @returns {{type: string, orientation: string|null}}
 */
export function viewportSpecFor(orientation) {
    switch (orientation) {
        case ORIENTATIONS.AXIAL:
            return { type: VIEWPORT_TYPES.ORTHOGRAPHIC, orientation: ORIENTATION_AXES.AXIAL };
        case ORIENTATIONS.SAGITTAL:
            return { type: VIEWPORT_TYPES.ORTHOGRAPHIC, orientation: ORIENTATION_AXES.SAGITTAL };
        case ORIENTATIONS.CORONAL:
            return { type: VIEWPORT_TYPES.ORTHOGRAPHIC, orientation: ORIENTATION_AXES.CORONAL };
        case ORIENTATIONS.RENDER:
            // A 3D viewport has no slice orientation; passing one is how a volume3d
            // viewport ends up silently behaving like an orthographic one.
            return { type: VIEWPORT_TYPES.VOLUME_3D, orientation: null };
        default:
            throw new Error(`Unknown orientation '${orientation}'.`);
    }
}

/** True when this orientation is a 2D slice, and so takes part in slice sync. */
export function isSliceOrientation(orientation) {
    return SLICE_ORIENTATIONS.includes(orientation);
}

/**
 * The fixed maxillo CBCT layout: three orthogonal slices and a volume render.
 *
 * `maxillo_cbct_grid_adapter.js` made the 3D window lazy behind a "Load 3D" button,
 * on the reasoning that a volume render of a full CBCT is the most expensive thing the
 * page can do. In practice the volume is already decoded and in GPU memory for the
 * three slice views, so the render costs a transfer function rather than a second
 * load -- and the maintainer's call is that it should simply be there. Nothing is
 * lazy any more.
 */
export const FIXED_CBCT_LAYOUT = Object.freeze([
    Object.freeze({ window: 0, orientation: ORIENTATIONS.AXIAL, lazy: false }),
    Object.freeze({ window: 1, orientation: ORIENTATIONS.SAGITTAL, lazy: false }),
    Object.freeze({ window: 2, orientation: ORIENTATIONS.CORONAL, lazy: false }),
    Object.freeze({ window: 3, orientation: ORIENTATIONS.RENDER, lazy: false }),
]);

/**
 * The layout brain uses: four axial windows, each showing whatever was dropped on it.
 *
 * This surface compares *sequences*, not planes. Four axial windows side by side is what
 * FLAIR against T1 against T1c against T2 looks like, and it is the maintainer's call.
 *
 * It was briefly three orthogonal planes plus an axial, to give `CrosshairsTool`
 * something to work with -- **the crosshair draws the intersection lines of the other
 * viewports' planes, and four parallel planes intersect nowhere**. That reasoning is
 * correct and the conclusion was backwards: the layout is the requirement, so the
 * crosshair is what goes. {@link supportsCrosshairs} is how that is decided rather than
 * remembered, and `createToolGroups` leaves the tool out of a grid that cannot use it
 * instead of binding the primary mouse button to something inert.
 *
 * Dropping a modality into a window does not change its orientation --
 * `loadVolumeIntoWindows` only swaps what the viewport shows -- so these assignments
 * survive the page being rearranged.
 */
export const FREE_LAYOUT = Object.freeze(
    Array.from({ length: GRID_WINDOWS }, (unused, index) =>
        Object.freeze({ window: index, orientation: ORIENTATIONS.AXIAL, lazy: false })
    )
);

/**
 * Whether `CrosshairsTool` can operate on this layout.
 *
 * It needs **two viewports cutting on different planes**, and neither half of that is
 * negotiable. With fewer than two it warns "For crosshairs to operate, at least two
 * viewports must be given" (`CrosshairsTool.js:190-193`); with two or more that are all
 * parallel, `_calculateToolCenterFromAbsoluteCameras` collapses them to a single unique
 * plane and returns `null` (`CrosshairsTool.js:1360-1366`), so the tool centre is never
 * computed and every click on the image does nothing at all.
 *
 * A grid that fails this must not be given the tool: a left mouse button bound to a
 * tool that cannot act is worse than a left mouse button bound to nothing, because it
 * looks like it is working.
 *
 * @param {object[]} layout entries of `{window, orientation, lazy}`.
 * @returns {boolean}
 */
export function supportsCrosshairs(layout = []) {
    const planes = new Set(
        layout
            .filter((entry) => !entry.lazy && isSliceOrientation(entry.orientation))
            .map((entry) => entry.orientation)
    );
    return planes.size >= 2;
}

/**
 * Runtime viewport id for a grid window.
 *
 * **Not an identity.** Session-scoped, never persisted, never stored in an annotation.
 *
 * @param {number} windowIndex
 * @returns {string}
 */
export function viewportId(windowIndex) {
    assertWindowIndex(windowIndex);
    return `ygg-grid-${windowIndex}`;
}

/**
 * The scheme Cornerstone's *image* loader for NIfTI frames is registered under.
 *
 * Per-slice imageIds look like `nifti:<url>?frame=N`, and the loader mints them itself.
 */
export const IMAGE_LOADER_SCHEME = 'nifti';

/**
 * The scheme our volume ids carry. Deliberately **not** {@link IMAGE_LOADER_SCHEME}.
 *
 * `loadVolumeFromVolumeLoader` (`@cornerstonejs/core/loaders/volumeLoader.js:15-25`)
 * picks the volume loader by the text before the first `:` in the volume id, and falls
 * back to `cornerstoneStreamingImageVolumeLoader` for any scheme nobody registered.
 * That fallback is what we want: the streaming loader builds the volume out of the
 * per-frame imageIds.
 *
 * Using `nifti:` here instead routes *volume* loading into the *image* loader, which
 * then looks up `imagePlaneModule` for an id that has no per-frame metadata and dies on
 * `const { rows, columns } = imagePlaneModule` — a minified "Cannot destructure
 * property 'rows' of 'i'" with nothing pointing at the cause. That is not hypothetical:
 * it is what the first real harness run produced, on all 56 studies.
 */
export const VOLUME_ID_SCHEME = 'ygg-volume';

/**
 * Runtime volume id for one loaded volume.
 *
 * Keyed by the loader URL because that is what Cornerstone's cache is keyed by: two
 * windows showing the same file must share one cached volume, or a CBCT is decoded and
 * held in GPU memory twice. **Not an identity** -- see `viewportId`.
 *
 * @param {string} url the loader URL.
 * @returns {string}
 */
export function volumeIdFor(url) {
    if (typeof url !== 'string' || url.length === 0) {
        throw new Error('A volume id needs the loader URL it is derived from.');
    }
    return `${VOLUME_ID_SCHEME}:${url}`;
}

/** The tool group id. One group per orientation class -- see `toolGroupIdFor`. */
export function toolGroupIdFor(orientation) {
    return orientation === ORIENTATIONS.RENDER ? 'ygg-grid-3d' : 'ygg-grid-2d';
}

export function assertWindowIndex(windowIndex) {
    if (!Number.isInteger(windowIndex) || windowIndex < 0 || windowIndex >= GRID_WINDOWS) {
        throw new Error(
            `Window index must be an integer in 0..${GRID_WINDOWS - 1}, got ${JSON.stringify(windowIndex)}.`
        );
    }
}

/**
 * Check the inlined enum strings against the real ones.
 *
 * Called once, at start-up, with `coreEnums.ViewportType` and
 * `coreEnums.OrientationAxis`. The whole point of inlining the values is that this
 * module stays testable without importing Cornerstone; the cost of that is a copy that
 * can drift, and the copy drifting silently means a viewport that builds without error
 * and renders nothing. So the copy is checked rather than trusted.
 *
 * @param {object} enums
 * @param {object} enums.ViewportType
 * @param {object} enums.OrientationAxis
 * @throws {Error} listing every value that no longer matches.
 */
export function assertEnumsMatch({ ViewportType, OrientationAxis }) {
    const problems = [];

    for (const [name, value] of Object.entries(VIEWPORT_TYPES)) {
        if (ViewportType?.[name] !== value) {
            problems.push(`ViewportType.${name}: expected '${value}', got '${ViewportType?.[name]}'`);
        }
    }
    for (const [name, value] of Object.entries(ORIENTATION_AXES)) {
        if (OrientationAxis?.[name] !== value) {
            problems.push(
                `OrientationAxis.${name}: expected '${value}', got '${OrientationAxis?.[name]}'`
            );
        }
    }

    if (problems.length) {
        throw new Error(
            'Cornerstone enum values have changed under frontend/imaging/grid/layout.js:\n  ' +
                problems.join('\n  ') +
                '\nUpdate the inlined constants and the tests that pin them.'
        );
    }
}

/**
 * How long a reference line is drawn, before it is clipped to the canvas.
 *
 * {@link crosshairLinesOnly} runs the tool in its `minimal` profile, and that profile
 * exists to draw a *short stub* either side of the centre -- 40 px by default. This grid
 * wants the full-width lines it has always had, and the tool gives that for free: the
 * minimal branch builds each line as `centre + unit * lineLengthInPx` and then runs the
 * same `liangBarksyClip` against the canvas box that the normal branch does, so any
 * length past the canvas diagonal produces exactly the line the normal branch would.
 * The normal branch itself uses `canvasDiagonalLength * 100`; this module cannot see a
 * canvas, so it names a length no viewport will ever exceed.
 */
export const CROSSHAIR_LINE_LENGTH_PX = 100000;

/**
 * Crosshairs that draw reference lines and nothing else -- and still navigate.
 *
 * `CrosshairsTool` decorates each reference line with two kinds of handle: circles that
 * rotate the plane, and squares that drag the slab thickness. Both were reported as
 * clutter -- "an additional square and circle on all axis" -- and neither is wanted here:
 * this grid's planes are orthogonal by construction, and its slab thickness is not a
 * control the application exposes anywhere.
 *
 * **This was done by turning `getReferenceLineDraggableRotatable` off, and that switch
 * does not mean what its name suggests.** It gates the *rotation handles*, yes -- and it
 * also gates every **translation** the tool performs. `_jump`, which is what a click on
 * the image runs, filters the other viewports through
 *
 *     this._getReferenceLineControllable(id) && this._getReferenceLineDraggableRotatable(id) && sameScene
 *
 * and returns `false` without moving anything when that leaves an empty list
 * (`CrosshairsTool.js:942-952`). `_dragCallback`'s `OPERATION.DRAG` branch filters on the
 * same flag (`:1029-1035`), and `addNewAnnotation` builds `activeViewportIds` from it
 * too, so the drag had nothing to act on either. The result was a crosshair that drew
 * three clean green lines and could not be moved by clicking or by dragging -- the
 * reported bug, and a strictly worse one than the clutter it was fixing.
 *
 * So the handles are removed by the switch that removes *only* the handles: the tool's
 * `minimal` profile. Under it, and only in the drawing and hit-testing paths,
 * `viewportDraggableRotatable` and `viewportSlabThicknessControlsOn` are forced false
 * (`:533-538`), the slab handle points are never even computed (`:588`), and
 * `_getRotationHandleNearImagePoint` / `_getSlabThicknessHandleNearImagePoint` refuse to
 * match (`:1668`, `:1695`) -- so neither handle can be drawn *or* grabbed invisibly.
 * `_jump` and `_dragCallback` read the raw callbacks, which are left at their default
 * `true`, so click-to-navigate and line dragging work exactly as they always should have.
 * {@link CROSSHAIR_LINE_LENGTH_PX} is what keeps the lines full length under that profile.
 *
 * **`mobile.enabled` is the third switch, and it stays off.** `mobile` defaults to
 * `{enabled: isMobile(), ...}` (`CrosshairsTool.js:80-85`) where `isMobile()` is
 * `matchMedia('(any-pointer:coarse)').matches` (`utilities/touch/index.js:153-157`) --
 * **true on any machine with a touchscreen attached**, which a clinical workstation
 * frequently is. Mobile mode raises `handleRadius` to 9 and draws the handles
 * *permanently* rather than during a drag (`:622-641`), which is how the clutter got
 * reported in the first place. This grid is driven with a mouse.
 *
 * `getReferenceLineControllable` is deliberately left alone -- dragging a line is how the
 * crosshair navigates, and that is the point of having it.
 *
 * @returns {object} configuration for `toolGroup.addTool`.
 */
export function crosshairLinesOnly() {
    return {
        minimal: { enabled: true, lineLengthInPx: CROSSHAIR_LINE_LENGTH_PX },
        mobile: { enabled: false },
    };
}
