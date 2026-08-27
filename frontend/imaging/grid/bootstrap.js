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
import {
    CLEARED_MESSAGE,
    SAVED_MESSAGE,
    bindControls,
    loadingIndicator,
    markActiveTool,
} from './controls.js';
import { buildSaveRequest, interpretSaveResponse, measurementAnnotations } from './measurements.js';

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

/** Prefix for every diagnostic this module emits, so a console can be filtered. */
export const LOG_PREFIX = '[ygg-grid]';

/**
 * Say what the bootstrap decided, and why.
 *
 * Not decoration. The first version of this module returned `null` from three
 * different places with no output at all, and the result was a blank viewer that
 * reported nothing anywhere -- no error, no warning, no clue. A bootstrap that can
 * decline to run has to say so, or the only way to tell "there is no volume on this
 * page" from "the volume failed to load" is to read the source.
 *
 * @param {string} message
 * @param {object} [detail]
 */
export function report(message, detail) {
    const line = `${LOG_PREFIX} ${message}`;
    if (detail === undefined) {
        console.info(line);
    } else {
        console.info(line, detail);
    }
}

/**
 * Whether an element can currently be measured.
 *
 * Used to decide when to *resize*, not whether to mount -- see
 * {@link bootstrapVolumeGrid}.
 *
 * @param {HTMLElement} element
 * @returns {boolean}
 */
export function isMeasurable(element) {
    return Boolean(element) && element.offsetParent !== null && element.clientWidth > 0;
}

/**
 * Call back whenever an element's size changes, and once it first has one.
 *
 * @param {HTMLElement} element
 * @param {(measurable: boolean) => void} callback
 * @returns {() => void} disconnect.
 */
export function observeSize(element, callback) {
    const view = element?.ownerDocument?.defaultView ?? globalThis;
    if (!element || typeof view.ResizeObserver !== 'function') {
        return () => {};
    }
    const observer = new view.ResizeObserver(() => callback(isMeasurable(element)));
    observer.observe(element);
    return () => observer.disconnect();
}

/**
 * Start the grid on this page.
 *
 * **Mounts unconditionally**, and resizes when the container becomes visible. The
 * previous version gated mounting on visibility, which was wrong twice over:
 *
 *   1. `#cbct-viewer` is `display: none` unless CBCT is the page's default modality,
 *      so on every other page the grid waited for a trigger.
 *   2. The trigger it waited for cannot work. `patient_detail.js` is a **classic**
 *      script and `{% cornerstone_entry %}` emits a **deferred module**, so
 *      `ensureCbctViewerReady` runs *before* this module defines `window.CBCTViewer`,
 *      finds it undefined, and returns. Nothing calls it again.
 *
 * Cornerstone handles a viewport whose element starts at zero size: `resize()` is the
 * documented way to pick up a container that was hidden when its viewport was built.
 * So the grid mounts now and is resized -- and its cameras reset -- the first time it
 * is actually on screen. `CBCTViewer.init()` is still installed, but only as an extra
 * nudge; nothing depends on it arriving.
 *
 * @param {object} options
 * @param {(opts: object) => Promise<object>} options.mount `mountVolumeGrid` from the entry.
 * @param {Document} [options.doc]
 * @returns {Promise<object|null>}
 */
export async function bootstrapVolumeGrid({ mount, doc = globalThis.document }) {
    const data = readGridData(doc);
    if (!data) {
        report('no #viewerGridData on this page; nothing to mount.');
        return null;
    }
    const elements = readWindowElements(doc);
    if (!elements) {
        report('no complete .viewer-grid on this page; nothing to mount.', {
            found: doc.querySelectorAll(WINDOW_SELECTOR).length,
            needed: GRID_WINDOWS,
        });
        return null;
    }

    report('mounting', { namespace: data.projectNamespace, fixedMode: Boolean(data.fixedMode) });
    const grid = await mountAndLoad({ mount, doc, data, elements });
    if (!grid?.renderingEngine) {
        return grid ?? null;
    }

    // Size the viewports to the container whenever it changes -- which includes the
    // moment a hidden tab is shown. The first time it has a real size, reset the
    // cameras too: a camera fitted to a 0x0 viewport is not a camera.
    let sized = false;
    const resize = (measurable) => {
        if (!measurable) {
            return;
        }
        try {
            grid.renderingEngine.resize(true, true);
            if (!sized) {
                sized = true;
                grid.resetCameras?.();
                grid.refreshOverlays?.();
                report('sized to the visible container.');
            }
        } catch (error) {
            report(`resize failed: ${error.message}`);
        }
    };

    for (const element of elements) {
        observeSize(element, resize);
    }
    resize(isMeasurable(elements[0]));

    // Kept for `patient_detail.js`, which calls it on tab switch. It can no longer be
    // the thing that starts the grid -- see above -- so it only nudges the size.
    const view = doc.defaultView ?? globalThis;
    view.CBCTViewer = Object.assign(view.CBCTViewer || {}, {
        loading: false,
        init: () => {
            resize(isMeasurable(elements[0]));
            return grid;
        },
    });

    return grid;
}

