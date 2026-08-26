/**
 * Mounting the volume grid on a patient page.
 *
 * `{% cornerstone_entry 'volume-grid' %}` loads the bundle for its side effects, so
 * something has to notice the page and start. This is that something, and it is
 * deliberately the only module in `imaging/grid/` that reads the DOM or the global
 * scope by itself.
 *
 * Two rules it follows, both learned from what it replaces:
 *
 * **It never throws into the page.** `viewer_grid.js` ran on every patient detail page
 * in three namespaces, most of which have no volume at all. A bootstrap that throws on
 * a page without a grid takes the rest of the patient record down with it -- the
 * classification form, the file management section, the export button. So every exit is
 * a return, and a real failure is reported into the grid itself where a user can see
 * it, not only to the console.
 *
 * **It reports the F2 warning.** A volume whose header declares no orientation renders
 * perfectly and may be left-right mirrored. `loadVolumeIntoWindow` hands the warning
 * back rather than logging it; this is what puts it on screen.
 */

import { FIXED_CBCT_LAYOUT, FREE_LAYOUT, GRID_WINDOWS, volumeIdFor } from './layout.js';
import { windowAt } from './windowState.js';
import { volumeUrl } from '../ids/imageIds.js';
import { announceVolumeReady, installPanoramicBridge, nativeRawVolumeDescriptor } from './panoramicSource.js';
import { readScalarData } from './volumeLoading.js';
import { CONTROL_IDS, bindControls, windowReadout } from './controls.js';

/** The element `viewer_grid_data` is rendered into by both content templates. */
export const DATA_ELEMENT_ID = 'viewerGridData';

/** One `.viewer-window` per grid position. */
export const WINDOW_SELECTOR = '.viewer-window[data-window-index]';

/**
 * Read the Django payload, or null when this page has none.
 *
 * @param {Document} doc
 * @returns {object|null}
 */
export function readGridData(doc) {
    const element = doc.getElementById(DATA_ELEMENT_ID);
    if (!element) {
        return null;
    }
    try {
        return JSON.parse(element.textContent || '{}');
    } catch {
        // A malformed payload is a server bug, but it must not take the page with it.
        return null;
    }
}

/**
 * The four window elements, in window order, or null if the grid is not on this page.
 *
 * @param {Document} doc
 * @returns {HTMLElement[]|null}
 */
export function readWindowElements(doc) {
    const found = Array.from(doc.querySelectorAll(WINDOW_SELECTOR));
    if (found.length === 0) {
        return null;
    }
    const elements = new Array(GRID_WINDOWS).fill(null);
    for (const element of found) {
        const index = Number(element.dataset.windowIndex);
        if (Number.isInteger(index) && index >= 0 && index < GRID_WINDOWS) {
            elements[index] = element;
        }
    }
    return elements.every(Boolean) ? elements : null;
}

/**
 * Which volume the grid should open on, from `viewer_grid_data`.
 *
 * Returns null rather than guessing: a page whose payload names no CBCT is a patient
 * without one, which is ordinary and not an error.
 *
 * @param {object} data
 * @returns {{fileId: number, bundleKey: string, filename: string, modality: string}|null}
 */
export function primaryVolumeFrom(data) {
    const files = data?.modalityFiles || {};
    // CBCT first for maxillo; otherwise whichever modality the page defaulted to, then
    // anything at all -- brain pages carry several MRI series and no single "primary".
    const slug = files.cbct
        ? 'cbct'
        : data?.defaultModality && files[data.defaultModality]
          ? data.defaultModality
          : Object.keys(files)[0];

    const entry = slug ? files[slug] : null;
    if (!entry?.id) {
        return null;
    }
    return {
        fileId: Number(entry.id),
        bundleKey: entry.file_key || 'primary',
        filename: entry.filename || `${slug}.nii.gz`,
        modality: slug,
    };
}

/**
 * Show a message inside a grid window.
 *
 * The F2 warning and load failures both land here. `viewer_grid.js` had a bespoke
 * `showOrientationWarning`; this is the same idea with one entry point, because a
 * failure the user cannot see is a failure that gets reported as "the viewer is blank".
 *
 * @param {HTMLElement} element
 * @param {string} message
 * @param {string} [level] `'warning'` or `'error'`.
 */
