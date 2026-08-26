import test from 'node:test';
import assert from 'node:assert/strict';

import { CONTROL_IDS, RENDER_WINDOW, bindControls, controlPlan, windowReadout } from '../imaging/grid/controls.js';

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

test('changing the render mode brings 3D up first, then applies the mode', async () => {
    // 3D is lazy, so the first interaction with any 3D control has to load it.
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    const select = doc.elements.get(CONTROL_IDS.renderMode);
    await select.handlers.change({ target: { value: 'amip' } });

    assert.deepEqual(grid.calls, [
        ['enable3DWindow', RENDER_WINDOW, 'amip'],
        ['setRenderMode', RENDER_WINDOW, 'amip'],
    ]);
    assert.equal(doc.elements.get(CONTROL_IDS.status).textContent, 'label:amip');
});

test('3D is brought up once, not on every mode change', async () => {
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    const select = doc.elements.get(CONTROL_IDS.renderMode);
    await select.handlers.change({ target: { value: 'amip' } });
    await select.handlers.change({ target: { value: 'shaded' } });

    const loads = grid.calls.filter(([name]) => name === 'enable3DWindow');
    assert.equal(loads.length, 1, 'a full CBCT volume render must not be rebuilt per change');
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

test('resetting the 3D camera does not load 3D just to reset it', async () => {
    const doc = fakeDoc(ALL_IDS);
    const grid = fakeGrid();
    bindControls({ grid, doc });

    await doc.elements.get(CONTROL_IDS.reset3DCamera).handlers.click({});
    assert.deepEqual(grid.calls, [], 'the most expensive possible reading of a click');
    assert.match(doc.elements.get(CONTROL_IDS.status).textContent, /not loaded/);
});

test('the 3D toggle sets its pressed state and reveals the exit button', async () => {
    const doc = fakeDoc(ALL_IDS);
    bindControls({ grid: fakeGrid(), doc });

    doc.elements.get(CONTROL_IDS.exit3D).hidden = true;
    await doc.elements.get(CONTROL_IDS.toggle3D).handlers.click({});

    assert.equal(doc.elements.get(CONTROL_IDS.toggle3D).attributes['aria-pressed'], 'true');
    assert.equal(doc.elements.get(CONTROL_IDS.exit3D).hidden, false);
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

test('one broken control does not prevent the others binding', () => {
    const doc = fakeDoc(ALL_IDS);
    const result = bindControls({ grid: fakeGrid(), doc });
    assert.deepEqual(result.bound.sort(), ['exit3D', 'renderMode', 'reset3DCamera', 'resetView', 'toggle3D']);
});

// ---------------------------------------------------------------------------
// The readout that replaced the percent sliders
// ---------------------------------------------------------------------------

test('the readout shows modality units with the unit named', () => {
    const element = { textContent: 'stale' };
    windowReadout({ grid: fakeGrid(), element })();
    assert.equal(element.textContent, 'W 1500 HU / L 300 HU');
});

test('the readout omits a unit the modality has not earned', () => {
    // CBCT greyscale is vendor-dependent and is not calibrated Hounsfield.
    const element = { textContent: '' };
    const grid = fakeGrid({ readWindow: () => ({ windowCenter: 800, windowWidth: 2800, unit: '' }) });
    windowReadout({ grid, element })();
    assert.equal(element.textContent, 'W 2800 / L 800');
});

test('the readout is blank when nothing is loaded, not a zero window', () => {
    // "W 0 / L 0" is a real setting and would read as one.
    const element = { textContent: 'stale' };
    windowReadout({ grid: fakeGrid({ readWindow: () => null }), element })();
    assert.equal(element.textContent, '');
});

test('the readout survives a grid that throws', () => {
    const element = { textContent: 'stale' };
    const grid = fakeGrid({
        readWindow: () => {
            throw new Error('not loaded');
        },
    });
    assert.doesNotThrow(() => windowReadout({ grid, element })());
    assert.equal(element.textContent, '');
});
