import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CLEARED_MESSAGE,
    CLEAR_CONFIRM,
    CONTROL_IDS,
    SAVED_MESSAGE,
    applyAnnotationMode,
    bindControls,
    controlPlan,
    isAnnotationModeOn,
    loadingIndicator,
    markActiveTool,
} from '../imaging/grid/controls.js';

/** A DOM stub: enough of Document and Element for the binding, and nothing more. */
function fakeDoc(ids, toolNames = []) {
    const container = { classes: new Set(), classList: { toggle(name) {
        const has = container.classes.has(name);
        if (has) { container.classes.delete(name); } else { container.classes.add(name); }
        return !has;
    } } };
    const elements = new Map();
    const tools = toolNames.map((name) => ({
        dataset: { yggTool: name },
        attributes: {},
        classList: { toggle() {} },
        handlers: {},
        addEventListener(type, handler) {
            this.handlers[type] = handler;
        },
        setAttribute(key, value) {
            this.attributes[key] = value;
        },
    }));
    for (const id of ids) {
        // `state` is the switch's visible on/off word; every element carries one so the
        // stub does not have to know which id is the switch.
        const state = { textContent: '' };
        elements.set(id, {
            id,
            ownerDocument: null,
            value: '',
            hidden: false,
            dataset: {},
            textContent: '',
            state,
            attributes: {},
            handlers: {},
            addEventListener(type, handler) {
                this.handlers[type] = handler;
            },
            setAttribute(name, value) {
                this.attributes[name] = value;
            },
            getAttribute(name) {
                return this.attributes[name];
            },
            removeAttribute(name) {
                delete this.attributes[name];
            },
            querySelector: () => state,
        });
    }
    return {
        getElementById: (id) => elements.get(id) ?? null,
        querySelectorAll: () => tools,
        createElement: () => ({
            className: '',
            textContent: '',
            children: [],
            setAttribute() {},
            appendChild(child) {
                this.children.push(child);
            },
            remove() {
                this.removed = true;
            },
        }),
        querySelector: () => container,
        elements,
        tools,
        container,
    };
}

function fakeGrid(overrides = {}) {
    const calls = [];
    return {
        calls,
        resetCameras: (index) => calls.push(['resetCameras', index]),
        setPrimaryTool: (name) => calls.push(['setPrimaryTool', name]),
        setAnnotationMode: (enabled) => calls.push(['setAnnotationMode', enabled]),
        clearAnnotations: () => {
            calls.push(['clearAnnotations']);
            return 2;
        },
        enable3DWindow: async (index, mode) => calls.push(['enable3DWindow', index, mode]),
        setRenderMode: (index, mode) => {
            calls.push(['setRenderMode', index, mode]);
            return { label: `label:${mode}` };
        },
        readWindow: () => ({ windowCenter: 300, windowWidth: 1500, unit: 'HU' }),
        ...overrides,
    };
}

const ALL_IDS = Object.values(CONTROL_IDS);

test('the plan reports exactly which controls this page has', () => {
    // The toolbar is one template and the grid appears in two namespaces, so "the brain
    // page has no save button" has to be a fact, not a null dereference on one of two
    // pages.
    const full = controlPlan(fakeDoc(ALL_IDS, ['Length']));
    for (const id of Object.keys(CONTROL_IDS)) {
        assert.ok(full[id], id);
    }
    assert.equal(full.tools.length, 1);

    const bare = controlPlan(fakeDoc([], []));
    for (const id of Object.keys(CONTROL_IDS)) {
        assert.equal(bare[id], null, id);
    }
    assert.deepEqual(bare.tools, []);
});

test('binding a page with no controls at all is a no-op, not a crash', () => {
    const result = bindControls({ grid: fakeGrid(), doc: fakeDoc([]) });
    assert.deepEqual(result.bound, []);
    assert.doesNotThrow(() => result.setStatus('anything'));
});

test('reset view resets every loaded window', async () => {
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    await doc.elements.get(CONTROL_IDS.resetView).handlers.click({});
    assert.deepEqual(grid.calls, [['resetCameras', undefined]]);
});

