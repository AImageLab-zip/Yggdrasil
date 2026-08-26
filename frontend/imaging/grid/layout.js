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
 * The fixed maxillo CBCT layout: three orthogonal slices and 3D on demand.
 *
 * Reproduces `maxillo_cbct_grid_adapter.js` `initFixedCbctGrid`, which loads the CBCT
 * into windows 0-2 as axial/sagittal/coronal and leaves window 3 showing a "Load 3D"
 * button. The laziness is not cosmetic: a volume render of a full CBCT is the most
 * expensive thing the page can do, and most visits never need it.
 */
export const FIXED_CBCT_LAYOUT = Object.freeze([
    Object.freeze({ window: 0, orientation: ORIENTATIONS.AXIAL, lazy: false }),
    Object.freeze({ window: 1, orientation: ORIENTATIONS.SAGITTAL, lazy: false }),
    Object.freeze({ window: 2, orientation: ORIENTATIONS.CORONAL, lazy: false }),
    Object.freeze({ window: 3, orientation: ORIENTATIONS.RENDER, lazy: true }),
]);

/** The free layout brain uses: four independent windows, all axial to begin with. */
export const FREE_LAYOUT = Object.freeze(
    Array.from({ length: GRID_WINDOWS }, (unused, index) =>
        Object.freeze({ window: index, orientation: ORIENTATIONS.AXIAL, lazy: false })
    )
);

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
