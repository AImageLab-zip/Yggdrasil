/**
 * The panoramic editor's controls, and the template ids they resolve.
 *
 * Separated from the bootstrap for the reason Phase 5 paid for: **a template id joining
 * two files is an untested interface.** `maxillo/tests_panoramic_surface.py` asserts the
 * ids below against the rendered page, so a rename there fails a test instead of leaving
 * this module holding `null` and a control stuck in whatever state the template shipped.
 *
 * Everything here is a pure function of a plan and a value. The bootstrap decides *when*;
 * this decides *what it looks like*.
 */

/** Every element the surface touches, by the id the template gives it. */
export const CONTROL_IDS = Object.freeze({
    root: 'cbctPanorexEditor',
    status: 'panorexEditorStatus',
    axialStage: 'panorexAxialStage',
    cprStage: 'panorexCprStage',
    resultCanvas: 'panorexResultCanvas',
    emptyResult: 'panorexEmptyResult',
    progress: 'panorexProgress',
    progressBar: 'panorexProgressBar',
    error: 'panorexEditorError',
    errorMessage: 'panorexEditorErrorMessage',
    retry: 'panorexRetry',
    save: 'panorexSave',
    zSlider: 'panorexZSlider',
    zValue: 'panorexZValue',
    prevZ: 'panorexPrevZ',
    nextZ: 'panorexNextZ',
    resetAuto: 'panorexResetAuto',
    savedViewer: 'cbctInlinePanoramic',
});

/** The projection buttons, which are keyed by data attribute rather than by id. */
export const MODE_SELECTOR = '[data-panorex-mode]';

/**
 * Resolve the plan once.
 *
 * A missing element is `null` and every setter tolerates it, because this surface renders
 * inside a patient page that must survive one section being absent.
 *
 * @param {Document} doc
 * @returns {object} the ids above, resolved, plus `modes`.
 */
export function controlPlan(doc) {
    const plan = {};
    for (const [name, id] of Object.entries(CONTROL_IDS)) {
        plan[name] = doc?.getElementById?.(id) ?? null;
    }
    plan.modes = Array.from(plan.root?.querySelectorAll?.(MODE_SELECTOR) ?? []);
    return plan;
}

export function setStatus(plan, text) {
    if (plan.status) {
        plan.status.textContent = text;
    }
}

/**
 * Show or clear the error banner.
 *
 * `retryable` is separate from "there is a message" because most failures are not: an arch
 * that cannot be fitted on this slice is answered by moving to another one, and a Retry
 * button that reruns the same thing is a button that does nothing twice.
 */
export function setError(plan, message, retryable = false) {
    if (plan.errorMessage) {
        plan.errorMessage.textContent = message || '';
    } else if (plan.error) {
        plan.error.textContent = message || '';
    }
    if (plan.error) {
        plan.error.hidden = !message;
    }
    if (plan.retry) {
        plan.retry.hidden = !message || !retryable;
    }
    if (message) {
        setStatus(plan, 'Generation failed');
    }
}

/** `null` hides the bar; anything else is a 0..1 fraction. */
export function setProgress(plan, value, label) {
    if (plan.progress) {
        plan.progress.hidden = value === null;
    }
    if (plan.progressBar && value !== null) {
        plan.progressBar.style.width = `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
    }
    if (label) {
        setStatus(plan, label);
    }
}

/** The slice readout and the slider, which are two owners of one number. */
export function setSlice(plan, z, depth) {
    if (plan.zSlider) {
        if (depth) {
            plan.zSlider.max = String(depth - 1);
        }
        plan.zSlider.value = String(z);
    }
    if (plan.zValue) {
        plan.zValue.textContent = String(z);
    }
}

/**
 * Which of the two panes is showing.
 *
 * The live reformat and the baked strip are different images -- see `cprViewport.js` -- so
 * only one is ever on screen, and which one says what the reader is being asked to judge:
 * the CPR while the arch is moving, the bake once it has settled and can be saved.
 */
export function showPane(plan, pane) {
    if (plan.cprStage) {
        plan.cprStage.hidden = pane !== 'live';
    }
    if (plan.resultCanvas) {
        plan.resultCanvas.hidden = pane !== 'baked';
    }
    if (plan.emptyResult) {
        plan.emptyResult.hidden = pane !== 'empty';
    }
}

/** Reflect the projection choice on the two buttons. */
export function setMode(plan, mode) {
    for (const button of plan.modes) {
        const active = button.dataset.panorexMode === mode;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
}

export function setSaveEnabled(plan, enabled) {
    if (plan.save) {
        plan.save.disabled = !enabled;
    }
}

/**
 * Show or hide the editor itself.
 *
 * The saved-panoramic card moves the other way: the two occupy the same place on the page,
 * and leaving both up shows the reader a strip and an editor claiming to produce it.
 */
export function setEditorVisible(plan, visible) {
    if (plan.root) {
        plan.root.hidden = !visible;
    }
    if (plan.savedViewer) {
        plan.savedViewer.hidden = visible;
    }
}

/**
 * Whether this patient's arch may be edited at all.
 *
 * The Edit button is not rendered for a locked patient; this guards the *programmatic*
 * entry point, which the warm-up harness and the console both reach.
 */
export function isLocked(plan) {
    return plan.root?.dataset?.panoramicLocked === 'true';
}

/** Whether the page grants this user the editor at all. */
export function canEdit(plan) {
    return plan.root?.dataset?.canEdit === 'true';
}
