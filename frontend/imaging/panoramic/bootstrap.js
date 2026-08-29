/**
 * Mounting the panoramic surface, and the unattended pass that keeps exports populated.
 *
 * Same contract as the other surfaces' bootstraps: read the DOM, never throw into the
 * page, say out loud when it declines to run. The patient page carries the whole record
 * and a broken payload here must not take the classification form down with it.
 *
 * Three behaviours are load-bearing and are the reason this module reads the way it does.
 *
 * **The editor is never opened on load.** The patient view shows the saved strip; editing
 * starts from the Edit button. A page that opened an editor over a finished reconstruction
 * every time somebody looked at a patient would be worse than the `<img>` it replaced.
 *
 * **A patient with no panoramic gets one, silently.** `panoramic_warmup` drives patient
 * pages in a hidden frame precisely so this runs: the MIP from the automatic slice and the
 * automatic arch, baked and saved without the editor ever being shown, so a `.png` exists
 * in the patient's files and in every export without anybody drawing a spline by hand.
 *
 * **Every exit announces.** `static/js/panoramic_warmup.js:158-163` advances on the
 * message. A path that returns without one does not fail the run -- it costs it a
 * five-minute timeout, for that patient, over a whole folder. So the announcement is not
 * an afterthought at the end of the happy path; it is what every `return` here does.
 *
 * Cornerstone and vtk reach this module only through `mount`, which the entry supplies.
 */

import {
    ANNOUNCE_EVENT,
    OUTCOMES,
    announcement,
    canRestore,
    generationUuid,
    hasSavedPanoramic,
    interpretSave,
    saveBody,
    saveState,
} from './savePayload.js';
import {
    canEdit,
    controlPlan,
    isLocked,
    setEditorVisible,
    setError,
    setMode,
    setProgress,
    setSaveEnabled,
    setSlice,
    setStatus,
    showPane,
} from './controls.js';

export const LOG_PREFIX = '[ygg-panoramic]';

/** The element carrying the volume grid's payload, which names this patient's CBCT. */
export const DATA_ELEMENT_ID = 'viewerGridData';

export function report(message, detail) {
    const line = `${LOG_PREFIX} ${message}`;
    if (detail === undefined) {
        console.info(line);
    } else {
        console.info(line, detail);
    }
}

/** Django's CSRF token: the hidden input, because `CSRF_USE_SESSIONS` leaves no cookie. */
export function csrfToken(doc) {
    return doc?.querySelector?.('input[name="csrfmiddlewaretoken"]')?.value ?? '';
}

export function readGridData(doc) {
    const element = doc?.getElementById?.(DATA_ELEMENT_ID);
    if (!element) {
        return null;
    }
    try {
        return JSON.parse(element.textContent || '{}');
    } catch {
        return null;
    }
}

/**
 * Announce an unattended outcome, both ways.
 *
 * A DOM event for anything on the page, and a `postMessage` for the warm-up frame. Both,
 * always: the page-level listener is what a future in-page batch would use, and the frame
 * message is what today's harness reads.
 */
export function announce(view, outcome, patientId, detail) {
    const payload = announcement(outcome, patientId, detail);
    try {
        view.dispatchEvent(new view.CustomEvent(ANNOUNCE_EVENT, { detail: payload }));
    } catch (error) {
        console.debug('Could not dispatch the panoramic announcement', error);
    }
    if (view.parent && view.parent !== view) {
        try {
            view.parent.postMessage(payload, view.location.origin);
        } catch (error) {
            console.debug('Could not notify the parent window', error);
        }
    }
    return payload;
}

/**
 * Start the surface.
 *
 * @param {object} options
 * @param {(context: object) => Promise<object>} options.mount the entry's factory: given
 *   the plan and the page data, it returns `{descriptor, worker, arch, cpr, core}`.
 * @param {Document} [options.doc]
 * @param {Function} [options.fetchImpl]
 * @returns {Promise<object|null>}
 */
