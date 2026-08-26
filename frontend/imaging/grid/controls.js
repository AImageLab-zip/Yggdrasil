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
import { formatWindow } from './voi.js';

/** Element ids the template provides, and what each is for. */
export const CONTROL_IDS = Object.freeze({
    resetView: 'resetCBCTView',
    renderMode: 'cbctRenderMode',
    toggle3D: 'toggleCBCT3DOnly',
    reset3DCamera: 'resetCBCT3DCamera',
    exit3D: 'exitCBCT3DFocus',
    status: 'cbctRenderStatus',
    windowReadout: 'cbctWindowReadout',
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

    // 3D is lazy, so the first interaction with any 3D control has to bring it up.
    let renderReady = false;
    const ensureRender = async (mode) => {
        if (!renderReady) {
            setStatus('Loading 3D…');
            await grid.enable3DWindow(RENDER_WINDOW, mode);
            renderReady = true;
        }
        return renderReady;
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
            guarded('Render mode', async (event) => {
                const mode = event.target.value;
                await ensureRender(mode);
                const spec = grid.setRenderMode(RENDER_WINDOW, mode);
                setStatus(spec.label);
            })
        );
        plan.renderMode.dataset.initialMode = initial;
        bound.push('renderMode');
    }

    if (plan.toggle3D) {
        plan.toggle3D.addEventListener(
            'click',
            guarded('Load 3D', async () => {
                const mode = plan.renderMode?.value || DEFAULT_RENDER_MODE;
                await ensureRender(mode);
                plan.toggle3D.setAttribute('aria-pressed', 'true');
                if (plan.exit3D) {
                    plan.exit3D.hidden = false;
                }
                setStatus(`3D: ${mode}`);
            })
        );
        bound.push('toggle3D');
    }

    if (plan.reset3DCamera) {
        plan.reset3DCamera.addEventListener(
            'click',
            guarded('Reset 3D camera', () => {
                if (!renderReady) {
                    // Nothing to reset, and bringing 3D up because somebody asked to
                    // reset it would be the most expensive possible reading of a click.
                    setStatus('3D is not loaded.');
                    return;
                }
                grid.resetCameras(RENDER_WINDOW);
            })
        );
        bound.push('reset3DCamera');
    }

    if (plan.exit3D) {
        plan.exit3D.addEventListener(
            'click',
            guarded('Exit 3D', () => {
                plan.toggle3D?.setAttribute('aria-pressed', 'false');
                plan.exit3D.hidden = true;
                setStatus('');
            })
        );
        bound.push('exit3D');
    }

    return { bound, setStatus, plan };
}

/**
 * Keep the window/level readout in step with the viewport.
 *
 * This is what replaced the percent sliders (decision #5). It is a *readout*, not a
 * control: window/level is dragged on the image, and the number shown is in the
 * volume's own modality units with the unit named — or omitted, for CBCT, whose
 * greyscale is not calibrated Hounsfield.
 *
 * @param {object} options
 * @param {object} options.grid
 * @param {HTMLElement} options.element the `<output>` to write into.
 * @param {number} [options.windowIndex] which window to report.
 * @returns {() => void} call to refresh.
 */
export function windowReadout({ grid, element, windowIndex = 0 }) {
    return function refresh() {
        if (!element) {
            return;
        }
        let current = null;
        try {
            current = grid.readWindow(windowIndex);
        } catch {
            current = null;
        }
        // Blank rather than "W 0 / L 0" when there is nothing loaded: a zero window is
        // a real setting and would read as one.
        element.textContent = current ? formatWindow(current, current.unit) : '';
    };
}