test('each measurement button activates its tool and shows which is on', () => {
    // The tools are discovered from the DOM: each button carries the Cornerstone tool
    // name, so the template alone decides what the toolbar offers.
    const doc = fakeDoc(ALL_IDS, ['Crosshairs', 'Length', 'Angle']);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    doc.tools[1].handlers.click({});

    assert.deepEqual(grid.calls, [['setPrimaryTool', 'Length']]);
    assert.equal(doc.tools[1].attributes['aria-pressed'], 'true');
    assert.equal(doc.tools[0].attributes['aria-pressed'], 'false');
    assert.equal(doc.tools[2].attributes['aria-pressed'], 'false');
});

test('exactly one tool is ever marked active', () => {
    const doc = fakeDoc(ALL_IDS, ['Crosshairs', 'Length', 'Angle']);
    bindControls({ grid: fakeGrid(), doc });

    doc.tools[1].handlers.click({});
    doc.tools[2].handlers.click({});

    const pressed = doc.tools.filter((t) => t.attributes['aria-pressed'] === 'true');
    assert.equal(pressed.length, 1);
    assert.equal(pressed[0].dataset.yggTool, 'Angle');
});

test('a page with no tool buttons binds none, and does not crash', () => {
    // The brain page carries the grid without the CBCT toolbar.
    const result = bindControls({ grid: fakeGrid(), doc: fakeDoc(ALL_IDS, []) });
    assert.ok(!result.bound.includes('tools'));
});

test('a clean save is a green toast, not a sentence in the toolbar', async () => {
    // "Saved 3 measurements." in a toolbar is a sentence nobody reads. The platform's
    // own notification is the confirmation; the status line stays empty.
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    let saved = 0;
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onSave: async () => {
            saved += 1;
            return { level: 'success', message: SAVED_MESSAGE };
        },
    });

    await doc.elements.get(CONTROL_IDS.save).handlers.click({});
    assert.equal(saved, 1);
    assert.deepEqual(toasts, [['success', SAVED_MESSAGE]]);
    assert.equal(doc.elements.get(CONTROL_IDS.status).textContent, '');
});

test('a save that reports a failure is not toasted green', async () => {
    // A 409 arriving in the same box as a success is worse than no notification: the
    // user walks away believing the work is stored.
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onSave: async () => ({ level: 'danger', message: 'Someone else saved.' }),
    });

    await doc.elements.get(CONTROL_IDS.save).handlers.click({});
    assert.deepEqual(toasts, [['danger', 'Someone else saved.']]);
});

test('a save that throws is reported, and the button stops looking busy', async () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onSave: async () => {
            throw new Error('HTTP 500');
        },
    });

    const button = doc.elements.get(CONTROL_IDS.save);
    await button.handlers.click({});
    assert.deepEqual(toasts, [['danger', 'The save failed: HTTP 500']]);
    assert.equal(button.attributes['aria-busy'], undefined);
});

test('a handler that throws reports into the status line, not only the console', async () => {
    // A toolbar that fails silently gets reported as "the viewer is broken" with
    // nothing to go on.
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid({
        resetCameras: () => {
            throw new Error('no viewport');
        },
    });
    bindControls({ grid, doc });

    await doc.elements.get(CONTROL_IDS.resetView).handlers.click({});
    assert.match(doc.elements.get(CONTROL_IDS.status).textContent, /Reset view failed: no viewport/);
});

test('every control the template offers is bound', () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const result = bindControls({
        grid: fakeGrid(),
        doc,
        onSave: async () => ({}),
        onClear: async () => ({}),
        onAnnotationMode: () => {},
    });
    assert.deepEqual(result.bound.sort(), [
        'annotationMode',
        'clear',
        'expand3D',
        'resetView',
        'save',
        'tools',
    ]);
});


// ---------------------------------------------------------------------------
// The loading indicator
// ---------------------------------------------------------------------------

function fakeWindowElement() {
    const doc = {
        createElement: () => ({
            className: '',
            textContent: '',
            children: [],
            setAttribute() {},
            appendChild(child) {
                this.children.push(child);
            },
            remove() {
                this.removed = true;
            },
        }),
    };
    return { ownerDocument: doc, children: [], appendChild(node) { this.children.push(node); } };
}

test('the indicator reports progress as a percentage', () => {
    // Without it the grid is simply black while a CBCT arrives, which is
    // indistinguishable from a viewer that failed -- and was reported as exactly that.
    const element = fakeWindowElement();
    const indicator = loadingIndicator(element);
    const text = element.children[0].children[1];

    indicator.update(0.42);
    assert.match(text.textContent, /42%/);
});