export function showWindowMessage(element, message, level = 'warning') {
    if (!element) {
        return;
    }
    const existing = element.querySelector('.viewer-window__message');
    const node = existing || element.ownerDocument.createElement('div');
    node.className = `viewer-window__message viewer-window__message--${level}`;
    node.setAttribute('role', level === 'error' ? 'alert' : 'status');
    node.textContent = message;
    if (!existing) {
        element.appendChild(node);
    }
}

/**
 * Whether an element can actually be measured yet.
 *
 * Cornerstone sizes a viewport from its element when `setViewports` runs. Inside a
 * `display: none` container that is 0x0, and the viewport is built against nothing --
 * it does not throw, it renders blank forever.
 *
 * @param {HTMLElement} element
 * @returns {boolean}
 */
export function isMeasurable(element) {
    return Boolean(element) && element.offsetParent !== null && element.clientWidth > 0;
}

/**
 * Call back once an element has non-zero size.
 *
 * `ResizeObserver` rather than a poll, because the thing being waited for *is* a resize
 * -- the container going from `display: none` to visible when its tab is selected.
 *
 * Where there is no `ResizeObserver`, this does **nothing**, deliberately. The
 * tempting fallback is to run the callback anyway, and that is precisely the bug:
 * it mounts into the hidden container. `CBCTViewer.init()` is the primary trigger and
 * needs no observer, so doing nothing here degrades to "starts when the tab is
 * clicked" rather than "starts wrong".
 *
 * @param {HTMLElement} element
 * @param {() => void} callback
 * @returns {() => void} disconnect.
 */
export function observeMeasurable(element, callback) {
    const view = element?.ownerDocument?.defaultView ?? globalThis;
    if (!element || typeof view.ResizeObserver !== 'function') {
        return () => {};
    }
    const observer = new view.ResizeObserver(() => {
        if (isMeasurable(element)) {
            observer.disconnect();
            callback();
        }
    });
    observer.observe(element);
    return () => observer.disconnect();
}

/**
 * Prepare the grid on this page, starting it when it becomes visible.
 *
 * **Not eager.** `#cbct-viewer` is `display: none` unless CBCT is the page's default
 * modality, so mounting on module evaluation builds four viewports inside a 0x0
 * container -- which does not throw and never renders. `viewer_grid.js` avoided this by
 * starting from `CBCTViewer.init()`, which `patient_detail.js:429` already calls when
 * the tab is shown; that hook is reinstated here, and a size observer covers the case
 * where the container becomes visible some other way.
 *
 * Starting is idempotent: the hook and the observer can both fire, and the second is a
 * no-op rather than a second grid over the first.
 *
 * @param {object} options
 * @param {(opts: object) => Promise<object>} options.mount `mountVolumeGrid` from the entry.
 * @param {Document} [options.doc]
 * @returns {Promise<object|null>} the grid handle if it started now, else null.
 */
export async function bootstrapVolumeGrid({ mount, doc = globalThis.document }) {
    const data = readGridData(doc);
    const elements = readWindowElements(doc);
    if (!data || !elements) {
        // Not a page with a volume grid. The overwhelmingly common case.
        return null;
    }

    let pending = null;
    const start = () => {
        if (!pending) {
            pending = mountAndLoad({ mount, doc, data, elements });
        }
        return pending;
    };

    // The hook `patient_detail.js` already calls when the CBCT tab is shown. Merged
    // rather than assigned: the old adapter defined this global too, and the panoramic
    // bridge writes to a neighbouring one.
    const view = doc.defaultView ?? globalThis;
    view.CBCTViewer = Object.assign(view.CBCTViewer || {}, {
        loading: false,
        init: () => start(),
    });

    // Already on screen (CBCT is the default modality): start now.
    if (isMeasurable(elements[0])) {
        return start();
    }

    // Otherwise wait for it to be shown. Not awaited: module evaluation must not block
    // on a tab the user may never open.
    observeMeasurable(elements[0], start);
    return null;
}

/**
 * Mount the grid and load its volume. Called once, when the grid is visible.
 *
 * @returns {Promise<object|null>}
 */