/**
 * Mount the grid and load its volume.
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
        report(`could not create the rendering engine: ${error.message}`);
        for (const element of elements) {
            showWindowMessage(element, error.message, 'error');
        }
        return null;
    }

    const volume = primaryVolumeFrom(data);
    if (!volume) {
        report('this patient has no volume to show.', { modalities: Object.keys(data.modalityFiles || {}) });
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

    // A CBCT takes real seconds to arrive, and without an indicator the grid is simply
    // black for all of them -- which is indistinguishable from a viewer that failed,
    // and was reported as exactly that.
    const indicators = elements.map((element) => loadingIndicator(element));
    const onProgress = (event) => {
        const { loaded, total } = event?.detail ?? {};
        if (Number.isFinite(loaded) && Number.isFinite(total) && total > 0) {
            for (const indicator of indicators) {
                indicator.update(loaded / total);
            }
        }
    };
    view.addEventListener?.('CORNERSTONE_NIFTI_VOLUME_PROGRESS', onProgress);

    // One call for every window: the same volume in three viewports is one load, and
    // one read of its scalar data. See `loadVolumeIntoWindows`.
    report(`loading ${volume.modality} #${volume.fileId}…`);
    try {
        const result = await grid.loadVolumeIntoWindows(targets, {
            url,
            modality: volume.modality,
            fileId: volume.fileId,
        });
        loaded = !result?.superseded;
        if (result?.orientationWarning) {
            for (const windowIndex of result.windows ?? targets) {
                showWindowMessage(elements[windowIndex], result.orientationWarning, 'warning');
            }
        }
    } catch (error) {
        report(`load failed: ${error.message}`);
        for (const windowIndex of targets) {
            showWindowMessage(elements[windowIndex], `Could not load this volume: ${error.message}`, 'error');
        }
    } finally {
        // In `finally` so a failed load does not leave a spinner turning forever over
        // an error message.
        view.removeEventListener?.('CORNERSTONE_NIFTI_VOLUME_PROGRESS', onProgress);
        for (const indicator of indicators) {
            indicator.done();
        }
    }

    // Draw whatever the study already has. After the load, because an annotation needs
    // a viewport with a volume in it to be drawn against.
    try {
        const stored = await fetch(measurementsUrl(data, volume, namespace, origin, '/state/'), {
            credentials: 'same-origin',
        }).then((response) => (response.ok ? response.json() : null));
        const restored = grid.restoreAnnotations?.(stored?.annotations ?? []) ?? 0;
        if (restored) {
            report(`restored ${restored} saved measurement(s).`);
        }
    } catch (error) {
        // A study whose measurements cannot be fetched is still usable; failing to
        // draw them must not cost the images.
        report(`could not restore measurements: ${error.message}`);
    }

    // Bound after the load, and bound at all only on pages that have a toolbar --
    // `bindControls` returns an empty plan on the brain page, which carries the grid
    // without the CBCT controls.
    const controls = bindControls({
        grid,
        doc,
        onSave: () => saveMeasurements({ grid, data, volume, view, origin, namespace }),
        onClear: () => clearMeasurements({ grid }),
        // One switch, one grid call. It decides which measurement tools have a mode at
        // all -- without which a restored annotation is not drawn no matter what its
        // visibility says, see `setAnnotationMode` -- as well as visibility and the
        // primary tool. `bindControls` calls this once at bind time with the starting
        // state, so a study opens read-only rather than opening editable until
        // something switches it off.
        onAnnotationMode: (enabled) => grid.setAnnotationMode?.(enabled),
    });
    // The template marks Crosshairs pressed; make the grid agree rather than trusting
    // two places to say the same thing.
    markActiveTool(controls.plan.tools, 'Crosshairs');

    // The window/level readout now lives in each viewport's own overlay rather than in
    // the toolbar: it belongs to the image it describes, and the toolbar `<output>` it
    // replaced looked like what it was -- a slider's label with the slider removed.
    grid.refreshOverlays?.();

    if (loaded) {
        report(`loaded ${volume.modality} #${volume.fileId} into ${targets.length} window(s).`);
        announceVolumeReady(
            { windowIndex: targets[0], modality: volume.modality, fileId: volume.fileId },
            view
        );
    }
    return { ...grid, controls };
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
    // The scalar data was read once at load time and kept. Re-reading here would
    // materialise another full copy of the volume every time the panorex editor polls.
    if (!cached?.header || !cached?.scalarData) {
        // Still loading. Null is what the old implementation returned, and what the
        // panorex editor already handles by waiting.
        return null;
    }

    return nativeRawVolumeDescriptor({
        scalarData: cached.scalarData,
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

/**
 * Persist everything currently drawn on the study.
 *
 * Replace-the-set, matching the server: a revision *is* the state of the work at a
 * moment, and the viewer knows what is on screen far better than it knows which
 * annotation the user deleted. See `annotations/services/viewer.py`.
 *
 * Returns a level as well as a message because the caller toasts it, and a failed save
 * that arrives in the same green box as a clean one is worse than no notification.
 *
 * @returns {Promise<{level: string, message: string}>}
 */
