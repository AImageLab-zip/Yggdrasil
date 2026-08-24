'use strict';

/**
 * Panoramic editor entry behaviour.
 *
 * Two things this locks in:
 *  - the spline editor is never shown on page load (it used to take over the
 *    patient view for every CBCT patient with no saved panoramic);
 *  - a patient with no panoramic gets a default one generated and saved
 *    unattended: MIP projection, auto axial slice, auto arch spline.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const EDITOR_SOURCE = fs.readFileSync(
    path.join(__dirname, '../modality_viewers/cbct_panorex_editor.js'),
    'utf8'
);

/** Minimal element stub: only what the editor actually touches. */
function makeElement(id) {
    return {
        id,
        hidden: true,
        disabled: false,
        textContent: '',
        value: '',
        max: '',
        width: 0,
        height: 0,
        dataset: {},
        style: {},
        classList: { add() {}, remove() {}, toggle() {} },
        parentElement: null,
        addEventListener() {},
        removeEventListener() {},
        setAttribute() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        appendChild() {},
        getContext() {
            return {
                createImageData: (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
                putImageData() {},
                drawImage() {},
                save() {},
                restore() {},
                beginPath() {},
                moveTo() {},
                lineTo() {},
                stroke() {}
            };
        },
        toBlob(callback) { callback({ size: 8, type: 'image/png' }); }
    };
}

function buildHarness({ savedState = null, revision = 0, panoramicLocked = false } = {}) {
    const elements = {};
    const workers = [];
    const saves = [];
    const listeners = {};

    const csrfInput = makeElement('csrf');
    csrfInput.value = 'test-csrf';

    const root = makeElement('cbctPanorexEditor');
    root.dataset.canEdit = 'true';
    root.dataset.panoramicLocked = panoramicLocked ? 'true' : 'false';
    root.querySelector = (selector) =>
        (selector === 'input[name="csrfmiddlewaretoken"]' ? csrfInput : null);
    elements.cbctPanorexEditor = root;

    const document_ = {
        getElementById: (id) => {
            if (!(id in elements)) elements[id] = makeElement(id);
            return elements[id];
        },
        createElement: () => makeElement('created'),
        addEventListener(name, handler) { listeners[name] = handler; },
        cookie: ''
    };

    class FakeWorker {
        constructor() {
            this.messages = [];
            workers.push(this);
        }
        postMessage(message) { this.messages.push(message); }
        terminate() { this.terminated = true; }
    }

    const context = {
        console: { debug: (...a) => { if (process.env.EDITOR_DEBUG) console.error('DEBUG:', ...a); }, error: console.error, log: console.log, warn: console.warn },
        setTimeout: (fn) => { fn(); return 0; },
        clearInterval() {},
        setInterval: () => 0,
        Promise,
        Math,
        Number,
        Array,
        Object,
        JSON,
        Uint8Array,
        Uint8ClampedArray,
        Float32Array,
        CustomEvent: class { constructor(name, options) { this.type = name; Object.assign(this, options); } },
        FormData: class { constructor() { this.entries = {}; } append(key, value) { this.entries[key] = value; } },
        document: document_
    };
    context.window = context;
    context.globalThis = context;
    context.Worker = FakeWorker;
    // Konva stub: drawAxialEditor() builds a stage, two image layers, a spline
    // line and one draggable group per control point.
    const konvaNode = () => ({
        add() {}, on() {}, off() {}, position() {}, size() {}, draw() {}, batchDraw() {},
        destroyChildren() {}, listening() {}, points() {}, x: () => 0, y: () => 0,
        isDragging: () => false, startDrag() {}, stopDrag() {},
        container: () => ({ style: {} }), getPointerPosition: () => null
    });
    context.Konva = {
        Stage: function () { return konvaNode(); },
        Layer: function () { return konvaNode(); },
        Image: function () { return konvaNode(); },
        Line: function () { return konvaNode(); },
        Group: function () { return konvaNode(); },
        Circle: function () { return konvaNode(); }
    };
    context.scanId = 4242;
    context.canEdit = true;
    context.Seg2PanoCore = {
        clamp: (value, low, high) => Math.min(high, Math.max(low, value)),
        canonicalZToNative: (z) => z,
        catmullRomChain: (points) => points,
        normalizeOpenCV: (values) => new Uint8Array(values.length),
        projectColumnPair() {}
    };
    context.ViewerGrid = {
        getPanorexSourceDescriptor: () => ({
            jobId: 7,
            volumeFileId: 11,
            volumeFileKey: 'volume_nifti',
            volumeFileHash: 'a'.repeat(64),
            segmentationFileId: 12,
            segmentationFileKey: 'segmentation_nifti',
            segmentationFileHash: 'b'.repeat(64),
            revision: revision,
            state: savedState
        }),
        getNativeRawVolumeDescriptor: () => ({
            data: new Float32Array(8),
            dimensions: { width: 2, height: 2, depth: 2 },
            affine: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            flipZ: false,
            slope: 1,
            intercept: 0,
            source: { fileId: '11' }
        }),
        getPanorexSegmentationSource: () => Promise.resolve({ arrayBuffer: new ArrayBuffer(16) })
    };
    context.addEventListener = (name, handler) => { listeners[name] = handler; };
    context.removeEventListener = () => {};
    context.dispatchEvent = () => true;
    context.Blob = class { constructor() { this.size = 8; } };
    context.File = class { constructor() { this.size = 8; } };
    context.crypto = { randomUUID: () => '00000000-0000-4000-8000-000000000000' };
    context.fetch = (url, options) => {
        saves.push({ url, options });
        return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ revision: 1, default_mode: 'mip', variants: [] })
        });
    };
    context.PanoramicViewer = { refreshAfterSave() {} };

    vm.createContext(context);
    vm.runInContext(EDITOR_SOURCE, context, { filename: 'cbct_panorex_editor.js' });

    return { context, elements, workers, saves, listeners, root };
}

