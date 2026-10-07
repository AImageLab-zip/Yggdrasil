import test from 'node:test';
import assert from 'node:assert/strict';

import {
    base64ToBytes,
    bytesToBase64,
    decodePlane,
    encodePlane,
    isEmptyPlane,
    planeFromLabelmap,
    writePlaneToLabelmap,
} from '../imaging/segment/planes.js';

const rect = (w, h, x0, y0, x1, y1) => {
    const plane = new Uint8Array(w * h);
    for (let y = y0; y < y1; y += 1) for (let x = x0; x < x1; x += 1) plane[y * w + x] = 1;
    return plane;
};

test('a plane survives encode and decode', async () => {
    const plane = rect(64, 48, 5, 6, 30, 20);
    const back = await decodePlane(await encodePlane(plane), 64, 48);
    assert.deepEqual(back, plane);
});

test('a large mostly-empty plane compresses far below its raw size', async () => {
    const plane = rect(1920, 1080, 100, 100, 900, 600);
    const encoded = await encodePlane(plane);
    assert.ok(encoded.length < 40_000, `encoded to ${encoded.length} characters`);
});

test('decoding refuses a plane of the wrong size', async () => {
    const encoded = await encodePlane(rect(8, 8, 0, 0, 4, 4));
    await assert.rejects(decodePlane(encoded, 9, 8), /needs 72/);
});

test('base64 round-trips bytes larger than one chunk', () => {
    const bytes = new Uint8Array(100_000).map((_, index) => index % 251);
    assert.deepEqual(base64ToBytes(bytesToBase64(bytes)), bytes);
});

test('emptiness is "no pixel set"', () => {
    assert.equal(isEmptyPlane(new Uint8Array(10)), true);
    assert.equal(isEmptyPlane(rect(4, 4, 1, 1, 2, 2)), false);
});

test('a labelmap becomes a 0/1 plane whatever its segment index', () => {
    assert.deepEqual([...planeFromLabelmap(Uint8Array.from([0, 1, 2, 0, 255]))], [0, 1, 1, 0, 1]);
});

test('a plane is written into a labelmap as the segment index, in place', () => {
    const pixels = new Uint8Array(4).fill(9);
    writePlaneToLabelmap(pixels, Uint8Array.from([1, 0, 1, 0]), 1);
    assert.deepEqual([...pixels], [1, 0, 1, 0]);
    assert.throws(() => writePlaneToLabelmap(pixels, new Uint8Array(3)), /3/);
});
