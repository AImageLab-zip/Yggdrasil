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
 *
 * **Annotation mode is one switch with two effects, and the DOM holds its state.** It
 * reveals the measurement tools *and* shows the measurements, because two controls meant
 * two states that could disagree -- which is how "I clicked show and saw nothing" got
 * reported. And the current state is read back off `aria-checked` at click time rather
 * than kept in a closure variable: a variable that starts out disagreeing with the
 * rendered markup inverts every click after it, which is the other half of the same bug.
 */

import { NAVIGATION_TOOL } from './measurements.js';

/** Element ids the template provides, and what each is for. */
export const CONTROL_IDS = Object.freeze({
    resetView: 'resetCBCTView',
    save: 'cbctSaveMeasurements',
    clear: 'cbctClearMeasurements',
    expand3D: 'cbctExpand3D',
    annotationMode: 'cbctAnnotationMode',
    annotationTools: 'cbctAnnotationTools',
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

/** What a clean save says. No count: it is a number nobody acts on. */
export const SAVED_MESSAGE = 'Measurements saved.';

/**
 * What clearing asks, and what it says afterwards.
 *
 * Both name the save, because a clear is *not* persisted on its own: the server's
 * revisions are replace-the-whole-set, so the next save is what makes it permanent and
 * a reload is what undoes it. Saying so is the difference between a reversible action
 * and one a user thinks was reversible.
 */
export const CLEAR_CONFIRM =
    'Remove every measurement drawn on this study? They come back if you reload without saving.';
export const CLEARED_MESSAGE = 'Measurements removed. Save to make it permanent.';

/**
 * The platform's toasts, when there are any.
 *
 * `static/js/notifications.js` installs `window.appNotify` sitewide from `base.html`.
 * It is reached through the global rather than imported because it is a classic script
 * and this is a module -- and if it is somehow absent, a missing toast must not cost
 * the save.
 */
function defaultNotify(level, message) {
    const notify = globalThis.appNotify;
    if (typeof notify === 'function') {
        notify(level, message);
    } else {
        console.info(`[ygg-grid] ${level}: ${message}`);
    }
}

/**
 * Whether annotation mode is currently on, according to the DOM.
 *
 * @param {object} plan
 * @returns {boolean}
 */
export function isAnnotationModeOn(plan) {
    return plan?.annotationMode?.getAttribute?.('aria-checked') === 'true';
}

/**
 * Render annotation mode: the switch, its word, and the tools it reveals.
 *
 * Split out from the click handler so the bind-time default and every later toggle go
 * through exactly one piece of code. `hidden` rather than a class, so the state lives
 * where the switch's `aria-controls` points.
 *
 * @param {object} options
 * @param {object} options.plan
 * @param {boolean} options.enabled
 */
export function applyAnnotationMode({ plan, enabled }) {
    const on = Boolean(enabled);
    if (plan.annotationMode) {
        plan.annotationMode.setAttribute('aria-checked', String(on));
        const state = plan.annotationMode.querySelector?.('[data-mode-state]');
        if (state) {
            state.textContent = on ? 'on' : 'off';
        }
    }
    if (plan.annotationTools) {
        plan.annotationTools.hidden = !on;
    }
}

/**
 * Bind the toolbar to a grid.
 *
 * @param {object} options
 * @param {object} options.grid the handle from `createVolumeGrid`.
 * @param {Document} [options.doc]
 * @param {() => Promise<{level?: string, message?: string}>} [options.onSave] invoked by
 *   the save button; whatever it returns is toasted.
 * @param {() => Promise<{level?: string, message?: string}>} [options.onClear] invoked by
 *   the clear button, after the user confirms; whatever it returns is toasted.
 * @param {(enabled: boolean) => void} [options.onAnnotationMode] told whenever the mode
 *   changes, and once at bind time with the starting state.
 * @param {boolean} [options.annotationsOn] the starting state. False, deliberately.
 * @param {(level: string, message: string) => void} [options.notify]
 * @returns {{bound: string[], setStatus: Function, plan: object, setAnnotationMode: Function}}
 */
export function bindControls({
    grid,
    doc = globalThis.document,
    onSave,
    onClear,
    onAnnotationMode,
    annotationsOn = false,
    notify = defaultNotify,
}) {
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

    if (plan.annotationMode) {
        // Assert the default rather than trusting the template to have rendered it: the
        // grid is told too, so the two cannot start out disagreeing. Off is the default
        // -- a clinician opening a study is reading it, not measuring it.
        const start = annotationsOn === true;
        applyAnnotationMode({ plan, enabled: start });
        onAnnotationMode?.(start);

        plan.annotationMode.addEventListener(
            'click',
            guarded('Annotation mode', () => {
                // Read the state back from the DOM. See the module note.
                const enabled = !isAnnotationModeOn(plan);
                applyAnnotationMode({ plan, enabled });
                // The grid does the rest, and does it in one call: which tools have a
                // mode, which annotations are visible, and -- switching off -- putting
                // the crosshair back on the left button. The order between those is not
                // free (see `setAnnotationMode`), which is exactly why it is not
                // sequenced from here.
                onAnnotationMode?.(enabled);
                if (!enabled) {
                    markActiveTool(plan.tools, NAVIGATION_TOOL);
                }
                setStatus('');
            })
        );
        bound.push('annotationMode');
    }

    if (plan.clear && onClear) {
        plan.clear.addEventListener('click', async () => {
            // Confirmed, because it destroys visible work and the only undo is a reload
            // -- and only until the next save. `window.confirm` is what the rest of the
            // page uses for this (`static/js/patient_detail.js`); a bespoke modal here
            // would be a second convention for the same question.
            const view = plan.clear.ownerDocument?.defaultView ?? globalThis;
            if (view.confirm && !view.confirm(CLEAR_CONFIRM)) {
                return;
            }
            try {
                const result = await onClear();
                notify(result?.level ?? 'success', result?.message ?? CLEARED_MESSAGE);
            } catch (error) {
                notify('danger', `Could not clear the measurements: ${error.message}`);
            }
        });
        bound.push('clear');
    }

    if (plan.save && onSave) {
        plan.save.addEventListener('click', async () => {
            // Not `guarded`: a save reports through the platform's toasts, because
            // "Saved 3 measurements." in a toolbar is a sentence nobody reads. The
            // status line is for failures that are about the toolbar itself.
            plan.save.setAttribute('aria-busy', 'true');
            try {
                const result = await onSave();
                notify(result?.level ?? 'success', result?.message ?? SAVED_MESSAGE);
            } catch (error) {
                notify('danger', `The save failed: ${error.message}`);
            } finally {
                plan.save.removeAttribute('aria-busy');
            }
        });
        bound.push('save');
    }

    /** Move the mode from outside the toolbar, keeping the switch and the grid in step. */
    const setAnnotationMode = (enabled) => {
        applyAnnotationMode({ plan, enabled });
        onAnnotationMode?.(enabled);
    };

    return { bound, setStatus, plan, setAnnotationMode };
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
