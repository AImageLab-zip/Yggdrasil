/**
 * The CBCT toolbar, bound to the grid.
 *
 * `templates/maxillo/patient_detail_content.html` carries seven controls that
 * `maxillo_cbct_grid_adapter.js` used to wire. Deleting that file without replacing
 * this leaves a toolbar of buttons that do nothing — the failure a user notices first
 * and reports as "the viewer is broken", with nothing in the console to go on.
 *
 * Every binding here is **optional and independently guarded**. The toolbar is rendered
 * by one template and the grid appears in two namespaces, so a control that exists on
 * the maxillo page does not exist on the brain one; and a handler that throws must not
 * take its neighbours with it. So each is bound only if present, and each runs inside a
 * reporter that puts the failure in the status line rather than only in the console.
 *
 * {@link controlPlan} is the decision — which controls exist, what each does — and is
 * pure. The binding below is the mechanical part.
 */

import { DEFAULT_RENDER_MODE, RENDER_MODES } from './renderModes.js';

/** Element ids the template provides, and what each is for. */
export const CONTROL_IDS = Object.freeze({
    resetView: 'resetCBCTView',
    renderMode: 'cbctRenderMode',
    reset3DCamera: 'resetCBCT3DCamera',
    status: 'cbctRenderStatus',
});

/** The grid window the 3D render occupies, per `FIXED_CBCT_LAYOUT`. */
export const RENDER_WINDOW = 3;

/**
 * Which controls this page actually has.
 *
 * Separated from the binding so "the brain page has no render-mode select" is a fact a
 * test can assert, rather than something that only shows up as a null dereference on
 * one of two pages.
 *
 * @param {Document} doc
 * @returns {Record<string, HTMLElement|null>}
 */
export function controlPlan(doc) {
    const plan = {};
    for (const [name, id] of Object.entries(CONTROL_IDS)) {
        plan[name] = doc.getElementById(id);
    }
    return plan;
}

/**
 * Bind the toolbar to a grid.
 *
 * @param {object} options
 * @param {object} options.grid the handle from `createVolumeGrid`.
 * @param {Document} [options.doc]
 * @returns {{bound: string[], setStatus: (message: string) => void}}
 */
export function bindControls({ grid, doc = globalThis.document }) {
    const plan = controlPlan(doc);
    const bound = [];

    const setStatus = (message) => {
        if (plan.status) {
            plan.status.textContent = message || '';
        }
    };

    /** Run a handler, reporting any failure where a user can see it. */
    const guarded = (label, handler) => async (event) => {
        try {
            await handler(event);
        } catch (error) {
            setStatus(`${label} failed: ${error.message}`);
        }
    };


    if (plan.resetView) {
        plan.resetView.addEventListener(
            'click',
            guarded('Reset view', () => {
                grid.resetCameras();
                setStatus('');
            })
        );
        bound.push('resetView');
    }

    if (plan.renderMode) {
        // Reflect the template's default rather than assuming it: the `selected`
        // attribute is what decides which mode a clinician actually sees, and it lives
        // in the template.
        const initial = RENDER_MODES.includes(plan.renderMode.value)
            ? plan.renderMode.value
            : DEFAULT_RENDER_MODE;

        plan.renderMode.addEventListener(
            'change',
            guarded('Render mode', (event) => {
                // The 3D window is built with the rest of the grid now, so there is
                // nothing to bring up first.
                const spec = grid.setRenderMode(RENDER_WINDOW, event.target.value);
                setStatus(spec.label);
            })
        );
        plan.renderMode.dataset.initialMode = initial;
        bound.push('renderMode');
    }

    if (plan.reset3DCamera) {
        // Now that the render is always there, this resets it -- which is what the
        // button always claimed to do.
        plan.reset3DCamera.addEventListener(
            'click',
            guarded('Reset 3D camera', () => {
                grid.resetCameras(RENDER_WINDOW);
                setStatus('');
            })
        );
        bound.push('reset3DCamera');
    }

    return { bound, setStatus, plan };
}

/*
 * The window/level readout used to live here, writing into a toolbar `<output>`. It
 * moved into each viewport's own overlay (`viewportOverlay.js`): the number belongs
 * next to the image it describes, and as a lone `<output>` in a toolbar it looked like
 * exactly what it was -- a slider's label with the slider taken away.
 */
