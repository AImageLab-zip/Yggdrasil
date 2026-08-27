import test from 'node:test';
import assert from 'node:assert/strict';

import { CONTROL_IDS, RENDER_WINDOW, bindControls, controlPlan } from '../imaging/grid/controls.js';

/** A DOM stub: enough of Document and Element for the binding, and nothing more. */
function fakeDoc(ids) {
    const elements = new Map();
    for (const id of ids) {
        elements.set(id, {
            id,
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
    return { getElementById: (id) => elements.get(id) ?? null, elements };
}

function fakeGrid(overrides = {}) {
    const calls = [];
    return {
        calls,
        resetCameras: (index) => calls.push(['resetCameras', index]),
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
    // page has no render-mode select" has to be a fact, not a null dereference on one
    // of two pages.
    const full = controlPlan(fakeDoc(ALL_IDS));
    assert.ok(Object.values(full).every(Boolean));

    const bare = controlPlan(fakeDoc([]));
    assert.ok(Object.values(bare).every((value) => value === null));
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

test('changing the render mode applies it to the 3D window', () => {
    // The 3D window is built with the rest of the grid now, so there is nothing to
    // bring up first -- the old "Load 3D" step is gone with the button.
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    doc.elements.get(CONTROL_IDS.renderMode).handlers.change({ target: { value: 'amip' } });

    assert.deepEqual(grid.calls, [['setRenderMode', RENDER_WINDOW, 'amip']]);
    assert.equal(doc.elements.get(CONTROL_IDS.status).textContent, 'label:amip');
});

test('the render-mode default is read from the template, not assumed', () => {
    // The `selected` attribute decides what a clinician actually sees, and it lives in
    // the template.
    const doc = fakeDoc(ALL_IDS);
    doc.elements.get(CONTROL_IDS.renderMode).value = 'shaded';
    bindControls({ grid: fakeGrid(), doc });
    assert.equal(doc.elements.get(CONTROL_IDS.renderMode).dataset.initialMode, 'shaded');

    const empty = fakeDoc(ALL_IDS);
    bindControls({ grid: fakeGrid(), doc: empty });
    assert.equal(empty.elements.get(CONTROL_IDS.renderMode).dataset.initialMode, 'amip');
});

test('resetting the 3D camera resets the 3D window, which is what it always claimed', () => {
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    doc.elements.get(CONTROL_IDS.reset3DCamera).handlers.click({});
    assert.deepEqual(grid.calls, [['resetCameras', RENDER_WINDOW]]);
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
    const doc = fakeDoc(ALL_IDS);
    const result = bindControls({ grid: fakeGrid(), doc });
    assert.deepEqual(result.bound.sort(), ['renderMode', 'reset3DCamera', 'resetView']);
});
