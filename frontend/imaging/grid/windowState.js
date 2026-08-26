/**
 * What each of the four grid windows is showing, and which of them move together.
 *
 * Pure state, deliberately separated from the viewports it describes. `viewer_grid.js`
 * held this as eight module-level objects mutated from twenty places
 * (`windowStates`, `windowLoadGenerations`, `synchronizationGroups`,
 * `freeScrollWindows`, `measurementOverlayState`, …), and the interesting bugs in a
 * multi-window viewer are all in the transitions between them, not in the rendering.
 * Here they are one object with named transitions and a test each.
 *
 * Three behaviours are carried over from the old implementation on purpose, because
 * each one was earned:
 *
 *   - **Load generations.** `beginLoad` bumps a per-window counter and returns it;
 *     an async load may only write back if its generation is still current. Without
 *     this, dropping a second volume onto a window while the first is still fetching
 *     lets the slower fetch win, and the window ends up showing a volume the user
 *     already replaced. The counter is per window, not global: two windows loading
 *     concurrently must not invalidate each other.
 *   - **Free scroll.** A window can opt out of synchronisation in both directions --
 *     it neither broadcasts nor receives. One-directional opt-out reads as a bug the
 *     first time a user scrolls a "free" window and watches the others follow.
 *   - **`render` never synchronises.** A 3D view has no slice index, so including it
 *     in a slice group means computing a slice for something that has none.
 *
 * What is *not* carried over: the fractional (0..1) crosshair coordinates NiiVue used.
 * Cornerstone's crosshairs are in patient world millimetres, which is the frame
 * everything else in Phase 3 speaks -- see `syncTargets` for the one place the old
 * convention still has to be honoured.
 */

import { GRID_WINDOWS, ORIENTATIONS, assertWindowIndex, isSliceOrientation } from './layout.js';

/**
 * Build the initial state for a grid.
 *
 * @param {object[]} [layout] entries of `{window, orientation, lazy}`.
 * @returns {object} grid state; treat as opaque and use the functions below.
 */
export function createGridState(layout = []) {
    const windows = Array.from({ length: GRID_WINDOWS }, (unused, index) => ({
        index,
        orientation: ORIENTATIONS.AXIAL,
        modality: null,
        fileId: null,
        volumeId: null,
        loading: false,
        error: null,
        loadGeneration: 0,
        freeScroll: false,
        lazy: false,
        // F2: set when the volume's header declares no orientation. The window must
        // show this; a viewer that renders an inferred orientation silently is the
        // hazard the finding is about.
        orientationWarning: null,
    }));

    for (const entry of layout) {
        assertWindowIndex(entry.window);
        windows[entry.window].orientation = entry.orientation;
        windows[entry.window].lazy = Boolean(entry.lazy);
    }

    return { windows };
}

/** Read one window's state. */
export function windowAt(state, windowIndex) {
    assertWindowIndex(windowIndex);
    return state.windows[windowIndex];
}

/**
 * Begin a load into a window, invalidating any load still in flight for it.
 *
 * @param {object} state
 * @param {number} windowIndex
 * @param {object} descriptor `{modality, fileId, volumeId}`.
 * @returns {number} the generation this load must quote to write back.
 */
export function beginLoad(state, windowIndex, { modality = null, fileId = null, volumeId = null } = {}) {
    const window = windowAt(state, windowIndex);
    window.loadGeneration += 1;
    window.loading = true;
    window.error = null;
    window.orientationWarning = null;
    window.modality = modality;
    window.fileId = fileId;
    window.volumeId = volumeId;
    return window.loadGeneration;
}

/**
 * Whether a load that started at `generation` may still write to this window.
 *
 * @param {object} state
 * @param {number} windowIndex
 * @param {number} generation
 * @returns {boolean}
 */
export function isLoadCurrent(state, windowIndex, generation) {
    return windowAt(state, windowIndex).loadGeneration === generation;
}

/**
 * Record a successful load. A no-op if the load has been superseded.
 *
 * @returns {boolean} whether the state was written.
 */