test('progress that overshoots is clamped rather than shown as 103%', () => {
    const element = fakeWindowElement();
    const indicator = loadingIndicator(element);
    const text = element.children[0].children[1];

    indicator.update(1.03);
    assert.match(text.textContent, /100%/);
    indicator.update(-0.5);
    assert.match(text.textContent, /0%/);
});

test('the indicator is removed on completion, not merely hidden', () => {
    // A spinner that is only hidden is a spinner an early return can leave behind.
    const element = fakeWindowElement();
    const indicator = loadingIndicator(element);
    indicator.done();
    assert.equal(element.children[0].removed, true);
});

test('an indicator on a missing element is a harmless no-op', () => {
    const indicator = loadingIndicator(null);
    assert.doesNotThrow(() => indicator.update(0.5));
    assert.doesNotThrow(() => indicator.done());
});

test('markActiveTool tolerates a button with no classList', () => {
    const buttons = [{ dataset: { yggTool: 'Length' }, setAttribute() {} }];
    assert.doesNotThrow(() => markActiveTool(buttons, 'Length'));
});

test('expanding gives the 3D window the whole grid, and re-sizes Cornerstone', () => {
    // The grid is a CSS grid, so this is a class on the container. Cornerstone has to
    // be told afterwards: a viewport whose element changed size keeps rendering at the
    // old size until `resize()` says otherwise.
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const grid = fakeGrid();
    let resized = 0;
    grid.resize = () => {
        resized += 1;
    };
    bindControls({ grid, doc });

    doc.elements.get(CONTROL_IDS.expand3D).ownerDocument = doc;
    doc.elements.get(CONTROL_IDS.expand3D).handlers.click({});

    assert.ok(doc.container.classes.has('is-3d-expanded'));
    assert.equal(doc.elements.get(CONTROL_IDS.expand3D).attributes['aria-pressed'], 'true');
    assert.equal(resized, 1);
});

test('expanding again collapses back to the four-panel grid', () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const grid = fakeGrid();
    grid.resize = () => {};
    bindControls({ grid, doc });

    const button = doc.elements.get(CONTROL_IDS.expand3D);
    button.ownerDocument = doc;
    button.handlers.click({});
    button.handlers.click({});

    assert.ok(!doc.container.classes.has('is-3d-expanded'));
    assert.equal(button.attributes['aria-pressed'], 'false');
});

// ---------------------------------------------------------------------------
// Annotation mode
// ---------------------------------------------------------------------------

test('annotation mode starts off, and says so', async () => {
    // Off is the default: a clinician opening a study is reading it, not measuring it.
    // The state is asserted at bind time rather than assumed from the markup, so the
    // switch, the tool group and the grid cannot start out disagreeing.
    const doc = fakeDoc(ALL_IDS, ['Crosshairs', 'Length']);
    const seen = [];
    bindControls({ grid: fakeGrid(), doc, onAnnotationMode: (v) => seen.push(v) });

    const button = doc.elements.get(CONTROL_IDS.annotationMode);
    assert.deepEqual(seen, [false]);
    assert.equal(button.attributes['aria-checked'], 'false');
    assert.equal(button.state.textContent, 'off');
    assert.equal(doc.elements.get(CONTROL_IDS.annotationTools).hidden, true);
});

test('switching the mode on reveals the tools and shows the measurements', () => {
    // One switch, both effects. Two controls meant two states that could disagree,
    // which is how "I clicked show and saw nothing" was reported.
    const doc = fakeDoc(ALL_IDS, ['Crosshairs', 'Length']);
    const seen = [];
    bindControls({ grid: fakeGrid(), doc, onAnnotationMode: (v) => seen.push(v) });

    const button = doc.elements.get(CONTROL_IDS.annotationMode);
    button.handlers.click({});

    assert.deepEqual(seen, [false, true]);
    assert.equal(button.attributes['aria-checked'], 'true');
    assert.equal(button.state.textContent, 'on');
    assert.equal(doc.elements.get(CONTROL_IDS.annotationTools).hidden, false);
});

