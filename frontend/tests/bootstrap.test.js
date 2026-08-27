import test from 'node:test';
import assert from 'node:assert/strict';

import {
    bootstrapVolumeGrid,
    isMeasurable,
    measurementsUrl,
    observeSize,
    primaryVolumeFrom,
    readGridData,
    readWindowElements,
    showWindowMessage,
} from '../imaging/grid/bootstrap.js';

/** A DOM stub with four grid windows and a JSON payload. */
function fakeDoc({ data = {}, windows = 4, measurable = true } = {}) {
    const made = [];
    const elements = [];
    for (let index = 0; index < windows; index += 1) {
        elements.push({
            dataset: { windowIndex: String(index) },
            offsetParent: measurable ? {} : null,
            clientWidth: measurable ? 400 : 0,
            children: [],
            addEventListener() {},
            querySelector: () => null,
            classList: { add() {}, toggle() {} },
            appendChild(node) {
                this.children.push(node);
            },
            ownerDocument: null,
        });
    }
    const doc = {
        defaultView: {
            location: { origin: 'https://ygg.example' },
            dispatchEvent: () => true,
            addEventListener() {},
            removeEventListener() {},
            CustomEvent: globalThis.CustomEvent,
        },
        getElementById: (id) =>
            id === 'viewerGridData' && data ? { textContent: JSON.stringify(data) } : null,
        querySelectorAll: (selector) => (selector === '[data-ygg-tool]' ? [] : elements),
        createElement: () => {
            const node = {
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
            };
            made.push(node);
            return node;
        },
    };
    for (const element of elements) {
        element.ownerDocument = doc;
    }
    return { doc, elements, made };
}

/** A mounted grid, with a rendering engine that counts resizes. */
function fakeMountedGrid() {
    const grid = {
        resets: 0,
        renderingEngine: {
            resizes: 0,
            resize() {
                this.resizes += 1;
            },
        },
        resetCameras() {
            grid.resets += 1;
        },
        loadVolumeIntoWindows: async () => ({ windows: [0, 1, 2] }),
        refreshOverlays() {},
        readAnnotations: () => [],
        currentHeader: () => null,
        state: { windows: [] },
    };
    return grid;
}

/** Capture console.info for the tests that assert the bootstrap says why. */
function captureInfo(run) {
    const lines = [];
    const original = console.info;
    console.info = (...args) => lines.push(args.map(String).join(' '));
    return Promise.resolve()
        .then(run)
        .finally(() => {
            console.info = original;
        })
        .then(() => lines.join('\n'));
}

const DATA = {
    projectNamespace: 'maxillo',
    fixedMode: true,
    modalityFiles: { cbct: { id: 42, file_key: 'volume_nifti' } },
};

// ---------------------------------------------------------------------------
// Reading the page
// ---------------------------------------------------------------------------

test('a page without the payload or the windows yields nothing', () => {
    assert.equal(readGridData({ getElementById: () => null }), null);
    assert.equal(readWindowElements({ querySelectorAll: () => [] }), null);
});

test('a malformed payload does not take the page down', () => {
    // It is a server bug, but the classification form and the export button are on
    // this page too.
    assert.equal(readGridData({ getElementById: () => ({ textContent: '{oops' }) }), null);
});

test('a partial grid is refused rather than half-mounted', () => {
    const { doc } = fakeDoc({ windows: 3 });
    assert.equal(readWindowElements(doc), null);
});

test('the primary volume is the CBCT when there is one', () => {
    assert.deepEqual(primaryVolumeFrom(DATA), {
        fileId: 42,
        bundleKey: 'volume_nifti',
        filename: 'cbct.nii.gz',
        modality: 'cbct',
    });
});

test('a patient with no volume is ordinary, not an error', () => {
    assert.equal(primaryVolumeFrom({ modalityFiles: {} }), null);
    assert.equal(primaryVolumeFrom({}), null);
    assert.equal(primaryVolumeFrom(null), null);
});

test('a brain page falls back to its default modality', () => {
    const brain = {
        defaultModality: 'braintumor-mri-t1',
        modalityFiles: { 'braintumor-mri-t1': { id: 7 }, 'braintumor-mri-t2': { id: 8 } },
    };
    assert.equal(primaryVolumeFrom(brain).fileId, 7);
});

