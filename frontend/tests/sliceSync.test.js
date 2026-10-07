import test from 'node:test';
import assert from 'node:assert/strict';

import { FREE_LAYOUT, FIXED_CBCT_LAYOUT, ORIENTATIONS } from '../imaging/grid/layout.js';
import {
    beginLoad,
    completeLoad,
    createGridState,
    setFreeScroll,
    setOrientation,
    sliceSyncTargets,
} from '../imaging/grid/windowState.js';
import { bindSliceSync, cameraAtDepthOf } from '../imaging/grid/sliceSync.js';

/** A free-layout grid with `loaded` windows holding a volume each. */
function brainGrid(loaded = [0, 1, 2, 3]) {
    const state = createGridState(FREE_LAYOUT);
    for (const index of loaded) {
        const generation = beginLoad(state, index, { modality: `m${index}`, fileId: index, volumeId: `v${index}` });
        completeLoad(state, index, generation);
    }
    return state;
}

// ---------------------------------------------------------------------------
// The policy
// ---------------------------------------------------------------------------

test('windows on the same plane follow each other, and only those', () => {
    const state = brainGrid();
    setOrientation(state, 3, ORIENTATIONS.SAGITTAL);

    assert.deepEqual(sliceSyncTargets(state, 0), [1, 2]);
    // A sagittal window has no depth in common with the axial ones.
    assert.deepEqual(sliceSyncTargets(state, 3), []);
});

test('free scroll opts a window out in both directions', () => {
    const state = brainGrid();
    setFreeScroll(state, 1, true);

    assert.deepEqual(sliceSyncTargets(state, 0), [2, 3], 'a free window does not follow');
    assert.deepEqual(sliceSyncTargets(state, 1), [], 'and does not lead');
});

test('an empty window neither leads nor follows', () => {
    const state = brainGrid([0, 2]);

    assert.deepEqual(sliceSyncTargets(state, 0), [2]);
    assert.deepEqual(sliceSyncTargets(state, 1), []);
});

test('a window still loading neither leads nor follows', () => {
    // Mounting a series sets its camera to its middle slice; that must not drag the
    // windows the user has already scrolled.
    const state = brainGrid([0, 1]);
    beginLoad(state, 2, { modality: 'flair', fileId: 9, volumeId: 'v9' });

    assert.deepEqual(sliceSyncTargets(state, 2), []);
    assert.deepEqual(sliceSyncTargets(state, 0), [1]);
});

test('the CBCT grid has one window per plane, so nothing syncs', () => {
    const state = createGridState(FIXED_CBCT_LAYOUT);
    for (const index of [0, 1, 2, 3]) {
        const generation = beginLoad(state, index, { volumeId: 'cbct' });
        completeLoad(state, index, generation);
    }
    for (const index of [0, 1, 2, 3]) {
        assert.deepEqual(sliceSyncTargets(state, index), []);
    }
});

// ---------------------------------------------------------------------------
// The geometry
// ---------------------------------------------------------------------------

const AXIAL = [0, 0, 1];

test('the target moves along the normal to the source depth, keeping its pan', () => {
    const source = { focalPoint: [10, 20, 42], viewPlaneNormal: AXIAL };
    const target = { focalPoint: [-5, 3, 30], position: [-5, 3, 530], viewPlaneNormal: AXIAL };

    const camera = cameraAtDepthOf(source, target);

    // Depth (z) follows the source; x/y -- the target's own pan -- do not.
    assert.deepEqual(camera.focalPoint, [-5, 3, 42]);
    assert.deepEqual(camera.position, [-5, 3, 542]);
});

test('an opposite-facing normal is the same axis', () => {
    const source = { focalPoint: [0, 0, 12], viewPlaneNormal: [0, 0, -1] };
    const target = { focalPoint: [0, 0, 2], position: [0, 0, -498], viewPlaneNormal: AXIAL };

    assert.deepEqual(cameraAtDepthOf(source, target).focalPoint, [0, 0, 12]);
});

test('nothing moves across planes, at the same depth, or with a half-built camera', () => {
    const source = { focalPoint: [0, 0, 5], viewPlaneNormal: AXIAL };
    assert.equal(
        cameraAtDepthOf(source, { focalPoint: [0, 0, 0], position: [500, 0, 0], viewPlaneNormal: [1, 0, 0] }),
        null
    );
    assert.equal(cameraAtDepthOf(source, { focalPoint: [9, 9, 5], position: [9, 9, 505], viewPlaneNormal: AXIAL }), null);
    assert.equal(cameraAtDepthOf(source, {}), null);
    assert.equal(cameraAtDepthOf({}, { focalPoint: [0, 0, 0], position: [0, 0, 1], viewPlaneNormal: AXIAL }), null);
});

// ---------------------------------------------------------------------------
// The wiring
// ---------------------------------------------------------------------------

/** A viewport whose camera can be read and set; setting it fires a camera event. */
function fakeViewport(element, z) {
    let camera = { focalPoint: [0, 0, z], position: [0, 0, z + 500], viewPlaneNormal: AXIAL };
    return {
        getCamera: () => camera,
        setCamera(next) {
            camera = { ...camera, ...next };
            element.fire();
        },
        render() {},
    };
}

function fakeElement() {
    const element = {
        handlers: [],
        addEventListener(type, handler) {
            element.handlers.push(handler);
        },
        fire: () => element.handlers.forEach((handler) => handler()),
    };
    return element;
}

test('scrolling one window moves its peers, without echoing back', () => {
    const elements = [fakeElement(), fakeElement(), fakeElement()];
    const viewports = [fakeViewport(elements[0], 10), fakeViewport(elements[1], 10), fakeViewport(elements[2], 10)];
    const peers = { 0: [1, 2], 1: [0, 2], 2: [0, 1] };
    bindSliceSync({
        windows: elements.map((element, index) => [index, element]),
        viewportFor: (index) => viewports[index],
        targetsFor: (index) => peers[index],
    });

    // The user scrolls window 0 to z = 25.
    viewports[0].setCamera({ focalPoint: [0, 0, 25], position: [0, 0, 525] });

    assert.deepEqual(viewports.map((viewport) => viewport.getCamera().focalPoint[2]), [25, 25, 25]);
});

test('syncFrom moves only the windows the policy names', () => {
    const elements = [fakeElement(), fakeElement(), fakeElement()];
    const viewports = [fakeViewport(elements[0], 40), fakeViewport(elements[1], 0), fakeViewport(elements[2], 0)];
    const { syncFrom } = bindSliceSync({
        windows: elements.map((element, index) => [index, element]),
        viewportFor: (index) => viewports[index],
        // Window 2 is in free scroll.
        targetsFor: (index) => (index === 0 ? [1] : []),
    });

    assert.deepEqual(syncFrom(0), [1]);
    assert.deepEqual(viewports.map((viewport) => viewport.getCamera().focalPoint[2]), [40, 40, 0]);
});