test('switching the mode off hides the tools and shows the crosshair as active', () => {
    // The *grid* puts the crosshair back on the left button, because the order between
    // that and disabling the measurement tools is not free -- see `setAnnotationMode`.
    // What the toolbar owns is which button looks pressed.
    const doc = fakeDoc(ALL_IDS, ['Crosshairs', 'Length']);
    const grid = fakeGrid();
    bindControls({ grid, doc, onAnnotationMode: (enabled) => grid.setAnnotationMode(enabled) });

    const button = doc.elements.get(CONTROL_IDS.annotationMode);
    button.handlers.click({});
    doc.tools[1].handlers.click({});
    button.handlers.click({});

    assert.equal(button.attributes['aria-checked'], 'false');
    assert.equal(doc.elements.get(CONTROL_IDS.annotationTools).hidden, true);
    assert.deepEqual(grid.calls.at(-1), ['setAnnotationMode', false]);
    const pressed = doc.tools.filter((t) => t.attributes['aria-pressed'] === 'true');
    assert.deepEqual(pressed.map((t) => t.dataset.yggTool), ['Crosshairs']);
});

test('the mode reads its state from the DOM, so it cannot invert', () => {
    // The bug this replaces: a closure variable initialised to `true` against markup
    // that rendered "shown" meant the first click *hid*, and every click after it was
    // the opposite of what the switch said.
    const doc = fakeDoc(ALL_IDS, ['Crosshairs']);
    const seen = [];
    bindControls({ grid: fakeGrid(), doc, onAnnotationMode: (v) => seen.push(v) });

    const button = doc.elements.get(CONTROL_IDS.annotationMode);
    // Something else moves the switch -- a re-render, a second binding, anything.
    applyAnnotationMode({ plan: { annotationMode: button }, enabled: true });
    button.handlers.click({});

    assert.equal(isAnnotationModeOn({ annotationMode: button }), false);
    assert.deepEqual(seen, [false, false]);
});

test('the mode can be driven from outside the toolbar', () => {
    const doc = fakeDoc(ALL_IDS, ['Crosshairs']);
    const seen = [];
    const controls = bindControls({ grid: fakeGrid(), doc, onAnnotationMode: (v) => seen.push(v) });

    controls.setAnnotationMode(true);
    assert.deepEqual(seen, [false, true]);
    assert.equal(doc.elements.get(CONTROL_IDS.annotationMode).state.textContent, 'on');
});

test('applying the mode to a page that has no switch is a no-op', () => {
    // The brain page carries the grid without the CBCT toolbar.
    assert.doesNotThrow(() => applyAnnotationMode({ plan: {}, enabled: true }));
    assert.equal(isAnnotationModeOn({}), false);
});


// ---------------------------------------------------------------------------
// Clearing
// ---------------------------------------------------------------------------

/** Give one element a window, so the clear button can ask for confirmation. */
function withConfirm(element, answer) {
    const asked = [];
    element.ownerDocument = {
        defaultView: {
            confirm: (message) => {
                asked.push(message);
                return answer;
            },
        },
    };
    return asked;
}

test('clearing asks first, and says the save is what makes it permanent', async () => {
    // It destroys visible work and the only undo is a reload -- and only until the next
    // save. Both the question and the confirmation have to say so.
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    let cleared = 0;
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onClear: async () => {
            cleared += 1;
            return { level: 'success', message: CLEARED_MESSAGE };
        },
    });

    const button = doc.elements.get(CONTROL_IDS.clear);
    const asked = withConfirm(button, true);
    await button.handlers.click({});

    assert.deepEqual(asked, [CLEAR_CONFIRM]);
    assert.equal(cleared, 1);
    assert.deepEqual(toasts, [['success', CLEARED_MESSAGE]]);
    assert.match(CLEAR_CONFIRM, /reload/);
    assert.match(CLEARED_MESSAGE, /[Ss]ave/);
});

test('declining the confirmation clears nothing', async () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    let cleared = 0;
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onClear: async () => {
            cleared += 1;
            return {};
        },
    });

    const button = doc.elements.get(CONTROL_IDS.clear);
    withConfirm(button, false);
    await button.handlers.click({});

    assert.equal(cleared, 0);
    assert.deepEqual(toasts, []);
});

test('a clear that fails is reported rather than looking like a success', async () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const toasts = [];
    bindControls({
        grid: fakeGrid(),
        doc,
        notify: (level, message) => toasts.push([level, message]),
        onClear: async () => {
            throw new Error('no viewport');
        },
    });

    const button = doc.elements.get(CONTROL_IDS.clear);
    withConfirm(button, true);
    await button.handlers.click({});

    assert.deepEqual(toasts, [['danger', 'Could not clear the measurements: no viewport']]);
});
