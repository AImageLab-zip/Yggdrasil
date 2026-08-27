/**
 * The per-viewport overlay: panel name, orientation letters, slice counter.
 *
 * Everything anatomical here comes from **Cornerstone's own utilities**, not from a
 * table of our own. `getOrientationStringLPS` and `invertOrientationStringLPS`
 * (`@cornerstonejs/tools/utilities/orientation`) are what OHIF uses for exactly this,
 * and they take the live camera -- so the letters stay correct if a viewport is ever
 * rotated or flipped, which a hard-coded per-plane table would not.
 *
 * They are passed in rather than imported so this module loads under `node --test`.
 * The tests inject the real ones from `node_modules`, so what is verified is
 * Cornerstone's behaviour and not a paraphrase of it.
 *
 * One deliberate translation. Cornerstone reports the superior/inferior axis as
 * **H**ead and **F**oot; radiology, and the reference this was built against, show
 * **S** and **I**. {@link DISPLAY_LETTERS} is that swap, and it is the only place any
 * letter is decided by us.
 */

/** Cornerstone's head/foot letters, in the superior/inferior spelling clinicians read. */
export const DISPLAY_LETTERS = Object.freeze({ H: 'S', F: 'I' });

/** Short panel names, matching the reference layout. */
export const PANEL_LABELS = Object.freeze({
    axial: 'Ax',
    sagittal: 'Sag',
    coronal: 'Cor',
    render: '3D',
});

/** Class names the stylesheet keys off. */
export const OVERLAY_CLASSES = Object.freeze({
    root: 'ygg-overlay',
    panel: 'ygg-overlay__panel',
    slice: 'ygg-overlay__slice',
    window: 'ygg-overlay__window',
    edge: 'ygg-overlay__edge',
});

/**
 * The screen-right vector of a camera, in patient LPS.
 *
 * `cross(viewUp, viewPlaneNormal)`, verified against Cornerstone's own
 * `MPR_CAMERA_VALUES`: it reproduces the `viewRight` that file states for all three
 * planes. Derived rather than read off the constant because `getCamera()` does not
 * always carry `viewRight`, and because a rotated viewport has one that no constant
 * knows.
 *
 * @param {{viewUp: number[], viewPlaneNormal: number[]}} camera
 * @returns {number[]}
 */
export function viewRightFrom({ viewUp, viewPlaneNormal }) {
    return [
        viewUp[1] * viewPlaneNormal[2] - viewUp[2] * viewPlaneNormal[1],
        viewUp[2] * viewPlaneNormal[0] - viewUp[0] * viewPlaneNormal[2],
        viewUp[0] * viewPlaneNormal[1] - viewUp[1] * viewPlaneNormal[0],
        // `+ 0` turns -0 into 0. A cross product of axis-aligned vectors produces
        // negative zeros, and while `-0 < 0` is false -- so the letters happen to come
        // out right -- carrying a signed zero into anatomy code is a trap waiting for
        // the first comparison that does not.
    ].map((component) => component + 0);
}

/**
 * Translate Cornerstone's letters into the ones a clinician reads.
 *
 * @param {string} orientation e.g. `'H'`, `'RH'`.
 * @returns {string}
 */
export function toDisplayLetters(orientation) {
    return String(orientation || '')
        .split('')
        .map((letter) => DISPLAY_LETTERS[letter] ?? letter)
        .join('');
}

/**
 * The four edge letters for a viewport, from its live camera.
 *
 * @param {object} camera from `viewport.getCamera()`.
 * @param {object} utilities Cornerstone's orientation helpers.
 * @param {(v: number[]) => string} utilities.getOrientationStringLPS
 * @param {(s: string) => string} utilities.invertOrientationStringLPS
 * @returns {{top: string, bottom: string, left: string, right: string}}
 */
export function edgeLabels(camera, { getOrientationStringLPS, invertOrientationStringLPS }) {
    if (!camera?.viewUp || !camera?.viewPlaneNormal) {
        return { top: '', bottom: '', left: '', right: '' };
    }
    const right = getOrientationStringLPS(viewRightFrom(camera));
    const top = getOrientationStringLPS(camera.viewUp);
    return {
        right: toDisplayLetters(right),
        left: toDisplayLetters(invertOrientationStringLPS(right)),
        top: toDisplayLetters(top),
        bottom: toDisplayLetters(invertOrientationStringLPS(top)),
    };
}

/**
 * The slice counter text, one-based for display.
 *
 * `getSliceIndex()` is zero-based; every viewer in the world shows slice 1 as "1".
 * Returns the empty string when there is nothing to count rather than "1 / 0", which
 * reads as a loaded volume with no slices.
 *
 * @param {number} index zero-based.
 * @param {number} count
 * @returns {string}
 */
export function sliceText(index, count) {
    if (!Number.isFinite(index) || !Number.isFinite(count) || count <= 0) {
        return '';
    }
    return `${Math.round(index) + 1} / ${Math.round(count)}`;
}

/**
 * Build the overlay DOM for one viewport window.
 *
 * Absolutely positioned and `pointer-events: none` (see the stylesheet): the overlay
 * sits over the canvas and must never eat a drag meant for the image.
 *
 * @param {HTMLElement} element the `.viewer-window`.
 * @param {object} options
 * @param {string} options.orientation
 * @returns {object} node handles for {@link updateOverlay}.
 */
export function createOverlay(element, { orientation }) {
    const doc = element.ownerDocument;
    const existing = element.querySelector(`.${OVERLAY_CLASSES.root}`);
    if (existing) {
        existing.remove();
    }

    const root = doc.createElement('div');
    root.className = OVERLAY_CLASSES.root;

    const make = (className, text = '') => {
        const node = doc.createElement('div');
        node.className = className;
        node.textContent = text;
        root.appendChild(node);
        return node;
    };

    const nodes = {
        root,
        panel: make(OVERLAY_CLASSES.panel, PANEL_LABELS[orientation] ?? ''),
        slice: make(OVERLAY_CLASSES.slice),
        window: make(OVERLAY_CLASSES.window),
        edges: {},
    };
    for (const edge of ['top', 'bottom', 'left', 'right']) {
        nodes.edges[edge] = make(`${OVERLAY_CLASSES.edge} ${OVERLAY_CLASSES.edge}--${edge}`);
    }

    element.appendChild(root);
    return nodes;
}

/**
 * Refresh an overlay from the viewport's current state.
 *
 * @param {object} nodes from {@link createOverlay}.
 * @param {object} state
 * @param {object} [state.camera]
 * @param {number} [state.sliceIndex]
 * @param {number} [state.sliceCount]
 * @param {string} [state.windowText] the window/level readout.
 * @param {object} state.utilities Cornerstone's orientation helpers.
 */
export function updateOverlay(nodes, { camera, sliceIndex, sliceCount, windowText, utilities }) {
    if (!nodes) {
        return;
    }
    const labels = edgeLabels(camera, utilities);
    for (const edge of ['top', 'bottom', 'left', 'right']) {
        nodes.edges[edge].textContent = labels[edge];
    }
    nodes.slice.textContent = sliceText(sliceIndex, sliceCount);
    nodes.window.textContent = windowText || '';
}
