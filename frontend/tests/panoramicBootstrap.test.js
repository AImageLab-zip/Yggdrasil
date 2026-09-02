import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { GRID_READY_EVENT } from '../imaging/grid/bootstrap.js';
import { ANNOUNCE_TYPE, OUTCOMES } from '../imaging/panoramic/savePayload.js';
import { CONTROL_IDS } from '../imaging/panoramic/controls.js';
import { bootstrapPanoramic } from '../imaging/panoramic/bootstrap.js';

/**
 * The port of `static/js/tests/cbct_panorex_editor.test.js`.
 *
 * The roadmap says port it, do not delete it, and the reason is that the behaviours it
 * locks are the ones nothing else checks: the editor is never opened on load, a patient
 * with no panoramic gets one silently, and every path out announces so the warm-up harness
 * does not wait out a five-minute timeout per patient.
 *
 * What changed in the port is only the mechanism -- the surface reads `#viewerGridData`
 * instead of `window.ViewerGrid`, and mounts Cornerstone instead of Konva. The assertions
 * are the same claims.
 */

// ------------------------------------------------------------------- harness

function makeElement(id) {
    const listeners = new Map();
    return {
        id,
        hidden: false,
        disabled: false,
        textContent: '',
        dataset: {},
        style: {},
        classList: { toggle() {} },
        setAttribute() {},
        addEventListener(name, handler) { listeners.set(name, handler); },
        querySelectorAll: () => [],
        listeners,
    };
}

/**
 * A document holding the panoramic section, its controls and the grid payload.
 *
 * @param {object} options
 * @param {object} [options.source] the `panorexSource` payload.
 * @param {boolean} [options.canEdit]
 * @param {boolean} [options.locked]
 * @param {boolean} [options.globalsSet] whether `window.canEdit`/`window.scanId` have
 *   been assigned yet. **False is the real page**: they are written inside
 *   `patient_detail.js`'s `DOMContentLoaded` handler and the entry is a deferred module,
 *   which runs first.
 */
function buildHarness({ source, canEdit = true, locked = false, globalsSet = false, mountImpl } = {}) {
    const elements = new Map();
    for (const id of Object.values(CONTROL_IDS)) {
        elements.set(id, makeElement(id));
    }
    const root = elements.get(CONTROL_IDS.root);
    root.dataset.canEdit = canEdit ? 'true' : 'false';
    root.dataset.panoramicLocked = locked ? 'true' : 'false';
    root.querySelectorAll = () => [];

    const data = {
        scanId: 4242,
        projectNamespace: 'maxillo',
        modalityFiles: { cbct: { id: 12, file_key: 'volume_nifti', filename: 'v.nii.gz' } },
        panorexSource: source === undefined
            ? {
                jobId: 7,
                volumeFileId: 12,
                volumeFileKey: 'volume_nifti',
                volumeFileHash: 'a'.repeat(64),
                segmentationFileId: 13,
                segmentationFileKey: 'segmentation_nifti',
                segmentationFileHash: 'b'.repeat(64),
                revision: 0,
                state: null,
            }
            : source,
    };

    const posted = [];
    const events = [];
    const saves = [];
    const view = {
        ...(globalsSet ? { scanId: 4242, canEdit: true } : {}),
        Worker: class {},
        location: { origin: 'https://ygg.test' },
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
        dispatchEvent(event) { events.push(event.detail); return true; },
        listeners: new Map(),
        addEventListener(name, handler) { view.listeners.set(name, handler); },
        removeEventListener(name) { view.listeners.delete(name); },
        parent: null,
        PanoramicViewer: { refreshAfterSave() {} },
    };
    view.parent = { postMessage: (payload) => posted.push(payload) };

    const doc = {
        defaultView: view,
        getElementById: (id) => elements.get(id) ?? null,
        querySelector: (selector) =>
            (selector.includes('csrfmiddlewaretoken') ? { value: 'csrf-token' } : null),
    };
    // The payload element, read as JSON by the bootstrap.
    elements.set('viewerGridData', { textContent: JSON.stringify(data) });
    // And the page's own facts, which the bootstrap reads for the same reason: the
    // globals above do not exist yet when a deferred module runs.
    elements.set('django-data', {
        textContent: JSON.stringify({ canEdit, scanId: 4242 }),
    });

    const mounts = [];
    const mount = mountImpl ?? defaultMount(mounts, saves);

    return { doc, view, elements, events, posted, saves, mounts, mount, data };
}

