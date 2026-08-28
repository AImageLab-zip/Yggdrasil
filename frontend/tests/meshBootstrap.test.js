import test from 'node:test';
import assert from 'node:assert/strict';

import { bootstrapMeshLandmarks, readMeshData } from '../imaging/mesh/bootstrap.js';

/**
 * The editor, end to end, against a fake viewport.
 *
 * This is what the injected-Cornerstone shape buys: the click-to-save path is exercised
 * with no GPU, no WebGL context and no network. What it cannot reach -- whether a pick
 * lands where the user clicked on a rotated scan -- is named in the roadmap as a browser
 * check, because pretending a fake proves it would be worse than admitting it does not.
 */

const PAYLOAD = {
    patientId: 7,
    projectNamespace: 'maxillo',
    canModify: true,
    meshEndpoint: '/maxillo/api/patient/7/data/',
    landmarkEndpoint: '/maxillo/api/patients/7/ios-landmarks/',
};

function element(id) {
    const listeners = new Map();
    return {
        id,
        hidden: false,
        disabled: false,
        // Matches the template: the axes ship on, the white background off.
        checked: id === 'toggleAxis',
        textContent: '',
        innerHTML: '',
        style: { setProperty() {}, display: '' },
        dataset: {},
        classList: { toggle() {}, add() {}, remove() {} },
        children: [],
        clientWidth: 800,
        clientHeight: 600,
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        getAttribute(name) { return this.attributes[name] ?? null; },
        querySelector() { return null; },
        querySelectorAll() { return this.children; },
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren(...kids) { this.children = kids; },
        append(...kids) { this.children.push(...kids); },
        addEventListener(name, handler) { listeners.set(name, handler); },
        removeEventListener(name) { listeners.delete(name); },
        click() { listeners.get('click')?.({}); },
        fire(name, event) { listeners.get(name)?.(event ?? {}); },
        getContext() { return null; },
    };
}

function fakeDocument(payload = PAYLOAD, { ids = null } = {}) {
    const elements = new Map();
    const known = ids ?? [
        'scan-viewer', 'ios-viewer', 'resetView', 'toggleWireframe', 'toggleGrid',
        'showUpper', 'showLower', 'viewRight', 'viewLeft', 'viewFront', 'viewUpper',
        'viewLower', 'toggleLandmarkMode', 'iosLandmarkWorkbench', 'iosLandmarkStatus',
        'iosLandmarkTeeth', 'iosLandmarkTypes', 'toggleLandmarkVisibility',
        'iosVisualizationMenu', 'iosLandmarkVisibilityWorkbench',
        'landmarkPlaceTool', 'landmarkSelectTool',
        'undoLandmark', 'redoLandmark', 'deleteLandmark', 'saveLandmarks',
        'landmarkSizeRange', 'toggleAxis', 'toggleWhiteBackground',
    ];
    for (const id of known) elements.set(id, element(id));

    const data = element('meshLandmarkData');
    data.textContent = payload === null ? null : JSON.stringify(payload);
    if (payload !== null) elements.set('meshLandmarkData', data);

    return {
        activeElement: { tagName: 'BODY' },
        documentElement: { getAttribute: () => 'dark' },
        getElementById: (id) => elements.get(id) ?? null,
        querySelector: (selector) =>
            selector.includes('csrfmiddlewaretoken') ? { value: 'tok' } : null,
        querySelectorAll: () => [],
        createElement: (tag) => element(tag),
        createTextNode: (text) => ({ text }),
        addEventListener() {},
        removeEventListener() {},
        get: (id) => elements.get(id),
    };
}

/** A viewport that records what it was asked to draw. */
function fakeViewport() {
    const calls = { markers: [], cameras: [], visibility: { upper: true, lower: true } };
    return {
        calls,
        async load() { calls.loaded = true; },
        setMarkers(markers) { calls.markers.push(markers); },
        setJawVisible(jaw, visible) { calls.visibility[jaw] = visible; },
        jawVisibility: () => ({ ...calls.visibility }),
        setWireframe(on) { calls.wireframe = on; },
        setAxesVisible(on) { calls.axes = on; },
        setBackground(name) { calls.background = name; },
        setCamera(name) { calls.cameras.push(name); return null; },
        bounds: () => [-1, 1, -1, 1, -1, 1],
        resize() {},
        destroy() { calls.destroyed = true; },
    };
}

