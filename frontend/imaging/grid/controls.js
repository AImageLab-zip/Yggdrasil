/**
 * The CBCT toolbar, bound to the grid.
 *
 * `maxillo_cbct_grid_adapter.js` used to wire these; deleting it without replacing this
 * leaves a toolbar of buttons that do nothing — the failure a user notices first and
 * reports as "the viewer is broken", with nothing in the console to go on.
 *
 * Every binding is **optional and independently guarded**. The toolbar is rendered by
 * one template and the grid appears in two namespaces, so a control that exists on the
 * maxillo page does not exist on the brain one; and a handler that throws must not take
 * its neighbours with it. Each is bound only if present, and each runs inside a
 * reporter that puts the failure in the status line rather than only in the console.
 *
 * The measurement tools are **discovered from the DOM**, not listed here. Each button
 * carries `data-ygg-tool="<Cornerstone tool name>"`, so a tool can be added to or
 * removed from the toolbar by editing the template alone. Hard-coding the list in two
 * places is how a button ends up bound to a tool that was never registered.
 */

/** Element ids the template provides, and what each is for. */
export const CONTROL_IDS = Object.freeze({
    resetView: 'resetCBCTView',
    save: 'cbctSaveMeasurements',
    expand3D: 'cbctExpand3D',
    toggleAnnotations: 'cbctToggleAnnotations',
    status: 'cbctRenderStatus',
});

/** Class on the grid container while the 3D view fills it. */
export const EXPANDED_CLASS = 'is-3d-expanded';

/** Selector for the measurement-tool buttons. */
export const TOOL_BUTTON_SELECTOR = '[data-ygg-tool]';

/** The grid window the 3D render occupies, per `FIXED_CBCT_LAYOUT`. */
export const RENDER_WINDOW = 3;

/**
 * Which controls this page actually has.
 *
 * @param {Document} doc
 * @returns {object}
 */
export function controlPlan(doc) {
    const plan = {};
    for (const [name, id] of Object.entries(CONTROL_IDS)) {
        plan[name] = doc.getElementById(id);
    }
    plan.tools = Array.from(doc.querySelectorAll(TOOL_BUTTON_SELECTOR));
    return plan;
}

/**
 * Show which tool is active.
 *
 * `aria-pressed` as well as a class: these are toggle buttons in a group, and a screen
 * reader has to be able to say which one is on.
 *
 * @param {HTMLElement[]} buttons
 * @param {string} toolName
 */
export function markActiveTool(buttons, toolName) {
    for (const button of buttons) {
        const isActive = button.dataset.yggTool === toolName;
        button.setAttribute('aria-pressed', String(isActive));
        button.classList?.toggle('is-active', isActive);
    }
}

/**
 * Bind the toolbar to a grid.
 *
 * @param {object} options
 * @param {object} options.grid the handle from `createVolumeGrid`.
 * @param {Document} [options.doc]
 * @param {() => Promise<object>} [options.onSave] invoked by the save button.
 * @returns {{bound: string[], setStatus: (message: string) => void, plan: object}}
 */
export function bindControls({ grid, doc = globalThis.document, onSave, onToggleAnnotations }) {
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

    for (const button of plan.tools) {
        const toolName = button.dataset.yggTool;
        button.addEventListener(
            'click',
            guarded(toolName, () => {
                grid.setPrimaryTool(toolName);
                markActiveTool(plan.tools, toolName);
                setStatus('');
            })
        );
    }
    if (plan.tools.length) {
        bound.push('tools');
    }

    if (plan.expand3D) {
        // Hides the three slice views and gives their space to the render. The grid is
        // a CSS grid, so this is a class on the container -- and Cornerstone has to be
        // told afterwards, because a viewport whose element changed size renders at the
        // old size until `resize()` says otherwise.
        plan.expand3D.addEventListener(
            'click',
            guarded('Expand 3D', () => {
                const container = plan.expand3D.ownerDocument.querySelector('.viewer-grid');
                const expanded = container?.classList.toggle(EXPANDED_CLASS) ?? false;
                plan.expand3D.setAttribute('aria-pressed', String(expanded));
                grid.resize?.();
            })
        );
        bound.push('expand3D');
    }

    if (plan.toggleAnnotations && onToggleAnnotations) {
        // Visibility, not deletion: the measurements stay in Cornerstone's state, so
        // hiding them cannot lose work and a save while hidden still writes them all.
        let visible = true;
        plan.toggleAnnotations.addEventListener(
            'click',
            guarded('Show measurements', () => {
                visible = !visible;
                onToggleAnnotations(visible);
                plan.toggleAnnotations.setAttribute('aria-pressed', String(visible));
                plan.toggleAnnotations.classList?.toggle('is-active', visible);
            })
        );
        bound.push('toggleAnnotations');
    }

    if (plan.save && onSave) {
        plan.save.addEventListener(
            'click',
            guarded('Save', async () => {
                setStatus('Saving…');
                const result = await onSave();
                setStatus(result?.message ?? 'Saved.');
            })
        );
        bound.push('save');
    }

    return { bound, setStatus, plan };
}

/**
 * A loading indicator for one grid window.
 *
 * A CBCT is tens of millions of voxels and takes real seconds to arrive. Without this
 * the grid is simply black for that whole time, which is indistinguishable from a
 * viewer that has failed — and was reported as exactly that.
 *
 * The element is removed on completion rather than hidden: a spinner that is merely
 * hidden is a spinner that can be left behind by an early return.
 *
 * @param {HTMLElement} element the `.viewer-window`.
 * @returns {{update: (fraction: number|null, label?: string) => void, done: () => void}}
 */
export function loadingIndicator(element) {
    const doc = element?.ownerDocument;
    if (!doc) {
        return { update: () => {}, done: () => {} };
    }

    const root = doc.createElement('div');
    root.className = 'ygg-loading';
    root.setAttribute('role', 'status');
    root.setAttribute('aria-live', 'polite');

    const spinner = doc.createElement('div');
    spinner.className = 'ygg-loading__spinner';
    root.appendChild(spinner);

    const text = doc.createElement('div');
    text.className = 'ygg-loading__text';
    text.textContent = 'Loading volume…';
    root.appendChild(text);

    element.appendChild(root);

    return {
        update(fraction, label) {
            if (label) {
                text.textContent = label;
            } else if (Number.isFinite(fraction)) {
                // Clamped: a progress event that overshoots would read as "103%".
                const percent = Math.min(100, Math.max(0, Math.round(fraction * 100)));
                text.textContent = `Loading volume… ${percent}%`;
            }
        },
        done() {
            root.remove();
        },
    };
}
