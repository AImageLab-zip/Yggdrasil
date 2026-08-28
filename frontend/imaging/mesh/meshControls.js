/**
 * The IOS toolbar and landmark workbench: DOM in, a resolved plan out.
 *
 * Same contract as `photos/controls.js` -- the ids are declared here rather than hunted
 * for in the template, and a missing control degrades the toolbar instead of throwing.
 *
 * That second part is a fix, not a convention. `ios.js:793-814` did unguarded
 * `document.getElementById('toggleLandmarkMode').addEventListener(...)`, while the element
 * lived behind `{% if 'ios_landmarks' in allowed_annotations %}`. A project with
 * annotations enabled but landmarks switched off therefore threw here and took the rest of
 * the 3D controls down with it -- reset, wireframe, grid and all seven camera buttons, on
 * a page where nothing said why.
 *
 * The ids are also the interface Phase 5 learned to test: `maxillo/tests_ios_surface.py`
 * asserts the template still carries every one of them, because a rename leaves this
 * module holding `null` and the control stuck in whatever state the template shipped --
 * silently, and only on the surface being replaced.
 */

/** Every element this surface binds, by role. */
export const MESH_CONTROL_IDS = Object.freeze({
    // The viewport and its stage.
    viewport: 'scan-viewer',
    container: 'ios-viewer',

    // View controls.
    reset: 'resetView',
    wireframe: 'toggleWireframe',
    grid: 'toggleGrid',
    showUpper: 'showUpper',
    showLower: 'showLower',
    viewRight: 'viewRight',
    viewLeft: 'viewLeft',
    viewFront: 'viewFront',
    viewUpper: 'viewUpper',
    viewLower: 'viewLower',

    // The landmark workbench.
    landmarkMode: 'toggleLandmarkMode',
    workbench: 'iosLandmarkWorkbench',
    status: 'iosLandmarkStatus',
    teeth: 'iosLandmarkTeeth',
    types: 'iosLandmarkTypes',
    visibility: 'toggleLandmarkVisibility',
    visualizationMenu: 'iosVisualizationMenu',
    visibilityTypes: 'iosLandmarkVisibilityWorkbench',
    place: 'landmarkPlaceTool',
    select: 'landmarkSelectTool',
    undo: 'undoLandmark',
    redo: 'redoLandmark',
    delete: 'deleteLandmark',
    save: 'saveLandmarks',
    markerSize: 'landmarkSizeRange',
    markerSizeValue: 'landmarkSizeValue',
    axis: 'toggleAxis',
    whiteBackground: 'toggleWhiteBackground',
});

/** Grid-size buttons carry their value in a data attribute rather than an inline handler. */
export const GRID_SIZE_SELECTOR = '[data-grid-size]';

export const TOOTH_SELECTOR = '.ios-landmark-tooth';
export const TYPE_SELECTOR = '.ios-landmark-type';
export const VISIBILITY_SELECTOR = '.ios-landmark-vis';

export const UNSAVED_MESSAGE = 'Unsaved changes';
export const SAVED_MESSAGE = 'Saved';
export const CONFLICT_MESSAGE =
    'Somebody else saved landmarks for this patient while you were working. ' +
    'Reload to see their changes; your unsaved points will be lost.';

/**
 * Resolve the toolbar.
 *
 * @param {Document} doc
 * @returns {object} `{[role]: Element|null}` plus `gridSizeButtons`.
 */
export function controlPlan(doc) {
    const plan = {};
    for (const [role, id] of Object.entries(MESH_CONTROL_IDS)) {
        plan[role] = doc?.getElementById?.(id) ?? null;
    }
    plan.gridSizeButtons = Array.from(doc?.querySelectorAll?.(GRID_SIZE_SELECTOR) ?? []);
    return plan;
}

/**
 * What the workbench should say right now.
 *
 * One function so the instruction line cannot disagree with what a click will actually do.
 * The legacy version computed this in three places and they had drifted -- the status line
 * still said "Shift + left-click to place" for `planar`, which is read-only.
 */
export function instructionFor({ canEdit, tooth, type, active }) {
    if (!active) return '';
    if (!canEdit) return 'Viewing saved landmarks';
    if (!tooth) return 'Select an FDI tooth';
    if (!type) return `Tooth ${tooth} · Select a landmark type`;
    return `Tooth ${tooth} · ${type} · Shift + left-click to place`;
}

/**
 * Bind a click, if the element is there.
 *
 * The guard is the point of this helper: see the module header for what its absence cost.
 */
export function onClick(element, handler) {
    element?.addEventListener?.('click', handler);
}

/** Set `.active` and `aria-pressed` together, so the two cannot disagree. */
export function setPressed(element, pressed) {
    if (!element) return;
    element.classList?.toggle?.('active', Boolean(pressed));
    element.setAttribute?.('aria-pressed', String(Boolean(pressed)));
}

/**
 * Swap an eye icon between open and slashed.
 *
 * Carried over from `updateButtonStates` (`ios.js:1234-1264`), which did it inline for two
 * buttons and would have done it inline for a third.
 */
export function setEyeIcon(element, visible) {
    const icon = element?.querySelector?.('i');
    if (!icon) return;
    icon.classList.toggle('fa-eye', Boolean(visible));
    icon.classList.toggle('fa-eye-slash', !visible);
}

export function setDisabled(element, disabled) {
    if (element) element.disabled = Boolean(disabled);
}


/**
 * Drive a `role="switch"` control.
 *
 * The toolbar's own component (`.cbct-mode-toggle`), shared with the measurement surfaces:
 * `aria-checked` carries the state and a visible on/off word repeats it, because "is this
 * eye icon telling me the state or the action?" is the ambiguity that got reported on the
 * icon button this replaced.
 *
 * The state is read back out of the DOM at click time rather than held in a closure --
 * that is how the volume grid's switch came to invert itself after the first click.
 */
export function setSwitch(element, on) {
    if (!element) return;
    element.setAttribute?.('aria-checked', String(Boolean(on)));
    element.classList?.toggle?.('is-on', Boolean(on));
    const state = element.querySelector?.('[data-mode-state]');
    if (state) state.textContent = on ? 'on' : 'off';
}

/** Whether a switch is currently on, from the DOM. */
export function isSwitchOn(element) {
    return element?.getAttribute?.('aria-checked') === 'true';
}
