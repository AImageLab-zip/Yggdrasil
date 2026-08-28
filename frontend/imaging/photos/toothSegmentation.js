/**
 * The tooth-segmentation editor.
 *
 * Replaces `static/js/intraoral_segmentation.js` (1901 lines of Konva: three layers,
 * hand-rolled tension midpoint handles, per-image edit sessions, a 32-button grid). What
 * survives the port is everything that was a *decision* rather than a Konva mechanism:
 *
 * - **The representation.** Edits mutate `{[fileId]: {FDI: [[[x, y], …], …]}}` and the
 *   viewer is redrawn from it. Cornerstone's annotation state is keyed by `annotationUID`,
 *   the one identifier the governing rule says is never persisted, so an editor built on it
 *   would hold references to objects that stop existing on reload.
 * - **The undo log** (`../annotations/history.js`), over that representation.
 * - **The image-edit replay** (`./editReplay.js`), so a photograph cropped or rotated
 *   mid-session shows its polygons in the right place.
 * - **The autosave**: a 250 ms debounce, one request in flight per image, a version guard,
 *   and a coalesced follow-up. Ported from `scheduleSave`/`saveImage:729-796` because the
 *   editing pattern it exists for is unchanged -- a drag emits a save per mouse-move.
 * - **`tension: 0.35`**, exactly, via `../annotations/tensionSpline.js`.
 *
 * What changes is that the concurrency check is now the revision number rather than an
 * `updated_at` string, so a stale save is refused by the unique constraint on
 * `(annotation_set, revision_number)` instead of by comparing timestamps.
 *
 * ## Cornerstone is injected
 *
 * Same contract as the rest of `photos/`: this module never imports Cornerstone. It is
 * handed a stack handle, the two coordinate converters and an event subscription, so the
 * state machine, the autosave and every conversion are
 * `node --test`-able. Only `entries/photo-stack.js` supplies the real ones.
 */

import {
    actionsBetween,
    canRedo,
    canUndo,
    clearHistory,
    createHistory,
    record,
    redo,
    undo,
} from '../annotations/history.js';
import { isFdiCode } from './labelMapper.js';
import {
    MIN_VERTICES,
    fdiOf,
    outlinesToDraw,
    setFdi,
    teethDiffer,
    teethFromAnnotations,
    unassignedOutlines,
} from './toothOutlines.js';
import {
    confirmControl,
    onlySelectedControl,
    renderToothGrid,
    toothButtons,
} from './toothGrid.js';

/** Element ids the intraoral template must provide. A missing one disables its feature. */
export const SEGMENTATION_CONTROL_IDS = Object.freeze({
    mode: 'segMode',
    tools: 'segTools',
    teeth: 'segTeethGrid',
    onlySelected: 'segOnlySelectedBtn',
    confirm: 'segConfirmBtn',
    reset: 'segResetViewBtn',
    undo: 'segUndoBtn',
    redo: 'segRedoBtn',
    status: 'segStatusText',
});

/** The debounce the old editor used. A drag emits one save per mouse-move without it. */
export const SAVE_DELAY_MS = 250;

export const MESSAGES = Object.freeze({
    selectTooth: 'Select a tooth, then draw its outline.',
    drawing: (code) => `Tooth ${code}. Click to place points, click the first point to close.`,
    confirmed: 'Confirmed. Reopen to edit.',
    readOnly: 'You do not have permission to edit this segmentation.',
    saving: 'Saving…',
    saved: 'Saved.',
    saveFailed: 'Save failed.',
    stale: 'Segmentation changed elsewhere. Reloading…',
    unassigned: (count) =>
        `${count} outline${count === 1 ? '' : 's'} ${count === 1 ? 'has' : 'have'} no tooth ` +
        'selected and cannot be saved. Select a tooth before drawing.',
});

/**
 * Resolve the controls once.
 *
 * @param {Document} doc
 * @returns {object}
 */
export function segmentationControlPlan(doc) {
    const plan = {};
    for (const [key, id] of Object.entries(SEGMENTATION_CONTROL_IDS)) {
        plan[key] = doc?.getElementById?.(id) ?? null;
    }
    return plan;
}