async function saveMeasurements({ grid, data, volume, view, origin, namespace }) {
    // Filtered, not everything Cornerstone holds: `getAllAnnotations()` includes the
    // state tools keep for themselves, and the crosshair's has no handles at all.
    const annotations = measurementAnnotations(grid.readAnnotations?.() ?? []);
    const header = grid.currentHeader?.();
    if (!header) {
        return { level: 'warning', message: 'Nothing to save yet: the volume is still loading.' };
    }

    const state = await fetch(measurementsUrl(data, volume, namespace, origin, '/state/'), {
        credentials: 'same-origin',
    })
        .then((response) => (response.ok ? response.json() : { revision: 0 }))
        .catch(() => ({ revision: 0 }));

    const body = buildSaveRequest({
        fileId: volume.fileId,
        bundleKey: volume.bundleKey,
        annotations,
        header,
        expectedRevision: Number(state.revision) || 0,
    });

    const response = await fetch(measurementsUrl(data, volume, namespace, origin, '/'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken(view?.document) },
        body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => null);
    const result = interpretSaveResponse(response, payload);

    return result.saved
        ? // No count and no revision number. Revisions are the audit trail and stay in
          // the database; the count was a number nobody acted on, and both of them made
          // the confirmation a sentence to read rather than a colour to glance at.
          { level: 'success', message: SAVED_MESSAGE }
        : { level: 'danger', message: result.message };
}

/**
 * Remove every measurement from the viewer.
 *
 * No request. A clear is made permanent by the next save -- the server replaces the
 * whole set -- and until then a reload brings them back, which is the only undo there
 * is. The message says both.
 *
 * @returns {Promise<{level: string, message: string}>}
 */
async function clearMeasurements({ grid }) {
    const removed = grid.clearAnnotations?.() ?? 0;
    if (!removed) {
        return { level: 'info', message: 'There are no measurements on this study to remove.' };
    }
    return { level: 'success', message: CLEARED_MESSAGE };
}

/** The domain-oriented measurement endpoint for this patient. */
function measurementsUrl(data, volume, namespace, origin, suffix) {
    const prefix = namespace === 'api' ? '/api' : `/${namespace}/api`;
    return new URL(`${prefix}/patients/${data.scanId}/measurements${suffix}`, origin).href;
}

/**
 * Django's CSRF token, the way this project actually issues it.
 *
 * **Not the cookie.** `yggdrasil/settings.py` sets `CSRF_USE_SESSIONS = True`, so the
 * token lives in the session and there is no `csrftoken` cookie to read at all;
 * `CSRF_COOKIE_HTTPONLY = True` would block reading one even if there were. The first
 * version read the cookie, always found nothing, and every save was a bare 403 with
 * Django's HTML error page rather than a message from the endpoint.
 *
 * The hidden input rendered by `{% csrf_token %}` is the source that works, and it is
 * what `static/js/patient_detail.js:183-189` already uses -- so this matches the
 * convention on the page rather than inventing a second one.
 */
export function csrfToken(doc) {
    const input = doc?.querySelector?.('input[name="csrfmiddlewaretoken"]');
    return input?.value ?? '';
}
