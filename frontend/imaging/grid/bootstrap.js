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

import { FIXED_CBCT_LAYOUT, FREE_LAYOUT, GRID_WINDOWS, ORIENTATIONS, viewportId } from './layout.js';
import { windowAt } from './windowState.js';
import { isMeasurable, observeSize } from '../runtime/elementSize.js';
import { volumeUrl } from '../ids/imageIds.js';
import { dicomSeriesUrl } from './dicomVolume.js';
import {
    CLEARED_MESSAGE,
    SAVED_MESSAGE,
    bindControls,
    loadingIndicator,
    markActiveTool,
} from './controls.js';
import { buildSaveRequest, interpretSaveResponse, measurementAnnotations } from './measurements.js';
import { bindDragDrop } from './dragDrop.js';
import {
    SEGMENTATION_ID,
    gridMismatch,
    loadSegmentation,
    ownedVolumeIds,
    paletteFor,
    segmentationUrl,
    setOverlayVisible,
    showSegmentation,
} from './segmentation.js';

/** The element `viewer_grid_data` is rendered into by both content templates. */
export const DATA_ELEMENT_ID = 'viewerGridData';

/**
 * The SEG control's id, as the shared grid toolbar renders it.
 *
 * Named here for the reason `laparoscopy/tests_video_surface.py` gives: a template id
 * joining two files is an untested interface, and a rename leaves the JS holding
 * `null` and a button that does nothing. `frontend/tests/gridSegmentation.test.js`
 * asserts it against the template.
 */
export const SEGMENTATION_IDS = Object.freeze({
    toggle: 'viewerSegToggle',
});

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

    return volumeFor(data, slug);
}

/**
 * What the payload says about one modality, or null if it says nothing.
 *
 * Split out of {@link primaryVolumeFrom} because a dropped chip asks the same question
 * about a slug the user picked rather than one the page chose. Two readers of the same
 * payload shape is how the two would drift.
 *
 * @param {object} data the `viewer_grid_data` payload.
 * @param {string} slug a modality slug.
 * @returns {{fileId: number, bundleKey: string, filename: string, modality: string, dicom: object|null}|null}
 */
export function volumeFor(data, slug) {
    const entry = slug ? (data?.modalityFiles || {})[slug] : null;
    if (!entry?.id) {
        return null;
    }
    return {
        fileId: Number(entry.id),
        bundleKey: entry.file_key || 'primary',
        filename: entry.filename || `${slug}.nii.gz`,
        modality: slug,
        // Present only for a stored DICOM series (Phase 8). Its presence is what
        // selects the DICOM volume path; `maxillo.views.patient_detail` omits it for
        // every NIfTI row, so neither side has to guess from a filename.
        dicom: entry.dicom ?? null,
    };
}

/**
 * The descriptor `loadVolumeIntoWindows` takes, for one modality.
 *
 * One URL either way, because it is also the volume cache key (`volumeIdFor`): one per
 * volume, stable across reloads, unique. For a series that is its metadata endpoint;
 * for a file, its serve path.
 *
 * @param {object} volume from {@link volumeFor}.
 * @param {object} options `namespace` and `origin`.
 * @returns {{url: string, modality: string, fileId: number, dicom: object|null}}
 */
export function descriptorFor(volume, { namespace, origin } = {}) {
    const url = volume.dicom
        ? dicomSeriesUrl({
              studyUid: volume.dicom.studyUid,
              seriesUid: volume.dicom.seriesUid,
              origin,
          })
        : volumeUrl({
              fileId: volume.fileId,
              bundleKey: volume.bundleKey,
              filename: volume.filename,
              namespace,
              origin,
          });
    return { url, modality: volume.modality, fileId: volume.fileId, dicom: volume.dicom };
}

/**
 * Whether this page lets the user put a modality in a window by dragging a chip.
 *
 * The flag has been in the payload since before 3.0 -- `maxillo/views/patient_detail.py`
 * sets `enableDragDrop: False` because a CBCT grid is three fixed planes and a render,
 * and brain leaves it unset, meaning yes. It has been read by nobody since `c03afa6`
 * deleted `viewer_grid.js`. Reading it again is what tells the two surfaces apart, and
 * it also decides what loads on arrival: a grid whose windows are the user's to fill
 * should not pre-fill them.
 *
 * @param {object} data
 * @returns {boolean}
 */