/**
 * A mount that stands in for Cornerstone, vtk and the worker.
 *
 * It records what the surface asked for and lets a test drive the worker's replies, the
 * way the deleted harness drove `postMessage`.
 */
function defaultMount(mounts, saves) {
    return async ({ plan, onReady, onGeometry, onError }) => {
        const requests = [];
        const drawn = { planes: [], arches: [], masks: [], strips: [] };
        const mounted = {
            descriptor: {
                dimensions: { width: 2, height: 2, depth: 2 },
                affine: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                flipZ: false, slope: 1, intercept: 0,
            },
            worker: { request: (z, controlPoints) => requests.push({ z, controlPoints }) },
            arch: {
                showPlane: (point) => drawn.planes.push(point),
                setArch: (points) => drawn.arches.push(points),
                setMask: (mask) => drawn.masks.push(mask),
            },
            cpr: { setArch: (options) => drawn.strips.push(options), setMode() {} },
            worldFor: (point) => [point[0], point[1], 0],
            projectStrips: async () => ({ mip: new Float32Array(4), raysum: new Float32Array(4), width: 2, height: 2 }),
            encode: () => ({ mip: { width: 2, height: 2 }, raysum: { width: 2, height: 2 } }),
            // Real blobs: `saveBody` builds a real `FormData`, which refuses anything else,
            // and a fake that skipped that would not be exercising the request the endpoint
            // actually receives.
            encodeBlobs: async () => ({
                mip: new Blob(['MIP'], { type: 'image/png' }),
                raysum: new Blob(['RAY'], { type: 'image/png' }),
            }),
            paint() {},
            destroy() {},
            adopt() {},
            // Test handles.
            requests,
            drawn,
            plan,
            ready: onReady,
            geometry: onGeometry,
            fail: onError,
        };
        mounts.push(mounted);
        return mounted;
    };
}

/** Drive the worker through init and one arch reply, as the real one would. */
function completeGeneration(mounted, { autoZ = 96, z = autoZ } = {}) {
    mounted.ready({ dimensions: { width: 2, height: 2, depth: 200 }, autoZ, flipZ: false });
    mounted.geometry({
        z,
        source: 'auto',
        controlPoints: [[1, 1], [2, 2], [3, 3], [4, 4]],
        spline: [[1, 1], [2, 2]],
        centerline: [[1, 1], [2, 2]],
        slab: [[[0, 0], [1, 1]], [[0, 0], [1, 1]]],
        mask: new Uint8Array(4),
    });
}

function okFetch(saves, body = { revision: 1 }) {
    return async (url, options) => {
        saves.push({ url, options });
        return { ok: true, status: 200, json: async () => body };
    };
}

// --------------------------------------------------------------------- tests

test('the editor is not shown on load when no panoramic exists', async () => {
    const harness = buildHarness();

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });

    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, true);
    // Hidden, but started: the silent pass is under way.
    assert.equal(harness.mounts.length, 1);
});

test('the editor is not shown on load when a panoramic already exists', async () => {
    const harness = buildHarness({
        source: {
            volumeFileId: 12, volumeFileKey: 'volume_nifti', segmentationFileId: 13,
            segmentationFileKey: 'segmentation_nifti', revision: 3,
            state: { algorithmVersion: 'panorex-js-v2-mip' },
        },
    });

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });

    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, true);
    // Nothing to generate, so nothing is mounted: no worker, no second viewport, no
    // volume read, on every page view of every patient that already has a panoramic.
    assert.equal(harness.mounts.length, 0);
    assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.EXISTING]);
});