/** Walk the worker protocol far enough for a full unattended generation. */
async function completeGeneration(harness) {
    await new Promise((resolve) => setImmediate(resolve));
    const worker = harness.workers[0];
    assert.ok(worker, 'the geometry worker must have started');
    const initMessage = worker.messages.find((message) => message.type === 'init');
    assert.ok(initMessage, 'the worker must be initialized');

    worker.onmessage({
        data: {
            type: 'initialized',
            id: initMessage.id,
            autoZ: 96,
            flipZ: false,
            dimensions: { width: 2, height: 2, depth: 2 }
        }
    });

    const geometryMessage = worker.messages.filter((message) => message.type === 'geometry').pop();
    assert.ok(geometryMessage, 'auto geometry must be requested');
    worker.onmessage({
        data: {
            type: 'geometry',
            id: geometryMessage.id,
            z: 96,
            source: 'auto',
            polynomial: [1, 0, 0],
            start: 0,
            end: 1,
            controlPoints: [[0, 0], [1, 1], [2, 2], [3, 3]],
            spline: [[0, 0], [1, 1]],
            centerline: [[0, 0], [1, 1]],
            slab: [[[0, 0], [1, 1]]],
            mask: new Uint8Array(4)
        }
    });
    // canvasBlob and fetch both resolve on the microtask queue.
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    return { geometryMessage };
}

test('the editor is not shown on load when no panoramic exists', async () => {
    const harness = buildHarness();
    harness.listeners.DOMContentLoaded();
    // The raw volume + segmentation lookups resolve on the microtask queue.
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.root.hidden, true, 'the spline editor must stay closed on load');
    // ...but the default generation is under way.
    assert.equal(harness.workers.length, 1);
});

test('the editor is not shown on load when a panoramic already exists', () => {
    const harness = buildHarness({
        revision: 3,
        savedState: { algorithmVersion: 'panorex-js-v2-mip', spline: [], volumeShape: [2, 2, 2] }
    });
    harness.listeners.DOMContentLoaded();

    assert.equal(harness.root.hidden, true);
    assert.equal(harness.workers.length, 0, 'nothing to generate: no worker should start');
});

test('a patient with no panoramic gets a MIP default from the auto geometry', async () => {
    const harness = buildHarness();
    harness.listeners.DOMContentLoaded();
    const { geometryMessage } = await completeGeneration(harness);

    // Auto geometry means: no caller-supplied control points.
    assert.equal(geometryMessage.controlPoints, null);
    assert.equal(geometryMessage.z, 96, 'the auto axial slice must be used');

    assert.equal(harness.saves.length, 1, 'the default must be saved without user action');
    const state = JSON.parse(harness.saves[0].options.body.entries.state);
    assert.equal(state.default_mode, 'mip');
    assert.equal(state.geometry_source, 'auto');
    assert.equal(state.axial_slice, 96);
    assert.equal(state.base_revision, 0);
    assert.deepEqual(state.volume_shape, [2, 2, 2]);
    assert.equal(harness.saves[0].url, '/maxillo/api/patient/4242/panoramic/generated/');
});

test('the editor stays hidden after an unattended save', async () => {
    const harness = buildHarness();
    harness.listeners.DOMContentLoaded();
    await completeGeneration(harness);

    assert.equal(harness.root.hidden, true, 'a silent generation must never reveal the editor');
});

test('entering edit mode reveals the editor', async () => {
    const harness = buildHarness();
    harness.listeners.DOMContentLoaded();
    await completeGeneration(harness);

    harness.context.CBCTPanorexEditor.enterEditMode();

    assert.equal(harness.root.hidden, false, 'the Edit button must open the editor');
});

test('a locked patient cannot enter edit mode', async () => {
    // Raw data and the arch freeze once annotations exist. The Edit button is not
    // rendered at all then, so this guards the programmatic entry point.
    const harness = buildHarness({ panoramicLocked: true });
    harness.listeners.DOMContentLoaded();
    await completeGeneration(harness);

    const entered = harness.context.CBCTPanorexEditor.enterEditMode();

    assert.equal(entered, false, 'enterEditMode must refuse a locked patient');
    assert.equal(harness.root.hidden, true, 'the editor must stay closed');
});

test('a missing segmentation is not reported as an error to the reader', async () => {
    const harness = buildHarness();
    harness.context.ViewerGrid.getPanorexSegmentationSource = () =>
        Promise.reject(new Error('No paired panoramic segmentation source is available'));

    harness.listeners.DOMContentLoaded();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(harness.root.hidden, true, 'a still-processing CBCT must not open the editor');
    const error = harness.elements.panorexEditorError;
    assert.ok(!error || error.hidden !== false, 'no error banner for a CBCT without a segmentation');
    assert.equal(harness.saves.length, 0);
});