function fakeFetch({ jaws = { upper: {}, lower: {} }, revision = 3, save } = {}) {
    const sent = [];
    const impl = async (url, options) => {
        sent.push({ url, options });
        if (url.endsWith('/data/')) {
            return {
                ok: true,
                json: async () => ({
                    upper_scan_url: '/maxillo/api/file/1/',
                    lower_scan_url: '/maxillo/api/file/2/',
                }),
            };
        }
        if (url.endsWith('state/')) {
            return { ok: true, json: async () => ({ jaws, revision }) };
        }
        return save ?? { ok: true, status: 200, json: async () => ({ revision: revision + 1 }) };
    };
    impl.sent = sent;
    return impl;
}

async function mountFixture(options = {}) {
    const viewport = options.viewport ?? fakeViewport();
    const doc = options.doc ?? fakeDocument();
    const fetchImpl = options.fetchImpl ?? fakeFetch(options.fetch);
    const surface = await bootstrapMeshLandmarks({
        mount: async () => viewport,
        doc,
        fetchImpl,
    });
    return { surface, viewport, doc, fetchImpl };
}

// ---------------------------------------------------------------------------

test('no payload means it declines and says so, rather than throwing', () => {
    assert.equal(readMeshData(fakeDocument(null)), null);
});

test('a malformed payload is refused', () => {
    assert.equal(readMeshData(fakeDocument({ patientId: 7 })), null);
});

test('it mounts, loads both scans and draws the landmarks it was given', async () => {
    const { surface, viewport } = await mountFixture({
        fetch: { jaws: { upper: { 11: { incisal: [1, 2, 3] } }, lower: {} } },
    });
    assert.ok(surface);
    assert.ok(viewport.calls.loaded);
    assert.equal(surface.state.revision, 3);
    // Not drawn yet: the workbench is closed and the eye is off.
    assert.deepEqual(viewport.calls.markers.at(-1), []);
});

test('opening the workbench draws the landmarks', async () => {
    const { surface, doc, viewport } = await mountFixture({
        fetch: { jaws: { upper: { 11: { incisal: [1, 2, 3] } }, lower: {} } },
    });
    doc.get('toggleLandmarkMode').click();
    assert.ok(surface.state.active);
    assert.equal(viewport.calls.markers.at(-1).length, 1);
    assert.deepEqual(viewport.calls.markers.at(-1)[0].position, [1, 2, 3]);
});

test('a mesh that will not render leaves the page alone', async () => {
    const viewport = fakeViewport();
    viewport.load = async () => { throw new Error('no WebGL'); };
    const { surface } = await mountFixture({ viewport });
    assert.equal(surface, null);
});

test('a patient with no scan pair does not mount', async () => {
    const fetchImpl = async (url) =>
        url.endsWith('/data/')
            ? { ok: false, json: async () => ({}) }
            : { ok: true, json: async () => ({ jaws: {}, revision: 0 }) };
    const { surface } = await mountFixture({ fetchImpl });
    assert.equal(surface, null);
});

test('the save body carries both arches and the loaded revision', async () => {
    const { surface, doc, fetchImpl } = await mountFixture();
    surface.state.selectedTooth = '11';
    surface.state.selectedType = 'incisal';
    surface.state.active = true;
    surface.state.canEdit = true;
    // Place through the viewport's callback, the way a real pick arrives.
    surface.state.dirty = false;
    doc.get('saveLandmarks');
    surface.state.document.upper['11'] = { incisal: [1, 2, 3] };
    surface.state.dirty = true;
    await surface.save();

    const post = fetchImpl.sent.find((call) => call.options?.method === 'POST');
    const body = JSON.parse(post.options.body);
    assert.equal(body.expectedRevision, 3);
    assert.deepEqual(body.meshes.map((mesh) => mesh.jaw), ['upper', 'lower']);
    assert.deepEqual(body.meshes[0].landmarks['11'].incisal, [1, 2, 3]);
    assert.equal(post.options.headers['X-CSRFToken'], 'tok');
});

test('a 409 is reported and never retried with a bumped revision', async () => {
    // Retrying would overwrite the other editor's work, which is exactly what the unique
    // constraint exists to prevent.
    const notices = [];
    globalThis.appNotify = (kind, message) => notices.push([kind, message]);
    const { surface, fetchImpl } = await mountFixture({
        fetch: { save: { ok: false, status: 409, json: async () => ({ conflict: true }) } },
    });
    surface.state.document.upper['11'] = { incisal: [1, 2, 3] };
    surface.state.dirty = true;
    await surface.save();
    assert.equal(fetchImpl.sent.filter((call) => call.options?.method === 'POST').length, 1);
    assert.equal(notices.at(-1)[0], 'danger');
    assert.match(notices.at(-1)[1], /Someone else|somebody else/i);
    assert.ok(surface.state.dirty, 'the work is still unsaved, not silently dropped');
    delete globalThis.appNotify;
});