async function mountAndLoad({ mount, doc, data, elements }) {
    const namespace = data.projectNamespace || 'api';
    // From the document rather than a global: it is what the page was served from, and
    // it makes the whole path testable without a browser.
    const origin = doc.defaultView?.location?.origin;
    // The page's own window, not a global: the panoramic bridge is installed on it and
    // the ready event is dispatched from it, and both belong to this document.
    const view = doc.defaultView ?? globalThis;
    // maxillo pins three orthogonal planes and loads 3D on demand; brain lets the user
    // put anything anywhere, which is what `fixedMode` has always meant.
    const layout = data.fixedMode ? FIXED_CBCT_LAYOUT : FREE_LAYOUT;

    let grid;
    try {
        grid = await mount({ elements, layout });
    } catch (error) {
        // WebGL2 missing (decision #13) lands here, and the message is written for a
        // clinician rather than a console.
        for (const element of elements) {
            showWindowMessage(element, error.message, 'error');
        }
        return null;
    }

    const volume = primaryVolumeFrom(data);
    if (!volume) {
        return grid;
    }

    const url = volumeUrl({
        fileId: volume.fileId,
        bundleKey: volume.bundleKey,
        filename: volume.filename,
        namespace,
        origin,
    });

    // The panoramic reads through this, so it is installed before the load rather than
    // after: `cbct_panorex_editor.js` polls for the descriptor and also listens for the
    // ready event, and a bridge that appeared only afterwards would lose the race for
    // the polling half.
    installPanoramicBridge({
        data,
        getDescriptor: () => descriptorFor({ grid, url, volume }),
        target: view,
    });

    const targets = layout.filter((entry) => !entry.lazy).map((entry) => entry.window);
    let loaded = false;

    for (const windowIndex of targets) {
        try {
            const result = await grid.loadVolumeIntoWindow(windowIndex, {
                url,
                modality: volume.modality,
                fileId: volume.fileId,
            });
            if (result?.orientationWarning) {
                showWindowMessage(elements[windowIndex], result.orientationWarning, 'warning');
            }
            loaded = loaded || !result?.superseded;
        } catch (error) {
            showWindowMessage(elements[windowIndex], `Could not load this volume: ${error.message}`, 'error');
        }
    }

    // Bound after the load so the first readout has something to report, and bound at
    // all only on pages that have a toolbar -- `bindControls` returns an empty plan on
    // the brain page, which carries the grid without the CBCT controls.
    const controls = bindControls({ grid, doc });
    const refreshReadout = windowReadout({
        grid,
        element: doc.getElementById(CONTROL_IDS.windowReadout),
        windowIndex: targets[0],
    });
    refreshReadout();
    for (const element of elements) {
        // Window/level is dragged on the image now that the percent sliders are gone,
        // so the readout follows the pointer rather than a control's change event.
        element.addEventListener('pointerup', refreshReadout);
    }

    if (loaded) {
        announceVolumeReady(
            { windowIndex: targets[0], modality: volume.modality, fileId: volume.fileId },
            view
        );
    }
    return { ...grid, controls, refreshReadout };
}

/**
 * Build the panoramic's descriptor from whatever the grid currently holds.
 *
 * Evaluated lazily, on each call, because the panorex editor asks before the volume has
 * necessarily arrived and expects null until it has.
 */
function descriptorFor({ grid, url, volume }) {
    const volumeId = volumeIdFor(url);
    const holder = grid.state.windows.find((entry) => entry.volumeId === volumeId && !entry.loading);
    if (!holder) {
        return null;
    }

    const cached = grid.volumeCache?.get?.(volumeId);
    if (!cached?.volume || !cached?.header) {
        return null;
    }

    let scalarData;
    try {
        scalarData = readScalarData(cached.volume);
    } catch {
        // Still loading, or loaded short. Null is what the old implementation returned
        // and what the panorex editor already handles by waiting.
        return null;
    }

    return nativeRawVolumeDescriptor({
        scalarData,
        header: cached.header,
        fileName: volume.filename,
        source: {
            fileId: String(volume.fileId),
            fileKey: volume.bundleKey,
            revision: null,
        },
    });
}

export { windowAt };
