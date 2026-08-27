/**
 * The photo stack's toolbar: DOM in, callbacks out.
 *
 * Same contract as `grid/controls.js` -- the ids are declared here rather than hunted for
 * in the template, a missing control is a degraded toolbar rather than a thrown error, and
 * the annotation mode's state is read back out of `aria-checked` at click time rather than
 * held in a closure. That last one is not fastidiousness: holding it in a closure is how
 * the grid's switch came to invert itself after the first click.
 */

/** Element ids the template must provide. A missing one disables its feature only. */
export const PHOTO_CONTROL_IDS = Object.freeze({
    viewport: 'photoViewport',
    strip: 'photoStrip',
    prev: 'photoPrev',
    next: 'photoNext',
    counter: 'photoCounter',
    calibrate: 'photoCalibrate',
    calibration: 'photoCalibration',
    edit: 'photoEditImage',
    save: 'photoSaveMeasurements',
    clear: 'photoClearMeasurements',
    annotationMode: 'photoAnnotationMode',
    annotationTools: 'photoAnnotationTools',
    status: 'photoStatus',
});

/** Selector for the measurement-tool buttons, matching the grid's convention. */
export const TOOL_BUTTON_SELECTOR = '[data-ygg-tool]';

export const SAVED_MESSAGE = 'Measurements saved.';
export const CLEAR_CONFIRM =
    'Remove every measurement on this image? Save afterwards to make it permanent; ' +
    'reload to undo it.';
export const CLEARED_MESSAGE = 'Measurements removed. Save to make it permanent.';

/**
 * Resolve the toolbar once.
 *
 * @param {Document} doc
 * @returns {object} `{[key]: Element|null}` plus `toolButtons`.
 */
export function controlPlan(doc) {
    const plan = {};
    for (const [key, id] of Object.entries(PHOTO_CONTROL_IDS)) {
        plan[key] = doc?.getElementById?.(id) ?? null;
    }
    plan.toolButtons = Array.from(doc?.querySelectorAll?.(TOOL_BUTTON_SELECTOR) ?? []);
    return plan;
}

/**
 * Is annotation mode on?
 *
 * Read from the DOM, never from a variable. The switch is the only thing that knows, and
 * a second copy of the answer is a second thing that can be wrong.
 */
export function isAnnotationModeOn(plan) {
    return plan.annotationMode?.getAttribute?.('aria-checked') === 'true';
}

/**
 * Reflect annotation mode into the toolbar.
 *
 * @param {object} options
 * @param {object} options.plan
 * @param {boolean} options.enabled
 */
export function applyAnnotationMode({ plan, enabled }) {
    plan.annotationMode?.setAttribute?.('aria-checked', enabled ? 'true' : 'false');
    if (plan.annotationTools) {
        if (enabled) {
            plan.annotationTools.removeAttribute?.('hidden');
        } else {
            plan.annotationTools.setAttribute?.('hidden', '');
        }
    }
    // The word beside the switch, so the state is readable without inspecting the
    // control -- an icon-only toggle leaves a clinician guessing which way is on. Same
    // hook the grid uses (`grid/controls.js`), so the two toolbars share one convention.
    if (plan.annotationMode) {
        const state = plan.annotationMode.querySelector?.('[data-mode-state]');
        if (state) {
            state.textContent = enabled ? 'on' : 'off';
        }
    }
}

/** Mark one tool button active, clearing the others. */
export function markActiveTool(buttons, toolName) {
    for (const button of buttons) {
        const isActive = button.dataset?.yggTool === toolName;
        button.classList?.toggle?.('active', isActive);
        button.setAttribute?.('aria-pressed', isActive ? 'true' : 'false');
    }
}

/** `n of m`, or empty for an empty stack. */
export function formatCounter(index, total) {
    return total ? `${index + 1} of ${total}` : '';
}

/**
 * Wire the toolbar.
 *
 * Every handler is optional; a template that omits a control simply has no handler for
 * it. `notify` is injected so the module needs no global, and defaults to the platform's
 * toast (`window.appNotify`) with the toolbar status line as the fallback.
 *
 * @param {object} options
 * @returns {object} `{setAnnotationMode, setCounter, setCalibration, setBusy}`
 */
export function bindControls({
    plan,
    onTool,
    onPrev,
    onNext,
    onSave,
    onClear,
    onCalibrate,
    onEdit,
    onAnnotationMode,
    annotationsOn = false,
    confirm = (message) => globalThis.confirm?.(message) ?? false,
    notify,
}) {
    const report = (level, message) => {
        if (!message) return;
        const toast = notify ?? globalThis.appNotify;
        if (typeof toast === 'function') {
            toast(message, level);
            return;
        }
        if (plan.status) {
            plan.status.textContent = message;
        }
    };

    const setBusy = (element, busy) => {
        if (!element) return;
        element.disabled = busy;
        element.setAttribute?.('aria-busy', busy ? 'true' : 'false');
    };

    for (const button of plan.toolButtons) {
        button.addEventListener?.('click', () => {
            const toolName = button.dataset?.yggTool ?? null;
            markActiveTool(plan.toolButtons, toolName);
            onTool?.(toolName);
        });
    }

    plan.prev?.addEventListener?.('click', () => onPrev?.());
    plan.next?.addEventListener?.('click', () => onNext?.());
    plan.edit?.addEventListener?.('click', () => onEdit?.());
    plan.calibrate?.addEventListener?.('click', () => onCalibrate?.());

    plan.save?.addEventListener?.('click', async () => {
        setBusy(plan.save, true);
        try {
            const result = await onSave?.();
            report(result?.level ?? 'success', result?.message ?? SAVED_MESSAGE);
        } finally {
            setBusy(plan.save, false);
        }
    });

    plan.clear?.addEventListener?.('click', async () => {
        if (!confirm(CLEAR_CONFIRM)) {
            return;
        }
        const result = await onClear?.();
        report(result?.level ?? 'success', result?.message ?? CLEARED_MESSAGE);
    });

    plan.annotationMode?.addEventListener?.('click', () => {
        // Read the state back out of the DOM rather than flipping a captured boolean.
        const next = !isAnnotationModeOn(plan);
        applyAnnotationMode({ plan, enabled: next });
        onAnnotationMode?.(next);
    });

    applyAnnotationMode({ plan, enabled: annotationsOn });

    return {
        setAnnotationMode(enabled) {
            applyAnnotationMode({ plan, enabled });
        },
        setCounter(index, total) {
            if (plan.counter) {
                plan.counter.textContent = formatCounter(index, total);
            }
            setBusy(plan.prev, total <= 1);
            setBusy(plan.next, total <= 1);
        },
        setCalibration(text) {
            if (plan.calibration) {
                plan.calibration.textContent = text;
            }
        },
        report,
    };
}
