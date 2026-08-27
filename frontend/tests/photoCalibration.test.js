import test from 'node:test';
import assert from 'node:assert/strict';

import {
    MIN_PIXEL_DISTANCE,
    calibrationRequest,
    formatCalibration,
    pixelSpacingFromKnownLength,
    recalibrationWarning,
} from '../imaging/photos/calibration.js';

test('ten millimetres over a hundred pixels is a tenth', () => {
    const { mmPerPixel, pixelDistance } = pixelSpacingFromKnownLength({
        pointA: [0, 0],
        pointB: [100, 0],
        knownLengthMm: 10,
    });
    assert.equal(mmPerPixel, 0.1);
    assert.equal(pixelDistance, 100);
});

test('the distance is euclidean, not axis-aligned', () => {
    const { pixelDistance } = pixelSpacingFromKnownLength({
        pointA: [0, 0],
        pointB: [30, 40],
        knownLengthMm: 10,
    });
    assert.equal(pixelDistance, 50);
});

test('a sub-pixel line is refused rather than producing a huge scale', () => {
    // Not merely "not zero": a line a fraction of a pixel long divides a real length by
    // almost nothing, and the resulting scale is enormous, confident, and entirely a
    // function of where two clicks landed.
    assert.throws(
        () => pixelSpacingFromKnownLength({ pointA: [10, 10], pointB: [10.4, 10], knownLengthMm: 10 }),
        /Draw a longer line/
    );
    assert.equal(MIN_PIXEL_DISTANCE, 1.0);
});

test('a non-positive or non-finite length is refused', () => {
    for (const knownLengthMm of [0, -5, NaN, Infinity, '10', null, undefined]) {
        assert.throws(
            () => pixelSpacingFromKnownLength({ pointA: [0, 0], pointB: [100, 0], knownLengthMm }),
            undefined,
            `${knownLengthMm} must not become a scale`
        );
    }
});

test('malformed points are refused', () => {
    for (const pointA of [[0], [0, 0, 0], [0, NaN], 'nope', null, [0, '5']]) {
        assert.throws(() =>
            pixelSpacingFromKnownLength({ pointA, pointB: [100, 0], knownLengthMm: 10 })
        );
    }
});

test('the request body carries the points, never the computed scale', () => {
    // The server derives it and ignores a client value; sending one anyway would create a
    // second apparent source of truth that a later reader has to discover is ignored.
    const body = calibrationRequest({ pointA: [0, 0], pointB: [100, 0], knownLengthMm: 10 });
    assert.deepEqual(body, { pointA: [0, 0], pointB: [100, 0], knownLengthMm: 10 });
    assert.ok(!('mmPerPixel' in body));
    assert.ok(!('pixelSpacingMm' in body));
});

test('an unusable measurement never becomes a request', () => {
    assert.throws(() =>
        calibrationRequest({ pointA: [0, 0], pointB: [0, 0], knownLengthMm: 10 })
    );
});

test('the readout names its provenance', () => {
    // "0.142 mm/px" alone invites more confidence than a line somebody drew has earned.
    const text = formatCalibration({
        x_mm: 0.1423,
        known_length_mm: 10,
        calibrated_by: 'jdoe',
    });
    assert.match(text, /0\.142 mm\/px/);
    assert.match(text, /from 10 mm/);
    assert.match(text, /jdoe/);
});

test('an uncalibrated image formats as nothing, not as zero', () => {
    for (const record of [null, undefined, {}, { x_mm: 'nope' }, { x_mm: NaN }]) {
        assert.equal(formatCalibration(record), '');
    }
});

test('a recalibration that affects nothing warns about nothing', () => {
    assert.equal(recalibrationWarning(0), '');
    assert.equal(recalibrationWarning(undefined), '');
});

test('a recalibration warning says the shapes are unchanged', () => {
    // The distinction that matters: the stored numbers are pixels and stay correct, and
    // it is their millimetre *reading* that moves. A warning implying the measurements
    // themselves changed would send somebody looking for corruption.
    const text = recalibrationWarning(3);
    assert.match(text, /3 saved measurements/);
    assert.match(text, /shapes are unchanged/);
    assert.match(recalibrationWarning(1), /1 saved measurement\b/);
});