export async function bootstrapPanoramic({
    mount, doc = globalThis.document, fetchImpl = globalThis.fetch?.bind(globalThis),
} = {}) {
    const view = doc?.defaultView ?? globalThis;
    const plan = controlPlan(doc);
    const data = readGridData(doc);
    const patientId = view.scanId ?? null;
    const say = (outcome, detail) => announce(view, outcome, patientId, detail);

    if (!plan.root || !canEdit(plan) || !view.canEdit || !view.Worker) {
        report('the panoramic editor is unavailable on this page; not mounting.');
        say(OUTCOMES.SKIPPED, 'the panoramic editor is unavailable on this page');
        return null;
    }
    const source = data?.panorexSource ?? null;
    if (!source?.volumeFileId || !source?.segmentationFileId) {
        report('this patient has no paired CBCT volume and segmentation.');
        say(OUTCOMES.SKIPPED, 'no paired CBCT volume and segmentation');
        return null;
    }

    // Hidden first, before anything slow: the editor is never opened on load, and a
    // surface that revealed itself while deciding would flash on every patient page.
    setEditorVisible(plan, false);

    const surface = createSurface({ plan, data, source, doc, view, fetchImpl, mount, say });

    if (hasSavedPanoramic(source)) {
        report('this patient already has a current panoramic; nothing to generate.');
        say(OUTCOMES.EXISTING);
        // Not started. Opening the editor from the button is what starts it, so a page
        // that only *displays* a panoramic never loads a worker or a second viewport.
        view.CBCTPanorexEditor = editorApi(plan, surface);
        return surface;
    }

    view.CBCTPanorexEditor = editorApi(plan, surface);
    await surface.generateDefault();
    return surface;
}

/**
 * The two methods `static/js/modality_viewers/panoramic.js:93-96` calls by name.
 *
 * Unchanged from what the Konva editor exposed, deliberately: the Edit button is on a
 * different surface, and Phase 7 is not the phase to renegotiate that.
 */
export function editorApi(plan, surface) {
    return {
        enterEditMode() {
            if (isLocked(plan)) {
                return false;
            }
            surface.activate();
            return true;
        },
        hasSavedPanoramic: () => surface.hasSaved(),
    };
}

/**
 * The surface's state machine.
 *
 * Kept as one closure rather than a class because every one of these values is read by
 * more than one of the handlers below and by nothing outside them -- and because the thing
 * being modelled genuinely is one editing session.
 */