test('the toolbar is inert until the editor is mounted', async () => {
    // `bindControls` wires the Z slider, prev/next and Reset auto unconditionally, but the
    // saved-panoramic path returns before `mount()` -- so `descriptor` and `mounted` are
    // still null. Touching a control used to be `Cannot read properties of null (reading
    // 'dimensions')` / `(reading 'worker')`, on a page that had done nothing wrong.
    const harness = buildHarness({
        source: {
            volumeFileId: 12, volumeFileKey: 'volume_nifti', segmentationFileId: 13,
            segmentationFileKey: 'segmentation_nifti', revision: 3,
            state: { algorithmVersion: 'panorex-js-v2-mip' },
        },
    });

    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    assert.equal(harness.mounts.length, 0, 'nothing was mounted, which is the precondition');

    surface.setSlice(40);
    surface.setSlice(surface.state().slice - 1);
    surface.resetAuto();
    surface.editArch([[1, 1], [2, 2]]);

    // A no-op, not a throw: there is no volume to re-fit against until Edit is clicked.
    assert.equal(surface.state().slice, 0);
    assert.equal(surface.state().hasGeometry, false);
});

test('a patient with no panoramic gets a MIP default from the automatic arch', async () => {
    const harness = buildHarness();

    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    const mounted = harness.mounts[0];
    completeGeneration(mounted);
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The automatic arch on the automatic slice: no control points, and the worker's own Z.
    assert.deepEqual(mounted.requests, [{ z: 96, controlPoints: null }]);
    assert.equal(harness.saves.length, 1);
    const state = JSON.parse(harness.saves[0].options.body.get('state'));
    assert.equal(state.default_mode, 'mip');
    assert.equal(state.geometry_source, 'auto');
    assert.equal(state.axial_slice, 96);
    assert.equal(state.base_revision, 0);
    assert.deepEqual(state.volume_shape, [2, 2, 2]);
    assert.equal(harness.saves[0].url, '/maxillo/api/patient/4242/panoramic/generated/');
    assert.equal(surface.state().mode, 'mip');
});

test('the editor stays hidden after an unattended save', async () => {
    const harness = buildHarness();

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });
    completeGeneration(harness.mounts[0]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, true);
    assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.CREATED]);
});

test('entering edit mode reveals the editor', async () => {
    const harness = buildHarness();

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });
    await harness.view.CBCTPanorexEditor.enterEditMode();

    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, false);
});

test('a locked patient cannot enter edit mode', async () => {
    const harness = buildHarness({ locked: true });

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });
    const entered = harness.view.CBCTPanorexEditor.enterEditMode();

    // The Edit button is not rendered for a locked patient; this is the programmatic
    // entry point, which the warm-up harness and the console both reach.
    assert.equal(entered, false);
    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, true);
});

test('a missing segmentation is not reported as an error to the reader', async () => {
    const harness = buildHarness({
        mountImpl: async () => { throw new Error('No paired panoramic segmentation source is available'); },
    });

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });

    // Most often the CBCT job is still running. The next visit tries again, and in the
    // meantime nothing on the patient page says anything went wrong.
    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, true);
    assert.notEqual(harness.elements.get(CONTROL_IDS.error).hidden, false);
    assert.equal(harness.saves.length, 0);
    assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.SKIPPED]);
});

// ------------------------------------------------ what the port adds over it

test('every declining path announces, on both channels', async () => {
    for (const harness of [
        buildHarness({ canEdit: false }),
        buildHarness({ source: null }),
        buildHarness({ source: { volumeFileId: 12, segmentationFileId: null } }),
    ]) {
        await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves) });

        // Not decoration: a patient page that never announces costs the warm-up run a
        // five-minute timeout, and a folder of them is an afternoon.
        assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.SKIPPED]);
        assert.deepEqual(harness.events.map((entry) => entry.type), [ANNOUNCE_TYPE]);
        assert.equal(harness.mounts.length, 0);
    }
});