export function completeLoad(state, windowIndex, generation, { orientationWarning = null } = {}) {
    if (!isLoadCurrent(state, windowIndex, generation)) {
        return false;
    }
    const window = windowAt(state, windowIndex);
    window.loading = false;
    window.error = null;
    window.orientationWarning = orientationWarning;
    return true;
}

/**
 * Record a failed load. A no-op if the load has been superseded.
 *
 * @returns {boolean} whether the state was written.
 */
export function failLoad(state, windowIndex, generation, message) {
    if (!isLoadCurrent(state, windowIndex, generation)) {
        return false;
    }
    const window = windowAt(state, windowIndex);
    window.loading = false;
    window.error = message;
    return true;
}

/**
 * Empty a window.
 *
 * Bumps the generation, so a load still in flight for this window can no longer write
 * to it -- clearing a window whose fetch has not returned must not be undone by the
 * fetch returning.
 */
export function clearWindow(state, windowIndex) {
    const window = windowAt(state, windowIndex);
    window.loadGeneration += 1;
    window.modality = null;
    window.fileId = null;
    window.volumeId = null;
    window.loading = false;
    window.error = null;
    window.orientationWarning = null;
}

/** Point a window at a different plane. */
export function setOrientation(state, windowIndex, orientation) {
    if (!Object.values(ORIENTATIONS).includes(orientation)) {
        throw new Error(`Unknown orientation '${orientation}'.`);
    }
    windowAt(state, windowIndex).orientation = orientation;
}

/** Opt a window in or out of synchronisation, in both directions. */
export function setFreeScroll(state, windowIndex, freeScroll) {
    windowAt(state, windowIndex).freeScroll = Boolean(freeScroll);
}

/**
 * The windows that should receive an update broadcast by `sourceIndex`.
 *
 * The whole synchronisation policy, in one pure function, because it is the piece that
 * was hardest to reason about when it was spread across an event listener, a
 * requestAnimationFrame callback and two module-level flags.
 *
 * A window receives an update when **all** of these hold:
 *
 *   - it is not the source (a viewer must not act on its own broadcast, or a rounding
 *     difference between the two directions turns into an oscillation);
 *   - neither it nor the source has free scroll set -- opt-out is symmetric;
 *   - neither it nor the source is a 3D render, which has no slice to synchronise;
 *   - it actually has a volume loaded, so nothing is computed for an empty window.
 *
 * @param {object} state
 * @param {number} sourceIndex
 * @returns {number[]} window indices, ascending.
 */
export function syncTargets(state, sourceIndex) {
    const source = windowAt(state, sourceIndex);
    if (source.freeScroll || !isSliceOrientation(source.orientation)) {
        return [];
    }
    return state.windows
        .filter(
            (window) =>
                window.index !== sourceIndex &&
                !window.freeScroll &&
                isSliceOrientation(window.orientation) &&
                window.volumeId !== null
        )
        .map((window) => window.index);
}

/**
 * Windows currently showing a given orientation and taking part in synchronisation.
 *
 * The replacement for `synchronizationGroups`, which was a mutable index maintained
 * alongside the window states and could therefore disagree with them. Deriving it on
 * demand costs four comparisons and cannot go stale.
 *
 * @param {object} state
 * @param {string} orientation
 * @returns {number[]}
 */
export function orientationGroup(state, orientation) {
    return state.windows
        .filter(
            (window) =>
                window.orientation === orientation && !window.freeScroll && window.volumeId !== null
        )
        .map((window) => window.index);
}

/** Every window with a volume loaded. */
export function loadedWindows(state) {
    return state.windows.filter((window) => window.volumeId !== null).map((window) => window.index);
}

/**
 * The distinct volume ids the grid currently holds.
 *
 * What the cache should contain, and therefore what may be evicted when it should not.
 * Two windows on the same file share one id by construction (`volumeIdFor` keys on the
 * URL), so this is how "is anything still using this volume?" is answered without a
 * reference count that could leak.
 *
 * @param {object} state
 * @returns {string[]}
 */
export function activeVolumeIds(state) {
    return [...new Set(state.windows.map((window) => window.volumeId).filter(Boolean))];
}
