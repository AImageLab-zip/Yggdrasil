import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
    CHIP_SELECTOR,
    DRAG_OVER_CLASS,
    bindDragDrop,
    readDroppedModality,
    resolveWindowDropTarget,
} from '../imaging/grid/dragDrop.js';
import {
    descriptorFor,
    dragDropEnabled,
    primaryVolumeFrom,
    volumeFor,
} from '../imaging/grid/bootstrap.js';

// --- what the payload says about a modality ---------------------------------------

const DATA = {
    projectNamespace: 'brain',
    modalityFiles: {
        'braintumor-mri-flair': { id: 11, file_type: 'braintumor_mri_flair_processed' },
        'braintumor-mri-t1c': { id: 12, file_type: 'braintumor_mri_t1c_processed' },
    },
};

test('volumeFor reads the entry for the slug the user picked', () => {
    const volume = volumeFor(DATA, 'braintumor-mri-t1c');
    assert.equal(volume.fileId, 12);
    assert.equal(volume.modality, 'braintumor-mri-t1c');
    // brain sends no file_key or filename; the defaults are what make its plain serve
    // route work (its serve_file raises Http404 for any bundle key).
    assert.equal(volume.bundleKey, 'primary');
    assert.equal(volume.filename, 'braintumor-mri-t1c.nii.gz');
});

test('volumeFor says nothing rather than guessing for a modality with no file', () => {
    assert.equal(volumeFor(DATA, 'braintumor-mri-t2'), null);
    assert.equal(volumeFor(DATA, undefined), null);
    assert.equal(volumeFor({}, 'cbct'), null);
});

test('primaryVolumeFrom still prefers CBCT, then the page default', () => {
    assert.equal(primaryVolumeFrom({ modalityFiles: { cbct: { id: 1 }, ios: { id: 2 } } }).modality, 'cbct');
    assert.equal(
        primaryVolumeFrom({
            defaultModality: 'panoramic',
            modalityFiles: { panoramic: { id: 3 }, ios: { id: 2 } },
        }).modality,
        'panoramic'
    );
});

test('descriptorFor builds the serve URL for a volume', () => {
    const nifti = descriptorFor(volumeFor(DATA, 'braintumor-mri-flair'), {
        namespace: 'brain',
        origin: 'https://ygg.example',
    });
    assert.match(nifti.url, /\/brain\/api\/processing\/files\/serve\/11\/braintumor-mri-flair\.nii\.gz$/);

    // The bundle-member form, which is how a maxillo CBCT display volume is addressed.
    const bundled = descriptorFor(
        {
            fileId: 9,
            modality: 'cbct',
            bundleKey: 'volume_nifti',
            filename: 'cbct.nii.gz',
        },
        { namespace: 'maxillo', origin: 'https://ygg.example' }
    );
    assert.match(bundled.url, /\/9\/key\/volume_nifti\/cbct\.nii\.gz$/);
});

// --- which surface gets drag-and-drop, and what it opens with ----------------------

test('the CBCT grid keeps its fixed layout and its opening load', () => {
    // maxillo/views/patient_detail.py has sent enableDragDrop: False since before 3.0.
    assert.equal(dragDropEnabled({ enableDragDrop: false }), false);
});

test('brain opts in, and a payload that says nothing is treated as opting in', () => {
    assert.equal(dragDropEnabled({ enableDragDrop: true }), true);
    assert.equal(dragDropEnabled({}), true);
    assert.equal(dragDropEnabled(null), true);
});

// --- the handlers -----------------------------------------------------------------

function fakeElement(overrides = {}) {
    const classes = new Set();
    const listeners = new Map();
    const node = {
        dataset: {},
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
        },
        classes,
        listeners,
        addEventListener: (type, handler) => {
            listeners.set(type, handler);
        },
        removeEventListener: (type) => listeners.delete(type),
        contains: () => false,
        closest: () => node,
        ...overrides,
    };
    return node;
}

function fakeTransfer(entries = {}) {
    const store = { ...entries };
    return {
        store,
        setData: (type, value) => {
            store[type] = value;
        },
        getData: (type) => store[type] ?? '',
    };
}