test('a clean save clears dirty and the undo stack', async () => {
    const { surface } = await mountFixture();
    surface.state.document.upper['11'] = { incisal: [1, 2, 3] };
    surface.state.dirty = true;
    await surface.save();
    assert.equal(surface.state.dirty, false);
    assert.equal(surface.state.revision, 4);
});

test('a reader cannot save', async () => {
    const doc = fakeDocument({ ...PAYLOAD, canModify: false });
    const { surface, fetchImpl } = await mountFixture({ doc });
    surface.state.dirty = true;
    await surface.save();
    assert.equal(fetchImpl.sent.filter((call) => call.options?.method === 'POST').length, 0);
});

test('a camera preset reaches the viewport', async () => {
    const { doc, viewport } = await mountFixture();
    doc.get('viewUpper').click();
    assert.equal(viewport.calls.cameras.at(-1), 'upper');
});

test('toggling an arch updates the viewport and the markers', async () => {
    const { doc, viewport } = await mountFixture({
        fetch: { jaws: { upper: { 11: { incisal: [1, 2, 3] } }, lower: {} } },
    });
    doc.get('toggleLandmarkMode').click();
    assert.equal(viewport.calls.markers.at(-1).length, 1);
    doc.get('showUpper').click();
    assert.equal(viewport.calls.visibility.upper, false);
    // The landmarks went with the arch.
    assert.equal(viewport.calls.markers.at(-1).length, 0);
});

test('wireframe reaches the viewport', async () => {
    // The white background is a checkbox in the visualization menu now, not a toolbar
    // button; it has its own test above.
    const { doc, viewport } = await mountFixture();
    doc.get('toggleWireframe').click();
    assert.equal(viewport.calls.wireframe, true);
});

test('a project without the landmark controls still gets a working viewer', async () => {
    // The pre-existing crash this replaces: `ios.js` bound `toggleLandmarkMode` unguarded,
    // so a project with landmarks switched off lost the camera buttons too.
    const doc = fakeDocument(PAYLOAD, {
        ids: ['scan-viewer', 'resetView', 'viewUpper', 'showUpper', 'showLower'],
    });
    const { surface, viewport } = await mountFixture({ doc });
    assert.ok(surface, 'it mounted without the workbench');
    doc.get('viewUpper').click();
    assert.equal(viewport.calls.cameras.at(-1), 'upper');
});

test('the axis toggle is applied at mount and on change', async () => {
    // A checkbox the template ships checked. Binding only the change event would leave the
    // viewport disagreeing with the control until somebody clicked it twice.
    const { doc, viewport } = await mountFixture();
    assert.equal(viewport.calls.axes, true);
    const axis = doc.get('toggleAxis');
    axis.checked = false;
    axis.fire('change');
    assert.equal(viewport.calls.axes, false);
});

test('the per-type visibility list lives in the visualization menu', async () => {
    // A viewer control: which landmark types are drawn changes what a reader sees, and has
    // nothing to do with holding annotation rights.
    const doc = fakeDocument(PAYLOAD, {
        ids: ['scan-viewer', 'iosLandmarkVisibilityWorkbench'],
    });
    await mountFixture({ doc });
    assert.equal(doc.get('iosLandmarkVisibilityWorkbench').children.length, 10);
});

test('the background follows the page theme, and White overrides it', async () => {
    // Two bugs in one: `viewport.setBackground?.()` was an optional call on a method no
    // Cornerstone viewport has, so nothing happened at all; and the fallback was a
    // hardcoded dark, so a scan sat on a dark canvas inside a light page.
    const doc = fakeDocument();
    doc.documentElement = { getAttribute: () => 'light' };
    const { viewport } = await mountFixture({ doc });
    assert.equal(viewport.calls.background, 'light');

    const box = doc.get('toggleWhiteBackground');
    box.checked = true;
    box.fire('change');
    assert.equal(viewport.calls.background, 'white');

    box.checked = false;
    box.fire('change');
    assert.equal(viewport.calls.background, 'light', 'it returned to the theme, not to dark');
});

test('the landmark switches report their state', async () => {
    const { doc, surface } = await mountFixture();
    const visibility = doc.get('toggleLandmarkVisibility');
    assert.equal(visibility.attributes['aria-checked'], 'false');
    visibility.click();
    assert.equal(visibility.attributes['aria-checked'], 'true');
    assert.ok(surface.state.showLandmarks);

    // Annotating implies seeing, so opening the workbench turns visibility on and locks it.
    doc.get('toggleLandmarkMode').click();
    assert.equal(doc.get('toggleLandmarkMode').attributes['aria-checked'], 'true');
    assert.equal(visibility.disabled, true);
});
