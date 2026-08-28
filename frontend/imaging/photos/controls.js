/**
 * The photo stack's toolbar: DOM in, callbacks out.
 *
 * Same contract as `grid/controls.js` -- the ids are declared here rather than hunted for
 * in the template, a missing control is a degraded toolbar rather than a thrown error, and
 * the annotation mode's state is read back out of `aria-checked` at click time rather than
 * held in a closure. That last one is not fastidiousness: holding it in a closure is how
 * the grid's switch came to invert itself after the first click.
 */

/**
 * Element ids for one instance of the toolbar.
 *
 * A function of a prefix rather than a constant, because two photo stacks now share a
 * patient-detail page: teleradiography and the intraoral photographs are the same surface,
 * and ids are unique per *document*, not per surface. Without this the intraoral stack
 * would resolve teleradiography's viewport and both would render into one element.
 *
 * @param {string} prefix
 * @returns {object} `{[key]: id}`
 */
export function controlIds(prefix) {
    return Object.freeze({
        viewport: `${prefix}Viewport`,
        strip: `${prefix}Strip`,
        prev: `${prefix}Prev`,
        next: `${prefix}Next`,
        counter: `${prefix}Counter`,
        calibrate: `${prefix}Calibrate`,
        calibration: `${prefix}Calibration`,
        // The group the two above sit in, so annotation mode can hide the whole thing --
        // hiding the button but leaving its label and its group border behind reads as a
        // broken toolbar rather than a hidden feature.
        calibrationGroup: `${prefix}CalibrationGroup`,
        edit: `${prefix}EditImage`,
        save: `${prefix}SaveMeasurements`,
        clear: `${prefix}ClearMeasurements`,
        annotationMode: `${prefix}AnnotationMode`,
        annotationTools: `${prefix}AnnotationTools`,
        status: `${prefix}Status`,
    });
}

/** Teleradiography's toolbar, and the historical spelling every existing test uses. */
export const PHOTO_CONTROL_IDS = controlIds('photo');

/** The intraoral photographs' toolbar. */
export const INTRAORAL_CONTROL_IDS = controlIds('intraoralPhoto');

/**
 * Selector for the measurement-tool buttons, matching the grid's convention.
 *
 * Scoped per surface by {@link controlPlan}, which searches inside the toolbar's own
 * container: two toolbars on one page both carry `[data-ygg-tool]` buttons, and a document
 * -wide query would wire each surface to the other's.
 */
export const TOOL_BUTTON_SELECTOR = '[data-ygg-tool]';

export const SAVED_MESSAGE = 'Measurements saved.';
export const CLEAR_CONFIRM =
    'Remove every measurement on this image? Save afterwards to make it permanent; ' +
    'reload to undo it.';
export const CLEARED_MESSAGE = 'Measurements removed. Save to make it permanent.';

/**
 * Resolve one toolbar.
 *
 * @param {Document} doc
 * @param {object} [ids] from {@link controlIds}; defaults to teleradiography's.
 * @returns {object} `{[key]: Element|null}` plus `toolButtons`.
 */
export function controlPlan(doc, ids = PHOTO_CONTROL_IDS) {
    const plan = {};
    for (const [key, id] of Object.entries(ids)) {
        plan[key] = doc?.getElementById?.(id) ?? null;
    }
    // Scoped to this surface's tool container when it has one, so two toolbars on one page
    // do not each claim the other's buttons. Falls back to the document, which is what a
    // single-surface page and every existing test look like.
    const scope = plan.annotationTools ?? doc;
    plan.toolButtons = Array.from(scope?.querySelectorAll?.(TOOL_BUTTON_SELECTOR) ?? []);
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
    // Calibration rides with the measurement tools rather than sitting in the toolbar
    // permanently. It only does anything once a Length line has been drawn, so on a
    // read-only look at an image it was a button that could only ever answer "draw a line
    // first". One owner for both groups, so every surface built on `controlIds` inherits it.
    for (const group of [plan.annotationTools, plan.calibrationGroup]) {
        if (!group) {
            continue;
        }
        if (enabled) {
            group.removeAttribute?.('hidden');
        } else {
            group.setAttribute?.('hidden', '');
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
    // `window.appNotify(type, message, options)` -- **type first**. Getting this backwards
    // is what produced a toast titled "Info" whose body read "danger": the level was being
    // passed as the message and the message as the level, and `normalizeType` fell back to
    // 'info' for an unrecognised string. Named `type` here rather than `level` so the
    // argument order is legible at the call site.
    const report = (type, message) => {
        if (!message) return;
        const toast = notify ?? globalThis.appNotify;
        if (typeof toast === 'function') {
            toast(type, message);
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
            report(result?.type ?? 'success', result?.message ?? SAVED_MESSAGE);
        } finally {
            setBusy(plan.save, false);
        }
    });

    plan.clear?.addEventListener?.('click', async () => {
        if (!confirm(CLEAR_CONFIRM)) {
            return;
        }
        const result = await onClear?.();
        report(result?.type ?? 'success', result?.message ?? CLEARED_MESSAGE);
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
