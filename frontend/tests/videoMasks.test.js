/**
 * The mask wire format, pinned against the same fixtures the Python tests use.
 *
 * `decodeRuns`/`encodeRuns` mirror `decode_rle`/`encode_rle` in
 * `annotations/services/video.py`. Two implementations of a wire format is the usual way
 * a client and a server come to disagree about the last row, so the fixtures below are
 * deliberately the same ones `annotations/tests_video.py::RunLengthTests` asserts on --
 * including the awkward one, a mask whose first pixel is set, which needs an explicit
 * leading empty run because the format always opens with zeros.
 */

import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
    buildSaveRequest,
    createMaskStore,
    decodeRuns,
    encodeRuns,
    isEmpty,
} from '../imaging/video/masks.js';

test('an empty mask is one run', () => {
    const mask = decodeRuns([12], 4, 3);
    assert.equal(mask.length, 12);
    assert.equal(isEmpty(mask), true);
});

test('a full mask opens with an explicit zero run', () => {
    const mask = decodeRuns([0, 12], 4, 3);
    assert.equal([...mask].every((v) => v === 1), true);
    assert.deepEqual(encodeRuns(mask), [0, 12]);
});

test('runs round trip', () => {
    const mask = new Uint8Array(12);
    mask[5] = 1;
    mask[6] = 1;
    assert.deepEqual(decodeRuns(encodeRuns(mask), 4, 3), mask);
    assert.deepEqual(encodeRuns(mask), [5, 2, 5]);
});

test('a short run list is refused rather than padded', () => {
    // Padding would put every pixel after the shortfall on the wrong row, which reads
    // as a mask that drifted rather than as a malformed message.
    assert.throws(() => decodeRuns([5], 4, 3), /does not describe this frame/);
});

test('an overlong run list is refused', () => {
    assert.throws(() => decodeRuns([4, 40], 4, 3), /overflow/);
});

test('a negative run is refused', () => {
    assert.throws(() => decodeRuns([4, -1, 9], 4, 3), /non-negative/);
});

test('the store drops a plane that has been erased empty', () => {
    // An all-zero plane is the absence of an annotation. Keeping it would send one empty
    // plane per region per frame, and would make "erased" indistinguishable from
    // "annotated with nothing" on the way back in.
    const store = createMaskStore({ width: 2, height: 2 });
    store.set(100, 'Liver', Uint8Array.from([0, 1, 0, 0]));
    assert.deepEqual(store.annotatedTimes(), [100]);
    store.set(100, 'Liver', new Uint8Array(4));
    assert.deepEqual(store.annotatedTimes(), []);
});

test('the store keeps overlapping regions on one frame apart', () => {
    // Cornerstone's labelmap is single-valued per voxel; the record is layered. If these
    // shared a plane, painting one region over another would erase it silently.
    const store = createMaskStore({ width: 2, height: 2 });
    store.set(0, 'Liver', Uint8Array.from([1, 1, 0, 0]));
    store.set(0, 'Gallbladder', Uint8Array.from([1, 0, 0, 0]));
    assert.deepEqual(store.regionsAt(0), ['Gallbladder', 'Liver']);
    assert.equal(store.peek(0, 'Liver')[1], 1);
    assert.equal(store.peek(0, 'Gallbladder')[1], 0);
});

test('the store round-trips a state response', () => {
    const store = createMaskStore({ width: 2, height: 2 });
    store.load([{ timeMs: 40, regions: { Liver: { rle: [1, 1, 2] } } }]);
    assert.deepEqual([...store.peek(40, 'Liver')], [0, 1, 0, 0]);
});

test('the save request carries the whole state, not a delta', () => {
    // The server carries nothing forward -- see save_video_regions -- so a delta would
    // make an erase indistinguishable from an omission.
    const store = createMaskStore({ width: 2, height: 2 });
    store.set(0, 'Liver', Uint8Array.from([1, 0, 0, 0]));
    store.set(80, 'Liver', Uint8Array.from([0, 0, 0, 1]));
    const body = buildSaveRequest({ store, expectedRevision: 3 });
    assert.equal(body.width, 2);
    assert.equal(body.height, 2);
    assert.equal(body.expectedRevision, 3);
    assert.deepEqual(body.frames.map((f) => f.timeMs), [0, 80]);
    assert.deepEqual(body.frames[0].regions.Liver.rle, [0, 1, 3]);
});

test('a mask of the wrong size is refused by the store', () => {
    const store = createMaskStore({ width: 2, height: 2 });
    assert.throws(() => store.set(0, 'Liver', new Uint8Array(9)), /needs 4 values/);
});