function createSurface({ plan, data, source, doc, view, fetchImpl, mount, say }) {
    let mounted = null;
    let autoMode = false;
    let started = false;
    let geometry = null;
    let descriptor = null;
    let slice = 0;
    let autoSlice = 0;
    let mode = 'mip';
    let baked = null;
    let uuid = null;
    let revision = Number(source.revision) || 0;
    let bakeToken = 0;
    let restoreOnReady = false;

    /** Whether a running bake has been superseded. */
    const cancelled = (token) => () => token !== bakeToken;

    async function start() {
        if (started) {
            return mounted;
        }
        started = true;
        setError(plan, '');
        setProgress(plan, 0.02, 'Waiting for the CBCT');
        try {
            mounted = await mount({ plan, data, source, onReady, onGeometry, onError });
            descriptor = mounted.descriptor;
            // The viewport reports arch edits by callback, and the callbacks have to reach
            // *this* state machine. Handing it over after the mount rather than before is
            // what keeps the entry from needing a reference to a surface that does not
            // exist yet -- and a mount that forgot to would leave dragging inert, which is
            // the kind of defect only a browser finds.
            mounted.adopt?.(api);
            return mounted;
        } catch (error) {
            started = false;
            setProgress(plan, null);
            if (autoMode) {
                // Most often the segmentation does not exist yet because the CBCT job is
                // still running. That is not an error a reader should be shown; the next
                // visit tries again.
                autoMode = false;
                report(`no default panoramic yet: ${error.message}`);
                say(OUTCOMES.SKIPPED, error.message);
                return null;
            }
            setError(plan, error.message || 'Unable to start the panoramic editor.', true);
            setEditorVisible(plan, true);
            return null;
        }
    }

    /** The worker has read the segmentation and chosen an automatic slice. */
    function onReady({ dimensions, autoZ }) {
        autoSlice = autoZ;
        slice = autoZ;
        setSlice(plan, slice, dimensions.depth);
        if (restoreOnReady && canRestore(source.state, dimensions)) {
            restoreOnReady = false;
            slice = source.state.axialSlice;
            mode = source.state.defaultMode === 'raysum' ? 'raysum' : 'mip';
            setSlice(plan, slice, dimensions.depth);
            setMode(plan, mode);
            requestArch(source.state.geometrySource === 'auto' ? null : source.state.spline);
            return;
        }
        restoreOnReady = false;
        if (autoMode) {
            mode = 'mip';
            requestArch(null);
        }
    }

    /** The worker has fitted an arch. Draw it, show it live, then bake it. */
    function onGeometry(next) {
        geometry = next;
        slice = next.z;
        baked = null;
        setSaveEnabled(plan, false);
        setSlice(plan, slice);
        if (!autoMode) {
            mounted.arch.showSlice(slice);
            mounted.arch.setArch(next.controlPoints, mounted.worldFor);
            mounted.arch.setMask(next.mask, descriptor, slice);
            mounted.cpr.setArch({ geometry: next, sliceIndex: slice, descriptor, mode });
            showPane(plan, 'live');
            setEditorVisible(plan, true);
        }
        bake();
    }

    function onError(error, fatal) {
        if (autoMode) {
            autoMode = false;
            report(`default panoramic failed: ${error.message}`);
            say(OUTCOMES.FAILED, error.message);
            return;
        }
        setProgress(plan, null);
        setError(plan, error.message, fatal);
        setEditorVisible(plan, true);
    }

    function requestArch(controlPoints) {
        geometry = null;
        baked = null;
        setSaveEnabled(plan, false);
        setError(plan, '');
        setProgress(plan, 0.08, 'Fitting the dental arch');
        mounted.worker.request(slice, controlPoints);
    }

    /**
     * Bake the strips the save will carry.
     *
     * On every settled arch, not only on Save: the reader approves an image and then
     * stores it, and a bake deferred to the save would mean the thing they approved was
     * the GPU preview and the thing stored was something else.
     */
    async function bake() {
        const token = ++bakeToken;
        setProgress(plan, 0.32, 'Generating both projections');
        try {
            const strips = await mounted.projectStrips({
                descriptor,
                slab: geometry.slab,
                cancelled: cancelled(token),
                onProgress: (fraction) => setProgress(plan, 0.32 + 0.62 * fraction, 'Generating both projections'),
            });
            if (!strips) {
                return;
            }
            baked = mounted.encode(strips);
            uuid = generationUuid();
            setProgress(plan, null);
            setSaveEnabled(plan, true);
            if (!autoMode) {
                showBaked();
                setStatus(plan, `Ready | Z ${slice} | ${strips.width} columns | 41-ray slab`);
            }
            if (autoMode) {
                await save();
            }
        } catch (error) {
            onError(error, false);
        }
    }

    /** Paint the strip that will actually be stored, replacing the live reformat. */
    function showBaked() {
        mounted.paint(baked[mode], slice);
        showPane(plan, 'baked');
        setMode(plan, mode);
    }

    async function save() {
        if (!baked) {
            return;
        }
        // Captured up front: `autoMode` is cleared the moment a reader opens the editor,
        // and this save has to keep behaving the way it started.
        const silent = autoMode;
        const csrf = csrfToken(doc);
        if (!csrf) {
            if (silent) {
                autoMode = false;
                report('default panoramic not saved: no CSRF token.');
                return;
            }
            setError(plan, 'The security token is missing. Reload the page and try again.');
            return;
        }
        setSaveEnabled(plan, false);
        setProgress(plan, 0.2, 'Encoding the panoramic PNGs');
        try {
            const blobs = await mounted.encodeBlobs(baked);
            const state = saveState({
                source: { ...source, revision },
                dimensions: descriptor.dimensions,
                axialSlice: slice,
                controlPoints: geometry.controlPoints,
                geometrySource: geometry.source,
                mode,
                generationUuid: uuid,
            });
            setProgress(plan, 0.65, 'Saving the projection');
            const response = await fetchImpl(
                `/maxillo/api/patient/${view.scanId}/panoramic/generated/`,
                { method: 'POST', headers: { 'X-CSRFToken': csrf }, body: saveBody(state, blobs) }
            );
            const payload = await response.json().catch(() => null);
            const result = interpretSave(response, payload, silent);
            if (!result.saved) {
                throw new Error(result.message);
            }
            if (result.revision !== null) {
                revision = result.revision;
            }
            setProgress(plan, null);
            setStatus(plan, 'Saved');
            view.PanoramicViewer?.refreshAfterSave?.(result.conflicted ? {} : payload ?? {});
            if (silent) {
                autoMode = false;
                // The editor was never shown; leave it hidden.
                say(result.outcome);
                return;
            }
            setEditorVisible(plan, false);
        } catch (error) {
            setProgress(plan, null);
            if (silent) {
                autoMode = false;
                report(`default panoramic not saved: ${error.message}`);
                say(OUTCOMES.FAILED, error.message);
                return;
            }
            setError(plan, error.message || 'Unable to save the generated panoramic images.');
            setSaveEnabled(plan, true);
        }
    }

    const api = {
        /** The unattended pass. */
        async generateDefault() {
            if (autoMode || started) {
                return;
            }
            autoMode = true;
            mode = 'mip';
            await start();
        },

        /** A reader took over. */
        async activate() {
            autoMode = false;
            restoreOnReady = Boolean(source.state);
            setEditorVisible(plan, true);
            if (geometry) {
                showPane(plan, baked ? 'baked' : 'live');
                setStatus(plan, 'Ready. Adjust the axial arch to replace the saved panoramic.');
                return;
            }
            await start();
        },

        hasSaved: () => hasSavedPanoramic({ ...source, revision }),

        /** Bound by the entry to the toolbar. Exposed for the same reason: testability. */
        setSlice(next) {
            const clamped = Math.max(0, Math.min(descriptor.dimensions.depth - 1, Math.trunc(next)));
            if (clamped === slice) {
                return;
            }
            slice = clamped;
            setSlice(plan, slice);
            requestArch(null);
        },
        resetAuto() {
            slice = autoSlice;
            setSlice(plan, slice);
            requestArch(null);
        },
        editArch(controlPoints) {
            requestArch(controlPoints);
        },
        dragArch(controlPoints) {
            // Live, and only live: the CPR follows the handles continuously and the bake
            // waits for the release. That split is the whole point of the phase.
            //
            // The centreline is re-fitted **here, on the main thread**, from the same
            // `buildEditedGeometry` the worker would use -- a degree-12 fit over a
            // resampled spline, no voxels read, microseconds. Waiting for the worker
            // instead would make the strip lag the handles by a round trip, which is the
            // thing this replaces. The worker's reply on release is still what the bake
            // and the save use, so nothing is authoritative that was computed here.
            if (!mounted?.core || !descriptor) {
                return;
            }
            try {
                const preview = mounted.core.buildEditedGeometry(controlPoints);
                mounted.cpr.setArch({ geometry: preview, sliceIndex: slice, descriptor, mode });
            } catch {
                // An arch dragged into a shape too degenerate to fit is not an error
                // worth a banner: the handles are still moving, and the next position
                // usually fits. The release re-requests through the worker, which does
                // report.
            }
        },
        setMode(next) {
            mode = next;
            setMode(plan, mode);
            mounted?.cpr?.setMode?.(mode);
            if (baked) {
                showBaked();
                // A new UUID: the endpoint keys idempotency on it, and the default mode is
                // part of what a save records, so the same one would read as a repeat.
                uuid = generationUuid();
                setSaveEnabled(plan, true);
            }
        },
        save,
        state: () => ({ slice, mode, autoMode, revision, hasGeometry: Boolean(geometry) }),
        destroy: () => mounted?.destroy?.(),
    };
    return api;
}