// ---------------------------------------------------------------------------
// Mounting: unconditional, then sized when visible
// ---------------------------------------------------------------------------

test('an element inside a hidden container is not measurable', () => {
    assert.equal(isMeasurable({ offsetParent: null, clientWidth: 0 }), false);
    assert.equal(isMeasurable({ offsetParent: {}, clientWidth: 0 }), false);
    assert.equal(isMeasurable({ offsetParent: {}, clientWidth: 400 }), true);
    assert.equal(isMeasurable(null), false);
});

test('a hidden grid mounts anyway', async () => {
    // Gating the mount on visibility failed twice over: `#cbct-viewer` is
    // `display: none` unless CBCT is the default modality, and the trigger it waited
    // for cannot arrive -- `patient_detail.js` is a classic script and this is a
    // deferred module, so `ensureCbctViewerReady` runs before `window.CBCTViewer`
    // exists, finds it undefined, and returns. Nothing calls it again.
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    let mounted = 0;
    const grid = await bootstrapVolumeGrid({
        mount: async () => {
            mounted += 1;
            return fakeMountedGrid();
        },
        doc,
    });

    assert.equal(mounted, 1, 'a hidden container must not stop the grid mounting');
    assert.ok(grid);
});

test('a hidden grid is not resized until it is actually on screen', async () => {
    // A camera fitted to a 0x0 viewport is not a camera.
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    const mounted = fakeMountedGrid();
    await bootstrapVolumeGrid({ mount: async () => mounted, doc });

    assert.equal(mounted.renderingEngine.resizes, 0);
    assert.equal(mounted.resets, 0);
});

test('a visible grid is sized and its cameras reset', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: true });
    const mounted = fakeMountedGrid();
    await bootstrapVolumeGrid({ mount: async () => mounted, doc });

    assert.ok(mounted.renderingEngine.resizes >= 1);
    assert.equal(mounted.resets, 1);
});

test('the CBCTViewer hook sizes a grid that has since become visible', async () => {
    const { doc, elements } = fakeDoc({ data: DATA, measurable: false });
    const mounted = fakeMountedGrid();
    await bootstrapVolumeGrid({ mount: async () => mounted, doc });
    assert.equal(mounted.renderingEngine.resizes, 0);

    for (const element of elements) {
        element.offsetParent = {};
        element.clientWidth = 400;
    }
    doc.defaultView.CBCTViewer.init();

    assert.equal(mounted.renderingEngine.resizes, 1);
    assert.equal(mounted.resets, 1);
});

test('the camera is reset only on the FIRST sizing, not on every resize', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: true });
    const mounted = fakeMountedGrid();
    await bootstrapVolumeGrid({ mount: async () => mounted, doc });

    doc.defaultView.CBCTViewer.init();
    doc.defaultView.CBCTViewer.init();

    assert.ok(mounted.renderingEngine.resizes >= 3, 'every nudge resizes');
    assert.equal(mounted.resets, 1, 'a later resize must not throw the view away');
});

test('the CBCTViewer global is merged, not clobbered', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: true });
    doc.defaultView.CBCTViewer = { somethingElse: () => 'kept' };
    await bootstrapVolumeGrid({ mount: async () => fakeMountedGrid(), doc });

    assert.equal(doc.defaultView.CBCTViewer.somethingElse(), 'kept');
    assert.equal(typeof doc.defaultView.CBCTViewer.init, 'function');
});

// ---------------------------------------------------------------------------
// Saying why -- the property whose absence caused a blank viewer to report nothing
// ---------------------------------------------------------------------------

