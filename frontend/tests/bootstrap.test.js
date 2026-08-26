import test from 'node:test';
import assert from 'node:assert/strict';

import {
    bootstrapVolumeGrid,
    isMeasurable,
    observeMeasurable,
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
            handlers: {},
            addEventListener(type, fn) {
                this.handlers[type] = fn;
            },
            querySelector: () => null,
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
            CustomEvent: globalThis.CustomEvent,
        },
        getElementById: (id) =>
            id === 'viewerGridData' && data ? { textContent: JSON.stringify(data) } : null,
        querySelectorAll: () => elements,
        createElement: () => {
            const node = { className: '', textContent: '', setAttribute() {} };
            made.push(node);
            return node;
        },
    };
    for (const element of elements) {
        element.ownerDocument = doc;
    }
    return { doc, elements, made };
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
// The visibility gate -- the bug this exists for
// ---------------------------------------------------------------------------

test('an element inside a hidden container is not measurable', () => {
    // Cornerstone sizes a viewport from its element. Inside `display: none` that is
    // 0x0, and the viewport is built against nothing -- it does not throw, it renders
    // blank forever.
    assert.equal(isMeasurable({ offsetParent: null, clientWidth: 0 }), false);
    assert.equal(isMeasurable({ offsetParent: {}, clientWidth: 0 }), false);
    assert.equal(isMeasurable({ offsetParent: {}, clientWidth: 400 }), true);
    assert.equal(isMeasurable(null), false);
});

test('a hidden grid does NOT mount on load', async () => {
    // #cbct-viewer is display:none unless CBCT is the default modality.
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    let mounted = 0;
    const result = await bootstrapVolumeGrid({ mount: async () => { mounted += 1; return {}; }, doc });

    assert.equal(result, null);
    assert.equal(mounted, 0, 'mounting into a 0x0 container renders blank forever');
});

test('a hidden grid installs the hook patient_detail.js already calls', async () => {
    // `ensureCbctViewerReady` calls window.CBCTViewer.init() when the tab is shown.
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    await bootstrapVolumeGrid({ mount: async () => ({ ok: true }), doc });

    assert.equal(typeof doc.defaultView.CBCTViewer.init, 'function');
    assert.equal(doc.defaultView.CBCTViewer.loading, false);
});

test('a visible grid mounts immediately', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: true });
    let mounted = 0;
    await bootstrapVolumeGrid({
        mount: async () => {
            mounted += 1;
            return { loadVolumeIntoWindow: async () => ({}), state: { windows: [] } };
        },
        doc,
    });
    assert.equal(mounted, 1);
});

test('starting is idempotent: the hook and the observer cannot mount twice', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    let mounted = 0;
    const mount = async () => {
        mounted += 1;
        return { loadVolumeIntoWindow: async () => ({}), state: { windows: [] } };
    };
    await bootstrapVolumeGrid({ mount, doc });

    await doc.defaultView.CBCTViewer.init();
    await doc.defaultView.CBCTViewer.init();
    assert.equal(mounted, 1, 'a second grid over the first');
});

test('the CBCTViewer global is merged, not clobbered', async () => {
    const { doc } = fakeDoc({ data: DATA, measurable: false });
    doc.defaultView.CBCTViewer = { somethingElse: () => 'kept' };
    await bootstrapVolumeGrid({ mount: async () => ({}), doc });

    assert.equal(doc.defaultView.CBCTViewer.somethingElse(), 'kept');
    assert.equal(typeof doc.defaultView.CBCTViewer.init, 'function');
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


test('without a ResizeObserver the observer does nothing, rather than mounting hidden', () => {
    // The tempting fallback is to run the callback anyway, and that is exactly the bug
    // it exists to prevent. CBCTViewer.init() is the primary trigger and needs no
    // observer, so this degrades to "starts when the tab is clicked".
    let called = 0;
    const element = { ownerDocument: { defaultView: {} }, offsetParent: null, clientWidth: 0 };
    const disconnect = observeMeasurable(element, () => { called += 1; });

    assert.equal(called, 0);
    assert.doesNotThrow(disconnect);
});

test('the observer fires once the element gains size', () => {
    let observed = null;
    const element = { ownerDocument: null, offsetParent: null, clientWidth: 0 };
    element.ownerDocument = {
        defaultView: {
            ResizeObserver: class {
                constructor(fn) { observed = fn; }
                observe() {}
                disconnect() {}
            },
        },
    };

    let called = 0;
    observeMeasurable(element, () => { called += 1; });

    observed();
    assert.equal(called, 0, 'still hidden');

    element.offsetParent = {};
    element.clientWidth = 400;
    observed();
    assert.equal(called, 1);
});
