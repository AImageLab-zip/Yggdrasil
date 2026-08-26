import test from 'node:test';
import assert from 'node:assert/strict';

import {
    normalizeScaling,
    modalityLutModule,
    applyModalityLut,
    upstreamWouldSkipRescale,
    upstreamIsWrongFor,
    upstreamAppliesRescale,
    residualModalityLut,
    toStoredValue,
    upstreamRescaleMayOverflow,
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

// ---------------------------------------------------------------------------
// The residual LUT: which half of F1 a given volume actually landed in.
//
// F1 is usually stated as "the rescale is skipped". The operative hazard for Phase 3
// is narrower and worse: it is skipped for two of the four branches and applied for
// the other two, so cached scalar data is in raw units for some volumes and modality
// units for others, with nothing on the volume recording which.
// ---------------------------------------------------------------------------

for (const branch of BRANCHES) {
    test(`${branch.name}: the residual LUT complements what upstream did`, () => {
        assert.equal(upstreamAppliesRescale(branch.header), !branch.upstreamSkips);

        const residual = residualModalityLut(branch.header);
        if (branch.upstreamSkips) {
            // Upstream left the data raw, so the full header LUT is still outstanding.
            assert.deepEqual(residual, branch.lut);
        } else {
            // Upstream already scaled it; applying the header LUT again would double
            // the intercept.
            assert.deepEqual(residual, { rescaleSlope: 1, rescaleIntercept: 0 });
        }
    });
}

test('the residual LUT reaches the same modality value by either route', () => {
    // One physical voxel, 1500 HU, encoded two ways. Whatever upstream did to the
    // stored array, routing through the residual LUT must recover the same number --
    // that equivalence is what lets one windowing path serve both.
    const skipped = { scl_slope: 1, scl_inter: -1024 };
    assert.equal(applyModalityLut(2524, residualModalityLut(skipped)), 1500);

    const applied = { scl_slope: 2, scl_inter: -1024 };
    // Upstream already wrote 2 * 1262 - 1024 = 1500 into the array.
    assert.equal(applyModalityLut(1500, residualModalityLut(applied)), 1500);
});

test('toStoredValue inverts applyModalityLut, so absolute presets stay expressible', () => {
    // A bone window is 300/1500 HU whichever units the cached array happens to be in.
    const lut = residualModalityLut({ scl_slope: 1, scl_inter: -1024 });
    assert.equal(toStoredValue(-450, lut), 574);
    assert.equal(toStoredValue(1050, lut), 2074);

    for (const header of BRANCHES.map((branch) => branch.header)) {
        const residual = residualModalityLut(header);
        for (const hu of [-1000, -450, 0, 300, 1050, 3000]) {
            assert.ok(
                Math.abs(applyModalityLut(toStoredValue(hu, residual), residual) - hu) < 1e-9,
                `round trip failed for ${hu} under ${JSON.stringify(header)}`
            );
        }
    }
});

// ---------------------------------------------------------------------------
// The overflow consequence of the same code path.
// ---------------------------------------------------------------------------

test('an integral rescale into an Int16Array can overflow, and is reported', () => {
    // NIFTI_TYPE_INT16 with slope 2: upstream applies the rescale in place, into the
    // Int16Array it just allocated. A raw maximum above 16383 wraps silently.
    const header = { datatypeCode: 4, scl_slope: 2, scl_inter: -1024 };
    assert.equal(upstreamAppliesRescale(header), true);
    assert.equal(upstreamRescaleMayOverflow(header, { min: 0, max: 20000 }), true);
    assert.equal(upstreamRescaleMayOverflow(header, { min: 0, max: 10000 }), false);
});

test('overflow is not reported when upstream promotes the array to Float32', () => {
    // A fractional rescale takes the Float32Array branch, so nothing can wrap...
    assert.equal(
        upstreamRescaleMayOverflow({ datatypeCode: 4, scl_slope: 2.5, scl_inter: -1024 }, { min: 0, max: 1e9 }),
        false
    );
    // ...and so does a negative rescale, for the two unsigned types.
    assert.equal(
        upstreamRescaleMayOverflow({ datatypeCode: 512, scl_slope: 2, scl_inter: -1024 }, { min: 0, max: 1e9 }),
        false
    );
    // But a positive integral rescale on UINT16 does stay a Uint16Array.
    assert.equal(
        upstreamRescaleMayOverflow({ datatypeCode: 512, scl_slope: 2, scl_inter: 100 }, { min: 0, max: 60000 }),
        true
    );
});

test('overflow is never reported for a branch upstream skips entirely', () => {
    // Nothing is written back, so nothing can wrap -- however extreme the data.
    for (const header of [
        { datatypeCode: 4, scl_slope: 1, scl_inter: -1024 },
        { datatypeCode: 4, scl_slope: 2, scl_inter: 0 },
        { datatypeCode: 512, scl_slope: 1, scl_inter: 0 },
    ]) {
        assert.equal(upstreamRescaleMayOverflow(header, { min: -1e9, max: 1e9 }), false);
    }
});

test('the F16 over-allocation is what saves the INT8 branch, and is not "corrected"', () => {
    // `allocateScalarData('Int8Array')` allocates an Int16Array (finding F16). That
    // wastes half the cache budget, but it is also the reason a rescaled int8 volume
    // has room to grow -- so the limits here are Int16's, deliberately.
    const header = { datatypeCode: 256, scl_slope: 100, scl_inter: 50 };
    assert.equal(upstreamRescaleMayOverflow(header, { min: -128, max: 127 }), false);
    assert.equal(upstreamRescaleMayOverflow(header, { min: -128, max: 400 }), true);
});

test('an unknown or floating datatype never claims an overflow', () => {
    for (const datatypeCode of [16, 64, 8, 768, 1024, undefined]) {
        assert.equal(
            upstreamRescaleMayOverflow({ datatypeCode, scl_slope: 2, scl_inter: 5 }, { min: 0, max: 1e12 }),
            false
        );
    }
});