/**
 * Build the editor.
 *
 * @param {object} options
 * @param {object} options.stack the handle from `createPhotoStack`.
 * @param {object} options.plan from {@link segmentationControlPlan}.
 * @param {string} options.toolName the contour tool's registered name.
 * @param {object} options.endpoints `{state, save}` URLs.
 * @param {object} options.cornerstone `{worldToImage, imageToWorld, splineType,
 *   onAnnotationChange, removeAnnotation, readAnnotations}`.
 * @param {object} [options.io] `{fetchImpl, csrfToken, setTimeoutImpl, clearTimeoutImpl}`.
 * @param {boolean} [options.canModify]
 * @param {(type: string, message: string) => void} [options.report]
 * @returns {object} the editor handle.
 */
export function createToothEditor({
    stack,
    plan,
    toolName,
    endpoints,
    cornerstone,
    io = {},
    canModify = false,
    report = () => {},
}) {
    const {
        worldToImage,
        imageToWorld,
        splineType,
        onAnnotationChange,
        removeAnnotation,
        readAnnotations,
    } = cornerstone;
    const fetchImpl = io.fetchImpl ?? globalThis.fetch;
    const csrfToken = io.csrfToken ?? (() => '');
    const setTimeoutImpl = io.setTimeoutImpl ?? globalThis.setTimeout;
    const clearTimeoutImpl = io.clearTimeoutImpl ?? globalThis.clearTimeout;

    const state = {
        /** `{[fileId]: {FDI: polygons}}` -- the representation everything edits. */
        teethByFile: {},
        /** What the server last confirmed, to decide whether a save is needed at all. */
        savedByFile: {},
        confirmations: {},
        revision: 0,
        /** fileId -> imageId, so an annotation can be traced back to a record. */
        imageIdByFile: new Map(),
        fileByImageId: new Map(),
        currentFileId: null,
        selectedTooth: null,
        onlySelected: false,
        history: createHistory(),
        /** Geometry as Cornerstone last reported it, for the action diff. */
        lastKnown: new Map(),
        /** Suppress the change handler while we are the ones writing annotations. */
        restoring: false,
        /**
         * Is the Teeth switch on?
         *
         * Held here as well as in the DOM because it decides whether the outlines are
         * *visible*, and every redraw has to re-apply that -- a freshly added annotation is
         * visible by default, so restoring an image while the switch is off would put the
         * outlines back on screen.
         */
        mode: false,
        saveTimers: new Map(),
        saveVersions: new Map(),
        saveInFlight: new Set(),
        savePending: new Set(),
    };

    // -- reading and writing the server ------------------------------------

    async function load() {
        const response = await fetchImpl(endpoints.state);
        if (!response.ok) {
            throw new Error(`Could not load the segmentation (HTTP ${response.status}).`);
        }
        const payload = await response.json();
        state.revision = Number(payload.revision) || 0;
        state.teethByFile = {};
        state.savedByFile = {};
        for (const [fileId, teeth] of Object.entries(payload.images ?? {})) {
            state.teethByFile[Number(fileId)] = structuredClone(teeth);
            state.savedByFile[Number(fileId)] = structuredClone(teeth);
        }
        state.confirmations = {};
        for (const [fileId, confirmed] of Object.entries(payload.confirmations ?? {})) {
            state.confirmations[Number(fileId)] = Boolean(confirmed);
        }
        clearHistory(state.history);
        return payload;
    }

    function isEditable(fileId = state.currentFileId) {
        return canModify && fileId !== null && !state.confirmations[fileId];
    }

    /**
     * Queue a save for one image.
     *
     * The version counter is what makes "Saved." honest: it is bumped on every queue, and
     * the message is only shown if no later edit arrived while the request was in flight.
     */
    function scheduleSave(fileId) {
        if (!canModify || fileId === null) {
            return;
        }
        state.saveVersions.set(fileId, (state.saveVersions.get(fileId) ?? 0) + 1);
        clearTimeoutImpl(state.saveTimers.get(fileId));
        setStatus(MESSAGES.saving);
        state.saveTimers.set(
            fileId,
            // The promise is returned rather than discarded. `setTimeout` ignores it, but a
            // caller that controls the clock -- a test -- can await the save it just fired
            // instead of racing it, which is the difference between a deterministic
            // assertion and a flaky one.
            setTimeoutImpl(() => saveImage(fileId), SAVE_DELAY_MS)
        );
    }

    async function saveImage(fileId, { confirmed = null } = {}) {
        if (!canModify) {
            return;
        }
        if (state.saveInFlight.has(fileId)) {
            // One request per image at a time. Two in flight would race on the revision
            // number and the loser would get a 409 for its own earlier keystroke.
            state.savePending.add(fileId);
            return;
        }
        const version = state.saveVersions.get(fileId) ?? 0;
        const teeth = state.teethByFile[fileId] ?? {};

        state.saveInFlight.add(fileId);
        try {
            const response = await fetchImpl(endpoints.save, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken(),
                },
                body: JSON.stringify({
                    expectedRevision: state.revision,
                    images: [
                        {
                            fileId,
                            teeth,
                            // Tri-state: `null` leaves confirmation alone, which is what
                            // every autosave must do -- otherwise the first save after a
                            // confirmation silently retracts it.
                            isConfirmed: confirmed,
                        },
                    ],
                }),
            });
            const payload = await readJson(response);
            if (response.status === 409) {
                // Somebody else saved. Reload rather than retry: retrying would overwrite
                // whoever won, which is the one thing the revision constraint exists to
                // prevent.
                setStatus(MESSAGES.stale);
                report('warning', payload?.error ?? MESSAGES.stale);
                await reload();
                return;
            }
            if (!response.ok) {
                setStatus(MESSAGES.saveFailed);
                report('danger', payload?.error ?? MESSAGES.saveFailed);
                return;
            }
            state.revision = Number(payload.revision) || state.revision;
            state.savedByFile[fileId] = structuredClone(teeth);
            for (const [id, value] of Object.entries(payload.confirmations ?? {})) {
                state.confirmations[Number(id)] = Boolean(value);
            }
            if ((state.saveVersions.get(fileId) ?? 0) === version && !state.savePending.has(fileId)) {
                setStatus(MESSAGES.saved);
            }
            renderControls();
        } catch (error) {
            setStatus(MESSAGES.saveFailed);
            report('danger', error.message || MESSAGES.saveFailed);
        } finally {
            state.saveInFlight.delete(fileId);
            if (state.savePending.has(fileId) || (state.saveVersions.get(fileId) ?? 0) !== version) {
                state.savePending.delete(fileId);
                await saveImage(fileId);
            }
        }
    }

    async function readJson(response) {
        const text = await response.text();
        if (!text) {
            return {};
        }
        try {
            return JSON.parse(text);
        } catch {
            return {};
        }
    }

    async function reload() {
        await load();
        await showImage(state.currentFileId, { force: true });
    }

    // -- the viewer ---------------------------------------------------------

    /**
     * Draw one image's stored polygons.
     *
     * Every existing outline is removed first and rebuilt from the map. Diffing
     * Cornerstone's state against the map would need a stable per-annotation identity, and
     * the only candidate is the `annotationUID` -- which is never persisted, so after a
     * reload there is nothing to diff against.
     */
    async function showImage(fileId, { force = false } = {}) {
        if (fileId === null || fileId === undefined) {
            return;
        }
        if (!force && fileId === state.currentFileId) {
            return;
        }
        const imageId = state.imageIdByFile.get(fileId);
        if (!imageId) {
            return;
        }
        state.currentFileId = fileId;

        state.restoring = true;
        try {
            // Every outline this tool holds, not just the incoming image's. Filtering by
            // the current image would leave the *previous* one's outlines on screen -- in
            // the wrong place, and attributed to the wrong file on the next save.
            for (const annotation of allOutlines()) {
                if (annotation.annotationUID) {
                    removeAnnotation(annotation.annotationUID);
                }
            }
            state.lastKnown.clear();
            const outlines = outlinesToDraw(state.teethByFile[fileId] ?? {}, {
                imageId,
                imageToWorld,
            });
            for (const outline of outlines) {
                stack.addToothOutline({
                    imageId,
                    label: outline.fdi,
                    worldPoints: outline.points,
                    splineType,
                    toolName,
                });
            }
        } finally {
            state.restoring = false;
        }
        applyVisibility();
        rememberGeometry();
        renderControls();
        setStatus(statusForImage());
    }

    /**
     * Show or hide the outlines to match the switch.
     *
     * Symmetrical with the measurement toggle, which hides its measurements when Measure is
     * off. Leaving tooth outlines drawn while the switch reads "off" made the switch look
     * broken: it says the segmentation is not in play, and the screen said otherwise.
     */
    function applyVisibility() {
        stack.setAnnotationsVisible?.(state.mode, [toolName]);
    }

    /** Every outline this tool holds, across all images. */
    function allOutlines() {
        return readAnnotations().filter(
            (annotation) => annotation?.metadata?.toolName === toolName
        );
    }

    /** The outlines on the image being edited. */
    function ownOutlines() {
        const imageId = state.imageIdByFile.get(state.currentFileId);
        return readAnnotations().filter(
            (annotation) =>
                annotation?.metadata?.toolName === toolName &&
                annotation?.metadata?.referencedImageId === imageId
        );
    }

    /** Snapshot the current geometry, so the next change can be diffed against it. */
    function rememberGeometry() {
        state.lastKnown.clear();
        const outlines = ownOutlines();
        const seenPerTooth = new Map();
        for (const annotation of outlines) {
            if (!annotation.annotationUID) {
                continue;
            }
            const code = fdiOf(annotation);
            const polygonIndex = code ? seenPerTooth.get(code) ?? 0 : null;
            if (code) {
                seenPerTooth.set(code, polygonIndex + 1);
            }
            // The *position* is remembered as well as the geometry, and that is not
            // belt-and-braces: `ANNOTATION_REMOVED` fires after Cornerstone has dropped the
            // annotation, so `locate` can no longer find it and a deletion would go
            // unrecorded -- deleted, saved, and not undoable.
            state.lastKnown.set(annotation.annotationUID, {
                points: (annotation.data?.handles?.points ?? []).map((point) => [...point]),
                tooth: code,
                polygonIndex,
            });
        }
    }

    /**
     * Where in the teeth map an annotation's polygon lives.
     *
     * By position among the outlines that share its FDI code, in Cornerstone's own order --
     * which is the order they were drawn back in, which is the order they are stored in. The
     * index is what the history log names, so it has to mean the same thing on both sides.
     */
    function locate(annotation) {
        const code = fdiOf(annotation);
        if (!code) {
            return null;
        }
        const siblings = ownOutlines().filter((other) => fdiOf(other) === code);
        const polygonIndex = siblings.indexOf(annotation);
        return polygonIndex < 0 ? null : { tooth: code, polygonIndex };
    }

    /** Re-derive the whole map for the image on screen from what Cornerstone holds. */
    function syncFromViewer() {
        const imageId = state.imageIdByFile.get(state.currentFileId);
        if (!imageId) {
            return false;
        }
        const teeth = teethFromAnnotations(readAnnotations(), {
            imageId,
            worldToImage,
            toolName,
            bounds: stack.imageBounds?.(imageId),
        });
        if (!teethDiffer(state.teethByFile[state.currentFileId] ?? {}, teeth)) {
            return false;
        }
        state.teethByFile[state.currentFileId] = teeth;
        return true;
    }

    /**
     * Cornerstone reported a change to one of our outlines.
     *
     * Ignored while we are the ones writing -- hydrating a restore fires the same events,
     * and recording those would fill the undo log with the act of loading.
     */
    function onChange(annotation) {
        if (state.restoring || annotation?.metadata?.toolName !== toolName) {
            return;
        }
        if (annotation.metadata.referencedImageId !== state.imageIdByFile.get(state.currentFileId)) {
            return;
        }
        if (!isEditable()) {
            // A confirmed image or a read-only user. Put the stored shape back rather than
            // leaving an edit on screen the server will refuse.
            void showImage(state.currentFileId, { force: true });
            report('warning', canModify ? MESSAGES.confirmed : MESSAGES.readOnly);
            return;
        }

        // A newly drawn outline has no tooth yet: stamp the selected one on it.
        if (!fdiOf(annotation) && isFdiCode(state.selectedTooth)) {
            setFdi(annotation, state.selectedTooth);
        }

        const at = locate(annotation);
        if (at) {
            const previous = state.lastKnown.get(annotation.annotationUID);
            const next = (annotation.data?.handles?.points ?? []).map((point) => [...point]);
            const geometry = (points) =>
                points.map((world) => worldToImage(annotation.metadata.referencedImageId, world));
            const actions = previous
                ? actionsBetween(geometry(previous.points), geometry(next), {
                      fileId: state.currentFileId,
                      ...at,
                  })
                : [
                      {
                          type: 'polygon-create',
                          fileId: state.currentFileId,
                          ...at,
                          polygon: geometry(next),
                      },
                  ];
            // Recorded before the map is re-derived, so an undo of the *first* change to a
            // freshly drawn outline removes the outline rather than reverting one handle.
            for (const action of actions) {
                record(state.history, action);
            }
        }

        if (syncFromViewer()) {
            scheduleSave(state.currentFileId);
        }
        rememberGeometry();
        renderControls();
        setStatus(statusForImage());
    }

    /** An outline was deleted in the viewer. */
    function onRemoved(annotation) {
        if (state.restoring || annotation?.metadata?.toolName !== toolName) {
            return;
        }
        // From the remembered position, not from `locate`: the annotation is already out of
        // Cornerstone's state by the time this fires.
        const previous = state.lastKnown.get(annotation.annotationUID);
        if (previous?.tooth && isEditable()) {
            record(state.history, {
                type: 'polygon-delete',
                fileId: state.currentFileId,
                tooth: previous.tooth,
                polygonIndex: previous.polygonIndex,
                polygon: previous.points.map((world) =>
                    worldToImage(annotation.metadata.referencedImageId, world)
                ),
            });
        }
        if (syncFromViewer()) {
            scheduleSave(state.currentFileId);
        }
        rememberGeometry();
        renderControls();
    }

    // -- history ------------------------------------------------------------

    async function undoOne() {
        const action = undo(state.history, state.teethByFile);
        if (!action) {
            return;
        }
        await afterHistory(action);
    }

    async function redoOne() {
        const action = redo(state.history, state.teethByFile);
        if (!action) {
            return;
        }
        await afterHistory(action);
    }

    /**
     * An action can belong to an image that is not the one on screen.
     *
     * Following it is the point: an undo that silently changed an image the user was not
     * looking at, with no feedback, is how work disappears.
     */
    async function afterHistory(action) {
        if (action.fileId !== state.currentFileId) {
            await showImage(action.fileId, { force: true });
        } else {
            await showImage(state.currentFileId, { force: true });
        }
        scheduleSave(action.fileId);
    }

    // -- confirmation -------------------------------------------------------

    async function toggleConfirmation() {
        const fileId = state.currentFileId;
        if (fileId === null || !canModify) {
            return;
        }
        const next = !state.confirmations[fileId];
        state.confirmations[fileId] = next;
        // Immediate, not debounced: this is an explicit act, and the user is entitled to
        // find out now whether it took.
        clearTimeoutImpl(state.saveTimers.get(fileId));
        state.saveVersions.set(fileId, (state.saveVersions.get(fileId) ?? 0) + 1);
        await saveImage(fileId, { confirmed: next });
        await showImage(fileId, { force: true });
        renderControls();
        setStatus(next ? MESSAGES.confirmed : statusForImage());
    }

    // The image-edit replay is deliberately *not* here.
    //
    // `rgb_editor.js` writes a new `FileRegistry` row when a photograph is cropped or
    // rotated, and the bootstrap reloads the page afterwards -- so the polygons come back
    // through `load()`, already re-projected onto the new row by
    // `annotations.services.segmentation`. Doing it here as well would be a second
    // implementation of the transform on the same side of the wire, which is how the
    // client and the server came to disagree about rotation in the first place.

    // -- the DOM ------------------------------------------------------------

    function setStatus(message) {
        if (plan?.status) {
            plan.status.textContent = message;
        }
    }

    function statusForImage() {
        if (!canModify) {
            return MESSAGES.readOnly;
        }
        if (state.confirmations[state.currentFileId]) {
            return MESSAGES.confirmed;
        }
        const stray = unassignedOutlines(readAnnotations(), {
            imageId: state.imageIdByFile.get(state.currentFileId),
            toolName,
        });
        if (stray.length) {
            return MESSAGES.unassigned(stray.length);
        }
        return state.selectedTooth ? MESSAGES.drawing(state.selectedTooth) : MESSAGES.selectTooth;
    }

    function renderControls() {
        const teeth = state.teethByFile[state.currentFileId] ?? {};
        renderToothGrid(plan?.teeth, toothButtons({
            teeth,
            selected: state.selectedTooth,
            onlySelected: state.onlySelected,
            editable: isEditable(),
        }), {
            onSelect: (code) => selectTooth(state.selectedTooth === code ? null : code),
            onZoom: (code) => zoomToTooth(code),
        });

        const only = onlySelectedControl({
            onlySelected: state.onlySelected,
            hasImage: state.currentFileId !== null,
            selected: state.selectedTooth,
        });
        if (plan?.onlySelected) {
            plan.onlySelected.textContent = only.label;
            plan.onlySelected.disabled = only.disabled;
            plan.onlySelected.classList.toggle('active', only.active);
        }

        const confirmState = confirmControl({
            confirmed: Boolean(state.confirmations[state.currentFileId]),
            hasImage: state.currentFileId !== null,
            canModify,
        });
        if (plan?.confirm) {
            plan.confirm.textContent = confirmState.label;
            plan.confirm.disabled = confirmState.disabled;
            plan.confirm.classList.toggle('confirmed', confirmState.confirmed);
        }

        if (plan?.undo) {
            plan.undo.disabled = !isEditable() || !canUndo(state.history);
        }
        if (plan?.redo) {
            plan.redo.disabled = !isEditable() || !canRedo(state.history);
        }
    }

    function selectTooth(code) {
        state.selectedTooth = isFdiCode(code) ? code : null;
        renderControls();
        setStatus(statusForImage());
    }

    /** Frame one tooth's outlines. Double-click on its button. */
    function zoomToTooth(code) {
        const polygons = state.teethByFile[state.currentFileId]?.[code];
        if (!Array.isArray(polygons) || !polygons.length) {
            return;
        }
        const points = polygons.flat();
        const xs = points.map((point) => point[0]);
        const ys = points.map((point) => point[1]);
        stack.frameImageRegion?.({
            imageId: state.imageIdByFile.get(state.currentFileId),
            minX: Math.min(...xs),
            maxX: Math.max(...xs),
            minY: Math.min(...ys),
            maxY: Math.max(...ys),
        });
        selectTooth(code);
    }

    // -- wiring -------------------------------------------------------------

    const unsubscribe = onAnnotationChange?.({
        onChange,
        onRemoved,
    });

    plan?.onlySelected?.addEventListener?.('click', () => {
        state.onlySelected = !state.onlySelected;
        renderControls();
    });
    plan?.confirm?.addEventListener?.('click', () => void toggleConfirmation());
    plan?.undo?.addEventListener?.('click', () => void undoOne());
    plan?.redo?.addEventListener?.('click', () => void redoOne());
    plan?.reset?.addEventListener?.('click', () => stack.resetCamera?.());

    return {
        load,
        showImage,
        selectTooth,
        undo: undoOne,
        redo: redoOne,
        toggleConfirmation,
        renderControls,
        setStatus,
        reload,

        /** Tell the editor which `FileRegistry` row each imageId belongs to. */
        setImages(records) {
            state.imageIdByFile = new Map(records.map((record_) => [record_.fileId, record_.imageId]));
            state.fileByImageId = new Map(records.map((record_) => [record_.imageId, record_.fileId]));
        },

        /** The file the given imageId belongs to, for the stack's scroll handler. */
        fileFor(imageId) {
            return state.fileByImageId.get(imageId) ?? null;
        },

        /** Segmentation mode on or off, mirrored onto the viewport. */
        setMode(enabled) {
            state.mode = Boolean(enabled);
            stack.setSegmentationMode(enabled);
            applyVisibility();
            if (plan?.tools) {
                if (enabled) {
                    plan.tools.removeAttribute?.('hidden');
                } else {
                    plan.tools.setAttribute?.('hidden', '');
                }
            }
            setStatus(enabled ? statusForImage() : '');
            renderControls();
        },

        /** Are there outlines with no tooth, which the server would refuse? */
        unassignedCount() {
            return unassignedOutlines(readAnnotations(), {
                imageId: state.imageIdByFile.get(state.currentFileId),
                toolName,
            }).length;
        },

        destroy() {
            for (const timer of state.saveTimers.values()) {
                clearTimeoutImpl(timer);
            }
            unsubscribe?.();
        },

        /** For tests and the debug console. Never read by the surface itself. */
        state,
        MIN_VERTICES,
    };
}
