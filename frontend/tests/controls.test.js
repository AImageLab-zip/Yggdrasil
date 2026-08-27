import test from 'node:test';
import assert from 'node:assert/strict';

import {
    CONTROL_IDS,
    bindControls,
    controlPlan,
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
        elements.set(id, {
            id,
            ownerDocument: null,
            value: '',
            hidden: false,
            dataset: {},
            textContent: '',
            attributes: {},
            handlers: {},
            addEventListener(type, handler) {
                this.handlers[type] = handler;
            },
            setAttribute(name, value) {
                this.attributes[name] = value;
            },
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

test('saving reports what happened, in the status line', async () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    let saved = 0;
    bindControls({
        grid: fakeGrid(),
        doc,
        onSave: async () => {
            saved += 1;
            return { message: 'Saved 2 measurement(s) as revision 5.' };
        },
    });

    await doc.elements.get(CONTROL_IDS.save).handlers.click({});
    assert.equal(saved, 1);
    assert.match(doc.elements.get(CONTROL_IDS.status).textContent, /revision 5/);
});

test('a failed save says so rather than looking like a success', async () => {
    const doc = fakeDoc(ALL_IDS, ['Length']);
    bindControls({
        grid: fakeGrid(),
        doc,
        onSave: async () => {
            throw new Error('HTTP 409');
        },
    });

    await doc.elements.get(CONTROL_IDS.save).handlers.click({});
    assert.match(doc.elements.get(CONTROL_IDS.status).textContent, /Save failed: HTTP 409/);
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
        onToggleAnnotations: () => {},
    });
    assert.deepEqual(result.bound.sort(), [
        'expand3D',
        'resetView',
        'save',
        'toggleAnnotations',
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

test('the measurements toggle hides and shows without deleting', () => {
    // Visibility, not deletion: the annotations stay in Cornerstone's state, so hiding
    // cannot lose work and a save while hidden still writes them all.
    const doc = fakeDoc(ALL_IDS, ['Length']);
    const seen = [];
    bindControls({ grid: fakeGrid(), doc, onToggleAnnotations: (v) => seen.push(v) });

    const button = doc.elements.get(CONTROL_IDS.toggleAnnotations);
    button.handlers.click({});
    assert.deepEqual(seen, [false]);
    assert.equal(button.attributes['aria-pressed'], 'false');

    button.handlers.click({});
    assert.deepEqual(seen, [false, true]);
    assert.equal(button.attributes['aria-pressed'], 'true');
});

test('the toggle starts from visible, matching the template', () => {
    // The template renders it pressed; the first click must therefore hide.
    const doc = fakeDoc(ALL_IDS, []);
    const seen = [];
    bindControls({ grid: fakeGrid(), doc, onToggleAnnotations: (v) => seen.push(v) });
    doc.elements.get(CONTROL_IDS.toggleAnnotations).handlers.click({});
    assert.deepEqual(seen, [false]);
});