test('the page decides who is looking, not a global that has not been set yet', async () => {
    // The bug this pins. `window.canEdit` is assigned inside `patient_detail.js`'s
    // `DOMContentLoaded` handler; `{% cornerstone_entry %}` emits a *module* script,
    // which is deferred and therefore runs before that handler. `view.canEdit` was
    // `undefined` on every visit by every user, this gate refused, and no patient page
    // ever generated its default panoramic. The harness leaves the globals unset for
    // exactly that reason -- so the surface has to read the page.
    const harness = buildHarness();
    assert.equal(harness.view.canEdit, undefined, 'the harness must reproduce the race');

    await bootstrapPanoramic({
        doc: harness.doc,
        mount: harness.mount,
        fetchImpl: okFetch(harness.saves),
    });

    assert.equal(harness.mounts.length, 1, 'the unattended pass must run');
});

test('a page that states no rights is still refused, whatever the globals say', async () => {
    const harness = buildHarness({ canEdit: false, globalsSet: true });
    await bootstrapPanoramic({
        doc: harness.doc,
        mount: harness.mount,
        fetchImpl: okFetch(harness.saves),
    });
    assert.equal(harness.mounts.length, 0);
    assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.SKIPPED]);
    // And it names the patient it is skipping. `window.scanId` is set by the same
    // handler as `window.canEdit`, so the same race made every announcement carry
    // `patientId: null` -- which left the warm-up run unable to tell one skip from
    // another across a whole folder.
    assert.deepEqual(harness.posted.map((entry) => entry.patientId), [4242]);
});

