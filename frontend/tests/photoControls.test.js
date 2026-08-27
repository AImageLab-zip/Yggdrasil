import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CLEARED_MESSAGE,
    CLEAR_CONFIRM,
    PHOTO_CONTROL_IDS,
    SAVED_MESSAGE,
    applyAnnotationMode,
    bindControls,
    controlPlan,
    formatCounter,
    isAnnotationModeOn,
    markActiveTool,
} from '../imaging/photos/controls.js';

/** A DOM stub carrying every control the toolbar knows about. */
function fakeDoc({ omit = [], tools = ['Length', 'Angle'] } = {}) {
    const elements = new Map();
    const make = (id) => ({
        id,
        attributes: new Map(),
        children: [],
        textContent: '',
        disabled: false,
        listeners: new Map(),
        dataset: {},
        classList: { toggle: () => {} },
        setAttribute(name, value) {
            this.attributes.set(name, value);
        },
        getAttribute(name) {
            return this.attributes.get(name) ?? null;
        },
        removeAttribute(name) {
            this.attributes.delete(name);
        },
        addEventListener(type, handler) {
            this.listeners.set(type, handler);
        },
        click() {
            return this.listeners.get('click')?.();
        },
        querySelector(selector) {
            return selector === '[data-mode-state]' ? this.stateLabel : null;
        },
    });

    for (const id of Object.values(PHOTO_CONTROL_IDS)) {
        if (omit.includes(id)) continue;
        elements.set(id, make(id));
    }
    const mode = elements.get(PHOTO_CONTROL_IDS.annotationMode);
    if (mode) {
        mode.stateLabel = make('state');
    }

    const toolButtons = tools.map((name) => {
        const button = make(`tool-${name}`);
        button.dataset.yggTool = name;
        return button;
    });

    return {
        getElementById: (id) => elements.get(id) ?? null,
        querySelectorAll: () => toolButtons,
        _elements: elements,
        _toolButtons: toolButtons,
    };
}

// ---------------------------------------------------------------------------
// The plan
// ---------------------------------------------------------------------------

test('a missing control disables its feature rather than throwing', () => {
    // A template that omits a button must degrade, not break the tab -- the viewer is one
    // pane of a patient record the rest of which has to keep working.
    const doc = fakeDoc({ omit: [PHOTO_CONTROL_IDS.calibrate, PHOTO_CONTROL_IDS.edit] });
    const plan = controlPlan(doc);
    assert.equal(plan.calibrate, null);
    assert.equal(plan.edit, null);
    assert.ok(plan.viewport);
    assert.doesNotThrow(() => bindControls({ plan, onCalibrate: () => {} }));
});

test('the plan picks up every declared id', () => {
    const plan = controlPlan(fakeDoc());
    for (const key of Object.keys(PHOTO_CONTROL_IDS)) {
        assert.ok(plan[key], key);
    }
});

// ---------------------------------------------------------------------------
// Annotation mode
// ---------------------------------------------------------------------------

test('annotation mode is off by default and the tools are hidden', () => {
    const doc = fakeDoc();
    const plan = controlPlan(doc);
    bindControls({ plan });
    assert.equal(isAnnotationModeOn(plan), false);
    assert.equal(plan.annotationTools.getAttribute('hidden'), '');
    assert.equal(plan.annotationMode.stateLabel.textContent, 'off');
});

test('the switch state is read from the DOM, so it cannot invert', () => {
    // Holding it in a closure is how the grid's switch came to invert itself after the
    // first click. Two clicks must land on, then off -- not on, then on again.
    const plan = controlPlan(fakeDoc());
    const seen = [];
    bindControls({ plan, onAnnotationMode: (enabled) => seen.push(enabled) });

    plan.annotationMode.click();
    plan.annotationMode.click();
    plan.annotationMode.click();
    assert.deepEqual(seen, [true, false, true]);
});

test('turning the mode on reveals the tools and says so in words', () => {
    const plan = controlPlan(fakeDoc());
    bindControls({ plan });
    plan.annotationMode.click();
    // The attribute is *removed*, not set to a falsy value: `hidden=""` is still hidden.
    assert.equal(plan.annotationTools.getAttribute('hidden'), null);
    assert.equal(plan.annotationTools.attributes.has('hidden'), false);
    assert.equal(plan.annotationMode.getAttribute('aria-checked'), 'true');
    assert.equal(plan.annotationMode.stateLabel.textContent, 'on');
});

test('applyAnnotationMode is idempotent', () => {
    const plan = controlPlan(fakeDoc());
    applyAnnotationMode({ plan, enabled: true });
    applyAnnotationMode({ plan, enabled: true });
    assert.equal(plan.annotationMode.getAttribute('aria-checked'), 'true');
});

