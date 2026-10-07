import test from 'node:test';
import assert from 'node:assert/strict';

import {
    isRedundant,
    markerSegments,
    moveMarker,
    msFromPointer,
    retypeMarker,
    quadrantAt,
    withMarker,
    withoutMarker,
} from '../imaging/segment/quadrants.js';

const labels = new Map([
    ['l1', { name: 'Upper left', color: '#111111' }],
    ['l2', { name: 'Lower right', color: '#222222' }],
]);

test('a marker runs to the next marker, the last to the end of the video', () => {
    const segments = markerSegments(
        [{ timeMs: 5000, code: 'l2' }, { timeMs: 0, code: 'l1' }],
        8000,
        labels
    );
    assert.deepEqual(
        segments.map((s) => [s.startMs, s.endMs, s.name]),
        [[0, 5000, 'Upper left'], [5000, 8000, 'Lower right']]
    );
});

test('no duration, no segments; an unknown code is drawn grey, not dropped', () => {
    assert.deepEqual(markerSegments([{ timeMs: 0, code: 'l1' }], 0, labels), []);
    const [segment] = markerSegments([{ timeMs: 0, code: 'zz' }], 100, labels);
    assert.equal(segment.name, '—');
});

test('quadrantAt is null before the first marker and the last started one after', () => {
    const segments = markerSegments([{ timeMs: 1000, code: 'l1' }, { timeMs: 3000, code: 'l2' }], 9000, labels);
    assert.equal(quadrantAt(segments, 500), null);
    assert.equal(quadrantAt(segments, 1000).code, 'l1');
    assert.equal(quadrantAt(segments, 2999).code, 'l1');
    assert.equal(quadrantAt(segments, 9000).code, 'l2');
});

test('a marker on an occupied instant replaces it, and removal is by instant', () => {
    const start = [{ timeMs: 0, code: 'l1' }];
    assert.deepEqual(withMarker(start, 0, 'l2'), [{ timeMs: 0, code: 'l2' }]);
    assert.deepEqual(withMarker(start, 10, 'l2').map((m) => m.timeMs), [0, 10]);
    assert.deepEqual(withoutMarker(withMarker(start, 10, 'l2'), 0), [{ timeMs: 10, code: 'l2' }]);
});

test('marking the quadrant already in force is redundant', () => {
    const markers = [{ timeMs: 0, code: 'l1' }];
    assert.equal(isRedundant(markers, 500, 'l1'), true);
    assert.equal(isRedundant(markers, 500, 'l2'), false);
    assert.equal(isRedundant([], 500, 'l1'), false);
});

test('moving a needle keeps order, and refuses to land on another marker', () => {
    const markers = [{ timeMs: 0, code: 'l1' }, { timeMs: 4000, code: 'l2' }];
    assert.deepEqual(moveMarker(markers, 4000, 6000), [{ timeMs: 0, code: 'l1' }, { timeMs: 6000, code: 'l2' }]);
    assert.deepEqual(moveMarker(markers, 4000, 2000).map((m) => m.timeMs), [0, 2000]);
    assert.equal(moveMarker(markers, 4000, 0), null);
    assert.equal(moveMarker(markers, 4000, 4000), markers);
    assert.deepEqual(moveMarker(markers, 0, 8000).map((m) => m.code), ['l2', 'l1'], 'it re-sorts');
});

test('changing a marker\'s quadrant touches only that marker', () => {
    const markers = [{ timeMs: 0, code: 'l1' }, { timeMs: 4000, code: 'l1' }];
    assert.deepEqual(retypeMarker(markers, 4000, 'l2'), [{ timeMs: 0, code: 'l1' }, { timeMs: 4000, code: 'l2' }]);
});

test('a pointer maps to an instant, clamped to the bar and snapped to the frame grid', () => {
    const rect = { left: 100, width: 200 };
    assert.equal(msFromPointer(200, rect, 10000), 5000);
    assert.equal(msFromPointer(50, rect, 10000), 0);
    assert.equal(msFromPointer(999, rect, 10000), 10000);
    assert.equal(msFromPointer(203, rect, 10000, 40), 5160, '51.5% -> 5150 ms -> nearest 40 ms frame');
    assert.equal(msFromPointer(200, { left: 0, width: 0 }, 10000), 0);
});