test('a silent save that conflicts reports existing, not failed', async () => {
    const harness = buildHarness();
    const conflict = async () => ({ ok: false, status: 409, json: async () => ({ error: 'Stale' }) });

    await bootstrapPanoramic({ doc: harness.doc, mount: harness.mount, fetchImpl: conflict });
    completeGeneration(harness.mounts[0]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Another tab, or an earlier visit, already wrote the default for this exact source.
    assert.deepEqual(harness.posted.map((entry) => entry.outcome), [OUTCOMES.EXISTING]);
});

test('a reader taking over stops the pass behaving silently', async () => {
    const harness = buildHarness();

    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    await surface.activate();
    completeGeneration(harness.mounts[0]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The editor is on screen and driven by hand, so the save is the reader's and is not
    // announced to a harness that is no longer the one driving.
    assert.equal(harness.elements.get(CONTROL_IDS.root).hidden, false);
    assert.equal(surface.state().autoMode, false);
    assert.equal(harness.posted.length, 0);
});

test('dragging the arch reformats live without waiting for the worker', async () => {
    const harness = buildHarness();
    const reformats = [];
    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    await surface.activate();
    const mounted = harness.mounts[0];
    mounted.cpr.setArch = (options) => reformats.push(options.geometry.source);
    mounted.core = {
        buildEditedGeometry: (points) => ({
            source: 'custom_cp', centerline: points, slab: points.map(() => [[0, 0], [1, 1]]),
        }),
    };
    completeGeneration(mounted);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const before = mounted.requests.length;
    // The settled arch has already been reformatted once, from the worker's reply.
    reformats.length = 0;

    surface.dragArch([[1, 1], [2, 2], [3, 3], [4, 4]]);

    // The strip follows the handles now, from the same fit the worker would do. Waiting
    // for a round trip instead is the lag this phase exists to remove -- and nothing here
    // is authoritative: the release re-requests, and the bake uses that reply.
    assert.deepEqual(reformats, ['custom_cp']);
    assert.equal(mounted.requests.length, before);
});

test('an arch dragged into an unfittable shape is not an error banner', async () => {
    const harness = buildHarness();
    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    await surface.activate();
    const mounted = harness.mounts[0];
    mounted.core = { buildEditedGeometry: () => { throw new Error('too few samples'); } };
    completeGeneration(mounted);
    await new Promise((resolve) => setTimeout(resolve, 0));

    surface.dragArch([[1, 1], [1, 1], [1, 1], [1, 1]]);

    // The handles are still moving and the next position usually fits. The release
    // re-requests through the worker, which does report.
    assert.notEqual(harness.elements.get(CONTROL_IDS.error).hidden, false);
});

test('switching projection re-arms the save with a fresh generation id', async () => {
    const harness = buildHarness();

    const surface = await bootstrapPanoramic({
        doc: harness.doc, mount: harness.mount, fetchImpl: okFetch(harness.saves),
    });
    await surface.activate();
    completeGeneration(harness.mounts[0]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await surface.save();
    surface.setMode('raysum');
    await surface.save();

    const states = harness.saves.map((entry) => JSON.parse(entry.options.body.get('state')));
    assert.equal(states.length, 2);
    assert.equal(states[0].default_mode, 'mip');
    assert.equal(states[1].default_mode, 'raysum');
    // The endpoint keys idempotency on the generation uuid, and the default mode is part
    // of what a save records -- so reusing it would be answered 409, not 200.
    assert.notEqual(states[1].generation_uuid, states[0].generation_uuid);
    // And the second quotes the revision the first was given, or it is a stale writer.
    assert.equal(states[1].base_revision, 1);
});

// --- the way in, and what is behind it -------------------------------------------

test('the Edit arch button waits for the CBCT, then appears', async () => {
    // Clicking it before the volume is in the cache puts "The CBCT is still loading." on
    // screen. The template ships it hidden and the surface is what offers it, so a page
    // whose grid never finishes never shows a button that cannot work.
    const harness = buildHarness();
    const button = harness.elements.get(CONTROL_IDS.editButton);

    await bootstrapPanoramic({ mount: harness.mount, doc: harness.doc, fetchImpl: okFetch(harness.saves) });
    assert.equal(button.hidden, true, 'no way in while the grid is still loading');

    harness.view.listeners.get(GRID_READY_EVENT)();
    assert.equal(button.hidden, false);
});

test('a grid that was already loaded is not waited for', async () => {
    // The two bundles start in no fixed order, so the event may have been dispatched
    // before this one ever subscribed. The flag is the record of it.
    const harness = buildHarness();
    harness.view.CBCTViewer = { ready: true };

    await bootstrapPanoramic({ mount: harness.mount, doc: harness.doc, fetchImpl: okFetch(harness.saves) });
    assert.equal(harness.elements.get(CONTROL_IDS.editButton).hidden, false);
});

test('a reader opening the editor sees the arch the unattended pass fitted', async () => {
    // The pass draws nothing -- there is no editor on screen to draw into -- so the
    // geometry it leaves behind has never been handed to a viewport. `activate` returning
    // on `if (geometry)` showed the reader an empty axial and a spline they could not grab.
    const harness = buildHarness();
    const surface = await bootstrapPanoramic({
        mount: harness.mount, doc: harness.doc, fetchImpl: okFetch(harness.saves),
    });
    const mounted = harness.mounts[0];
    completeGeneration(mounted);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(mounted.drawn.arches, [], 'nothing is drawn while nobody is watching');

    await surface.activate();

    assert.equal(mounted.drawn.arches.length, 1, 'the control points reach the axial');
    assert.equal(mounted.drawn.masks.length, 1, 'and so does the mandible they were fitted to');
    assert.equal(mounted.drawn.planes.length, 1, 'on the slice the arch is on');
    assert.equal(mounted.drawn.strips.length, 1, 'and the strip is rebuilt for the live pane');
});

test('the entry re-measures both stages when they become visible', async () => {
    // Not unit-testable -- it is the entry, and the entry imports Cornerstone and vtk --
    // but it is the wiring the whole defect turned on, so the shape of it is pinned here.
    // Both stages are `hidden` when their viewports are enabled, and the editor's is
    // hidden for the entire unattended pass; without an observer the canvas stays at the
    // 300x150 default it was built with and the axial, the mask and the spline handles end
    // up crammed into the top-left corner of the box they are displayed in.
    const entry = await readFile(new URL('../entries/panoramic-cpr.js', import.meta.url), 'utf8');

    assert.match(entry, /observeSize\(stage/, 'both stages are observed');
    assert.match(entry, /\[plan\.axialStage, plan\.cprStage\]/);
    assert.match(entry, /renderingEngine\.resize\(true, true\)/, 'one call covers the shared engine');
    assert.match(entry, /arch\.reframe\(!sized\)/, 'the camera is refit only on the first real sizing');
    assert.match(entry, /cpr\.reframe\(\)/);
});