test('the mode can be initialised on', () => {
    const plan = controlPlan(fakeDoc());
    bindControls({ plan, annotationsOn: true });
    assert.equal(isAnnotationModeOn(plan), true);
});

// ---------------------------------------------------------------------------
// Save, clear, and how they report
// ---------------------------------------------------------------------------

test('a save reports through the toast, at the level the handler chose', async () => {
    const plan = controlPlan(fakeDoc());
    const notified = [];
    bindControls({
        plan,
        notify: (type, message) => notified.push([type, message]),
        onSave: () => ({ type: 'danger', message: 'nope' }),
    });
    await plan.save.click();
    assert.deepEqual(notified, [['danger', 'nope']]);
});

test('a save with no message falls back to the success wording', async () => {
    const plan = controlPlan(fakeDoc());
    const notified = [];
    bindControls({ plan, notify: (type, message) => notified.push([type, message]), onSave: () => ({}) });
    await plan.save.click();
    assert.deepEqual(notified, [['success', SAVED_MESSAGE]]);
});

test('the save button is busy while saving and released afterwards', async () => {
    // Including when the handler throws: a permanently disabled save button is worse
    // than a failed save, because the user cannot retry.
    const plan = controlPlan(fakeDoc());
    let during = null;
    bindControls({
        plan,
        notify: () => {},
        onSave: async () => {
            during = plan.save.disabled;
            return {};
        },
    });
    await plan.save.click();
    assert.equal(during, true);
    assert.equal(plan.save.disabled, false);
});

test('clear asks first, and declining does nothing', () => {
    const plan = controlPlan(fakeDoc());
    let cleared = false;
    bindControls({
        plan,
        confirm: () => false,
        notify: () => {},
        onClear: () => {
            cleared = true;
            return {};
        },
    });
    plan.clear.click();
    assert.equal(cleared, false);
});

test('the confirmation says a reload undoes it and a save makes it permanent', () => {
    // The clear is viewer-only; the server replaces the whole set on save. Saying so is
    // what stops it reading as an irreversible delete.
    assert.match(CLEAR_CONFIRM, /Save afterwards/);
    assert.match(CLEAR_CONFIRM, /reload to undo/i);
    assert.match(CLEARED_MESSAGE, /Save to make it permanent/);
});

test('accepting the confirmation clears and reports', async () => {
    const plan = controlPlan(fakeDoc());
    const notified = [];
    bindControls({
        plan,
        confirm: () => true,
        notify: (type, message) => notified.push([type, message]),
        onClear: () => ({}),
    });
    await plan.clear.click();
    assert.deepEqual(notified, [['success', CLEARED_MESSAGE]]);
});

test('with no toast available, the status line is the fallback', () => {
    // Not silence: a failure nobody is told about is the bug the grid's bootstrap had.
    const plan = controlPlan(fakeDoc());
    const handle = bindControls({ plan, notify: undefined });
    const previous = globalThis.appNotify;
    globalThis.appNotify = undefined;
    handle.report('danger', 'something broke');
    globalThis.appNotify = previous;
    assert.equal(plan.status.textContent, 'something broke');
});

// ---------------------------------------------------------------------------
// Tools and the counter
// ---------------------------------------------------------------------------

test('choosing a tool marks exactly one button active', () => {
    const doc = fakeDoc();
    const plan = controlPlan(doc);
    const chosen = [];
    bindControls({ plan, onTool: (name) => chosen.push(name) });

    doc._toolButtons[0].click();
    assert.deepEqual(chosen, ['Length']);
    assert.equal(doc._toolButtons[0].getAttribute('aria-pressed'), 'true');
    assert.equal(doc._toolButtons[1].getAttribute('aria-pressed'), 'false');
});

test('markActiveTool with null clears every button', () => {
    const doc = fakeDoc();
    markActiveTool(doc._toolButtons, null);
    for (const button of doc._toolButtons) {
        assert.equal(button.getAttribute('aria-pressed'), 'false');
    }
});

test('the counter is one-based for humans and empty for an empty stack', () => {
    assert.equal(formatCounter(0, 3), '1 of 3');
    assert.equal(formatCounter(2, 3), '3 of 3');
    assert.equal(formatCounter(0, 0), '');
});

test('a single-image stack disables the arrows', () => {
    const plan = controlPlan(fakeDoc());
    const handle = bindControls({ plan });
    handle.setCounter(0, 1);
    assert.equal(plan.prev.disabled, true);
    handle.setCounter(0, 4);
    assert.equal(plan.prev.disabled, false);
});