export function dragDropEnabled(data) {
    return data?.enableDragDrop !== false;
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
 * Re-exported so this module's readers -- and `frontend/tests/bootstrap.test.js` -- keep
 * finding them where the grid has always named them. The definitions moved to
 * `runtime/elementSize.js` when the video surface turned out to need the same signal and
 * the photos surface turned out to have quietly grown its own copy.
 */
export { isMeasurable, observeSize };

/**
 * Announced on the window once the grid has a volume on screen.
 *
 * The panoramic editor is a **separate bundle on the same page**, and the one thing it
 * cannot do without is a loaded CBCT: `mount()` reads the volume out of the cache and
 * throws "The CBCT is still loading." if it is not there yet. Offering the reader an Edit
 * button before that is offering them an error. `window.CBCTViewer.ready` carries the same
 * fact for anything that arrives after the event -- the two bundles' start order is not
 * fixed, which is the race `bootstrapPanoramic` already documents for `window.canEdit`.
 */
export const GRID_READY_EVENT = 'ygg:volume-grid-ready';


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
        ready: true,
        init: () => {
            resize(isMeasurable(elements[0]));
            return grid;
        },
    });
    try {
        view.dispatchEvent(new view.CustomEvent(GRID_READY_EVENT));
    } catch (error) {
        report(`could not announce readiness: ${error.message}`);
    }

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

    const dragDrop = dragDropEnabled(data);

    /**
     * Load one modality into a set of windows, with the progress indicator and the
     * F2 warning that belong to any load.
     *
     * Used by the opening load and by a dropped chip, so a window filled by hand
     * behaves exactly like one filled on arrival -- the same indicator, the same
     * orientation warning, the same error text.
     */
    async function loadModality(slug, windowIndices) {
        const volume = volumeFor(data, slug);
        if (!volume) {
            report(`no file for modality '${slug}' on this patient.`);
            return null;
        }

        // A CBCT takes real seconds to arrive, and without an indicator the grid is
        // simply black for all of them -- which is indistinguishable from a viewer
        // that failed, and was reported as exactly that.
        const indicators = windowIndices.map((index) => loadingIndicator(elements[index]));
        const onProgress = (event) => {
            const { loaded: done, total } = event?.detail ?? {};
            if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
                for (const indicator of indicators) {
                    indicator.update(done / total);
                }
            }
        };
        view.addEventListener?.('CORNERSTONE_NIFTI_VOLUME_PROGRESS', onProgress);

        // One call for every window: the same volume in three viewports is one load,
        // and one read of its scalar data. See `loadVolumeIntoWindows`.
        report(`loading ${volume.modality} #${volume.fileId}…`);
        try {
            const result = await grid.loadVolumeIntoWindows(
                windowIndices,
                descriptorFor(volume, { namespace, origin })
            );
            if (result?.orientationWarning) {
                for (const windowIndex of result.windows ?? windowIndices) {
                    showWindowMessage(elements[windowIndex], result.orientationWarning, 'warning');
                }
            }
            return result?.superseded ? null : volume;
        } catch (error) {
            report(`load failed: ${error.message}`);
            for (const windowIndex of windowIndices) {
                showWindowMessage(
                    elements[windowIndex],
                    `Could not load this volume: ${error.message}`,
                    'error'
                );
            }
            return null;
        } finally {
            // In `finally` so a failed load does not leave a spinner turning forever
            // over an error message.
            view.removeEventListener?.('CORNERSTONE_NIFTI_VOLUME_PROGRESS', onProgress);
            for (const indicator of indicators) {
                indicator.done();
            }
        }
    }

    // The SEG overlay. Built here rather than in `controls.js` because it needs the
    // grid, the payload and the load path, and `controls.js` is a DOM binder.
    const segmentationControl = createSegmentationControl({
        grid,
        doc,
        data,
        namespace,
        origin,
    });

    // `volume` is what the *measurement* endpoint is narrowed by, so it tracks
    // whatever the grid is actually showing rather than staying at the opening choice.
    // It starts null on a drag-and-drop page because nothing is showing yet.
    let volume = null;
    let loaded = false;

    /**
     * Draw whatever measurements this volume already has.
     *
     * **After a load, never before.** An annotation needs a viewport with a volume in
     * it to be drawn against, and on a drag-and-drop page there is no volume until
     * something is dropped -- so this is called when one arrives rather than at mount.
     * Once per volume: re-restoring on a later drop would add a second copy of every
     * annotation, since `addAnnotation` mints a fresh UID each time.
     */
    const restored = new Set();
    async function restoreMeasurements(shown) {
        if (!shown || restored.has(shown.fileId)) {
            return;
        }
        restored.add(shown.fileId);
        try {
            const stored = await fetch(
                measurementsUrl(data, shown, namespace, origin, '/state/'),
                { credentials: 'same-origin' }
            ).then((response) => (response.ok ? response.json() : null));
            const count = grid.restoreAnnotations?.(stored?.annotations ?? []) ?? 0;
            if (count) {
                report(`restored ${count} saved measurement(s).`);
            }
        } catch (error) {
            // A study whose measurements cannot be fetched is still usable; failing to
            // draw them must not cost the images.
            report(`could not restore measurements: ${error.message}`);
        }
    }

    if (dragDrop) {
        // **Nothing is loaded on arrival.** A grid whose windows are the user's to
        // fill should not pre-fill them: before 3.0 a brain page could show FLAIR,
        // T1, T1c and T2 side by side, and the replacement loaded one arbitrary
        // series -- `Object.keys(modalityFiles)[0]`, since brain sends no
        // `defaultModality` -- into all four windows and offered no way to change
        // any of them. Four `.drop-hint` placeholders and four series' worth of
        // bandwidth unspent is the honest opening state.
        bindDragDrop({
            doc,
            elements,
            onDrop: async (windowIndex, slug) => {
                const dropped = await loadModality(slug, [windowIndex]);
                if (!dropped) {
                    return;
                }
                volume = dropped;
                // A window that changed series may have left a volume nothing is
                // showing any more; without this the cache grows by a whole MRI per
                // drop and the third or fourth one runs the GPU out of memory. The
                // overlay's own volumes are spared: they are in use without being in
                // a window.
                grid.releaseUnusedVolumes?.(ownedVolumeIds());
                grid.refreshOverlay?.(windowIndex);
                await restoreMeasurements(dropped);
                await segmentationControl?.reapply();
            },
        });
        report('drag-and-drop bound; windows start empty.', {
            modalities: Object.keys(data.modalityFiles || {}),
        });
    } else {
        const opening = primaryVolumeFrom(data);
        if (!opening) {
            report('this patient has no volume to show.', {
                modalities: Object.keys(data.modalityFiles || {}),
            });
            return grid;
        }
        const targets = layout.filter((entry) => !entry.lazy).map((entry) => entry.window);
        volume = await loadModality(opening.modality, targets);
        loaded = Boolean(volume);
        await restoreMeasurements(volume);
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
    // The template marks the navigation tool pressed; make the grid agree rather than
    // trusting two places to say the same thing. Which tool that is depends on the
    // layout -- a grid of parallel planes has no crosshair -- so it comes from the grid
    // and is not spelled here.
    markActiveTool(controls.plan.tools, grid.navigationTool);

    // The window/level readout now lives in each viewport's own overlay rather than in
    // the toolbar: it belongs to the image it describes, and the toolbar `<output>` it
    // replaced looked like what it was -- a slider's label with the slider removed.
    grid.refreshOverlays?.();

    if (loaded) {
        report(`loaded ${volume.modality} #${volume.fileId}.`);
    }
    return { ...grid, controls, segmentation: segmentationControl };
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
    // `volume` as well as the header: on a drag-and-drop grid nothing is showing until
    // something is dropped, so a save before then has no resource to anchor to and
    // would otherwise be a TypeError inside a click handler.
    if (!header || !volume) {
        return { level: 'warning', message: 'Nothing to save yet: no volume is loaded.' };
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

/**
 * The domain-oriented measurement endpoint for this patient.
 *
 * The state read names the volume it is reading for. A measurement set is per *patient*
 * and can now hold work on several resources at once -- a CBCT and a teleradiography,
 * or a stack of photographs -- so the unnarrowed response is whatever the last save
 * happened to write. Without `fileId` the grid would draw another modality's
 * measurements on this volume, or find none at all once a photo save had been the most
 * recent one.
 */
export function measurementsUrl(data, volume, namespace, origin, suffix) {
    const prefix = namespace === 'api' ? '/api' : `/${namespace}/api`;
    const url = new URL(`${prefix}/patients/${data.scanId}/measurements${suffix}`, origin);
    if (suffix === '/state/' && volume?.fileId) {
        url.searchParams.set('fileId', String(volume.fileId));
    }
    return url.href;
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


/**
 * The SEG on/off switch, or null on a page with no segmentation.
 *
 * **Deferred.** The overlay is a whole second volume, and most visits to a study never
 * ask for it, so nothing is fetched until the button is first pressed -- which is also
 * how it behaved before 3.0.
 *
 * Two failure modes are surfaced rather than logged, because both look identical to a
 * broken viewer from the outside: a segmentation whose grid is not the volume's, and
 * one with no labels in it. {@link loadSegmentation} returns a reason for each.
 */
export function createSegmentationControl({ grid, doc, data, namespace, origin }) {
    const toggle = doc.getElementById(SEGMENTATION_IDS.toggle);
    const url = segmentationUrl({
        segmentationFile: data?.segmentationFile,
        namespace,
        origin,
        volumeUrl,
    });
    if (!toggle || !url) {
        // Not an error: most studies have no segmentation, and the template only
        // renders the button when the payload names one.
        return null;
    }

    const cornerstone = grid.cornerstone;
    let state = { loaded: false, labelValues: [] };

    /**
     * Which viewports are showing a volume, and which of them is the 3D one.
     *
     * Once the overlay is loaded, a window is only a target if its volume is on the
     * *same grid* as the labelmap. On the brain page the four sequences are
     * co-registered so they all are; a window holding something that is not -- which a
     * dropped chip can produce -- is skipped rather than given an overlay that
     * describes different voxels. Same rule as {@link gridMismatch} applies at load.
     */
    function targets() {
        const labelmap = state.loaded
            ? grid.cornerstone.cache.getVolume(SEGMENTATION_ID)
            : null;
        return grid.state.windows
            .filter((window) => {
                if (!window.volumeId) {
                    return false;
                }
                if (!labelmap) {
                    return true;
                }
                const shown = grid.cornerstone.cache.getVolume(window.volumeId);
                return shown ? gridMismatch(shown, labelmap) === null : false;
            })
            .map((window) => ({
                index: window.index,
                orientation: window.orientation,
                viewportId: viewportId(window.index),
                volumeId: window.volumeId,
            }));
    }

    function setBusy(busy) {
        toggle.disabled = Boolean(busy);
        toggle.title = busy
            ? 'Loading the segmentation overlay'
            : (isOn() ? 'Hide the segmentation overlay' : 'Show the segmentation overlay');
    }

    /**
     * The DOM holds the state, and it is `aria-checked`.
     *
     * The same `role="switch"` the annotation-mode control uses, for the reason that
     * one's comment gives: a pressed-looking button is the ambiguity that got reported,
     * and reading the state back off the DOM at click time is what stops a closure
     * variable that started out disagreeing with the markup from inverting every click
     * after it.
     */
    function isOn() {
        return toggle.getAttribute('aria-checked') === 'true';
    }

    /** Render the switch: the flag and the word beside it. */
    function setOn(on) {
        toggle.setAttribute('aria-checked', String(Boolean(on)));
        const word = toggle.querySelector('[data-mode-state]');
        if (word) {
            word.textContent = on ? 'on' : 'off';
        }
    }

    /** Load once, then show in every window that currently holds a volume. */
    async function apply() {
        const shown = targets();
        if (shown.length === 0) {
            showMessageOnToggle('Load a volume before showing its segmentation.');
            return false;
        }

        if (!state.loaded) {
            const result = await loadSegmentation({
                cornerstone,
                referenceVolumeId: shown[0].volumeId,
                url,
            });
            if (!result.ok) {
                showMessageOnToggle(result.reason);
                return false;
            }
            state = { loaded: true, labelValues: result.labelValues };
        }

        const colorLUT = paletteFor(state.labelValues);
        const { shown: reached, colorLUTIndex } = await showSegmentation({
            cornerstone,
            viewports: shown,
            colorLUT,
            // The index the first switch-on minted, so a second one overwrites that LUT
            // rather than appending an identical copy nothing ever collects.
            colorLUTIndex: state.colorLUTIndex ?? null,
            // The volume render wants solid voxels; the three slice windows keep the
            // outline that makes a tooth's border readable against the bone.
            solidViewportIds: shown
                .filter((target) => target.orientation === ORIENTATIONS.RENDER)
                .map((target) => target.viewportId),
        });
        // Said out loud, because the failure this had twice was a window that showed
        // nothing and reported nothing. If a viewport is missing from `reached`, or
        // reached and still blank, that line is where to start.
        report('segmentation shown', {
            labels: state.labelValues,
            viewports: reached,
            declined: shown
                .map((target) => target.viewportId)
                .filter((id) => !reached.includes(id)),
        });
        // Re-asserted on every switch-on, because switching *off* marks every segment
        // hidden -- that is how Cornerstone hides a labelmap -- and those flags survive
        // until something sets them back.
        for (const target of shown) {
            setOverlayVisible({ cornerstone, viewportId: target.viewportId, visible: true });
        }
        state.colorLUTIndex = colorLUTIndex;
        // The 3D window gained a second volume actor just now, and it arrived with vtk's
        // default projection rather than this grid's: Cornerstone asks for a blend mode on
        // the volume input and `VolumeViewport3D.setBlendMode()` drops it. Re-asserting the
        // render mode puts both actors on the same projection -- see `setRenderMode`. Also
        // the reason `reapply()` works after a drop, which rebuilds those actors again.
        reassertRenderModes();
        grid.renderingEngine.renderViewports(shown.map((t) => t.viewportId));
        return true;
    }

    /** Put every 3D window's actors back on this grid's projections. */
    function reassertRenderModes() {
        for (const target of targets()) {
            if (target.orientation !== ORIENTATIONS.RENDER) {
                continue;
            }
            try {
                grid.setRenderMode?.(target.index);
            } catch (error) {
                report(`could not re-apply the 3D render mode: ${error.message}`);
            }
        }
    }

    /**
     * Re-assert the projections **after** Cornerstone has mounted the labelmap actor.
     *
     * **The second switch-on is not the first, and that is the whole defect.**
     * `addSegmentationRepresentations` reads `config.blendMode` only when it *creates* a
     * representation (`createLegacyVolumeLabelmapPlan`); on a re-show it recognises the
     * one it already holds and the config is never read again. Switching the overlay off
     * and on rebuilds the labelmap's actor without it, so the actor falls back to
     * Cornerstone's default for a labelmap volume input -- `MAXIMUM_INTENSITY_BLEND` --
     * while the study beneath it stays on this grid's mode. Two volume actors projected
     * differently in one renderer is the haze that was reported, the second time round, as
     * the segmentation "becoming dark".
     *
     * Calling {@link reassertRenderModes} straight after `showSegmentation` cannot fix it:
     * that call is **synchronous and returns undefined**, filing the representation and
     * leaving the actor to be mounted by the segmentation render loop, so the `await`
     * resolves while the viewport still holds only the study. `SEGMENTATION_RENDERED` is
     * the signal that the actor exists. Re-applying a spec an actor already carries costs
     * nothing -- vtk's setters compare before they mark modified -- so this is left
     * registered rather than armed and disarmed around each toggle.
     */
    cornerstone.eventTarget?.addEventListener?.(
        cornerstone.toolsEnums.Events.SEGMENTATION_RENDERED,
        reassertRenderModes
    );

    function showMessageOnToggle(message) {
        report(`segmentation: ${message}`);
        const notify = globalThis.appNotify;
        if (typeof notify === 'function') {
            notify('warning', message);
        }
    }

    toggle.addEventListener('click', async () => {
        const turningOn = !isOn();
        setBusy(true);
        try {
            if (turningOn) {
                setOn(await apply());
                return;
            }
            // Off means off: the whole representation in every viewport, including the
            // 3D one, whose surface is a separate representation of the same
            // segmentation and is not touched by hiding the labelmap.
            for (const target of targets()) {
                setOverlayVisible({ cornerstone, viewportId: target.viewportId, visible: false });
            }
            grid.renderingEngine.renderViewports(targets().map((t) => t.viewportId));
            setOn(false);
        } catch (error) {
            showMessageOnToggle(`could not be shown: ${error.message}`);
            setOn(false);
        } finally {
            setBusy(false);
        }
    });

    return {
        /**
         * Put the overlay back after a window changed what it shows.
         *
         * A dropped chip rebuilds that window's actors, so a representation added
         * before the drop is no longer on it. No-op while the overlay is off.
         */
        reapply: async () => {
            if (!isOn()) {
                return;
            }
            await apply();
        },
        isOn,
    };
}
