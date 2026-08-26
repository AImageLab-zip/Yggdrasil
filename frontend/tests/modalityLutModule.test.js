import test from 'node:test';
import assert from 'node:assert/strict';

import {
    normalizeScaling,
    modalityLutModule,
    applyModalityLut,
    upstreamWouldSkipRescale,
    upstreamIsWrongFor,
} from '../imaging/metadata/modalityLutModule.js';

/**
 * The four branches the upstream predicate `slope !== 1 && inter !== 0` splits on.
 * Each row is a real encoding, not a synthetic edge case.
 */
const BRANCHES = [
    {
        name: 'uint16 plus intercept -- the ordinary CT/CBCT encoding',
        header: { scl_slope: 1, scl_inter: -1024 },
        lut: { rescaleSlope: 1, rescaleIntercept: -1024 },
        upstreamSkips: true,
        upstreamWrong: true, // off by exactly 1024 HU, silently
    },
    {
        name: 'pure gain, no offset',
        header: { scl_slope: 2, scl_inter: 0 },
        lut: { rescaleSlope: 2, rescaleIntercept: 0 },
        upstreamSkips: true,
        upstreamWrong: true, // every voxel halved
    },
    {
        name: 'already in modality units',
        header: { scl_slope: 1, scl_inter: 0 },
        lut: { rescaleSlope: 1, rescaleIntercept: 0 },
        upstreamSkips: true,
        upstreamWrong: false, // skipping identity is harmless
    },
    {
        name: 'float gain and offset -- the only case upstream gets right',
        header: { scl_slope: 0.5, scl_inter: -100 },
        lut: { rescaleSlope: 0.5, rescaleIntercept: -100 },
        upstreamSkips: false,
        upstreamWrong: false,
    },
];

for (const branch of BRANCHES) {
    test(`${branch.name}: LUT is derived from the header unconditionally`, () => {
        assert.deepEqual(modalityLutModule(branch.header), branch.lut);
    });

    test(`${branch.name}: the upstream skip predicate is pinned`, () => {
        assert.equal(upstreamWouldSkipRescale(branch.header), branch.upstreamSkips);
        assert.equal(upstreamIsWrongFor(branch.header), branch.upstreamWrong);
    });
}

test('F1 costs exactly 1024 HU on the commonest CBCT encoding', () => {
    // The concrete consequence, asserted as a number so it cannot be argued away.
    const lut = modalityLutModule({ scl_slope: 1, scl_inter: -1024 });
    assert.equal(applyModalityLut(0, lut), -1024); // air, correctly
    assert.equal(applyModalityLut(1024, lut), 0); // water
    // What upstream produces by skipping the loop: the raw stored value.
    assert.equal(0 - applyModalityLut(0, lut), 1024);
});

test('scl_slope == 0 means "no scaling defined", not "multiply by zero"', () => {
    // nifti1.h. Reading it literally would blank the entire volume to the intercept.
    assert.deepEqual(normalizeScaling({ scl_slope: 0, scl_inter: -1024 }), {
        rescaleSlope: 1,
        rescaleIntercept: -1024,
    });
    assert.equal(applyModalityLut(500, normalizeScaling({ scl_slope: 0, scl_inter: 0 })), 500);
});

test('missing, NaN and infinite fields fall back to identity, not to NaN voxels', () => {
    for (const header of [
        {},
        undefined,
        { scl_slope: NaN, scl_inter: NaN },
        { scl_slope: Infinity, scl_inter: -Infinity },
        { scl_slope: null, scl_inter: null },
        { scl_slope: '1', scl_inter: '-1024' }, // strings are not finite numbers
    ]) {
        const lut = modalityLutModule(header);
        assert.equal(lut.rescaleSlope, 1, JSON.stringify(header));
        assert.equal(lut.rescaleIntercept, 0, JSON.stringify(header));
        assert.ok(Number.isFinite(applyModalityLut(42, lut)));
    }
});

test('a negative slope is preserved -- it is legal and inverts the scale', () => {
    const lut = modalityLutModule({ scl_slope: -1, scl_inter: 100 });
    assert.deepEqual(lut, { rescaleSlope: -1, rescaleIntercept: 100 });
    assert.equal(applyModalityLut(40, lut), 60);
});

test('applying the LUT is a plain multiply-add with no skip path', () => {
    // There is deliberately no fast path for identity: that fast path is the bug.
    const identity = { rescaleSlope: 1, rescaleIntercept: 0 };
    assert.equal(applyModalityLut(-3000, identity), -3000);
    assert.equal(applyModalityLut(0, identity), 0);
});
