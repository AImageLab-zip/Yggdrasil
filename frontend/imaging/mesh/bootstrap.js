/**
 * Mounting the IOS mesh viewer and its landmark workbench.
 *
 * Same contract as `photos/bootstrap.js`: read the DOM, never throw into the page, and say
 * out loud when it declines to run. The patient-detail page carries several surfaces and a
 * clinician needs the rest of it, so a broken payload here must not take the record down.
 *
 * Cornerstone and vtk reach this module only through `mount`, which the entry supplies.
 * Every decision worth testing -- what a click does, what gets saved, when a control is
 * disabled -- lives in the pure modules beside this one and is exercised against a fake
 * viewport.
 */

import {
    EDITABLE_TYPES,
    LANDMARK_TYPES,
    TYPE_LABELS,
    cloneDocument,
    countForTooth,
    emptyDocument,
    fromState,
    jawForTooth,
    refusePlacement,
    toSaveBody,
} from './landmarkDocument.js';
import {
    canRedo,
    canUndo,
    clearHistory,
    createHistory,
    placeAndRecord,
    redo,
    removeAndRecord,
    undo,
} from './landmarkHistory.js';
import {
    DEFAULT_MARKER_SIZE,
    cssColor,
    landmarkForUid,
    markersFor,
} from './landmarkMarkers.js';
import {
    CONFLICT_MESSAGE,
    SAVED_MESSAGE,
    UNSAVED_MESSAGE,
    controlPlan,
    instructionFor,
    onClick,
    setDisabled,
    setEyeIcon,
    setPressed,
    setSwitch,
} from './meshControls.js';
import { renderToothGrid, toothButtons } from '../photos/toothGrid.js';
import { DEFAULT_GRID_SIZE, createOverlay, drawGrid, resizeOverlay } from './screenGrid.js';
import { isPlacementEvent, isSelectionEvent } from './pickMath.js';
import { interpretSaveResponse } from '../annotations/protocol.js';

export const LOG_PREFIX = '[ygg-ios]';

/** The element carrying this surface's JSON payload. */
export const DATA_ELEMENT_ID = 'meshLandmarkData';

export function report(message, detail) {
    const line = `${LOG_PREFIX} ${message}`;
    if (detail === undefined) console.info(line);
    else console.info(line, detail);
}

/**
 * Read the surface's payload.
 *
 * @returns {object|null} `{patientId, projectNamespace, canModify, meshEndpoint,
 *   landmarkEndpoint}`
 */
export function readMeshData(doc, elementId = DATA_ELEMENT_ID) {
    const element = doc?.getElementById?.(elementId);
    if (!element) return null;
    try {
        const parsed = JSON.parse(element.textContent ?? '{}');
        if (!parsed?.patientId || !parsed?.meshEndpoint || !parsed?.landmarkEndpoint) {
            return null;
        }
        return parsed;
    } catch (error) {
        report(`#${elementId} is not valid JSON: ${error.message}`);
        return null;
    }
}

/** Django's CSRF token: the hidden input, because `CSRF_USE_SESSIONS` leaves no cookie. */
export function csrfToken(doc) {
    return doc?.querySelector?.('input[name="csrfmiddlewaretoken"]')?.value ?? '';
}

/**
 * Mount the surface.
 *
 * @param {object} options
 * @param {Function} options.mount the entry's viewport factory.
 * @param {Document} [options.doc]
 * @param {Function} [options.fetchImpl]
 */
