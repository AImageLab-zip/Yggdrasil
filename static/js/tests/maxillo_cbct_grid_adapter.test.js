'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadAdapter(viewerGrid) {
    const window = { ViewerGrid: viewerGrid };
    const context = {
        window,
        document: {
            getElementById: () => null,
            querySelector: () => null,
            addEventListener: () => {}
        },
        console,
        requestAnimationFrame: callback => callback()
    };
    const source = fs.readFileSync(
        path.join(__dirname, '../modality_viewers/maxillo_cbct_grid_adapter.js'),
        'utf8'
    );
    vm.runInNewContext(source, context);
    return window.CBCTViewer;
}

test('failed optional 3D load clears the shared promise so a retry starts', async () => {
    let loadCount = 0;
    const viewerGrid = {
        windowStates: { 3: { niivueInstance: null } },
        loadModalityInWindow: async () => {
            loadCount += 1;
            throw new Error('temporary load failure');
        }
    };
    const viewer = loadAdapter(viewerGrid);
    viewer.activeFileId = '42';

    const first = viewer.ensureRenderViewer();
    assert.strictEqual(viewer.ensureRenderViewer(), first);
    await assert.rejects(first, /temporary load failure/);
    assert.equal(viewer.renderLoadPromise, null);

    await assert.rejects(viewer.ensureRenderViewer(), /temporary load failure/);
    assert.equal(loadCount, 2);
});

test('successful optional 3D load remains single-shot', async () => {
    let loadCount = 0;
    const niivueInstance = {
        isReady: () => true,
        setLevelWindow: () => {},
        getRenderModeAvailability: () => ({})
    };
    const viewerGrid = {
        windowStates: { 3: { niivueInstance: null } },
        loadModalityInWindow: async () => {
            loadCount += 1;
            viewerGrid.windowStates[3].niivueInstance = niivueInstance;
        },
        setWindowRenderMode: () => ({ available: true, custom: true, message: '' })
    };
    const viewer = loadAdapter(viewerGrid);
    viewer.activeFileId = '42';

    assert.strictEqual(await viewer.ensureRenderViewer(), niivueInstance);
    assert.ok(viewer.renderLoadPromise);
    assert.strictEqual(await viewer.ensureRenderViewer(), niivueInstance);
    assert.equal(loadCount, 1);
});