test('a page with no payload says so rather than failing silently', async () => {
    const output = await captureInfo(() => {
        const { doc } = fakeDoc({ data: null });
        return bootstrapVolumeGrid({ mount: async () => fakeMountedGrid(), doc });
    });
    assert.match(output, /no #viewerGridData on this page/);
});

test('an incomplete grid says how many windows it found', async () => {
    const output = await captureInfo(() => {
        const { doc } = fakeDoc({ data: DATA, windows: 2 });
        return bootstrapVolumeGrid({ mount: async () => fakeMountedGrid(), doc });
    });
    assert.match(output, /no complete \.viewer-grid/);
});

test('a patient with no volume says so, and still mounts the grid', async () => {
    const output = await captureInfo(() => {
        const { doc } = fakeDoc({ data: { ...DATA, modalityFiles: {} } });
        return bootstrapVolumeGrid({ mount: async () => fakeMountedGrid(), doc });
    });
    assert.match(output, /no volume to show/);
});

// ---------------------------------------------------------------------------
// Failure reporting
// ---------------------------------------------------------------------------

test('a mount failure is written into every window, not just logged', async () => {
    // WebGL2 missing (decision #13) lands here, and a clinician reads it.
    const { doc, elements, made } = fakeDoc({ data: DATA, measurable: true });
    const result = await bootstrapVolumeGrid({
        mount: async () => {
            throw new Error('WebGL2 is required');
        },
        doc,
    });

    assert.equal(result, null);
    assert.equal(made.length, elements.length);
    assert.ok(made.every((node) => node.textContent === 'WebGL2 is required'));
});

test('showWindowMessage reuses its node rather than stacking messages', () => {
    const existing = { className: '', textContent: 'old', setAttribute() {} };
    const element = { querySelector: () => existing, appendChild: () => assert.fail('appended twice') };
    showWindowMessage(element, 'new');
    assert.equal(existing.textContent, 'new');
});

test('showWindowMessage on a missing element is a no-op', () => {
    assert.doesNotThrow(() => showWindowMessage(null, 'anything'));
});

// ---------------------------------------------------------------------------
// The size observer
// ---------------------------------------------------------------------------

test('without a ResizeObserver, observing is a harmless no-op', () => {
    let called = 0;
    const element = { ownerDocument: { defaultView: {} }, offsetParent: null, clientWidth: 0 };
    const disconnect = observeSize(element, () => {
        called += 1;
    });
    assert.equal(called, 0);
    assert.doesNotThrow(disconnect);
});

test('the observer reports measurability on every size change', () => {
    let observed = null;
    const element = { ownerDocument: null, offsetParent: null, clientWidth: 0 };
    element.ownerDocument = {
        defaultView: {
            ResizeObserver: class {
                constructor(fn) {
                    observed = fn;
                }
                observe() {}
                disconnect() {}
            },
        },
    };

    const seen = [];
    observeSize(element, (measurable) => seen.push(measurable));

    observed();
    assert.deepEqual(seen, [false], 'still hidden');

    element.offsetParent = {};
    element.clientWidth = 400;
    observed();
    assert.deepEqual(seen, [false, true]);
});


// ---------------------------------------------------------------------------
// measurementsUrl
// ---------------------------------------------------------------------------

const URL_DATA = { scanId: 42 };
const URL_VOLUME = { fileId: 7 };

test('the state read names the volume it is reading for', () => {
    // A measurement set is per *patient* and can hold work on several resources at once
    // -- a CBCT and a teleradiography, or a stack of photographs. Unnarrowed, the
    // response is whatever the last save happened to write, so the grid would draw
    // another modality's measurements on this volume, or find none at all once a photo
    // save had been the most recent one.
    const url = new URL(measurementsUrl(URL_DATA, URL_VOLUME, 'maxillo', 'https://h', '/state/'));
    assert.equal(url.pathname, '/maxillo/api/patients/42/measurements/state/');
    assert.equal(url.searchParams.get('fileId'), '7');
});

test('the save posts to the bare endpoint, which names its file in the body', () => {
    const url = new URL(measurementsUrl(URL_DATA, URL_VOLUME, 'maxillo', 'https://h', '/'));
    assert.equal(url.pathname, '/maxillo/api/patients/42/measurements/');
    assert.equal(url.search, '', 'a fileId query on the save would be a second source of truth');
});

test('the global api namespace is not doubled into /api/api/', () => {
    const url = new URL(measurementsUrl(URL_DATA, URL_VOLUME, 'api', 'https://h', '/state/'));
    assert.equal(url.pathname, '/api/patients/42/measurements/state/');
});

test('a volume with no file id still produces a usable url', () => {
    // Narrowing is an optimisation over a correct default: with no fileId the endpoint
    // returns what it always returned, so a missing id must not throw.
    for (const volume of [null, undefined, {}, { fileId: 0 }]) {
        const url = new URL(measurementsUrl(URL_DATA, volume, 'maxillo', 'https://h', '/state/'));
        assert.equal(url.searchParams.has('fileId'), false);
    }
});