test('a drop loads the dragged modality into the window it landed in', () => {
    const chip = fakeElement({ dataset: { modality: 'braintumor-mri-t1c' } });
    const window0 = fakeElement();
    window0.dataset.windowIndex = '0';
    const window3 = fakeElement();
    window3.dataset.windowIndex = '3';
    window3.closest = () => window3;

    const dropped = [];
    bindDragDrop({
        doc: { querySelectorAll: (selector) => (selector === CHIP_SELECTOR ? [chip] : []) },
        elements: [window0, window3],
        onDrop: (index, slug) => dropped.push([index, slug]),
    });

    const transfer = fakeTransfer();
    chip.listeners.get('dragstart')({ currentTarget: chip, dataTransfer: transfer });
    assert.ok(chip.classes.has('is-dragging'));

    window3.listeners.get('drop')({
        target: window3,
        preventDefault() {},
        dataTransfer: transfer,
    });
    assert.deepEqual(dropped, [[3, 'braintumor-mri-t1c']]);
});

test('dragover highlights the window and permits the drop', () => {
    // Without preventDefault the browser refuses the drop and says nothing at all.
    const element = fakeElement();
    element.dataset.windowIndex = '1';
    let prevented = false;
    bindDragDrop({
        doc: { querySelectorAll: () => [] },
        elements: [element],
        onDrop: () => {},
    });
    element.listeners.get('dragover')({
        target: element,
        preventDefault: () => {
            prevented = true;
        },
        dataTransfer: {},
    });
    assert.ok(prevented);
    assert.ok(element.classes.has(DRAG_OVER_CLASS));
});

test('dragleave keeps the highlight while the pointer is over a child', () => {
    // Otherwise it flickers off every time the pointer crosses the canvas boundary.
    const child = {};
    const element = fakeElement({ contains: (node) => node === child });
    element.dataset.windowIndex = '1';
    bindDragDrop({ doc: { querySelectorAll: () => [] }, elements: [element], onDrop: () => {} });

    element.listeners.get('dragover')({ target: element, preventDefault() {}, dataTransfer: {} });
    element.listeners.get('dragleave')({ target: element, relatedTarget: child });
    assert.ok(element.classes.has(DRAG_OVER_CLASS), 'still over the same window');

    element.listeners.get('dragleave')({ target: element, relatedTarget: {} });
    assert.ok(!element.classes.has(DRAG_OVER_CLASS));
});

test('a drop is read from JSON, and falls back to plain text', () => {
    assert.equal(
        readDroppedModality(fakeTransfer({ 'application/json': '{"modality":"cbct"}' })),
        'cbct'
    );
    assert.equal(
        readDroppedModality(fakeTransfer({ 'text/plain': 'braintumor-mri-t2' })),
        'braintumor-mri-t2'
    );
    assert.equal(readDroppedModality(fakeTransfer({})), null);
});

test('an event on a child resolves to the window that contains it', () => {
    // The events fire on the canvas and the overlay, not on the window.
    const window0 = fakeElement();
    const canvas = { closest: (selector) => (selector.includes('viewer-window') ? window0 : null) };
    assert.equal(resolveWindowDropTarget(canvas), window0);
    assert.equal(resolveWindowDropTarget(null), null);
    assert.equal(resolveWindowDropTarget({}), null);
});

// --- the markup the handlers bind to ----------------------------------------------

test('the brain template still renders draggable chips and drop hints', () => {
    // These survived 3.0 untouched; what was deleted was the code that bound them.
    const html = readFileSync(
        new URL('../../templates/brain/patient_detail_content.html', import.meta.url),
        'utf8'
    );
    assert.ok(html.includes('class="modality-chip"'));
    assert.ok(html.includes('draggable="true"'));
    assert.ok(html.includes('data-modality='));
    assert.equal((html.match(/class="drop-hint"/g) ?? []).length, 4);
    // The segmentation is an overlay, not a series to drop in a window.
    assert.ok(html.includes("m.slug != 'braintumor-mri-seg'"));
});

// --- telling the windows apart ----------------------------------------------------

test('a window says which series it is showing', async () => {
    // Constant on the CBCT grid, and the only way to tell four greyscale MRIs apart on
    // the brain one. `viewer_grid.js` wrote a `.window-label` for the same reason.
    const { modalityText } = await import('../imaging/grid/viewportOverlay.js');
    assert.equal(modalityText('braintumor-mri-t1c'), 'T1C');
    assert.equal(modalityText('braintumor-mri-flair'), 'FLAIR');
    assert.equal(modalityText('cbct'), 'CBCT');
    assert.equal(modalityText(null), '');
});
