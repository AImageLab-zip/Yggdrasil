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
 * **A patient with no panoramic gets one, silently -- once the CBCT has arrived.** The MIP
 * from the automatic slice and the automatic arch, baked and saved without the editor ever
 * being shown, so a `.png` exists in the patient's files and in every export without
 * anybody drawing a spline by hand. `panoramic_warmup` drives patient pages in a hidden
 * frame precisely so this runs. It waits for `GRID_READY_EVENT`, because the volume it
 * bakes from is the one the grid loads: starting at `DOMContentLoaded` only ever reached
 * "The CBCT is still loading." See {@link createSurface}'s `armDefault`.
 *
 * **While it waits, and if it declines, the pane says so.** Not as an error -- a patient
 * uploaded five minutes ago has no panoramic for entirely ordinary reasons, and a warning
 * triangle is the wrong thing to greet them with. `placeholder.js` holds the wording.
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
import { GRID_READY_EVENT } from '../grid/bootstrap.js';
import { PLACEHOLDER, setPlaceholder } from './placeholder.js';
import {
    canEdit,
    controlPlan,
    isLocked,
    setEditReady,
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

/**
 * How long the unattended pass waits for the volume grid before giving up on this visit.
 *
 * The grid announces readiness only once it has the CBCT; a load that fails returns from
 * `bootstrapVolumeGrid` before dispatching anything, and a page that then waited forever
 * would never announce -- which costs the warm-up harness a five-minute timeout for that
 * patient. This is comfortably inside `static/js/panoramic_warmup.js`'s 300s budget, so
 * the harness hears "skipped" and moves on rather than stalling.
 */
export const GRID_WAIT_MS = 240000;

/** What the pane says when the CBCT has no segmentation to fit an arch against. */
export const NO_SEGMENTATION_TEXT =
    'No panoramic yet. A panoramic can be generated once CBCT processing has finished.';

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

/** The element carrying the page's own facts: who is looking, and at which patient. */
export const PAGE_ELEMENT_ID = 'django-data';

/**
 * The page's `canEdit` and `scanId`, **from the document rather than from globals**.
 *
 * `window.canEdit` and `window.scanId` are assigned in exactly one place -- inside
 * `static/js/patient_detail.js`'s `DOMContentLoaded` handler -- and this module runs
 * before that. `{% cornerstone_entry %}` emits a module script, which is deferred: it
 * executes once parsing finishes, with `readyState === 'interactive'`, so the entry's
 * `readyState === 'loading' ? wait : start()` starts immediately and the classic script's
 * handler has not run yet. `view.canEdit` was therefore `undefined` on every visit by
 * every user, the gate below refused, and no patient page ever generated its default
 * panoramic. `grid/bootstrap.js` documents the same race for `window.CBCTViewer`; this is
 * the same resolution -- read what the page was served with.
 *
 * The globals are still consulted as a fallback, so a page that carries them and not the
 * payload keeps working.
 *
 * @param {Document} doc
 * @param {object} [view] the window, for the fallback.
 * @returns {{canEdit: boolean, patientId: number|null}}
 */
export function readPageData(doc, view = doc?.defaultView ?? globalThis) {
    let payload = null;
    const element = doc?.getElementById?.(PAGE_ELEMENT_ID);
    if (element) {
        try {
            payload = JSON.parse(element.textContent || 'null');
        } catch {
            payload = null;
        }
    }
    const scanId = payload?.scanId ?? view?.scanId ?? null;
    return {
        canEdit: Boolean(payload?.canEdit ?? view?.canEdit),
        patientId: Number.isFinite(Number(scanId)) && scanId !== null ? Number(scanId) : null,
    };
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
 * Call back once the volume grid has this patient's CBCT on screen.
 *
 * Both orders happen: the grid's bundle may finish first, in which case the event has
 * already been dispatched and `CBCTViewer.ready` is the only record of it, or this one may
 * be listening before the grid ever starts. Reading the flag *and* subscribing covers both
 * without either bundle having to know which one it is.
 *
 * @param {object} view the window.
 * @param {() => void} callback
 * @returns {() => void} unsubscribe.
 */
export function whenGridReady(view, callback) {
    if (view?.CBCTViewer?.ready) {
        callback();
        return () => {};
    }
    const handler = () => callback();
    view?.addEventListener?.(GRID_READY_EVENT, handler, { once: true });
    return () => view?.removeEventListener?.(GRID_READY_EVENT, handler);
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
    const page = readPageData(doc, view);
    const patientId = page.patientId;
    const say = (outcome, detail) => announce(view, outcome, patientId, detail);

    if (!plan.root || !canEdit(plan) || !page.canEdit || !view.Worker) {
        report('the panoramic editor is unavailable on this page; not mounting.');
        // Nothing here can produce one, so the pane must not offer a button that would
        // only fail. Every declining path leaves the placeholder on a terminal state --
        // the template ships one, so a page whose bundle never runs still reads honestly.
        setPlaceholder(doc, PLACEHOLDER.UNAVAILABLE);
        say(OUTCOMES.SKIPPED, 'the panoramic editor is unavailable on this page');
        return null;
    }
    const source = data?.panorexSource ?? null;
    if (!source?.volumeFileId || !source?.segmentationFileId) {
        report('this patient has no paired CBCT volume and segmentation.');
        // The common case for a patient uploaded minutes ago: the CBCT job that produces
        // the segmentation is still running. Say that, rather than showing an error or a
        // button that cannot work until it finishes.
        setPlaceholder(doc, PLACEHOLDER.UNAVAILABLE, { message: NO_SEGMENTATION_TEXT });
        say(OUTCOMES.SKIPPED, 'no paired CBCT volume and segmentation');
        return null;
    }

    // Hidden first, before anything slow: the editor is never opened on load, and a
    // surface that revealed itself while deciding would flash on every patient page.
    setEditorVisible(plan, false);
    // And no way in until there is a CBCT to open it on -- see `controls.setEditReady`.
    setEditReady(plan, false);
    whenGridReady(view, () => setEditReady(plan, true));

    const surface = createSurface({ plan, data, source, doc, view, patientId, fetchImpl, mount, say });

    view.CBCTPanorexEditor = editorApi(plan, surface);

    if (hasSavedPanoramic(source)) {
        report('this patient already has a current panoramic; nothing to generate.');
        say(OUTCOMES.EXISTING);
        // Not started. Opening the editor from the button is what starts it, so a page
        // that only *displays* a panoramic never loads a worker or a second viewport.
        return surface;
    }

    // **The pass waits for the CBCT.** It used to run here, at DOMContentLoaded, where
    // `mount()` reads the volume out of Cornerstone's cache and throws "The CBCT is still
    // loading." on every visit -- swallowed by `autoMode` and announced as skipped, so no
    // patient page ever generated the default it is documented above as generating, and
    // the warm-up collected `skipped` for a whole folder. `GRID_READY_EVENT` exists for
    // exactly this (`grid/bootstrap.js:250-254` names that error as its reason); it was
    // gating the Edit button and nothing else.
    setPlaceholder(doc, PLACEHOLDER.WAITING, {
        onGenerate: () => {
            setPlaceholder(doc, PLACEHOLDER.GENERATING);
            surface.generateDefault();
        },
    });
    surface.armDefault();
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
function createSurface({ plan, data, source, doc, view, patientId, fetchImpl, mount, say }) {
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
                setPlaceholder(doc, PLACEHOLDER.OFFER);
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

    /**
     * Put the arch that has been fitted on screen.
     *
     * Separate from {@link onGeometry} because a reader can arrive at a geometry that was
     * fitted while nobody was watching. The unattended pass draws nothing -- there is no
     * editor on screen to draw into -- so a patient whose default panoramic was generated
     * on this visit had a fitted arch, a mask and a centreline in hand and an **empty**
     * axial the moment the Edit button revealed the editor, because `activate` returned on
     * `if (geometry)` without ever asking for any of it. One function, called from both.
     */
    function present(pane) {
        if (!mounted || !geometry) {
            return;
        }
        // The plane by a world point, not by a slice index: the arch's `z` counts
        // slices of the RAS-reoriented array and Cornerstone's counts steps along its
        // camera normal, and the two run opposite ways. `worldFor` is the transform
        // the control points and the mask already go through, so naming the plane with
        // it is what makes all three agree. See `archViewport.showPlane`.
        mounted.arch.showPlane(mounted.worldFor([0, 0]));
        mounted.arch.setArch(geometry.controlPoints, mounted.worldFor);
        mounted.arch.setMask(geometry.mask, descriptor, slice);
        mounted.cpr.setArch({ geometry, sliceIndex: slice, descriptor, mode });
        showPane(plan, pane);
    }

    /** The worker has fitted an arch. Draw it, show it live, then bake it. */
    function onGeometry(next) {
        geometry = next;
        slice = next.z;
        baked = null;
        setSaveEnabled(plan, false);
        setSlice(plan, slice);
        if (!autoMode) {
            present('live');
            setEditorVisible(plan, true);
        }
        bake();
    }

    function onError(error, fatal) {
        if (autoMode) {
            autoMode = false;
            report(`default panoramic failed: ${error.message}`);
            setPlaceholder(doc, PLACEHOLDER.OFFER);
            say(OUTCOMES.FAILED, error.message);
            return;
        }
        setProgress(plan, null);
        setError(plan, error.message, fatal);
        setEditorVisible(plan, true);
    }

    /**
     * Ask the worker to re-fit at the current slice.
     *
     * Guarded because the toolbar is bound before the editor is mounted: the entry calls
     * `bindControls` unconditionally (`panoramic-cpr.js`), while the saved-panoramic path
     * above returns before `mount()`, so `mounted` and `descriptor` are still null until
     * the reader clicks Edit. A no-op is the honest answer -- there is nothing to re-fit
     * yet -- and it is what `dragArch` already does.
     */
    function requestArch(controlPoints) {
        if (!mounted) {
            return;
        }
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
                setPlaceholder(doc, PLACEHOLDER.OFFER);
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
                `/maxillo/api/patient/${patientId}/panoramic/generated/`,
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
                setPlaceholder(doc, PLACEHOLDER.OFFER);
                say(OUTCOMES.FAILED, error.message);
                return;
            }
            setError(plan, error.message || 'Unable to save the generated panoramic images.');
            setSaveEnabled(plan, true);
        }
    }

    const api = {
        /**
         * Wait for the CBCT, then run the unattended pass.
         *
         * Both halves matter. `mount()` reads the volume out of Cornerstone's cache, so
         * starting before the grid has it is starting into "The CBCT is still loading.";
         * and a grid whose load *failed* never announces at all, so the wait is bounded
         * and its expiry announces in its place -- see {@link GRID_WAIT_MS}.
         */
        armDefault() {
            let settled = false;
            const timeout = (view.setTimeout ?? globalThis.setTimeout).call(view, () => {
                if (settled) {
                    return;
                }
                settled = true;
                report('the CBCT did not finish loading; no default panoramic this visit.');
                setPlaceholder(doc, PLACEHOLDER.OFFER);
                say(OUTCOMES.SKIPPED, 'the CBCT did not finish loading');
            }, GRID_WAIT_MS);
            whenGridReady(view, () => {
                if (settled) {
                    return;
                }
                settled = true;
                (view.clearTimeout ?? globalThis.clearTimeout).call(view, timeout);
                setPlaceholder(doc, PLACEHOLDER.GENERATING);
                api.generateDefault();
            });
        },

        /** The unattended pass. */
        async generateDefault() {
            if (autoMode) {
                return;
            }
            autoMode = true;
            mode = 'mip';
            if (mounted) {
                // The pane's Generate button, after a pass that got as far as mounting
                // and then failed -- a bake or a save. The volume and the worker are
                // still here, so re-fitting is the whole of the retry; `start()` would
                // return on `started` and the button would do nothing.
                requestArch(null);
                return;
            }
            await start();
        },

        /** A reader took over. */
        async activate() {
            autoMode = false;
            restoreOnReady = Boolean(source.state);
            setEditorVisible(plan, true);
            if (geometry) {
                // Drawn now, not when it was fitted: see `present`.
                present('live');
                if (baked) {
                    showBaked();
                }
                setStatus(plan, 'Ready. Adjust the axial arch to replace the saved panoramic.');
                return;
            }
            await start();
        },

        hasSaved: () => hasSavedPanoramic({ ...source, revision }),

        /** Bound by the entry to the toolbar. Exposed for the same reason: testability. */
        setSlice(next) {
            if (!descriptor) {
                return;
            }
            const clamped = Math.max(0, Math.min(descriptor.dimensions.depth - 1, Math.trunc(next)));
            if (clamped === slice) {
                return;
            }
            slice = clamped;
            setSlice(plan, slice);
            requestArch(null);
        },
        resetAuto() {
            if (!mounted) {
                return;
            }
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
