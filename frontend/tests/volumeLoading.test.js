import test from 'node:test';
import assert from 'node:assert/strict';

import {
    describeLoadOutcome,
    initialLoadState,
    readScalarData,
    reduceLoadEvent,
} from '../imaging/grid/volumeLoading.js';

/** One Cornerstone load-status event. */
function event(overrides = {}) {
    return {
        success: true,
        framesLoaded: 1,
        framesProcessed: 1,
        totalNumFrames: 4,
        ...overrides,
    };
}

function fold(events) {
    return events.reduce(reduceLoadEvent, initialLoadState());
}

// ---------------------------------------------------------------------------
// The three rules, each of which was wrong in the version that shipped.
// ---------------------------------------------------------------------------

test('completion is the frame count, NOT the arrival of a callback', () => {
    // `callLoadStatusCallback` fires once per frame. Treating the first one as "done"
    // is what produced `cached 0` on 20 of 31 studies in the first real run.
    const partial = fold([event({ framesProcessed: 1 }), event({ framesProcessed: 2 })]);
    assert.equal(partial.done, false);
    assert.equal(partial.framesProcessed, 2);

    const complete = fold([
        event({ framesProcessed: 1 }),
        event({ framesProcessed: 2 }),
        event({ framesProcessed: 3 }),
        event({ framesProcessed: 4 }),
    ]);
    assert.equal(complete.done, true);
});

test('a failed frame still counts as processed, so a bad frame cannot hang the wait', () => {
    // `errorCallback` increments framesProcessed and can drive the volume to "loaded"
    // with frames missing. Waiting for framesLoaded to reach the total would hang here
    // forever.
    const state = fold([
        event({ framesProcessed: 1, framesLoaded: 1 }),
        event({ success: false, imageId: 'nifti:x?frame=1', error: 'HTTP 500', framesProcessed: 2, framesLoaded: 1 }),
        event({ framesProcessed: 3, framesLoaded: 2 }),
        event({ framesProcessed: 4, framesLoaded: 3 }),
    ]);
    assert.equal(state.done, true, 'the wait must terminate');
    assert.equal(state.framesLoaded, 3);
    assert.equal(state.failures.length, 1);
});

test('failures are collected, and a volume with gaps is refused rather than measured', () => {
    const state = fold([
        event({ success: false, imageId: 'nifti:x?frame=0', error: 'HTTP 500', framesProcessed: 4 }),
    ]);
    const outcome = describeLoadOutcome(state);
    assert.equal(outcome.ok, false);
    assert.match(outcome.message, /1 of 4 frames failed to load/);
    assert.match(outcome.message, /nifti:x\?frame=0/);
    assert.match(outcome.message, /the gaps read as data/);
});

test('a clean, complete load is ok', () => {
    const outcome = describeLoadOutcome(fold([event({ framesProcessed: 4, framesLoaded: 4 })]));
    assert.deepEqual(outcome, { ok: true, message: null });
});

test('an incomplete load says how far it got', () => {
    const outcome = describeLoadOutcome(fold([event({ framesProcessed: 2 })]));
    assert.equal(outcome.ok, false);
    assert.match(outcome.message, /2 of 4 frames were processed/);
});

test('frame counters never go backwards', () => {
    // Events can arrive out of order; a later event reporting a lower count must not
    // un-complete a load that has finished.
    const state = fold([event({ framesProcessed: 4 }), event({ framesProcessed: 2 })]);
    assert.equal(state.framesProcessed, 4);
});

test('an event with no totals at all leaves the load incomplete', () => {
    // Better to time out with a clear message than to call an unknown state done.
    const state = fold([{ success: true }]);
    assert.equal(state.done, false);
    assert.equal(describeLoadOutcome(state).ok, false);
});

// ---------------------------------------------------------------------------
// readScalarData -- the guard on the silent-empty return
// ---------------------------------------------------------------------------

function volumeWith(data, dimensions = [2, 2, 2]) {
    return {
        dimensions,
        voxelManager: { getCompleteScalarDataArray: () => data },
    };
}

test('an empty scalar array is refused, and named as a read-before-load', () => {
    // `getCompleteScalarDataArray` returns `new Uint8Array(0)` when no slice has data
    // (VoxelManager.js:643-647). It does not throw, so a caller that trusts it computes
    // statistics over nothing and reports them.
    assert.throws(
        () => readScalarData(volumeWith(new Uint8Array(0))),
        /cached no voxels at all[\s\S]*read before the load completed/
    );
});

test('a short array is refused rather than measured with the tail read as zeros', () => {
    assert.throws(
        () => readScalarData(volumeWith(new Int16Array(4), [2, 2, 2])),
        /cached 4 voxels but its dimensions imply 8/
    );
});

test('a complete array is returned', () => {
    const data = new Int16Array(8).fill(7);
    assert.equal(readScalarData(volumeWith(data)), data);
});

test('a volume with no voxel manager is refused', () => {
    assert.throws(() => readScalarData({ dimensions: [2, 2, 2] }), /no voxel manager/);
    assert.throws(() => readScalarData(undefined), /no voxel manager/);
});

test('a volume whose dimensions are unknown still rejects the empty case', () => {
    // The length check needs dimensions; the empty check does not, and the empty case
    // is the one that actually happened.
    assert.throws(
        () => readScalarData({ dimensions: undefined, voxelManager: { getCompleteScalarDataArray: () => new Uint8Array(0) } }),
        /cached no voxels at all/
    );
});