export async function bootstrapMeshLandmarks({
    mount,
    doc = globalThis.document,
    fetchImpl = globalThis.fetch?.bind(globalThis),
} = {}) {
    const data = readMeshData(doc);
    if (!data) {
        report('no #meshLandmarkData payload on this page; not mounting.');
        return null;
    }
    const controls = controlPlan(doc);
    if (!controls.viewport) {
        report(`no #${'scan-viewer'} element; not mounting.`);
        return null;
    }

    // ---------------------------------------------------------------- state
    const state = {
        document: emptyDocument(),
        revision: 0,
        history: createHistory(),
        active: false,
        showLandmarks: false,
        tool: 'place',
        selectedTooth: '',
        selectedType: null,
        selected: null,
        typeVisible: {},
        markerSize: DEFAULT_MARKER_SIZE,
        showAxis: true,
        gridSize: 0,
        wireframe: false,
        whiteBackground: false,
        dirty: false,
        canEdit: Boolean(data.canModify),
    };

    const meshUrls = await loadMeshUrls(fetchImpl, data);
    if (!meshUrls) {
        report('the patient has no complete IOS scan pair; not mounting.');
        return null;
    }

    const viewport = await mount({
        element: controls.viewport,
        onMarkerPicked: (uid) => {
            state.selected = landmarkForUid(state.document, uid);
            if (state.selected) state.selectedTooth = state.selected.tooth;
            redraw();
        },
        onSurfacePicked: (point, hitJaw) => placeLandmark(point, hitJaw),
        shouldPlace: (event) => state.active && state.tool === 'place' && isPlacementEvent(event),
        shouldSelect: (event) => state.active && state.tool === 'select' && isSelectionEvent(event),
    });

    const overlay = createOverlay(doc, controls.viewport);
    let themeObserver = null;

    try {
        await viewport.load(meshUrls);
    } catch (error) {
        report('the scans could not be rendered:', error);
        return null;
    }

    await reloadLandmarks();
    buildTypeButtons();
    buildVisibilityMenu();
    bindControls();
    redraw();
    report(`mounted for patient ${data.patientId}.`);

    // ---------------------------------------------------------------- data
    async function loadMeshUrls(fetcher, payload) {
        try {
            const response = await fetcher(payload.meshEndpoint);
            if (!response.ok) return null;
            const body = await response.json();
            if (!body?.upper_scan_url || !body?.lower_scan_url) return null;
            return { upper: body.upper_scan_url, lower: body.lower_scan_url };
        } catch (error) {
            report('the scan endpoint could not be read:', error);
            return null;
        }
    }

    async function reloadLandmarks() {
        try {
            const response = await fetchImpl(`${data.landmarkEndpoint}state/`);
            if (!response.ok) {
                setStatus('Landmarks could not be loaded');
                return;
            }
            const body = await response.json();
            state.document = fromState(body?.jaws);
            state.revision = Number(body?.revision) || 0;
            state.dirty = false;
            clearHistory(state.history);
            state.selected = null;
        } catch (error) {
            report('the landmark state could not be read:', error);
            setStatus('Landmarks could not be loaded');
        }
    }

    async function save() {
        if (!state.dirty || !state.canEdit) return;
        setStatus('Saving...');
        try {
            const response = await fetchImpl(data.landmarkEndpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken(doc),
                },
                body: JSON.stringify(toSaveBody(state.document, state.revision)),
            });
            const body = await response.json().catch(() => ({}));
            const outcome = interpretSaveResponse(response, body);
            if (outcome.saved) {
                state.revision = outcome.revision ?? state.revision;
                state.dirty = false;
                clearHistory(state.history);
                setStatus(SAVED_MESSAGE);
                globalThis.appNotify?.('success', 'Landmarks saved');
            } else if (outcome.reload) {
                // Never retry with a bumped revision: that is exactly the overwrite the
                // unique constraint exists to prevent.
                setStatus('Save failed');
                globalThis.appNotify?.('danger', CONFLICT_MESSAGE);
            } else {
                setStatus('Save failed');
                globalThis.appNotify?.('danger', outcome.message);
            }
        } catch (error) {
            report('the save failed:', error);
            setStatus('Save failed');
        }
        redraw();
    }

    // ---------------------------------------------------------------- editing
    function placeLandmark(point, hitJaw) {
        if (!state.canEdit) return;
        const refusal = refusePlacement({
            tooth: state.selectedTooth,
            type: state.selectedType,
            hitJaw,
        });
        if (refusal) {
            setStatus(refusal);
            return;
        }
        placeAndRecord(state.history, state.document, {
            jaw: jawForTooth(state.selectedTooth),
            tooth: state.selectedTooth,
            type: state.selectedType,
            point,
        });
        state.selected = null;
        state.dirty = true;
        setStatus(
            `Placed ${TYPE_LABELS[state.selectedType] ?? state.selectedType} on tooth ` +
            `${state.selectedTooth} · ${UNSAVED_MESSAGE}`,
        );
        redraw();
    }

    function deleteSelected() {
        if (!state.canEdit || !state.selected) return;
        if (!EDITABLE_TYPES.includes(state.selected.type)) {
            setStatus(`${state.selected.type} landmarks are read-only`);
            return;
        }
        removeAndRecord(state.history, state.document, state.selected);
        state.selected = null;
        state.dirty = true;
        setStatus(UNSAVED_MESSAGE);
        redraw();
    }

    function step(direction) {
        const action = direction === 'undo'
            ? undo(state.history, state.document)
            : redo(state.history, state.document);
        if (!action) return;
        state.selected = null;
        state.selectedTooth = action.at?.tooth ?? state.selectedTooth;
        state.dirty = true;
        setStatus(UNSAVED_MESSAGE);
        redraw();
    }

    // ---------------------------------------------------------------- rendering
    function redraw() {
        viewport.setMarkers(
            markersFor(state.document, {
                visible: state.active || state.showLandmarks,
                jawVisible: viewport.jawVisibility(),
                typeVisible: state.typeVisible,
                selected: state.selected,
                markerSize: state.markerSize,
            }),
        );
        refreshToothGrid();
        refreshControls();
    }

    function setStatus(message) {
        if (controls.status) controls.status.textContent = message;
    }

    /**
     * The FDI selector, which is the intraoral editor's grid.
     *
     * Reused rather than reimplemented: the tinted, icon-bearing grid is how a clinician
     * finds a tooth on the other surface, and a second 32-button layout naming the same
     * teeth differently is a second thing to learn for no reason. It is rebuilt wholesale
     * on every redraw, which is what that module already does -- 32 buttons is small, and
     * keeping per-button DOM in step with counts and selection is four ways to disagree.
     */
    function refreshToothGrid() {
        if (!controls.teeth) return;
        renderToothGrid(
            controls.teeth,
            toothButtons({
                teeth: state.document,
                selected: state.selectedTooth || null,
                editable: state.canEdit,
                // The one thing that differs from segmentation: a tooth's badge counts
                // landmarks, not polygons.
                countFor: (document, code) => countForTooth(document, code),
            }),
            {
                onSelect: (code) => {
                    state.selectedTooth = state.selectedTooth === code ? '' : code;
                    state.selected = null;
                    redraw();
                },
                documentRef: doc,
            },
        );
    }

    function buildTypeButtons() {
        if (!controls.types) return;
        controls.types.innerHTML = '';
        for (const type of EDITABLE_TYPES) {
            const button = doc.createElement('button');
            button.type = 'button';
            button.className = 'ios-landmark-type';
            button.dataset.landmarkType = type;
            button.textContent = TYPE_LABELS[type] ?? type;
            button.style.setProperty('--landmark-color', cssColor(type));
            button.addEventListener('click', () => {
                state.selectedType = state.selectedType === type ? null : type;
                redraw();
            });
            controls.types.appendChild(button);
        }
    }

    /**
     * Per-type landmark visibility, in the visualization menu.
     *
     * One container, and it is a *viewer* control: which landmark types are drawn changes
     * what a reader sees and has nothing to do with holding annotation rights.
     */
    function buildVisibilityMenu() {
        const host = controls.visibilityTypes;
        if (!host) return;
        host.innerHTML = '';
        for (const type of LANDMARK_TYPES) {
            const item = doc.createElement('label');
            item.className = 'ios-landmark-vis-item';
            const box = doc.createElement('input');
            box.type = 'checkbox';
            box.className = 'ios-landmark-vis';
            box.dataset.landmarkType = type;
            box.checked = state.typeVisible[type] !== false;
            box.addEventListener('change', () => {
                state.typeVisible[type] = box.checked;
                redraw();
            });
            const dot = doc.createElement('span');
            dot.className = 'ios-landmark-vis-dot';
            dot.style.setProperty('--landmark-color', cssColor(type));
            item.append(box, dot, doc.createTextNode(TYPE_LABELS[type] ?? 'Planar'));
            host.appendChild(item);
        }
    }

    function refreshControls() {
        setSwitch(controls.landmarkMode, state.active);
        controls.landmarkMode?.setAttribute?.('aria-expanded', String(state.active));
        if (controls.workbench) controls.workbench.hidden = !state.active;
        // Annotating implies seeing: the switch reads on, and turning it off while the
        // workbench is open would leave a user placing landmarks they cannot see.
        setSwitch(controls.visibility, state.showLandmarks || state.active);
        setDisabled(controls.visibility, state.active);
        setPressed(controls.place, state.tool === 'place');
        setPressed(controls.select, state.tool === 'select');
        setDisabled(controls.undo, !canUndo(state.history));
        setDisabled(controls.redo, !canRedo(state.history));
        setDisabled(controls.delete, !state.selected || !state.canEdit);
        setDisabled(controls.save, !state.dirty || !state.canEdit);
        for (const button of controls.types?.querySelectorAll?.('.ios-landmark-type') ?? []) {
            setPressed(button, button.dataset.landmarkType === state.selectedType);
            setDisabled(button, !state.canEdit);
        }
        const visibility = viewport.jawVisibility();
        setPressed(controls.showUpper, visibility.upper);
        setEyeIcon(controls.showUpper, visibility.upper);
        setPressed(controls.showLower, visibility.lower);
        setEyeIcon(controls.showLower, visibility.lower);
        if (!state.dirty && controls.status?.textContent === UNSAVED_MESSAGE) {
            setStatus('');
        }
        if (state.active) {
            const instruction = instructionFor({
                canEdit: state.canEdit,
                tooth: state.selectedTooth,
                type: state.selectedType && (TYPE_LABELS[state.selectedType] ?? state.selectedType),
                active: state.active,
            });
            if (instruction && !state.dirty) setStatus(instruction);
        }
    }

    // ---------------------------------------------------------------- controls
    function bindControls() {
        onClick(controls.reset, () => applyCamera('reset'));
        onClick(controls.viewFront, () => applyCamera('front'));
        onClick(controls.viewRight, () => applyCamera('right'));
        onClick(controls.viewLeft, () => applyCamera('left'));
        onClick(controls.viewUpper, () => applyCamera('upper'));
        onClick(controls.viewLower, () => applyCamera('lower'));
        onClick(controls.showUpper, () => toggleJaw('upper'));
        onClick(controls.showLower, () => toggleJaw('lower'));

        onClick(controls.wireframe, () => {
            state.wireframe = !state.wireframe;
            viewport.setWireframe(state.wireframe);
            setPressed(controls.wireframe, state.wireframe);
        });
        onClick(controls.grid, () => {
            state.gridSize = state.gridSize ? 0 : DEFAULT_GRID_SIZE;
            paintGrid();
        });
        for (const button of controls.gridSizeButtons) {
            button.addEventListener('click', () => {
                state.gridSize = Number(button.dataset.gridSize) || DEFAULT_GRID_SIZE;
                paintGrid();
            });
        }
        // A checkbox, not a button: `change`, and applied once at mount so the viewport
        // agrees with the box the template shipped checked.
        if (controls.axis) {
            state.showAxis = controls.axis.checked !== false;
            viewport.setAxesVisible(state.showAxis);
            controls.axis.addEventListener('change', () => {
                state.showAxis = Boolean(controls.axis.checked);
                viewport.setAxesVisible(state.showAxis);
            });
        }
        // A checkbox, like the axes. White overrides the theme; unchecking returns the
        // viewport to whichever theme the page is in, rather than to a hardcoded dark --
        // which is what made this look broken in light mode even once it worked at all.
        if (controls.whiteBackground) {
            state.whiteBackground = Boolean(controls.whiteBackground.checked);
            controls.whiteBackground.addEventListener('change', () => {
                state.whiteBackground = Boolean(controls.whiteBackground.checked);
                applyBackground();
            });
        }
        applyBackground();
        watchTheme();

        onClick(controls.landmarkMode, () => {
            state.active = !state.active;
            state.selected = null;
            redraw();
        });
        onClick(controls.visibility, () => {
            state.showLandmarks = !state.showLandmarks;
            redraw();
        });

        onClick(controls.place, () => { state.tool = 'place'; redraw(); });
        onClick(controls.select, () => { state.tool = 'select'; redraw(); });
        onClick(controls.undo, () => step('undo'));
        onClick(controls.redo, () => step('redo'));
        onClick(controls.delete, () => deleteSelected());
        onClick(controls.save, () => save());

        controls.markerSize?.addEventListener?.('input', () => {
            state.markerSize = Number(controls.markerSize.value) || DEFAULT_MARKER_SIZE;
            if (controls.markerSizeValue) {
                controls.markerSizeValue.textContent = state.markerSize.toFixed(2);
            }
            redraw();
        });

        doc.addEventListener('keydown', onKeyDown);
        globalThis.addEventListener?.('resize', onResize);
    }

    /**
     * The background the page's theme asks for, unless White is ticked.
     *
     * `data-theme` is stamped on `<html>` before first paint by the inline script in
     * `base.html`, and the nav's toggle changes it live -- so this is read at apply time
     * and re-read on change rather than sampled once at mount.
     */
    function applyBackground() {
        if (state.whiteBackground) {
            viewport.setBackground('white');
            return;
        }
        const theme = doc.documentElement?.getAttribute?.('data-theme');
        viewport.setBackground(theme === 'light' ? 'light' : 'dark');
    }

    /**
     * Follow the theme toggle.
     *
     * The legacy viewer did this and it is worth keeping: a scan left on a dark canvas
     * inside a light page reads as a rendering failure rather than a preference.
     */
    function watchTheme() {
        const root = doc.documentElement;
        if (!root || typeof globalThis.MutationObserver !== 'function') return;
        themeObserver = new globalThis.MutationObserver(applyBackground);
        themeObserver.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    }

    function applyCamera(name) {
        viewport.setCamera(name);
        redraw();
    }

    function toggleJaw(jaw) {
        viewport.setJawVisible(jaw, !viewport.jawVisibility()[jaw]);
        redraw();
    }

    function paintGrid() {
        resizeOverlay(overlay, controls.viewport);
        overlay.style.display = state.gridSize ? 'block' : 'none';
        drawGrid(overlay, state.gridSize);
        setPressed(controls.grid, Boolean(state.gridSize));
    }

    function onResize() {
        viewport.resize();
        if (state.gridSize) paintGrid();
    }

    /**
     * Keyboard shortcuts, ignored while a form field has focus.
     *
     * The guard is the legacy behaviour and it matters: without it, typing a patient note
     * with the workbench open would delete landmarks on every backspace.
     */
    function onKeyDown(event) {
        const tag = doc.activeElement?.tagName;
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return;
        if (!state.active) return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
            event.preventDefault();
            step(event.shiftKey ? 'redo' : 'undo');
            return;
        }
        if (['Delete', 'Backspace'].includes(event.key)) {
            event.preventDefault();
            deleteSelected();
            return;
        }
        if (event.key === 'Escape') {
            state.selected = null;
            redraw();
        }
    }

    return {
        state,
        viewport,
        save,
        reload: reloadLandmarks,
        destroy() {
            themeObserver?.disconnect();
            doc.removeEventListener('keydown', onKeyDown);
            globalThis.removeEventListener?.('resize', onResize);
            viewport.destroy();
        },
        /** For tests: the document as it currently stands. */
        snapshot: () => cloneDocument(state.document),
    };
}
